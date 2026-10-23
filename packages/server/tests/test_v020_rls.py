"""v0.20 tables (0024_dashboard_chat): row-level security and least-privilege grants on chat
settings, kept transcripts and usage rows; the checks that back the contracts (roles,
principal kinds, text length, budgets, tool calls, outcomes); ``api_tokens.dashboard_id``
(a read token goes with its dashboard); and a downgrade that refuses while chat data or a
dashboard read token exists."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, get_args
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic.script import ScriptDirectory
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.db.migrate import alembic_config, downgrade, upgrade
from mapi_server.services.runs import now
from mapi_server_testkit.identity import make_tenant
from mapi_spec import chat as spec
from mapi_spec.api import new_id
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.integration

V020_TABLES: tuple[type[m.Base], ...] = (
    m.DashboardChatSettings,
    m.DashboardChatTranscript,
    m.DashboardChatUsage,
)
TABLE_NAMES = tuple(t.__tablename__ for t in V020_TABLES)


def _rows(tenant: str) -> dict[str, Any]:
    """A published dashboard of ``tenant``'s ``main`` project with chat on, one kept
    exchange (user and assistant turn), one usage row per principal kind and two tokens: an
    ordinary one and a read token for the dashboard."""
    ts = now()
    scope = {"tenant_id": tenant, "project_id": "main"}
    dsh, cnv, share = new_id("dsh"), spec.new_conversation_id(), new_id("dss")
    expires = ts + timedelta(days=spec.CHAT_TRANSCRIPT_DAYS)

    def token(dashboard_id: str | None) -> m.ApiToken:
        return m.ApiToken(
            id=new_id("tok"),
            owner_id=f"usr_{tenant}_client",
            owner_kind="user",
            name="mcp",
            prefix=secrets.token_hex(4),
            secret_hash="h" * 64,
            scopes=[f"dashboard:{dashboard_id}"] if dashboard_id else ["models:read"],
            created_at=ts,
            expires_at=ts + timedelta(days=30),
            dashboard_id=dashboard_id,
            **scope,
        )

    return {
        "dashboard": m.Dashboard(
            id=dsh, name=f"{tenant}-weekly", status="published", draft_spec={"title": "W"},
            draft_etag='"1"', created_by="usr_ana", **scope,
        ),
        "settings": m.DashboardChatSettings(
            dashboard_id=dsh, enabled=True, keep_transcripts=True, updated_by="usr_ana", **scope
        ),
        "user_turn": m.DashboardChatTranscript(
            id=new_id("ctt"), dashboard_id=dsh, conversation_id=cnv, principal_kind="client",
            principal_id=f"usr_{tenant}_client", turn_index=0, role="user",
            text="What changed since the last readout?", expires_at=expires, **scope,
        ),
        "assistant_turn": m.DashboardChatTranscript(
            id=new_id("ctt"), dashboard_id=dsh, conversation_id=cnv, principal_kind="client",
            principal_id=f"usr_{tenant}_client", turn_index=1, role="assistant",
            text="Sales rose by 4.2%.",
            tool_calls=[{"name": "get_latest_readout", "ok": True}], expires_at=expires,
            **scope,
        ),
        "member_usage": m.DashboardChatUsage(
            dashboard_id=dsh, principal_kind="member", principal_id="usr_ana",
            model="fake-chat", input_tokens=900, output_tokens=120, thought_tokens=0,
            tool_calls=2, est_micros=405, price_version="gemini-3.1-flash-lite@2026-10-22",
            outcome="ok", **scope,
        ),
        "share_usage": m.DashboardChatUsage(
            dashboard_id=dsh, principal_kind="share", principal_id=share, share_id=share,
            model="fake-chat", price_version="gemini-3.1-flash-lite@2026-10-22",
            outcome="budget", **scope,
        ),
        "token": token(None),
        "read_token": token(dsh),
    }  # fmt: skip


ORDER = (
    "dashboard",
    "settings",
    "user_turn",
    "assistant_turn",
    "member_usage",
    "share_usage",
    "token",
    "read_token",
)


async def _seed(ctx: ServerContext) -> dict[str, dict[str, Any]]:
    """Both tenants' rows; returns each tenant's ids."""
    ids: dict[str, dict[str, Any]] = {}
    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant, tier="internal", projects=("main",))
        rows = _rows(tenant)
        async with ctx.db.tenant_session(tenant) as s:
            for key in ORDER:
                s.add(rows[key])
                await s.flush()  # parents before children
        ids[tenant] = {
            "dashboard": rows["dashboard"].id,
            "conversation": rows["user_turn"].conversation_id,
            "turn": rows["user_turn"].id,
            "usage": rows["member_usage"].id,
            "token": rows["token"].id,
            "read_token": rows["read_token"].id,
        }
    return ids


def test_database_enums_match_the_contracts() -> None:
    """The checks in the database name exactly the contract's roles and principal kinds,
    and its caps agree with the contract's."""
    assert get_args(spec.ChatRole) == m.CHAT_ROLES
    kind = spec.ChatTranscriptSummary.model_fields["principal_kind"].annotation
    assert get_args(kind) == m.CHAT_PRINCIPAL_KINDS
    assert spec.CHAT_MAX_REPLY_CHARS == m.CHAT_TEXT_MAX_CHARS
    assert spec.CHAT_MAX_USER_CHARS <= m.CHAT_TEXT_MAX_CHARS
    assert spec.CHAT_MAX_TOOL_CALLS == m.CHAT_MAX_TOOL_CALLS
    assert spec.CHAT_MAX_TOKENS_PER_DAY == m.CHAT_MAX_TOKENS_PER_DAY
    assert spec.CHAT_MAX_SHARE_TOKENS_PER_DAY == m.CHAT_MAX_SHARE_TOKENS_PER_DAY
    assert spec.CONVERSATION_ID_RE == m.CONVERSATION_ID_RE
    index = spec.ChatTranscriptTurn.model_fields["index"]
    assert any(getattr(c, "le", None) == m.CHAT_MAX_TURN_INDEX for c in index.metadata)
    settings = m.DashboardChatSettings.__table__.c
    assert settings.tokens_per_day.default.arg == spec.CHAT_DEFAULT_TOKENS_PER_DAY
    assert settings.share_tokens_per_day.default.arg == spec.CHAT_DEFAULT_SHARE_TOKENS_PER_DAY
    audiences = spec.ChatAudiences()
    assert (audiences.members, audiences.clients, audiences.share_links) == (
        settings.aud_members.default.arg,
        settings.aud_clients.default.arg,
        settings.aud_share.default.arg,
    )
    for _ in range(20):
        assert re.fullmatch(m.CONVERSATION_ID_RE.strip("^$"), spec.new_conversation_id())
        assert len(new_id("ctt")) == 30
    assert set(TABLE_NAMES) <= set(m.TENANT_TABLES)


async def test_v020_tables_force_rls(ctx: ServerContext) -> None:
    """Every new table has forced row-level security with the tenant and platform policies,
    full CRUD for the request path and nothing for the gateway: a tenant session sees only
    its own rows (no tenant filter in the query) and cannot reach, change or add the other
    tenant's."""
    ids = await _seed(ctx)
    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        for table in TABLE_NAMES:
            rls = await (
                await conn.execute(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s",
                    (table,),
                )
            ).fetchone()
            assert rls == (True,), table
            policies = {
                r[0]
                for r in await (
                    await conn.execute(
                        "SELECT policyname FROM pg_policies WHERE tablename = %s", (table,)
                    )
                ).fetchall()
            }
            assert policies == {"mapi_tenant_isolation", "mapi_platform_access"}, table
            grants = await (
                await conn.execute(
                    "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = %s",
                    (table,),
                )
            ).fetchall()
            assert {p for g, p in grants if g == "mapi_app"} == {
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
            }, table
            assert {"SELECT", "DELETE"} <= {p for g, p in grants if g == "mapi_system"}, table
            assert not {p for g, p in grants if g == "mapi_gateway"}, table
    for tenant, other in (("alpha", "beta"), ("beta", "alpha")):
        theirs = ids[other]
        async with ctx.db.tenant_session(tenant) as s:
            for model in V020_TABLES:
                found = set((await s.execute(select(model.tenant_id))).scalars().all())  # type: ignore[attr-defined]
                assert found == {tenant}, model
            assert await s.get(m.DashboardChatSettings, (other, theirs["dashboard"])) is None
            assert await s.get(m.DashboardChatTranscript, theirs["turn"]) is None
            assert await s.get(m.DashboardChatUsage, theirs["usage"]) is None
            for stmt in (
                update(m.DashboardChatSettings)
                .where(m.DashboardChatSettings.dashboard_id == theirs["dashboard"])
                .values(aud_share=True),
                update(m.DashboardChatTranscript)
                .where(m.DashboardChatTranscript.conversation_id == theirs["conversation"])
                .values(text="Mine."),
                update(m.DashboardChatUsage)
                .where(m.DashboardChatUsage.dashboard_id == theirs["dashboard"])
                .values(est_micros=0),
                delete(m.DashboardChatTranscript).where(
                    m.DashboardChatTranscript.conversation_id == theirs["conversation"]
                ),
                delete(m.DashboardChatUsage).where(
                    m.DashboardChatUsage.dashboard_id == theirs["dashboard"]
                ),
            ):
                res = await s.execute(stmt)
                assert res.rowcount == 0  # type: ignore[attr-defined]
    foreign = _rows("beta")
    foreign_dashboard = ids["beta"]["dashboard"]
    for key in ("settings", "user_turn", "member_usage"):
        row = foreign[key]
        row.dashboard_id = foreign_dashboard
        with pytest.raises((ProgrammingError, IntegrityError)):
            async with ctx.db.tenant_session("alpha") as s:
                s.add(row)
                await s.flush()
    async with ctx.db.session() as s:  # the platform role sees both tenants, untouched
        texts = (await s.execute(select(m.DashboardChatTranscript.text))).scalars().all()
        assert sorted(texts) == sorted(
            ["What changed since the last readout?", "Sales rose by 4.2%."] * 2
        )
        shares = (await s.execute(select(m.DashboardChatSettings.aud_share))).scalars().all()
        assert shares == [False, False]


async def test_foreign_rows_are_refused_by_row_level_security(ctx: ServerContext) -> None:
    """A tenant session cannot add a chat row that names another tenant."""
    ids = await _seed(ctx)
    rows = _rows("beta")
    for key in ("settings", "user_turn", "share_usage"):
        row = rows[key]
        row.dashboard_id = ids["beta"]["dashboard"]
        with pytest.raises(ProgrammingError, match="row-level security"):
            async with ctx.db.tenant_session("alpha") as s:
                s.add(row)
                await s.flush()


async def test_v020_checks_back_the_contracts(ctx: ServerContext) -> None:
    """Roles, principal kinds and outcomes are closed; text, turn index, budgets, tool calls
    and token counts are capped; a share principal names its link and nobody else does; one
    conversation turn is stored once."""
    ids = await _seed(ctx)
    checks: tuple[tuple[Any, str], ...] = (
        (update(m.DashboardChatSettings).values(tokens_per_day=-1), "tokens_per_day"),
        (update(m.DashboardChatSettings).values(tokens_per_day=5_000_001), "tokens_per_day"),
        (
            update(m.DashboardChatSettings).values(share_tokens_per_day=1_000_001),
            "share_tokens_per_day",
        ),
        (update(m.DashboardChatSettings).values(share_tokens_per_day=-1), "share_tokens_per_day"),
        (update(m.DashboardChatSettings).values(version=0), "version"),
        (update(m.DashboardChatTranscript).values(role="model"), "role"),
        (update(m.DashboardChatTranscript).values(role="system"), "role"),
        (update(m.DashboardChatTranscript).values(principal_kind="token"), "principal_kind"),
        (update(m.DashboardChatTranscript).values(text="x" * 4001), "text_length"),
        (update(m.DashboardChatTranscript).values(turn_index=-1), "turn_index"),
        (update(m.DashboardChatTranscript).values(turn_index=64), "turn_index"),
        (update(m.DashboardChatTranscript).values(conversation_id="cnv_X"), "conversation_id"),
        (update(m.DashboardChatTranscript).values(tool_calls={"name": "x"}), "tool_calls_array"),
        (
            update(m.DashboardChatTranscript).values(tool_calls=[{"name": "x" * 5000}]),
            "tool_calls_size",
        ),
        (update(m.DashboardChatUsage).values(principal_kind="admin"), "principal_kind"),
        (update(m.DashboardChatUsage).values(outcome="timeout"), "outcome"),
        (update(m.DashboardChatUsage).values(tool_calls=7), "tool_calls"),
        (update(m.DashboardChatUsage).values(tool_calls=-1), "tool_calls"),
        (update(m.DashboardChatUsage).values(input_tokens=-1), "tokens"),
        (update(m.DashboardChatUsage).values(thought_tokens=-1), "tokens"),
        (update(m.DashboardChatUsage).values(est_micros=-1), "est_micros"),
        (
            update(m.DashboardChatUsage)
            .where(m.DashboardChatUsage.principal_kind == "member")
            .values(share_id="dss_x"),
            "share_id",
        ),
        (
            update(m.DashboardChatUsage)
            .where(m.DashboardChatUsage.principal_kind == "share")
            .values(share_id=None),
            "share_id",
        ),
    )
    for stmt, name in checks:
        table = stmt.table.name
        with pytest.raises(IntegrityError, match=f"ck_{table}_{name}"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(stmt)
    async with ctx.db.tenant_session("alpha") as s:  # the allowed shapes
        await s.execute(
            update(m.DashboardChatSettings).values(
                tokens_per_day=5_000_000, share_tokens_per_day=0, version=7
            )
        )
        await s.execute(
            update(m.DashboardChatTranscript)
            .where(m.DashboardChatTranscript.role == "assistant")
            .values(text="é" * 4000, turn_index=63, ok=False)
        )
        await s.execute(update(m.DashboardChatUsage).values(tool_calls=6, outcome="refused"))
    again = _rows("alpha")["user_turn"]
    again.dashboard_id = ids["alpha"]["dashboard"]
    again.conversation_id = ids["alpha"]["conversation"]
    with pytest.raises(IntegrityError, match="uq_dashboard_chat_transcripts_turn"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(again)
            await s.flush()
    twice = _rows("alpha")["settings"]
    twice.dashboard_id = ids["alpha"]["dashboard"]
    with pytest.raises(IntegrityError, match="pk_dashboard_chat_settings"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(twice)
            await s.flush()


async def test_chat_rows_and_read_tokens_go_with_their_dashboard(ctx: ServerContext) -> None:
    """An editor's delete of a transcript works on the request path; deleting a dashboard
    removes its settings, transcripts, usage rows and read tokens (not ordinary tokens);
    everything goes with the tenant."""
    ids = await _seed(ctx)
    alpha = ids["alpha"]
    async with ctx.db.tenant_session("alpha") as s:
        res = await s.execute(
            delete(m.DashboardChatTranscript).where(
                m.DashboardChatTranscript.conversation_id == alpha["conversation"]
            )
        )
        assert res.rowcount == 2  # type: ignore[attr-defined]
        await s.execute(delete(m.Dashboard).where(m.Dashboard.id == alpha["dashboard"]))
    async with ctx.db.session() as s:
        for model in V020_TABLES:
            tenants = (await s.execute(select(model.tenant_id))).scalars().all()  # type: ignore[attr-defined]
            assert set(tenants) == {"beta"}, model
        tokens = set((await s.execute(select(m.ApiToken.id))).scalars().all())
        assert alpha["read_token"] not in tokens
        assert {alpha["token"], ids["beta"]["token"], ids["beta"]["read_token"]} <= tokens
        indexes = {
            str(ix.name): [c.name for c in ix.columns]
            for ix in m.Base.metadata.tables["api_tokens"].indexes
        }
        assert indexes["ix_api_tokens_dashboard"] == ["tenant_id", "dashboard_id", "owner_id"]
    async with ctx.db.session() as s:  # the platform role purges and deletes
        await s.execute(
            delete(m.DashboardChatTranscript).where(m.DashboardChatTranscript.expires_at < now())
        )
        await s.execute(delete(m.Tenant).where(m.Tenant.id == "beta"))
    async with ctx.db.session() as s:
        for model in V020_TABLES:
            assert not (await s.execute(select(model.tenant_id))).scalars().all(), model  # type: ignore[attr-defined]
        dashboard_tokens = (
            await s.execute(select(m.ApiToken.id).where(m.ApiToken.dashboard_id.is_not(None)))
        ).scalars()
        assert not dashboard_tokens.all()


# ------------------------------------------------------------------ migration downgrade


@pytest.fixture
def scratch_db(pg_admin_url: str) -> Iterator[str]:
    name = f"mapi_mig_{secrets.token_hex(4)}"
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(pg_admin_url)
    url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    try:
        yield url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(url: str, table: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {r[0] for r in rows}


def _seed_v019(url: str) -> None:
    """Rows an earlier server made: a tenant with a project, a dashboard, an ordinary token,
    a plugin claim and a browser session with ``auth_time``."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, trust_tier, status) VALUES ('t1', 't1', "
            "'internal', 'active')"
        )
        conn.execute(
            "INSERT INTO projects (tenant_id, id, project_id, name) VALUES ('t1', 'p1', 'p1', 'p1')"
        )
        conn.execute(
            "INSERT INTO dashboards (id, tenant_id, project_id, name, draft_spec, draft_etag, "
            "created_by) VALUES ('dsh_1', 't1', 'p1', 'd', '{}', '\"1\"', 'usr_1')"
        )
        conn.execute(
            "INSERT INTO api_tokens (id, tenant_id, project_id, owner_id, owner_kind, name, "
            "prefix, secret_hash, scopes, budget_period, created_at, expires_at) VALUES "
            "('tok_1', 't1', 'p1', 'usr_1', 'user', 'ci', 'abcd1234', 'h', "
            "'[\"models:read\"]', 'month', now(), now() + interval '30 days')"
        )
        conn.execute(
            "INSERT INTO tenant_plugin_settings (tenant_id, tenant_code) VALUES ('t1', 'k3vq7m')"
        )


def test_downgrade_below_0024_refuses_with_chat_data_or_read_tokens(scratch_db: str) -> None:
    """0024 is the single head after v0.19's 0023. Stepping below it keeps the v0.19 rows and
    refuses, naming the counts, while chat settings, a transcript turn, a usage row or a
    dashboard read token exists; once those are gone it drops the tables and the column, and
    upgrading again restores them."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    assert script.get_heads() == ["0024_dashboard_chat"]
    before = script.get_revision("0024_dashboard_chat").down_revision
    assert before == "0023_session_auth_time"
    upgrade(scratch_db)
    _seed_v019(scratch_db)
    cnv = spec.new_conversation_id()
    seeds = [
        (
            "INSERT INTO dashboard_chat_settings (tenant_id, project_id, dashboard_id, enabled) "
            "VALUES ('t1', 'p1', 'dsh_1', true)",
            "DELETE FROM dashboard_chat_settings",
            "1 chat settings",
        ),
        (
            "INSERT INTO dashboard_chat_transcripts (id, tenant_id, project_id, dashboard_id, "
            "conversation_id, principal_kind, principal_id, turn_index, role, text, expires_at) "
            f"VALUES ('ctt_1', 't1', 'p1', 'dsh_1', '{cnv}', 'member', 'usr_1', 0, 'user', "
            "'Hi', now() + interval '30 days')",
            "DELETE FROM dashboard_chat_transcripts",
            "1 transcript turns",
        ),
        (
            "INSERT INTO dashboard_chat_usage (tenant_id, project_id, dashboard_id, "
            "principal_kind, principal_id, model, price_version, outcome) VALUES ('t1', 'p1', "
            "'dsh_1', 'member', 'usr_1', 'fake-chat', 'v1', 'ok')",
            "DELETE FROM dashboard_chat_usage",
            "1 usage rows",
        ),
        (
            "INSERT INTO api_tokens (id, tenant_id, project_id, owner_id, owner_kind, name, "
            "prefix, secret_hash, scopes, budget_period, created_at, expires_at, dashboard_id) "
            "VALUES ('tok_2', 't1', 'p1', 'usr_1', 'user', 'mcp', 'efgh5678', 'h', "
            "'[\"dashboard:dsh_1\"]', 'month', now(), now() + interval '30 days', 'dsh_1')",
            "DELETE FROM api_tokens WHERE id = 'tok_2'",
            "1 dashboard read tokens",
        ),
    ]
    for insert, remove, message in seeds:
        with psycopg.connect(scratch_db, autocommit=True) as conn:
            conn.execute(insert)
        with pytest.raises(Exception, match=message):
            downgrade(scratch_db, before)
        assert set(TABLE_NAMES) <= _tables(scratch_db)
        with psycopg.connect(scratch_db, autocommit=True) as conn:
            conn.execute(remove)
    downgrade(scratch_db, before)
    assert not set(TABLE_NAMES) & _tables(scratch_db)
    assert "dashboard_id" not in _columns(scratch_db, "api_tokens")
    with psycopg.connect(scratch_db) as conn:  # the v0.19 rows survived the round trip
        assert conn.execute("SELECT id FROM api_tokens").fetchall() == [("tok_1",)]
        assert conn.execute("SELECT id FROM dashboards").fetchall() == [("dsh_1",)]
        assert conn.execute("SELECT tenant_code FROM tenant_plugin_settings").fetchall() == [
            ("k3vq7m",)
        ]
    assert "auth_time" in _columns(scratch_db, "web_sessions")
    upgrade(scratch_db)
    assert set(TABLE_NAMES) <= _tables(scratch_db)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute(seeds[3][0])
        conn.execute("DELETE FROM dashboards WHERE id = 'dsh_1'")
        assert conn.execute("SELECT id FROM api_tokens").fetchall() == [("tok_1",)]
