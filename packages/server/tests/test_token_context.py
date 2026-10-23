"""The dashboard context of a read token (v0.20, plan I-6, D11-D13).

:func:`token_context` re-checks the owner's membership and grant on every use, needs a
published layout 2 dashboard in the token's project, never sees private readouts, and refuses
a dashboard made with uploaded code for member owners too. Also :func:`chat_layout_ok`, and
the share-session re-check that does not count against the link's limits (D9).
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import sources
from mapi_server.services.dashboards.access import (
    DashboardTokenAuth,
    chat_layout_ok,
    member_context,
    token_context,
)
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.services.dashboards.shares import resolve_session, resolve_shared
from mapi_server.services.runs import now
from mapi_server.settings import Settings
from mapi_server_testkit.identity import grant_role, make_tenant
from mapi_spec.errors import MapiError
from sqlalchemy import delete, select, update
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit
from test_dashboards_crud_v2 import create_v2

pytestmark = pytest.mark.integration

UPLOADED: Final = "UPLOADED_CODE_NOT_SHAREABLE"
PIN: Final[dict[str, str]] = {
    "plugin_version_id": "plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
    "namespace": "t_k3vq7m_acme",
    "sha256": "sha256:" + "ab" * 32,
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=LAYOUT1)  # one layout 1 dashboard is made here too


async def v2_kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    """The dashboard users and a published layout 2 (``forecast``) dashboard granted to
    ``cleo``, with a ready published readout (seq 2) and a ready private one of ``eve`` (seq
    3). Shared by the v0.20 WP2 tests."""
    clear_grid_cache()
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    dash_id, _ = await create_v2(kit, "chat-fc")
    r = await kit["ana"].post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    r = await kit["ana"].put(f"/v1/dashboards/{dash_id}/grants/{kit['users']['cleo'].id}")
    assert r.status_code == 200, r.text
    kit["dash_id"] = dash_id
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.published_revision_id is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        for seq, visibility, actor_kind, actor_id in (
            (2, "published", "system", None),
            (3, "private", "member", kit["users"]["eve"].id),
        ):
            s.add(
                m.DashboardReadout(
                    tenant_id=dash.tenant_id, project_id=dash.project_id, dashboard_id=dash_id,
                    seq=seq, revision_id=rev.id, kind="scenario" if seq == 3 else "refresh",
                    trigger="manual", visibility=visibility, status="ready",
                    title=f"Readout {seq}", actor_kind=actor_kind, actor_id=actor_id,
                    model_version_id=rev.model_version_id, model_ref=rev.model_ref,
                    spec_hash=rev.spec_hash, run_ids=[], snapshot={"lead": f"R{seq}."},
                    created_at=now(), ready_at=now(),
                )
            )  # fmt: skip
    return kit


def token_for(kit: dict[str, Any], who: str, **over: str) -> DashboardTokenAuth:
    fields = {
        "token_id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
        "tenant_id": "default",
        "project_id": "default",
        "owner_id": kit["users"][who].id,
        "owner_kind": "user",
        "dashboard_id": kit["dash_id"],
        **over,
    }
    return DashboardTokenAuth(**fields)


async def pin_uploaded(ctx: ServerContext, kit: dict[str, Any]) -> None:
    """The dashboard's model version was fitted with an uploaded step (v0.19 lineage pin)."""
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, kit["model"]["mv_id"])
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlays": [PIN]}


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await v2_kit(ctx, client)


async def _refused(ctx: ServerContext, tok: DashboardTokenAuth) -> str:
    with pytest.raises(MapiError) as err:
        await token_context(ctx, tok)
    return err.value.code.value


# ======================================================================================= ok


async def test_member_and_client_tokens_are_non_analysts(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    for who, kind in (("eve", "member"), ("vic", "member"), ("ana", "member"), ("cleo", "client")):
        dctx = await token_context(ctx, token_for(kit, who))
        assert dctx.dashboard_id == kit["dash_id"] and dctx.tenant_id == "default"
        assert dctx.principal_kind == kind and dctx.principal_id == kit["users"][who].id
        assert dctx.is_analyst is False, who
        async with ctx.db.tenant_session("default") as s:
            assert await chat_layout_ok(s, dctx)


async def test_the_inputs_are_a_client_users(ctx: ServerContext, kit: dict[str, Any]) -> None:
    eve = await owner_auth(ctx, "default", "default", kit["users"]["eve"].id)
    assert eve is not None
    signed_in = await member_context(ctx.for_request(eve), eve, kit["dash_id"])
    assert signed_in.is_analyst is True
    tok = await token_context(ctx, token_for(kit, "eve"))
    cleo = await token_context(ctx, token_for(kit, "cleo"))
    assert tok.inputs == cleo.inputs
    assert tok.revision_id == signed_in.revision_id


# ================================================================================= refusals


async def test_a_removed_grant_ends_a_client_token(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await token_context(ctx, token_for(kit, "cleo"))
    r = await kit["ana"].delete(f"/v1/dashboards/{kit['dash_id']}/grants/{kit['users']['cleo'].id}")
    assert r.status_code in (200, 204), r.text
    assert await _refused(ctx, token_for(kit, "cleo")) == "NOT_FOUND"


async def test_a_removed_member_ends_the_token(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await token_context(ctx, token_for(kit, "vic"))
    async with ctx.db.session() as s:
        await s.execute(
            delete(m.Membership).where(m.Membership.principal_id == kit["users"]["vic"].id)
        )
    assert await _refused(ctx, token_for(kit, "vic")) == "NOT_FOUND"


async def test_a_disabled_owner_or_a_suspended_tenant_is_unauthenticated(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            update(m.User).where(m.User.id == kit["users"]["eve"].id).values(status="disabled")
        )
    assert await _refused(ctx, token_for(kit, "eve")) == "UNAUTHENTICATED"
    assert await _refused(ctx, token_for(kit, "cleo", owner_kind="service_account")) == (
        "UNAUTHENTICATED"
    )
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).where(m.Tenant.id == "default").values(status="suspended"))
    assert await _refused(ctx, token_for(kit, "cleo")) == "UNAUTHENTICATED"


async def test_an_unpublished_dashboard_is_not_found(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    draft_id, _ = await create_v2(kit, "chat-draft")
    assert await _refused(ctx, token_for(kit, "eve", dashboard_id=draft_id)) == "NOT_FOUND"
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}:archive")
    assert r.status_code == 200, r.text
    assert await _refused(ctx, token_for(kit, "eve")) == "NOT_FOUND"


async def test_a_layout_1_dashboard_is_refused(ctx: ServerContext, kit: dict[str, Any]) -> None:
    v1_id = await create_published(kit, "chat-v1")
    assert await _refused(ctx, token_for(kit, "cleo", dashboard_id=v1_id)) == "FORBIDDEN"
    cleo = await owner_auth(ctx, "default", "default", kit["users"]["cleo"].id)
    assert cleo is not None
    dctx = await member_context(ctx.for_request(cleo), cleo, v1_id)
    async with ctx.db.tenant_session("default") as s:
        assert not await chat_layout_ok(s, dctx)


async def test_another_tenant_or_project_is_not_found(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await make_tenant(ctx, "other", projects=("main",))
    eve = kit["users"]["eve"]
    await grant_role(ctx, eve, "other", "main", "editor")
    other = token_for(kit, "eve", tenant_id="other", project_id="main")
    assert await _refused(ctx, other) == "NOT_FOUND"  # row-level security hides it
    # a token of the same tenant whose project is not the dashboard's
    await make_tenant(ctx, "default", projects=("second",))
    await grant_role(ctx, eve, "default", "second", "editor")
    assert await _refused(ctx, token_for(kit, "eve", project_id="second")) == "NOT_FOUND"


async def test_uploaded_code_is_refused_for_member_owners_too(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await pin_uploaded(ctx, kit)
    for who in ("eve", "ana", "cleo"):
        assert await _refused(ctx, token_for(kit, who)) == UPLOADED, who
    # signed in, the analyst still works on it
    eve = await owner_auth(ctx, "default", "default", kit["users"]["eve"].id)
    assert eve is not None
    assert (await member_context(ctx.for_request(eve), eve, kit["dash_id"])).is_analyst


# ======================================================== share sessions: re-checks (D9)


async def _share_counts(ctx: ServerContext) -> dict[str, int]:
    async with ctx.db.session() as s:
        rows = await s.execute(select(m.RateCounter.subject, m.RateCounter.count))
        return {
            str(subject): int(n)
            for subject, n in rows
            if str(subject).startswith(("shareip:", "share:"))
        }


async def test_a_recheck_without_count_leaves_the_share_limits_alone(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/shares", json={})
    assert created.status_code == 201, created.text
    r = await client.post("/v1/share-sessions", json={"token": created.json()["token"]})
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    counted = await resolve_session(ctx, session, "203.0.113.9")  # the request's own check
    before = await _share_counts(ctx)
    assert any(k.startswith("shareip:") for k in before)
    assert any(k.startswith("share:") for k in before)
    for _ in range(5):
        again = await resolve_session(ctx, session, "203.0.113.9", count=False)
        assert again == counted
    view = await resolve_shared(ctx, session, "203.0.113.9", count=False)
    assert view.context == counted
    assert await _share_counts(ctx) == before
    await resolve_session(ctx, session, "203.0.113.9")  # the default still counts
    after = await _share_counts(ctx)
    assert sum(after.values()) == sum(before.values()) + 2
    # every other check still runs: a forged session, then a revoked link
    with pytest.raises(MapiError) as err:
        forged = session[:-1] + ("B" if session.endswith("A") else "A")
        await resolve_session(ctx, forged, "203.0.113.9", count=False)
    assert err.value.code.value == "UNAUTHENTICATED"
    path = f"/v1/dashboards/{kit['dash_id']}/shares/{created.json()['id']}:revoke"
    assert (await kit["ana"].post(path)).status_code == 200
    with pytest.raises(MapiError) as err:
        await resolve_session(ctx, session, "203.0.113.9", count=False)
    assert err.value.code.value == "LINK_EXPIRED"
    assert sum((await _share_counts(ctx)).values()) == sum(after.values())
