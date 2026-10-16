"""Saved plots and embeds across tenants: requests, row-level security and token scopes.

The model lives in the platform tenant ``default``; ``ana`` administers it. ``bob``
administers tenant ``beta``. Embed tokens are public by design, everything else is not.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_server_testkit.plots import model_with_plots
from mapi_server_testkit.plots import plot_source as _source
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, ProgrammingError

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def model(ctx: ServerContext) -> dict[str, Any]:
    return await model_with_plots(ctx)


@pytest.fixture
async def shared(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    model: dict[str, Any],
) -> dict[str, Any]:
    await make_tenant(ctx, "beta")
    ana = await make_user(ctx, "ana@default.test")
    await grant_role(ctx, ana, "default", None, "admin")
    bob = await make_user(ctx, "bob@beta.test")
    await grant_role(ctx, bob, "beta", None, "admin")
    alice = await session_caller(ctx, client, ana, tenant_id="default", project_id="default")
    r = await alice.post(
        "/v1/plots", json={"title": "fan", "source": _source(model, "core.ppc_fan")}
    )
    assert r.status_code == 201, r.text
    plot_id = r.json()["id"]
    r = await alice.post(f"/v1/plots/{plot_id}/embeds", json={})
    assert r.status_code == 201, r.text
    beta = await session_caller(ctx, client, bob, tenant_id="beta", project_id="main")
    return {
        "ana": ana,
        "alice": alice,
        "beta": beta,
        "plot_id": plot_id,
        "embed_id": r.json()["embed"]["id"],
        "token": r.json()["token"],
        "model": model,
    }


async def test_another_tenant_cannot_reach_saved_plots_or_embeds(
    client: httpx.AsyncClient, shared: dict[str, Any]
) -> None:
    beta, plot_id, embed_id = shared["beta"], shared["plot_id"], shared["embed_id"]
    model = shared["model"]
    for method, path, body in [
        ("GET", f"/v1/plots/{plot_id}", None),
        ("DELETE", f"/v1/plots/{plot_id}", None),
        ("GET", f"/v1/plots/{plot_id}/embeds", None),
        ("POST", f"/v1/plots/{plot_id}/embeds", {}),
        ("DELETE", f"/v1/plots/{plot_id}/embeds/{embed_id}", None),
        ("POST", "/v1/plots:render", {"saved_plot_id": plot_id}),
        ("POST", "/v1/plots:render", {"source": _source(model, "core.ppc_fan")}),
        ("POST", "/v1/plots", {"title": "x", "source": _source(model, "core.ppc_fan")}),
    ]:
        r = await beta.request(method, path, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    assert (await beta.get("/v1/plots")).json()["items"] == []
    # naming the other tenant's project is refused before any handler runs
    r = await beta.in_project("default", "default").get(f"/v1/plots/{plot_id}")
    assert r.status_code == 404
    # the owner still sees everything, and the public link still works
    alice = shared["alice"]
    assert (await alice.get(f"/v1/plots/{plot_id}")).status_code == 200
    assert (await client.get(f"/v1/embeds/{shared['token']}")).status_code == 200


async def test_row_level_security_isolates_and_the_gateway_sees_only_the_lookup_columns(
    ctx: ServerContext, shared: dict[str, Any]
) -> None:
    from mapi_server.db import acting_as_gateway

    async with ctx.db.tenant_session("beta") as s:
        assert list((await s.execute(select(m.SavedPlot))).scalars()) == []
        assert list((await s.execute(select(m.PlotEmbed))).scalars()) == []
    async with ctx.db.tenant_session("default") as s:
        assert [p.id for p in (await s.execute(select(m.SavedPlot))).scalars()] == [
            shared["plot_id"]
        ]
    with pytest.raises((ProgrammingError, DBAPIError)):  # WITH CHECK: no rows into a tenant
        async with ctx.db.tenant_session("beta") as s:
            await s.execute(
                update(m.SavedPlot)
                .where(m.SavedPlot.id == shared["plot_id"])
                .values(tenant_id="beta")
            )
            row = await s.get(m.SavedPlot, shared["plot_id"])
            assert row is None
            s.add(
                m.PlotEmbed(
                    id="emb_" + "0" * 26,
                    tenant_id="default",
                    project_id="default",
                    plot_id=shared["plot_id"],
                    token_hash="x",
                    display="x",
                    allowed_origins=[],
                    views=0,
                    created_at=row.created_at if row else None,  # type: ignore[arg-type]
                    expires_at=None,  # type: ignore[arg-type]
                )
            )
            await s.flush()
    with acting_as_gateway():
        async with ctx.db.session() as s:
            assert (await s.execute(text("SELECT current_user"))).scalar_one() == "mapi_gateway"
            found = (
                await s.execute(
                    select(
                        m.PlotEmbed.id,
                        m.PlotEmbed.tenant_id,
                        m.PlotEmbed.project_id,
                        m.PlotEmbed.token_hash,
                        m.PlotEmbed.expires_at,
                        m.PlotEmbed.revoked_at,
                    )
                )
            ).all()
            assert [r[0] for r in found] == [shared["embed_id"]]
    forbidden = [
        select(m.SavedPlot),
        select(m.PlotEmbed.label),
        select(m.PlotEmbed.allowed_origins),
        update(m.PlotEmbed).values(revoked_at=None),
        update(m.SavedPlot).values(title="x"),
    ]
    for stmt in forbidden:
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with ctx.db.gateway_session() as s:
                # even pinned to the tenant (so row-level security alone would let rows through)
                await s.execute(text("SELECT set_config('app.tenant_id', 'default', true)"))
                await s.execute(stmt)


async def test_token_scopes_on_plot_routes(
    ctx: ServerContext, client: httpx.AsyncClient, shared: dict[str, Any]
) -> None:
    ana, plot_id = shared["ana"], shared["plot_id"]
    project = {"X-Mapi-Project": "default/default"}
    reader = token_caller(
        client, await make_token(ctx, ana, "default", "default", ["models:read"]), **project
    )
    assert (await reader.get("/v1/plots")).status_code == 200
    assert (await reader.get(f"/v1/plots/{plot_id}")).status_code == 200
    assert (await reader.post("/v1/plots", json={})).status_code == 403
    assert (
        await reader.post("/v1/plots:render", json={"saved_plot_id": plot_id})
    ).status_code == 403
    assert (await reader.post(f"/v1/plots/{plot_id}/embeds", json={})).status_code == 403
    runner = token_caller(
        client, await make_token(ctx, ana, "default", "default", ["runs:create"]), **project
    )
    r = await runner.post("/v1/plots:render", json={"saved_plot_id": plot_id})
    assert r.status_code == 202, r.text
    assert (await runner.delete(f"/v1/plots/{plot_id}")).status_code == 403
    admin = token_caller(
        client, await make_token(ctx, ana, "default", "default", ["admin"]), **project
    )
    assert (await admin.post(f"/v1/plots/{plot_id}/embeds", json={})).status_code == 201


async def test_an_editor_embeds_without_a_png_fallback(
    ctx: ServerContext, client: httpx.AsyncClient, shared: dict[str, Any]
) -> None:
    """Embedding needs ``create`` on embeds (editor); rendering the PNG fallback needs
    ``run`` on plots (runner), so an editor's embed simply has no image link."""
    carol = await make_user(ctx, "carol@default.test")
    await grant_role(ctx, carol, "default", "default", "editor")
    editor = await session_caller(ctx, client, carol, tenant_id="default", project_id="default")
    r = await editor.post(f"/v1/plots/{shared['plot_id']}/embeds", json={})
    assert r.status_code == 201, r.text
    async with ctx.db.session() as s:
        row = await s.get(m.PlotEmbed, r.json()["embed"]["id"])
        assert row is not None and row.render_run_id is None
    r = await editor.post("/v1/plots:render", json={"saved_plot_id": shared["plot_id"]})
    assert r.status_code == 403
