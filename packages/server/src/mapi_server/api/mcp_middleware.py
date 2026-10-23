"""Transport rules of the MCP endpoint ``/v1/mcp`` (v0.20, plan D18-D19).

The MCP endpoint is called by agents (desktop chat apps, IDEs, the ``mapi-mcp`` stdio
bridge), not by the web app, and it authenticates with a dashboard read token or a share
session only (plan D14). :class:`McpMiddleware` applies the transport rules for the path
``/v1/mcp`` exactly, before anything else of the app sees the request:

* ``Cookie`` is dropped (a signed-in browser session is never a credential here) and so is
  ``Origin``, after it was checked, so the app's credentialed CORS rules never apply;
* credentials in the query string (``?token=``, ``?key=``, a token-shaped value) answer
  ``401``: URLs end up in logs, histories and traces;
* a request with an ``Origin`` that is not in ``MAPI_MCP_ALLOWED_ORIGINS`` answers ``403``
  (DNS rebinding; MCP 2025-11-25 "Security Warning"); an allowed origin gets
  ``Access-Control-Allow-Origin`` and never ``Access-Control-Allow-Credentials``;
* the ``Host`` must be one of the API's own hosts (``api_url`` and ``MAPI_ALLOWED_HOSTS``,
  never the ``*`` wildcard), else ``421 Misdirected Request``;
* ``GET`` and ``DELETE`` answer ``405`` (no server-sent event stream and no session to
  end: the endpoint is stateless); ``OPTIONS`` is a CORS preflight, answered here;
* every response is ``Cache-Control: no-store``, ``Referrer-Policy: no-referrer``, without
  ``Set-Cookie``.

Register it outside the app's ``CORSMiddleware`` (so the preflight and the dropped
``Origin`` happen first): ``app.add_middleware(McpMiddleware, settings=settings)`` after the
``CORSMiddleware`` line, like :class:`~mapi_server.api.share_cors.ShareCorsMiddleware`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Final
from urllib.parse import parse_qsl

from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.errors import ERROR_INFO, PROBLEM_CONTENT_TYPE, ErrorCode, Problem
from mapi_spec.mcp import INVALID_REQUEST, JSONRPC_VERSION
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mapi_server.settings import Settings

__all__ = [
    "MCP_PATH",
    "MCP_RESPONSE_HEADERS",
    "QUERY_CREDENTIAL_NAMES",
    "McpMiddleware",
    "mcp_hosts",
]

MCP_PATH: Final = "/v1/mcp"
"""The MCP endpoint (the only path these rules apply to)."""

MCP_RESPONSE_HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
)
QUERY_CREDENTIAL_NAMES: Final = frozenset(
    {
        "token",
        "key",
        "api_key",
        "apikey",
        "access_token",
        "auth",
        "authorization",
        "bearer",
        "session",
        "share",
        "x-mapi-share",
        "passcode",
        "password",
        "secret",
    }
)
"""Query parameter names refused as credentials (compared in lower case)."""
_TOKEN_PREFIXES: Final = ("mapi_", "mps_", "mpd_")
"""Token-shaped values (API tokens, share sessions, share links) refused in any parameter."""

_ALLOW_METHODS: Final = b"POST"
_ALLOW_HEADERS: Final = ", ".join(
    ("Content-Type", "Accept", "Authorization", SHARE_SESSION_HEADER, "MCP-Protocol-Version")
).encode("latin-1")
_DROPPED_REQUEST: Final = frozenset({b"cookie", b"origin"})
_REPLACED_RESPONSE: Final = frozenset(
    {b"set-cookie", b"vary", b"cache-control", b"referrer-policy", b"x-content-type-options"}
)


def mcp_hosts(settings: Settings) -> list[str]:
    """The ``Host`` values the MCP endpoint answers: the API's trusted hosts without the
    ``*`` wildcard (``*.example.com`` patterns stay)."""
    return [h.lower() for h in settings.trusted_hosts() if h != "*"]


def _host_ok(host: str, allowed: Sequence[str]) -> bool:
    if not host:
        return False
    if host.startswith("["):  # [::1]:8000
        name = host[1 : host.find("]")] if "]" in host else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.lower().rstrip(".")
    for pattern in allowed:
        if pattern.startswith("*.") and name.endswith(pattern[1:]):
            return True
        if name == pattern:
            return True
    return False


def _query_has_credentials(raw: bytes) -> bool:
    if not raw:
        return False
    try:
        pairs = parse_qsl(raw.decode("latin-1"), keep_blank_values=True)
    except ValueError:  # pragma: no cover - parse_qsl does not raise without strict_parsing
        return True
    for name, value in pairs:
        if name.strip().lower() in QUERY_CREDENTIAL_NAMES:
            return True
        if value.strip().lower().startswith(_TOKEN_PREFIXES):
            return True
    return False


def _normal_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


async def _send(
    send: Send,
    status: int,
    headers: Iterable[tuple[bytes, bytes]],
    body: bytes = b"",
    content_type: bytes | None = None,
) -> None:
    out = [*headers, *MCP_RESPONSE_HEADERS, (b"content-length", str(len(body)).encode())]
    if content_type is not None:
        out.append((b"content-type", content_type))
    await send({"type": "http.response.start", "status": status, "headers": out})
    await send({"type": "http.response.body", "body": body})


async def _problem(
    send: Send, code: ErrorCode, detail: str, headers: Iterable[tuple[bytes, bytes]] = ()
) -> None:
    body = Problem.from_code(code, detail).model_dump_json(exclude_none=True).encode()
    await _send(send, ERROR_INFO[code].status, headers, body, PROBLEM_CONTENT_TYPE.encode())


async def _jsonrpc_error(send: Send, status: int, message: str) -> None:
    """An HTTP error whose body is a JSON-RPC error response with no ``id`` (MCP 2025-11-25:
    the transport may answer this way when it cannot accept a message)."""
    doc = {"jsonrpc": JSONRPC_VERSION, "id": None, "error": {"code": INVALID_REQUEST,
                                                             "message": message}}  # fmt: skip
    await _send(send, status, (), json.dumps(doc).encode(), b"application/json")


class McpMiddleware:
    """See the module docstring. ``settings`` gives the allowed origins
    (``mcp_allowed_origins``) and the API's hosts."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.origins = frozenset(_normal_origin(o) for o in settings.mcp_allowed_origins)
        self.hosts = mcp_hosts(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        origin: str | None = None
        host = ""
        headers: list[tuple[bytes, bytes]] = []
        for name, value in scope.get("headers", []):
            lname = name.lower()
            if lname == b"origin":
                origin = value.decode("latin-1")
            elif lname == b"host":
                host = value.decode("latin-1")
            if lname in _DROPPED_REQUEST:
                continue
            headers.append((name, value))
        if not _host_ok(host, self.hosts):
            await _jsonrpc_error(send, 421, "This server does not answer for that host name")
            return
        allowed = origin is not None and _normal_origin(origin) in self.origins
        if origin is not None and not allowed:
            await _problem(
                send,
                ErrorCode.FORBIDDEN,
                "requests from this web origin are not accepted; an operator can add the "
                "origin to MAPI_MCP_ALLOWED_ORIGINS",
                [(b"vary", b"Origin")],
            )
            return
        cors: list[tuple[bytes, bytes]] = [(b"vary", b"Origin")]
        if allowed:
            assert origin is not None
            cors += [
                (b"access-control-allow-origin", origin.strip().encode("latin-1")),
                (b"access-control-expose-headers", b"Retry-After, WWW-Authenticate"),
            ]
        method = scope.get("method", "")
        if method == "OPTIONS":
            if allowed:
                cors += [
                    (b"access-control-allow-methods", _ALLOW_METHODS),
                    (b"access-control-allow-headers", _ALLOW_HEADERS),
                    (b"access-control-max-age", b"600"),
                ]
            await _send(send, 204, cors)
            return
        if method != "POST":
            await _send(send, 405, [*cors, (b"allow", b"POST, OPTIONS")])
            return
        if _query_has_credentials(scope.get("query_string", b"")):
            await _problem(
                send,
                ErrorCode.UNAUTHENTICATED,
                "do not send credentials in the URL; send the read token in the "
                "Authorization header or the share session in X-Mapi-Share",
                [*cors, (b"www-authenticate", b"Bearer")],
            )
            return
        inner = {**scope, "headers": headers}

        async def send_with_rules(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-")
                    and k.lower() not in _REPLACED_RESPONSE
                ]
                message = {**message, "headers": [*raw, *cors, *MCP_RESPONSE_HEADERS]}
            await send(message)

        await self.app(inner, receive, send_with_rules)
