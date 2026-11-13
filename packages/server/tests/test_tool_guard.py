"""One capped dashboard tool call for chat and MCP (v0.20, plan I-6 and D11).

:func:`call_tool_capped` returns the capped result or the tool's refusal (never raises a
:class:`MapiError`), flags trimmed results, keeps private readouts with their actor and
analysts, and refuses a dashboard made with uploaded code to share links, client users and
read tokens (analyst members keep it).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.access import (
    DashboardContext,
    member_context,
    token_context,
)
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.services.dashboards.shares import resolve_session
from mapi_server.services.dashboards.tool_guard import call_tool_capped, guarded_like_clients
from mapi_server.settings import Settings
from mapi_spec.chat import CHAT_TOOL_RESULT_MAX_BYTES, MCP_TOOL_RESULT_MAX_BYTES
from mapi_spec.tool_results import MIN_CAP_BYTES, canonical_tool_json
from sqlalchemy import select
from test_dashboards_crud import LAYOUT1
from test_token_context import UPLOADED, pin_uploaded, token_for, v2_kit

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=LAYOUT1)


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    kit = await v2_kit(ctx, client)
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/shares", json={})
    assert r.status_code == 201, r.text
    r = await client.post("/v1/share-sessions", json={"token": r.json()["token"]})
    assert r.status_code == 200, r.text
    kit["share_session"] = r.json()["session"]
    return kit


async def _contexts(ctx: ServerContext, kit: dict[str, Any]) -> dict[str, DashboardContext]:
    """A context of every kind of caller on the kit's dashboard."""
    out: dict[str, DashboardContext] = {}
    for who in ("eve", "cleo"):
        auth = await owner_auth(ctx, "default", "default", kit["users"][who].id)
        assert auth is not None
        out[who] = await member_context(ctx.for_request(auth), auth, kit["dash_id"])
    out["share"] = await resolve_session(ctx, kit["share_session"], "127.0.0.1")
    out["eve_token"] = await token_context(ctx, token_for(kit, "eve"))
    out["cleo_token"] = await token_context(ctx, token_for(kit, "cleo"))
    return out


async def test_results_are_the_tool_results_within_the_budget(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dctx = (await _contexts(ctx, kit))["cleo"]
    capped, err, ms = await call_tool_capped(
        ctx, dctx, "get_readout", {"seq": 2}, max_bytes=CHAT_TOOL_RESULT_MAX_BYTES
    )
    assert err is None and capped is not None and ms >= 0
    assert capped.truncated is False
    assert isinstance(capped.payload, dict) and capped.payload["seq"] == 2
    assert capped.text == canonical_tool_json(capped.payload)
    assert json.loads(capped.text) == capped.payload
    assert capped.original_bytes == len(capped.text.encode())


async def test_a_large_result_is_trimmed_and_flagged(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dctx = (await _contexts(ctx, kit))["eve"]
    full, err, _ = await call_tool_capped(
        ctx, dctx, "get_dashboard", {}, max_bytes=MCP_TOOL_RESULT_MAX_BYTES
    )
    assert err is None and full is not None and not full.truncated
    budget = max(MIN_CAP_BYTES, full.original_bytes // 3)
    capped, err, _ = await call_tool_capped(ctx, dctx, "get_dashboard", {}, max_bytes=budget)
    assert err is None and capped is not None
    assert capped.truncated is True
    assert len(capped.text.encode()) <= budget
    assert capped.original_bytes == full.original_bytes


async def test_refusals_are_returned_not_raised(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dctx = (await _contexts(ctx, kit))["cleo"]
    for name, args, code in (
        ("delete_dashboard", {}, "SCHEMA_INVALID"),  # no such tool
        ("get_readout", {"seq": 2, "dashboard_id": "dsh_x"}, "SCHEMA_INVALID"),  # extra arg
        ("get_readout", {"seq": "two"}, "SCHEMA_INVALID"),
        ("get_readout", {"seq": 99}, "NOT_FOUND"),
        ("get_channel", {"name": "tv"}, "DASHBOARD_INPUT_DISABLED"),  # no media planner
    ):
        capped, err, ms = await call_tool_capped(ctx, dctx, name, args, max_bytes=4096)
        assert capped is None and err is not None, name
        assert err.code.value == code, (name, err.code)
        assert ms >= 0


async def test_private_readouts_stay_with_their_actor_and_analysts(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    contexts = await _contexts(ctx, kit)

    async def seqs(dctx: DashboardContext) -> set[int]:
        capped, err, _ = await call_tool_capped(
            ctx, dctx, "list_readouts", {"limit": 50}, max_bytes=MCP_TOOL_RESULT_MAX_BYTES
        )
        assert err is None and capped is not None and isinstance(capped.payload, dict)
        items = capped.payload["items"]
        assert isinstance(items, list)
        return {int(i["seq"]) for i in items if isinstance(i, dict)}

    assert 3 in await seqs(contexts["eve"])  # an analyst sees every readout
    for who in ("share", "cleo", "cleo_token"):
        assert 3 not in await seqs(contexts[who]), who
        assert 2 in await seqs(contexts[who]), who
    # eve's own private readout over her read token: still hers (actor), never others'
    assert 3 in await seqs(contexts["eve_token"])
    for who in ("share", "cleo", "cleo_token"):
        _, err, _ = await call_tool_capped(ctx, contexts[who], "get_readout", {"seq": 3},
                                           max_bytes=4096)  # fmt: skip
        assert err is not None and err.code.value == "NOT_FOUND", who


async def test_chat_tool_on_overlay_readout(ctx: ServerContext, kit: dict[str, Any]) -> None:
    contexts = await _contexts(ctx, kit)
    assert not guarded_like_clients(contexts["eve"])
    assert all(guarded_like_clients(contexts[k]) for k in ("cleo", "share", "eve_token"))
    await pin_uploaded(ctx, kit)
    capped, err, _ = await call_tool_capped(
        ctx, contexts["eve"], "list_readouts", {"limit": 5}, max_bytes=4096
    )
    assert err is None and capped is not None  # analysts: inside the workspace
    # the contexts were made before the pin: the read tokens' member context is refused by
    # the guard itself (run_tool checks only client and share principals)
    for who in ("cleo", "share", "eve_token", "cleo_token"):
        for name, args in (("get_dashboard", {}), ("get_readout", {"seq": 2})):
            capped, err, _ = await call_tool_capped(ctx, contexts[who], name, args, max_bytes=4096)
            assert capped is None and err is not None, (who, name)
            assert err.code.value == UPLOADED, (who, name)
    # hiding the readouts does not lift it: the pinned version itself used uploaded code
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, kit["dash_id"])
        assert dash is not None
        for row in (await s.execute(select(m.DashboardReadout))).scalars():
            row.visibility = "hidden"
    _, err, _ = await call_tool_capped(ctx, contexts["share"], "get_dashboard", {}, max_bytes=4096)
    assert err is not None and err.code.value == UPLOADED
