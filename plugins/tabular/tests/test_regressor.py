"""``tabular.regressor`` on the plan §6.2 store-demand panel."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_plugin_tabular import TabularRegressor, TabularRegressorSettings
from mapi_plugin_tabular.datasets import (
    STORE_DEMAND_SCHEMA,
    STORE_DEMAND_SPEC_YAML,
    store_demand_dataset,
)
from mapi_plugin_tabular.regressor import QUANTILE_ESTIMATORS
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sklearn.estimators import ESTIMATORS
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/store-demand\n"


def _req(body: str) -> Any:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="module")
def data() -> xr.Dataset:
    return store_demand_dataset(weeks=60, seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, data: xr.Dataset) -> Bundle:
    return fit_spec(STORE_DEMAND_SPEC_YAML, data, registry=registry, raw_schema=STORE_DEMAND_SCHEMA)


def _future(weeks: int = 4) -> xr.Dataset:
    nxt = store_demand_dataset(weeks=60 + weeks).isel(week=slice(60, None)).drop_vars("units_sold")
    extra = nxt.sel(store_id=["s0"]).assign_coords(store_id=["s_new"])
    return xr.concat([nxt, extra], dim="store_id")


def test_settings_expose_exactly_the_quantile_capable_registry() -> None:
    capable = {
        k for k, e in ESTIMATORS.items() if e.quantile is not None and e.task == "regression"
    }
    assert set(QUANTILE_ESTIMATORS) == capable
    schema = TabularRegressor.manifest().settings_schema
    kinds = set()
    for ref in schema["$defs"].values():
        kind = ref.get("properties", {}).get("kind", {})
        if "const" in kind:
            kinds.add(kind["const"])
        kinds.update(kind.get("enum", []))
    assert capable <= kinds
    assert "sklearn.Ridge" not in kinds
    assert set(TabularRegressor.estimators()) == capable
    assert TabularRegressor.estimators()["xgboost.XGBRegressor"]["quantile"] == "native_multi"


def test_defaults() -> None:
    s = TabularRegressorSettings(rows=["store", "sku", "time"])
    assert s.uncertainty.quantiles == [0.1, 0.5, 0.9]
    assert s.estimator.kind == "xgboost.XGBRegressor"
    man = TabularRegressor.manifest()
    assert man.capabilities.representation.value == "quantiles"
    assert man.plots == ["core.fit_vs_actual", "core.residuals_over_time", "ml.feature_importance"]


def test_non_quantile_estimators_are_rejected_statically(registry: Registry) -> None:
    spec = load_model_spec(
        STORE_DEMAND_SPEC_YAML.replace("kind: xgboost.XGBRegressor", "kind: sklearn.Ridge").replace(
            "params: {n_estimators: 80, max_depth: 3, learning_rate: 0.15}", "params: {}"
        )
    )
    res = validate_spec(spec, registry.catalog(), STORE_DEMAND_SCHEMA)
    assert any(i.code is ErrorCode.SETTINGS_INVALID for i in res.errors), res.issues


def test_fit_evaluates_then_refits(bundle: Bundle) -> None:
    assert bundle.evaluation is not None
    assert {"rmse", "mape", "coverage_80"} <= set(bundle.metrics.values)
    assert 0.5 <= bundle.metrics.values["coverage_80"] <= 1.0
    assert bundle.model.native  # xgboost JSON, never pickle


def test_forecast_new_store_quantiles_are_ordered(bundle: Bundle, registry: Registry) -> None:
    out = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: time, periods: 4}\n"),
        _future(),
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()["y_hat"]
    assert set(pred.dims) == {"quantile", "store_id", "sku", "week"}
    assert "s_new" in pred["store_id"].values and bool(np.isfinite(pred).all())
    assert (np.diff(pred.transpose("quantile", ...).values, axis=0) >= -1e-9).all()
    assert "UNSEEN_LEVELS" in out.attrs["issues"]


def test_quantiles_cannot_be_aggregated(bundle: Bundle, registry: Registry) -> None:
    with pytest.raises(MapiError) as err:
        score_bundle(
            bundle,
            _req(
                "mode: forecast\nhorizon: {dim: time, periods: 2}\n"
                "output: {aggregate: {over: [store_id]}}\n"
            ),
            _future(2),
            registry=registry,
        )
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_manifest_plots_render(bundle: Bundle, data: xr.Dataset, registry: Registry) -> None:
    assert bundle.evaluation is not None
    holdout = data.isel(week=slice(-8, None))
    out = score_bundle(
        bundle.evaluation,
        _req("mode: evaluate\ndata: dataset://retail/holdout@v1\n"),
        holdout,
        registry=registry,
        metrics=["rmse"],
        return_full=True,
    )
    scores = render_plots(
        out, ["core.fit_vs_actual", "core.residuals_over_time"], registry=registry
    )
    assert {p.id for p in scores.plots} == {"core.fit_vs_actual", "core.residuals_over_time"}, (
        scores.issues
    )
    importance = render_plots(bundle.to_tree(), ["ml.feature_importance"], registry=registry)
    assert [p.id for p in importance.plots] == ["ml.feature_importance"], importance.issues


@pytest.mark.mapi_contract
def test_tabular_regressor_passes_the_contract_kit(registry: Registry, data: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=STORE_DEMAND_SPEC_YAML, data=data, raw_schema=STORE_DEMAND_SCHEMA, registry=registry
    )
    report = check_model(TabularRegressor, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "extend_typed_errors",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "representation_aggregation",
        "seed_determinism",
        "outputs_match_manifest",
        "output_space_and_units",
    } <= ran
    # the static manifest describes y_hat over the quantile and the row dims
    rows = ["store", "sku", "time"]
    y_hat = TabularRegressor.manifest().outputs["y_hat"]
    assert y_hat.dims == ["quantile"] and y_hat.dims_of == "y"
    schema = TabularRegressor.manifest().schema_template.resolve({"rows": rows})
    assert set(y_hat.full_dims(schema)) == {"quantile", *rows}
