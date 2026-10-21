"""Pictures of layout 2 dashboards (v0.17, WP6, plan D4 and §3.5).

A picture panel shows a plot of the pinned model version, rendered to PNG by one render run
at publish (as the publisher) and served as bytes to anyone who may read the dashboard. The
model has the plots of :func:`~mapi_server_testkit.plots.model_with_plots`: ``core.inline``
(platform recipe ``core.fan``: a picture), ``acme.custom`` (plugin code: never shown) and
``core.surface`` (Plotly). ``ana`` administers ``default``, ``cleo`` is a client of it and
``bob`` administers tenant ``beta``.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import sources
from mapi_server.services.dashboards.assets import (
    MAX_PICTURES_PER_REVISION,
    png_size,
    queue_pictures,
    revision_assets,
)
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, grant_role, make_tenant, make_user, session_caller
from mapi_server_testkit.plots import complete_run, model_with_plots
from mapi_spec.dashboard_v2 import DashboardSpecV2
from sqlalchemy import select, update

pytestmark = pytest.mark.integration


def tiny_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PICTURE = {"id": "fan_pic", "kind": "recipe", "title": "Forecast picture",
           "recipe": "core.fan", "plot_id": "core.inline"}  # fmt: skip


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    model = await model_with_plots(ctx)
    await make_tenant(ctx, "beta")
    users = {}
    callers: dict[str, Caller] = {}
    for name, tenant, project, role in (
        ("ana", "default", None, "admin"),
        ("cleo", "default", "default", "client"),
        ("bob", "beta", None, "admin"),
    ):
        user = await make_user(ctx, f"{name}@{tenant}.test")
        await grant_role(ctx, user, tenant, project, role)
        users[name] = user
        callers[name] = await session_caller(
            ctx, client, user, tenant_id=tenant,
            project_id="default" if tenant == "default" else "main",
        )  # fmt: skip
    return {"model": model, "users": users, **callers}


async def picture_dashboard(
    kit: dict[str, Any], *panels: dict[str, Any], name: str = "pics", publish: bool = True
) -> str:
    ana: Caller = kit["ana"]
    r = await ana.get(f"/v1/dashboards:presets?model_id={kit['model']['model_id']}")
    assert r.status_code == 200, r.text
    spec = r.json()["spec"]
    spec["panels"] = [*spec["panels"][:4], *panels]
    r = await ana.post("/v1/dashboards", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    dash_id = str(r.json()["id"])
    if publish:
        r = await ana.post(f"/v1/dashboards/{dash_id}:publish")
        assert r.status_code == 200, r.text
        r = await ana.put(f"/v1/dashboards/{dash_id}/grants/{kit['users']['cleo'].id}")
        assert r.status_code == 200, r.text
    return dash_id


async def assets_of(ctx: ServerContext, dash_id: str) -> list[m.DashboardAsset]:
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.DashboardAsset).where(m.DashboardAsset.dashboard_id == dash_id)
                )
            ).scalars()
        )
        s.expunge_all()
    return rows


# ================================================================================== pictures


def test_png_size_reads_the_header() -> None:
    assert png_size(tiny_png(7, 3)) == (7, 3)
    assert png_size(b"GIF89a" + b"\x00" * 30) is None
    assert png_size(b"\x89PNG") is None


async def test_publish_renders_the_picture_and_serves_the_png(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await picture_dashboard(kit, PICTURE)
    (asset,) = await assets_of(ctx, dash_id)
    assert asset.status == "queued" and asset.run_id is not None
    assert asset.plot_id == "core.inline" and asset.recipe == "core.fan@1.0.0"
    async with ctx.db.session() as s:
        run = await s.get(m.Run, asset.run_id)
        assert run is not None and run.kind == "render"
    url = f"/v1/dashboards/{dash_id}/assets/{asset.id}"
    assert (await kit["ana"].get(url)).status_code == 404  # being drawn
    png = tiny_png(16, 9)
    await complete_run(ctx, asset.run_id, {"renders/core.inline.png": (png, "image/png")})
    for who in ("ana", "cleo"):
        r = await kit[who].get(url)
        assert r.status_code == 200, (who, r.text)
        assert r.headers["content-type"] == "image/png" and r.content == png
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "private, max-age=300"
    (ready,) = await assets_of(ctx, dash_id)
    assert ready.status == "ready" and (ready.width, ready.height) == (16, 9)
    assert ready.bytes == len(png) and ready.bucket == ctx.buckets.bundles
    # the readout builder sees it ready by plot id
    async with ctx.db.session() as s:
        found = await revision_assets(ctx, s, ready.revision_id)
    assert found["core.inline"].status == "ready"


async def test_picture_renders_count_against_the_daily_run_cap(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """The input caps (``inputs._limits_check``) count picture renders as spent runs."""
    from mapi_server.errors import ProblemWithHeaders
    from mapi_server.services.dashboards import inputs
    from mapi_server.services.dashboards.access import any_revision_spec
    from mapi_server.services.dashboards.readouts import Actor

    dash_id = await picture_dashboard(kit, PICTURE)
    (asset,) = await assets_of(ctx, dash_id)
    assert asset.run_id is not None
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.published_revision_id is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        spec = any_revision_spec(rev)

        def capped(n: int) -> Any:
            return spec.model_copy(
                update={"limits": spec.limits.model_copy(update={"exact_runs_per_day": n})}
            )

        actor = Actor(kind="member", id=kit["users"]["ana"].id)
        await inputs._limits_check(ctx, s, dash, capped(2), actor)  # the picture is 1 of 2
        with pytest.raises(ProblemWithHeaders) as err:
            await inputs._limits_check(ctx, s, dash, capped(1), actor)
        assert err.value.code.value == "DASHBOARD_RUN_LIMIT"
        assert int(err.value.headers["Retry-After"]) > 0


async def test_assets_are_isolated_per_dashboard_and_tenant(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await picture_dashboard(kit, PICTURE)
    other = await picture_dashboard(kit, name="other")
    (asset,) = await assets_of(ctx, dash_id)
    assert asset.run_id is not None
    await complete_run(
        ctx, asset.run_id, {"renders/core.inline.png": (tiny_png(4, 4), "image/png")}
    )
    ana: Caller = kit["ana"]
    assert (await ana.get(f"/v1/dashboards/{dash_id}/assets/{asset.id}")).status_code == 200
    # another dashboard's path, an unknown id, another tenant
    assert (await ana.get(f"/v1/dashboards/{other}/assets/{asset.id}")).status_code == 404
    unknown = "dsa_" + "0" * 26
    assert (await ana.get(f"/v1/dashboards/{dash_id}/assets/{unknown}")).status_code == 404
    bob: Caller = kit["bob"]
    assert (await bob.get(f"/v1/dashboards/{dash_id}/assets/{asset.id}")).status_code == 404
    async with ctx.db.tenant_session("beta") as s:  # row-level security
        assert await s.get(m.DashboardAsset, asset.id) is None
    # a client loses the picture with the grant
    cleo = kit["users"]["cleo"].id
    assert (await ana.delete(f"/v1/dashboards/{dash_id}/grants/{cleo}")).status_code == 204
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}/assets/{asset.id}")
    assert r.status_code == 404


async def test_a_client_reads_only_the_pictures_of_the_published_revision(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await picture_dashboard(kit, PICTURE)
    (asset,) = await assets_of(ctx, dash_id)
    assert asset.run_id is not None
    await complete_run(
        ctx, asset.run_id, {"renders/core.inline.png": (tiny_png(4, 4), "image/png")}
    )
    url = f"/v1/dashboards/{dash_id}/assets/{asset.id}"
    assert (await kit["cleo"].get(url)).status_code == 200
    # the analyst removes the picture and publishes again: the old one is gone for clients
    ana: Caller = kit["ana"]
    got = (await ana.get(f"/v1/dashboards/{dash_id}")).json()
    spec = {**got["draft_spec"], "panels": [p for p in got["draft_spec"]["panels"]
                                            if p.get("id") != PICTURE["id"]]}  # fmt: skip
    r = await ana.put(f"/v1/dashboards/{dash_id}/draft", json={"spec": spec},
                      headers={"If-Match": got["draft_etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (await ana.post(f"/v1/dashboards/{dash_id}:publish")).status_code == 200
    assert (await kit["cleo"].get(url)).status_code == 404
    assert (await ana.get(url)).status_code == 200  # members: History of older revisions


async def test_plugin_plots_are_refused(ctx: ServerContext, kit: dict[str, Any]) -> None:
    plugin = {"id": "custom", "kind": "recipe", "title": "Custom",
              "recipe": "acme.custom", "plot_id": "acme.custom"}  # fmt: skip
    dash_id = await picture_dashboard(kit, plugin, publish=False)
    r = await kit["ana"].post(f"/v1/dashboards/{dash_id}:validate")
    body = r.json()
    assert body["ok"] is False
    hit = [i for i in body["issues"] if i["pointer"].endswith("/plot_id")]
    assert hit and "platform recipes" in hit[0]["message"]
    # queued anyway (a spec that skipped the checks): stored as failed, no run
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, dash_id)
        assert row is not None
        rev = await _revision(s, row)
        spec = DashboardSpecV2.model_validate(row.draft_spec)
        from mapi_server.services.dashboards.runas import owner_auth

        auth = await owner_auth(ctx, row.tenant_id, row.project_id, kit["users"]["ana"].id)
        assert auth is not None
        made = await queue_pictures(ctx.for_request(auth), s, rev, spec)
    assert [(a.status, a.run_id) for a in made] == [("failed", None)]
    assert made[0].problem is not None and "platform recipes" in made[0].problem


async def _revision(s: Any, row: m.Dashboard) -> m.DashboardRevision:
    """A revision of the draft as it stands (written straight to the table: the publish
    would refuse the spec)."""
    from mapi_spec.api import new_id
    from mapi_spec.dashboard_v2 import parse_dashboard_spec
    from mapi_spec.dashboards import dashboard_spec_hash

    mv = (
        await s.execute(select(m.ModelVersion).where(m.ModelVersion.status == "ready").limit(1))
    ).scalar_one()
    rev = m.DashboardRevision(
        id=new_id("dsr"), tenant_id=row.tenant_id, project_id=row.project_id,
        dashboard_id=row.id, seq=1, spec=row.draft_spec,
        spec_hash=dashboard_spec_hash(parse_dashboard_spec(row.draft_spec)),
        model_version_id=mv.id, model_ref="model://default/trend@v1", created_by="t",
    )  # fmt: skip
    s.add(rev)
    await s.flush()
    return rev


async def test_versions_with_uploaded_code_and_the_daily_cap_get_no_render(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            update(m.ModelVersion)
            .where(m.ModelVersion.id == kit["model"]["mv_id"])
            .values(lineage={"overlay_hash": "sha256:" + "1" * 64})
        )
    dash_id = await picture_dashboard(kit, PICTURE, publish=False)
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, dash_id)
        assert row is not None
        rev = await _revision(s, row)
        spec = DashboardSpecV2.model_validate(row.draft_spec)
        made = await queue_pictures(ctx, s, rev, spec)
        assert [(a.status, a.run_id) for a in made] == [("failed", None)]
        assert made[0].problem is not None and "uploaded code" in made[0].problem
        # idempotent per revision and plot
        assert await queue_pictures(ctx, s, rev, spec) == []
    async with ctx.db.session() as s:
        await s.execute(
            update(m.ModelVersion)
            .where(m.ModelVersion.id == kit["model"]["mv_id"])
            .values(lineage={})
        )
    capped = await picture_dashboard(kit, PICTURE, name="capped", publish=False)
    async with ctx.db.session() as s:
        row = await s.get(m.Dashboard, capped)
        assert row is not None
        draft = DashboardSpecV2.model_validate(row.draft_spec)
        spec = draft.model_copy(update={"limits": draft.limits.model_copy(
            update={"exact_runs_per_day": 0})})  # fmt: skip
        rev = await _revision(s, row)
        made = await queue_pictures(ctx, s, rev, spec)
    assert [a.status for a in made] == ["failed"]
    assert made[0].problem is not None and "daily run limit" in made[0].problem


def test_at_most_four_pictures_per_revision() -> None:
    assert MAX_PICTURES_PER_REVISION == 4
