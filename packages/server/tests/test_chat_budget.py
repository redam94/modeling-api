"""Chat availability, budgets, limits, the kill switch and the residency gate (v0.20, plan D4,
D7 and D20)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.chat import budget
from mapi_server.services.chat.budget import ChatDisabled, availability, estimate_micros
from mapi_server.services.chat.fake import FAKE_MODEL_ID, FakeChatModel, FakeStep
from mapi_server.services.chat.model import ModelUsage
from mapi_server.services.dashboards.access import DashboardContext
from mapi_server.services.quotas import KILL_SWITCHES_KEY, charge_daily, peek_daily
from mapi_server.services.runs import now
from mapi_spec.admin import KillSwitches
from mapi_spec.chat import CHAT_SUGGESTIONS
from mapi_spec.errors import MapiError
from sqlalchemy import select, update
from test_chat_loop import ask, chat_kit, enable_chat, member, share, usage_rows
from test_chat_loop import settings as settings  # the chat settings fixture
from test_dashboards_crud import create_published

pytestmark = pytest.mark.integration


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await chat_kit(ctx, client)


async def _dctx(ctx: ServerContext, principal: Any) -> DashboardContext:
    dctx: DashboardContext = await principal.recheck()
    return dctx


async def _reason(ctx: ServerContext, principal: Any) -> str | None:
    return (await availability(ctx, await _dctx(ctx, principal), principal)).reason


async def _kill(ctx: ServerContext, chat_enabled: bool) -> None:
    async with ctx.db.session() as s:
        row = await s.get(m.PlatformSetting, KILL_SWITCHES_KEY)
        value = KillSwitches(chat_enabled=chat_enabled).model_dump(mode="json")
        if row is None:
            s.add(m.PlatformSetting(key=KILL_SWITCHES_KEY, value=value, updated_at=now()))
        else:
            row.value = value


# ============================================================================= availability


async def test_available_with_the_fixed_suggestions(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    p = await member(ctx, kit)
    got = await availability(ctx, await _dctx(ctx, p), p)
    assert got.enabled_for_me and got.reason is None
    assert got.suggestions == list(CHAT_SUGGESTIONS)
    assert got.model == FAKE_MODEL_ID and got.keeps_transcripts is False
    assert got.transcript_days == ctx.settings.chat_transcript_ttl_days


async def test_chat_is_off_by_default(ctx: ServerContext, kit: dict[str, Any]) -> None:
    async with ctx.db.session() as s:
        await s.execute(update(m.DashboardChatSettings).values(enabled=False))
    p = await member(ctx, kit)
    got = await availability(ctx, await _dctx(ctx, p), p)
    assert (got.enabled_for_me, got.reason, got.suggestions, got.model) == (
        False, "off", [], None,
    )  # fmt: skip
    with pytest.raises(ChatDisabled) as err:
        await ask(ctx, p, kit)
    assert err.value.code.value == "CHAT_DISABLED" and err.value.reason == "off"
    assert await usage_rows(ctx) == []


async def test_no_row_means_off(ctx: ServerContext, kit: dict[str, Any]) -> None:
    async with ctx.db.session() as s:
        await s.execute(m.DashboardChatSettings.__table__.delete())
    assert await _reason(ctx, await member(ctx, kit)) == "off"


async def test_audiences(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], aud_members=False, aud_clients=True, aud_share=False)
    assert await _reason(ctx, await member(ctx, kit, "eve")) == "audience"
    assert await _reason(ctx, await member(ctx, kit, "cleo")) is None
    assert await _reason(ctx, share(ctx, kit)) == "audience"
    with pytest.raises(ChatDisabled) as err:
        await ask(ctx, share(ctx, kit), kit)
    assert err.value.reason == "audience"
    await enable_chat(ctx, kit["dash_id"], aud_members=True, aud_clients=False, aud_share=True)
    assert await _reason(ctx, await member(ctx, kit, "cleo")) == "audience"
    assert await _reason(ctx, share(ctx, kit)) is None


async def test_provider_off(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ctx.settings = ctx.settings.model_copy(update={"chat_provider": "off"})
    assert await _reason(ctx, await member(ctx, kit)) == "provider"


async def test_killswitch_off(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await _kill(ctx, False)
    p = await member(ctx, kit)
    assert await _reason(ctx, p) == "killswitch"
    with pytest.raises(ChatDisabled) as err:
        await ask(ctx, p, kit)
    assert err.value.reason == "killswitch"
    await _kill(ctx, True)
    assert await _reason(ctx, p) is None


async def test_killswitch_mid_turn_stops_the_next_model_call(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    async def kill(n: int) -> None:
        if n == 0:
            await _kill(ctx, False)

    model = FakeChatModel(
        [FakeStep(call=("get_latest_readout", {})), FakeStep(text="never")], on_generate=kill
    )
    with pytest.raises(ChatDisabled):
        await ask(ctx, await member(ctx, kit), kit, model=model)
    assert len(model.calls) == 1
    assert [r.outcome for r in await usage_rows(ctx)] == ["refused"]


async def test_eu_residency_chat_refused(ctx: ServerContext, kit: dict[str, Any]) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Tenant).where(m.Tenant.id == "default").values(data_residency="eu")
        )
    p = await member(ctx, kit)
    assert await _reason(ctx, p) == "region"
    with pytest.raises(ChatDisabled) as err:
        await ask(ctx, p, kit)
    assert err.value.reason == "region"
    ctx.settings = ctx.settings.model_copy(update={"chat_allowed_residencies": ["us", "eu"]})
    assert await _reason(ctx, p) is None


async def test_a_layout_1_dashboard_has_no_chat(ctx: ServerContext, kit: dict[str, Any]) -> None:
    v1_id = await create_published(kit, "chat-v1")
    await enable_chat(ctx, v1_id)
    kit = {**kit, "dash_id": v1_id}
    p = await member(ctx, kit)
    assert await _reason(ctx, p) == "layout"


# ================================================================================== budgets


async def test_budget_exhausted_429(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], tokens_per_day=200)
    p = await member(ctx, kit)
    script = [FakeStep(text="Readout 2 is the latest readout.", usage=300)]
    first = await ask(ctx, p, kit, model=FakeChatModel(script))
    assert "budget_low" in first.notices
    assert await _reason(ctx, p) == "budget"
    with pytest.raises(MapiError) as err:
        await ask(ctx, await member(ctx, kit), kit, model=FakeChatModel(script))
    assert err.value.code.value == "CHAT_BUDGET_EXHAUSTED" and err.value.status == 429
    retry = int(getattr(err.value, "headers", {})["Retry-After"])
    at = datetime.now(UTC)
    to_midnight = 86400 - (at.hour * 3600 + at.minute * 60 + at.second)
    assert 1 <= retry <= to_midnight + 1


async def test_the_budget_is_checked_before_every_model_call(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], tokens_per_day=250)
    script = [FakeStep(call=("get_latest_readout", {}), usage=150) for _ in range(3)]
    model = FakeChatModel([*script, FakeStep(text="x")])
    with pytest.raises(MapiError) as err:
        await ask(ctx, await member(ctx, kit), kit, model=model)
    assert err.value.code.value == "CHAT_BUDGET_EXHAUSTED"
    assert len(model.calls) == 2  # 150, then 300 >= 250: the third call never happens
    rows = await usage_rows(ctx)
    assert [(r.outcome, r.tool_calls) for r in rows] == [("budget", 2)]


async def test_a_share_link_has_its_own_budget(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], share_tokens_per_day=100)
    script = [FakeStep(text="Readout 2 is the latest readout.", usage=120)]
    await ask(ctx, share(ctx, kit), kit, model=FakeChatModel(script))
    assert await _reason(ctx, share(ctx, kit)) == "budget"
    assert await _reason(ctx, await member(ctx, kit)) is None  # the dashboard budget is fine
    rows = await usage_rows(ctx)
    assert rows[0].principal_kind == "share" and rows[0].share_id == kit["share_id"]
    async with ctx.db.session() as s:
        assert await peek_daily(s, f"chattokshare:{kit['share_id']}", now()) == 120
        assert await peek_daily(s, f"chattok:{kit['dash_id']}", now()) == 120


async def test_daily_counters_reset_by_day(ctx: ServerContext) -> None:
    day1 = datetime(2026, 10, 22, 23, 59, tzinfo=UTC)
    day2 = datetime(2026, 10, 23, 0, 1, tzinfo=UTC)
    async with ctx.db.session() as s:
        assert await charge_daily(s, "chattok:dsh_x", 100, day1) == 100
        assert await charge_daily(s, "chattok:dsh_x", 50, day1) == 150
        assert await peek_daily(s, "chattok:dsh_x", day1) == 150
        assert await peek_daily(s, "chattok:dsh_x", day2) == 0
        assert await charge_daily(s, "chattok:dsh_x", 7, day2) == 7
        assert await charge_daily(s, "chattok:dsh_x", 0, day2) == 7
        rows = (
            await s.execute(
                select(m.RateCounter.window_start).where(m.RateCounter.subject == "chattok:dsh_x")
            )
        ).scalars()
        assert [r.astimezone(UTC).date().isoformat() for r in rows] == [
            "2026-10-23"
        ]  # old days are deleted


def test_estimate_micros_uses_the_pinned_prices(settings: Any) -> None:
    s = settings.model_copy(
        update={
            "chat_price_in_micros_per_mtok": 250_000,
            "chat_price_out_micros_per_mtok": 1_500_000,
        }
    )
    assert estimate_micros(ModelUsage(1_000_000, 0, 0, 0, 1_000_000), s) == 250_000
    assert estimate_micros(ModelUsage(0, 1_000, 1_000, 0, 2_000), s) == 3_000
    assert estimate_micros(ModelUsage(1, 0, 0, 0, 1), s) == 1  # rounded up
    # a total above the parts is charged as input (tool-use prompt tokens)
    assert estimate_micros(ModelUsage(0, 0, 0, 0, 4_000), s) == 1_000


# ============================================================================== turn limits


async def test_member_turns_per_minute(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ctx.settings = ctx.settings.model_copy(update={"chat_member_turns_per_minute": 2})
    for _ in range(2):
        await ask(ctx, await member(ctx, kit), kit)
    with pytest.raises(MapiError) as err:
        await ask(ctx, await member(ctx, kit), kit)
    assert err.value.code.value == "RATE_LIMITED"
    assert "Retry-After" in getattr(err.value, "headers", {})
    await ask(ctx, await member(ctx, kit, "cleo"), kit)  # another caller has their own


async def test_share_ip_flood(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ctx.settings = ctx.settings.model_copy(
        update={"chat_share_ip_turns_per_minute": 2, "chat_share_turns_per_minute": 6}
    )
    for _ in range(2):
        await ask(ctx, share(ctx, kit), kit)
    with pytest.raises(MapiError) as err:
        await ask(ctx, share(ctx, kit), kit)
    assert err.value.code.value == "RATE_LIMITED"
    assert len(await usage_rows(ctx)) == 2  # the refused turn never reached the model


async def test_subjects_fit_the_counter_column(ctx: ServerContext, kit: dict[str, Any]) -> None:
    p = await member(ctx, kit)
    dctx = await _dctx(ctx, p)
    p.id = "u" * 90
    subj = budget.subjects(dctx, p)
    assert len(subj.dashboard_tokens) <= 96 and len(subj.turns) <= 96
    assert subj.turns.startswith("chat:") and subj.share_tokens is None
