"""Public share-link routes (v0.16, plan D13): no account, no cookie.

``POST /v1/share-sessions`` exchanges a share token (sent once, in the body; the page strips
it from the URL fragment first) and its passcode for a short-lived session, which every
other route takes in ``X-Mapi-Share``. :class:`~mapi_server.api.share_cors.ShareCorsMiddleware`
drops ``Cookie`` and ``Authorization`` before routing, answers CORS for the web origin only
(no credentials) and marks every response ``Cache-Control: private, no-store``.

The session checks (and their order) are in :mod:`mapi_server.services.dashboards.shares`.

The readout and input routes act through the link's
:class:`~mapi_server.services.dashboards.access.DashboardContext` (its inputs are the
published spec's, narrowed by the link's ``allowed_inputs``): the reads call the read-only
tool surface; scenarios and uploads are the member routes' functions (their runs act as
the publisher, within the link's hourly cap).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from mapi_spec.dashboards import (
    SHARE_SESSION_HEADER,
    ActualsSubmit,
    ActualsTemplate,
    ClientDashboard,
    DashboardGrid,
    Readout,
    ReadoutHeader,
    ReadoutKind,
    ReadoutList,
    ScenarioSave,
    ShareSession,
    WhatIfRequest,
    WhatIfResult,
)
from mapi_spec.errors import ErrorCode, MapiError

from mapi_server.api.common import Auth, Ctx, IdempotencyKeyHeader
from mapi_server.api.dashboard_readouts import PreferHeader, save_scenario_as, submit_actuals_as
from mapi_server.api.problems import responses_for
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.dashboards import inputs
from mapi_server.services.dashboards.access import DashboardContext
from mapi_server.services.dashboards.shares import (
    open_session,
    refresh_session,
    resolve_shared,
    shared_dashboard,
)
from mapi_server.services.dashboards.tools import call_tool

__all__ = ["SESSION_BODY_MAX_BYTES", "router"]

router = APIRouter(tags=["dashboards"])

SESSION_BODY_MAX_BYTES: Final = 4096
"""A session exchange is a token and a passcode: anything larger is refused unread."""

ShareHeader = Annotated[
    str | None, Header(alias=SHARE_SESSION_HEADER, max_length=512, include_in_schema=True)
]
Seq = Annotated[int, Path(ge=1)]


def _op(operation_id: str, **extra: Any) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id), **extra}


_SESSION_BODY: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ShareSessionCreate"}}
        },
    }
}


@router.post(
    "/v1/share-sessions",
    response_model=ShareSession,
    **_op("create_share_session", openapi_extra=_SESSION_BODY),
)
async def create_share_session(ctx: Ctx, auth: Auth, request: Request) -> ShareSession:
    """Public: exchange a share token (and its passcode) for a session (``401
    SHARE_PASSCODE_REQUIRED`` / ``SHARE_PASSCODE_INVALID``, ``429 SHARE_LOCKED`` after too
    many wrong passcodes, ``410 LINK_EXPIRED`` once the link ended, ``404`` for an unknown
    token). The body is read here, so the per-address limit counts malformed ones too."""
    raw = await request.body()
    if len(raw) > SESSION_BODY_MAX_BYTES:
        raise MapiError("the request body is too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
    try:
        body = loads_limited(raw) if raw else {}
    except (ValueError, JsonTooDeep):
        body = {}
    return await open_session(ctx, body if isinstance(body, dict) else {}, auth.client_ip)


@router.post(
    "/v1/share-sessions:refresh", response_model=ShareSession, **_op("refresh_share_session")
)
async def refresh_share_session(
    ctx: Ctx, auth: Auth, x_mapi_share: ShareHeader = None
) -> ShareSession:
    """Public: a fresh session while the link is valid (``401`` for an ended session)."""
    return await refresh_session(ctx, x_mapi_share, auth.client_ip)


@router.get("/v1/shared/dashboard", response_model=ClientDashboard, **_op("get_shared_dashboard"))
async def get_shared_dashboard(
    ctx: Ctx, auth: Auth, x_mapi_share: ShareHeader = None
) -> ClientDashboard:
    """Public: the shared dashboard (the published revision; ``ends_at`` = the link's end)."""
    view = await resolve_shared(ctx, x_mapi_share, auth.client_ip)
    return await shared_dashboard(ctx, view)


# ======================================================================== readouts and inputs


async def _view(ctx: Ctx, auth: Auth, header: str | None) -> DashboardContext:
    return (await resolve_shared(ctx, header, auth.client_ip)).context


@router.get("/v1/shared/readouts", response_model=ReadoutList, **_op("list_shared_readouts"))
async def list_shared_readouts(
    ctx: Ctx,
    auth: Auth,
    kind: ReadoutKind | None = None,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    x_mapi_share: ShareHeader = None,
) -> ReadoutList:
    """Public: published readouts and this link's own private ones, newest first (the
    ``list_readouts`` tool)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    args: dict[str, Any] = {"limit": limit}
    if kind is not None:
        args["kind"] = kind
    if before_seq is not None:
        args["before_seq"] = before_seq
    result = await call_tool(ctx, dctx, "list_readouts", args)
    assert isinstance(result, ReadoutList)
    return result


@router.get("/v1/shared/readouts/{seq}", response_model=Readout, **_op("get_shared_readout"))
async def get_shared_readout(
    ctx: Ctx, auth: Auth, seq: Seq, x_mapi_share: ShareHeader = None
) -> Readout:
    """Public: one readout (``404`` for another link's private one)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    result = await call_tool(ctx, dctx, "get_readout", {"seq": seq})
    assert isinstance(result, Readout)
    return result


@router.get("/v1/shared/grid", response_model=DashboardGrid, **_op("get_shared_grid"))
async def get_shared_grid(ctx: Ctx, auth: Auth, x_mapi_share: ShareHeader = None) -> DashboardGrid:
    """Public: the response grid (the link must allow what-ifs)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        return await inputs.grid_for(ctx, session, dctx)


@router.post(
    "/v1/shared/whatif:evaluate", response_model=WhatIfResult, **_op("evaluate_shared_whatif")
)
async def evaluate_shared_whatif(
    ctx: Ctx, auth: Auth, body: WhatIfRequest, x_mapi_share: ShareHeader = None
) -> WhatIfResult:
    """Public: evaluate a spend plan on the grid (no run, nothing saved)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    result = await call_tool(ctx, dctx, "evaluate_whatif", body.model_dump(mode="json"))
    assert isinstance(result, WhatIfResult)
    return result


@router.post("/v1/shared/scenarios", response_model=Readout, **_op("save_shared_scenario"))
async def save_shared_scenario(
    ctx: Ctx,
    auth: Auth,
    body: ScenarioSave,
    x_mapi_share: ShareHeader = None,
    prefer: PreferHeader = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Public: save a scenario as this link's private readout (``exact`` only when the
    dashboard and the link allow it; it spends the publisher's compute, within the link's
    hourly cap)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    return await save_scenario_as(ctx, dctx, "/v1/shared/scenarios", body, idempotency_key, prefer)


@router.get(
    "/v1/shared/actuals/template",
    response_model=ActualsTemplate,
    **_op("get_shared_actuals_template"),
)
async def get_shared_actuals_template(
    ctx: Ctx, auth: Auth, x_mapi_share: ShareHeader = None
) -> ActualsTemplate:
    """Public: the actuals template (the link must allow uploads)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        return await inputs.actuals_template(ctx, session, dctx)


@router.post(
    "/v1/shared/actuals",
    response_model=ReadoutHeader,
    status_code=202,
    **_op("submit_shared_actuals"),
)
async def submit_shared_actuals(
    ctx: Ctx,
    auth: Auth,
    body: ActualsSubmit,
    x_mapi_share: ShareHeader = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Public: send new actuals (the readout waits for the analyst's review unless the
    dashboard publishes uploads automatically)."""
    dctx = await _view(ctx, auth, x_mapi_share)
    return await submit_actuals_as(ctx, dctx, "/v1/shared/actuals", body, idempotency_key)
