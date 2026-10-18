"""E31 checks: the features span the true strokes, held-out halves are completed and the
pointwise log-likelihood is kept for elpd_loo."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.ibp import IBPFeaturesModel
from mapi_plugin_science.ibp_data import IBP_TRUTH, stroke_features
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {"n_features": 4, "score_draws": 100}


def registry(base: Registry) -> Registry:
    """Register the plugin until ``mapi_plugin_science.PLUGINS`` lists it."""
    base.register_model(IBPFeaturesModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    post = groups["posterior"]
    truth = stroke_features().reshape(3, -1)
    for c in range(post.sizes["chain"]):
        a = post["features"].isel(chain=c, draw=-1).values
        a = a.reshape(a.shape[0], -1)
        coef, *_ = np.linalg.lstsq(a.T, truth.T, rcond=None)
        r2 = 1 - ((a.T @ coef - truth.T) ** 2).sum(0) / (truth**2).sum(1)
        assert (r2 > 0.8).all(), f"chain {c}: the features do not span the strokes {r2.round(2)}"
    ll = groups["log_likelihood"]["ink"]
    assert ll.dims == ("chain", "draw", "digit") and ll.sizes["digit"] == data.sizes["digit"]
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    assert bundle.model.learned["start"] == "digits"
    logpi = post["logpi"].isel(feature=0).transpose("chain", "draw").values
    pi_first = np.exp(logpi).mean(axis=1)
    assert (pi_first > 0.1).all(), f"a chain switched the features off: {pi_first}"


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_two_digits":
        mean = summary["ink_hat"].sel(stat="mean")
        err = float(np.sqrt(((mean - data["pixels"].isel(digit=[0, 1])) ** 2).mean()))
        assert err < 2.0 * IBP_TRUTH["noise_sd"], f"reconstruction error {err:.3f}"
        owns = summary["owns"].sel(stat="mean")
        assert float(owns.min()) >= 0.0 and float(owns.max()) <= 1.0
        assert summary["ink_hat"].attrs["units"] == "ink"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["rmse"] < 2.0 * IBP_TRUTH["noise_sd"], f"held-out RMSE {values['rmse']:.3f}"
