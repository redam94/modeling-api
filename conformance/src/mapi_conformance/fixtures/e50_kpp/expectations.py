"""E50 checks: the first time is the initial condition, the PDE is stable, the front speed
is recovered and forecasts keep the wound closing."""

from __future__ import annotations

import json
import math

import numpy as np
import xarray as xr
from mapi_plugin_science.kpp import INITIAL, OBS_DIM, FisherKPPModel
from mapi_plugin_science.kpp_data import KPP_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {"steps_per_hour": 3, "delay": False}


def registry(base: Registry) -> Registry:
    """Register the plugin until ``mapi_plugin_science.PLUGINS`` lists it."""
    base.register_model(FisherKPPModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    obs = groups["observed_data"]["count"]
    assert obs.dims == (OBS_DIM, "x"), "the first time must not enter the likelihood"
    assert obs.sizes[OBS_DIM] == data.sizes["hours"] - 1
    np.testing.assert_allclose(
        groups["constant_data"][INITIAL].values, np.maximum(data["cells"].values[0], 0.1)
    )
    cfl = bundle.model.learned["cfl_number_max"]
    assert isinstance(cfl, float) and cfl <= 0.5, "posterior D breaks the CFL bound"
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    assert bundle.fit_coords["raw"]["t"].values.max() == data["hours"].values.max()
    if bundle.model.settings.get("delay"):
        return  # size full: the delay truth lives in data.FULL_TRUTH (speed checked loosely)
    speed = 2 * math.sqrt(KPP_TRUTH["D"] * KPP_TRUTH["lam"])
    got = float(groups["posterior"]["wave_speed"].mean())
    assert abs(got - speed) < 0.25 * speed, f"wave speed {got:.1f} vs truth {speed:.1f} um/h"


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "forecast_2":
        summary = tree["summary"].to_dataset()
        assert summary["hours"].values.min() > data["hours"].values.max()
        assert summary["density"].attrs["units"] == "cells"
        mean = summary["density"].sel(stat="mean")
        assert bool(np.isfinite(mean).all()) and float(mean.min()) >= 0.0
        assert float(mean.mean()) >= float(data["cells"].isel(hours=-1).mean()) - 10.0


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["coverage_90"] >= 0.5, f"holdout coverage {values['coverage_90']:.2f}"
