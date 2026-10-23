"""The mount gate (v0.19 D9, D11, D15; I-7 ``gate.py``): which pins a run may mount and which
versions a caller's catalog view shows.

``pins_problem`` matrix: approved (anyone in the tenant), private (only the uploader, only
while the tenant allows private use), yanked, purged, not yet passed, a hash or namespace
that differs from the stored version, another tenant's version, a run with no creator.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.plugins.gate import pins_problem, visible_versions
from mapi_server.services.plugins.store import set_state
from mapi_server.services.runs import now
from mapi_spec.plugins import OverlayPin, PluginExports
from sqlalchemy import update
from test_plugins_api import (
    SHA_I,
    World,
    claim,
    fake_inspect,
    make_world,
    move,
    pass_kit,
    settings,
    upload,
)

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]
__all__ = ["fake_inspect", "settings"]  # fixtures shared with test_plugins_api


def pin_of(plugin: dict[str, Any], v: dict[str, Any], **override: str) -> OverlayPin:
    return OverlayPin.model_validate(
        {
            "plugin_version_id": v["id"],
            "namespace": plugin["namespace"],
            "sha256": v["overlay_sha256"],
            **override,
        }
    )


async def problem(
    w: World, created_by: str | None, *pins: OverlayPin, tenant: str = "acme"
) -> str | None:
    async with w.ctx.db.tenant_session(tenant) as s:
        p = await pins_problem(s, tenant_id=tenant, created_by=created_by, pins=list(pins))
    if p is None:
        return None
    assert p.code == "PLUGIN_UNAVAILABLE" and p.status == 409
    return p.detail


async def test_pins_problem_matrix(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    editor, plain = w.uid("editor"), w.uid("plain")
    plugin = await claim(w["editor"])
    v = await upload(w, w["editor"], plugin)
    pin = pin_of(plugin, v)
    assert await problem(w, editor) is None  # no pins, no problem
    # inspecting: nobody may mount it
    assert "did not pass" in (await problem(w, editor, pin) or "")
    await pass_kit(ctx, v["id"])
    # private use: the uploader only
    assert await problem(w, editor, pin) is None
    assert "not approved" in (await problem(w, plain, pin) or "")
    assert "not approved" in (await problem(w, None, pin) or "")
    # ... and only while the tenant allows it
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantPluginSettings).values(private_use=False))
    assert "turned off" in (await problem(w, editor, pin) or "")
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantPluginSettings).values(private_use=True))
    # a different file or namespace than the stored version
    other_sha = "sha256:" + "00" * 32
    assert "not the file" in (await problem(w, editor, pin_of(plugin, v, sha256=other_sha)) or "")
    assert "not the file" in (
        await problem(w, editor, pin_of(plugin, v, namespace="t_aaaaaa_acme")) or ""
    )
    # approved: everyone in the tenant, nobody in another tenant
    await move(ctx, v["id"], "awaiting_approval", "approved")
    assert await problem(w, plain, pin) is None
    assert await problem(w, None, pin) is None
    assert "does not exist" in (await problem(w, None, pin, tenant="other") or "")
    # yanked, then purged
    async with ctx.db.session() as s:
        row = await s.get(m.PluginVersion, v["id"], with_for_update=True)
        assert row is not None
        set_state(row, "yanked", now=now())
        row.yank_reason = "security"
        row.yanked_by = editor
    detail = await problem(w, editor, pin) or ""
    assert "withdrawn" in detail and plugin["namespace"] in detail and "0.1.0" in detail
    await move(ctx, v["id"], "purged")
    assert "removed" in (await problem(w, editor, pin) or "")
    # an unknown version
    ghost = pin_of(plugin, v, plugin_version_id="plv_" + "0" * 26)
    assert "does not exist" in (await problem(w, editor, ghost) or "")


async def test_one_bad_pin_refuses_the_run(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["editor"])
    good = await upload(w, w["editor"], plugin)
    bad = await upload(w, w["editor"], plugin, version="0.2.0")
    await pass_kit(ctx, good["id"])
    await move(ctx, good["id"], "awaiting_approval", "approved")
    detail = await problem(w, w.uid("plain"), pin_of(plugin, good), pin_of(plugin, bad))
    assert detail is not None and "0.2.0" in detail


async def test_visible_versions(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    editor, plain = w.uid("editor"), w.uid("plain")
    plugin = await claim(w["editor"])
    approved = await upload(w, w["editor"], plugin)
    private = await upload(w, w["editor"], plugin, version="0.2.0")
    pending = await upload(w, w["editor"], plugin, version="0.3.0")
    approved_digest = await pass_kit(ctx, approved["id"], base="sha256:" + "11" * 32)
    await move(ctx, approved["id"], "awaiting_approval", "approved")
    await pass_kit(ctx, private["id"], PluginExports())

    async def seen(user: str | None, tenant: str = "acme") -> dict[str, str]:
        async with ctx.db.tenant_session(tenant) as s:
            rows = await visible_versions(
                s, tenant_id=tenant, user_id=user, base_image_digest=SHA_I
            )
        return {r.version_id: r.scope for r in rows}

    assert await seen(editor) == {approved["id"]: "tenant", private["id"]: "private"}
    assert await seen(plain) == {approved["id"]: "tenant"}
    assert await seen(None) == {approved["id"]: "tenant"}
    assert await seen(editor, "other") == {}
    assert pending["id"] not in await seen(editor)
    # the approved one has a describe for another base image only: the newest passing one
    async with ctx.db.tenant_session("acme") as s:
        [row] = [
            r
            for r in await visible_versions(
                s, tenant_id="acme", user_id=plain, base_image_digest=SHA_I
            )
        ]
    assert row.describe_digest == approved_digest and row.namespace == plugin["namespace"]
    assert row.sha256 == approved["overlay_sha256"] and row.uploaded_by == editor
    # private use off: the uploader's own version leaves their view
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantPluginSettings).values(private_use=False))
    assert await seen(editor) == {approved["id"]: "tenant"}
