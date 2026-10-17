"""Workflow automation: cron schedules, dataset triggers and the orchestrator's workflow loop.

* **Schedules** store their ``next_run_at`` (:func:`mapi_spec.cron.next_fire` in the
  schedule's time zone). The loop starts a run for every due, active schedule under
  ``FOR UPDATE SKIP LOCKED`` and moves ``next_run_at`` on - a schedule fires at most once
  per due time however many orchestrators run, and a missed time (an outage) fires once,
  not once per missed slot.
* **Triggers** watch a dataset: when one of its versions becomes ready
  (:func:`dataset_version_ready`, called by the finalizer in the ingest's transaction)
  every active trigger on it records the version as *pending*; the loop fires it once the
  version is ``debounce_seconds`` old, binding the newest pending version to the trigger's
  input (versions arriving within the window collapse into one run). Datasets written by
  workflow nodes never fire triggers (no feedback loops).
* The loop also advances active workflow runs (:class:`WorkflowOrchestrator`) every
  ``workflow_tick_s``.

Runs started here are audited as ``workflow.run`` with the schedule or trigger as actor
detail; failures to start (an archived workflow, a quota) are recorded on the schedule or
trigger (``last_error``) and retried at its next time.

A schedule or trigger runs on behalf of whoever created it, so it fires only while that
principal may still run workflows in the project (:func:`creator_problem`): a creator
removed from the project, demoted below ``runner``, or whose API token was revoked or
expired, pauses it (``active: false`` with the reason in ``last_error``, audited). Its runs
are created by the person (a token's owner), with the token recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.cron import next_fire
from mapi_spec.errors import MapiError
from mapi_spec.tenancy import (
    Action,
    PrincipalKind,
    ProjectRole,
    ResourceKind,
    can,
    effective_role,
)
from mapi_spec.tokens import scope_allows
from mapi_spec.webhooks import WebhookEventType
from mapi_spec.workflow import WorkflowRunCreate, WorkflowTriggerEvent
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.identity import LOCAL_USER_ID, memberships_of
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.audit import write_audit
from mapi_server.services.runs import Scope, now
from mapi_server.services.webhooks.outbox import emit_event
from mapi_server.services.workflows.orchestrate import WorkflowOrchestrator
from mapi_server.services.workflows.runs import start_workflow_run

__all__ = [
    "Creator",
    "WorkflowLoop",
    "creator_problem",
    "dataset_version_ready",
    "fire_due_schedules",
    "fire_due_triggers",
    "schedule_next",
]

log = logging.getLogger(__name__)


def schedule_next(cron: str, tz: str, after: datetime) -> datetime:
    """The next fire time strictly after ``after``."""
    nxt = next_fire(cron, after, tz)
    probe = after
    while nxt <= after:  # backstop: a past next_run_at would fire on every loop tick
        probe += timedelta(minutes=30)
        nxt = next_fire(cron, probe, tz)
    return nxt


@dataclass(frozen=True, slots=True)
class Creator:
    """Who a schedule's or trigger's runs are started for."""

    user_id: str | None
    token_id: str | None


async def creator_problem(
    s: AsyncSession, ctx: ServerContext, tenant_id: str, project_id: str, created_by: str | None
) -> tuple[str | None, Creator]:
    """``(why the creator may no longer run workflows here, or None; the creator)``.

    ``created_by`` is the principal that made the schedule or trigger: a person, a service
    account or an API token (then its owner must still be a member and the token must be
    live, in this project, with the ``workflows:run`` scope)."""
    if not created_by:
        return None, Creator(None, None)  # made by the platform
    member, token_id, scopes = created_by, None, None
    if created_by.startswith("tok_"):
        tok = await s.get(m.ApiToken, created_by)
        if (
            tok is None
            or tok.revoked_at is not None
            or tok.expires_at <= now()
            or (tok.tenant_id, tok.project_id) != (tenant_id, project_id)
        ):
            return "the API token that created it was revoked or has expired", Creator(None, None)
        member, token_id, scopes = tok.owner_id, tok.id, list(tok.scopes or [])
    grants = await memberships_of(s, member, tenant_ids=[tenant_id])
    role = effective_role(grants, member, tenant_id, project_id)
    if role is None and member == LOCAL_USER_ID and not ctx.settings.multi_tenant:
        role = ProjectRole.ADMIN  # the single-tenant local admin (never a stored grant)
    if not can(role, Action.RUN, ResourceKind.WORKFLOW):
        return (
            "its creator is no longer allowed to run workflows in this project",
            Creator(None, None),
        )
    if scopes is not None and not scope_allows(scopes, Action.RUN.value, "workflow"):
        return "the API token that created it may not run workflows", Creator(None, None)
    return None, Creator(member, token_id)


async def _pause(
    s: AsyncSession,
    ctx: ServerContext,
    row: m.WorkflowScheduleRow | m.WorkflowTriggerRow,
    why: str,
    ts: datetime,
) -> None:
    """Stop a schedule or trigger whose creator may no longer run it (audited)."""
    row.active = False
    row.last_error = f"paused: {why}"[:500]
    schedule = isinstance(row, m.WorkflowScheduleRow)
    if isinstance(row, m.WorkflowScheduleRow):
        row.updated_at = ts
    log.info("%s %s paused: %s", "schedule" if schedule else "trigger", row.id, why)
    await write_audit(
        ctx.db,
        [
            make_audit_event(
                action=AuditAction.WORKFLOW_SCHEDULE_UPDATE
                if schedule
                else AuditAction.WORKFLOW_UPDATE,
                resource_kind=ResourceKind.WORKFLOW,
                outcome=AuditOutcome.SUCCESS,
                ts=ts,
                fingerprint_key=b"",
                tenant_id=row.tenant_id,
                project_id=row.project_id,
                actor_kind=PrincipalKind.SERVICE,
                actor_id="svc_workflows",
                resource_id=row.workflow_id,
                details={
                    "schedule_id" if schedule else "trigger_id": row.id,
                    "active": False,
                    "reason": why,
                    "created_by": row.created_by,
                },
            )
        ],
        session=s,
    )


async def fire_due_schedules(ctx: ServerContext, *, at: datetime | None = None) -> int:
    """Start a run for every due schedule; returns how many fired."""
    ts = at or now()
    fired = 0
    async with ctx.db.session() as s:
        due = list(
            (
                await s.execute(
                    select(m.WorkflowScheduleRow)
                    .where(
                        m.WorkflowScheduleRow.active.is_(True),
                        m.WorkflowScheduleRow.next_run_at <= ts,
                    )
                    .order_by(m.WorkflowScheduleRow.next_run_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for sch in due:
            sch.next_run_at = schedule_next(sch.cron, sch.timezone, ts)
            sch.updated_at = ts
            wf = await s.get(m.Workflow, sch.workflow_id)
            if wf is None:
                continue
            why, creator = await creator_problem(
                s, ctx, sch.tenant_id, sch.project_id, sch.created_by
            )
            if why is not None:
                await _pause(s, ctx, sch, why, ts)
                continue
            body = WorkflowRunCreate.model_validate(
                {"revision": sch.revision, "params": sch.params, "inputs": sch.inputs}
            )
            try:
                async with s.begin_nested():
                    wr = await start_workflow_run(
                        s,
                        ctx,
                        Scope(sch.tenant_id, sch.project_id),
                        wf,
                        body,
                        trigger=WorkflowTriggerEvent.SCHEDULE,
                        trigger_id=sch.id,
                        created_by=creator.user_id,
                        token_id=creator.token_id,
                    )
            except MapiError as exc:
                sch.last_error = exc.detail[:500]
                log.info("schedule %s could not start a run: %s", sch.id, exc.detail)
                continue
            sch.last_run_id = wr.run_id
            sch.last_run_at = ts
            sch.last_error = None
            fired += 1
    return fired


async def dataset_version_ready(session: AsyncSession, ds: m.Dataset, dv: m.DatasetVersion) -> int:
    """A dataset version just became ready (an ingest finished): mark the triggers that
    watch the dataset as pending and emit ``dataset.version.created``. Returns how many
    triggers are now pending. Runs in the caller's transaction."""
    labels = ds.labels or {}
    await emit_event(
        session,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        event_type=WebhookEventType.DATASET_VERSION_CREATED,
        data={
            "dataset_id": ds.id,
            "ref": f"dataset://{ds.project_id}/{ds.name}@v{dv.version}",
            "version": dv.version,
            "sha256": dv.sha256,
        },
    )
    if "workflow" in labels:
        return 0  # written by a workflow node: never re-trigger workflows
    # one atomic UPDATE per trigger row: it waits for a firing loop holding the row and then
    # sees the row it committed (pending cleared), so the version and its pending_since are
    # always written together and a version arriving while the trigger fires is never lost
    t = m.WorkflowTriggerRow.__table__.c
    stmt = (
        update(m.WorkflowTriggerRow)
        .where(
            t.tenant_id == ds.tenant_id,
            t.project_id == ds.project_id,
            t.dataset_name == ds.name,
            t.active.is_(True),
        )
        .values(
            pending_version=func.greatest(func.coalesce(t.pending_version, 0), dv.version),
            pending_since=func.coalesce(t.pending_since, now()),
        )
        .returning(t.id)
        .execution_options(synchronize_session=False)
    )
    return len(list((await session.execute(stmt)).scalars()))


async def fire_due_triggers(ctx: ServerContext, *, at: datetime | None = None) -> int:
    """Fire every pending trigger whose debounce window has passed."""
    ts = at or now()
    fired = 0
    async with ctx.db.session() as s:
        pending = list(
            (
                await s.execute(
                    select(m.WorkflowTriggerRow)
                    .where(m.WorkflowTriggerRow.pending_since.is_not(None))
                    .order_by(m.WorkflowTriggerRow.pending_since)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for trg in pending:
            assert trg.pending_since is not None
            if trg.pending_since + timedelta(seconds=trg.debounce_seconds) > ts:
                continue
            version = trg.pending_version
            trg.pending_since = None
            trg.pending_version = None
            if not trg.active or version is None:
                continue
            wf = await s.get(m.Workflow, trg.workflow_id)
            if wf is None:
                continue
            why, creator = await creator_problem(
                s, ctx, trg.tenant_id, trg.project_id, trg.created_by
            )
            if why is not None:
                await _pause(s, ctx, trg, why, ts)
                continue
            ref = f"dataset://{trg.project_id}/{trg.dataset_name}@v{version}"
            body = WorkflowRunCreate.model_validate(
                {"revision": trg.revision, "params": trg.params, "inputs": {trg.input: ref}}
            )
            try:
                async with s.begin_nested():
                    wr = await start_workflow_run(
                        s,
                        ctx,
                        Scope(trg.tenant_id, trg.project_id),
                        wf,
                        body,
                        trigger=WorkflowTriggerEvent.DATASET_VERSION_CREATED,
                        trigger_id=trg.id,
                        created_by=creator.user_id,
                        token_id=creator.token_id,
                    )
            except MapiError as exc:
                trg.last_error = exc.detail[:500]
                log.info("trigger %s could not start a run: %s", trg.id, exc.detail)
                continue
            trg.last_fired_at = ts
            trg.last_run_id = wr.run_id
            trg.last_error = None
            fired += 1
    return fired


class WorkflowLoop:
    """The orchestrator's workflow loop: schedules, triggers and active runs."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx
        self.orchestrator = WorkflowOrchestrator(ctx)

    async def once(self) -> None:
        await fire_due_schedules(self.ctx)
        await fire_due_triggers(self.ctx)
        await self.orchestrator.tick_all()

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("workflow loop started (every %ss)", self.ctx.settings.workflow_tick_s)
        while not stop.is_set():
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("workflow loop pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.ctx.settings.workflow_tick_s)
