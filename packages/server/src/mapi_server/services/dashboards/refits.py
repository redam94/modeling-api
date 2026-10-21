"""Refit requests made with an actuals upload (v0.16, plan §10.4).

A client user, a share link (with uploads) or a member may tick "Ask the analyst to refit
with these numbers" when the dashboard allows it (``actuals.allow_refit_requests`` and a
``refit_workflow_id``). :func:`request_refit` records an ``open`` request for the upload's
readout; a newer request supersedes the open one (at most one per dashboard). Nothing runs.

Only an editor with runner rights decides (``…/refit-requests/{id}:approve`` / ``:reject``):

* :func:`approve` publishes the linked readout, then ingests the uploaded numbers (the
  stored inline input, re-validated by a sandboxed ingest run with the same limits as any
  upload) as a new version of the dataset the pinned version was fitted on;
* :func:`advance` (the dashboard loop) starts ``refit_workflow_id`` as the approver once
  that version is ready - with its dataset input set to the new version, through the usual
  workflow admission (the approver's rights now, governance, active-run and cost caps) - or
  fails the request when the ingest failed. The workflow's success then appends a refresh
  readout through :func:`~mapi_server.services.dashboards.readouts.on_workflow_succeeded`;
* :func:`reject` closes the request; nothing starts.
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
from collections.abc import Sequence
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.dashboards import DashboardRefitRequest, RefitRequestDecision
from mapi_spec.endpoints import EndpointSchemaSnapshot
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.ingest import DimMapping, IngestMapping, VarMapping
from mapi_spec.refs import DatasetRef
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.schema import Role
from mapi_spec.storage import quarantine_key
from mapi_spec.tenancy import ResourceKind
from mapi_spec.workflow import WorkflowRunCreate, WorkflowSpec, WorkflowTriggerEvent
from mapi_spec.workflow_nodes import PortType
from mapi_spec.xrjson import MAX_XRJSON_BYTES, XrJsonDataset
from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError, ProblemWithHeaders
from mapi_server.jsonlimits import loads_limited
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.access import DashboardContext, load_dashboard, revision_spec
from mapi_server.services.dashboards.readouts import _lock_dashboard, review
from mapi_server.services.endpoints import live_capabilities_snapshot
from mapi_server.services.quotas import admit_storage
from mapi_server.services.runs import Scope, now
from mapi_server.services.submit import fit_dataset_version, submit_ingest

__all__ = [
    "advance",
    "approve",
    "list_requests",
    "on_workflow_ended",
    "refit_dto",
    "reject",
    "request_refit",
    "upload_csv",
]

log = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES: Final = RESOURCE_CLASSES[ResourceClass.L].max_dataset_bytes


def refit_dto(row: m.DashboardRefitRequest) -> DashboardRefitRequest:
    return DashboardRefitRequest(
        id=row.id,
        dashboard_id=row.dashboard_id,
        readout_seq=row.readout_seq,
        status=row.status,
        requested_by_kind=row.requested_by_kind,
        requested_by=row.requested_by,
        created_at=row.created_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        note=row.note,
        workflow_run_id=row.workflow_run_id,
        dataset_version_id=row.dataset_version_id,
        problem=Problem.model_validate(row.problem) if row.problem else None,
    )


# =================================================================================== request


async def request_refit(
    ctx: ServerContext,
    session: AsyncSession,
    dctx: DashboardContext,
    dash: m.Dashboard,
    readout: m.DashboardReadout,
) -> m.DashboardRefitRequest:
    """Record an ``open`` request for ``readout`` (an actuals upload); the dashboard's open
    request, if any, is ``superseded``. Audited (the requester as the actor)."""
    if not dctx.inputs.refit_requests:
        raise MapiError(
            "asking for a refit is not available on this dashboard",
            code=ErrorCode.DASHBOARD_INPUT_DISABLED,
        )
    # the supersede and the insert run under the dashboard row lock, so two requests never
    # both insert an open row (uq_dashboard_refit_requests_open)
    await _lock_dashboard(session, dash.id)
    ts = now()
    await session.execute(
        update(m.DashboardRefitRequest)
        .where(
            m.DashboardRefitRequest.dashboard_id == dash.id,
            m.DashboardRefitRequest.status == "open",
        )
        .values(status="superseded", decided_at=ts)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    row = m.DashboardRefitRequest(
        id=new_id("drr"),
        tenant_id=dash.tenant_id,
        project_id=dash.project_id,
        dashboard_id=dash.id,
        readout_seq=readout.seq,
        status="open",
        requested_by_kind=dctx.principal_kind,
        requested_by=dctx.principal_id,
        created_at=ts,
    )
    session.add(row)
    await session.flush()
    auth = ctx.request
    details: dict[str, JsonValue] = {"request_id": row.id, "seq": readout.seq}
    if dctx.principal_kind != "share" and auth is not None and auth.context is not None:
        ev = auth.audit(ctx, AuditAction.DASHBOARD_REFIT_REQUEST, ResourceKind.DASHBOARD,
                        resource_id=dash.id, details=details)  # fmt: skip
    else:
        ev = make_audit_event(
            action=AuditAction.DASHBOARD_REFIT_REQUEST,
            resource_kind=ResourceKind.DASHBOARD,
            outcome=AuditOutcome.SUCCESS,
            ts=ts,
            fingerprint_key=ctx.secrets.audit_key,
            tenant_id=dash.tenant_id,
            project_id=dash.project_id,
            actor_id=dctx.principal_id,
            resource_id=dash.id,
            details={**details, "share_id": dctx.principal_id},
        )
    await write_audit(ctx.db, [ev], session=session)
    return row


async def list_requests(
    session: AsyncSession,
    ctx: ServerContext,
    dashboard_id: str,
    *,
    status: str | None,
    limit: int,
    before: str | None,
) -> tuple[list[m.DashboardRefitRequest], bool]:
    """Requests newest first (``before``: the id of the last row of the previous page)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    stmt = select(m.DashboardRefitRequest).where(m.DashboardRefitRequest.dashboard_id == row.id)
    if status is not None:
        stmt = stmt.where(m.DashboardRefitRequest.status == status)
    if before is not None:
        stmt = stmt.where(m.DashboardRefitRequest.id < before)
    rows = list(
        (
            await session.execute(stmt.order_by(m.DashboardRefitRequest.id.desc()).limit(limit + 1))
        ).scalars()
    )
    return rows[:limit], len(rows) > limit


# ================================================================================== decisions


async def _open_request(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, request_id: str
) -> tuple[m.Dashboard, m.DashboardRefitRequest]:
    dash = await load_dashboard(session, ctx, dashboard_id)
    req = (
        await session.execute(
            select(m.DashboardRefitRequest)
            .where(
                m.DashboardRefitRequest.id == request_id,
                m.DashboardRefitRequest.dashboard_id == dash.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None:
        raise NotFoundError("refit request", request_id)
    if req.status != "open":
        raise Conflict(f"the refit request is {req.status}; only an open request is decided")
    return dash, req


def _decider(ctx: ServerContext) -> str:
    auth = ctx.request
    return (auth.user_id or auth.principal_id or "") if auth is not None else "system"


async def _audit_decision(
    ctx: ServerContext, session: AsyncSession, req: m.DashboardRefitRequest, decision: str
) -> None:
    if ctx.request is None:
        return
    await write_audit(
        ctx.db,
        [
            ctx.request.audit(
                ctx,
                AuditAction.DASHBOARD_REFIT_DECIDE,
                ResourceKind.DASHBOARD,
                resource_id=req.dashboard_id,
                details={
                    "request_id": req.id,
                    "decision": decision,
                    "seq": req.readout_seq,
                    "dataset_version": req.dataset_version_id,
                },
            )
        ],
        session=session,
    )


async def reject(
    session: AsyncSession,
    ctx: ServerContext,
    dashboard_id: str,
    request_id: str,
    body: RefitRequestDecision,
) -> m.DashboardRefitRequest:
    """``…:reject``: close an open request. Nothing starts. Audited."""
    _, req = await _open_request(session, ctx, dashboard_id, request_id)
    req.status = "rejected"
    req.decided_by = _decider(ctx)
    req.decided_at = now()
    req.note = body.note
    await session.flush()
    await _audit_decision(ctx, session, req, "rejected")
    return req


def _cell(node: Any, at: Sequence[int]) -> Any:
    for k in at:
        if not isinstance(node, list) or k >= len(node):
            return None
        node = node[k]
    return node


def upload_csv(payload: XrJsonDataset, dims: Sequence[str]) -> bytes:
    """The inline dataset as a dense CSV table: one column per dim (``dims`` order), then one
    per variable, one row per combination of the dims' labels."""
    labels: list[list[Any]] = []
    for d in dims:
        coord = payload.coords.get(d)
        data = coord.data if coord is not None and isinstance(coord.data, list) else None
        if data is None:
            size = payload.dims.get(d) or 0
            data = list(range(size))
        labels.append(list(data))
    names = list(payload.data_vars)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([*dims, *names])
    for combo in itertools.product(*[range(len(ls)) for ls in labels]):
        cells: list[Any] = [labels[i][k] for i, k in enumerate(combo)]
        for n in names:
            var = payload.data_vars[n]
            at = [combo[dims.index(d)] for d in var.dims if d in dims]
            v = _cell(var.data, at) if len(at) == len(var.dims) else None
            cells.append("" if v is None else v)
        writer.writerow(cells)
    return out.getvalue().encode("utf-8")


def _mapping(
    snapshot: EndpointSchemaSnapshot, payload: XrJsonDataset
) -> tuple[list[str], IngestMapping]:
    schema = snapshot.input_schema
    dims = [d for d in schema.dims if any(d in v.dims for v in payload.data_vars.values())]
    return dims, IngestMapping(
        format="csv",
        dims={d: DimMapping(column=d, spec=schema.dims[d]) for d in dims},
        vars={
            n: VarMapping(
                column=n,
                role=schema.vars[n].role if n in schema.vars else Role.COVARIATE,
                units=schema.vars[n].units if n in schema.vars else None,
                missing="allow",
            )
            for n in payload.data_vars
        },
    )


async def approve(
    session: AsyncSession,
    ctx: ServerContext,
    dashboard_id: str,
    request_id: str,
    body: RefitRequestDecision,
) -> m.DashboardRefitRequest:
    """``…:approve`` (an editor with runner rights; the route checks both): publish the
    request's readout, then ingest its numbers as a new version of the dataset the pinned
    version was fitted on (a sandboxed ingest run). The refit workflow starts as the approver
    once that version is ready (:func:`advance`). Audited."""
    dash, req = await _open_request(session, ctx, dashboard_id, request_id)
    if dash.status != "published" or dash.published_revision_id is None:
        raise Conflict("the dashboard is not published")
    rev = await session.get(m.DashboardRevision, dash.published_revision_id)
    assert rev is not None
    spec = revision_spec(rev)
    wf_id = spec.actuals.refit_workflow_id
    if not spec.actuals.allow_refit_requests or wf_id is None:
        raise Conflict("refit requests are turned off on this dashboard")
    wf = await session.get(m.Workflow, wf_id)
    if (
        wf is None
        or wf.archived
        or (wf.tenant_id, wf.project_id) != (dash.tenant_id, dash.project_id)
    ):
        raise MapiError(
            f"the refit workflow {wf_id!r} is not available in this project",
            code=ErrorCode.WORKFLOW_NOT_RUNNABLE,
        )
    readout = (
        await session.execute(
            select(m.DashboardReadout).where(
                m.DashboardReadout.dashboard_id == dash.id,
                m.DashboardReadout.seq == req.readout_seq,
            )
        )
    ).scalar_one_or_none()
    if readout is None or not readout.input_ref:
        raise Conflict("the request's upload is no longer available")
    if readout.visibility == "pending_review":
        await review(session, ctx, dash, readout.seq, "published")
    mv = await session.get(m.ModelVersion, readout.model_version_id)
    assert mv is not None
    fit = await fit_dataset_version(session, mv)
    ds = await session.get(m.Dataset, fit.dataset_id, with_for_update=True)
    assert ds is not None
    ref = readout.input_ref
    try:
        raw = await ctx.store.get(
            str(ref["bucket"]), str(ref["name"]), generation=int(ref["generation"]),
            max_bytes=MAX_XRJSON_BYTES,
        )  # fmt: skip
        payload = XrJsonDataset.model_validate(loads_limited(raw))
    except (MapiError, ValidationError, ValueError, KeyError, TypeError):
        raise Conflict("the request's upload is no longer available") from None
    snapshot = await live_capabilities_snapshot(session, ctx.catalogs, mv)
    dims, mapping = _mapping(snapshot, payload)
    body_bytes = upload_csv(payload, dims)
    if len(body_bytes) > _MAX_UPLOAD_BYTES:
        raise MapiError("the upload is too large to ingest", code=ErrorCode.PAYLOAD_TOO_LARGE)
    async with ctx.db.session() as system:
        decision = await admit_storage(
            session, system, tenant_id=ds.tenant_id, nbytes=len(body_bytes), now=now()
        )
    if not decision.allowed:
        assert decision.code is not None
        raise ProblemWithHeaders(
            decision.message or "storage quota exceeded",
            code=decision.code,
            headers=decision.headers(),
        )
    upload_id = new_id("upl")
    name = quarantine_key(ds.tenant_id, upload_id)
    meta = await ctx.store.put(ctx.buckets.quarantine, name, body_bytes, content_type="text/csv")
    ts = now()
    version = ds.next_version
    ds.next_version += 1
    ds.updated_at = ts
    upload = m.Upload(
        id=upload_id,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        dataset_id=ds.id,
        filename=f"dashboard-{dash.name}-{req.readout_seq}.csv"[:255],
        content_type="text/csv",
        size_bytes=len(body_bytes),
        status="consumed",
        bucket=ctx.buckets.quarantine,
        object_name=name,
        upload={},
        expires_at=ts,
        consumed_by_version=version,
        created_at=ts,
    )
    session.add(upload)
    dv = m.DatasetVersion(
        dataset_id=ds.id,
        version=version,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        status="ingesting",
        upload_id=upload_id,
        sources=[upload_id],
        mapping=mapping.model_dump(mode="json"),
        message=f"Numbers from dashboard {dash.name} (readout {req.readout_seq})"[:2000],
        objects={},
        issues=[],
        created_at=ts,
    )
    session.add(dv)
    await session.flush()
    run = await submit_ingest(
        session,
        ctx,
        dataset=ds,
        version=version,
        uploads=[(upload, meta.generation)],
        mapping=mapping.model_dump(mode="json"),
    )
    dv.ingest_run_id = run.id
    req.status = "approved"
    req.decided_by = _decider(ctx)
    req.decided_at = ts
    req.note = body.note
    req.dataset_version_id = f"dataset://{ds.project_id}/{ds.name}@v{version}"  # ≤ 215 chars
    await session.flush()
    await _audit_decision(ctx, session, req, "approved")
    return req


# ==================================================================================== advance


def _dataset_input(spec: WorkflowSpec, dataset_name: str) -> str | None:
    """The workflow input that takes the dashboard's dataset: the dataset input whose
    default names it, else the only dataset input."""
    datasets = [n for n, d in spec.inputs.items() if d.type is PortType.DATASET]
    for n in datasets:
        default = spec.inputs[n].default
        if default is not None:
            try:
                if DatasetRef.parse(default).name == dataset_name:
                    return n
            except ValueError:
                continue
    return datasets[0] if len(datasets) == 1 else None


async def on_workflow_ended(session: AsyncSession, wr: m.WorkflowRunRow) -> int:
    """A workflow run failed or was cancelled: the approved refit requests it ran become
    ``failed`` with the run's problem (the uploader's "refit requested" then says so).
    Returns how many."""
    rows = list(
        (
            await session.execute(
                select(m.DashboardRefitRequest).where(
                    m.DashboardRefitRequest.tenant_id == wr.tenant_id,
                    m.DashboardRefitRequest.workflow_run_id == wr.run_id,
                    m.DashboardRefitRequest.status == "approved",
                )
            )
        ).scalars()
    )
    error = wr.error or {}
    try:
        code = ErrorCode(str(error.get("code")))
    except ValueError:
        code = ErrorCode.CANCELLED if wr.status == "cancelled" else ErrorCode.INTERNAL_ERROR
    detail = (
        "the refit workflow run was cancelled"
        if wr.status == "cancelled"
        else f"the refit workflow run failed: {str(error.get('message') or code.value)[:400]}"
    )
    for req in rows:
        await _fail(req, code, detail)
    if rows:
        await session.flush()
    return len(rows)


async def _fail(req: m.DashboardRefitRequest, code: ErrorCode, detail: str) -> None:
    req.status = "failed"
    req.problem = Problem.from_code(code, detail).model_dump(mode="json", exclude_none=True)


async def advance(ctx: ServerContext, *, limit: int = 20) -> int:
    """The dashboard loop: start the refit workflow of every approved request whose dataset
    version became ready (as the approver, if they still may run workflows here), or fail
    the request when the ingest failed. Returns how many changed."""
    from mapi_server.services.workflows.automation import creator_problem
    from mapi_server.services.workflows.runs import start_workflow_run
    from mapi_server.services.workflows.store import load_revision

    changed = 0
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.DashboardRefitRequest)
                    .where(
                        m.DashboardRefitRequest.status == "approved",
                        m.DashboardRefitRequest.workflow_run_id.is_(None),
                        m.DashboardRefitRequest.dataset_version_id.is_not(None),
                    )
                    .order_by(m.DashboardRefitRequest.decided_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for req in rows:
            try:
                ref = DatasetRef.parse(str(req.dataset_version_id))
            except ValueError:  # never abort the pass (and the prune after it) for one row
                await _fail(req, ErrorCode.REF_UNRESOLVED, "the dataset version ref is invalid")
                changed += 1
                continue
            ds = (
                await s.execute(
                    select(m.Dataset).where(
                        m.Dataset.tenant_id == req.tenant_id,
                        m.Dataset.project_id == req.project_id,
                        m.Dataset.name == ref.name,
                    )
                )
            ).scalar_one_or_none()
            version = ref.version_number
            dv = await s.get(m.DatasetVersion, (ds.id, version)) if ds and version else None
            if ds is None or dv is None or dv.status == "failed":
                await _fail(req, ErrorCode.REF_UNRESOLVED, "the numbers could not be ingested")
                changed += 1
                continue
            if dv.status != "ready":
                continue
            dash = await s.get(m.Dashboard, req.dashboard_id)
            if dash is None or dash.status != "published":
                await _fail(req, ErrorCode.DASHBOARD_NOT_PUBLISHED, "the dashboard is archived")
                changed += 1
                continue
            rev = (
                await s.get(m.DashboardRevision, dash.published_revision_id)
                if dash is not None and dash.published_revision_id
                else None
            )
            wf_id = revision_spec(rev).actuals.refit_workflow_id if rev is not None else None
            wf = await s.get(m.Workflow, wf_id) if wf_id else None
            if wf is None or wf.archived:
                await _fail(req, ErrorCode.WORKFLOW_NOT_RUNNABLE, "the refit workflow is gone")
                changed += 1
                continue
            why, creator = await creator_problem(
                s, ctx, req.tenant_id, req.project_id, req.decided_by
            )
            if why is not None:
                await _fail(req, ErrorCode.FORBIDDEN, f"the refit cannot start: {why}")
                changed += 1
                continue
            try:
                wrev = await load_revision(s, wf, None)
                wspec = WorkflowSpec.model_validate(wrev.document)
                port = _dataset_input(wspec, ds.name)
                if port is None:
                    raise MapiError(
                        "the refit workflow has no input for the dashboard's dataset",
                        code=ErrorCode.WORKFLOW_NOT_RUNNABLE,
                    )
                pin = f"dataset://{ds.project_id}/{ds.name}@v{dv.version}"
                async with s.begin_nested():
                    wr = await start_workflow_run(
                        s,
                        ctx,
                        Scope(req.tenant_id, req.project_id),
                        wf,
                        WorkflowRunCreate.model_validate({"inputs": {port: pin}}),
                        trigger=WorkflowTriggerEvent.MANUAL,
                        created_by=creator.user_id,
                        token_id=creator.token_id,
                    )
            except MapiError as exc:
                await _fail(req, exc.code, exc.detail[:500])
                changed += 1
                continue
            req.workflow_run_id = wr.run_id
            changed += 1
            log.info("refit request %s started workflow run %s", req.id, wr.run_id)
    return changed
