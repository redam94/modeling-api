"""The MCP protocol core (v0.20, ``mapi_spec.mcp``): parsing, negotiation, tools, dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mapi_spec.chat import MCP_TOOL_RESULT_MAX_BYTES
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, tools_json_schema
from mapi_spec.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_HTTP_DEFAULT_VERSION,
    MCP_INSTRUCTIONS,
    MCP_LATEST,
    MCP_MAX_MESSAGE_BYTES,
    MCP_METHODS,
    MCP_PROTOCOL_VERSIONS,
    MCP_SERVER_NAME,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcError,
    ToolOutcome,
    dispatch,
    error_response,
    is_notification,
    mcp_tools,
    negotiate,
    parse_message,
    protocol_version_from_header,
)
from mapi_spec.tool_results import cap_tool_result
from pydantic import JsonValue

Calls = list[tuple[str, dict[str, JsonValue]]]


def _req(method: str, params: dict[str, Any] | None = None, rid: Any = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _caller(outcome: ToolOutcome, calls: Calls | None = None) -> Any:
    async def call(name: str, args: dict[str, JsonValue]) -> ToolOutcome:
        if calls is not None:
            calls.append((name, args))
        return outcome

    return call


_OK = ToolOutcome({"seq": 3, "title": "Week 12"}, '{"seq":3,"title":"Week 12"}', False)


async def _run(msg: dict[str, Any], *, version: str = MCP_LATEST, outcome: ToolOutcome = _OK,
               calls: Calls | None = None) -> dict[str, Any] | None:  # fmt: skip
    parsed = parse_message(json.dumps(msg).encode())
    return await dispatch(parsed, call=_caller(outcome, calls), version=version)


# ============================================================================ constants


def test_constants() -> None:
    assert MCP_PROTOCOL_VERSIONS == ("2025-11-25", "2025-06-18")
    assert MCP_PROTOCOL_VERSIONS[0] == MCP_LATEST
    assert {
        "initialize", "notifications/initialized", "ping", "tools/list", "tools/call",
    } == MCP_METHODS  # fmt: skip
    assert MCP_SERVER_NAME == "mapi-dashboards"
    assert MCP_MAX_MESSAGE_BYTES == 65_536
    assert MCP_TOOL_RESULT_MAX_BYTES == 65_536


def test_instructions_are_fixed_plain_text() -> None:
    assert MCP_INSTRUCTIONS.strip() == MCP_INSTRUCTIONS
    assert "{" not in MCP_INSTRUCTIONS and "http" not in MCP_INSTRUCTIONS


# ============================================================================ tools/list


def test_mcp_tools_mirror_tools_json_schema() -> None:
    tools = mcp_tools()
    schema = tools_json_schema()
    assert [t["name"] for t in tools] == list(DASHBOARD_TOOL_NAMES)
    for tool, ref in zip(tools, schema, strict=True):
        assert set(tool) == {"name", "description", "inputSchema", "annotations"}
        assert tool["name"] == ref["name"]
        assert tool["description"] == ref["description"]
        assert tool["inputSchema"] == ref["parameters"]
        assert tool["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
        assert "outputSchema" not in tool


def test_mcp_tools_returns_a_fresh_copy() -> None:
    first = mcp_tools()
    first[0]["name"] = "changed"
    assert mcp_tools()[0]["name"] == DASHBOARD_TOOL_NAMES[0]


def test_input_schemas_are_objects() -> None:
    for tool in mcp_tools():
        schema = tool["inputSchema"]
        assert isinstance(schema, dict) and schema["type"] == "object"


async def test_tools_list_dispatch() -> None:
    reply = await _run(_req("tools/list", {"cursor": "ignored"}))
    assert reply is not None
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {"tools": mcp_tools()}}
    assert "nextCursor" not in reply["result"]


# ============================================================================ initialize


@pytest.mark.parametrize("version", MCP_PROTOCOL_VERSIONS)
async def test_initialize_echoes_each_supported_version(version: str) -> None:
    reply = await _run(
        _req("initialize", {"protocolVersion": version, "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}})
    )  # fmt: skip
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "mapi-dashboards"
    assert result["serverInfo"]["version"] == "0.16.0"
    assert result["instructions"] == MCP_INSTRUCTIONS


@pytest.mark.parametrize("requested", ["2024-11-05", "2099-01-01", None, 7])
async def test_initialize_unknown_version_answers_latest(requested: Any) -> None:
    params = {} if requested is None else {"protocolVersion": requested}
    reply = await _run(_req("initialize", params))
    assert reply is not None and reply["result"]["protocolVersion"] == MCP_LATEST


def test_negotiate() -> None:
    assert negotiate("2025-06-18") == "2025-06-18"
    assert negotiate("2024-11-05") == MCP_LATEST
    assert negotiate(None) == MCP_LATEST


def test_protocol_version_from_header() -> None:
    assert protocol_version_from_header(None) == MCP_HTTP_DEFAULT_VERSION == "2025-06-18"
    # mcp-1: 2025-03-26 requires batches, which this server refuses: it is not offered
    assert protocol_version_from_header("2025-03-26") is None
    assert negotiate("2025-03-26") == MCP_LATEST
    assert protocol_version_from_header("2025-11-25") == "2025-11-25"
    assert protocol_version_from_header(" 2025-06-18 ") == "2025-06-18"
    assert protocol_version_from_header("2024-11-05") is None
    assert protocol_version_from_header("") is None


# ============================================================================ notifications, ping


async def test_notifications_get_no_answer() -> None:
    assert await _run({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # unknown notifications are ignored as well (a notification is never answered)
    assert await _run({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
    # a response from the client (to a server request) is accepted and not answered
    assert await _run({"jsonrpc": "2.0", "id": 4, "result": {}}) is None


def test_is_notification() -> None:
    assert is_notification({"jsonrpc": "2.0", "method": "x"})
    assert is_notification({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert not is_notification({"jsonrpc": "2.0", "id": 1, "method": "ping"})


async def test_ping() -> None:
    assert await _run(_req("ping", rid="a-1")) == {"jsonrpc": "2.0", "id": "a-1", "result": {}}


# ============================================================================ errors


@pytest.mark.parametrize(
    "method",
    ["resources/list", "resources/read", "prompts/list", "prompts/get", "sampling/createMessage",
     "roots/list", "elicitation/create", "completion/complete", "logging/setLevel",
     "server/discover", "tools/delete", "notifications/initialized"],
)  # fmt: skip
async def test_unknown_methods_answer_32601(method: str) -> None:
    reply = await _run(_req(method, {}))
    error = {"code": METHOD_NOT_FOUND, "message": "Method not found"}
    assert reply == {"jsonrpc": "2.0", "id": 1, "error": error}


@pytest.mark.parametrize(
    "body",
    [b"{", b"not json", b"\xff\xfe", b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "x": NaN}',
     b"[[[[[[[[[["],
)  # fmt: skip
def test_parse_errors(body: bytes) -> None:
    with pytest.raises(JsonRpcError) as info:
        parse_message(body)
    assert info.value.code in (PARSE_ERROR, INVALID_REQUEST)


def test_deep_nesting_is_a_parse_error() -> None:
    body = b"[" * 60_000 + b"]" * 5_000
    with pytest.raises(JsonRpcError) as info:
        parse_message(body[:MCP_MAX_MESSAGE_BYTES])
    assert info.value.code == PARSE_ERROR


def test_batch_refused() -> None:
    body = json.dumps([_req("ping"), _req("ping", rid=2)]).encode()
    with pytest.raises(JsonRpcError) as info:
        parse_message(body)
    assert info.value.code == INVALID_REQUEST == -32600
    assert error_response(info.value)["id"] is None


def test_too_large_refused() -> None:
    pad = "x" * MCP_MAX_MESSAGE_BYTES
    with pytest.raises(JsonRpcError) as info:
        parse_message(json.dumps(_req("ping", {"pad": pad})).encode())
    assert info.value.code == INVALID_REQUEST


@pytest.mark.parametrize(
    "msg",
    [
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
        {"jsonrpc": "2.0", "id": "x" * 300, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "method": ""},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        "ping",
        7,
    ],
)
def test_invalid_requests(msg: Any) -> None:
    with pytest.raises(JsonRpcError) as info:
        parse_message(json.dumps(msg).encode())
    assert info.value.code == INVALID_REQUEST


def test_invalid_request_keeps_a_known_id() -> None:
    with pytest.raises(JsonRpcError) as info:
        parse_message(json.dumps({"jsonrpc": "1.0", "id": 9, "method": "ping"}).encode())
    assert error_response(info.value)["id"] == 9


def test_params_must_be_an_object() -> None:
    with pytest.raises(JsonRpcError) as info:
        parse_message(json.dumps(_req("tools/list", ["a"])).encode())  # type: ignore[arg-type]
    assert info.value.code == INVALID_PARAMS and info.value.request_id == 1


# ============================================================================ tools/call


async def test_tools_call_ok_has_structured_content() -> None:
    calls: Calls = []
    reply = await _run(
        _req("tools/call", {"name": "get_readout", "arguments": {"seq": 3}}), calls=calls
    )
    assert calls == [("get_readout", {"seq": 3})]
    assert reply is not None
    assert reply["result"] == {
        "content": [{"type": "text", "text": _OK.text}],
        "structuredContent": _OK.payload,
        "isError": False,
    }


async def test_tools_call_without_arguments() -> None:
    calls: Calls = []
    await _run(_req("tools/call", {"name": "get_dashboard"}), calls=calls)
    await _run(_req("tools/call", {"name": "get_dashboard", "arguments": None}), calls=calls)
    assert calls == [("get_dashboard", {}), ("get_dashboard", {})]


async def test_every_supported_revision_gets_structured_content() -> None:
    for version in MCP_PROTOCOL_VERSIONS:
        reply = await _run(_req("tools/call", {"name": "get_dashboard"}), version=version)
        assert reply is not None and "structuredContent" in reply["result"], version


async def test_truncated_result_has_no_structured_content() -> None:
    big = {"seq": 1, "points": list(range(40_000))}
    capped = cap_tool_result("get_channel", big, MCP_TOOL_RESULT_MAX_BYTES)
    assert capped.truncated
    outcome = ToolOutcome(capped.payload, capped.text, capped.truncated)
    reply = await _run(
        _req("tools/call", {"name": "get_channel", "arguments": {}}), outcome=outcome
    )
    assert reply is not None
    result = reply["result"]
    assert "structuredContent" not in result and result["isError"] is False
    assert result["content"][0]["text"] == capped.text
    assert len(capped.text.encode()) <= MCP_TOOL_RESULT_MAX_BYTES


async def test_non_object_payload_is_text_only() -> None:
    outcome = ToolOutcome([1, 2], "[1,2]", False)
    reply = await _run(_req("tools/call", {"name": "list_panels"}), outcome=outcome)
    assert reply is not None and "structuredContent" not in reply["result"]


async def test_tool_error_is_is_error_result() -> None:
    outcome = ToolOutcome(None, "", False, ("SCHEMA_INVALID", "seq: Input should be an integer"))
    reply = await _run(
        _req("tools/call", {"name": "get_readout", "arguments": {"seq": "x"}}), outcome=outcome
    )
    assert reply is not None
    result = reply["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"]) == {
        "code": "SCHEMA_INVALID", "message": "seq: Input should be an integer",
    }  # fmt: skip


async def test_unknown_tool_is_a_protocol_error() -> None:
    calls: Calls = []
    reply = await _run(_req("tools/call", {"name": "submit_run", "arguments": {}}), calls=calls)
    assert calls == []
    assert reply is not None
    assert reply["error"] == {"code": -32602, "message": "Unknown tool: submit_run"}


async def test_unknown_tool_name_echo_is_bounded() -> None:
    reply = await _run(_req("tools/call", {"name": "é" * 500}))
    assert reply is not None
    assert len(reply["error"]["message"]) <= len("Unknown tool: ") + 64
    assert reply["error"]["message"].isascii()


@pytest.mark.parametrize(
    "params",
    [{}, {"name": 5}, {"name": ""}, {"name": "get_readout", "arguments": [1]},
     {"name": "get_readout", "arguments": "seq=1"}],
)  # fmt: skip
async def test_invalid_call_params(params: dict[str, Any]) -> None:
    calls: Calls = []
    reply = await _run(_req("tools/call", params), calls=calls)
    assert calls == []
    assert reply is not None and reply["error"]["code"] == INVALID_PARAMS


async def test_caller_exceptions_propagate() -> None:
    class Ended(Exception):
        pass

    async def call(name: str, args: dict[str, JsonValue]) -> ToolOutcome:
        raise Ended

    msg = parse_message(json.dumps(_req("tools/call", {"name": "get_dashboard"})).encode())
    with pytest.raises(Ended):
        await dispatch(msg, call=call, version=MCP_LATEST)


async def test_caller_jsonrpc_error_is_answered() -> None:
    async def call(name: str, args: dict[str, JsonValue]) -> ToolOutcome:
        raise JsonRpcError(-32603, "Internal error")

    msg = parse_message(json.dumps(_req("tools/call", {"name": "get_dashboard"}, rid=7)).encode())
    reply = await dispatch(msg, call=call, version=MCP_LATEST)
    assert reply == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32603, "message": "Internal error"},
    }


async def test_tool_surface_has_no_dashboard_argument() -> None:
    for tool in mcp_tools():
        props = tool["inputSchema"].get("properties", {})  # type: ignore[union-attr]
        assert not {"dashboard_id", "dashboard", "tenant_id", "project_id"} & set(props)
