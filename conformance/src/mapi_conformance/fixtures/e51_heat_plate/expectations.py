"""E51 checks: the readings stay sparse on the grid, the plate is recovered, a new heater
layout is hotter next to its heaters and simulated readings carry the sensor noise."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.heat_plate import FIELD, OBS_DIM, HeatPlateModel, spread_length_cm
from mapi_plugin_science.heat_plate_data import HEAT_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}


def registry(base: Registry) -> Registry:
    """Register the plugin until ``mapi_plugin_science.PLUGINS`` lists it."""
    base.register_model(HeatPlateModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    obs = groups["observed_data"]["temperature"]
    assert obs.dims == ("run", "y", "x"), "observed_data keeps the readings on the grid"
    assert int(np.isfinite(obs).sum()) == int(np.isfinite(data["reading_c"]).sum())
    ll = groups["log_likelihood"]["temperature"]
    assert ll.dims == ("chain", "draw", OBS_DIM), "one pointwise value per reading"
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    post = groups["posterior"]
    for name in ("k", "h", "eta"):
        lo, hi = np.quantile(post[name].values, [0.01, 0.99])
        assert lo < HEAT_TRUTH[name] < hi, f"{name}: [{lo:.3g}, {hi:.3g}]"
    spread = float(spread_length_cm(HEAT_TRUTH["k"], HEAT_TRUTH["h"], 0.005))
    got = float(post["spread_length"].mean())
    assert abs(got - spread) < 0.05 * spread, f"spread length {got:.2f} vs {spread:.2f} cm"
    assert bundle.model.learned["posterior_correlations"]["k-eta"] > 0.5


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    mean = summary[FIELD].sel(stat="mean")
    assert summary[FIELD].attrs["units"] == "degC"
    assert bool(np.isfinite(mean).all()) and float(mean.min()) >= 19.0
    if name == "new_layout":
        cells = mean.isel(run=0)
        nx = cells.sizes["x_cm"]
        right = float(cells.isel(x_cm=slice(nx - 3, None)).mean())
        left = float(cells.isel(x_cm=slice(0, 3)).mean())
        assert right > left > 20.0, f"right end {right:.1f}, left end {left:.1f} degC"
    if name == "simulate_layout":
        sd = float(summary[FIELD].sel(stat="sd").mean())
        sigma = float(bundle.model.posterior["sigma"].mean())
        assert sd >= 0.8 * sigma, f"simulated readings sd {sd:.3f} < sensor noise {sigma:.3f}"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    # the held-out run: a heater the fit never saw, read by the same thermocouples
    assert values["rmse"] < 3 * HEAT_TRUTH["sigma"], f"holdout RMSE {values['rmse']:.3f}"
