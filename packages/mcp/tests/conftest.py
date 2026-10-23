"""A fake control plane for the bridge tests: ``POST /v1/mcp`` runs the real protocol core
(:func:`mapi_spec.mcp.dispatch`) over a scripted tool, and the share-session routes mint
sessions with a controllable clock. Everything runs in-process through
``httpx.MockTransport``; no test opens a socket."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mapi_spec.mcp import (
    JsonRpcError,
    ToolOutcome,
    dispatch,
    error_response,
    parse_message,
    protocol_version_from_header,
)
from mapi_spec.tokens import generate_token
from pydantic import JsonValue

API = "https://api.example.test"
SHARE_TOKEN = "mpd_" + "7k2m9q4x01" * 4
PASSCODE = "correct horse 42"
SESSION_TTL = 1800.0


def make_session(n: int) -> str:
    body = f"eyJzaWQiOiJkc3NfMDEiLCJuIjo{n:04d}"
    return f"mps_{body}." + "A" * 43


@dataclass
class FakeApi:
    """Records every request; answers like the control plane would."""

    token: str = field(default_factory=lambda: str(generate_token()))
    now: float = 1_800_000_000.0
    sessions_opened: int = 0
    sessions_refreshed: int = 0
    valid_sessions: set[str] = field(default_factory=set)
    session_expiry: dict[str, float] = field(default_factory=dict)
    link_ended: bool = False
    requests: list[httpx.Request] = field(default_factory=list)
    tool_calls: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    status_override: int | None = None
    redirect: bool = False
    note: str = "line\nbreak"

    def clock(self) -> float:
        return self.now

    # ---------------------------------------------------------------- share sessions

    def _mint(self) -> httpx.Response:
        session = make_session(self.sessions_opened + self.sessions_refreshed)
        self.valid_sessions.add(session)
        self.session_expiry[session] = self.now + SESSION_TTL
        expires = datetime.fromtimestamp(self.now + SESSION_TTL, UTC).isoformat()
        return httpx.Response(200, json={"session": session, "expires_at": expires,
                                         "dashboard": {"id": "dsh_x"}})  # fmt: skip

    def _session_ok(self, request: httpx.Request) -> bool:
        session = request.headers.get("x-mapi-share")
        return (
            session in self.valid_sessions and self.session_expiry[session] > self.now
            if session
            else False
        )

    def _problem(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(
            status,
            json={"type": "about:blank", "title": code, "status": status, "code": code,
                  "detail": "secret detail must not be shown"},
            headers={"content-type": "application/problem+json", "retry-after": "7"},
        )  # fmt: skip

    # ---------------------------------------------------------------- handler

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.redirect:
            return httpx.Response(307, headers={"location": "https://evil.example/v1/mcp"})
        if path == "/v1/share-sessions" and request.method == "POST":
            if self.link_ended:
                return self._problem(410, "LINK_EXPIRED")
            body = json.loads(request.content)
            if body.get("token") != SHARE_TOKEN or body.get("passcode") != PASSCODE:
                return self._problem(403, "SHARE_PASSCODE_INVALID")
            self.sessions_opened += 1
            return self._mint()
        if path == "/v1/share-sessions:refresh" and request.method == "POST":
            if self.link_ended:
                return self._problem(410, "LINK_EXPIRED")
            if not self._session_ok(request):
                return self._problem(401, "UNAUTHENTICATED")
            self.sessions_refreshed += 1
            return self._mint()
        if path == "/v1/mcp" and request.method == "POST":
            return await self._mcp(request)
        return self._problem(404, "NOT_FOUND")

    async def _mcp(self, request: httpx.Request) -> httpx.Response:
        if self.status_override is not None:
            return self._problem(self.status_override, "RATE_LIMITED")
        if self.link_ended and "x-mapi-share" in request.headers:
            return self._problem(410, "LINK_EXPIRED")
        auth = request.headers.get("authorization")
        if auth is not None:
            if auth != f"Bearer {self.token}":
                return self._problem(401, "UNAUTHENTICATED")
        elif not self._session_ok(request):
            return self._problem(401, "UNAUTHENTICATED")
        version = protocol_version_from_header(request.headers.get("mcp-protocol-version"))
        if version is None:
            return httpx.Response(400, json=error_response(JsonRpcError(-32600, "Bad version")))
        try:
            msg = parse_message(request.content)
        except JsonRpcError as err:
            return httpx.Response(400, json=error_response(err))

        async def call(name: str, args: dict[str, JsonValue]) -> ToolOutcome:
            self.tool_calls.append((name, args))
            if name == "get_readout" and not isinstance(args.get("seq"), int):
                return ToolOutcome(None, "", False, ("SCHEMA_INVALID", "seq: integer"))
            payload: dict[str, JsonValue] = {"tool": name, "seq": 12, "note": self.note}
            return ToolOutcome(payload, json.dumps(payload), False)

        reply = await dispatch(msg, call=call, version=version)
        if reply is None:
            return httpx.Response(202)
        return httpx.Response(200, json=reply)


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def transport(api: FakeApi) -> httpx.MockTransport:
    return httpx.MockTransport(api.handler)


def rpc(method: str, params: dict[str, Any] | None = None, rid: Any = 1) -> str:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)
