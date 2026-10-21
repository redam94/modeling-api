"""Dashboard inputs, readouts, refresh and refit requests (v0.16).

Project routes in the ``X-Mapi-Project`` project. Members and ``client`` users act through
a :class:`~mapi_server.services.dashboards.access.DashboardContext` (a client sees only a
published dashboard granted to them, else ``404``): the grid and what-ifs (no run), saved
scenarios (private readouts; ``exact`` ones are score runs as the publisher), actuals
uploads and the readout history. The read routes call the read-only tool surface
(:func:`~mapi_server.services.dashboards.tools.call_tool`), so they answer exactly what the
tools answer. Review, refresh and refit decisions need an editor (approval: the runner
role too).

``Prefer: wait=N`` (N ≤ 30) on ``…/scenarios`` waits for an exact result: ``200`` with the
readout when it is ready in time, else ``202`` with its header (poll ``…/readouts/{seq}``).

v0.17 (layout 2): scenario rows (``…/predictions``), batch scoring (``…/batch`` and its
template) and the predictions download of a batch readout (``…/predictions.csv``: its own
caller or an editor). ``…/predictions`` and ``…/batch`` take ``Prefer: wait=N`` too.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Header, Path, Query
from fastapi.responses import JSONResponse, Response
from mapi_spec.api import (
    PREFER_HEADER,
    PREFERENCE_APPLIED_HEADER,
    Page,
    parse_prefer_wait,
)
from mapi_spec.dashboard_v2 import BatchSubmit, BatchTemplate, ScenarioRowsRequest
from mapi_spec.dashboards import (
    ActualsSubmit,
    ActualsTemplate,
    DashboardGrid,
    DashboardRefitRequest,
    Readout,
    ReadoutHeader,
    ReadoutKind,
    ReadoutList,
    ReadoutVisibility,
    RefitRequestDecision,
    RefitRequestStatus,
    ScenarioSave,
    WhatIfRequest,
    WhatIfResult,
)
from mapi_spec.tenancy import Action, ResourceKind
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Idempotency,
    IdempotencyKeyHeader,
    Limit,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.dashboards import DashboardId
from mapi_server.api.problems import responses_for
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import cases, downloads, inputs, readouts, refits
from mapi_server.services.dashboards.access import (
    DashboardContext,
    caller_is_client,
    load_dashboard,
    member_context,
    readout_dto,
    readout_header,
    refit_statuses,
)
from mapi_server.services.dashboards.tools import call_tool
from mapi_server.services.runs import Scope

__all__ = [
    "CSV_HEADERS",
    "MAX_WAIT_S",
    "csv_response",
    "dashboard_scope",
    "readout_response",
    "router",
    "submit_batch_as",
    "submit_predictions_as",
    "wait_readout",
]

router = APIRouter(prefix="/v1/dashboards", tags=["dashboards"])

Seq = Annotated[int, Path(ge=1)]
RequestId = Annotated[str, Path(pattern=r"^drr_[0-9a-z]{26}$")]
PreferHeader = Annotated[str | None, Header(alias=PREFER_HEADER, max_length=200)]
MAX_WAIT_S: Final = 30
_RESOURCE: Final = "dashboard_readout"


def _op(operation_id: str, **extra: Any) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id), **extra}


def dashboard_scope(dctx: DashboardContext) -> Scope:
    return Scope(dctx.tenant_id, dctx.project_id)


def _operation(path: str, dctx: DashboardContext) -> str:
    """Idempotency keys are per project: the caller is part of the request's identity, so
    a key reused by somebody else never replays their readout (``IDEMPOTENCY_CONFLICT``)."""
    return f"POST {path} {dctx.principal_kind}:{dctx.principal_id}"


async def _header(session: AsyncSession, row: m.DashboardReadout) -> ReadoutHeader:
    statuses = await refit_statuses(session, row.dashboard_id, [row.seq])
    return readout_header(row, statuses.get(row.seq))


async def wait_readout(
    ctx: ServerContext, dctx: DashboardContext, seq: int, seconds: float
) -> Readout:
    """Poll the readout (each poll a fresh tenant session) until it is ready or failed, or
    ``seconds`` passed; returns the last state seen."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while True:
        async with ctx.db.tenant_session(dctx.tenant_id) as s:
            row = await readouts.load_visible(s, dctx, seq)
            statuses = await refit_statuses(s, row.dashboard_id, [row.seq])
            dto = readout_dto(row, statuses.get(row.seq), analyst=dctx.is_analyst)
        if dto.status in ("ready", "failed") or loop.time() >= deadline:
            return dto
        await asyncio.sleep(min(1.0, max(0.05, deadline - loop.time())))


def readout_response(
    dto: Readout, *, applied: int | None = None, replay: bool = False
) -> JSONResponse:
    """``200`` with the readout once it is ready (or failed), else ``202`` with its header."""
    done = dto.status in ("ready", "failed")
    body: BaseModel = (
        dto
        if done
        else ReadoutHeader.model_validate(dto.model_dump(exclude={"snapshot", "problem"}))
    )
    headers = {PREFERENCE_APPLIED_HEADER: f"wait={applied}"} if applied else {}
    return JSONResponse(
        body.model_dump(mode="json"), status_code=200 if done or replay else 202, headers=headers
    )


async def save_scenario_as(
    ctx: ServerContext,
    dctx: DashboardContext,
    path: str,
    body: ScenarioSave,
    idempotency_key: str | None,
    prefer: str | None,
) -> JSONResponse:
    """``…/scenarios`` for a member, a client or a share link (the share route uses it)."""
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        idem = Idempotency(session, dashboard_scope(dctx), idempotency_key)
        replay = await idem.begin(_operation(path, dctx), body.model_dump(mode="json"))
        if replay is not None:
            seq = int(replay.resource_id)
        else:
            row = await inputs.save_scenario(ctx, session, dctx, body)
            seq = row.seq
            await idem.complete(_RESOURCE, str(seq), 202)
    wait = parse_prefer_wait(prefer)
    wait = min(wait, MAX_WAIT_S) if wait else None
    dto = await wait_readout(ctx, dctx, seq, float(wait or 0))
    return readout_response(dto, applied=wait)


async def submit_actuals_as(
    ctx: ServerContext,
    dctx: DashboardContext,
    path: str,
    body: ActualsSubmit,
    idempotency_key: str | None,
) -> JSONResponse:
    """``…/actuals`` for a member, a client or a share link: ``202`` with the readout header
    (``200`` on an idempotent replay)."""
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        idem = Idempotency(session, dashboard_scope(dctx), idempotency_key)
        replay = await idem.begin(_operation(path, dctx), body.model_dump(mode="json"))
        if replay is not None:
            row = await readouts.load_visible(session, dctx, int(replay.resource_id))
            status = 200
        else:
            row = await inputs.submit_actuals(ctx, session, dctx, body)
            await idem.complete(_RESOURCE, str(row.seq), 202)
            status = 202
        head = await _header(session, row)
    return JSONResponse(head.model_dump(mode="json"), status_code=status)


async def _spend_as(
    ctx: ServerContext,
    dctx: DashboardContext,
    path: str,
    body: BaseModel,
    idempotency_key: str | None,
    prefer: str | None,
    submit: Any,
) -> JSONResponse:
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        idem = Idempotency(session, dashboard_scope(dctx), idempotency_key)
        replay = await idem.begin(_operation(path, dctx), body.model_dump(mode="json"))
        if replay is not None:
            seq = int(replay.resource_id)
        else:
            row = await submit(ctx, session, dctx, body)
            seq = row.seq
            await idem.complete(_RESOURCE, str(seq), 202)
    wait = parse_prefer_wait(prefer)
    wait = min(wait, MAX_WAIT_S) if wait else None
    dto = await wait_readout(ctx, dctx, seq, float(wait or 0))
    return readout_response(dto, applied=wait)


async def submit_predictions_as(
    ctx: ServerContext,
    dctx: DashboardContext,
    path: str,
    body: ScenarioRowsRequest,
    idempotency_key: str | None,
    prefer: str | None,
) -> JSONResponse:
    """``…/predictions`` for a member, a client or a share link (v0.17): ``202`` with the
    header of the private prediction readout (``200`` with it when ready in time)."""
    return await _spend_as(
        ctx, dctx, path, body, idempotency_key, prefer, cases.submit_scenario_rows
    )


async def submit_batch_as(
    ctx: ServerContext,
    dctx: DashboardContext,
    path: str,
    body: BatchSubmit,
    idempotency_key: str | None,
    prefer: str | None,
) -> JSONResponse:
    """``…/batch`` for a member, a client or a share link (v0.17)."""
    return await _spend_as(ctx, dctx, path, body, idempotency_key, prefer, cases.submit_batch)


CSV_HEADERS: Final = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}


def csv_response(found: downloads.PredictionsCsv, *, share: bool = False) -> Response:
    """The predictions file as an attachment (never cached, never sniffed; on a share route
    :class:`~mapi_server.api.share_cors.ShareCorsMiddleware` sets those headers)."""
    return Response(
        content=found.data,
        media_type="text/csv; charset=utf-8",
        headers={
            **({} if share else CSV_HEADERS),
            "Content-Disposition": f'attachment; filename="{found.filename}"',
        },
    )


_CSV_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {200: {"content": {"text/csv": {}}}}


async def _dctx(ctx: ServerContext, auth: Auth, dashboard_id: str) -> DashboardContext:
    return await member_context(ctx, auth, dashboard_id)


# =================================================================================== inputs


@router.get("/{dashboard_id}/grid", response_model=DashboardGrid, **_op("get_dashboard_grid"))
async def get_dashboard_grid(ctx: Ctx, auth: Auth, dashboard_id: DashboardId) -> DashboardGrid:
    """The response grid of the pinned version, clipped to the dashboard's channels and
    slider ranges (``409 DASHBOARD_INPUT_DISABLED`` without what-ifs or a grid). The
    browser evaluates the sliders with it; no run is made."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        return await inputs.grid_for(ctx, session, dctx)


@router.post(
    "/{dashboard_id}/whatif:evaluate",
    response_model=WhatIfResult,
    **_op("evaluate_dashboard_whatif"),
)
async def evaluate_dashboard_whatif(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId, body: WhatIfRequest
) -> WhatIfResult:
    """Evaluate a spend plan on the grid (``422 SCENARIO_OUT_OF_RANGE`` outside a slider's
    range). No run, nothing saved: the ``evaluate_whatif`` tool."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    result = await call_tool(ctx, dctx, "evaluate_whatif", body.model_dump(mode="json"))
    assert isinstance(result, WhatIfResult)
    return result


@router.post(
    "/{dashboard_id}/scenarios",
    response_model=Readout,
    **_op("save_dashboard_scenario"),
)
async def save_dashboard_scenario(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    body: ScenarioSave,
    prefer: PreferHeader = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Save a scenario as a private readout: the grid result at once (200), or with
    ``exact`` a score run as the publisher (202 with the header; ``Prefer: wait=N`` waits).
    Repeated plans answer with the earlier result (no new run)."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    path = f"/v1/dashboards/{dashboard_id}/scenarios"
    return await save_scenario_as(ctx, dctx, path, body, idempotency_key, prefer)


@router.get(
    "/{dashboard_id}/actuals/template",
    response_model=ActualsTemplate,
    **_op("get_dashboard_actuals_template"),
)
async def get_dashboard_actuals_template(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId
) -> ActualsTemplate:
    """The table an actuals upload fills in, its KPI column, an example CSV and what happens
    to the numbers (``conditioned`` or ``compared``)."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        return await inputs.actuals_template(ctx, session, dctx)


@router.post(
    "/{dashboard_id}/actuals",
    response_model=ReadoutHeader,
    status_code=202,
    **_op("submit_dashboard_actuals"),
)
async def submit_dashboard_actuals(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    body: ActualsSubmit,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Send new actuals (inline XrJSON): ``422 ACTUALS_INVALID`` with pointers to what to
    fix, else a readout scored as the publisher (``202``). ``request_refit`` asks the analyst
    for a refit when the dashboard allows it (nothing runs until an analyst approves)."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    path = f"/v1/dashboards/{dashboard_id}/actuals"
    return await submit_actuals_as(ctx, dctx, path, body, idempotency_key)


# ======================================================== scenario rows and batch (v0.17)


@router.post(
    "/{dashboard_id}/predictions",
    response_model=Readout,
    status_code=202,
    **_op("submit_dashboard_predictions"),
)
async def submit_dashboard_predictions(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    body: ScenarioRowsRequest,
    prefer: PreferHeader = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Score scenario rows ("Try a case"): every value is checked against the published
    revision (``422 DASHBOARD_INPUT_INVALID`` with ``/rows/<i>/<name>`` pointers; never
    clipped), then scored as the publisher - a private prediction readout (``202``; a
    repeated request answers with the earlier result)."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    path = f"/v1/dashboards/{dashboard_id}/predictions"
    return await submit_predictions_as(ctx, dctx, path, body, idempotency_key, prefer)


@router.get(
    "/{dashboard_id}/batch/template",
    response_model=BatchTemplate,
    **_op("get_dashboard_batch_template"),
)
async def get_dashboard_batch_template(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId
) -> BatchTemplate:
    """The columns of a batch file (the id, then the drivers with their ranges), an
    example CSV and the row cap (``409 DASHBOARD_INPUT_DISABLED`` without batch scoring)."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        return await cases.batch_template(ctx, session, dctx)


@router.post(
    "/{dashboard_id}/batch",
    response_model=Readout,
    status_code=202,
    **_op("submit_dashboard_batch"),
)
async def submit_dashboard_batch(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    body: BatchSubmit,
    prefer: PreferHeader = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Score a file of new cases (inline XrJSON, one row per id) as the publisher: a
    private batch readout with a summary and a predictions download (``202``). ``409
    DASHBOARD_BATCH_TOO_LARGE`` over the row cap or the result budget; ``429`` over a daily
    batch cap."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    path = f"/v1/dashboards/{dashboard_id}/batch"
    return await submit_batch_as(ctx, dctx, path, body, idempotency_key, prefer)


@router.get(
    "/{dashboard_id}/readouts/{seq}/predictions.csv",
    response_class=Response,
    responses={**_CSV_RESPONSES, **responses_for("download_dashboard_predictions")},
    operation_id="download_dashboard_predictions",
)
async def download_dashboard_predictions(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId, seq: Seq
) -> Response:
    """The predictions of a batch readout as CSV, rendered from the run's results (its own
    caller, or an editor). ``404`` for anybody else; ``410 DASHBOARD_DOWNLOAD_GONE`` once
    the readout or the run's results expired. Charged to egress and audited."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    editor = not caller_is_client(auth) and auth.allows(Action.UPDATE, ResourceKind.DASHBOARD)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        found = await downloads.predictions_csv(ctx, session, dctx, seq, editor=editor)
    return csv_response(found)


# ================================================================================== readouts


@router.get(
    "/{dashboard_id}/readouts", response_model=ReadoutList, **_op("list_dashboard_readouts")
)
async def list_dashboard_readouts(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    kind: ReadoutKind | None = None,
    visibility: ReadoutVisibility | None = None,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ReadoutList:
    """Readouts newest first: the published history plus the caller's own private and
    pending ones (analysts: all, filterable by ``visibility``). The ``list_readouts``
    tool."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    if visibility is not None and dctx.is_analyst:
        async with ctx.db.tenant_session(dctx.tenant_id) as session:
            return await readouts.list_readouts(
                session, dctx, kind=kind, visibility=visibility, before_seq=before_seq,
                limit=limit,
            )  # fmt: skip
    args: dict[str, Any] = {"limit": limit}
    if kind is not None:
        args["kind"] = kind
    if before_seq is not None:
        args["before_seq"] = before_seq
    result = await call_tool(ctx, dctx, "list_readouts", args)
    assert isinstance(result, ReadoutList)
    return result


@router.get(
    "/{dashboard_id}/readouts/{seq}", response_model=Readout, **_op("get_dashboard_readout")
)
async def get_dashboard_readout(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId, seq: Seq
) -> Readout:
    """One readout with its snapshot (``null`` until ready) - poll it while it is built.
    The ``get_readout`` tool."""
    dctx = await _dctx(ctx, auth, dashboard_id)
    result = await call_tool(ctx, dctx, "get_readout", {"seq": seq})
    assert isinstance(result, Readout)
    return result


async def _review(
    ctx: ServerContext, dashboard_id: str, seq: int, to: Literal["published", "hidden"]
) -> ReadoutHeader:
    async with ctx.data_session() as session:
        dash = await load_dashboard(session, ctx, dashboard_id)
        row = await readouts.review(session, ctx, dash, seq, to)
        return await _header(session, row)


@router.post(
    "/{dashboard_id}/readouts/{seq}:publish",
    response_model=ReadoutHeader,
    **_op("publish_dashboard_readout"),
)
async def publish_dashboard_readout(ctx: Ctx, dashboard_id: DashboardId, seq: Seq) -> ReadoutHeader:
    """Publish a readout that waits for review (an actuals upload) into the history.
    Idempotent. Audited."""
    return await _review(ctx, dashboard_id, seq, "published")


@router.post(
    "/{dashboard_id}/readouts/{seq}:hide",
    response_model=ReadoutHeader,
    **_op("hide_dashboard_readout"),
)
async def hide_dashboard_readout(ctx: Ctx, dashboard_id: DashboardId, seq: Seq) -> ReadoutHeader:
    """Hide a readout from clients and share links (the row is kept). Idempotent.
    Audited."""
    return await _review(ctx, dashboard_id, seq, "hidden")


@router.post(
    "/{dashboard_id}:refresh",
    response_model=ReadoutHeader,
    status_code=202,
    **_op("refresh_dashboard"),
)
async def refresh_dashboard(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Queue a refresh readout of the published revision (the worker scores it as the
    publisher). Within the debounce window the last refresh answers again."""
    async with ctx.data_session() as session:
        idem = Idempotency(session, ctx.scope, idempotency_key)
        replay = await idem.begin(f"POST /v1/dashboards/{dashboard_id}:refresh", {})
        dash = await load_dashboard(session, ctx, dashboard_id)
        if replay is not None:
            row = await readouts.load_visible(
                session, await member_context(ctx, auth, dashboard_id, session=session),
                int(replay.resource_id),
            )  # fmt: skip
            status = 200
        else:
            row = await inputs.refresh(ctx, session, dash, auth.user_id or auth.principal_id)
            await idem.complete(_RESOURCE, str(row.seq), 202)
            status = 202
        head = await _header(session, row)
    return JSONResponse(head.model_dump(mode="json"), status_code=status)


# ============================================================================ refit requests


@router.get(
    "/{dashboard_id}/refit-requests",
    response_model=Page[DashboardRefitRequest],
    **_op("list_dashboard_refit_requests"),
)
async def list_dashboard_refit_requests(
    ctx: Ctx,
    dashboard_id: DashboardId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_: Annotated[RefitRequestStatus | None, Query(alias="status")] = None,
) -> Page[DashboardRefitRequest]:
    """Refit requests, newest first (editors)."""
    after = decode_cursor(cursor)
    async with ctx.data_session() as session:
        rows, more = await refits.list_requests(
            session, ctx, dashboard_id, status=status_, limit=limit,
            before=str(after) if after is not None else None,
        )  # fmt: skip
        return Page[DashboardRefitRequest](
            items=[refits.refit_dto(r) for r in rows],
            next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
        )


@router.post(
    "/{dashboard_id}/refit-requests/{request_id}:approve",
    response_model=DashboardRefitRequest,
    **_op("approve_dashboard_refit_request"),
)
async def approve_dashboard_refit_request(
    ctx: Ctx,
    dashboard_id: DashboardId,
    request_id: RequestId,
    body: RefitRequestDecision,
    idempotency_key: IdempotencyKeyHeader = None,
) -> DashboardRefitRequest:
    """Approve an open refit request (editor with the runner role): its readout enters the
    history, its numbers are ingested as a new version of the dashboard's dataset, and the
    refit workflow starts as you once that version is ready. Audited."""
    async with ctx.data_session() as session:
        idem = Idempotency(session, ctx.scope, idempotency_key)
        path = f"POST /v1/dashboards/{dashboard_id}/refit-requests/{request_id}:approve"
        replay = await idem.begin(path, body.model_dump(mode="json"))
        if replay is not None:
            row = await session.get(m.DashboardRefitRequest, replay.resource_id)
            assert row is not None
            return refits.refit_dto(row)
        row = await refits.approve(session, ctx, dashboard_id, request_id, body)
        await idem.complete("dashboard_refit_request", row.id, 200)
        return refits.refit_dto(row)


@router.post(
    "/{dashboard_id}/refit-requests/{request_id}:reject",
    response_model=DashboardRefitRequest,
    **_op("reject_dashboard_refit_request"),
)
async def reject_dashboard_refit_request(
    ctx: Ctx, dashboard_id: DashboardId, request_id: RequestId, body: RefitRequestDecision
) -> DashboardRefitRequest:
    """Reject an open refit request: nothing runs. Audited."""
    async with ctx.data_session() as session:
        row = await refits.reject(session, ctx, dashboard_id, request_id, body)
        return refits.refit_dto(row)
