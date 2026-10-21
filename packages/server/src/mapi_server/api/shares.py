"""Public share-link routes (v0.16, plan D13): no account, no cookie.

``POST /v1/share-sessions`` exchanges a share token (sent once, in the body; the page strips
it from the URL fragment first) and its passcode for a short-lived session, which every
other route takes in ``X-Mapi-Share``. :class:`~mapi_server.api.share_cors.ShareCorsMiddleware`
drops ``Cookie`` and ``Authorization`` before routing, answers CORS for the web origin only
(no credentials) and marks every response ``Cache-Control: private, no-store``.

The session checks (and their order) are in :mod:`mapi_server.services.dashboards.shares`.

WP7 serves the session exchange, its refresh and the shared dashboard; the readout and
input routes (marked ``WP8``) answer ``501`` until the inputs and readouts land.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Header, HTTPException, Path, Request
from mapi_spec.dashboards import (
    SHARE_SESSION_HEADER,
    ActualsSubmit,
    ActualsTemplate,
    ClientDashboard,
    DashboardGrid,
    Readout,
    ReadoutHeader,
    ReadoutList,
    ScenarioSave,
    ShareSession,
    WhatIfRequest,
    WhatIfResult,
)
from mapi_spec.errors import ErrorCode, MapiError

from mapi_server.api.common import Auth, Ctx
from mapi_server.api.problems import responses_for
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.dashboards.shares import (
    open_session,
    refresh_session,
    resolve_shared,
    shared_dashboard,
)

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


def _wp8() -> NoReturn:  # replaced by the inputs and readouts work package
    raise HTTPException(status_code=501, detail="not available yet")


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


# --------------------------------------------------------------- WP8: readouts and inputs


@router.get("/v1/shared/readouts", response_model=ReadoutList, **_op("list_shared_readouts"))
async def list_shared_readouts(x_mapi_share: ShareHeader = None) -> ReadoutList:
    """Public: published readouts and this link's private ones (WP8)."""
    _wp8()


@router.get("/v1/shared/readouts/{seq}", response_model=Readout, **_op("get_shared_readout"))
async def get_shared_readout(seq: Seq, x_mapi_share: ShareHeader = None) -> Readout:
    """Public: one readout (WP8)."""
    _wp8()


@router.get("/v1/shared/grid", response_model=DashboardGrid, **_op("get_shared_grid"))
async def get_shared_grid(x_mapi_share: ShareHeader = None) -> DashboardGrid:
    """Public: the response grid (WP8)."""
    _wp8()


@router.post(
    "/v1/shared/whatif:evaluate", response_model=WhatIfResult, **_op("evaluate_shared_whatif")
)
async def evaluate_shared_whatif(
    body: WhatIfRequest, x_mapi_share: ShareHeader = None
) -> WhatIfResult:
    """Public: evaluate a spend plan on the grid (WP8)."""
    _wp8()


@router.post(
    "/v1/shared/scenarios", response_model=Readout | ReadoutHeader, **_op("save_shared_scenario")
)
async def save_shared_scenario(
    body: ScenarioSave, x_mapi_share: ShareHeader = None
) -> Readout | ReadoutHeader:
    """Public: save a scenario as this link's private readout (WP8)."""
    _wp8()


@router.get(
    "/v1/shared/actuals/template",
    response_model=ActualsTemplate,
    **_op("get_shared_actuals_template"),
)
async def get_shared_actuals_template(x_mapi_share: ShareHeader = None) -> ActualsTemplate:
    """Public: the actuals template (WP8)."""
    _wp8()


@router.post(
    "/v1/shared/actuals",
    response_model=ReadoutHeader,
    status_code=202,
    **_op("submit_shared_actuals"),
)
async def submit_shared_actuals(
    body: ActualsSubmit, x_mapi_share: ShareHeader = None
) -> ReadoutHeader:
    """Public: send new actuals (WP8)."""
    _wp8()
