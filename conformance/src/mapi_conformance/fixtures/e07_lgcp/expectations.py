"""E07 checks: the future is inside the model, maps normalise to the total, forecasts follow."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.lgcp import LGCPGridModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {"m_row": 5, "m_col": 5}


def registry(base: Registry) -> Registry:
    """Register the plugin directly (until it is listed in ``mapi_plugin_science.PLUGINS``)."""
    base.register_model(LGCPGridModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    n_fit = data.sizes["quarter"]
    max_ext = bundle.model.learned["max_extension"]
    assert isinstance(max_ext, int)
    assert post["step"].sizes["step_period"] == n_fit + max_ext, "steps cover the horizon"
    assert bundle.model.learned["anchor_era"] in range(-(-n_fit // 4))
    np.testing.assert_allclose(post["rate"].sum(("row", "col")), post["total"], rtol=1e-8)
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    assert bundle.fit_coords["raw"]["period"].values.max() == data["quarter"].values.max()


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "forecast_8":
        summary = tree["summary"].to_dataset()
        assert summary["quarter"].values.min() > data["quarter"].values.max()
        assert summary["rate"].attrs["units"] == "events"
        total = summary["total"].sel(stat="mean")
        last_year = data["quakes"].isel(quarter=slice(-4, None)).sum(("row", "col"))
        ratio = float(total.isel(quarter=0)) / max(float(last_year.mean()), 1.0)
        assert 0.2 < ratio < 5.0, f"first forecast quarter is {ratio:.2f}x the last year"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["coverage_90"] > 0.6, f"holdout coverage {values['coverage_90']:.2f}"
