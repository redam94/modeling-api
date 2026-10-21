"""The source catalog of a model version and the layout 2 checks against it (v0.17, WP6).

The pure part (:func:`~mapi_server.services.dashboards.sources.build_catalog`) is fed the
golden manifests of ``fixtures/dashboards_v017``; the server part reads the trend model of
the v0.16 dashboard tests (``ts.hsgp_trend``, draws, a ``t`` time dim). The layout 2 rules
that need another model family (quantiles, point predictions, fixed rows, declared bounds)
run through ``:validate`` with the catalog of a golden case in place of the real one.
"""

from __future__ import annotations

import sys
from importlib import util
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import sources
from mapi_server.services.dashboards.sources import build_catalog, version_sources
from mapi_server_testkit.identity import Caller
from mapi_spec.capabilities import ModelManifest
from mapi_spec.dashboard_presets import catalog_from_manifest, compile_preset
from mapi_spec.dashboard_v2 import CatalogMetric, CatalogPicture, SourceCatalog
from mapi_spec.dashboards import DashboardSource
from mapi_spec.endpoints import EndpointSchemaSnapshot
from mapi_spec.schema import DimPolicy

from test_dashboards_crud import dashboard_kit

_p = Path(__file__).parent / "fixtures" / "dashboards_v017" / "loader.py"
_spec = util.spec_from_file_location("dashboards_v017_loader", _p)
assert _spec is not None and _spec.loader is not None
golden = sys.modules.setdefault(_spec.name, util.module_from_spec(_spec))
_spec.loader.exec_module(golden)


def golden_catalog(case: str, **over: Any) -> SourceCatalog:
    """The catalog the server would build for a golden case (its manifest, input contract
    and fit metrics), with ``over`` applied."""
    mat = golden.load_case(case)
    snap = EndpointSchemaSnapshot.model_validate(mat.input_contract["snapshot"])
    metrics = [
        CatalogMetric(key=s.key, labels={k: str(v) for k, v in s.labels.items()}, last=s.last)
        for s in mat.fit_metrics
    ]
    cat = build_catalog(mat.manifest, snapshot=snap, metrics=metrics)
    return cat.model_copy(update=over)


# ================================================================================ pure builder


def test_build_catalog_renames_output_dims_to_dataset_names() -> None:
    mat = golden.load_case("forecast_sarimax")
    snap = EndpointSchemaSnapshot.model_validate(mat.input_contract["snapshot"])
    cat = build_catalog(mat.manifest, snapshot=snap, binding_dims={"t": "date"})
    y_hat = next(o for o in cat.outputs if o.name == "y_hat")
    # the score summary names the dim columns as the dataset does: geo, date
    assert set(y_hat.dims) == {"geo", "date"} and y_hat.time_dim == "date"
    assert y_hat.representation.value == "quantiles" and y_hat.centre == "median"
    assert "sum" not in y_hat.reduces  # quantiles cannot be added up (D6)
    # inputs keep the canonical names (inline payloads use them), with the fitted levels
    dims = {i.dim: i for i in cat.inputs if i.dim is not None}
    assert set(dims) == {"geo", "t"}
    assert dims["geo"].levels and dims["t"].date_min is not None
    assert "forecast" in cat.score_modes and "forecast" in cat.views
    assert cat.namespace == "ts"


def test_build_catalog_keeps_metrics_scorecard_and_pictures() -> None:
    mat = golden.load_case("classifier_binary")
    metrics = [CatalogMetric(key="auc", last=0.81), CatalogMetric(key="accuracy", last=0.7)]
    pics = [CatalogPicture(plot_id="core.roc_pr", recipe="core.roc_pr", title="ROC")]
    cat = build_catalog(
        mat.manifest, metrics=metrics, scorecard=["holdout.auc", "holdout.auc"], pictures=pics
    )
    assert [x.key for x in cat.metrics] == ["auc", "accuracy"]
    assert cat.scorecard == ["holdout.auc"] and cat.pictures == pics


def test_build_catalog_without_a_manifest_has_no_outputs() -> None:
    mat = golden.load_case("regression_conformal")
    snap = EndpointSchemaSnapshot.model_validate(mat.input_contract["snapshot"])
    cat = build_catalog(None, snapshot=snap)
    assert cat.outputs == [] and cat.inputs == []
    assert set(cat.score_modes) == set(snap.score_modes)


def test_golden_catalogs_compile_their_preset() -> None:
    for case in ("forecast_bsts", "regression_point", "classifier_binary", "causal_impact"):
        mat = golden.load_case(case)
        cat = golden_catalog(case)
        src = DashboardSource(kind="model_version", model_id="m")
        spec = compile_preset(mat.preset, cat, title="T", source=src)
        assert spec.preset == mat.preset


def test_catalog_matches_the_manifest_only_catalog_for_outputs() -> None:
    manifest: ModelManifest = golden.load_case("forecast_bsts").manifest
    plain = catalog_from_manifest(manifest)
    built = build_catalog(manifest)
    assert [o.name for o in built.outputs] == [o.name for o in plain.outputs]


# ===================================================================================== server



@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    return await dashboard_kit(ctx, client)


async def _mv(ctx: ServerContext, mv_id: str) -> m.ModelVersion:
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, mv_id)
        assert mv is not None
        s.expunge(mv)
    return mv


@pytest.mark.integration
async def test_source_catalog_of_a_fitted_version(ctx: ServerContext, kit: dict[str, Any]) -> None:
    mv = await _mv(ctx, kit["model"]["mv_id"])
    async with ctx.db.session() as s:
        found = await version_sources(ctx, s, mv)
        again = await version_sources(ctx, s, mv)
    assert again is found  # cached per model version
    assert found.manifest is not None and found.manifest.id == "ts.hsgp_trend"
    cat = found.catalog
    names = {o.name for o in cat.outputs}
    assert {"y_hat", "trend"} <= names
    y_hat = next(o for o in cat.outputs if o.name == "y_hat")
    assert y_hat.time_dim is not None and y_hat.representation.value == "draws"
    assert {"sum", "mean", "last", "next"} <= set(y_hat.reduces)
    assert {"forecast", "predict"} <= set(cat.score_modes)


@pytest.mark.integration
async def test_sources_route_is_for_editors(kit: dict[str, Any]) -> None:
    eve: Caller = kit["eve"]
    r = await eve.get(f"/v1/dashboards:presets?model_id={kit['model']['model_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggestions"][0]["preset"] == "forecast"
    assert body["spec"]["layout_version"] == 2 and body["spec"]["preset"] == "forecast"
    r = await eve.post("/v1/dashboards", json={"name": "fc", "spec": body["spec"]})
    assert r.status_code == 201, r.text
    dash_id = r.json()["id"]
    r = await eve.get(f"/v1/dashboards/{dash_id}/sources")
    assert r.status_code == 200, r.text
    assert {"y_hat", "trend"} <= {o["name"] for o in r.json()["outputs"]}
    assert (await kit["vic"].get(f"/v1/dashboards/{dash_id}/sources")).status_code in (403, 404)
    assert (await kit["cleo"].get(f"/v1/dashboards/{dash_id}/sources")).status_code in (403, 404)


@pytest.mark.integration
async def test_presets_need_one_source(kit: dict[str, Any]) -> None:
    eve: Caller = kit["eve"]
    r = await eve.get("/v1/dashboards:presets")
    assert r.status_code == 422
    r = await eve.get("/v1/dashboards:presets?model_id=nope")
    assert r.status_code == 404
    r = await eve.get("/v1/dashboards:presets?model_id=trend&alias=x")
    assert r.status_code == 422


# ======================================================================= checks via :validate


async def _validate_with(
    kit: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    cat: SourceCatalog,
    spec: dict[str, Any],
    name: str = "chk",
) -> dict[str, Any]:
    async def fake(ctx: Any, session: Any, mv: Any) -> SourceCatalog:
        return cat

    monkeypatch.setattr(sources, "source_catalog", fake)
    eve: Caller = kit["eve"]
    spec = {**spec, "source": {"kind": "model_version", "model_id": "trend"}}
    r = await eve.post("/v1/dashboards", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    r = await eve.post(f"/v1/dashboards/{r.json()['id']}:validate")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _compiled(case: str, cat: SourceCatalog) -> dict[str, Any]:
    src = DashboardSource(kind="model_version", model_id="trend")
    mat = golden.load_case(case)
    return compile_preset(mat.preset, cat, title="T", source=src).model_dump(mode="json")


def _kpi(variable: str, how: str, over: list[str]) -> dict[str, Any]:
    return {
        "id": "total",
        "kind": "kpi",
        "title": "Total",
        "source": {"kind": "output", "variable": variable},
        "reduce": {"how": how, "over": over},
    }


@pytest.mark.integration
async def test_a_sum_of_quantiles_is_refused(
    kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = golden_catalog("forecast_sarimax")
    spec = _compiled("forecast_sarimax", cat)
    y_hat = next(o for o in cat.outputs if o.name == "y_hat")
    spec["panels"] = [_kpi("y_hat", "sum", [y_hat.time_dim or "t"]), *spec["panels"][:3]]
    body = await _validate_with(kit, monkeypatch, cat, spec)
    assert body["ok"] is False
    hit = [i for i in body["issues"] if i["pointer"] == "/panels/0/reduce"]
    assert hit and hit[0]["code"] == "BINDING_INVALID"
    assert "added up" in hit[0]["message"]


@pytest.mark.integration
async def test_a_sum_of_point_predictions_is_flagged_not_refused(
    kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = golden_catalog("regression_point")
    spec = _compiled("regression_point", cat)
    y_hat = next(o for o in cat.outputs if o.name == "y_hat")
    spec["panels"] = [_kpi("y_hat", "sum", y_hat.dims[:1]), *spec["panels"][:3]]
    body = await _validate_with(kit, monkeypatch, cat, spec)
    hit = [i for i in body["issues"] if i["pointer"] == "/panels/0/reduce"]
    assert hit and hit[0]["severity"] == "info" and "approximate" in hit[0]["message"]
    assert not [i for i in hit if i["severity"] == "error"]


@pytest.mark.integration
async def test_batch_on_fixed_rows_is_refused(
    kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = golden_catalog("regression_point", row_dim="store", row_policy=DimPolicy.FIXED)
    spec = _compiled("regression_point", cat)
    spec["batch"] = {"enabled": True, "outputs": ["y_hat"], "id_column": "store"}
    body = await _validate_with(kit, monkeypatch, cat, spec)
    assert body["ok"] is False
    hit = [i for i in body["issues"] if i["pointer"] == "/batch/enabled"]
    assert hit and hit[0]["code"] == "CAPABILITY_UNSUPPORTED"


@pytest.mark.integration
async def test_the_forecast_preset_needs_a_forecast_mode(
    kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = golden_catalog("forecast_bsts")
    spec = _compiled("forecast_bsts", cat)
    no_forecast = cat.model_copy(update={"score_modes": ["predict"]})
    body = await _validate_with(kit, monkeypatch, no_forecast, spec)
    assert body["ok"] is False
    hit = [i for i in body["issues"] if i["pointer"] == "/preset"]
    assert hit and hit[0]["code"] == "CAPABILITY_UNSUPPORTED"


@pytest.mark.integration
async def test_scenario_ranges_outside_the_declared_bounds_are_refused(
    kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = golden_catalog("regression_point")
    price = next(i for i in cat.inputs if i.var == "price")
    bounded = [
        i.model_copy(update={"bound_min": 0.0, "bound_max": 50.0}) if i is price else i
        for i in cat.inputs
    ]
    cat = cat.model_copy(update={"inputs": bounded, "row_policy": None})
    spec = _compiled("regression_point", cat)
    spec["scenario_rows"] = {
        "enabled": True,
        "outputs": ["y_hat"],
        "inputs": [
            {"name": "price", "label": "Price", "bind": {"var": "price"}, "type": "number",
             "min": 0, "max": 80, "step": 1, "default": 10},
        ],
    }  # fmt: skip
    body = await _validate_with(kit, monkeypatch, cat, spec)
    assert body["ok"] is False
    hit = [i for i in body["issues"] if i["pointer"] == "/scenario_rows/inputs/0/min"]
    assert hit and "bounds" in hit[0]["message"]
    # inside the bounds: no issue on the input
    spec["scenario_rows"]["inputs"][0]["max"] = 40
    monkeypatch.undo()
    body = await _validate_with(kit, monkeypatch, cat, spec, name="chk2")
    assert not [i for i in body["issues"] if i["pointer"].startswith("/scenario_rows/inputs/0")]


@pytest.mark.integration
async def test_a_version_that_ran_uploaded_code_is_refused(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    from mapi_server.services.dashboards.sources import check_version_v2

    mv = await _mv(ctx, kit["model"]["mv_id"])
    mv.lineage = {**(mv.lineage or {}), "overlay_hash": "sha256:" + "0" * 64}
    async with ctx.db.session() as s:
        cat = await sources.source_catalog(ctx, s, mv)
        src = DashboardSource(kind="model_version", model_id="trend")
        spec = compile_preset("forecast", cat, title="T", source=src)
        issues = await check_version_v2(ctx, s, spec, mv)
    assert issues and issues[0].code.value == "CAPABILITY_UNSUPPORTED"
    assert issues[0].pointer == "/source"
