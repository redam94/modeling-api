"""E13 checks: the price effect, the terminal state and the updated state after filtering."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_ts.bsts_data import BSTS_TRUTH
from mapi_sdk.bundle import Bundle

TINY_FIT = {"tune": 100, "draws": 100, "chains": 2}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    term = bundle.model.terminal_state
    assert term is not None, "ts.bsts must write a terminal_state group"
    assert {"chain", "draw", "state", "state_aux", "regressor"} <= set(term.dims)
    assert int(term.attrs["n_fit"]) == data.sizes["date"]
    assert np.datetime64(term.attrs["last_label"]) == data["date"].values.max()
    assert {"one_step", "components"} <= set(bundle.model.groups)
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason
    sc = bundle.model.learned["scaling"]
    assert isinstance(sc, dict)
    y_scale, x_scale = sc["y_scale"], sc["x_scale"]
    assert isinstance(y_scale, int | float) and isinstance(x_scale, list)
    x0 = x_scale[0]
    assert isinstance(x0, int | float)
    beta = bundle.model.posterior["beta_reg"].sel(state_reg="price")
    effect = float(beta.mean()) * y_scale / x0
    assert abs(effect - BSTS_TRUTH["price"]) < 15.0, f"price effect {effect:.1f}"
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "forecast_12":
        assert summary["date"].values.min() > data["date"].values.max()
        assert summary["y_hat"].attrs["units"] == "units"
        mean = summary["y_hat"].sel(stat="mean")
        last_year = data["sales"].isel(date=slice(-12, None))
        assert abs(float(mean.mean()) - float(last_year.mean())) < 200.0
    if name == "filter_new_months":
        term = tree["terminal_state"].to_dataset()
        assert int(term.attrs["n_fit"]) == data.sizes["date"] + 3
        assert np.datetime64(term.attrs["previous_label"]) == data["date"].values.max()


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    import json

    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["mape"] < 10.0, f"holdout MAPE {values['mape']:.2f}% is implausibly high"
