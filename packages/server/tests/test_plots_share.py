"""Saved plots, static rendering and embed links (M3; plan §10.3 items 4-5).

The model version under test is finalized for real (signed manifest, server-side copy) with
four plots: a recipe plot with inline data, a recipe plot whose data is a bundle object
(``data_ref``), a plugin plot and a Plotly recipe plot.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.app import openapi_document
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import load_run, now
from mapi_server.services.sharing import clear_embed_cache, embed_token_hash
from mapi_server_testkit.plots import (
    ARROW,
    INLINE,
    PLOTS,
    complete_run,
    model_with_plots,
    plot_source,
    vl_plot,
)
from mapi_spec.api import M3_ENDPOINTS, PlotRender
from mapi_spec.audit import AuditAction
from mapi_spec.run import RunState
from mapi_spec.sharing import EMBED_TOKEN_RE, EmbedView, PlotEmbedCreated, SavedPlot
from sqlalchemy import select

pytestmark = pytest.mark.integration

PLOTS_ORIGIN = "http://127.0.0.1:5174"


@pytest.fixture
async def model(ctx: ServerContext) -> dict[str, Any]:
    return await model_with_plots(ctx)


_source = plot_source
_complete = complete_run


async def _save(client: httpx.AsyncClient, model: dict[str, Any], plot_id: str) -> SavedPlot:
    r = await client.post("/v1/plots", json={"title": plot_id, "source": _source(model, plot_id)})
    assert r.status_code == 201, r.text
    return SavedPlot.model_validate(r.json())


async def _embed(client: httpx.AsyncClient, plot_id: str, **body: Any) -> PlotEmbedCreated:
    r = await client.post(f"/v1/plots/{plot_id}/embeds", json=body)
    assert r.status_code == 201, r.text
    return PlotEmbedCreated.model_validate(r.json())


# ----------------------------------------------------------------------------- saved plots


async def test_saved_plot_lifecycle(client: httpx.AsyncClient, model: dict[str, Any]) -> None:
    saved = await _save(client, model, "core.ppc_fan")
    assert saved.embeddable and saved.provenance == "recipe"
    assert saved.recipe == "core.ppc_fan@1.0.0" and saved.source_kind == "model_plot"
    assert saved.spec is not None and saved.spec.datasets["d"].kind == "data_ref"
    plugin = await _save(client, model, "acme.custom")
    assert not plugin.embeddable and plugin.provenance == "plugin"
    page = (await client.get("/v1/plots?limit=1")).json()
    assert [p["id"] for p in page["items"]] == [plugin.id] and page["items"][0]["spec"] is None
    rest = (await client.get(f"/v1/plots?limit=1&cursor={page['next_cursor']}")).json()
    assert [p["id"] for p in rest["items"]] == [saved.id] and rest["next_cursor"] is None
    got = SavedPlot.model_validate((await client.get(f"/v1/plots/{saved.id}")).json())
    assert got.spec is not None and got.spec.id == "core.ppc_fan"
    assert (await client.delete(f"/v1/plots/{saved.id}")).status_code == 204
    assert (await client.get(f"/v1/plots/{saved.id}")).status_code == 404
    assert (await client.delete(f"/v1/plots/{saved.id}")).status_code == 404


async def test_saving_refuses_unknown_sources_and_unsafe_specs(
    client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    missing = await client.post(
        "/v1/plots", json={"title": "x", "source": _source(model, "core.nope")}
    )
    assert missing.status_code == 404
    other = {**_source(model, "core.ppc_fan"), "model_id": "mdl_nope"}
    assert (await client.post("/v1/plots", json={"title": "x", "source": other})).status_code == 404
    unsafe = vl_plot("x.evil", {"d": {"kind": "inline_arrow", "data": INLINE}}, author="me")
    unsafe["spec"] = {
        "mark": "image",
        "data": {"name": "d"},
        "encoding": {"url": {"value": "https://attacker.example/beacon.png"}},
    }
    r = await client.post("/v1/plots", json={"title": "x", "spec": unsafe})
    assert r.status_code == 422 and r.json()["code"] == "PLOT_UNSAFE"
    ref = vl_plot(
        "x.ref", {"d": {"kind": "data_ref", "path": "ui/plotdata/fan.arrow"}}, author="me"
    )
    r = await client.post("/v1/plots", json={"title": "x", "spec": ref})
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    both = {"title": "x", "spec": PLOTS[0], "source": _source(model, "core.inline")}
    assert (await client.post("/v1/plots", json=both)).status_code == 422


async def test_user_specs_are_authored_whatever_they_claim(
    client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    """A hand-written spec claiming a platform recipe is saved as ``author`` provenance and is
    never embeddable (plan §10.3 item 4)."""
    claimed = PLOTS[0]  # provenance.recipe = core.fan@1.0.0
    r = await client.post("/v1/plots", json={"title": "mine", "spec": claimed})
    assert r.status_code == 201, r.text
    saved = SavedPlot.model_validate(r.json())
    assert saved.provenance == "author" and saved.recipe is None and not saved.embeddable
    assert saved.spec is not None and saved.spec.provenance.recipe is None
    assert saved.spec.provenance.author is not None
    r = await client.post(f"/v1/plots/{saved.id}/embeds", json={})
    assert r.status_code == 422 and r.json()["code"] == "EMBED_NOT_ALLOWED"


# ----------------------------------------------------------------------------- embeds


async def test_embed_token_lifecycle(
    ctx: ServerContext, client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    saved = await _save(client, model, "core.ppc_fan")
    created = await _embed(
        client, saved.id, label="board deck", expires_in_days=7,
        allowed_origins=["https://acme.notion.site"],
    )  # fmt: skip
    token = created.token
    assert EMBED_TOKEN_RE.match(token) and created.url == f"{PLOTS_ORIGIN}/embed/{token}"
    assert created.embed.status == "active" and created.embed.display == token[:10] + "…"
    assert token not in repr(created)
    async with ctx.db.session() as s:
        row = await s.get(m.PlotEmbed, created.embed.id)
        assert row is not None
        assert row.token_hash == embed_token_hash(ctx.secrets.token_pepper, token)
        assert token not in json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.c})
    listing = (await client.get(f"/v1/plots/{saved.id}/embeds")).json()["items"]
    assert [e["id"] for e in listing] == [created.embed.id] and "token" not in listing[0]

    public = await client.get(f"/v1/embeds/{token}", headers={"Origin": PLOTS_ORIGIN})
    assert public.status_code == 200, public.text
    assert public.headers["access-control-allow-origin"] == PLOTS_ORIGIN
    assert "access-control-allow-credentials" not in public.headers
    # never cached by a browser or a CDN: revocation takes effect on the next load
    assert public.headers["cache-control"] == "private, no-cache"
    assert public.headers["x-robots-tag"].startswith("noindex")
    view = EmbedView.model_validate(public.json())
    assert view.title == "core.ppc_fan" and view.allowed_origins == ["https://acme.notion.site"]
    data = view.plot.datasets["d"]
    assert data.kind == "inline_arrow" and base64.b64decode(data.data) == ARROW
    assert view.expires_at == created.embed.expires_at

    again = await client.get(f"/v1/embeds/{token}")
    assert again.status_code == 200 and "access-control-allow-origin" not in again.headers
    listing = (await client.get(f"/v1/plots/{saved.id}/embeds")).json()["items"]
    assert listing[0]["views"] == 2 and listing[0]["last_viewed_at"] is not None

    url = f"/v1/plots/{saved.id}/embeds/{created.embed.id}"
    assert (await client.delete(url)).status_code == 204
    gone = await client.get(f"/v1/embeds/{token}")
    assert gone.status_code == 410 and gone.json()["code"] == "LINK_EXPIRED"
    assert (await client.delete(url)).status_code == 204  # idempotent
    listing = (await client.get(f"/v1/plots/{saved.id}/embeds")).json()["items"]
    assert listing[0]["status"] == "revoked" and listing[0]["revoked_at"] is not None
    async with ctx.db.session() as s:
        actions = [
            a.action
            for a in (await s.execute(select(m.AuditEvent).order_by(m.AuditEvent.ts))).scalars()
            if a.action.startswith("plot.")
        ]
    assert actions == [AuditAction.PLOT_EMBED_CREATE.value, AuditAction.PLOT_EMBED_REVOKE.value]


async def test_expired_unknown_and_malformed_links(
    ctx: ServerContext, client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    saved = await _save(client, model, "core.inline")
    created = await _embed(client, saved.id)
    async with ctx.db.session() as s:
        row = await s.get(m.PlotEmbed, created.embed.id)
        assert row is not None
        row.expires_at = now() - timedelta(seconds=1)
    r = await client.get(f"/v1/embeds/{created.token}")
    assert r.status_code == 410 and r.json()["code"] == "LINK_EXPIRED"
    listing = (await client.get(f"/v1/plots/{saved.id}/embeds")).json()["items"]
    assert listing[0]["status"] == "expired"
    unknown = "mpe_" + "0" * 40
    assert (await client.get(f"/v1/embeds/{unknown}")).status_code == 404
    assert (await client.get("/v1/embeds/not-a-token")).status_code == 404
    # deleting the plot removes its links (404, not 410)
    fresh = await _embed(client, saved.id)
    assert (await client.delete(f"/v1/plots/{saved.id}")).status_code == 204
    assert (await client.get(f"/v1/embeds/{fresh.token}")).status_code == 404


async def test_only_platform_recipes_are_embeddable(
    client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    plugin = await _save(client, model, "acme.custom")
    r = await client.post(f"/v1/plots/{plugin.id}/embeds", json={})
    assert r.status_code == 422 and r.json()["code"] == "EMBED_NOT_ALLOWED"
    assert "platform recipes" in r.json()["detail"]
    recipe = await _save(client, model, "core.inline")
    assert (await client.post(f"/v1/plots/{recipe.id}/embeds", json={})).status_code == 201
    bad = await client.post(
        f"/v1/plots/{recipe.id}/embeds", json={"allowed_origins": ["http://insecure.example"]}
    )
    assert bad.status_code == 422 and bad.json()["code"] == "SCHEMA_INVALID"


async def test_embeds_ignore_credentials_and_answer_cors_for_the_plot_sandbox_only(
    client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    saved = await _save(client, model, "core.inline")
    token = (await _embed(client, saved.id)).token
    r = await client.get(
        f"/v1/embeds/{token}",
        headers={
            "Origin": "http://127.0.0.1:5173",  # the web app: not allowed to read embeds
            "Cookie": "mapi_session=forged; mapi_csrf=x",
            "Authorization": "Bearer mapi_not_a_real_token",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers and "set-cookie" not in r.headers
    assert r.headers["vary"] == "Origin"
    pre = await client.options(
        f"/v1/embeds/{token}",
        headers={"Origin": PLOTS_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == PLOTS_ORIGIN
    assert pre.headers["access-control-allow-methods"] == "GET"
    other = await client.options(
        f"/v1/embeds/{token}",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert other.status_code == 403 and "access-control-allow-origin" not in other.headers
    # the app's own CORS policy is untouched elsewhere
    app = await client.get("/v1/plots", headers={"Origin": "http://127.0.0.1:5173"})
    assert app.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


async def test_public_views_are_rate_limited_per_link_and_per_address(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    model: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctx.settings, "embed_rate_per_minute", 3)
    monkeypatch.setattr(ctx.settings, "embed_ip_rate_per_minute", 5)
    saved = await _save(client, model, "core.inline")
    first = (await _embed(client, saved.id)).token
    second = (await _embed(client, saved.id)).token
    codes = [(await client.get(f"/v1/embeds/{first}")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    limited = await client.get(f"/v1/embeds/{second}")  # 5th request from this address
    assert limited.status_code == 200
    r = await client.get(f"/v1/embeds/{second}")
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    assert int(r.headers["retry-after"]) >= 1


# ----------------------------------------------------------------------------- rendering


async def test_render_pins_the_plot_and_its_bundle_data_then_links_the_image(
    ctx: ServerContext, client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    body = {"source": _source(model, "core.ppc_fan"), "format": "pdf", "width": 800}
    r = await client.post("/v1/plots:render", json=body, headers={"Idempotency-Key": "r-1"})
    assert r.status_code == 202, r.text
    pending = PlotRender.model_validate(r.json())
    assert not pending.done and pending.format == "pdf" and pending.run.kind == "render"
    again = await client.post("/v1/plots:render", json=body, headers={"Idempotency-Key": "r-1"})
    assert again.json()["run"]["id"] == pending.run.id
    async with ctx.db.session() as s:
        run = await load_run(s, None, pending.run.id)
        mv = await s.get(m.ModelVersion, model["mv_id"])
    assert mv is not None and run.model_version_id is None
    assert run.payload == {"plots": ["core.ppc_fan"], "format": "pdf", "scale": 2.0, "width": 800}
    assert run.timeout_s == ctx.settings.render_timeout_s and run.resource_class == "S"
    fan = mv.manifest["objects"]["ui/plotdata/fan.arrow"]
    assert run.inputs["plotdata.0"] == {
        "bucket": fan["bucket"],
        "name": fan["key"],
        "generation": fan["generation"],
    }
    doc = json.loads(
        await ctx.store.get(run.inputs["plots"]["bucket"], run.inputs["plots"]["name"])
    )
    assert doc[0]["datasets"]["d"]["path"] == "plotdata.0"
    assert run.inputs["plots"]["name"] == f"t/default/runs/{run.id}/inputs/plots.json"

    await _complete(
        ctx, run.id, {"renders/core.ppc_fan.pdf": (b"%PDF-1.7 fake", "application/pdf")}
    )
    done = PlotRender.model_validate((await client.get(f"/v1/plots/renders/{run.id}")).json())
    assert done.done and done.run.state is RunState.SUCCEEDED
    assert [o.name for o in done.outputs] == ["renders/core.ppc_fan.pdf"]
    other = await client.get(f"/v1/plots/renders/{model['mv_id']}")
    assert other.status_code == 404


async def test_render_of_inline_saved_and_plotly_plots(
    client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    mine = dict(PLOTS[0], id="mine.inline")
    r = await client.post(
        "/v1/plots:render", json={"plot": mine, "format": "svg"}, headers={"Prefer": "wait=1"}
    )
    assert r.status_code == 202 and r.headers["preference-applied"] == "wait=1"
    saved = await _save(client, model, "core.inline")
    r = await client.post("/v1/plots:render", json={"saved_plot_id": saved.id})
    assert r.status_code == 202 and r.json()["format"] == "png"
    plotly = await client.post("/v1/plots:render", json={"source": _source(model, "core.surface")})
    assert plotly.status_code == 422 and plotly.json()["code"] == "MODE_NOT_SUPPORTED"
    missing = await client.post("/v1/plots:render", json={"saved_plot_id": "plt_" + "0" * 26})
    assert missing.status_code == 404
    two = await client.post(
        "/v1/plots:render", json={"saved_plot_id": saved.id, "source": _source(model, "x")}
    )
    assert two.status_code == 422


async def test_embed_png_fallback_is_rendered_copied_and_signed(
    ctx: ServerContext, client: httpx.AsyncClient, model: dict[str, Any]
) -> None:
    saved = await _save(client, model, "core.inline")
    created = await _embed(client, saved.id)
    async with ctx.db.session() as s:
        row = await s.get(m.PlotEmbed, created.embed.id)
        assert row is not None and row.render_run_id is not None
        render_id = row.render_run_id
    first = EmbedView.model_validate((await client.get(f"/v1/embeds/{created.token}")).json())
    assert first.image_url is None  # not rendered yet
    png = b"\x89PNG\r\n\x1a\nfake"
    await _complete(ctx, render_id, {"renders/core.inline.png": (png, "image/png")})
    clear_embed_cache()
    view = EmbedView.model_validate((await client.get(f"/v1/embeds/{created.token}")).json())
    assert view.image_url is not None and view.image_url.startswith(ctx.store.base_url)  # type: ignore[attr-defined]
    key = f"t/default/embeds/{created.embed.id}/plot.png"
    assert await ctx.store.get(ctx.buckets.bundles, key) == png
    url = f"/v1/plots/{saved.id}/embeds/{created.embed.id}"
    assert (await client.delete(url)).status_code == 204
    snapshot = ctx.store.snapshot()  # type: ignore[attr-defined]
    assert (ctx.buckets.bundles, key) not in snapshot


# ----------------------------------------------------------------------------- the route table


def test_every_plot_row_is_served_with_its_operation_and_problems() -> None:
    doc = openapi_document()
    rows = [e for e in M3_ENDPOINTS if "/plots" in e.path or "/embeds" in e.path]
    assert len(rows) == 10
    for e in rows:
        op = doc["paths"][e.path][e.method.lower()]
        assert op["operationId"] == e.operation_id
        assert str(e.success) in op["responses"], e.operation_id
        for code in e.errors:
            text = json.dumps(op["responses"])
            assert code.value in text, (e.operation_id, code)
    assert "Problem" in doc["components"]["schemas"]
    render = doc["paths"]["/v1/plots:render"]["post"]
    assert "202" in render["responses"] and "MODE_NOT_SUPPORTED" in json.dumps(render)
