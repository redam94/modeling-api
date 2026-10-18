"""E23 checks: the pointwise LOO density covers the observed species, the slopes are
recovered, and missing traits are predicted from relatives."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.phylo import PhyloRegressionModel
from mapi_plugin_science.phylo_data import PHYLO_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}


def registry(base: Registry) -> Registry:
    """Register the plugin (``mapi_plugin_science.PLUGINS`` lists it too)."""
    base.register_model(PhyloRegressionModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    ll = groups["log_likelihood"]["trait"]
    n_obs = int(data["brain"].notnull().sum())
    assert ll.sizes["species"] == n_obs, "one LOO term per species with a brain value"
    assert bool(np.isfinite(ll).all())
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    slope = groups["posterior"]["slope"]
    for name, value in PHYLO_TRUTH["slope"].items():
        draws = slope.sel(predictor=name).values.ravel()
        lo, hi = np.quantile(draws, [0.005, 0.995])
        assert lo < value < hi, f"slope {name}: truth {value} outside [{lo:.3f}, {hi:.3f}]"
    hat = groups["posterior"]["trait_hat"]
    assert hat.sizes["species"] == data.sizes["species"]
    assert bool(np.isfinite(hat).all())


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "predict_missing_from_relatives":
        summary = tree["summary"].to_dataset()["brain"]
        missing = data["species"].values[data["brain"].isnull().values]
        np.testing.assert_array_equal(np.sort(summary["species"].values), np.sort(missing))
        mean = summary.sel(stat="mean")
        assert bool(np.isfinite(mean).all()) and float(mean.min()) > 0
        assert bool((summary.sel(stat="q5") < summary.sel(stat="q95")).all())


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["coverage_90"] >= 0.5, f"holdout coverage {values['coverage_90']:.2f}"
