"""Forward MCP messages from stdio to the control plane's ``POST /v1/mcp``.

:class:`Bridge` takes one JSON-RPC line, checks its shape locally (so a malformed line never
leaves the machine), sends it with the credential, and gives back the answer as one line
(or ``None`` for a notification). It never changes a message and never reads tool results.

* A dashboard read token goes in ``Authorization: Bearer``.
* A share link is exchanged for a share session (``POST /v1/share-sessions``) that goes in
  ``X-Mapi-Share``. The session is refreshed :data:`REFRESH_MARGIN_S` seconds before it
  expires (``POST /v1/share-sessions:refresh``), and opened again once if the server says it
  has ended (``401``). A revoked or expired link (``410``) ends every call.
* After ``initialize`` succeeds, each request carries ``MCP-Protocol-Version`` with the
  negotiated revision.
* HTTP failures become JSON-RPC errors in the server range (``-32000`` to ``-32099``) with
  fixed text. Response bodies, request bodies and credentials are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from importlib import metadata as _metadata
from typing import Any, Final, cast

import httpx
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.mcp import (
    INTERNAL_ERROR,
    MCP_MAX_MESSAGE_BYTES,
    MCP_METHODS,
    MCP_PROTOCOL_VERSIONS,
    JsonRpcError,
    RequestId,
    error_response,
    is_notification,
    parse_message,
)
from pydantic import JsonValue

from mapi_mcp.credentials import Credential, DashboardTokenCredential, ShareLinkCredential

__all__ = [
    "API_UNREACHABLE",
    "CREDENTIAL_REFUSED",
    "LINK_ENDED",
    "MCP_PATH",
    "RATE_LIMITED",
    "REFRESH_MARGIN_S",
    "SERVER_ERROR",
    "Bridge",
    "BridgeError",
]

MCP_PATH: Final = "/v1/mcp"
SESSIONS_PATH: Final = "/v1/share-sessions"
REFRESH_PATH: Final = "/v1/share-sessions:refresh"
REFRESH_MARGIN_S: Final = 120.0
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S: Final = 60.0

# JSON-RPC server-error codes used by the bridge
API_UNREACHABLE: Final = -32000
CREDENTIAL_REFUSED: Final = -32001
LINK_ENDED: Final = -32002
RATE_LIMITED: Final = -32003
SERVER_ERROR: Final = -32004

_ACCEPT: Final = "application/json, text/event-stream"


def _user_agent() -> str:
    try:
        return f"mapi-mcp/{_metadata.version('mapi-mcp')}"
    except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "mapi-mcp"


_USER_AGENT: Final = _user_agent()

log = logging.getLogger("mapi_mcp")


class BridgeError(Exception):
    """A failure to reach or use the API, as a JSON-RPC error (fixed, plain text)."""

    def __init__(self, code: int, message: str, data: JsonValue | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_jsonrpc(self) -> JsonRpcError:
        return JsonRpcError(self.code, self.message, self.data)


def json_line(obj: JsonValue) -> str:
    # json.dumps escapes newlines inside strings, so the output is always one line
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _problem_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) and len(code) <= 64 else None


def _status_error(response: httpx.Response) -> BridgeError:
    """A fixed JSON-RPC error for an HTTP refusal of the API."""
    status = response.status_code
    data: dict[str, JsonValue] = {"status": status}
    code = _problem_code(response)
    if code is not None:
        data["code"] = code
    if status in (401, 403):
        return BridgeError(
            CREDENTIAL_REFUSED,
            "The API refused the credential. Check the token or the share link and passcode.",
            data,
        )
    if status == 404:
        return BridgeError(
            CREDENTIAL_REFUSED, "The dashboard is not available with this credential.", data
        )
    if status == 410:
        return BridgeError(LINK_ENDED, "The share link has ended. Ask for a new link.", data)
    if status == 429:
        retry = response.headers.get("retry-after")
        if retry is not None and retry.isdigit():
            data["retry_after_s"] = int(retry)
        return BridgeError(RATE_LIMITED, "Too many requests. Wait and try again.", data)
    if 300 <= status < 400:
        return BridgeError(SERVER_ERROR, "The API sent a redirect. Check MAPI_API_URL.", data)
    return BridgeError(SERVER_ERROR, "The API could not answer the request.", data)


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Bridge:
    """One credential, one API: forwards JSON-RPC lines to ``POST {api_url}/v1/mcp``."""

    def __init__(
        self,
        api_url: str,
        credential: Credential,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._credential = credential
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": _USER_AGENT},
        )
        self._session: str | None = None
        self._session_expires: float = 0.0
        self._session_lock = asyncio.Lock()
        self._version: str | None = None

    @property
    def protocol_version(self) -> str | None:
        """The revision negotiated by the last successful ``initialize`` (``None`` before)."""
        return self._version

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Bridge:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ share sessions

    async def _share_post(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.post(self._api_url + path, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("cannot reach the API to open a share session (%s)", type(exc).__name__)
            raise BridgeError(API_UNREACHABLE, "The API is not reachable.") from None

    def _take_session(self, response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            body = None
        session = body.get("session") if isinstance(body, dict) else None
        expires = _parse_time(body.get("expires_at")) if isinstance(body, dict) else None
        if not isinstance(session, str) or expires is None:
            raise BridgeError(SERVER_ERROR, "The API sent an invalid share session.")
        self._session = session
        self._session_expires = expires

    async def _open_session(self, cred: ShareLinkCredential) -> None:
        body: dict[str, str] = {"token": cred.share_token}
        if cred.passcode is not None:
            body["passcode"] = cred.passcode
        response = await self._share_post(SESSIONS_PATH, json=body)
        if response.status_code != 200:
            self._session = None
            log.warning("the API refused to open a share session (HTTP %s)", response.status_code)
            raise _status_error(response)
        self._take_session(response)
        log.info("share session opened")

    async def _refresh_session(self, cred: ShareLinkCredential) -> None:
        assert self._session is not None
        response = await self._share_post(
            REFRESH_PATH, headers={SHARE_SESSION_HEADER: self._session}
        )
        if response.status_code == 200:
            self._take_session(response)
            log.info("share session refreshed")
            return
        if response.status_code == 410:
            self._session = None
            raise _status_error(response)
        await self._open_session(cred)

    async def _share_session(self, cred: ShareLinkCredential, *, reopen: bool = False) -> str:
        async with self._session_lock:
            now = self._clock()
            if reopen or self._session is None or now >= self._session_expires:
                await self._open_session(cred)
            elif now >= self._session_expires - REFRESH_MARGIN_S:
                await self._refresh_session(cred)
            assert self._session is not None
            return self._session

    # ------------------------------------------------------------------ forwarding

    async def _auth_headers(self, *, reopen: bool = False) -> dict[str, str]:
        cred = self._credential
        if isinstance(cred, DashboardTokenCredential):
            return {"Authorization": f"Bearer {cred.token}"}
        return {SHARE_SESSION_HEADER: await self._share_session(cred, reopen=reopen)}

    async def _post(self, body: bytes, *, reopen: bool = False) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
            **(await self._auth_headers(reopen=reopen)),
        }
        if self._version is not None:
            headers["MCP-Protocol-Version"] = self._version
        try:
            return await self._client.post(self._api_url + MCP_PATH, content=body, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("cannot reach the API (%s)", type(exc).__name__)
            raise BridgeError(API_UNREACHABLE, "The API is not reachable.") from None

    async def _send(self, body: bytes) -> httpx.Response:
        response = await self._post(body)
        if response.status_code == 401 and isinstance(self._credential, ShareLinkCredential):
            # the share session ended (link changed or session superseded): open it again once
            log.info("share session ended; opening it again")
            response = await self._post(body, reopen=True)
        return response

    def _answer(
        self, response: httpx.Response, msg: dict[str, JsonValue], rid: RequestId | None
    ) -> dict[str, JsonValue]:
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise BridgeError(SERVER_ERROR, "The API answer is too large.")
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or answer.get("jsonrpc") != "2.0":
            if response.status_code == 200:
                raise BridgeError(SERVER_ERROR, "The API sent an invalid answer.")
            raise _status_error(response)
        if rid is not None and answer.get("id") is None:
            answer["id"] = rid
        if msg.get("method") == "initialize" and isinstance(answer.get("result"), dict):
            negotiated = cast(dict[str, JsonValue], answer["result"]).get("protocolVersion")
            if isinstance(negotiated, str) and negotiated in MCP_PROTOCOL_VERSIONS:
                self._version = negotiated
        return cast(dict[str, JsonValue], answer)

    async def forward(self, line: str) -> str | None:
        """Send one JSON-RPC line to the API; the answer line, or ``None`` when nothing must
        be written back (a notification, a client response, an empty line)."""
        text = line.strip()
        if not text:
            return None
        body = text.encode("utf-8", "replace")
        if len(body) > MCP_MAX_MESSAGE_BYTES:
            return json_line(error_response(JsonRpcError(-32600, "Message too large")))
        try:
            msg = parse_message(body)
        except JsonRpcError as err:
            return json_line(error_response(err))
        notification = is_notification(msg)
        rid = None if notification else cast(RequestId, msg["id"])
        method = msg.get("method")
        try:
            response = await self._send(body)
            if notification:
                if response.status_code not in (200, 202):
                    log.warning("the API refused a notification (HTTP %s)", response.status_code)
                return None
            answer = self._answer(response, msg, rid)
        except BridgeError as exc:
            if notification:
                return None
            return json_line(error_response(exc.as_jsonrpc(), rid))
        except Exception as exc:  # never crash the stdio loop on one message
            log.error("unexpected failure (%s)", type(exc).__name__)
            if notification:
                return None
            return json_line(error_response(JsonRpcError(INTERNAL_ERROR, "Internal error"), rid))
        shown = method if method in MCP_METHODS else "other"
        log.info("%s -> HTTP %s", shown, response.status_code)
        return json_line(answer)
