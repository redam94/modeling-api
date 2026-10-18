"""E61 checks: the kinetics are recovered, the last image is predicted from the first three
by linear theory, P(Turing) is high and the simulated patterns settle."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.turing import TuringPatternModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

from mapi_conformance.fixtures.e61_turing.data import truth

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {"field_draws": 8, "simulate_draws": 2, "settle_time": 60}


def registry(base: Registry) -> Registry:
    """Register the plugin until ``mapi_plugin_science.PLUGINS`` is loaded by default."""
    base.register_model(TuringPatternModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    size = "tiny" if data.sizes["y"] == 32 else "full"
    t = truth(int(data.attrs.get("seed", 0)), size)
    for name in ("d", "g", "b"):
        lo, hi = np.quantile(post[name].values, [0.01, 0.99])
        assert lo < t[name] < hi, f"{name}: [{lo:.3g}, {hi:.3g}] vs {t[name]:.3g}"
    assert float(post["p_turing"].mean()) > 0.9
    ll = bundle.model.groups["log_likelihood"]["activator"]
    assert ll.sizes["mode"] == bundle.model.learned["n_modes"]
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, ctx: Any) -> None:
    if name == "predict_fit_images":
        summary = tree["summary"].to_dataset()
        mean = summary["pattern"].sel(stat="mean")
        err = float(np.sqrt(((mean - data["activator"]) ** 2).mean()))
        sigma = float(bundle.model.posterior["sigma"].mean())
        assert err < 2 * sigma, f"in-sample rmse {err:.2e} vs camera noise {sigma:.2e}"
        assert float(summary["p_turing"].sel(stat="mean")) > 0.9
    if name == "simulate_new_patterns":
        node = tree["draws"]
        assert isinstance(node, xr.DataTree)
        settled = node.to_dataset()["settled"].astype(float)
        early = float(data["activator"].isel(t=-1).std())
        assert float(settled.std(("y", "x")).min()) > 5 * early, "the patterns did not grow"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    sigma = float(bundle.model.posterior["sigma"].mean())
    assert values["rmse"] < 3 * sigma, f"holdout rmse {values['rmse']:.2e}"
