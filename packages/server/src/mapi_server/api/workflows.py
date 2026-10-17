"""Workflows (M3.5): documents and revisions, runs with their nodes and events, schedules,
dataset triggers, the template gallery and the node catalog.

Every write is audited; runs are started with an ``Idempotency-Key``; node events stream as
SSE (``Last-Event-ID`` resumes). Workflow documents are validated statically on every save
(JSON pointers to every problem); a revision with errors is stored but cannot run.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from mapi_spec.api import LAST_EVENT_ID_HEADER, ArtifactLink, Page, new_id, parse_last_event_id
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.cron import check_cron, check_timezone
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import FIT_BUNDLE
from mapi_spec.refs import DatasetRef, ModelVersionRef
from mapi_spec.tenancy import ResourceKind
from mapi_spec.workflow import (
    NodeRun,
    NodeRunStatus,
    ScheduleCreate,
    ScheduleUpdate,
    TriggerCreate,
    Workflow,
    WorkflowCreate,
    WorkflowEvent,
    WorkflowRevision,
    WorkflowRevisionCreate,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowRunStatus,
    WorkflowSchedule,
    WorkflowTrigger,
    WorkflowTriggerEvent,
    WorkflowUpdate,
    WorkflowValidateRequest,
    WorkflowValidation,
)
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation, NodeKind
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES, WorkflowTemplate
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError as DBIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Idempotency,
    IdempotencyKeyHeader,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.problems import responses_for
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import write_audit
from mapi_server.services.bundles import artifact_link, verified_bundle_object
from mapi_server.services.quotas import charge_egress
from mapi_server.services.runs import now
from mapi_server.services.specs import resolve_dataset, resolve_model_version
from mapi_server.services.workflows.automation import schedule_next
from mapi_server.services.workflows.events import event_dto, workflow_event_stream
from mapi_server.services.workflows.runs import (
    node_run_dto,
    request_cancel,
    start_workflow_run,
    workflow_run_dto,
)
from mapi_server.services.workflows.store import (
    document_of,
    load_revision,
    load_workflow,
    new_workflow,
    revision_dto,
    revision_row,
    validate_document,
    workflow_dto,
)

__all__ = ["catalog_router", "router", "runs_router", "templates_router"]

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])
runs_router = APIRouter(prefix="/v1/workflow-runs", tags=["workflows"])
templates_router = APIRouter(prefix="/v1/workflow-templates", tags=["workflows"])
catalog_router = APIRouter(prefix="/v1/catalog/workflow-nodes", tags=["catalog"])

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$"
WorkflowId = Annotated[str, Path(pattern=_ID, examples=["wf_01jaf3e1f2g3h4j5k6m7n8p9q0"])]
RunId = Annotated[str, Path(pattern=_ID, examples=["run_01jaf3q2x8m9v7k5h4g3f2e1d0"])]
ScheduleId = Annotated[str, Path(pattern=_ID, examples=["sch_01jaf3f1g2h3j4k5m6n7p8q9r0"])]
TriggerId = Annotated[str, Path(pattern=_ID, examples=["trg_01jaf3g1h2j3k4m5n6p7q8r9s0"])]
NodeId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,62}$", examples=["adf"])]


async def _audit(
    ctx: ServerContext,
    session: AsyncSession | None,
    action: AuditAction,
    resource_id: str | None,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    details: dict[str, JsonValue] | None = None,
) -> None:
    if ctx.request is None:
        return
    ev = ctx.request.audit(
        ctx, action, ResourceKind.WORKFLOW, outcome, resource_id=resource_id, details=details
    )
    await write_audit(ctx.db, [ev], session=session)


def _principal(ctx: ServerContext) -> str | None:
    return ctx.request.principal_id if ctx.request is not None else None


def _page[T](items: list[T], rows: list[Any], limit: int, key: Any) -> tuple[list[T], str | None]:
    more = len(rows) > limit
    return items[:limit], encode_cursor(key(rows[limit - 1])) if more and rows else None


# =========================================================================== workflows


@router.get(
    "",
    response_model=Page[Workflow],
    operation_id="list_workflows",
    summary="Workflows of the project",
    responses=responses_for("list_workflows"),
)
async def list_workflows(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    archived: bool | None = None,
) -> Page[Workflow]:
    stmt = select(m.Workflow).where(
        m.Workflow.tenant_id == ctx.scope.tenant_id, m.Workflow.project_id == ctx.scope.project_id
    )
    if archived is not None:
        stmt = stmt.where(m.Workflow.archived.is_(archived))
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Workflow.id < str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.Workflow.id.desc()).limit(limit + 1))).scalars()
    )
    items, nxt = _page([workflow_dto(r) for r in rows], rows, limit, lambda r: r.id)
    return Page[Workflow](items=items, next_cursor=nxt)


@router.post(
    "",
    response_model=Workflow,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workflow",
    summary="Create a workflow from a document, YAML text or a gallery template",
    responses=responses_for("create_workflow"),
)
async def create_workflow(ctx: Ctx, session: Session, body: WorkflowCreate) -> JSONResponse:
    """Stores revision 1 with its static validation (a document with errors is stored but
    cannot run until a valid revision is added). The workflow's name is
    ``metadata.name``; ``409 CONFLICT`` when the project already has one of that name."""
    doc, template = document_of(document=body.document, text=body.text, template=body.template)
    if template is not None:
        doc["metadata"]["project"] = ctx.scope.project_id
        doc["metadata"].setdefault("labels", {})["workflow-template"] = template
    validation = await validate_document(session, ctx.scope, ctx.catalogs, doc)
    wf = new_workflow(ctx.scope, doc, template=template, created_by=_principal(ctx))
    try:
        async with session.begin_nested():
            session.add(wf)
    except DBIntegrityError:
        raise Conflict(
            f"a workflow named {wf.name!r} already exists; add a revision instead"
        ) from None
    rev = revision_row(wf, 1, doc, validation, message=body.message, created_by=_principal(ctx))
    session.add(rev)
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.WORKFLOW_CREATE,
        wf.id,
        details={"name": wf.name, "valid": validation.valid, "template": template},
    )
    return JSONResponse(workflow_dto(wf).model_dump(mode="json"), status_code=201)


@router.post(
    ":validate",
    response_model=WorkflowValidation,
    operation_id="validate_workflow",
    summary="Static checks of a workflow document (JSON pointers); stores nothing",
    responses=responses_for("validate_workflow"),
)
async def validate_workflow(
    ctx: Ctx, session: Session, body: WorkflowValidateRequest
) -> WorkflowValidation:
    """200 even for invalid documents: the verdict is ``valid`` + ``issues``."""
    doc, _ = document_of(document=body.document, text=body.text)
    return await validate_document(session, ctx.scope, ctx.catalogs, doc)


@router.get(
    "/{workflow_id}",
    response_model=Workflow,
    operation_id="get_workflow",
    summary="A workflow",
    responses=responses_for("get_workflow"),
)
async def get_workflow(ctx: Ctx, session: Session, workflow_id: WorkflowId) -> Workflow:
    return workflow_dto(await load_workflow(session, ctx.scope, workflow_id))


@router.patch(
    "/{workflow_id}",
    response_model=Workflow,
    operation_id="update_workflow",
    summary="Change the description or archive it",
    responses=responses_for("update_workflow"),
)
async def update_workflow(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, body: WorkflowUpdate
) -> Workflow:
    wf = await load_workflow(session, ctx.scope, workflow_id, for_update=True)
    changed: dict[str, JsonValue] = {}
    if body.description is not None:
        wf.description = body.description
        changed["description"] = True
    if body.archived is not None:
        wf.archived = body.archived
        changed["archived"] = body.archived
        if body.archived:  # archived workflows stop running on their own
            for row in (
                await session.execute(
                    select(m.WorkflowScheduleRow).where(m.WorkflowScheduleRow.workflow_id == wf.id)
                )
            ).scalars():
                row.active = False
    wf.updated_at = now()
    await session.flush()
    await _audit(ctx, session, AuditAction.WORKFLOW_UPDATE, wf.id, details=changed)
    return workflow_dto(wf)


@router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_workflow",
    summary="Delete a workflow (its runs, schedules and triggers go with it)",
    responses=responses_for("delete_workflow"),
)
async def delete_workflow(ctx: Ctx, session: Session, workflow_id: WorkflowId) -> Response:
    """``409 CONFLICT`` while one of its runs is still going (cancel it first)."""
    wf = await load_workflow(session, ctx.scope, workflow_id, for_update=True)
    active = (
        await session.execute(
            select(func.count()).where(
                m.WorkflowRunRow.workflow_id == wf.id,
                m.WorkflowRunRow.status.in_(["queued", "running", "cancelling"]),
            )
        )
    ).scalar_one()
    if active:
        raise Conflict(f"the workflow has {active} run(s) going; cancel them first")
    await session.delete(wf)
    await session.flush()
    await _audit(ctx, session, AuditAction.WORKFLOW_DELETE, workflow_id, details={"name": wf.name})
    return Response(status_code=204)


# =========================================================================== revisions


@router.get(
    "/{workflow_id}/revisions",
    response_model=Page[WorkflowRevision],
    operation_id="list_workflow_revisions",
    summary="Revisions, newest first",
    responses=responses_for("list_workflow_revisions"),
)
async def list_workflow_revisions(
    ctx: Ctx,
    session: Session,
    workflow_id: WorkflowId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[WorkflowRevision]:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    stmt = select(m.WorkflowRevisionRow).where(m.WorkflowRevisionRow.workflow_id == wf.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.WorkflowRevisionRow.revision < int(after))
    rows = list(
        (
            await session.execute(
                stmt.order_by(m.WorkflowRevisionRow.revision.desc()).limit(limit + 1)
            )
        ).scalars()
    )
    items, nxt = _page([revision_dto(wf, r) for r in rows], rows, limit, lambda r: r.revision)
    return Page[WorkflowRevision](items=items, next_cursor=nxt)


@router.post(
    "/{workflow_id}/revisions",
    response_model=WorkflowRevision,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workflow_revision",
    summary="Save a new revision (validated; errors make it unrunnable)",
    responses=responses_for("create_workflow_revision"),
)
async def create_workflow_revision(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, body: WorkflowRevisionCreate
) -> JSONResponse:
    """``expected_revision`` guards concurrent edits (``409`` when another revision was
    saved in between). The document's ``metadata.name`` must stay the workflow's name."""
    wf = await load_workflow(session, ctx.scope, workflow_id, for_update=True)
    if body.expected_revision is not None and body.expected_revision != wf.latest_revision:
        raise Conflict(
            f"the latest revision is {wf.latest_revision}, not {body.expected_revision}; "
            "reload and save again"
        )
    doc, _ = document_of(document=body.document, text=body.text)
    meta = doc.get("metadata")
    if isinstance(meta, dict) and meta.get("name") not in (None, wf.name):
        raise MapiError(
            f"metadata.name must stay {wf.name!r} (create a new workflow to rename)",
            code=ErrorCode.WORKFLOW_INVALID,
            pointer="/metadata/name",
        )
    validation = await validate_document(session, ctx.scope, ctx.catalogs, doc)
    wf.latest_revision += 1
    wf.updated_at = now()
    rev = revision_row(
        wf, wf.latest_revision, doc, validation, message=body.message, created_by=_principal(ctx)
    )
    session.add(rev)
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.WORKFLOW_REVISION,
        wf.id,
        details={"revision": rev.revision, "valid": validation.valid},
    )
    return JSONResponse(revision_dto(wf, rev).model_dump(mode="json"), status_code=201)


@router.get(
    "/{workflow_id}/revisions/{revision}",
    response_model=WorkflowRevision,
    operation_id="get_workflow_revision",
    summary="A revision with its validation",
    responses=responses_for("get_workflow_revision"),
)
async def get_workflow_revision(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, revision: Annotated[int, Path(ge=1)]
) -> WorkflowRevision:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    return revision_dto(wf, await load_revision(session, wf, revision))


# ================================================================================ runs


async def _run_view(session: AsyncSession, run_id: str, status_code: int) -> JSONResponse:
    wr = await session.get(m.WorkflowRunRow, run_id)
    assert wr is not None
    view = await workflow_run_dto(session, wr)
    return JSONResponse(view.model_dump(mode="json"), status_code=status_code)


@router.post(
    "/{workflow_id}/runs",
    response_model=WorkflowRun,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="start_workflow_run",
    summary="Run a revision (a workflow parent run)",
    responses=responses_for(
        "start_workflow_run",
        extra={200: {"model": WorkflowRun, "description": "Idempotent replay"}},
    ),
)
async def start_run(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    request: Request,
    workflow_id: WorkflowId,
    body: WorkflowRunCreate,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Starts a run of a revision (default: the latest). Inputs are references
    (``dataset://project/name@latest`` is pinned to the version ready now); parameters are
    checked against the workflow's ``params``. The orchestrator then starts every node that
    can run; follow it with ``GET /v1/workflow-runs/{run_id}/events``."""
    wf = await load_workflow(session, ctx.scope, workflow_id)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST /v1/workflows/{workflow_id}/runs", await request.json())
    if replay is not None:
        return await _run_view(session, replay.resource_id, 200)
    wr = await start_workflow_run(
        session,
        ctx,
        ctx.scope,
        wf,
        body,
        trigger=WorkflowTriggerEvent.MANUAL,
        created_by=auth.user_id,
        token_id=auth.token.id if auth.token is not None else None,
    )
    await idem.complete("workflow_run", wr.run_id, 202)
    return await _run_view(session, wr.run_id, 202)


@router.get(
    "/{workflow_id}/runs",
    response_model=Page[WorkflowRun],
    operation_id="list_workflow_runs",
    summary="Runs of a workflow, newest first",
    responses=responses_for("list_workflow_runs"),
)
async def list_workflow_runs(
    ctx: Ctx,
    session: Session,
    workflow_id: WorkflowId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_: Annotated[WorkflowRunStatus | None, Query(alias="status")] = None,
) -> Page[WorkflowRun]:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    stmt = select(m.WorkflowRunRow).where(m.WorkflowRunRow.workflow_id == wf.id)
    if status_ is not None:
        stmt = stmt.where(m.WorkflowRunRow.status == status_.value)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.WorkflowRunRow.run_id < str(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.WorkflowRunRow.run_id.desc()).limit(limit + 1))
        ).scalars()
    )
    views = [await workflow_run_dto(session, r, with_cost=False) for r in rows[:limit]]
    items, nxt = _page(views, rows, limit, lambda r: r.run_id)
    return Page[WorkflowRun](items=items, next_cursor=nxt)


async def _load_run(ctx: ServerContext, session: AsyncSession, run_id: str) -> m.WorkflowRunRow:
    wr = await session.get(m.WorkflowRunRow, run_id)
    if wr is None or (wr.tenant_id, wr.project_id) != (ctx.scope.tenant_id, ctx.scope.project_id):
        raise NotFoundError("workflow run", run_id)
    return wr


@runs_router.get(
    "/{run_id}",
    response_model=WorkflowRun,
    operation_id="get_workflow_run",
    summary="A workflow run with every node, decision and output",
    responses=responses_for("get_workflow_run"),
)
async def get_workflow_run(ctx: Ctx, session: Session, run_id: RunId) -> WorkflowRun:
    return await workflow_run_dto(session, await _load_run(ctx, session, run_id))


@runs_router.post(
    "/{run_id}:cancel",
    response_model=WorkflowRun,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="cancel_workflow_run",
    summary="Cancel a workflow run and its running children",
    responses=responses_for("cancel_workflow_run"),
)
async def cancel_workflow_run(ctx: Ctx, session: Session, run_id: RunId) -> JSONResponse:
    """Idempotent: ``cancelling`` until every running child stopped, then ``cancelled``
    (a finished run is returned as it is)."""
    wr = await _load_run(ctx, session, run_id)
    if await request_cancel(session, wr):  # locks the row: a run finished meanwhile stays so
        await _audit(
            ctx,
            session,
            AuditAction.WORKFLOW_RUN_CANCEL,
            wr.workflow_id,
            details={"run_id": run_id},
        )
    return await _run_view(session, run_id, 202)


@runs_router.get(
    "/{run_id}/events",
    response_model=Page[WorkflowEvent],
    operation_id="workflow_run_events",
    summary="Workflow run events (SSE, Last-Event-ID)",
    responses={
        **responses_for("workflow_run_events"),
        200: {"content": {"text/event-stream": {}}, "model": Page[WorkflowEvent]},
    },
)
async def workflow_run_events(
    ctx: Ctx,
    request: Request,
    run_id: RunId,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Limit = DEFAULT_LIMIT,
    last_event_id: Annotated[str | None, Header(alias=LAST_EVENT_ID_HEADER)] = None,
) -> Response:
    """``Accept: text/event-stream`` → a live stream: ``snapshot`` (the run), then one frame
    per event (``event:`` is its type, ``id:`` its sequence number), then ``end``; anything
    else → a JSON page of events after ``after``."""
    async with ctx.data_session() as session:
        wr = await _load_run(ctx, session, run_id)
        if "text/event-stream" not in request.headers.get("accept", ""):
            stmt = select(m.WorkflowEventRow).where(m.WorkflowEventRow.run_id == run_id)
            if after is not None:
                stmt = stmt.where(m.WorkflowEventRow.id > after)
            rows = list(
                (
                    await session.execute(stmt.order_by(m.WorkflowEventRow.id).limit(limit + 1))
                ).scalars()
            )
            items, _ = _page([event_dto(r) for r in rows], rows, limit, lambda r: r.id)
            more = len(rows) > limit
            page = Page[WorkflowEvent](
                items=items, next_cursor=str(rows[limit - 1].id) if more else None
            )
            return JSONResponse(page.model_dump(mode="json"))
        snapshot = (await workflow_run_dto(session, wr, with_cost=False)).model_dump_json()
    resume = parse_last_event_id(last_event_id)
    if resume is None and after is not None:
        resume = after
    return StreamingResponse(
        workflow_event_stream(ctx, request, run_id, resume or 0, snapshot),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@runs_router.get(
    "/{run_id}/nodes",
    response_model=Page[NodeRun],
    operation_id="list_workflow_node_runs",
    summary="The node runs of a workflow run",
    responses=responses_for("list_workflow_node_runs"),
)
async def list_workflow_node_runs(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_: Annotated[NodeRunStatus | None, Query(alias="status")] = None,
) -> Page[NodeRun]:
    await _load_run(ctx, session, run_id)
    stmt = select(m.WorkflowNodeRunRow).where(m.WorkflowNodeRunRow.run_id == run_id)
    if status_ is not None:
        stmt = stmt.where(m.WorkflowNodeRunRow.status == status_.value)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.WorkflowNodeRunRow.position > int(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.WorkflowNodeRunRow.position).limit(limit + 1))
        ).scalars()
    )
    items, nxt = _page([node_run_dto(r) for r in rows], rows, limit, lambda r: r.position)
    return Page[NodeRun](items=items, next_cursor=nxt)


async def _node(
    ctx: ServerContext, session: AsyncSession, run_id: str, node_id: str
) -> m.WorkflowNodeRunRow:
    await _load_run(ctx, session, run_id)
    row = await session.get(m.WorkflowNodeRunRow, (run_id, node_id))
    if row is None:
        raise NotFoundError("node", node_id)
    return row


@runs_router.get(
    "/{run_id}/nodes/{node_id}",
    response_model=NodeRun,
    operation_id="get_workflow_node_run",
    summary="One node's status, result and outputs",
    responses=responses_for("get_workflow_node_run"),
)
async def get_workflow_node_run(
    ctx: Ctx, session: Session, run_id: RunId, node_id: NodeId
) -> NodeRun:
    return node_run_dto(await _node(ctx, session, run_id, node_id))


@runs_router.get(
    "/{run_id}/nodes/{node_id}/outputs/{port}",
    response_model=ArtifactLink,
    operation_id="get_workflow_node_output",
    summary="A signed link to a node output's object",
    responses=responses_for("get_workflow_node_output"),
)
async def get_workflow_node_output(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    node_id: NodeId,
    port: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,62}$")],
) -> ArtifactLink:
    """A dataset output links to its ``data.zarr.zip``, a model output to its bundle; JSON
    outputs (test results, metrics, tables, reports) are inline in the node and have no
    object (``404``)."""
    row = await _node(ctx, session, run_id, node_id)
    out = (row.outputs or {}).get(port)
    if not isinstance(out, dict) or not isinstance(out.get("ref"), str):
        raise NotFoundError("output object", f"{node_id}.{port}")
    ref = str(out["ref"])
    if ref.startswith("dataset://"):
        dv, _ = await resolve_dataset(session, ctx.scope, DatasetRef.parse(ref))
        obj = dv.objects.get("data.zarr.zip") if dv is not None else None
        if dv is None or obj is None:
            raise NotFoundError("output object", ref)
        link = await artifact_link(
            ctx.store,
            name="data.zarr.zip",
            bucket=obj["bucket"],
            key=obj["name"],
            generation=int(obj["generation"]),
        )
        await charge_egress(ctx, dv.tenant_id, link.size or 0)
        return link
    if ref.startswith("model://"):
        mv, _ = await resolve_model_version(session, ctx.scope, ModelVersionRef.parse(ref))
        if mv is None:
            raise NotFoundError("output object", ref)
        bundle = await verified_bundle_object(ctx.store, ctx.signer, mv, FIT_BUNDLE)
        link = await artifact_link(
            ctx.store,
            name=FIT_BUNDLE,
            bucket=bundle.bucket,
            key=bundle.key,
            generation=bundle.generation,
        )
        await charge_egress(ctx, mv.tenant_id, link.size or 0)
        return link
    raise NotFoundError("output object", ref)


# ========================================================================== schedules


def _schedule_dto(row: m.WorkflowScheduleRow) -> WorkflowSchedule:
    return WorkflowSchedule(
        id=row.id,
        workflow_id=row.workflow_id,
        cron=row.cron,
        timezone=row.timezone,
        revision=row.revision,
        params=row.params,
        inputs=row.inputs,
        active=row.active,
        description=row.description,
        next_run_at=row.next_run_at,
        last_run_id=row.last_run_id,
        last_run_at=row.last_run_at,
        created_at=row.created_at,
        created_by=row.created_by,
    )


async def _check_quota(
    ctx: ServerContext, session: AsyncSession, table: type[Any], limit: int, what: str
) -> None:
    count = (
        await session.execute(
            select(func.count()).where(
                table.tenant_id == ctx.scope.tenant_id,
                table.project_id == ctx.scope.project_id,
                table.active.is_(True),
            )
        )
    ).scalar_one()
    if count >= limit:
        raise ProblemWithHeaders(
            f"the project already has {count} active {what} (the limit is {limit})",
            code=ErrorCode.QUOTA_EXCEEDED,
        )


@router.get(
    "/{workflow_id}/schedules",
    response_model=Page[WorkflowSchedule],
    operation_id="list_workflow_schedules",
    summary="Cron schedules",
    responses=responses_for("list_workflow_schedules"),
)
async def list_workflow_schedules(
    ctx: Ctx,
    session: Session,
    workflow_id: WorkflowId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[WorkflowSchedule]:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    stmt = select(m.WorkflowScheduleRow).where(m.WorkflowScheduleRow.workflow_id == wf.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.WorkflowScheduleRow.id < str(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.WorkflowScheduleRow.id.desc()).limit(limit + 1))
        ).scalars()
    )
    items, nxt = _page([_schedule_dto(r) for r in rows], rows, limit, lambda r: r.id)
    return Page[WorkflowSchedule](items=items, next_cursor=nxt)


@router.post(
    "/{workflow_id}/schedules",
    response_model=WorkflowSchedule,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workflow_schedule",
    summary="Run the workflow on a cron schedule",
    responses=responses_for("create_workflow_schedule"),
)
async def create_workflow_schedule(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, body: ScheduleCreate
) -> JSONResponse:
    """Five-field cron in an IANA time zone, at most every 15 minutes
    (``SCHEDULE_INVALID``). Inputs may say ``@latest``: each run pins the version ready then.
    At most ``MAPI_WORKFLOW_MAX_SCHEDULES`` active schedules per project (429)."""
    wf = await load_workflow(session, ctx.scope, workflow_id)
    if body.active:
        await _check_quota(
            ctx, session, m.WorkflowScheduleRow, ctx.settings.workflow_max_schedules, "schedules"
        )
    ts = now()
    row = m.WorkflowScheduleRow(
        id=new_id("sch"),
        tenant_id=ctx.scope.tenant_id,
        project_id=ctx.scope.project_id,
        workflow_id=wf.id,
        cron=body.cron,
        timezone=body.timezone,
        revision=body.revision,
        params=dict(body.params),
        inputs=dict(body.inputs),
        active=body.active,
        description=body.description,
        next_run_at=schedule_next(body.cron, body.timezone, ts),
        created_by=_principal(ctx),
        created_at=ts,
        updated_at=ts,
    )
    session.add(row)
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.WORKFLOW_SCHEDULE_CREATE,
        wf.id,
        details={"schedule_id": row.id, "cron": row.cron, "timezone": row.timezone},
    )
    return JSONResponse(_schedule_dto(row).model_dump(mode="json"), status_code=201)


async def _schedule(
    ctx: ServerContext, session: AsyncSession, workflow_id: str, schedule_id: str
) -> tuple[m.Workflow, m.WorkflowScheduleRow]:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    row = await session.get(m.WorkflowScheduleRow, schedule_id, with_for_update=True)
    if row is None or row.workflow_id != wf.id:
        raise NotFoundError("schedule", schedule_id)
    return wf, row


@router.patch(
    "/{workflow_id}/schedules/{schedule_id}",
    response_model=WorkflowSchedule,
    operation_id="update_workflow_schedule",
    summary="Change or pause a schedule",
    responses=responses_for("update_workflow_schedule"),
)
async def update_workflow_schedule(
    ctx: Ctx,
    session: Session,
    workflow_id: WorkflowId,
    schedule_id: ScheduleId,
    body: ScheduleUpdate,
) -> WorkflowSchedule:
    wf, row = await _schedule(ctx, session, workflow_id, schedule_id)
    if body.active and not row.active:
        await _check_quota(
            ctx, session, m.WorkflowScheduleRow, ctx.settings.workflow_max_schedules, "schedules"
        )
    data = body.model_dump(exclude_unset=True)
    for key in ("cron", "timezone", "revision", "params", "inputs", "active"):
        if key in data:
            setattr(row, key, data[key])
    try:
        check_cron(row.cron)
        check_timezone(row.timezone)
    except MapiError:
        raise
    row.next_run_at = schedule_next(row.cron, row.timezone, now())
    row.updated_at = now()
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.WORKFLOW_SCHEDULE_UPDATE,
        wf.id,
        details={"schedule_id": row.id, "fields": list(sorted(data))},
    )
    return _schedule_dto(row)


@router.delete(
    "/{workflow_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_workflow_schedule",
    summary="Delete a schedule",
    responses=responses_for("delete_workflow_schedule"),
)
async def delete_workflow_schedule(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, schedule_id: ScheduleId
) -> Response:
    wf, row = await _schedule(ctx, session, workflow_id, schedule_id)
    await session.delete(row)
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.WORKFLOW_SCHEDULE_DELETE,
        wf.id,
        details={"schedule_id": schedule_id},
    )
    return Response(status_code=204)


# =========================================================================== triggers


def _trigger_dto(row: m.WorkflowTriggerRow) -> WorkflowTrigger:
    return WorkflowTrigger(
        id=row.id,
        workflow_id=row.workflow_id,
        event="dataset.version.created",
        dataset=row.dataset,
        input=row.input,
        revision=row.revision,
        params=row.params,
        active=row.active,
        debounce_seconds=row.debounce_seconds,
        last_fired_at=row.last_fired_at,
        last_run_id=row.last_run_id,
        created_at=row.created_at,
        created_by=row.created_by,
    )


@router.get(
    "/{workflow_id}/triggers",
    response_model=Page[WorkflowTrigger],
    operation_id="list_workflow_triggers",
    summary="Dataset triggers",
    responses=responses_for("list_workflow_triggers"),
)
async def list_workflow_triggers(
    ctx: Ctx,
    session: Session,
    workflow_id: WorkflowId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[WorkflowTrigger]:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    stmt = select(m.WorkflowTriggerRow).where(m.WorkflowTriggerRow.workflow_id == wf.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.WorkflowTriggerRow.id < str(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.WorkflowTriggerRow.id.desc()).limit(limit + 1))
        ).scalars()
    )
    items, nxt = _page([_trigger_dto(r) for r in rows], rows, limit, lambda r: r.id)
    return Page[WorkflowTrigger](items=items, next_cursor=nxt)


@router.post(
    "/{workflow_id}/triggers",
    response_model=WorkflowTrigger,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workflow_trigger",
    summary="Run the workflow when a dataset gets a new version",
    responses=responses_for("create_workflow_trigger"),
)
async def create_workflow_trigger(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, body: TriggerCreate
) -> JSONResponse:
    """The new version is bound to the workflow input ``input`` (a dataset input of the
    latest revision, ``SCHEDULE_INVALID`` otherwise); versions arriving within
    ``debounce_seconds`` start one run."""
    wf = await load_workflow(session, ctx.scope, workflow_id)
    rev = await load_revision(session, wf, body.revision)
    inputs = (rev.document.get("inputs") or {}) if isinstance(rev.document, dict) else {}
    decl = inputs.get(body.input) if isinstance(inputs, dict) else None
    if not isinstance(decl, dict) or decl.get("type") != "dataset":
        raise MapiError(
            f"the workflow has no dataset input {body.input!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            pointer="/input",
        )
    if body.dataset.project != ctx.scope.project_id:
        raise MapiError(
            "a trigger watches a dataset of its own project",
            code=ErrorCode.SCHEDULE_INVALID,
            pointer="/dataset",
        )
    if body.active:
        await _check_quota(
            ctx, session, m.WorkflowTriggerRow, ctx.settings.workflow_max_triggers, "triggers"
        )
    row = m.WorkflowTriggerRow(
        id=new_id("trg"),
        tenant_id=ctx.scope.tenant_id,
        project_id=ctx.scope.project_id,
        workflow_id=wf.id,
        event=body.event,
        dataset=str(body.dataset),
        dataset_name=body.dataset.name,
        input=body.input,
        revision=body.revision,
        params=dict(body.params),
        active=body.active,
        debounce_seconds=body.debounce_seconds,
        created_by=_principal(ctx),
        created_at=now(),
    )
    session.add(row)
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.WORKFLOW_TRIGGER_CREATE,
        wf.id,
        details={"trigger_id": row.id, "dataset": row.dataset},
    )
    return JSONResponse(_trigger_dto(row).model_dump(mode="json"), status_code=201)


@router.delete(
    "/{workflow_id}/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_workflow_trigger",
    summary="Delete a trigger",
    responses=responses_for("delete_workflow_trigger"),
)
async def delete_workflow_trigger(
    ctx: Ctx, session: Session, workflow_id: WorkflowId, trigger_id: TriggerId
) -> Response:
    wf = await load_workflow(session, ctx.scope, workflow_id)
    row = await session.get(m.WorkflowTriggerRow, trigger_id)
    if row is None or row.workflow_id != wf.id:
        raise NotFoundError("trigger", trigger_id)
    await session.delete(row)
    await session.flush()
    await _audit(
        ctx, session, AuditAction.WORKFLOW_TRIGGER_DELETE, wf.id, details={"trigger_id": trigger_id}
    )
    return Response(status_code=204)


# ======================================================================= gallery, catalog


@templates_router.get(
    "",
    response_model=Page[WorkflowTemplate],
    operation_id="list_workflow_templates",
    summary="The gallery of workflow templates",
    responses=responses_for("list_workflow_templates"),
)
async def list_workflow_templates(
    limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[WorkflowTemplate]:
    ids = sorted(WORKFLOW_TEMPLATES)
    after = decode_cursor(cursor)
    if after is not None:
        ids = [i for i in ids if i > str(after)]
    page = ids[:limit]
    nxt = encode_cursor(page[-1]) if len(ids) > limit and page else None
    return Page[WorkflowTemplate](items=[WORKFLOW_TEMPLATES[i] for i in page], next_cursor=nxt)


@templates_router.get(
    "/{template_id}",
    response_model=WorkflowTemplate,
    operation_id="get_workflow_template",
    summary="One gallery template",
    responses=responses_for("get_workflow_template"),
)
async def get_workflow_template(
    template_id: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
) -> WorkflowTemplate:
    t = WORKFLOW_TEMPLATES.get(template_id)
    if t is None:
        raise NotFoundError("workflow template", template_id)
    return t


@catalog_router.get(
    "",
    response_model=Page[NodeImplementation],
    operation_id="list_workflow_nodes",
    summary="Workflow node implementations with ports, parameters and About cards",
    responses=responses_for("list_workflow_nodes"),
)
async def list_workflow_nodes(
    limit: Limit = DEFAULT_LIMIT, cursor: str | None = None, kind: NodeKind | None = None
) -> Page[NodeImplementation]:
    ids = sorted(i for i, n in NODE_IMPLEMENTATIONS.items() if kind is None or n.kind is kind)
    after = decode_cursor(cursor)
    if after is not None:
        ids = [i for i in ids if i > str(after)]
    page = ids[:limit]
    nxt = encode_cursor(page[-1]) if len(ids) > limit and page else None
    return Page[NodeImplementation](items=[NODE_IMPLEMENTATIONS[i] for i in page], next_cursor=nxt)


@catalog_router.get(
    "/{node_id}",
    response_model=NodeImplementation,
    operation_id="get_workflow_node",
    summary="One node implementation",
    responses=responses_for("get_workflow_node"),
)
async def get_workflow_node(
    node_id: Annotated[str, Path(pattern=r"^[a-z]+\.[a-z0-9_]+$")],
) -> NodeImplementation:
    impl = NODE_IMPLEMENTATIONS.get(node_id)
    if impl is None:
        raise NotFoundError("workflow node", node_id)
    return impl
