"""C08 checks: sentinels never reach the likelihood; forecasts continue the refit series."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_ts.datasets import CO2_SENTINEL
from mapi_sdk.bundle import Bundle

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2, "target_accept": None}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    n_missing = int((data["co2"] == CO2_SENTINEL).sum())
    obs = bundle.model.groups["observed_data"]["y"]
    assert obs.size == data.sizes["date"] - n_missing, "sentinel months reached the likelihood"
    assert bool(np.isfinite(obs).all())
    assert bundle.model.learned["max_extension"] == 24
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    # the production fit covers the holdout: forecasts start from the latest data
    assert bundle.fit_coords["raw"]["t"].values.max() == data["date"].values.max()


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "forecast_12":
        summary = tree["summary"].to_dataset()
        assert summary["date"].values.min() > data["date"].values.max()
        assert summary["y_hat"].attrs["units"] == "ppm"
        mean = summary["y_hat"].sel(stat="mean")
        last_year = data["co2"].where(data["co2"] > 0).isel(date=slice(-12, None))
        assert abs(float(mean.mean()) - float(last_year.mean())) < 10.0


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    import json

    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["mape"] < 2.0, f"holdout MAPE {values['mape']:.2f}% is implausibly high"
