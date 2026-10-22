"""Chart templates (v0.18 D5): previews on the source recipe's catalog example, and the
template applied to a model version's stored plot (spec swapped, datasets kept untouched,
``provenance.author``, never embeddable). JSON only: no data is decoded, nothing runs."""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_plots.recipes import get_recipe
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_server_testkit.plots import model_with_plots
from mapi_spec.library import ChartPreview, LibraryItem, LibraryUseResult
from mapi_spec.plots import PlotExamples, PlotSpec
from mapi_spec.sharing import embeddable
from sqlalchemy import update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

URL = "/v1/library"
PREVIEW = f"{URL}/charts:preview"
SOURCE = "core.fit_vs_actual"
CHART: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ChartTemplate",
    "metadata": {"name": "fit-dots", "title": "Fit as dots"},
    "source_recipe": f"{SOURCE}@^1",
    "plot": {
        "data": {"name": "points"},
        "mark": "point",
        "encoding": {
            "x": {"field": "observed", "type": "quantitative"},
            "y": {"field": "point", "type": "quantitative"},
            "color": {"field": "group", "type": "nominal"},
        },
    },
    "caption_plain": "Each dot is one period: on the diagonal, the prediction was right.",
}


@functools.cache
def _checkout_catalog() -> str:
    registry = pytest.importorskip("mapi_sdk.registry")
    text: str = registry.default_registry().catalog().model_dump_json()
    return text


@functools.cache
def _example() -> PlotSpec:
    spec: PlotSpec = get_recipe(SOURCE).example_spec()
    return spec


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    path = tmp_path / "checkout_catalog.json"
    path.write_text(_checkout_catalog())
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, "catalog_path": path})


async def _with_examples(ctx: ServerContext) -> None:
    """Store the source recipe's example with the catalog (as a describe run does)."""
    example = _example()
    assert example.provenance.recipe is not None
    examples = PlotExamples(plots={example.provenance.recipe: example})
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Catalog)
            .where(m.Catalog.image_digest == ctx.catalogs.image_digest)
            .values(plot_examples=examples.model_dump(mode="json", exclude_none=True))
        )
    ctx.catalogs.invalidate()


async def _editor(ctx: ServerContext, client: httpx.AsyncClient) -> Any:
    u = await make_user(ctx, "editor@default.test")
    sc = ctx.scope
    await grant_role(ctx, u, sc.tenant_id, sc.project_id, "editor")
    return await session_caller(ctx, client, u, tenant_id=sc.tenant_id, project_id=sc.project_id)


def _chart(**plot: Any) -> dict[str, Any]:
    doc = copy.deepcopy(CHART)
    doc["plot"].update(plot)
    return doc


async def _preview(caller: Any, doc: dict[str, Any]) -> ChartPreview:
    r = await caller.post(PREVIEW, json={"document": doc})
    assert r.status_code == 200, r.text
    return ChartPreview.model_validate(r.json())


async def test_preview_on_the_catalog_example(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ed = await _editor(ctx, client)
    await _with_examples(ctx)
    out = await _preview(ed, CHART)
    assert out.plot is not None, out.issues
    plot = out.plot
    assert (plot.id, plot.title, plot.kind) == ("lib.fit-dots", "Fit as dots", "vega-lite")
    assert plot.provenance.author == "library-preview" and plot.provenance.recipe is None
    assert plot.spec["mark"] == "point"
    # the example's inline datasets pass through byte for byte (never decoded)
    example = _example()
    assert {k: v.model_dump() for k, v in plot.datasets.items()} == {
        k: v.model_dump() for k, v in example.datasets.items()
    }
    # an authored chart is never embeddable (share links, client portals, pictures)
    assert not embeddable("model_plot", "author", plot.provenance.recipe or "")


async def test_preview_answers_template_problems_as_issues(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ed = await _editor(ctx, client)
    await _with_examples(ctx)
    unknown_col = _chart(encoding={"x": {"field": "spend", "type": "quantitative"}})
    out = await _preview(ed, unknown_col)
    assert out.plot is None
    assert [(i.pointer, i.severity) for i in out.issues] == [("/plot/encoding/x/field", "error")]
    unknown_ds = _chart(data={"name": "secrets"})
    out = await _preview(ed, unknown_ds)
    assert out.plot is None and out.issues[0].pointer == "/plot/data/name"
    # the sanitizer refuses what could leave the sandbox
    for hostile in (
        _chart(usermeta={"embedOptions": {"loader": {"baseURL": "https://evil.test"}}}),
        _chart(data={"url": "https://evil.test/data.csv"}),
        _chart(encoding={"href": {"field": "group", "type": "nominal"},
                         "x": {"field": "observed", "type": "quantitative"},
                         "y": {"field": "point", "type": "quantitative"}}),
    ):  # fmt: skip
        out = await _preview(ed, hostile)
        assert out.plot is None, json.dumps(hostile["plot"])
        assert any(i.severity == "error" for i in out.issues)
    # not a chart template, or a recipe the catalog lacks
    r = await ed.post(PREVIEW, json={"document": {**CHART, "kind": "PrepPreset"}})
    assert r.status_code == 422 and r.json()["code"] == "LIBRARY_INVALID"
    r = await ed.post(PREVIEW, json={"document": {**CHART, "source_recipe": "acme.chart@^1"}})
    assert r.status_code == 409 and r.json()["code"] == "EXTENSION_UNAVAILABLE"


async def test_preview_without_an_example_is_409(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ed = await _editor(ctx, client)
    r = await ed.post(PREVIEW, json={"document": CHART})
    assert r.status_code == 409 and r.json()["code"] == "EXTENSION_UNAVAILABLE"
    # saving still works: the template is checked when it is applied (a warning)
    r = await ed.post(URL, json={"document": CHART})
    assert r.status_code == 201
    rev = (await ed.get(f"{URL}/{r.json()['id']}/revisions/1")).json()
    assert rev["valid"] is True
    assert [i["severity"] for i in rev["issues"]] == ["warning"]


def _pin_the_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limiter counts in fixed one-minute windows: keep every request of the test in
    one window, so a test that crosses a minute boundary does not flake."""
    from mapi_server.services.library import compile as library_compile

    at = library_compile.now()
    monkeypatch.setattr(library_compile, "now", lambda: at)


async def test_preview_is_rate_limited(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_the_minute(monkeypatch)
    ed = await _editor(ctx, client)
    await _with_examples(ctx)
    ctx.settings.library_chart_preview_per_minute = 1
    await _preview(ed, CHART)
    r = await ed.post(PREVIEW, json={"document": CHART})
    assert r.status_code == 429 and "Retry-After" in r.headers


async def test_apply_restyles_a_stored_plot(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    ed = await _editor(ctx, client)
    await _with_examples(ctx)
    model = await model_with_plots(ctx)
    stored = _example().model_dump(mode="json", by_alias=True)
    stored["datasets"] = {
        "points": {"kind": "data_ref", "path": "ui/plotdata/points.arrow"},
        "identity": {"kind": "data_ref", "path": "ui/plotdata/identity.arrow"},
    }
    sc = ctx.scope
    async with ctx.db.session() as s:
        s.add(
            m.Plot(
                model_version_id=model["mv_id"], plot_id=SOURCE, tenant_id=sc.tenant_id,
                project_id=sc.project_id, title="Fit", kind="vega-lite", persona="both",
                recipe=f"{SOURCE}@1.0.0", caption_plain=None, spec=stored,
            )
        )  # fmt: skip

    r = await ed.post(URL, json={"document": CHART})
    assert r.status_code == 201, r.text
    item = LibraryItem.model_validate(r.json())
    assert (await ed.get(f"{URL}/{item.id}/revisions/1")).json()["valid"] is True
    body = {"model_id": model["model_id"], "model_version": model["version"]}
    r = await ed.post(f"{URL}/{item.id}:use", json=body)
    assert r.status_code == 200, r.text
    used = LibraryUseResult.model_validate(r.json())
    plot = used.plot
    assert plot is not None and used.kind == "chart_template"
    assert plot.id == "lib.fit-dots" and plot.spec["mark"] == "point"
    assert plot.provenance.author == f"{item.id}-r1"
    assert plot.caption_plain == CHART["caption_plain"]
    # the stored plot's data_ref datasets are kept as they are
    kept = PlotSpec.model_validate(stored).datasets
    assert {k: v.model_dump(mode="json") for k, v in plot.datasets.items()} == {
        k: v.model_dump(mode="json") for k, v in kept.items()
    }
    assert {v.path for v in plot.datasets.values()} == {  # type: ignore[union-attr]
        "ui/plotdata/points.arrow",
        "ui/plotdata/identity.arrow",
    }
    assert not embeddable("model_plot", "author", "")

    # a revision that reads a column the source plot does not have: refused with 422
    bad = _chart(encoding={"x": {"field": "spend", "type": "quantitative"}})
    r = await ed.post(
        f"{URL}/{item.id}/revisions", json={"document": bad}, headers={"If-Match": item.etag}
    )
    assert r.status_code == 201 and r.json()["valid"] is False
    r = await ed.post(f"{URL}/{item.id}:use", json=body)
    assert r.status_code == 422 and r.json()["code"] == "LIBRARY_INVALID"
    assert r.json()["errors"][0]["pointer"] == "/plot/encoding/x/field"
    # the first revision still applies
    r = await ed.post(f"{URL}/{item.id}:use", json={**body, "revision": 1})
    assert r.status_code == 200

    # a version without a plot of the source recipe
    other = {**copy.deepcopy(CHART), "source_recipe": "core.residuals_over_time@^1"}
    other["metadata"]["name"] = "residual-dots"
    other["plot"]["data"] = {"name": "residuals"}
    other["plot"]["encoding"] = {
        "x": {"field": "x", "type": "temporal"},
        "y": {"field": "residual", "type": "quantitative"},
    }
    r = await ed.post(URL, json={"document": other})
    assert r.status_code == 201
    r = await ed.post(f"{URL}/{r.json()['id']}:use", json=body)
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/source_recipe"
