"""Store-demand checks: safe persistence, ordered quantiles and pooled new stores."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

TINY_SETTINGS = {
    "estimator": {"params": {"n_estimators": 80, "max_depth": 3, "learning_rate": 0.15}}
}
"""The tiny corpus run uses a small forest (the spec's 600 trees are for the full run)."""


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    assert bundle.model.native, "the fitted estimator must be persisted natively"
    assert all(n.endswith((".xgb.json", ".skops")) for n in bundle.model.native)
    assert bundle.evaluation is not None
    coverage = bundle.metrics.values["coverage_80"]
    assert coverage is not None and 0.4 <= coverage <= 1.0


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name != "forecast_with_new_store":
        return
    summary = tree["summary"].to_dataset()["y_hat"]
    lo, mid, hi = (summary.sel(stat=s).values for s in ("q10", "median", "q90"))
    assert (lo <= mid + 1e-9).all() and (mid <= hi + 1e-9).all(), "quantiles cross"
    assert "s_new" in summary["store_id"].values
    issues = json.loads(tree.attrs["issues"])
    assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)
    assert np.isfinite(summary.sel(store_id="s_new")).all()
