"""Ask about a dashboard: the chat routes of members and clients, the chat settings and the
kept conversations (v0.20, plan D1, D4, D8 and I-3).

* ``GET /v1/dashboards/{dashboard_id}/chat`` — may the caller chat now, and the suggested
  questions (``ChatAvailability``; viewers and up, and clients with a grant);
* ``POST /v1/dashboards/{dashboard_id}/chat`` — one chat turn (``ChatRequest`` →
  ``ChatResponse``). Web session only (:data:`mapi_spec.api.SESSION_ONLY_OPERATIONS`: an API
  token answers ``403``): a turn spends model tokens on behalf of a person;
* ``GET|PUT …/chat/settings`` — editors: on/off, audiences, kept conversations and the daily
  budgets (``ETag`` / ``If-Match``);
* ``GET …/chat/transcripts``, ``GET|DELETE …/chat/transcripts/{conversation_id}`` — editors,
  web session only: the kept conversations (only when the dashboard keeps them).

The chat model is built once per app (:func:`~mapi_server.services.chat.model.build_chat_model`
in :func:`mapi_server.app.create_app`) and kept in ``app.state.chat_model``; the routes read it
with :func:`get_chat_model` (tests put a ``FakeChatModel`` there). The turn itself, with its
checks before every model and tool call, is
:func:`~mapi_server.services.chat.loop.run_chat_turn`.

``409 CHAT_DISABLED`` names its reason in ``errors[0]``: ``pointer`` is ``/reason`` and
``message`` is one of ``ChatAvailability.reason`` (``off``, ``audience``, ``layout``,
``region``, ``provider``, ``killswitch``). Chat answers are never cached
(``Cache-Control: no-store``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status
from mapi_spec.api import Page
from mapi_spec.chat import (
    CONVERSATION_ID_RE,
    ChatAvailability,
    ChatRequest,
    ChatResponse,
    ChatSettingsUpdate,
    ChatSettingsView,
    ChatTranscript,
    ChatTranscriptSummary,
    ChatUnavailableReason,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError

from mapi_server.api.common import Auth, Ctx
from mapi_server.api.problems import responses_for
from mapi_server.context import ServerContext
from mapi_server.services.chat import budget, transcripts
from mapi_server.services.chat.budget import ChatDisabled
from mapi_server.services.chat.loop import run_chat_turn
from mapi_server.services.chat.model import ChatModel
from mapi_server.services.chat.principal import ChatPrincipal
from mapi_server.services.dashboards import chat_settings

__all__ = [
    "CHAT_BODY_MAX_BYTES",
    "CHAT_BODY_PATHS",
    "CHAT_SETTINGS_BODY_MAX_BYTES",
    "CHAT_SETTINGS_BODY_PATHS",
    "NO_STORE",
    "ChatModelDep",
    "availability_for",
    "chat_disabled",
    "chat_errors",
    "get_chat_model",
    "router",
    "turn_for",
]

router = APIRouter(prefix="/v1/dashboards", tags=["dashboards"])

CHAT_BODY_MAX_BYTES: Final = 64 * 1024
"""A chat request: at most 20 turns of text (24,000 characters of history; plan I-3)."""
CHAT_BODY_PATHS: Final = (r"/v1/dashboards/[^/]+/chat", r"/v1/shared/chat")
CHAT_SETTINGS_BODY_MAX_BYTES: Final = 4 * 1024
"""A settings update is a few flags and two numbers."""
CHAT_SETTINGS_BODY_PATHS: Final = (r"/v1/dashboards/[^/]+/chat/settings",)
NO_STORE: Final = "no-store"

DashboardId = Annotated[str, Path(pattern=r"^dsh_[0-9a-z]{26}$")]
ConversationId = Annotated[str, Path(pattern=CONVERSATION_ID_RE)]
IfMatch = Annotated[str | None, Header(alias="If-Match", max_length=130)]
TranscriptLimit = Annotated[int, Query(ge=1, le=transcripts.TRANSCRIPT_PAGE_MAX)]


def _op(operation_id: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "responses": responses_for(operation_id)}


# ============================================================================== the model


def get_chat_model(request: Request) -> ChatModel | None:
    """The app's chat model (``None`` when chat is not set up: ``MAPI_CHAT_PROVIDER=off``)."""
    model: ChatModel | None = getattr(request.app.state, "chat_model", None)
    return model


ChatModelDep = Annotated[ChatModel | None, Depends(get_chat_model)]


# ================================================================================= errors


def chat_disabled(reason: ChatUnavailableReason, detail: str) -> MapiError:
    """``409 CHAT_DISABLED`` with its reason in ``errors[0]`` (``pointer`` ``/reason``)."""
    return MapiError(
        detail,
        code=ErrorCode.CHAT_DISABLED,
        issues=[Issue(code=ErrorCode.CHAT_DISABLED, pointer="/reason", message=reason)],
    )


@contextmanager
def chat_errors() -> Iterator[None]:
    """Give ``CHAT_DISABLED`` its reason on the way out (every other error is unchanged)."""
    try:
        yield
    except ChatDisabled as exc:
        raise chat_disabled(exc.reason, exc.detail) from None


def _no_model() -> MapiError:
    return chat_disabled("provider", "chat is not set up on this server")


async def availability_for(
    ctx: ServerContext, principal: ChatPrincipal, model: ChatModel | None
) -> ChatAvailability:
    """``GET …/chat`` for any caller: the credential is checked first (its own errors), then
    what decides whether the caller may chat now."""
    dctx = await principal.recheck()
    if model is None:
        return ChatAvailability(
            enabled_for_me=False,
            reason="provider",
            keeps_transcripts=False,
            transcript_days=ctx.settings.chat_transcript_ttl_days,
            model=None,
            suggestions=[],
        )
    return await budget.availability(ctx, dctx, principal)


async def turn_for(
    ctx: ServerContext,
    principal: ChatPrincipal,
    dashboard_id: str,
    body: ChatRequest,
    model: ChatModel | None,
) -> ChatResponse:
    """``POST …/chat`` for any caller (``dashboard_id`` ``""`` for a share session: the
    session names the dashboard)."""
    with chat_errors():
        if model is None:
            await principal.recheck()  # the credential's own error first (404, 410)
            raise _no_model()
        return await run_chat_turn(ctx, principal, dashboard_id, body, model=model)


# =================================================================================== chat


@router.get("/{dashboard_id}/chat", response_model=ChatAvailability, **_op("get_dashboard_chat"))
async def get_dashboard_chat(
    ctx: Ctx,
    auth: Auth,
    request: Request,
    response: Response,
    dashboard_id: DashboardId,
    model: ChatModelDep,
) -> ChatAvailability:
    """Whether you can chat about this dashboard now (``reason`` when not), whether it keeps
    conversations, and the suggested questions."""
    principal = ChatPrincipal.for_member(ctx, request, auth, dashboard_id)
    response.headers["Cache-Control"] = NO_STORE
    return await availability_for(ctx, principal, model)


@router.post("/{dashboard_id}/chat", response_model=ChatResponse, **_op("post_dashboard_chat"))
async def post_dashboard_chat(
    ctx: Ctx,
    auth: Auth,
    request: Request,
    response: Response,
    dashboard_id: DashboardId,
    body: ChatRequest,
    model: ChatModelDep,
) -> ChatResponse:
    """Ask the assistant about this dashboard: one answer per request. Send the earlier
    turns of the conversation as they were returned (assistant turns with their ``sig``).
    ``409 CHAT_DISABLED``, ``429 CHAT_BUDGET_EXHAUSTED`` or ``RATE_LIMITED`` (with
    ``Retry-After``), ``400 CHAT_HISTORY_INVALID``, ``503 CHAT_UNAVAILABLE``."""
    principal = ChatPrincipal.for_member(ctx, request, auth, dashboard_id)
    answer = await turn_for(ctx, principal, dashboard_id, body, model)
    response.headers["Cache-Control"] = NO_STORE
    return answer


# =============================================================================== settings


@router.get(
    "/{dashboard_id}/chat/settings",
    response_model=ChatSettingsView,
    **_op("get_dashboard_chat_settings"),
)
async def get_dashboard_chat_settings(
    ctx: Ctx, auth: Auth, response: Response, dashboard_id: DashboardId
) -> ChatSettingsView:
    """The chat settings of the dashboard and the tokens used today (``ETag``: send it back
    in ``If-Match`` to change them)."""
    view = await chat_settings.read_settings(ctx, auth, dashboard_id)
    response.headers["ETag"] = view.etag
    response.headers["Cache-Control"] = NO_STORE
    return view


@router.put(
    "/{dashboard_id}/chat/settings",
    response_model=ChatSettingsView,
    **_op("put_dashboard_chat_settings"),
)
async def put_dashboard_chat_settings(
    ctx: Ctx,
    auth: Auth,
    response: Response,
    dashboard_id: DashboardId,
    body: ChatSettingsUpdate,
    if_match: IfMatch = None,
) -> ChatSettingsView:
    """Change the chat settings. ``If-Match`` must be the ``etag`` you read (``412
    PRECONDITION_FAILED`` when it is missing or the settings changed). Audited."""
    view = await chat_settings.put_settings(ctx, auth, dashboard_id, body, if_match)
    response.headers["ETag"] = view.etag
    response.headers["Cache-Control"] = NO_STORE
    return view


# ============================================================================ transcripts


@router.get(
    "/{dashboard_id}/chat/transcripts",
    response_model=Page[ChatTranscriptSummary],
    **_op("list_dashboard_chat_transcripts"),
)
async def list_dashboard_chat_transcripts(
    ctx: Ctx,
    auth: Auth,
    response: Response,
    dashboard_id: DashboardId,
    cursor: str | None = None,
    limit: TranscriptLimit = transcripts.TRANSCRIPT_PAGE_DEFAULT,
) -> Page[ChatTranscriptSummary]:
    """The kept conversations of the dashboard, newest first."""
    page = await transcripts.list_page(ctx, auth, dashboard_id, cursor=cursor, limit=limit)
    response.headers["Cache-Control"] = NO_STORE
    return page


@router.get(
    "/{dashboard_id}/chat/transcripts/{conversation_id}",
    response_model=ChatTranscript,
    **_op("get_dashboard_chat_transcript"),
)
async def get_dashboard_chat_transcript(
    ctx: Ctx,
    auth: Auth,
    response: Response,
    dashboard_id: DashboardId,
    conversation_id: ConversationId,
) -> ChatTranscript:
    """One kept conversation, oldest turn first (``404`` when nothing is kept)."""
    found = await transcripts.get(ctx, auth, dashboard_id, conversation_id)
    response.headers["Cache-Control"] = NO_STORE
    return found


@router.delete(
    "/{dashboard_id}/chat/transcripts/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    **_op("delete_dashboard_chat_transcript"),
)
async def delete_dashboard_chat_transcript(
    ctx: Ctx, auth: Auth, dashboard_id: DashboardId, conversation_id: ConversationId
) -> Response:
    """Delete a kept conversation (every turn of it). Audited with the number of turns."""
    await transcripts.delete(ctx, auth, dashboard_id, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
