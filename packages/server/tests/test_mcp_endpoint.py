"""The MCP endpoint ``POST /v1/mcp`` (v0.20, plan D14-D18, WP8): the protocol over HTTP.

The dashboard is a published layout 2 (``forecast``) dashboard granted to the client
``cleo``, with a published readout (seq 2) and a private one of ``eve`` (seq 3) — the
:func:`test_token_context.v2_kit` of WP2. Agents call it with a dashboard read token (minted
through the web-session route) or a share session. The helpers at the top are shared with
``test_mcp_security`` and ``test_mcp_conformance``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
import pytest
from mapi_server.api import dashboard_read_tokens, mcp
from mapi_server.api.mcp_middleware import McpMiddleware
from mapi_server.app import create_app, iter_api_routes
from mapi_server.context import ServerContext
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller
from mapi_spec.dashboard_tools import tools_json_schema
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.mcp import (
    MCP_INSTRUCTIONS,
    MCP_LATEST,
    MCP_PROTOCOL_VERSIONS,
    MCP_SERVER_NAME,
    mcp_tools,
)
from test_dashboards_crud import LAYOUT1
from test_token_context import v2_kit

pytestmark = pytest.mark.integration

MCP: Final = "/v1/mcp"
VERSION_HEADER: Final = "MCP-Protocol-Version"


# ==================================================================================== helpers


def mcp_app(ctx: ServerContext) -> Any:
    """The app, with the MCP endpoint, its middleware and the read-token routes mounted even
    before ``app.py`` includes them (WP5 wires them)."""
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    ops = {(r.operation_id or r.name) for r, _ in iter_api_routes(app)}
    if "create_dashboard_read_token" not in ops:
        app.include_router(dashboard_read_tokens.router)
    if "mcp_endpoint" not in ops:
        app.include_router(mcp.router)
    if not any(mw.cls is McpMiddleware for mw in app.user_middleware):
        app.add_middleware(McpMiddleware, settings=ctx.settings)
    return app


MCP_SETTINGS: Final[dict[str, Any]] = {"profile": Profile.MULTI_TENANT, **LAYOUT1}
"""The settings of the MCP tests (layout 1 dashboards are made and upgraded here)."""


@asynccontextmanager
async def mcp_client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client of :func:`mcp_app` (the ``client`` fixture of the MCP tests)."""
    transport = httpx.ASGITransport(app=mcp_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


async def mcp_kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    """:func:`test_token_context.v2_kit` with the context and the client (``kit``)."""
    kit = await v2_kit(ctx, client)
    kit["ctx"] = ctx
    kit["client"] = client
    return kit


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=MCP_SETTINGS)


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    async with mcp_client(ctx) as c:
        yield c


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await mcp_kit(ctx, client)


async def read_token(caller: Caller, dash_id: str, name: str = "Claude Desktop") -> str:
    """A read token minted by ``caller``'s web session; returns the secret."""
    r = await caller.post(f"/v1/dashboards/{dash_id}/read-tokens", json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["secret"])


async def share_session(kit: dict[str, Any], dash_id: str, **body: Any) -> tuple[str, str]:
    """A share link on ``dash_id`` made by ``ana`` and a session for it: ``(share id,
    session)``."""
    r = await kit["ana"].post(f"/v1/dashboards/{dash_id}/shares", json=body)
    assert r.status_code == 201, r.text
    share = r.json()
    client: httpx.AsyncClient = kit["client"]
    r = await client.post("/v1/share-sessions", json={"token": share["token"]})
    assert r.status_code == 200, r.text
    return str(share["id"]), str(r.json()["session"])


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def shared(session: str) -> dict[str, str]:
    return {SHARE_SESSION_HEADER: session}


async def post_mcp(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    message: Any,
    *,
    version: str | None = MCP_LATEST,
    **kw: Any,
) -> httpx.Response:
    sent = {"Accept": "application/json, text/event-stream", **headers}
    if version is not None:
        sent[VERSION_HEADER] = version
    return await client.post(MCP, content=json.dumps(message).encode(), headers=sent, **kw)


async def rpc(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    rid: int | str = 1,
    version: str | None = MCP_LATEST,
) -> dict[str, Any]:
    """One JSON-RPC request; asserts HTTP 200 and returns the response object."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    r = await post_mcp(client, headers, msg, version=version)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body: dict[str, Any] = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == rid
    return body


async def call(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    name: str,
    arguments: dict[str, Any] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """``tools/call``; returns the ``result``."""
    body = await rpc(
        client, headers, "tools/call", {"name": name, "arguments": arguments or {}}, **kw
    )
    assert "error" not in body, body
    result: dict[str, Any] = body["result"]
    return result


def text_of(result: dict[str, Any]) -> Any:
    (block,) = result["content"]
    assert block["type"] == "text"
    return json.loads(block["text"])


# ================================================================================ handshake


async def test_initialize_negotiates_the_protocol_version(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    for asked in (*MCP_PROTOCOL_VERSIONS, "2024-11-05", "2099-01-01"):
        body = await rpc(
            client,
            headers,
            "initialize",
            {"protocolVersion": asked, "capabilities": {},
             "clientInfo": {"name": "test", "version": "1"}},
            version=None,
        )  # fmt: skip
        result = body["result"]
        expected = asked if asked in MCP_PROTOCOL_VERSIONS else MCP_LATEST
        assert result["protocolVersion"] == expected
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"] == {"name": MCP_SERVER_NAME, "version": "0.16.0"}
        assert result["instructions"] == MCP_INSTRUCTIONS
    # the initialized notification and other notifications: 202, no body
    for method in ("notifications/initialized", "notifications/cancelled"):
        r = await post_mcp(client, headers, {"jsonrpc": "2.0", "method": method})
        assert r.status_code == 202 and r.content == b"", (method, r.text)
    assert (await rpc(client, headers, "ping"))["result"] == {}
    # stateless: no session id is issued
    r = await post_mcp(client, headers, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert "mcp-session-id" not in r.headers


async def test_tools_list_over_http_is_the_tool_surface(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    result = (await rpc(client, headers, "tools/list"))["result"]
    assert "nextCursor" not in result
    tools = result["tools"]
    assert tools == mcp_tools()
    declared = tools_json_schema()
    assert [t["name"] for t in tools] == [t["name"] for t in declared]
    for got, want in zip(tools, declared, strict=True):
        assert got["description"] == want["description"]
        assert got["inputSchema"] == want["parameters"]
        assert got["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
        assert "outputSchema" not in got
    # a cursor is ignored (one page)
    again = await rpc(client, headers, "tools/list", {"cursor": "abc"})
    assert again["result"]["tools"] == tools


# ===================================================================================== tools


async def test_a_tool_call_returns_text_and_structured_content(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    result = await call(client, headers, "get_readout", {"seq": 2})
    assert result["isError"] is False
    payload = text_of(result)
    assert payload["seq"] == 2 and payload["title"] == "Readout 2"
    assert result["structuredContent"] == payload
    # 2025-06-18 (also assumed with no header) has structured content too
    for version in ("2025-06-18", None):
        result = await call(client, headers, "get_readout", {"seq": 2}, version=version)
        assert result["structuredContent"]["seq"] == 2 and text_of(result)["seq"] == 2


async def test_a_large_result_is_truncated_without_structured_content(
    kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    full = await call(client, headers, "get_dashboard")
    assert "structuredContent" in full
    monkeypatch.setattr(mcp, "MCP_TOOL_RESULT_MAX_BYTES", 400)
    result = await call(client, headers, "get_dashboard")
    assert result["isError"] is False and "structuredContent" not in result
    (block,) = result["content"]
    assert len(block["text"].encode()) <= 400 and "_truncated" in block["text"]


async def test_tool_refusals_are_tool_errors(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("get_readout", {"seq": 999}, "NOT_FOUND"),
        ("get_readout", {"seq": 3}, "NOT_FOUND"),  # eve's private readout
        ("get_readout", {"seq": "two"}, "SCHEMA_INVALID"),
        ("get_readout", {"seq": 2, "dashboard_id": "dsh_" + "0" * 26}, "SCHEMA_INVALID"),
        ("get_channel", {"name": "tv"}, "DASHBOARD_INPUT_DISABLED"),
    ]
    for name, args, code in cases:
        result = await call(client, headers, name, args)
        assert result["isError"] is True and "structuredContent" not in result, (name, args)
        error = text_of(result)
        assert set(error) == {"code", "message"} and error["code"] == code, (args, error)
        assert "pydantic" not in error["message"] and len(error["message"]) <= 1000
    # an unknown tool is a protocol error (MCP 2025-11-25), not a tool result
    body = await rpc(client, headers, "tools/call", {"name": "submit_run", "arguments": {}})
    assert body["error"]["code"] == -32602 and "result" not in body
    body = await rpc(client, headers, "tools/call", {"name": "get_readout", "arguments": [1]})
    assert body["error"]["code"] == -32602


async def test_private_readouts_are_absent_for_tokens(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    for who in ("cleo", "vic", "ana"):
        headers = bearer(await read_token(kit[who], kit["dash_id"], f"{who}'s agent"))
        listed = text_of(await call(client, headers, "list_readouts"))
        assert 3 not in {i["seq"] for i in listed["items"]}, who
        assert 2 in {i["seq"] for i in listed["items"]}, who


async def test_unknown_methods_and_bad_messages(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    for method in ("resources/list", "prompts/list", "sampling/createMessage", "tools/delete"):
        body = await rpc(client, headers, method, {})
        assert body["error"]["code"] == -32601, method
    # a batch, a parse error, not JSON-RPC 2.0: 400 with a JSON-RPC error body
    for content, code in (
        (json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode(), -32600),
        (b"{not json", -32700),
        (json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}).encode(), -32600),
    ):
        r = await client.post(MCP, content=content, headers=headers)
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == code and r.json()["jsonrpc"] == "2.0"


async def test_the_protocol_version_header_is_checked(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    ping = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
    r = await post_mcp(client, headers, ping, version="2099-01-01")
    assert r.status_code == 400
    assert r.json()["id"] == 7 and r.json()["error"]["code"] == -32600
    assert r.json()["error"]["data"] == {"supported": list(MCP_PROTOCOL_VERSIONS)}
    for version in (*MCP_PROTOCOL_VERSIONS, None):
        r = await post_mcp(client, headers, ping, version=version)
        assert r.status_code == 200, (version, r.text)
    # initialize negotiates in its body, whatever the header says
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"}}  # fmt: skip
    r = await post_mcp(client, headers, init, version="2099-01-01")
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-06-18"


async def test_2025_03_26_which_needs_batches_is_not_offered(kit: dict[str, Any]) -> None:
    # mcp-1: batches are refused under every revision in effect, so the revision that
    # requires them is neither negotiated nor accepted in the header
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"}}  # fmt: skip
    r = await post_mcp(client, headers, init, version=None)
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == MCP_LATEST
    ping = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
    r = await post_mcp(client, headers, ping, version="2025-03-26")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    batch = [{"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}]  # fmt: skip
    for version in (*MCP_PROTOCOL_VERSIONS, None):
        r = await post_mcp(client, headers, batch, version=version)
        assert r.status_code == 400 and r.json()["error"]["code"] == -32600, version


async def test_share_sessions_reach_the_same_tools(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    share_id, session = await share_session(kit, kit["dash_id"])
    headers = shared(session)
    result = await rpc(client, headers, "initialize", {"protocolVersion": MCP_LATEST})
    assert result["result"]["protocolVersion"] == MCP_LATEST
    assert (await rpc(client, headers, "tools/list"))["result"]["tools"] == mcp_tools()
    latest = text_of(await call(client, headers, "get_latest_readout"))
    assert latest["seq"] == 2
    listed = text_of(await call(client, headers, "list_readouts"))
    assert 3 not in {i["seq"] for i in listed["items"]}
    assert share_id.startswith("dss_")


async def test_the_endpoint_can_be_turned_off(kit: dict[str, Any]) -> None:
    ctx: ServerContext = kit["ctx"]
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    ctx.settings = ctx.settings.model_copy(update={"mcp_enabled": False})
    r = await post_mcp(client, headers, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


async def test_openapi_documents_a_generic_json_body(ctx: ServerContext) -> None:
    app = mcp_app(ctx)
    doc = app.openapi()
    op = doc["paths"][MCP]["post"]
    assert op["operationId"] == "mcp_endpoint"
    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"type": "object"}
    assert {"200", "202"} <= set(op["responses"])
