"""CORS and credential handling of the public share-link routes (v0.16, plan D13).

``/v1/share-sessions`` and ``/v1/shared/…`` are read by the web app's share page without an
account: the share session travels in ``X-Mapi-Share``, never in a cookie. For these paths
:class:`ShareCorsMiddleware` (outside the app's CORS middleware, which is the signed-in
app's, with credentials)

* drops ``Cookie`` and ``Authorization`` from the request (no credential is ever looked at,
  even by mistake) and its ``Origin`` before the app's CORS middleware sees it;
* answers preflights itself (``GET`` and ``POST`` from the web origin, with the share,
  idempotency and ``Prefer`` headers; no credentials);
* adds ``Access-Control-Allow-Origin: <web origin>`` (no credentials) for that origin,
  ``Vary: Origin`` to all, drops any ``Set-Cookie``, and marks every response
  ``Cache-Control: private, no-store`` and ``Referrer-Policy: no-referrer``.
"""

from __future__ import annotations

from typing import Final

from mapi_spec.api import IDEMPOTENCY_KEY_HEADER, PREFER_HEADER
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["SHARE_PATHS", "SHARE_RESPONSE_HEADERS", "ShareCorsMiddleware", "is_share_path"]

SHARE_PATHS: Final = ("/v1/shared/", "/v1/share-sessions")
"""Path prefixes of the public share-link routes (``/v1/share-sessions:refresh`` included)."""

SHARE_RESPONSE_HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (b"cache-control", b"private, no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"x-content-type-options", b"nosniff"),
)
_ALLOW_HEADERS: Final = ", ".join(
    ("Content-Type", SHARE_SESSION_HEADER, IDEMPOTENCY_KEY_HEADER, PREFER_HEADER)
).encode("latin-1")
_EXPOSE_HEADERS: Final = b"Retry-After, Preference-Applied, Location"
_DROPPED_REQUEST: Final = frozenset({b"cookie", b"authorization", b"origin"})
_REPLACED_RESPONSE: Final = frozenset(
    {b"set-cookie", b"vary", b"cache-control", b"referrer-policy", b"x-robots-tag"}
)


def is_share_path(path: str) -> bool:
    return path.startswith(SHARE_PATHS)


class ShareCorsMiddleware:
    """See the module docstring. ``allowed_origin`` is the web app origin."""

    def __init__(self, app: ASGIApp, *, allowed_origin: str) -> None:
        self.app = app
        self.allowed = allowed_origin.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_share_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        origin = ""
        headers: list[tuple[bytes, bytes]] = []
        for name, value in scope.get("headers", []):
            lname = name.lower()
            if lname == b"origin":
                origin = value.decode("latin-1")
            if lname in _DROPPED_REQUEST:
                continue
            headers.append((name, value))
        allowed = origin.rstrip("/") == self.allowed
        origin_bytes = self.allowed.encode("latin-1")
        if scope.get("method") == "OPTIONS":
            out = [(b"vary", b"Origin"), (b"content-length", b"0"), *SHARE_RESPONSE_HEADERS]
            if allowed:
                out += [
                    (b"access-control-allow-origin", origin_bytes),
                    (b"access-control-allow-methods", b"GET, POST"),
                    (b"access-control-allow-headers", _ALLOW_HEADERS),
                    (b"access-control-max-age", b"600"),
                ]
            await send(
                {"type": "http.response.start", "status": 204 if allowed else 403, "headers": out}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        inner = {**scope, "headers": headers}

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-")
                    and k.lower() not in _REPLACED_RESPONSE
                ]
                raw += [(b"vary", b"Origin"), *SHARE_RESPONSE_HEADERS]
                if allowed:
                    raw += [
                        (b"access-control-allow-origin", origin_bytes),
                        (b"access-control-expose-headers", _EXPOSE_HEADERS),
                    ]
                message = {**message, "headers": raw}
            await send(message)

        await self.app(inner, receive, send_with_cors)
