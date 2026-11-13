"""Dashboard chat data over its lifetime (v0.20, plan D7/D8, WP9): the housekeeping loop
purges expired transcript turns and keeps running chat budgets, the GDPR export holds the
chat rows of the workspace's own people (not share-link turns), and a tenant purge removes
transcripts, usage rows, read tokens and the chat and MCP rate counters."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.deletion import TenantPurger, tenant_counter_subjects
from mapi_server.services.gdpr_export import ExportBuilder, export_object_name
from mapi_server.services.housekeeping import TRANSCRIPT_PURGE_EVERY, Housekeeper
from mapi_server.services.quotas import charge_daily, egress_subject, window_starts
from mapi_server.services.runs import now
from mapi_spec.api import new_id
from sqlalchemy import func, select, update
from test_chat_loop import ask, chat_kit, enable_chat, member, share
from test_chat_loop import settings as settings  # the chat settings fixture

pytestmark = pytest.mark.integration


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await chat_kit(ctx, client)


async def _transcripts(ctx: ServerContext) -> list[m.DashboardChatTranscript]:
    async with ctx.db.session() as s:
        stmt = select(m.DashboardChatTranscript).order_by(
            m.DashboardChatTranscript.conversation_id, m.DashboardChatTranscript.turn_index
        )
        return list((await s.execute(stmt)).scalars())


async def _counters(ctx: ServerContext) -> dict[str, int]:
    async with ctx.db.session() as s:
        rows = (await s.execute(select(m.RateCounter.subject, m.RateCounter.count))).all()
    return {str(subject): int(count) for subject, count in rows}


async def _count(ctx: ServerContext, model: Any, tenant_id: str) -> int:
    async with ctx.db.session() as s:
        stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        return int((await s.execute(stmt)).scalar_one())


# ================================================================================ housekeeping


async def test_transcript_ttl_purge(ctx: ServerContext, kit: dict[str, Any]) -> None:
    """Expired turns go on the next housekeeping pass; fresh ones stay. The purge runs at
    most once an hour."""
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    old = await ask(ctx, await member(ctx, kit, "eve"), kit)
    fresh = await ask(ctx, await member(ctx, kit, "cleo"), kit)
    assert len(await _transcripts(ctx)) == 4
    at = now()
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardChatTranscript)
            .where(m.DashboardChatTranscript.conversation_id == old.conversation_id)
            .values(expires_at=at - timedelta(minutes=1))
        )
    keeper = Housekeeper(ctx)
    assert await keeper.sweep_once(now=at) >= 2
    rows = await _transcripts(ctx)
    assert {r.conversation_id for r in rows} == {fresh.conversation_id}
    assert len(rows) == 2

    # the fresh conversation expires, but the purge already ran this hour
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardChatTranscript).values(expires_at=at + timedelta(minutes=5))
        )
    await keeper.sweep_once(now=at + timedelta(minutes=10))
    assert len(await _transcripts(ctx)) == 2
    await keeper.sweep_once(now=at + TRANSCRIPT_PURGE_EVERY + timedelta(minutes=1))
    assert await _transcripts(ctx) == []
    # a new housekeeper (a restarted orchestrator) purges on its first pass
    assert await Housekeeper(ctx).purge_transcripts(now=at) == 0


async def test_running_chat_budgets_survive_the_counter_sweep(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """The sweep deletes per-minute counters once their minute is over, but a day's chat
    token budget only once the UTC day is over (and the month's egress once the month is
    over): a sweep must never reset a running budget."""
    at = now()
    day, month = window_starts(at)
    dash, share_id = kit["dash_id"], kit["share_id"]
    async with ctx.db.session() as s:
        await charge_daily(s, f"chattok:{dash}", 500, at)
        await charge_daily(s, f"chattokshare:{share_id}", 70, at)
        s.add(m.RateCounter(subject=f"chat:{dash}:x", window_start=at - timedelta(minutes=5),
                            count=3))  # fmt: skip
        s.add(m.RateCounter(subject=egress_subject("default"), window_start=month, count=9))
        s.add(m.RateCounter(subject="egress:old", window_start=month - timedelta(days=40),
                            count=9))  # fmt: skip
        s.add(m.RateCounter(subject=f"chattok:{dash}-old", window_start=day - timedelta(days=1),
                            count=9))  # fmt: skip
    later = at + timedelta(minutes=30)
    if window_starts(later)[0] != day:  # the test runs just before midnight UTC
        later = at + timedelta(seconds=150)
    await Housekeeper(ctx).sweep_once(now=later)
    counters = await _counters(ctx)
    assert counters[f"chattok:{dash}"] == 500
    assert counters[f"chattokshare:{share_id}"] == 70
    assert counters[egress_subject("default")] == 9
    assert f"chat:{dash}:x" not in counters  # its minute is over
    assert "egress:old" not in counters and f"chattok:{dash}-old" not in counters
    # the next UTC day the budgets start again from zero
    await Housekeeper(ctx).sweep_once(now=day + timedelta(days=1, minutes=1))
    counters = await _counters(ctx)
    assert f"chattok:{dash}" not in counters and f"chattokshare:{share_id}" not in counters


# ===================================================================================== export


async def test_transcript_in_gdpr_export(ctx: ServerContext, kit: dict[str, Any]) -> None:
    """The tenant export holds the kept turns of members and client users, the chat
    settings and the usage rows; turns typed through a share link are left out."""
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    eve = await ask(ctx, await member(ctx, kit, "eve"), kit, "What changed for eve?")
    cleo = await ask(ctx, await member(ctx, kit, "cleo"), kit, "What changed for cleo?")
    shared = await ask(ctx, share(ctx, kit), kit, "What changed for the link?")
    async with ctx.db.session() as s:
        export = m.TenantExport(
            id=new_id("exp"), tenant_id="default", state="queued",
            include=["metadata", "audit", "usage"], requested_by=kit["users"]["ana"].id,
            created_at=now(),
        )  # fmt: skip
        s.add(export)
    assert await ExportBuilder(ctx).run(export.id) == "ready"
    data = await ctx.store.get(ctx.buckets.data, export_object_name("default", export.id))
    zf = zipfile.ZipFile(io.BytesIO(data))

    def rows(name: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in zf.read(name).decode().splitlines() if line]

    turns = rows("metadata/dashboard_chat_transcripts.jsonl")
    assert {t["conversation_id"] for t in turns} == {eve.conversation_id, cleo.conversation_id}
    assert "share" not in {t["principal_kind"] for t in turns}
    assert {t["principal_id"] for t in turns} == {
        kit["users"]["eve"].id, kit["users"]["cleo"].id
    }  # fmt: skip
    texts = {t["text"] for t in turns if t["role"] == "user"}
    assert texts == {"What changed for eve?", "What changed for cleo?"}
    blob = b"".join(zf.read(n) for n in zf.namelist())
    assert b"What changed for the link?" not in blob
    assert shared.conversation_id.encode() not in blob
    # the share-link turns are still kept in the app (only the export leaves them out)
    assert {r.conversation_id for r in await _transcripts(ctx)} >= {shared.conversation_id}
    usage = rows("metadata/dashboard_chat_usage.jsonl")
    assert len(usage) == 3 and all(u["tenant_id"] == "default" for u in usage)
    assert {"share"} <= {u["principal_kind"] for u in usage}  # counts only, never text
    [conf] = rows("metadata/dashboard_chat_settings.jsonl")
    assert conf["dashboard_id"] == kit["dash_id"] and conf["keep_transcripts"] is True
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["rows"]["metadata/dashboard_chat_transcripts.jsonl"] == 4
    assert b"dashboard_chat" in zf.read("README.txt")


# =================================================================================== deletion


async def test_tenant_purge_removes_chat_rows_read_tokens_and_counters(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """The database store of a tenant purge deletes transcripts, usage rows, chat settings
    and dashboard read tokens (catalog-discovered tables), and the platform rate counters
    of the tenant's dashboards, share links and tokens."""
    dash, share_id = kit["dash_id"], kit["share_id"]
    await enable_chat(ctx, dash, keep_transcripts=True)
    await ask(ctx, await member(ctx, kit, "cleo"), kit)
    await ask(ctx, share(ctx, kit), kit)
    r = await kit["cleo"].post(f"/v1/dashboards/{dash}/read-tokens", json={"name": "Agent"})
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]
    at = now()
    async with ctx.db.session() as s:
        for subject in (f"mcp:{token_id}", f"mcpshare:{share_id}", f"chatshare:{share_id}",
                        f"chat:{dash}:{kit['users']['cleo'].id}"):  # fmt: skip
            s.add(m.RateCounter(subject=subject, window_start=at, count=1))
        s.add(m.RateCounter(subject="chat:dsh_other:usr_x", window_start=at, count=1))
    before = await _counters(ctx)
    assert {f"chattok:{dash}", f"chattokshare:{share_id}"} <= before.keys()
    for model in (m.DashboardChatTranscript, m.DashboardChatUsage, m.DashboardChatSettings):
        assert await _count(ctx, model, "default") > 0, model
    async with ctx.db.session() as s:
        row = await s.get(m.ApiToken, token_id)
        assert row is not None and row.dashboard_id == dash
        deletion = m.TenantDeletion(
            id=new_id("del"), tenant_id="default", state="deleting", previous_status="active",
            requested_at=at, purge_after=at, started_at=at, stores=[],
        )  # fmt: skip
        s.add(deletion)

    assert await TenantPurger(ctx)._purge_database(deletion.id, "default", at)

    for model in (m.DashboardChatTranscript, m.DashboardChatUsage, m.DashboardChatSettings,
                  m.ApiToken, m.Dashboard, m.DashboardShare):  # fmt: skip
        assert await _count(ctx, model, "default") == 0, model
    counters = await _counters(ctx)
    gone = {
        f"chattok:{dash}", f"chattokshare:{share_id}", f"mcp:{token_id}", f"mcpshare:{share_id}",
        f"chatshare:{share_id}", f"chat:{dash}:{kit['users']['cleo'].id}",
    }  # fmt: skip
    assert not gone & counters.keys()
    assert "chat:dsh_other:usr_x" in counters  # another dashboard's counter is not touched


def test_tenant_counter_subjects() -> None:
    exact, prefixes = tenant_counter_subjects(
        "acme", token_ids=["tok_1"], dashboard_ids=["dsh_a_1"], share_ids=["shr_1"]
    )
    assert set(exact) == {
        "tenant:acme", "egress:acme", "token:tok_1", "mcp:tok_1", "chattok:dsh_a_1",
        "chattokshare:shr_1", "chatshare:shr_1", "mcpshare:shr_1",
    }  # fmt: skip
    assert prefixes == ["chat:dsh\\_a\\_1:"]  # LIKE wildcards in ids are escaped
