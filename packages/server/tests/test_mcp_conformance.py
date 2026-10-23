"""MCP conformance over HTTP (v0.20, plan D15-D18, WP8): every tool, both credentials.

An agent's session is driven end to end: ``initialize``, ``notifications/initialized``,
``tools/list`` and a ``tools/call`` of each of the 14 dashboard tools, once with a client's
read token and once with a share session. The dashboards are the v0.17 layout 2 upgrades of
``test_dashboard_tools``: a ``generic`` dashboard with scenario rows and batch scoring (the
two media tools answer ``DASHBOARD_INPUT_DISABLED`` there) and a media planner (the channel
and what-if tools). Each structured result equals what the tool gives in-process for the
same caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import pytest
from mapi_server.auth.request import resolve_dashboard_token
from mapi_server.context import ServerContext
from mapi_server.services.dashboards import sources
from mapi_server.services.dashboards.access import DashboardContext, token_context
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.readouts import Actor
from mapi_server.services.dashboards.shares import resolve_session
from mapi_server.services.dashboards.tools import call_tool
from mapi_server.settings import Settings
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, tools_json_schema
from mapi_spec.dashboards import ReadoutSnapshot
from mapi_spec.mcp import MCP_LATEST, MCP_SERVER_NAME
from starlette.requests import Request
from test_dashboard_tools import (
    BATCH,
    DETAIL,
    HISTORY,
    PREDICTION,
    SNAP_A,
    SNAP_B,
    _add,
    _as_mmm,
    _upgraded,
    _v2_snap,
)
from test_dashboards_crud import create_published, dashboard_kit
from test_mcp_endpoint import (
    MCP_SETTINGS,
    bearer,
    mcp_client,
    post_mcp,
    read_token,
    rpc,
    share_session,
    shared,
    text_of,
)

pytestmark = pytest.mark.integration

MEDIA: Final = frozenset({"get_channel", "evaluate_whatif"})


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=MCP_SETTINGS)


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    async with mcp_client(ctx) as c:
        yield c


async def _session(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    """The handshake every agent starts with."""
    body = await rpc(
        client,
        headers,
        "initialize",
        {"protocolVersion": MCP_LATEST, "capabilities": {},
         "clientInfo": {"name": "conformance", "version": "1"}},
        version=None,
    )  # fmt: skip
    assert body["result"]["protocolVersion"] == MCP_LATEST
    assert body["result"]["serverInfo"]["name"] == MCP_SERVER_NAME
    r = await post_mcp(client, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and not r.content
    listed = (await rpc(client, headers, "tools/list"))["result"]["tools"]
    assert [t["name"] for t in listed] == [t["name"] for t in tools_json_schema()]
    assert [t["inputSchema"] for t in listed] == [t["parameters"] for t in tools_json_schema()]


async def _context_of(ctx: ServerContext, headers: dict[str, str]) -> DashboardContext:
    """The caller's context as the endpoint resolves it (for the in-process comparison)."""
    if "Authorization" in headers:
        scope = {"type": "http", "headers": [(b"authorization", headers["Authorization"].encode())]}
        return await token_context(ctx, await resolve_dashboard_token(ctx, Request(scope)))
    return await resolve_session(ctx, next(iter(headers.values())), None, count=False)


async def _drive(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    calls: dict[str, dict[str, Any]],
    refused: dict[str, str],
) -> None:
    await _session(client, headers)
    dctx = await _context_of(ctx, headers)
    for rid, (name, args) in enumerate(calls.items(), start=10):
        body = await rpc(client, headers, "tools/call", {"name": name, "arguments": args}, rid=rid)
        result = body["result"]
        if name in refused:
            assert result["isError"] is True, (name, result)
            assert text_of(result)["code"] == refused[name], name
            continue
        assert result["isError"] is False, (name, text_of(result))
        expected = (await call_tool(ctx, dctx, name, args)).model_dump(mode="json")
        assert result["structuredContent"] == expected, name
        assert text_of(result) == expected, name


# ============================================================================== the generic


@pytest.fixture
async def generic(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    """A ``generic`` layout 2 dashboard (scenario rows and batch scoring on) with two
    refreshes, an actuals readout, and a private prediction and batch of ``cleo`` and of a
    share link (made before the readouts, so its own private ones exist)."""
    clear_grid_cache()
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    kit["client"] = client
    kit["dash_id"] = dash_id = await create_published(kit)

    def edit(d: dict[str, Any]) -> dict[str, Any]:
        out = [d["subject"]["variable"]]
        return {
            **d, "preset": "generic",
            "scenario_rows": {"enabled": True, "outputs": out,
                              "horizon": {"dim": "t", "max": 12, "default": 3}},
            "batch": {"enabled": True, "outputs": out, "id_column": "case"},
        }  # fmt: skip

    await _upgraded(ctx, kit, edit)
    kit["share_id"], kit["session"] = await share_session(kit, dash_id)
    a = _v2_snap("A.", [("next", "Orders next month", 400.0)])
    b = _v2_snap("B.", [("next", "Orders next month", 430.0)])
    actuals = _v2_snap("C.", [("next", "Orders next month", 430.0)],
                       accuracy_detail=DETAIL, accuracy_history=HISTORY)  # fmt: skip
    seqs: dict[str, int] = {
        "a": await _add(ctx, dash_id, "refresh", a),
        "b": await _add(ctx, dash_id, "refresh", b),
        "actuals": await _add(ctx, dash_id, "actuals", actuals),
    }
    for who, actor in (
        ("cleo", Actor("client", kit["users"]["cleo"].id)),
        ("share", Actor("share", kit["share_id"])),
    ):
        seqs[f"{who}_pred"] = await _add(
            ctx, dash_id, "prediction", _v2_snap("P.", [], prediction=PREDICTION),
            visibility="private", actor=actor,
        )  # fmt: skip
        seqs[f"{who}_batch"] = await _add(
            ctx, dash_id, "batch", _v2_snap("Q.", [], batch=BATCH),
            visibility="private", actor=actor,
        )  # fmt: skip
    kit["seqs"] = seqs
    return kit


def _generic_calls(seqs: dict[str, int], who: str) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {
        "get_dashboard": {},
        "get_latest_readout": {},
        "list_readouts": {"limit": 20},
        "get_readout": {"seq": seqs["a"]},
        "compare_readouts": {"a_seq": seqs["a"], "b_seq": seqs["b"]},
        "get_channel": {"name": "tv"},
        "explain_metric": {"metric": "prediction"},
        "evaluate_whatif": {"multipliers": {"tv": 0.8}},
        "list_panels": {},
        "get_panel": {"panel_id": "next", "seq": seqs["b"]},
        "get_accuracy": {},
        "list_inputs": {},
        "get_prediction": {"seq": seqs[f"{who}_pred"]},
        "get_batch_summary": {"seq": seqs[f"{who}_batch"]},
    }
    assert list(calls) == list(DASHBOARD_TOOL_NAMES)
    return calls


async def test_every_tool_with_a_client_read_token(
    ctx: ServerContext, generic: dict[str, Any]
) -> None:
    client: httpx.AsyncClient = generic["client"]
    headers = bearer(await read_token(generic["cleo"], generic["dash_id"]))
    calls = _generic_calls(generic["seqs"], "cleo")
    await _drive(ctx, client, headers, calls, dict.fromkeys(MEDIA, "DASHBOARD_INPUT_DISABLED"))
    # the share link's private rows are not the token owner's
    args = {"name": "get_prediction", "arguments": {"seq": generic["seqs"]["share_pred"]}}
    result = await rpc(client, headers, "tools/call", args)
    assert text_of(result["result"])["code"] == "NOT_FOUND"


async def test_every_tool_with_a_share_session(ctx: ServerContext, generic: dict[str, Any]) -> None:
    client: httpx.AsyncClient = generic["client"]
    headers = shared(generic["session"])
    calls = _generic_calls(generic["seqs"], "share")
    await _drive(ctx, client, headers, calls, dict.fromkeys(MEDIA, "DASHBOARD_INPUT_DISABLED"))
    args = {"name": "get_batch_summary", "arguments": {"seq": generic["seqs"]["cleo_batch"]}}
    result = await rpc(client, headers, "tools/call", args)
    assert text_of(result["result"])["code"] == "NOT_FOUND"


# ============================================================================== the planner


async def test_the_channel_and_whatif_tools_on_a_media_planner(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    clear_grid_cache()
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    kit["client"] = client
    kit["dash_id"] = dash_id = await create_published(kit, whatif={"enabled": True})
    spec = await _upgraded(ctx, kit, catalog_of=_as_mmm)
    assert spec.preset == "mmm_planner"
    seqs = []
    for snap in (SNAP_A, SNAP_B):
        parsed = ReadoutSnapshot.model_validate(
            {**snap, "schema_version": 2, "preset": "mmm_planner"}
        )
        seqs.append(await _add(ctx, dash_id, "refresh", parsed))
    calls: dict[str, dict[str, Any]] = {
        "get_latest_readout": {},
        "compare_readouts": {"a_seq": seqs[0], "b_seq": seqs[1]},
        "get_channel": {"name": "tv", "last_n": 5},
        "explain_metric": {"metric": "return_on_media"},
        "evaluate_whatif": {"multipliers": {"tv": 0.8}},
    }
    token = bearer(await read_token(kit["cleo"], dash_id))
    await _drive(ctx, client, token, calls, {})
    _, session = await share_session(kit, dash_id, allowed_inputs=["whatif"])
    await _drive(ctx, client, shared(session), calls, {})
    # a link without the what-if input reads the channels but cannot evaluate
    _, readonly = await share_session(kit, dash_id)
    await _drive(ctx, client, shared(readonly), calls,
                 {"evaluate_whatif": "DASHBOARD_INPUT_DISABLED"})  # fmt: skip
    channel = text_of(
        (await rpc(client, token, "tools/call",
                   {"name": "get_channel", "arguments": {"name": "tv"}}))["result"]
    )  # fmt: skip
    assert [p["seq"] for p in channel["points"]] == seqs
