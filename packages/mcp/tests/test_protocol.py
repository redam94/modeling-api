"""The full protocol through the bridge: stdio line → bridge → fake ``POST /v1/mcp`` (which
runs the real ``mapi_spec.mcp`` core) → one answer line."""

from __future__ import annotations

import json

import httpx
from mapi_mcp import Bridge, DashboardTokenCredential
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, tools_json_schema

from .conftest import API, FakeApi, rpc


def _bridge(api: FakeApi, transport: httpx.MockTransport) -> Bridge:
    return Bridge(API, DashboardTokenCredential(api.token), transport, clock=api.clock)


async def test_session_initialize_list_call(api: FakeApi, transport: httpx.MockTransport) -> None:
    async with _bridge(api, transport) as bridge:
        init = json.loads(
            await bridge.forward(rpc("initialize", {"protocolVersion": "2025-06-18"})) or ""
        )
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert bridge.protocol_version == "2025-06-18"
        assert (
            await bridge.forward('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
        )

        listed = json.loads(await bridge.forward(rpc("tools/list", rid=2)) or "")
        tools = listed["result"]["tools"]
        assert [t["name"] for t in tools] == list(DASHBOARD_TOOL_NAMES)
        assert [t["inputSchema"] for t in tools] == [t["parameters"] for t in tools_json_schema()]
        assert [t["description"] for t in tools] == [t["description"] for t in tools_json_schema()]

        called = json.loads(
            await bridge.forward(
                rpc("tools/call", {"name": "get_readout", "arguments": {"seq": 12}}, rid=3)
            )
            or ""
        )
        assert called["id"] == 3
        assert called["result"]["isError"] is False
        assert called["result"]["structuredContent"]["tool"] == "get_readout"
    assert api.tool_calls == [("get_readout", {"seq": 12})]
    # every request after initialize carries the negotiated revision
    versions = [r.headers.get("mcp-protocol-version") for r in api.requests]
    assert versions == [None, "2025-06-18", "2025-06-18", "2025-06-18"]
    for request in api.requests:
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["authorization"] == f"Bearer {api.token}"
        assert "cookie" not in request.headers
        assert "token" not in request.url.query.decode()


async def test_unknown_version_negotiates_latest(
    api: FakeApi, transport: httpx.MockTransport
) -> None:
    async with _bridge(api, transport) as bridge:
        init = json.loads(
            await bridge.forward(rpc("initialize", {"protocolVersion": "2024-11-05"})) or ""
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert bridge.protocol_version == "2025-11-25"


async def test_tool_error_is_is_error(api: FakeApi, transport: httpx.MockTransport) -> None:
    async with _bridge(api, transport) as bridge:
        reply = json.loads(
            await bridge.forward(
                rpc("tools/call", {"name": "get_readout", "arguments": {"seq": "x"}})
            )
            or ""
        )
    assert reply["result"]["isError"] is True
    assert json.loads(reply["result"]["content"][0]["text"])["code"] == "SCHEMA_INVALID"


async def test_unknown_tool_and_method(api: FakeApi, transport: httpx.MockTransport) -> None:
    async with _bridge(api, transport) as bridge:
        tool = json.loads(await bridge.forward(rpc("tools/call", {"name": "delete_all"})) or "")
        method = json.loads(await bridge.forward(rpc("resources/list", {}, rid=2)) or "")
        sampling = json.loads(await bridge.forward(rpc("sampling/createMessage", {}, rid=3)) or "")
    assert tool["error"]["code"] == -32602
    assert method["error"]["code"] == -32601 and method["id"] == 2
    assert sampling["error"]["code"] == -32601
    assert api.tool_calls == []


async def test_malformed_lines_never_leave_the_machine(
    api: FakeApi, transport: httpx.MockTransport
) -> None:
    async with _bridge(api, transport) as bridge:
        parse = json.loads(await bridge.forward("{not json") or "")
        batch = json.loads(await bridge.forward(f"[{rpc('ping')}]") or "")
        big = json.loads(await bridge.forward(rpc("ping", {"pad": "x" * 70_000})) or "")
        old = json.loads(await bridge.forward('{"jsonrpc":"1.0","id":5,"method":"ping"}') or "")
        assert await bridge.forward("   ") is None
    assert parse == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert batch["error"]["code"] == -32600
    assert big["error"]["code"] == -32600
    assert old["error"]["code"] == -32600 and old["id"] == 5
    assert api.requests == []


async def test_answers_are_single_lines(api: FakeApi, transport: httpx.MockTransport) -> None:
    async with _bridge(api, transport) as bridge:
        line = await bridge.forward(rpc("tools/call", {"name": "get_dashboard"}))
    assert line is not None and "\n" not in line and "\r" not in line
    text = json.loads(line)["result"]["content"][0]["text"]
    assert json.loads(text)["note"] == "line\nbreak"
