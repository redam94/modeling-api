"""A dashboard's chat settings (v0.20, plan D4 and I-6).

No row means the defaults (chat off); editors read and replace the settings under
``If-Match`` (``412`` without it or when stale); viewers and client users are refused; each
change is audited with the values only; a change never makes a revision; ``used today`` adds
up the current UTC day's usage rows.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import chat_settings as cs
from mapi_server.services.dashboards.access import member_context
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.services.runs import now
from mapi_server.settings import Settings
from mapi_spec.audit import AuditAction
from mapi_spec.chat import (
    CHAT_DEFAULT_SHARE_TOKENS_PER_DAY,
    CHAT_DEFAULT_TOKENS_PER_DAY,
    ChatAudiences,
    ChatSettingsUpdate,
)
from mapi_spec.errors import MapiError
from sqlalchemy import func, select
from test_dashboards_crud import LAYOUT1
from test_token_context import v2_kit

pytestmark = pytest.mark.integration

UPDATE = ChatSettingsUpdate(
    enabled=True,
    audiences=ChatAudiences(members=True, clients=False, share_links=True),
    keep_transcripts=True,
    tokens_per_day=1000,
    share_tokens_per_day=200,
)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=LAYOUT1)


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await v2_kit(ctx, client)


async def _auth(ctx: ServerContext, kit: dict[str, Any], who: str) -> RequestAuth:
    auth = await owner_auth(ctx, "default", "default", kit["users"][who].id)
    assert auth is not None
    return auth


async def _code(call: Any) -> str:
    with pytest.raises(MapiError) as err:
        await call
    return err.value.code.value


async def test_defaults_without_a_row(ctx: ServerContext, kit: dict[str, Any]) -> None:
    ids = cs.DashboardIds("default", "default", kit["dash_id"])
    async with ctx.db.tenant_session("default") as s:
        row = await cs.get_settings(s, ids)
    assert row.stored is False and row.enabled is False
    assert row.audiences == ChatAudiences(members=True, clients=True, share_links=False)
    assert row.keep_transcripts is False
    assert row.tokens_per_day == CHAT_DEFAULT_TOKENS_PER_DAY
    assert row.share_tokens_per_day == CHAT_DEFAULT_SHARE_TOKENS_PER_DAY
    assert row.version == 1 and row.etag == '"c1"'
    assert [row.audience_allows(k) for k in ("member", "client", "share")] == [True, True, False]
    view = await cs.read_settings(ctx, await _auth(ctx, kit, "eve"), kit["dash_id"])
    assert view.enabled is False and view.etag == '"c1"' and view.version == 1
    assert view.used_today_tokens == 0 and view.est_micros_today == 0
    assert view.updated_by is None and view.updated_at is None
    async with ctx.db.session() as s:
        n = await s.execute(select(func.count()).select_from(m.DashboardChatSettings))
        assert n.scalar_one() == 0  # reading writes nothing


async def test_put_needs_the_current_etag(ctx: ServerContext, kit: dict[str, Any]) -> None:
    eve = await _auth(ctx, kit, "eve")
    dash = kit["dash_id"]
    assert await _code(cs.put_settings(ctx, eve, dash, UPDATE, None)) == "PRECONDITION_FAILED"
    assert await _code(cs.put_settings(ctx, eve, dash, UPDATE, '"c2"')) == "PRECONDITION_FAILED"
    view = await cs.put_settings(ctx, eve, dash, UPDATE, '"c1"')
    assert view.version == 2 and view.etag == '"c2"'
    assert view.enabled is True and view.keep_transcripts is True
    assert view.audiences == UPDATE.audiences
    assert view.tokens_per_day == 1000 and view.share_tokens_per_day == 200
    assert view.updated_by == kit["users"]["eve"].id and view.updated_at is not None
    # a second editor with the old etag is refused; a weak etag of the current one is not
    ana = await _auth(ctx, kit, "ana")
    assert await _code(cs.put_settings(ctx, ana, dash, UPDATE, '"c1"')) == "PRECONDITION_FAILED"
    off = UPDATE.model_copy(update={"enabled": False})
    view = await cs.put_settings(ctx, ana, dash, off, 'W/"c2"')
    assert view.version == 3 and view.enabled is False
    assert (await cs.read_settings(ctx, eve, dash)) == view
    async with ctx.db.tenant_session("default") as s:
        row = await cs.get_settings(s, cs.DashboardIds("default", "default", dash))
    assert row.stored and row.version == 3 and row.audience_allows("share")
    assert not row.audience_allows("client")


async def test_only_editors_see_and_change_the_settings(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash = kit["dash_id"]
    for who in ("vic", "cleo"):
        auth = await _auth(ctx, kit, who)
        assert await _code(cs.read_settings(ctx, auth, dash)) == "FORBIDDEN", who
        assert await _code(cs.put_settings(ctx, auth, dash, UPDATE, '"c1"')) == "FORBIDDEN", who
    eve = await _auth(ctx, kit, "eve")
    assert await _code(cs.read_settings(ctx, eve, "dsh_" + "0" * 26)) == "NOT_FOUND"
    assert (await cs.read_settings(ctx, eve, dash)).dashboard_id == dash


async def test_a_change_is_audited_without_text_and_makes_no_revision(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash = kit["dash_id"]
    async with ctx.db.session() as s:
        before = await s.get(m.Dashboard, dash)
        assert before is not None
        rev_id, revisions = (
            before.published_revision_id,
            await s.scalar(select(func.count()).select_from(m.DashboardRevision)),
        )
    await cs.put_settings(ctx, await _auth(ctx, kit, "eve"), dash, UPDATE, '"c1"')
    async with ctx.db.session() as s:
        after = await s.get(m.Dashboard, dash)
        assert after is not None and after.published_revision_id == rev_id
        assert revisions == await s.scalar(select(func.count()).select_from(m.DashboardRevision))
        events = list(
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == AuditAction.DASHBOARD_CHAT_SETTINGS.value
                    )
                )
            ).scalars()
        )
    assert len(events) == 1
    ev = events[0]
    assert ev.resource_id == dash and ev.outcome == "success"
    assert ev.actor_id == kit["users"]["eve"].id
    assert ev.details == {
        "enabled": True, "members": True, "clients": False, "share_links": True,
        "keep_transcripts": True, "tokens_per_day": 1000, "share_tokens_per_day": 200,
        "version": 2,
    }  # fmt: skip


async def test_used_today_counts_the_current_utc_day(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash = kit["dash_id"]
    at = now()
    async with ctx.db.session() as s:
        for when, tokens in (
            (at, (100, 20, 5)),
            (at, (40, 10, 0)),
            (at - timedelta(days=2), (9000, 9000, 9000)),
        ):
            inp, out, thought = tokens
            s.add(
                m.DashboardChatUsage(
                    tenant_id="default", project_id="default", dashboard_id=dash,
                    principal_kind="member", principal_id=kit["users"]["eve"].id, share_id=None,
                    model="fake-chat", input_tokens=inp, output_tokens=out,
                    thought_tokens=thought, tool_calls=1, est_micros=inp + out,
                    price_version="test", outcome="ok", at=when,
                )
            )  # fmt: skip
    view = await cs.read_settings(ctx, await _auth(ctx, kit, "eve"), dash)
    assert view.used_today_tokens == 175 and view.est_micros_today == 170


async def test_the_settings_of_a_context(ctx: ServerContext, kit: dict[str, Any]) -> None:
    eve = await _auth(ctx, kit, "eve")
    await cs.put_settings(ctx, eve, kit["dash_id"], UPDATE, '"c1"')
    dctx = await member_context(ctx.for_request(eve), eve, kit["dash_id"])
    async with ctx.db.tenant_session("default") as s:
        row = await cs.get_settings(s, dctx)
    assert row.enabled and row.keep_transcripts and row.version == 2
