"""E60 checks: hyperparameters, field draws against the simulated truth, in-sample fit."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.traction import TractionForceModel
from mapi_plugin_science.traction_data import TRACTION_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

from mapi_conformance.fixtures.e60_traction.data import truth

TINY_FIT = {"tune": 300, "draws": 200, "chains": 2}
TINY_SETTINGS = {"field_draws": 40}


def registry(base: Registry) -> Registry:
    """``sci.traction_force`` until the science plugin pack registers it."""
    base.register_model(TractionForceModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    noise = float(post["noise_sd"].mean())
    assert abs(noise / TRACTION_TRUTH["noise_sd"] - 1) < 0.15, f"noise sd {noise:.4f} um"
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert bundle.model.groups["observed_data"]["displacement"].shape == (
        2,
        data.sizes["row"],
        data.sizes["col"],
    )


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, ctx: Any) -> None:
    summary = tree["summary"].to_dataset()
    assert summary["traction"].attrs["units"] == "Pa"
    if name != "predict_field":
        return
    true = truth(ctx.seed, ctx.size)["traction"]
    mean = summary["traction"].sel(stat="mean")
    r = float(np.corrcoef(mean.values.ravel(), true.values.ravel())[0, 1])
    assert r > 0.8, f"posterior mean vs true traction: correlation {r:.2f}"
    lo, hi = summary["traction"].sel(stat="q5"), summary["traction"].sel(stat="q95")
    cover = float(((true >= lo) & (true <= hi)).mean())
    assert 0.75 <= cover <= 0.98, f"90% pixelwise coverage {cover:.2f}"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    rmse = values["rmse"]
    assert rmse < 1.5 * TRACTION_TRUTH["noise_sd"], f"in-sample RMSE {rmse:.4f} um"
