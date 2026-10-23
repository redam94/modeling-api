"""Ask about a shared dashboard: chat over a share link (v0.20, plan D1, D9 and I-3).

* ``GET /v1/shared/chat`` — may this link chat now, and the suggested questions;
* ``POST /v1/shared/chat`` — one chat turn (``ChatRequest`` → ``ChatResponse``).

Both are public routes authorised by the share session in ``X-Mapi-Share``, like every
``/v1/shared/*`` route: :class:`~mapi_server.api.share_cors.ShareCorsMiddleware` drops
``Cookie`` and ``Authorization`` before routing and marks the responses ``no-store``. The
session is checked before the body is read (an anonymous caller learns nothing from the
body's validation; the body itself is capped at 64 KiB before routing). The dashboard
comes from the session, never from the request. The session is checked again before every
model call and every tool call of the turn (only the first check counts against the link's
request limits), so revoking the link ends the conversation at its next step with ``410
LINK_EXPIRED``.

Chat over a link needs the dashboard's ``share_links`` audience (off by default, plan §9 Q7);
every viewer of one link is the same principal, so they share its daily token budget
(``share_tokens_per_day``) and its per-minute turn limit, and each address has its own limit
too.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from mapi_spec.chat import ChatAvailability, ChatRequest, ChatResponse
from pydantic import ValidationError

from mapi_server.api.common import Auth, Ctx
from mapi_server.api.dashboard_chat import (
    NO_STORE,
    ChatModelDep,
    availability_for,
    turn_for,
)
from mapi_server.api.problems import responses_for
from mapi_server.api.shares import ShareHeader
from mapi_server.services.chat.principal import ChatPrincipal

__all__ = ["parse_chat_request", "router"]

router = APIRouter(tags=["dashboards"])

_CHAT_BODY: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ChatRequest"}}},
    }
}


def parse_chat_request(raw: bytes) -> ChatRequest:
    """The body of ``POST /v1/shared/chat`` (already capped and depth-checked by
    :class:`~mapi_server.api.problems.JsonBodyLimit`); ``422 SCHEMA_INVALID`` with the
    same pointers as a body FastAPI validates."""
    try:
        return ChatRequest.model_validate_json(raw or b"{}")
    except ValidationError as exc:
        errors = [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors()]
        raise RequestValidationError(errors) from None


@router.get(
    "/v1/shared/chat",
    response_model=ChatAvailability,
    operation_id="get_shared_chat",
    responses=responses_for("get_shared_chat"),
)
async def get_shared_chat(
    ctx: Ctx,
    auth: Auth,
    request: Request,
    response: Response,
    model: ChatModelDep,
    x_mapi_share: ShareHeader = None,
) -> ChatAvailability:
    """Public: whether this link can chat now (``reason`` when not), and the suggested
    questions."""
    del auth  # the share session is the credential
    principal = ChatPrincipal.for_share(ctx, request, x_mapi_share)
    response.headers["Cache-Control"] = NO_STORE
    return await availability_for(ctx, principal, model)


@router.post(
    "/v1/shared/chat",
    response_model=ChatResponse,
    operation_id="post_shared_chat",
    responses=responses_for("post_shared_chat"),
    openapi_extra=_CHAT_BODY,
)
async def post_shared_chat(
    ctx: Ctx,
    auth: Auth,
    request: Request,
    response: Response,
    model: ChatModelDep,
    x_mapi_share: ShareHeader = None,
) -> ChatResponse:
    """Public: ask the assistant about the shared dashboard (one answer per request). ``410
    LINK_EXPIRED`` once the link ended; ``409 CHAT_DISABLED`` when the dashboard does not
    open chat to share links."""
    del auth
    principal = ChatPrincipal.for_share(ctx, request, x_mapi_share)  # 401 before the body
    body = parse_chat_request(await request.body())
    answer = await turn_for(ctx, principal, "", body, model)
    response.headers["Cache-Control"] = NO_STORE
    return answer
