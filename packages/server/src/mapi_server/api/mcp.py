"""The MCP endpoint ``POST /v1/mcp`` (v0.20, plan D14-D19): the dashboard tools for agents.

An agent (a desktop chat app, an IDE, the ``mapi-mcp`` stdio bridge) sends one JSON-RPC 2.0
message per request (MCP streamable HTTP, stateless, JSON responses). The protocol is
:mod:`mapi_spec.mcp`; this module is the transport:

1. **The credential selects the dashboard.** ``Authorization: Bearer <dashboard read token>``
   (:func:`~mapi_server.auth.request.resolve_dashboard_token`, then
   :func:`~mapi_server.services.dashboards.access.token_context`: the owner's membership,
   grant and the published layout 2 revision are checked again on every request) or
   ``X-Mapi-Share: mps_…`` (:func:`~mapi_server.services.dashboards.shares.resolve_session`:
   revocation, expiry and the session version, on every request). Nothing else: no cookie
   (the middleware drops it), no query credential, no ordinary API token (``403``). Both
   headers at once answer ``401``. The route is public in the route table, so the generic
   resolver never reads the bearer.
2. **Rate limit** per read token (``mcp:<token id>``) or share link
   (``mcpshare:<share id>``): ``MAPI_MCP_RATE_PER_MINUTE`` requests a minute, then ``429``
   with ``Retry-After``.
3. **The message.** At most 64 KiB (``413``). A message that is not JSON-RPC 2.0, a batch,
   or a request with an unsupported ``MCP-Protocol-Version`` header answers ``400`` with a
   JSON-RPC error body. Notifications (and client responses) answer ``202`` with no body.
4. **Tools run in-process** through
   :func:`~mapi_server.services.dashboards.tool_guard.call_tool_capped` (the read-only tool
   surface, capped at :data:`~mapi_spec.chat.MCP_TOOL_RESULT_MAX_BYTES`). A tool refusal is a
   tool result with ``isError: true`` and ``{"code", "message"}`` text.

A credential that stops being valid answers with its own HTTP error (``401``, ``404``,
``410 LINK_EXPIRED``, ``403 UPLOADED_CODE_NOT_SHAREABLE``) before any tool runs. Calls are
not audited (MCP spends no model tokens); request bodies, results and credentials are never
logged. :class:`~mapi_server.api.mcp_middleware.McpMiddleware` applies the transport rules
(cookies, origins, hosts, methods, ``no-store``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from mapi_spec.chat import MCP_TOOL_RESULT_MAX_BYTES
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.mcp import (
    INVALID_REQUEST,
    MCP_HTTP_DEFAULT_VERSION,
    MCP_MAX_MESSAGE_BYTES,
    MCP_PROTOCOL_VERSIONS,
    JsonRpcError,
    ToolOutcome,
    dispatch,
    error_response,
    is_notification,
    parse_message,
    protocol_version_from_header,
)
from pydantic import JsonValue

from mapi_server.api.common import Auth, Ctx
from mapi_server.api.problems import responses_for
from mapi_server.auth.request import resolve_dashboard_token
from mapi_server.context import ServerContext
from mapi_server.errors import ProblemWithHeaders, unauthenticated
from mapi_server.services.dashboards.access import DashboardContext, token_context
from mapi_server.services.dashboards.shares import resolve_session
from mapi_server.services.dashboards.tool_guard import call_tool_capped
from mapi_server.services.quotas import count_requests
from mapi_server.services.runs import now

__all__ = [
    "MCP_PROTOCOL_HEADER",
    "McpCaller",
    "mcp_caller",
    "router",
    "tool_outcome",
]

router = APIRouter(tags=["dashboards"])

MCP_PROTOCOL_HEADER: Final = "MCP-Protocol-Version"
_JSON: Final = "application/json"
_MAX_ERROR_TEXT: Final = 1000
_PYDANTIC_URL: Final = re.compile(r"\s*For further information visit \S+")

_BODY: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "description": "One JSON-RPC 2.0 message (MCP 2025-11-25 or 2025-06-18)",
        "content": {_JSON: {"schema": {"type": "object"}}},
    }
}
_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    200: {
        "description": "The JSON-RPC response",
        "content": {_JSON: {"schema": {"type": "object"}}},
    },
    202: {"description": "Accepted (a notification; no body)"},
}


@dataclass(frozen=True, slots=True)
class McpCaller:
    """Who calls: the dashboard context of the credential and its rate-limit subject."""

    context: DashboardContext
    subject: str


# ======================================================================= the credential


async def _rate_limit(ctx: ServerContext, subject: str) -> None:
    settings = ctx.settings
    if not settings.rate_limits_enabled:
        return
    at = now()
    async with ctx.db.session() as s:
        before = await count_requests(s, subject, now=at)
    if before >= settings.mcp_rate_per_minute:
        raise ProblemWithHeaders(
            "too many requests for this credential; try again shortly",
            code=ErrorCode.RATE_LIMITED,
            headers={"Retry-After": str(max(1, 60 - at.second))},
        )


def _token_subject(token_id: str) -> str:
    return f"mcp:{token_id}"


def _share_subject(share_id: str) -> str:
    return f"mcpshare:{share_id}"


async def mcp_caller(ctx: ServerContext, request: Request, client_ip: str | None) -> McpCaller:
    """Resolve the request's one credential to a dashboard context (and count it against
    the credential's per-minute limit). Raises the credential's own error."""
    authorization = request.headers.get("authorization")
    share = request.headers.get(SHARE_SESSION_HEADER)
    if authorization is not None and share is not None:
        raise unauthenticated(
            "send one credential: a dashboard read token or a share session, not both"
        )
    if share is not None:
        dctx = await resolve_session(ctx, share, client_ip)
        subject = _share_subject(dctx.principal_id)
        await _rate_limit(ctx, subject)
        return McpCaller(context=dctx, subject=subject)
    if authorization is None:
        raise unauthenticated(
            "send a dashboard read token (Authorization: Bearer mapi_…) or a share session "
            f"({SHARE_SESSION_HEADER})"
        )
    tok = await resolve_dashboard_token(ctx, request)
    subject = _token_subject(tok.token_id)
    await _rate_limit(ctx, subject)
    return McpCaller(context=await token_context(ctx, tok), subject=subject)


# ======================================================================= tools


def _error_text(exc: MapiError) -> str:
    message = exc.detail
    if exc.code is ErrorCode.SCHEMA_INVALID and exc.untrusted_text:
        detail = _PYDANTIC_URL.sub("", exc.untrusted_text).strip()
        message = f"{message}: {detail}"
    return message[:_MAX_ERROR_TEXT]


async def tool_outcome(
    ctx: ServerContext, dctx: DashboardContext, name: str, args: dict[str, JsonValue]
) -> ToolOutcome:
    """Run one tool for ``dctx`` and shape the result for :func:`mapi_spec.mcp.dispatch`."""
    capped, err, _ms = await call_tool_capped(
        ctx, dctx, name, args, max_bytes=MCP_TOOL_RESULT_MAX_BYTES
    )
    if err is not None:
        return ToolOutcome(payload=None, text="", truncated=False,
                           error=(err.code.value, _error_text(err)))  # fmt: skip
    assert capped is not None
    return ToolOutcome(payload=capped.payload, text=capped.text, truncated=capped.truncated)


# ======================================================================= the message


async def _read_body(request: Request) -> bytes:
    """The body, refused (``413``) as soon as it passes the message cap."""
    declared = request.headers.get("content-length")
    too_large = MapiError(
        f"an MCP message is at most {MCP_MAX_MESSAGE_BYTES} bytes",
        code=ErrorCode.PAYLOAD_TOO_LARGE,
    )
    if declared is not None and declared.isdigit() and int(declared) > MCP_MAX_MESSAGE_BYTES:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MCP_MAX_MESSAGE_BYTES:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def _json(doc: JsonValue, status: int = 200) -> JSONResponse:
    return JSONResponse(doc, status_code=status, media_type=_JSON)


@router.post(
    "/v1/mcp",
    operation_id="mcp_endpoint",
    response_model=None,
    responses=responses_for("mcp_endpoint", extra=_RESPONSES),
    openapi_extra=_BODY,
)
async def mcp_endpoint(ctx: Ctx, auth: Auth, request: Request) -> Response:
    """Public: the MCP server of one dashboard. Send one JSON-RPC 2.0 message with a
    dashboard read token (``Authorization: Bearer``) or a share session
    (``X-Mapi-Share``). Methods: ``initialize``, ``ping``, ``tools/list``, ``tools/call``
    and the ``notifications/initialized`` notification (``202``, no body)."""
    if not ctx.settings.mcp_enabled:
        raise MapiError("the MCP endpoint is turned off on this server", code=ErrorCode.NOT_FOUND)
    caller = await mcp_caller(ctx, request, auth.client_ip)
    body = await _read_body(request)
    try:
        msg = parse_message(body)
    except JsonRpcError as bad:
        return _json(error_response(bad), 400)
    if is_notification(msg):
        return Response(status_code=202)
    version = protocol_version_from_header(request.headers.get(MCP_PROTOCOL_HEADER))
    if version is None and msg.get("method") != "initialize":
        unsupported = JsonRpcError(
            INVALID_REQUEST,
            "Unsupported MCP-Protocol-Version",
            {"supported": list(MCP_PROTOCOL_VERSIONS)},
        )
        return _json(error_response(unsupported, _request_id(msg)), 400)
    dctx = caller.context

    async def call(name: str, args: dict[str, JsonValue]) -> ToolOutcome:
        return await tool_outcome(ctx, dctx, name, args)

    answer = await dispatch(msg, call=call, version=version or MCP_HTTP_DEFAULT_VERSION)
    if answer is None:  # pragma: no cover - notifications are answered above
        return Response(status_code=202)
    return _json(answer)


def _request_id(msg: dict[str, JsonValue]) -> str | int | None:
    rid = msg.get("id")
    return rid if isinstance(rid, (str, int)) and not isinstance(rid, bool) else None
