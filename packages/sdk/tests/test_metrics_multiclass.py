"""v0.14 metrics: multiclass class probabilities (``meaning: class_probability``), numeric
labels, and the macro one-vs-rest AUC of categorical targets."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.metrics import (
    CATEGORICAL_METRICS,
    _label_codes,
    binary_auc,
    categorical_dim,
    compute_metrics,
)
from mapi_spec import Representation

R = Representation
CLASSES = ["0", "1", "2"]


def _probs(meaning: bool = True) -> xr.DataArray:
    p = np.array(
        [
            [0.7, 0.2, 0.1],
            [0.1, 0.8, 0.1],
            [0.2, 0.2, 0.6],
            [0.5, 0.3, 0.2],
            [0.3, 0.3, 0.4],
            [0.1, 0.6, 0.3],
        ]
    )
    da = xr.DataArray(p, dims=["row", "class"], coords={"row": np.arange(6), "class": CLASSES})
    if meaning:
        da.attrs["meaning"] = "class_probability"
    return da


def _obs(values: list[object]) -> xr.DataArray:
    return xr.DataArray(values, dims=["row"], coords={"row": np.arange(len(values))})


def test_numeric_labels_need_the_class_probability_meaning() -> None:
    obs = _obs([0, 1, 2, 0, 1, 1])
    assert categorical_dim(_probs(), obs) == "class"
    assert categorical_dim(_probs(meaning=False), obs) is None
    assert categorical_dim(_probs(), obs.astype(float)) == "class"
    assert categorical_dim(_probs(meaning=False), obs.astype(str)) == "class"


def test_integral_floats_match_text_categories() -> None:
    codes = _label_codes(np.array([0.0, 2.0, 1.5, np.nan]), CLASSES)
    assert codes.tolist() == [0, 2, -1, -1]
    assert _label_codes(np.array([1, 2]), CLASSES).tolist() == [1, 2]


def test_multiclass_metrics_with_numeric_labels() -> None:
    obs = _obs([0, 1, 2, 0, 2, 1])
    values, skipped = compute_metrics(_probs(), obs, R.POINT, [*CATEGORICAL_METRICS, "rmse"])
    assert values["accuracy"] == pytest.approx(1.0)
    assert values["log_score"] == pytest.approx(np.mean(np.log([0.7, 0.8, 0.6, 0.5, 0.4, 0.6])))
    p = _probs().values
    expected = np.mean([binary_auc(p[:, j], (obs.values == j).astype(float)) for j in range(3)])
    assert values["auc"] == pytest.approx(expected)
    assert "categorical" in skipped["rmse"]


def test_macro_auc_is_skipped_with_fewer_than_two_classes_with_both_outcomes() -> None:
    obs = _obs(["0"] * 6)
    values, skipped = compute_metrics(_probs(), obs, R.POINT, ["auc", "accuracy"])
    assert "auc" not in values and "two categories" in skipped["auc"]
    assert values["accuracy"] == pytest.approx(2 / 6)  # rows 0 and 3 predict class 0


def test_macro_auc_on_draws_uses_the_mean_probability() -> None:
    probs = _probs()
    draws = xr.concat([probs, probs], dim="draw").expand_dims(chain=[0])
    draws.attrs["meaning"] = "class_probability"
    obs = _obs([0.0, 1.0, 2.0, 0.0, 2.0, np.nan])
    values, _ = compute_metrics(draws, obs, R.DRAWS, ["auc", "accuracy", "brier"])
    assert values["accuracy"] == pytest.approx(1.0)
    assert 0.5 < values["auc"] <= 1.0
    assert values["brier"] > 0


def test_two_class_macro_auc_equals_the_binary_auc() -> None:
    p1 = np.array([0.9, 0.2, 0.6, 0.4, 0.7])
    probs = xr.DataArray(
        np.stack([1 - p1, p1], axis=1),
        dims=["row", "class"],
        coords={"row": np.arange(5), "class": ["no", "yes"]},
        attrs={"meaning": "class_probability"},
    )
    obs = _obs(["yes", "no", "yes", "no", "no"])
    values, _ = compute_metrics(probs, obs, R.POINT, ["auc"])
    y = (obs.values == "yes").astype(float)
    assert values["auc"] == pytest.approx(binary_auc(p1, y))
