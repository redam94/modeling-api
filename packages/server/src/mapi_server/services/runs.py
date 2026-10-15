"""Run rows: creation, state transitions (via :func:`mapi_spec.run.transition`), events.

Every state change goes through :func:`apply_transition`, which takes the row lock
(``SELECT … FOR UPDATE`` — the compare-and-set on the old state), asks the pure transition
table whether ``(state, trigger, actor)`` is legal, stamps timestamps and queues a
notification. Nothing else writes ``runs.state``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mapi_spec._types import Duration
from mapi_spec.api import ProgressSnapshot, RunCost, new_id
from mapi_spec.api import Run as RunDTO
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.executor import RunEventType, RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    Actor,
    FailureClass,
    RunState,
    RunTrigger,
    TransitionRule,
    transition,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.notify import notify_run

__all__ = [
    "Scope",
    "append_event",
    "apply_transition",
    "create_run",
    "load_run",
    "now",
    "progress_dto",
    "run_dto",
]


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Scope:
    """The tenant/project a request or job acts in (M1: always ``default``/``default``)."""

    tenant_id: str
    project_id: str


def progress_dto(p: m.RunProgress | None) -> ProgressSnapshot | None:
    if p is None:
        return None
    return ProgressSnapshot(
        seq=p.seq,
        phase=p.phase,
        fraction=p.fraction,
        message=p.message,
        eta_seconds=p.eta_seconds,
        chains=p.chains,
        updated_at=p.updated_at,
    )


def run_dto(run: m.Run, progress: m.RunProgress | None = None) -> RunDTO:
    return RunDTO(
        id=run.id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        kind=RunKind(run.kind),
        state=RunState(run.state),
        failure_class=FailureClass(run.failure_class) if run.failure_class else None,
        problem=Problem.model_validate(run.problem) if run.problem else None,
        attempt=run.attempt,
        spec_id=run.spec_id,
        spec_revision=run.spec_revision,
        spec_hash=run.spec_hash,
        model_version_id=run.model_version_id,
        dataset_id=run.dataset_id,
        output_model_version_id=run.output_model_version_id,
        parent_run_id=run.parent_run_id,
        retry_of=run.retry_of,
        resource_class=ResourceClass(run.resource_class),
        timeout=Duration(run.timeout_s),
        executor=run.executor,
        progress=progress_dto(progress)
        if progress is not None
        and progress.attempt == run.attempt  # never a previous attempt's progress
        and not RunState(run.state).terminal
        else None,
        cost=RunCost(),
        labels=run.labels,
        created_at=run.created_at,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.updated_at,
    )


async def load_run(
    session: AsyncSession, scope: Scope | None, run_id: str, *, for_update: bool = False
) -> m.Run:
    """The run row (scoped to ``scope`` unless ``None`` = orchestrator access)."""
    stmt = select(m.Run).where(m.Run.id == run_id)
    if scope is not None:
        stmt = stmt.where(m.Run.tenant_id == scope.tenant_id, m.Run.project_id == scope.project_id)
    if for_update:
        stmt = stmt.with_for_update()
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise NotFoundError("run", run_id)
    return run


async def create_run(
    session: AsyncSession,
    scope: Scope,
    *,
    kind: RunKind,
    resource_class: ResourceClass,
    timeout_s: int,
    payload: Mapping[str, Any],
    inputs: Mapping[str, Any] | None = None,
    labels: Mapping[str, str] | None = None,
    spec_id: str | None = None,
    spec_revision: int | None = None,
    spec_hash: str | None = None,
    model_version_id: str | None = None,
    dataset_id: str | None = None,
    dataset_version: int | None = None,
    retry_of: str | None = None,
    catalog_digest: str | None = None,
    checkpointable: bool = False,
) -> m.Run:
    """Insert a run in ``CREATED`` (callers then ``accept`` or ``reject`` it)."""
    if kind.is_parent:
        raise NotImplementedError(f"{kind.value!r} fan-out parents arrive in M4")
    ts = now()
    run = m.Run(
        id=new_id("run"),
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        kind=kind.value,
        state=RunState.CREATED.value,
        attempt=1,
        spec_id=spec_id,
        spec_revision=spec_revision,
        spec_hash=spec_hash,
        model_version_id=model_version_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        retry_of=retry_of,
        resource_class=resource_class.value,
        timeout_s=timeout_s,
        labels=dict(labels or {}),
        payload=dict(payload),
        inputs=dict(inputs or {}),
        catalog_digest=catalog_digest,
        checkpointable=checkpointable,
        infra_retries=0,
        finalize_attempts=0,
        last_event_id=0,
        created_at=ts,
        updated_at=ts,
    )
    session.add(run)
    await session.flush()
    await notify_run(session, run.id, "run")
    return run


async def apply_transition(
    session: AsyncSession,
    run: m.Run,
    trigger: RunTrigger,
    actor: Actor,
    *,
    problem: Problem | None = None,
) -> TransitionRule:
    """Apply ``trigger`` to a run that the caller loaded ``for_update``.

    Raises :class:`~mapi_spec.run.IllegalTransition` (409) for illegal edges. Stamps
    ``queued_at``/``started_at``/``finished_at``, bumps ``attempt`` for ``new_attempt``
    rules and records the failure class (and ``problem``) of failures.
    """
    rule = transition(run.state, trigger, actor=actor)
    if rule.noop:
        return rule
    ts = now()
    run.state = rule.target.value
    run.updated_at = ts
    if rule.new_attempt:
        run.attempt += 1
        run.lease_owner = None
        run.lease_expires_at = None
        run.last_event_at = None
    if rule.target is RunState.QUEUED:
        run.queued_at = ts
    if rule.target is RunState.RUNNING and run.started_at is None:
        run.started_at = ts
        if run.queued_at is not None:
            obs_metrics.record_time_to_running((ts - run.queued_at).total_seconds())
    obs_metrics.record_run_transition(rule.target.value)
    if rule.target.terminal:
        obs_metrics.record_run_terminal(run.kind, rule.target.value)
    if rule.failure_class is not None:
        run.failure_class = rule.failure_class.value
        run.problem = (
            problem or Problem.from_code(rule.failure_class.error_code, "the run failed")
        ).model_dump(mode="json", exclude_none=True)
    elif rule.target is RunState.REJECTED and problem is not None:
        run.problem = problem.model_dump(mode="json", exclude_none=True)
    elif rule.target is RunState.CANCELLED:
        run.problem = Problem.from_code(ErrorCode.CANCELLED, "the run was cancelled").model_dump(
            mode="json", exclude_none=True
        )
    if rule.target.terminal:
        run.finished_at = ts
        run.lease_owner = None
        run.lease_expires_at = None
        if rule.target is not RunState.SUCCEEDED:
            await _ingest_failed(session, run)
    await session.flush()
    await notify_run(session, run.id, "run")
    return rule


async def _ingest_failed(session: AsyncSession, run: m.Run) -> None:
    """A dataset version whose ingest run did not succeed is marked ``failed``."""
    if run.kind != RunKind.INGEST.value or run.dataset_id is None or run.dataset_version is None:
        return
    dv = await session.get(m.DatasetVersion, (run.dataset_id, run.dataset_version))
    if dv is None or dv.status != "ingesting":
        return
    dv.status = "failed"
    problem = Problem.model_validate(run.problem) if run.problem else None
    if problem is not None:
        dv.issues = [i.model_dump(mode="json") for i in problem.errors] or [
            {
                "code": problem.code.value,
                "severity": "error",
                "pointer": "",
                "message": problem.detail or problem.title,
            }
        ]


async def append_event(
    session: AsyncSession,
    run: m.Run,
    *,
    type: RunEventType,
    payload: Mapping[str, Any],
    attempt: int | None = None,
    seq: int = 0,
    ts: datetime | None = None,
    source: str = "platform",
) -> int:
    """Persist one event (the caller holds the run row lock) and return its SSE id."""
    run.last_event_id += 1
    event_id = run.last_event_id
    session.add(
        m.RunEvent(
            run_id=run.id,
            id=event_id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            attempt=attempt or run.attempt,
            seq=seq,
            source=source,
            type=type.value,
            ts=ts or now(),
            payload=dict(payload),
        )
    )
    await session.flush()
    await notify_run(session, run.id, "event", event_id)
    return event_id
