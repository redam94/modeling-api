"""Attested uploaders and the tenant's plugin settings (v0.19 D2, D11; I-5 settings routes).

A tenant admin attests an editor (not themselves), can revoke it, and turns private use on
or off; only tenant admins reach these routes, and API tokens never change them.
"""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server_testkit.identity import grant_role, make_token, make_user, token_caller
from sqlalchemy import select
from test_plugins_api import URL, claim, make_world, settings

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]
__all__ = ["settings"]  # the multi-tenant settings fixture of test_plugins_api

T = "/v1/tenants/acme"


async def test_attest_and_revoke(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    plain = w.uid("plain")
    r = await w["plain"].post(URL, json={"short_name": "plain", "title": "x"})
    assert r.status_code == 403
    r = await w["admin"].put(f"{T}/plugin-uploaders/{plain}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == plain and body["method"] == "admin_attested"
    assert body["verified_by"] == w.uid("admin") and body["email"] == "plain@acme.test"
    # idempotent
    assert (await w["admin2"].put(f"{T}/plugin-uploaders/{plain}")).json() == body
    listed = (await w["admin"].get(f"{T}/plugin-uploaders")).json()
    assert {u["user_id"] for u in listed} == {plain, w.uid("editor")}
    await claim(w["plain"], "plain")
    # revoked: no more uploads (claims, tickets, completes)
    r = await w["admin2"].delete(f"{T}/plugin-uploaders/{plain}")
    assert r.status_code == 204, r.text
    r = await w["plain"].post(URL, json={"short_name": "plain2", "title": "x"})
    assert r.status_code == 403
    assert (await w["admin2"].delete(f"{T}/plugin-uploaders/{plain}")).status_code == 404
    assert plain not in {
        u["user_id"] for u in (await w["admin"].get(f"{T}/plugin-uploaders")).json()
    }
    # attesting again restores it
    assert (await w["admin"].put(f"{T}/plugin-uploaders/{plain}")).status_code == 200
    await claim(w["plain"], "plain3")
    async with ctx.db.session() as s:
        actions = list(
            (
                await s.execute(
                    select(m.AuditEvent.action)
                    .where(m.AuditEvent.resource_id == plain)
                    .order_by(m.AuditEvent.ts)
                )
            ).scalars()
        )
    assert actions == [
        "plugin.uploader_attest",
        "plugin.uploader_revoke",
        "plugin.uploader_attest",
    ]


async def test_attest_refusals(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    r = await w["admin"].put(f"{T}/plugin-uploaders/{w.uid('admin')}")
    assert r.status_code == 403 and "your own" in r.json()["detail"], r.text
    stranger = await make_user(ctx, "stranger@else.test")
    r = await w["admin"].put(f"{T}/plugin-uploaders/{stranger.id}")
    assert r.status_code == 404, r.text
    r = await w["admin"].put(f"{T}/plugin-uploaders/{w.uid('outsider')}")
    assert r.status_code == 404, r.text  # a member of another tenant
    r = await w["admin"].put(f"{T}/plugin-uploaders/{w.uid('client')}")
    assert r.status_code == 409, r.text
    # only tenant admins, never a token
    for name in ("editor", "viewer"):
        r = await w[name].put(f"{T}/plugin-uploaders/{w.uid('plain')}")
        assert r.status_code == 403, (name, r.text)
        assert (await w[name].get(f"{T}/plugin-uploaders")).status_code == 403
        assert (await w[name].get(f"{T}/plugin-settings")).status_code == 403
    secret = await make_token(ctx, w.users["admin"], "acme", "growth", ["admin"])
    tok = token_caller(client, secret)
    assert (await tok.put(f"{T}/plugin-uploaders/{w.uid('plain')}")).status_code == 403
    assert (await tok.patch(f"{T}/plugin-settings", json={"private_use": False})).status_code == 403
    # another tenant's admin sees nothing of this one
    assert (await w["outsider"].get(f"{T}/plugin-settings")).status_code == 404


async def test_a_new_admin_member_can_be_attested(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    w = await make_world(ctx, client)
    person = await make_user(ctx, "new@acme.test")
    await grant_role(ctx, person, "acme", "retail", "editor")
    r = await w["admin"].put(f"{T}/plugin-uploaders/{person.id}")
    assert r.status_code == 200 and r.json()["display_name"] == "New"


async def test_settings(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    r = await w["admin"].get(f"{T}/plugin-settings")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tenant_code": None,
        "private_use": True,
        "uploads_allowed": True,
        "reason": "This workspace is trusted to run its own code.",
    }
    r = await w["admin"].patch(f"{T}/plugin-settings", json={"private_use": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["private_use"] is False and body["tenant_code"]  # assigned on first use
    plugin = await claim(w["admin"])
    assert plugin["namespace"].split("_")[1] == body["tenant_code"]
    async with ctx.db.session() as s:
        row = await s.get(m.TenantPluginSettings, "acme")
        assert row is not None and row.updated_by == w.uid("admin")
        details = (
            await s.execute(
                select(m.AuditEvent.details).where(m.AuditEvent.action == "plugin.settings_update")
            )
        ).scalar_one()
    assert details == {"private_use": False, "was": True}
    r = await w["admin"].patch(f"{T}/plugin-settings", json={"private_use": "maybe"})
    assert r.status_code == 422
