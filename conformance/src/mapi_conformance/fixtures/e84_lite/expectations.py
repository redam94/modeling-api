"""E84-lite checks: recovered price and promotion effects, more uncertainty for a new
household, and a price rise that moves shares away from the product."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_tabular.datasets import CHOICE_TRUTH
from mapi_sdk.bundle import Bundle

TINY_FIT = {"draws": 20}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    price = float(post["beta"].sel(covariate="price").mean())
    assert abs(price - CHOICE_TRUTH["price"]) < 0.8, f"price effect {price:.2f}"
    assert float(post["beta"].sel(covariate="promo").mean()) > 0
    assert post["u"].sizes["hh"] == data.sizes["household"]


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "new_household_from_spread":
        draws = tree["draws"].to_dataset()["y_hat"]
        sd = draws.std("sample")
        assert float(sd.sel(household="h_new").mean()) > float(sd.isel(household=0).mean())
        assert np.isfinite(tree["summary"].to_dataset()["y_hat"]).all()
    elif name == "price_rise_counterfactual":
        delta = tree["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
        assert float(delta.sel(product="cola").mean()) < 0
    elif name == "next_trips":
        total = tree["summary"].to_dataset()["y_hat"].sel(stat="mean").sum("product")
        np.testing.assert_allclose(total.values, 1.0, rtol=1e-6)
