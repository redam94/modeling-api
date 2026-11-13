"""Dashboard read tokens (v0.20, plan D13): connect a chat tool to one dashboard over MCP.

``GET|POST /v1/dashboards/{dashboard_id}/read-tokens`` and ``DELETE …/{token_id}``. Web
session only (:data:`mapi_spec.api.SESSION_ONLY_OPERATIONS`: an API token answers ``403``);
clients may use them on the dashboards granted to them. The server sets each token's scope
(``dashboard:<dashboard_id>``) and owner (the caller); the secret is returned once, with
``Cache-Control: no-store``. The rules are in
:mod:`mapi_server.services.dashboards.read_tokens`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status
from fastapi.responses import JSONResponse
from mapi_spec.chat import ReadToken, ReadTokenCreate, ReadTokenCreated
from mapi_spec.tokens import TOKEN_ID_RE

from mapi_server.api.common import Auth, Ctx
from mapi_server.api.problems import responses_for
from mapi_server.services.dashboards import read_tokens

__all__ = ["router"]

router = APIRouter(prefix="/v1/dashboards", tags=["dashboards"])

DashboardId = Annotated[str, Path(pattern=r"^dsh_[0-9a-z]{26}$")]
TokenId = Annotated[str, Path(pattern=TOKEN_ID_RE)]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


@router.get(
    "/{dashboard_id}/read-tokens",
    response_model=list[ReadToken],
    **_op("list_dashboard_read_tokens"),
)
async def list_dashboard_read_tokens(
    ctx: Ctx,
    auth: Auth,
    dashboard_id: DashboardId,
    all_: Annotated[bool, Query(alias="all")] = False,
) -> list[ReadToken]:
    """Your read tokens for this dashboard, newest first (``all=true``: every owner's, for
    editors). Never a secret."""
    return await read_tokens.list_tokens(ctx, auth, dashboard_id, all_=all_)


@router.post(
    "/{dashboard_id}/read-tokens",
    response_model=ReadTokenCreated,
    status_code=status.HTTP_201_CREATED,
    **_op("create_dashboard_read_token"),
)
async def create_dashboard_read_token(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId, body: ReadTokenCreate
) -> JSONResponse:
    """Make a read token for this dashboard. ``secret`` is returned now and never again."""
    created = await read_tokens.mint(ctx, auth, dashboard_id, body)
    return JSONResponse(
        created.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
        headers={"Cache-Control": "no-store"},
    )


@router.delete(
    "/{dashboard_id}/read-tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    **_op("revoke_dashboard_read_token"),
)
async def revoke_dashboard_read_token(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId, token_id: TokenId
) -> Response:
    """Revoke a read token (idempotent): yours, or any of the dashboard's as an editor."""
    await read_tokens.revoke(ctx, auth, dashboard_id, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
