"""Plan §6.2 end to end across packages: the xgboost store-demand spec as written in
``mapi_spec.examples``, resolved against the entry-point registry (``core.*`` steps from
mapi_steps, ``sklearn.*`` models from mapi_sklearn, ``core.*``/``ml.*`` plots from
mapi_plots), fitted with ``evaluate_then_refit``, scored for a forecast with a new store and
rendered with the plots the spec lists."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sklearn.testing import demand_dataset
from mapi_spec import DataSchema, ModelSpec, load_model_spec, load_score_request, validate_spec
from mapi_spec.examples import XGB_RAW, XGB_YAML

pytestmark = pytest.mark.integration

WEEKS = 60
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/store-demand-xgb\n"


def _spec() -> ModelSpec:
    """The plan's spec with a smaller forest so the test stays fast.

    ``core.impute`` defaults to ``unseen_levels: error``; scoring a new store therefore needs
    an explicit policy (``pooled`` carries the mean last price, only used before the new
    store's first supplied price).
    """
    text = XGB_YAML
    for old, new in (
        (
            "params: {n_estimators: 600, max_depth: 6, learning_rate: 0.05}",
            "params: {n_estimators: 60, max_depth: 3, learning_rate: 0.2}",
        ),
        ("method: ffill,", "method: ffill, unseen_levels: pooled,"),
    ):
        assert old in text
        text = text.replace(old, new)
    return load_model_spec(text)


def _data(weeks: int = WEEKS, seed: int = 0) -> xr.Dataset:
    """``units_sold``/``price``/``promo_flag`` over ``(store_id, sku, week)`` with price gaps."""
    ds = demand_dataset(weeks=weeks, seed=seed).rename({"store": "store_id"})
    price = ds["price"].values.copy()
    price[0, 0, 10:13] = np.nan  # a mid-series gap, forward filled
    price[1, 2, WEEKS - 2 :] = np.nan  # a trailing gap carried into the forecast
    return ds.assign(price=(ds["price"].dims, price, ds["price"].attrs))


def _future(weeks: int = 4, *, new_store: bool = True) -> xr.Dataset:
    """Prices and promotions for the weeks after the fitted range (no sales)."""
    nxt = _data(WEEKS + weeks).isel(week=slice(WEEKS, None)).drop_vars("units_sold")
    if new_store:
        extra = nxt.sel(store_id=["s0"]).assign_coords(store_id=["s_new"])
        nxt = xr.concat([nxt, extra], dim="store_id")
    return nxt


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="module")
def raw_schema() -> DataSchema:
    return DataSchema.model_validate(XGB_RAW)


@pytest.fixture(scope="module")
def bundle(registry: Registry, raw_schema: DataSchema) -> Bundle:
    return fit_spec(_spec(), _data(), registry=registry, raw_schema=raw_schema)


def test_entry_point_registry_resolves_every_reference_in_the_plan_spec(
    registry: Registry, raw_schema: DataSchema
) -> None:
    res = validate_spec(_spec(), registry.catalog(), raw_schema)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    rs = res.raise_for_errors()
    assert [s.resolved for s in rs.steps] == [
        "core.impute@1.0.0",
        "core.lag@1.0.0",
        "core.calendar@1.0.0",
        "core.log1p@1.0.0",
    ]
    assert rs.model.resolved == "sklearn.tabular_quantiles@1.0.0"
    plots = {p.id for p in registry.catalog().plots}
    assert {"core.fit_vs_actual", "core.residuals_over_time", "ml.feature_importance"} <= plots


def test_fit_evaluates_the_holdout_then_refits(bundle: Bundle) -> None:
    assert bundle.evaluation is not None
    assert {"rmse", "mape", "coverage_80"} <= set(bundle.metrics.values)
    assert bundle.metrics.n_holdout and bundle.metrics.n_holdout > 0
    # 80% intervals from the quantile grid cover a sensible share of held-out weeks
    assert 0.4 <= bundle.metrics.values["coverage_80"] <= 1.0
    assert bundle.states["plags"].context is not None  # promo tails for the forecast


def test_forecast_with_a_new_store_is_pooled_and_ordered(
    bundle: Bundle, registry: Registry
) -> None:
    request = load_score_request(
        REQ + "mode: forecast\nhorizon: {dim: time, periods: 4}\n"
        "output: {summary: [median, q10, q90]}\n"
    )
    out = score_bundle(bundle, request, _future(), registry=registry, return_full=True)
    pred = out["predictions"].to_dataset()["y_hat"]
    assert set(pred.dims) == {"quantile", "store_id", "sku", "week"}
    assert pred.sizes["week"] == 4 and "s_new" in pred["store_id"].values
    assert bool(np.isfinite(pred).all())
    q = pred.transpose("quantile", ...).values
    assert (np.diff(q, axis=0) >= -1e-9).all()  # rearranged quantiles never cross
    assert pred.attrs["output_space"] in {"time", "raw"}  # log1p inverted to units sold
    summary = out["summary"].to_dataset()["y_hat"]
    assert list(summary["stat"].values) == ["median", "q10", "q90"]
    issues = json.loads(out.attrs["issues"])
    assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)


def test_spec_plots_render_from_real_outputs(bundle: Bundle, registry: Registry) -> None:
    assert bundle.evaluation is not None
    holdout = _data().isel(week=slice(-12, None))
    request = load_score_request(
        REQ + "mode: evaluate\ndata: dataset://retail/store_sku_weekly_holdout@v1\n"
    )
    out = score_bundle(
        bundle.evaluation, request, holdout, registry=registry, metrics=["rmse"], return_full=True
    )
    spec = _spec()
    ids = [str(p) for p in spec.outputs.plots]
    on_scores = render_plots(out, [i for i in ids if i.startswith("core.")], registry=registry)
    assert not [i for i in on_scores.issues if i.severity == "error"], on_scores.issues
    assert {p.id for p in on_scores.plots} == {"core.fit_vs_actual", "core.residuals_over_time"}
    on_bundle = render_plots(bundle.to_tree(), ["ml.feature_importance"], registry=registry)
    assert [p.id for p in on_bundle.plots] == ["ml.feature_importance"], on_bundle.issues
    caption: Any = on_bundle.plots[0].caption_plain
    assert "relies most on" in caption
