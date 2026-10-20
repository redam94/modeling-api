"""Geo-lift checks: the impact and donor_weights groups, a positive lift, NaN donor rows."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

TINY_FIT = {"tune": 100, "draws": 100, "chains": 2}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    assert {"impact", "donor_weights"} <= set(groups), "the fit must store its effect groups"
    imp = groups["impact"]
    assert imp["counterfactual"].dims == ("sample", "unit", "time")
    assert imp["unit"].values.tolist() == ["geo_03"]
    assert int(imp["post"].sum()) == 12
    total = imp["effect"].where(imp["post"]).sum(("unit", "time")).values
    assert np.quantile(total, 0.05) > 0, "the simulated lift is positive"
    weights = groups["donor_weights"]
    np.testing.assert_allclose(weights["weight"].sum("donor"), 1.0, rtol=1e-6)
    assert weights.sizes["donor"] == data.sizes["geo"] - 1
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    y_hat = tree["summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert bool(np.isfinite(y_hat.sel(geo="geo_03")).all())
    assert bool(np.isnan(y_hat.drop_sel(geo="geo_03")).all()), "donor rows must be NaN"
    if name == "predict_in_sample":
        pre = data["sales"].sel(geo="geo_03").isel(week=slice(0, 60))
        rel = np.abs(y_hat.sel(geo="geo_03").isel(week=slice(0, 60)) / pre - 1)
        assert float(rel.mean()) < 0.05, "the synthetic copy does not follow the market"
