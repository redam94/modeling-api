"""Conformal store-demand checks: skops persistence, ordered intervals, pooled new stores."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

TINY_SETTINGS = {"estimator": {"params": {"max_iter": 60, "learning_rate": 0.15}}}
"""The tiny corpus run boosts fewer rounds (the spec's 300 are for the full run)."""


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    assert bundle.model.native, "the fitted estimator must be persisted natively"
    assert all(n.endswith(".skops") for n in bundle.model.native), list(bundle.model.native)
    assert bundle.model.manifest.capabilities.representation.value == "interval"
    assert bundle.evaluation is not None
    coverage = bundle.metrics.values["coverage_80"]
    assert coverage is not None and 0.4 <= coverage <= 1.0


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name != "forecast_with_new_store":
        return
    summary = tree["summary"].to_dataset()["y_hat"]
    stats = [str(s) for s in summary["stat"].values]
    point = summary.sel(stat="point")
    bounds = [s for s in stats if s != "point"]
    assert len(bounds) >= 2, f"no interval bounds in {stats}"
    lo, hi = (summary.sel(stat=s) for s in bounds[:2])
    assert bool((lo <= point + 1e-9).all()) and bool((point <= hi + 1e-9).all()), "bounds cross"
    assert "s_new" in summary["store_id"].values
    issues = json.loads(tree.attrs["issues"])
    assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)
    assert np.isfinite(summary.sel(store_id="s_new")).all()
