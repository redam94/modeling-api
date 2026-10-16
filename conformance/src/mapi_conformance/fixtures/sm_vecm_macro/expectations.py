"""sm_vecm_macro checks: one cointegrating relation, the Johansen table, impulse responses
and the variance decomposition stored with the fit; forecasts continue the levels."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    assert bundle.model.native == {}
    coint = groups["cointegration"]
    assert int(coint["rank"]) == 1 and int(coint["johansen_rank"]) == 1
    # the trace statistic for r = 0 exceeds its 5% critical value; for r <= 1 it does not
    stat, crit = coint["statistic"].values, coint["critical_value"].values
    assert stat[0] > crit[0] and stat[1] < crit[1], (stat, crit)
    beta = coint["beta"].sel(relation="ec1").values
    assert beta[0] == 1.0, "beta is normalised on the first series"
    fevd = groups["fevd"]["share"]
    np.testing.assert_allclose(fevd.sum("shock").values, 1.0, atol=1e-9)
    assert groups["irf"].sizes["horizon"] == 17
    assert bundle.evaluation is not None


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name != "forecast_8":
        return
    summary = tree["summary"].to_dataset()["y_hat"]
    lo, mid, hi = (summary.sel(stat=s) for s in ("q5", "median", "q95"))
    assert bool((lo < mid).all()) and bool((mid < hi).all())
    last = data["value"].isel(quarter=-1)
    first = mid.isel(quarter=0)
    assert bool((abs(first - last) < 0.1).all()), "the forecast should continue the log levels"
    width = (hi - lo).transpose("series", "quarter").values
    assert (np.diff(width, axis=1) > 0).all(), "the band widens with the horizon"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    # two years ahead in log units: about 2% off on average
    assert values["rmse"] < 0.05, f"holdout RMSE {values['rmse']:.3f} (log units) is too high"
