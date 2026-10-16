"""Workflow runs: starting a run (a ``workflow`` parent run), cancelling it, and its views.

A workflow run is a ``runs`` row of kind ``workflow`` (a parent: it never executes on an
executor, the dispatcher and the reconciler skip it) plus its orchestration state in
``workflow_runs`` and one ``workflow_node_runs`` row per node. Starting a run checks the
revision is runnable, the parameters and the inputs (dataset and model references are
pinned to versions now, so a re-run sees the same data), and the project's limit of active
workflow runs; the orchestrator's workflow loop does the rest
(:mod:`mapi_server.services.workflows.orchestrate`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction
from mapi_spec.cost import RunCost
from mapi_spec.errors import ErrorCode, Issue, MapiError, json_pointer
from mapi_spec.executor import RunKind
from mapi_spec.refs import DatasetRef, EndpointRef, ModelVersionRef, parse_model_target
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.tenancy import ResourceKind
from mapi_spec.workflow import (
    BranchDecision,
    NodeError,
    NodeOutputRef,
    NodeRun,
    NodeRunStatus,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunStatus,
    WorkflowSpec,
    WorkflowTriggerEvent,
    check_run_params,
    topological_order,
)
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeKind, PortType
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, ProblemWithHeaders
from mapi_server.services.audit import write_audit
from mapi_server.services.cost import run_costs
from mapi_server.services.notify import notify_run
from mapi_server.services.runs import Scope, now
from mapi_server.services.specs import resolve_dataset, resolve_model_version
from mapi_server.services.workflows.store import load_revision, workflow_ref

__all__ = [
    "ACTIVE_STATUSES",
    "PARENT_STATE",
    "node_run_dto",
    "request_cancel",
    "set_parent_state",
    "start_workflow_run",
    "sum_costs",
    "workflow_run_dto",
]

ACTIVE_STATUSES = (
    WorkflowRunStatus.QUEUED.value,
    WorkflowRunStatus.RUNNING.value,
    WorkflowRunStatus.CANCELLING.value,
)

PARENT_STATE: dict[WorkflowRunStatus, RunState] = {
    WorkflowRunStatus.QUEUED: RunState.QUEUED,
    WorkflowRunStatus.RUNNING: RunState.RUNNING,
    WorkflowRunStatus.SUCCEEDED: RunState.SUCCEEDED,
    WorkflowRunStatus.FAILED: RunState.FAILED,
    WorkflowRunStatus.CANCELLING: RunState.CANCELLING,
    WorkflowRunStatus.CANCELLED: RunState.CANCELLED,
}
"""The ``runs`` row state of a workflow parent run for each workflow status."""


def _invalid(pointer: str, message: str) -> MapiError:
    return MapiError(
        message,
        code=ErrorCode.WORKFLOW_INVALID,
        issues=[Issue(code=ErrorCode.WORKFLOW_INVALID, pointer=pointer, message=message)],
    )


async def _resolve_inputs(
    session: AsyncSession, scope: Scope, spec: WorkflowSpec, given: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, NodeOutputRef], dict[str, str]]:
    """``(pinned refs, NodeOutputRefs, content identities)`` of the run's inputs."""
    unknown = sorted(set(given) - set(spec.inputs))
    if unknown:
        raise _invalid(json_pointer("inputs", unknown[0]), f"unknown input {unknown[0]!r}")
    pinned: dict[str, str] = {}
    refs: dict[str, NodeOutputRef] = {}
    identities: dict[str, str] = {}
    for name, decl in spec.inputs.items():
        ptr = json_pointer("inputs", name)
        text = given.get(name) or decl.default
        if text is None:
            if decl.required:
                raise _invalid(ptr, f"input {name!r} is required")
            continue
        if decl.type is PortType.DATASET:
            try:
                dref = DatasetRef.parse(text)
            except ValueError as exc:
                raise _invalid(ptr, f"input {name!r}: {exc}") from None
            dv, why = await resolve_dataset(session, scope, dref)
            if dv is None:
                raise MapiError(
                    why or f"cannot resolve {text}", code=ErrorCode.REF_UNRESOLVED, pointer=ptr
                )
            ds = await session.get(m.Dataset, dv.dataset_id)
            assert ds is not None
            pin = f"dataset://{ds.project_id}/{ds.name}@v{dv.version}"
            pinned[name] = pin
            refs[name] = NodeOutputRef(type=PortType.DATASET, ref=pin)
            identities[name] = f"dataset:{dv.sha256 or pin}"
        elif decl.type is PortType.MODEL_VERSION:
            try:
                target = parse_model_target(text)
            except ValueError as exc:
                raise _invalid(ptr, f"input {name!r}: {exc}") from None
            assert isinstance(target, ModelVersionRef | EndpointRef)
            mv, why = await resolve_model_version(session, scope, target)
            if mv is None or mv.status != "ready":
                raise MapiError(
                    why or f"model {text} is not ready", code=ErrorCode.REF_UNRESOLVED, pointer=ptr
                )
            model = await session.get(m.Model, mv.model_id)
            assert model is not None
            pin = f"model://{mv.project_id}/{model.name}@v{mv.version}"
            pinned[name] = pin
            refs[name] = NodeOutputRef(type=PortType.MODEL_VERSION, ref=pin)
            identities[name] = f"model:{mv.id}"
        else:
            raise _invalid(ptr, f"inputs of type {decl.type.value} cannot be given by reference")
    return pinned, refs, identities


async def start_workflow_run(
    session: AsyncSession,
    ctx: ServerContext,
    scope: Scope,
    wf: m.Workflow,
    body: WorkflowRunCreate,
    *,
    trigger: WorkflowTriggerEvent = WorkflowTriggerEvent.MANUAL,
    trigger_id: str | None = None,
    parent_run_id: str | None = None,
    parent_node_id: str | None = None,
    created_by: str | None = None,
    token_id: str | None = None,
) -> m.WorkflowRunRow:
    """Create a workflow run (``queued``); the orchestrator starts its nodes."""
    if wf.archived:
        raise Conflict(f"workflow {workflow_ref(wf)} is archived")
    rev = await load_revision(session, wf, body.revision)
    if not rev.valid:
        raise MapiError(
            f"revision {rev.revision} of {workflow_ref(wf)} has errors and cannot run",
            code=ErrorCode.WORKFLOW_NOT_RUNNABLE,
        )
    spec = WorkflowSpec.model_validate(rev.document)
    params = check_run_params(spec, dict(body.params))
    pinned, refs, identities = await _resolve_inputs(session, scope, spec, body.inputs)
    active = (
        await session.execute(
            select(func.count()).where(
                m.WorkflowRunRow.tenant_id == scope.tenant_id,
                m.WorkflowRunRow.project_id == scope.project_id,
                m.WorkflowRunRow.status.in_(ACTIVE_STATUSES),
            )
        )
    ).scalar_one()
    limit = ctx.settings.workflow_max_active_runs
    if active >= limit and parent_run_id is None:
        raise ProblemWithHeaders(
            f"the project already has {active} workflow runs going (the limit is {limit}); "
            "wait for one to finish",
            code=ErrorCode.QUOTA_EXCEEDED,
            headers={"Retry-After": "60"},
        )
    ts = now()
    run_id = new_id("run")
    rc = body.resource_class or spec.execution.resource_class
    run = m.Run(
        id=run_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        kind=RunKind.WORKFLOW.value,
        state=RunState.QUEUED.value,
        attempt=1,
        spec_hash=rev.spec_hash,
        parent_run_id=parent_run_id,
        resource_class=rc.value,
        timeout_s=spec.execution.timeout.seconds,
        labels={"workflow": wf.name[:63]},
        payload={"workflow_id": wf.id, "revision": rev.revision, "workflow": workflow_ref(wf)},
        inputs={},
        checkpointable=False,
        infra_retries=0,
        finalize_attempts=0,
        finalize={},
        last_event_id=0,
        priority="batch",
        task_count=1,
        token_id=token_id,
        created_by=created_by,
        created_at=ts,
        updated_at=ts,
        queued_at=ts,
    )
    session.add(run)
    await session.flush()
    wr = m.WorkflowRunRow(
        run_id=run_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        workflow_id=wf.id,
        revision=rev.revision,
        status=WorkflowRunStatus.QUEUED.value,
        trigger=trigger.value,
        trigger_id=trigger_id,
        parent_run_id=parent_run_id,
        parent_node_id=parent_node_id,
        params=params,
        inputs=pinned,
        input_refs={k: v.model_dump(mode="json") for k, v in refs.items()},
        input_identities=identities,
        decisions=[],
        outputs={},
        use_cache=body.use_cache,
        cancel_requested=False,
        resource_class=body.resource_class.value if body.resource_class else None,
        seed=int.from_bytes(run_id.encode()[-4:], "big") % (2**31),
        last_event_id=0,
        deadline=ts + timedelta(seconds=spec.execution.timeout.seconds),
        created_by=created_by,
        created_at=ts,
        updated_at=ts,
    )
    session.add(wr)
    await session.flush()  # the node rows reference it
    for i, nid in enumerate(topological_order(spec)):
        node = spec.node(nid)
        assert node is not None
        impl = NODE_IMPLEMENTATIONS[node.impl_id]
        session.add(
            m.WorkflowNodeRunRow(
                run_id=run_id,
                node_id=nid,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                position=i,
                use=impl.ref,
                kind=impl.kind.value,
                status=NodeRunStatus.PENDING.value,
                attempt=0,
                child_runs=[],
                params={},
                outputs={},
                updated_at=ts,
            )
        )
    await session.flush()
    await notify_run(session, run_id, "run")
    details: dict[str, JsonValue] = {
        "run_id": run_id,
        "revision": rev.revision,
        "trigger": trigger.value,
        "trigger_id": trigger_id,
    }
    if ctx.request is not None:
        ev = ctx.request.audit(
            ctx,
            AuditAction.WORKFLOW_RUN,
            ResourceKind.WORKFLOW,
            resource_id=wf.id,
            details=details,
        )
    else:  # started by a schedule, a trigger or a parent workflow: the platform acts
        from mapi_spec.audit import AuditOutcome, make_audit_event
        from mapi_spec.tenancy import PrincipalKind

        ev = make_audit_event(
            action=AuditAction.WORKFLOW_RUN,
            resource_kind=ResourceKind.WORKFLOW,
            outcome=AuditOutcome.SUCCESS,
            ts=now(),
            fingerprint_key=b"",
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            actor_kind=PrincipalKind.SERVICE,
            actor_id="svc_workflows",
            resource_id=wf.id,
            details={**details, "created_by": created_by},
        )
    await write_audit(ctx.db, [ev], session=session)
    return wr


def set_parent_state(run: m.Run, status: WorkflowRunStatus, error: NodeError | None = None) -> None:
    """Mirror a workflow status onto its ``runs`` row (parents have no execution, so the
    executor-driven transition table does not apply)."""
    from mapi_spec.errors import Problem
    from mapi_spec.run import FailureClass

    target = PARENT_STATE[status]
    ts = now()
    if run.state == target.value:
        return
    run.state = target.value
    run.updated_at = ts
    if target is RunState.RUNNING and run.started_at is None:
        run.started_at = ts
    if target.terminal:
        run.finished_at = ts
    if target is RunState.FAILED:
        code = error.code if error is not None else ErrorCode.USER_CODE_ERROR
        fc = FailureClass.from_error_code(code)
        run.failure_class = fc.value
        run.problem = Problem.from_code(
            fc.error_code, (error.message if error else "the workflow run failed")[:2000]
        ).model_dump(mode="json", exclude_none=True)
    elif target is RunState.CANCELLED:
        run.problem = Problem.from_code(
            ErrorCode.CANCELLED, "the workflow run was cancelled"
        ).model_dump(mode="json", exclude_none=True)


async def request_cancel(session: AsyncSession, wr: m.WorkflowRunRow) -> None:
    """Flag a run for cancellation (the orchestrator stops its children)."""
    status = WorkflowRunStatus(wr.status)
    if status.terminal or wr.cancel_requested:
        return
    wr.cancel_requested = True
    wr.status = WorkflowRunStatus.CANCELLING.value
    wr.updated_at = now()
    await session.flush()
    await notify_run(session, wr.run_id, "run")


def node_run_dto(row: m.WorkflowNodeRunRow) -> NodeRun:
    return NodeRun(
        node_id=row.node_id,
        use=row.use,
        kind=NodeKind(row.kind),
        status=NodeRunStatus(row.status),
        attempt=row.attempt,
        run_id=row.child_run_id,
        child_runs=[str(c) for c in row.child_runs],
        cache_key=row.cache_key,
        cached_from=row.cached_from,
        params=row.params,
        result=row.result,
        outputs={k: NodeOutputRef.model_validate(v) for k, v in row.outputs.items()},
        skipped_reason=row.skipped_reason,
        error=NodeError.model_validate(row.error) if row.error else None,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def sum_costs(costs: Sequence[RunCost]) -> RunCost | None:
    """The roll-up of a workflow run's children."""
    if not costs:
        return None

    def total(attr: str) -> float | None:
        vals = [getattr(c, attr) for c in costs if getattr(c, attr) is not None]
        return float(sum(vals)) if vals else None

    micros = [c.amount_micros for c in costs if c.amount_micros is not None]
    currency = next((c.currency for c in costs if c.currency), None)
    return RunCost(
        vcpu_seconds=total("vcpu_seconds"),
        gib_seconds=total("gib_seconds"),
        usd=total("usd") if currency == "USD" else None,
        estimated=any(c.estimated for c in costs),
        amount_micros=int(sum(micros)) if micros else None,
        currency=currency,
        price_version=next((c.price_version for c in costs if c.price_version), None),
        estimate_micros=int(sum(c.estimate_micros or 0 for c in costs)) or None,
        attempts_billed=sum(c.attempts_billed for c in costs),
    )


async def workflow_run_dto(
    session: AsyncSession, wr: m.WorkflowRunRow, *, with_cost: bool = True
) -> WorkflowRun:
    nodes = list(
        (
            await session.execute(
                select(m.WorkflowNodeRunRow)
                .where(m.WorkflowNodeRunRow.run_id == wr.run_id)
                .order_by(m.WorkflowNodeRunRow.position)
            )
        ).scalars()
    )
    cost: RunCost | None = None
    if with_cost:
        child_ids = sorted({str(c) for n in nodes for c in n.child_runs})
        if child_ids:
            rows = list(
                (await session.execute(select(m.Run).where(m.Run.id.in_(child_ids)))).scalars()
            )
            cost = sum_costs(list((await run_costs(session, rows)).values()))
    params: dict[str, JsonValue] = dict(wr.params)
    return WorkflowRun(
        run_id=wr.run_id,
        workflow_id=wr.workflow_id,
        revision=wr.revision,
        tenant_id=wr.tenant_id,
        project_id=wr.project_id,
        status=WorkflowRunStatus(wr.status),
        trigger=WorkflowTriggerEvent(wr.trigger),
        trigger_id=wr.trigger_id,
        parent_run_id=wr.parent_run_id,
        params=params,
        inputs={k: str(v) for k, v in wr.inputs.items()},
        nodes=[node_run_dto(n) for n in nodes],
        decisions=[BranchDecision.model_validate(d) for d in wr.decisions],
        outputs={k: NodeOutputRef.model_validate(v) for k, v in wr.outputs.items()},
        cost=cost,
        error=NodeError.model_validate(wr.error) if wr.error else None,
        created_at=wr.created_at,
        started_at=wr.started_at,
        finished_at=wr.finished_at,
        created_by=wr.created_by,
    )


def resource_class_of(value: str | None, default: ResourceClass) -> ResourceClass:
    return ResourceClass(value) if value else default
