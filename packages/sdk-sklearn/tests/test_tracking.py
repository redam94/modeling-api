"""Run tracking: a tabular fit logs the estimator's scalar hyperparameters as params."""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.models import FitContext
from mapi_sdk.tracking import MetricRecorder
from mapi_sklearn import TabularRegressor
from mapi_sklearn.model import estimator_params
from mapi_spec.spec import FitConfig

SETTINGS = {
    "rows": ["store", "time"],
    "estimator": {"kind": "sklearn.Ridge", "params": {"alpha": 0.5}},
}


def _ds() -> xr.Dataset:
    rng = np.random.default_rng(0)
    time = pd.date_range("2024-01-07", periods=12, freq="W-SUN").as_unit("ns")
    x = rng.normal(size=(3, 12))
    return xr.Dataset(
        {
            "y": (("store", "time"), 2 * x + rng.normal(0, 0.1, (3, 12))),
            "x": (("store", "time"), x),
        },
        coords={"store": ["a", "b", "c"], "time": time},
    )


def test_fit_logs_estimator_params() -> None:
    plugin = TabularRegressor()
    rec = MetricRecorder()
    plugin.fit(_ds(), plugin.parse_settings(SETTINGS), FitConfig(seed=1), FitContext(tracker=rec))
    assert rec.params["estimator.alpha"] == 0.5
    assert rec.params["estimator.fit_intercept"] is True
    assert all(k.startswith("estimator.") for k in rec.params)
    assert all(v is not None for v in rec.params.values())


def test_fit_without_tracker_logs_nothing() -> None:
    plugin = TabularRegressor()
    ctx = FitContext()
    plugin.fit(_ds(), plugin.parse_settings(SETTINGS), FitConfig(seed=1), ctx)
    assert not ctx.tracking


def test_estimator_params_keeps_scalars_only() -> None:
    class Est:
        def get_params(self, deep: bool = True) -> dict[str, object]:
            return {
                "a": np.int64(3),
                "b": None,
                "c": [1, 2],
                "d": float("nan"),
                "e": "x" * 1000,
                "f": object(),
                "g": False,
            }

    out = estimator_params(Est())
    assert out == {"estimator.a": 3, "estimator.e": "x" * 500, "estimator.g": False}
    assert estimator_params(object()) == {}
