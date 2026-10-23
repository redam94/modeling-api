"""Kept chat conversations (v0.20, plan D8): opt-in per dashboard, 30-day expiry, names and
outcomes of tool calls only, editors list, read and delete them (audited without text), and
the housekeeping purge."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.chat import transcripts
from mapi_server.services.chat.budget import availability
from mapi_server.services.chat.fake import FakeChatModel, FakeStep
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.services.runs import now
from mapi_spec.audit import AuditAction
from mapi_spec.errors import MapiError
from sqlalchemy import select, update
from test_chat_loop import ask, chat_kit, enable_chat, member, request, share
from test_chat_loop import settings as settings  # the chat settings fixture

pytestmark = pytest.mark.integration


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await chat_kit(ctx, client)


async def _rows(ctx: ServerContext) -> list[m.DashboardChatTranscript]:
    async with ctx.db.session() as s:
        stmt = select(m.DashboardChatTranscript).order_by(
            m.DashboardChatTranscript.conversation_id, m.DashboardChatTranscript.turn_index
        )
        return list((await s.execute(stmt)).scalars())


async def _auth(ctx: ServerContext, kit: dict[str, Any], who: str) -> Any:
    auth = await owner_auth(ctx, "default", "default", kit["users"][who].id)
    assert auth is not None
    return auth


async def test_opt_out_stores_nothing(ctx: ServerContext, kit: dict[str, Any]) -> None:
    res = await ask(ctx, await member(ctx, kit), kit)
    assert res.transcript_kept is False and "transcript_kept" not in res.notices
    assert await _rows(ctx) == []


async def test_kept_turns_hold_text_and_tool_names_only(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    p = await member(ctx, kit)
    got = await availability(ctx, await p.recheck(), p)
    assert got.keeps_transcripts is True and got.transcript_days == 30
    script = [
        FakeStep(call=("get_latest_readout", {})),
        FakeStep(call=("get_readout", {"seq": 99})),
        FakeStep(text="Readout 2 shows 1,234,567."),
    ]
    first = await ask(ctx, p, kit, "What changed?", model=FakeChatModel(script))
    assert first.transcript_kept is True and "transcript_kept" in first.notices
    rows = await _rows(ctx)
    assert [(r.turn_index, r.role, r.text) for r in rows] == [
        (0, "user", "What changed?"),
        (1, "assistant", "Readout 2 shows 1,234,567."),
    ]
    assert rows[1].tool_calls == [
        {"name": "get_latest_readout", "ok": True},
        {"name": "get_readout", "ok": False},
    ]
    assert rows[0].tool_calls == []
    for row in rows:
        assert row.principal_kind == "member" and row.principal_id == kit["users"]["eve"].id
        assert row.conversation_id == first.conversation_id
        ttl = row.expires_at.astimezone(UTC) - row.created_at.astimezone(UTC)
        assert timedelta(days=30) - timedelta(seconds=5) <= ttl <= timedelta(days=30)
        assert "snapshot" not in row.text and "1234567.0" not in row.text  # no tool payloads
    # the next turn adds its two turns
    req = request("What changed?", first.reply.text, "And the tiles?",
                  sigs=[first.reply.sig], conversation_id=first.conversation_id)  # fmt: skip
    await ask(ctx, await member(ctx, kit), kit, req=req)
    assert [r.turn_index for r in await _rows(ctx)] == [0, 1, 2, 3]


async def test_turning_keep_off_stores_nothing_new(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    await ask(ctx, await member(ctx, kit), kit)
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=False)
    await ask(ctx, await member(ctx, kit), kit)
    assert len(await _rows(ctx)) == 2


async def test_another_caller_cannot_overwrite_a_kept_turn(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    eve = await ask(ctx, await member(ctx, kit, "eve"), kit)
    dctx = await (await member(ctx, kit, "cleo")).recheck()
    written = await transcripts.append(
        ctx, dctx, await member(ctx, kit, "cleo"), eve.conversation_id,
        [transcripts.TranscriptTurn(index=0, role="user", text="overwritten")],
    )  # fmt: skip
    assert written == 0
    assert (await _rows(ctx))[0].text == "What does the latest readout show?"


async def test_editors_list_read_and_delete(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    eve_turn = await ask(ctx, await member(ctx, kit, "eve"), kit)
    share_turn = await ask(ctx, share(ctx, kit), kit)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardShare)
            .where(m.DashboardShare.id == kit["share_id"])
            .values(label="Acme board")
        )
    ana = await _auth(ctx, kit, "ana")
    page = await transcripts.list_page(ctx, ana, kit["dash_id"])
    assert {i.conversation_id for i in page.items} == {
        eve_turn.conversation_id, share_turn.conversation_id
    }  # fmt: skip
    labels = {i.principal_kind: i.principal_label for i in page.items}
    assert labels["share"] == "Share link Acme board"
    eve = kit["users"]["eve"]
    assert labels["member"] == (eve.name or eve.email)  # a display label, never an IP address
    assert all(i.turns == 2 for i in page.items) and page.next_cursor is None
    # paging, latest first
    first = await transcripts.list_page(ctx, ana, kit["dash_id"], limit=1)
    assert first.items[0].conversation_id == share_turn.conversation_id
    assert first.next_cursor is not None
    second = await transcripts.list_page(ctx, ana, kit["dash_id"], cursor=first.next_cursor,
                                         limit=1)  # fmt: skip
    assert [i.conversation_id for i in second.items] == [eve_turn.conversation_id]
    assert second.next_cursor is None
    with pytest.raises(MapiError) as err:
        await transcripts.list_page(ctx, ana, kit["dash_id"], cursor="not-a-cursor!")
    assert err.value.code.value == "SCHEMA_INVALID"

    got = await transcripts.get(ctx, ana, kit["dash_id"], eve_turn.conversation_id)
    assert [(t.index, t.role) for t in got.turns] == [(0, "user"), (1, "assistant")]
    assert got.turns[1].tool_calls == ["get_latest_readout"] and got.turns[1].ok

    await transcripts.delete(ctx, ana, kit["dash_id"], eve_turn.conversation_id)
    with pytest.raises(MapiError) as err:
        await transcripts.get(ctx, ana, kit["dash_id"], eve_turn.conversation_id)
    assert err.value.code.value == "NOT_FOUND"
    with pytest.raises(MapiError) as err:
        await transcripts.delete(ctx, ana, kit["dash_id"], eve_turn.conversation_id)
    assert err.value.code.value == "NOT_FOUND"
    async with ctx.db.session() as s:
        audits = list(
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == AuditAction.DASHBOARD_CHAT_TRANSCRIPT_DELETE.value
                    )
                )
            ).scalars()
        )
    assert len(audits) == 1 and audits[0].resource_id == kit["dash_id"]
    assert audits[0].details == {"conversation_id": eve_turn.conversation_id, "turns": 2}


async def test_viewers_and_clients_cannot_read_transcripts(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    res = await ask(ctx, await member(ctx, kit, "cleo"), kit)
    for who in ("vic", "cleo"):
        auth = await _auth(ctx, kit, who)
        with pytest.raises(MapiError) as err:
            await transcripts.list_page(ctx, auth, kit["dash_id"])
        assert err.value.code.value == "FORBIDDEN", who
        with pytest.raises(MapiError):
            await transcripts.get(ctx, auth, kit["dash_id"], res.conversation_id)
        with pytest.raises(MapiError):
            await transcripts.delete(ctx, auth, kit["dash_id"], res.conversation_id)
    assert len(await _rows(ctx)) == 2


async def test_transcript_ttl_purge(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    old = await ask(ctx, await member(ctx, kit), kit)
    fresh = await ask(ctx, await member(ctx, kit, "cleo"), kit)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardChatTranscript)
            .where(m.DashboardChatTranscript.conversation_id == old.conversation_id)
            .values(expires_at=now() - timedelta(seconds=1))
        )
    ana = await _auth(ctx, kit, "ana")
    page = await transcripts.list_page(ctx, ana, kit["dash_id"])
    assert [i.conversation_id for i in page.items] == [fresh.conversation_id]  # hidden at once
    assert await transcripts.purge_expired(ctx) == 2
    assert {r.conversation_id for r in await _rows(ctx)} == {fresh.conversation_id}
    assert await transcripts.purge_expired(ctx, now() + timedelta(days=31)) == 2
    assert await _rows(ctx) == []


async def test_transcripts_stay_in_their_tenant(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    await ask(ctx, await member(ctx, kit), kit)
    async with ctx.db.tenant_session("another-tenant") as s:
        assert list((await s.execute(select(m.DashboardChatTranscript))).scalars()) == []
    async with ctx.db.tenant_session("default") as s:
        assert len(list((await s.execute(select(m.DashboardChatTranscript))).scalars())) == 2
