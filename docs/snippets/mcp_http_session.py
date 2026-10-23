"""Connect a chat tool with MCP (v0.20): one MCP session over streamable HTTP with a dashboard
read token.

Included by ``docs/guides/connect-a-chat-tool-with-mcp.md``. ``conformance/tests/
test_docs_chat_mcp.py`` calls :func:`session` against an in-process API with a fixture
dashboard and a real read token. Run on its own (``test_docs_snippets.py``), it talks to
the MCP protocol core of the platform (:mod:`mapi_spec.mcp`) through an in-memory transport
with a fixed tool result: no server and no network.

Against your own API: set ``MAPI_API_URL`` and ``MAPI_DASHBOARD_TOKEN`` and run the file.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

# --8<-- [start:session]
PROTOCOL = "2025-11-25"


async def rpc(http: httpx.AsyncClient, rid: int, method: str, **params: Any) -> Any:
    """Send one JSON-RPC request to POST /v1/mcp and return its result."""
    r = await http.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        headers={"MCP-Protocol-Version": PROTOCOL},
    )
    r.raise_for_status()  # 401: the token is not valid; 404: no access to the dashboard
    answer = r.json()
    if "error" in answer:
        raise RuntimeError(answer["error"]["message"])
    return answer["result"]


async def session(http: httpx.AsyncClient) -> dict[str, Any]:
    """Initialize, list the tools and read the latest readout of the token's dashboard."""
    hello = await rpc(http, 1, "initialize", protocolVersion=PROTOCOL, capabilities={},
                      clientInfo={"name": "docs-example", "version": "1.0"})  # fmt: skip
    print(hello["serverInfo"]["name"], hello["protocolVersion"])  # mapi-dashboards 2025-11-25
    # tell the server that the client is ready (a notification: 202, no body)
    await http.post("/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={"MCP-Protocol-Version": PROTOCOL})  # fmt: skip

    tools = await rpc(http, 2, "tools/list")
    print(len(tools["tools"]), tools["tools"][0]["name"])  # 14 get_dashboard

    called = await rpc(http, 3, "tools/call", name="get_latest_readout", arguments={})
    if called["isError"]:  # a tool error is a result, not a JSON-RPC error
        raise RuntimeError(called["content"][0]["text"])
    readout: dict[str, Any] = json.loads(called["content"][0]["text"])
    print(readout["seq"], readout["title"])
    return readout


def client(api_url: str, read_token: str) -> httpx.AsyncClient:
    """The token goes in the Authorization header, never in the URL."""
    headers = {
        "Authorization": f"Bearer {read_token}",
        "Accept": "application/json, text/event-stream",
    }
    return httpx.AsyncClient(base_url=api_url, headers=headers, timeout=30)


# --8<-- [end:session]


def offline() -> httpx.AsyncClient:
    """A client whose transport runs the platform's MCP protocol core in memory, with one
    fixed readout as the result of every tool."""
    from mapi_spec.mcp import ToolOutcome, dispatch, parse_message, protocol_version_from_header

    readout = {"seq": 2, "title": "Weekly readout", "lead": "Sales were above the expectation."}

    async def call(name: str, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(payload=readout, text=json.dumps(readout), truncated=False)

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        msg = parse_message(request.content)
        version = protocol_version_from_header(request.headers.get("mcp-protocol-version"))
        answer = await dispatch(msg, call=call, version=version or PROTOCOL)
        return httpx.Response(202) if answer is None else httpx.Response(200, json=answer)

    return httpx.AsyncClient(
        base_url="http://mcp.invalid",
        headers={"Authorization": "Bearer docs-example"},
        transport=httpx.MockTransport(handle),
    )


async def main() -> None:
    url, token = os.environ.get("MAPI_API_URL"), os.environ.get("MAPI_DASHBOARD_TOKEN")
    async with client(url, token) if url and token else offline() as http:
        await session(http)


if __name__ == "__main__":
    asyncio.run(main())
