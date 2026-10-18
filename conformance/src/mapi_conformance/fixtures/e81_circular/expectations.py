"""E81 checks: the coefficients of the simulated winds are recovered, the pointwise log
densities support LOO, the frame densities integrate to one and simulated angles stay on
the circle."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.circular import TARGET, THETA, CircularRegressionModel
from mapi_plugin_science.circular_data import WIND_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {
    "daily_harmonics": 1,
    "annual_harmonics": 1,
    "interaction_harmonics": 0,
    "frames": 4,
    "score_draws": 100,
}


def registry(base: Registry) -> Registry:
    """Register the plugin until ``mapi_plugin_science.PLUGINS`` lists it."""
    base.register_model(CircularRegressionModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    n = data.sizes["report"]
    assert groups["log_likelihood"][TARGET].sizes["obs"] == n
    rep = groups["posterior_predictive"][THETA]
    assert float(rep.min()) >= 0.0 and float(rep.max()) < 2 * np.pi
    elpd = bundle.metrics.elpd["elpd_loo"]
    assert elpd.n == n and elpd.elpd < 0
    post = groups["posterior"]["B"]
    for feat, truth in WIND_TRUTH.items():
        got = post.sel(feature=feat).mean(("chain", "draw")).values
        assert np.abs(got - np.asarray(truth)).max() < 0.35, (feat, got, truth)


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, ctx: Any) -> None:
    summary = tree["summary"].to_dataset()
    assert summary["direction_hat"].attrs["units"] == "deg"
    if name == "predict_tail":
        dens = summary["density"].sel(stat="mean")
        step = float(np.diff(summary["angle"].values)[0])
        np.testing.assert_allclose(dens.sum("angle").values * step, 1.0, atol=1e-6)
        r = summary["resultant_length"].sel(stat="mean")
        assert float(r.min()) >= 0.0 and float(r.max()) <= 1.0
    if name == "simulate_next_day":
        assert "next_day" in ctx.results, "the expected next-day directions ran first"
        sim = summary["direction_hat"].sel(stat="mean")
        assert float(sim.min()) >= 0.0 and float(sim.max()) < 360.0


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    # angles have no linear holdout metric (an rmse would ignore the wrap): LOO is fit-time
    json.loads(tree.attrs["metrics"])
    assert bundle.metrics.values["elpd_loo"] is not None
