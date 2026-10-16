"""sm_arimax checks: per-geo parameters recover the generating effects, no pickle, ordered
quantiles, the promotion what-if moves the forecast by the estimated effect."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_statsmodels.datasets import SALES_TRUTH
from mapi_sdk.bundle import Bundle

from mapi_conformance.fixtures.sm_arimax.data import payload_future
from mapi_conformance.runner import FixtureContext


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    assert bundle.model.native == {}, "statsmodels fits are stored as parameters, not pickles"
    params = bundle.model.groups["params"]
    assert params["estimate"].dims == ("geo", "param")
    price = params["estimate"].sel(param="price").values
    promo = params["estimate"].sel(param="promo").values
    assert np.allclose(price, SALES_TRUTH["price"], atol=2.0), price
    assert np.allclose(promo, SALES_TRUTH["promo"], atol=2.5), promo
    ar = params["estimate"].sel(param="ar.L1").values
    assert ((ar > 0.2) & (ar < 0.95)).all(), ar
    assert bundle.evaluation is not None
    assert bundle.fit_coords["raw"]["t"].values.max() == data["date"].values.max()
    cells = bundle.model.learned["cells"]
    assert isinstance(cells, list) and len(cells) == data.sizes["geo"]


def check_score(
    name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, ctx: FixtureContext
) -> None:
    summary = tree["summary"].to_dataset()["y_hat"]
    if name == "forecast_8":
        lo, mid, hi = (summary.sel(stat=s) for s in ("q5", "median", "q95"))
        assert bool((lo <= mid).all()) and bool((mid <= hi).all()), "quantiles cross"
        assert summary["date"].values.min() > data["date"].values.max()
        issues = json.loads(tree.attrs["issues"])
        assert not [i for i in issues if i["severity"] == "error"]
    if name == "promo_every_week":
        base = ctx.results["forecast_8"]["summary"].to_dataset()["y_hat"].sel(stat="median")
        moved = summary.sel(stat="median")
        effect = bundle.model.groups["params"]["estimate"].sel(param="promo")
        planned = payload_future(data, ctx.seed, ctx.size)["promo"]
        want = effect * (1 - planned)
        np.testing.assert_allclose(
            (moved - base).transpose(*want.dims).values, want.values, atol=1e-6
        )


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["mape"] < 25.0, f"holdout MAPE {values['mape']:.1f}% is implausibly high"
    assert values["coverage_90"] >= 0.6, values
