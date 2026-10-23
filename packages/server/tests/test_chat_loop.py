"""One chat turn with the fake model (v0.20, plan D1, D2, D6, D10 and I-5).

The loop runs the model's function calls through the real dashboard tools for the caller's
own context, caps them at six per turn, answers with cleaned, signed text and writes one usage
row per turn. Model failures answer ``CHAT_UNAVAILABLE`` without provider text. The helpers
here (:func:`chat_kit`, :func:`member`, :func:`share`, :func:`ask`) are shared by the other
v0.20 WP4 tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.chat import loop as chat_loop
from mapi_server.services.chat.budget import estimate_micros
from mapi_server.services.chat.fake import FAKE_MODEL_ID, FakeChatModel, FakeStep
from mapi_server.services.chat.history import chat_key, sign_turn
from mapi_server.services.chat.loop import (
    BLOCKED_REPLY,
    TOOL_LIMIT_REPLY,
    run_chat_turn,
)
from mapi_server.services.chat.model import ModelUsage
from mapi_server.services.chat.principal import ChatPrincipal
from mapi_server.services.chat.prompt import readout_note, system_prompt
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.settings import Settings
from mapi_spec.chat import (
    CHAT_MAX_TOOL_CALLS,
    ChatRequest,
    ChatResponse,
)
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES
from mapi_spec.errors import MapiError
from sqlalchemy import func, select, update
from test_dashboards_crud import LAYOUT1
from test_token_context import v2_kit

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parent / "fixtures" / "chat_injection"

SNAPSHOT: Final[dict[str, Any]] = {
    "lead": "Sales were above the expectation.",
    "schema_version": 2,
    "preset": "forecast",
    "tiles": [
        {"key": "expected", "label": "Expected sales", "value": 1234567.0, "unit": "count",
         "delta": 0.042},
        {"key": "vs_expected", "label": "Against expected", "value": 2.35, "unit": "ratio"},
    ],
    "notes_plain": ["The holiday week is in this readout."],
}  # fmt: skip


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"chat_provider": "fake", **LAYOUT1})


async def enable_chat(ctx: ServerContext, dash_id: str, **over: Any) -> None:
    """Turn chat on for every audience (share links too) unless ``over`` says otherwise."""
    values: dict[str, Any] = {
        "enabled": True,
        "aud_members": True,
        "aud_clients": True,
        "aud_share": True,
        "keep_transcripts": False,
        **over,
    }
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None
        row = await s.get(m.DashboardChatSettings, (dash.tenant_id, dash_id))
        if row is None:
            row = m.DashboardChatSettings(
                tenant_id=dash.tenant_id, project_id=dash.project_id, dashboard_id=dash_id
            )
            s.add(row)
        for key, value in values.items():
            setattr(row, key, value)


async def set_snapshot(
    ctx: ServerContext, dash_id: str, seq: int, snapshot: dict[str, Any], title: str | None = None
) -> None:
    async with ctx.db.session() as s:
        stmt = (
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == seq)
            .values(snapshot=snapshot, **({"title": title} if title is not None else {}))
        )
        await s.execute(stmt)


async def chat_kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    """:func:`test_token_context.v2_kit` with chat on, a readout with numbers (seq 2) and a
    share session (``share_session``, link ``share_id``)."""
    kit = await v2_kit(ctx, client)
    await set_snapshot(ctx, kit["dash_id"], 2, SNAPSHOT)
    await enable_chat(ctx, kit["dash_id"])
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/shares", json={})
    assert r.status_code == 201, r.text
    kit["share_id"] = r.json()["id"]
    r = await client.post("/v1/share-sessions", json={"token": r.json()["token"]})
    assert r.status_code == 200, r.text
    kit["share_session"] = r.json()["session"]
    return kit


async def member(ctx: ServerContext, kit: dict[str, Any], who: str = "eve") -> ChatPrincipal:
    auth = await owner_auth(ctx, "default", "default", kit["users"][who].id)
    assert auth is not None
    return ChatPrincipal.for_member(ctx, None, auth, kit["dash_id"])


def share(ctx: ServerContext, kit: dict[str, Any]) -> ChatPrincipal:
    return ChatPrincipal.for_share(ctx, None, kit["share_session"])


def request(*texts: str, **over: Any) -> ChatRequest:
    """A request from alternating texts (user first); assistant texts need ``sigs``."""
    sigs: list[str] = list(over.pop("sigs", []))
    messages: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        if i % 2 == 0:
            messages.append({"role": "user", "text": text})
        else:
            messages.append({"role": "assistant", "text": text, "sig": sigs.pop(0)})
    return ChatRequest.model_validate({"messages": messages, **over})


async def ask(
    ctx: ServerContext,
    principal: ChatPrincipal,
    kit: dict[str, Any],
    text: str = "What does the latest readout show?",
    *,
    model: FakeChatModel | None = None,
    req: ChatRequest | None = None,
) -> ChatResponse:
    return await run_chat_turn(
        ctx, principal, kit["dash_id"], req or request(text), model=model or FakeChatModel()
    )


async def usage_rows(ctx: ServerContext) -> list[m.DashboardChatUsage]:
    async with ctx.db.session() as s:
        return list((await s.execute(select(m.DashboardChatUsage))).scalars())


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await chat_kit(ctx, client)


# ================================================================================ answers


async def test_the_default_fake_model_answers_from_the_tools(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    model = FakeChatModel()
    res = await ask(ctx, await member(ctx, kit), kit, model=model)
    assert res.reply.role == "assistant" and res.reply.sig is not None
    assert "1,234,567" in res.reply.text and "Readout 2" in res.reply.text
    assert [c.name for c in res.tool_calls] == ["get_latest_readout"]
    assert res.tool_calls[0].ok and res.tool_calls[0].code is None
    assert res.notices == []  # every number is grounded
    assert res.usage.model == FAKE_MODEL_ID and res.usage.total_tokens > 0
    assert res.conversation_id.startswith("cnv_") and res.transcript_kept is False
    # the model saw the fixed system text and the dashboard tools only
    assert all(system == system_prompt() for system, _ in model.calls)
    assert all(set(seen) == set(DASHBOARD_TOOL_NAMES) for seen in model.tools_seen)
    # the signature binds caller, dashboard, conversation, index and text
    p = await member(ctx, kit)
    want = sign_turn(chat_key(ctx.secrets), p, kit["dash_id"], res.conversation_id, 1,
                     res.reply.text)  # fmt: skip
    assert res.reply.sig == want


async def test_a_conversation_continues_with_its_signed_history(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    p = await member(ctx, kit)
    first = await ask(ctx, p, kit)
    req = request(
        "What does the latest readout show?",
        first.reply.text,
        "What changed since the last readout?",
        sigs=[first.reply.sig],
        conversation_id=first.conversation_id,
    )
    model = FakeChatModel()
    second = await ask(ctx, await member(ctx, kit), kit, model=model, req=req)
    assert second.conversation_id == first.conversation_id
    roles = [t.role for t in model.calls[0][1]]
    assert roles == ["user", "model", "user"]  # history as text turns, nothing else
    assert all("functionCall" not in part for t in model.calls[0][1] for part in t.parts)


async def test_one_usage_row_per_turn_with_the_estimated_cost(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [
        FakeStep(call=("get_latest_readout", {}), usage=1000, thoughts=200),
        FakeStep(text="Readout 2 shows 1,234,567.", usage=500),
    ]
    res = await ask(ctx, await member(ctx, kit, "cleo"), kit, model=FakeChatModel(script))
    assert res.usage.total_tokens == 1700 and res.usage.thought_tokens == 200
    rows = await usage_rows(ctx)
    assert len(rows) == 1
    row = rows[0]
    assert row.principal_kind == "client" and row.principal_id == kit["users"]["cleo"].id
    assert row.share_id is None and row.outcome == "ok" and row.tool_calls == 1
    assert row.model == FAKE_MODEL_ID
    assert row.input_tokens + row.output_tokens + row.thought_tokens == 1700
    assert row.thought_tokens == 200
    usage = ModelUsage(input=1200, output=300, thoughts=200, cached=0, total=1700)
    assert row.est_micros == estimate_micros(usage, ctx.settings)
    assert row.price_version == ctx.settings.chat_price_version
    s = ctx.settings
    want = -(-(1200 * s.chat_price_in_micros_per_mtok + 500 * s.chat_price_out_micros_per_mtok)
             // 1_000_000)  # fmt: skip
    assert row.est_micros == want


async def test_thinking_tokens_are_counted_in_the_daily_budget(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [FakeStep(text="Readout 2 is the latest readout.", usage=100, thoughts=900)]
    await ask(ctx, await member(ctx, kit), kit, model=FakeChatModel(script))
    async with ctx.db.session() as s:
        count = (
            await s.execute(
                select(m.RateCounter.count).where(
                    m.RateCounter.subject == f"chattok:{kit['dash_id']}"
                )
            )
        ).scalar_one()
    assert count == 1000


async def test_the_readout_note_comes_before_the_question(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    model = FakeChatModel()
    await ask(ctx, await member(ctx, kit), kit, model=model,
              req=request("What does this readout show?", readout_seq=2))  # fmt: skip
    first_user = model.calls[0][1][-1]
    assert first_user.parts == (
        {"text": readout_note(2)},
        {"text": "What does this readout show?"},
    )


# ============================================================================== tool limit


async def test_at_most_six_tool_calls_then_the_tool_limit_notice(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [FakeStep(call=("list_readouts", {"limit": 5})) for _ in range(8)]
    script.append(FakeStep(text="I stopped."))
    model = FakeChatModel(script)
    res = await ask(ctx, await member(ctx, kit), kit, model=model)
    assert len(res.tool_calls) == CHAT_MAX_TOOL_CALLS
    assert "tool_limit" in res.notices
    # the 7th call was answered with the limit error, the 8th ended the turn
    assert res.reply.text == TOOL_LIMIT_REPLY
    assert len(model.calls) == 8
    seventh = model.calls[7][1][-1].parts[0]["functionResponse"]["response"]
    assert seventh["error"]["code"] == "TOOL_LIMIT"
    rows = await usage_rows(ctx)
    assert rows[0].tool_calls == CHAT_MAX_TOOL_CALLS


async def test_parallel_calls_over_the_limit_are_refused_and_the_model_answers(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [FakeStep(call=("get_latest_readout", {})) for _ in range(CHAT_MAX_TOOL_CALLS)]
    script += [FakeStep(call=("list_readouts", {})), FakeStep(text="Readout 2 is the latest.")]
    res = await ask(ctx, await member(ctx, kit), kit, model=FakeChatModel(script))
    assert res.reply.text == "Readout 2 is the latest."
    assert "tool_limit" in res.notices and len(res.tool_calls) == CHAT_MAX_TOOL_CALLS


async def test_an_unknown_tool_is_schema_invalid_and_counted(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [
        FakeStep(call=("submit_run", {"spec": "x"})),
        FakeStep(call=("get_readout", {"seq": 2, "dashboard_id": "dsh_other"})),
        FakeStep(text="I could not do that."),
    ]
    model = FakeChatModel(script)
    res = await ask(ctx, await member(ctx, kit), kit, model=model)
    assert [(c.name, c.ok, c.code) for c in res.tool_calls] == [
        ("unknown_tool", False, "SCHEMA_INVALID"),
        ("get_readout", False, "SCHEMA_INVALID"),
    ]
    assert res.tool_calls[0].args == {}  # an unknown tool's arguments are not echoed
    response = model.calls[1][1][-1].parts[0]["functionResponse"]["response"]
    assert response["error"]["code"] == "SCHEMA_INVALID"
    assert (await usage_rows(ctx))[0].tool_calls == 2


async def test_a_tool_error_goes_back_to_the_model_as_data(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    model = FakeChatModel([FakeStep(call=("get_readout", {"seq": 99})), FakeStep(text="None.")])
    res = await ask(ctx, await member(ctx, kit), kit, model=model)
    assert res.tool_calls[0].code == "NOT_FOUND"
    part = model.calls[1][1][-1].parts[0]["functionResponse"]
    assert part["id"] == "fake-call-0" and part["name"] == "get_readout"
    assert set(part["response"]) == {"error"}


async def test_a_large_result_is_trimmed_for_the_model(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    big = {**SNAPSHOT, "notes_plain": ["n" * 390] * 20, "technical": {"t": "x" * 30_000}}
    await set_snapshot(ctx, kit["dash_id"], 2, big)
    model = FakeChatModel([FakeStep(call=("get_readout", {"seq": 2})), FakeStep(text="Done.")])
    res = await ask(ctx, await member(ctx, kit), kit, model=model)
    assert res.tool_calls[0].truncated is True and "truncated" in res.notices
    response = model.calls[1][1][-1].parts[0]["functionResponse"]["response"]
    assert len(json.dumps(response["result"]).encode()) <= 24_576 + 100


# ================================================================================ failures


async def test_a_model_error_is_chat_unavailable_without_provider_text(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    for kind in ("timeout", "rate_limited", "unavailable", "bad_response", "auth"):
        model = FakeChatModel([FakeStep(call=("get_latest_readout", {})), FakeStep(error=kind)])
        with pytest.raises(MapiError) as err:
            await ask(ctx, await member(ctx, kit), kit, model=model)
        assert err.value.code.value == "CHAT_UNAVAILABLE", kind
        assert "chat model" not in err.value.detail.lower()
        assert err.value.untrusted_text is None
    rows = await usage_rows(ctx)
    assert len(rows) == 5 and {r.outcome for r in rows} == {"error"}


async def test_a_blocked_answer_is_a_fixed_reply(ctx: ServerContext, kit: dict[str, Any]) -> None:
    res = await ask(ctx, await member(ctx, kit), kit,
                    model=FakeChatModel([FakeStep(error="blocked")]))  # fmt: skip
    assert res.reply.text == BLOCKED_REPLY and res.tool_calls == []
    rows = await usage_rows(ctx)  # one row per turn, even a refused one
    assert [(r.outcome, r.input_tokens + r.output_tokens) for r in rows] == [("refused", 0)]


async def test_max_tokens_marks_the_answer_truncated(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [FakeStep(text="Readout 2 shows", finish_reason="MAX_TOKENS")]
    res = await ask(ctx, await member(ctx, kit), kit, model=FakeChatModel(script))
    assert res.notices == ["truncated"]


async def test_the_turn_deadline_ends_a_slow_turn(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ctx.settings = ctx.settings.model_copy(
        update={"chat_turn_deadline_s": 0.3, "chat_request_timeout_s": 0.3}
    )

    async def slow(n: int) -> None:
        if n == 1:
            await asyncio.sleep(0.5)

    model = FakeChatModel(
        [FakeStep(call=("get_latest_readout", {})), FakeStep(text="late")], on_generate=slow
    )
    with pytest.raises(MapiError) as err:
        await ask(ctx, await member(ctx, kit), kit, model=model)
    assert err.value.code.value == "CHAT_UNAVAILABLE"


async def test_an_empty_answer_becomes_a_fixed_reply(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    script = [FakeStep(text="<script>x</script> https://evil.example/a.png \u200b")]
    res = await ask(ctx, await member(ctx, kit), kit, model=FakeChatModel(script))
    assert res.reply.text == chat_loop.EMPTY_REPLY


async def test_the_dashboard_of_the_credential_must_match(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    p = await member(ctx, kit)
    with pytest.raises(MapiError) as err:
        await run_chat_turn(ctx, p, "dsh_01jaf3k1m2n3p4q5r6s7t8v9w0", request("Hi"),
                            model=FakeChatModel())  # fmt: skip
    assert err.value.code.value == "NOT_FOUND"
    async with ctx.db.session() as s:
        assert (
            await s.execute(select(func.count()).select_from(m.DashboardChatUsage))
        ).scalar_one() == 0
