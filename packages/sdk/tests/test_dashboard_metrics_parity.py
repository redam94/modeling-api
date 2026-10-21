"""v0.17: the dashboard accuracy of ``mapi_spec.dashboard_metrics`` (pure Python, run by the
server on an actuals upload) equals the holdout metrics of ``mapi_sdk.metrics.compute_metrics``
(numpy/xarray, run at fit time) to 1e-9, on 20 seeded datasets per family: numeric errors with
a band, binary classifiers and classifiers of several classes."""

from __future__ import annotations

import math

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.metrics import compute_metrics
from mapi_spec import Representation
from mapi_spec.dashboard_metrics import (
    classification_metrics_of,
    error_metrics_of,
    multiclass_metrics_of,
)

R = Representation
SEEDS = range(20)
TOL = 1e-9
ERROR_NAMES = ("mae", "rmse", "bias", "mape", "smape", "wape", "r2", "coverage_90")
CLASS_NAMES = ("accuracy", "brier", "log_score", "auc")


def _close(got: float | None, want: float | None, what: str) -> None:
    if want is None:
        assert got is None, what
        return
    assert got is not None, what
    assert math.isclose(got, want, rel_tol=TOL, abs_tol=TOL), (what, got, want)


# ===================================================================================== errors


def _error_case(seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(point, lower, upper, actual)``: some zero actuals, some exact ties, a few misses."""
    rng = np.random.default_rng(seed)
    n = int(rng.integers(5, 400))
    actual = rng.normal(50, 20, n)
    if seed % 2:
        actual = np.round(actual)  # integral actuals (counts) and ties
    zeros = rng.random(n) < 0.05
    actual[zeros] = 0.0
    point = actual + rng.normal(0, 5 + seed, n)
    if seed % 5 == 0:
        point[: n // 10] = actual[: n // 10]  # exact predictions
    radius = np.abs(rng.normal(8, 3, n))
    lower, upper = point - radius, point + radius
    if seed % 4 == 0:
        upper[:3] = actual[:3]  # actuals on the bound count as inside
    return point, lower, upper, actual


@pytest.mark.parametrize("seed", SEEDS)
def test_error_metrics_match_compute_metrics(seed: int) -> None:
    point, lower, upper, actual = _error_case(seed)
    n = point.size
    pred = xr.DataArray(
        np.stack([lower, upper, point], axis=-1),
        dims=("row", "bound"),
        coords={"row": np.arange(n), "bound": ["lower", "upper", "point"]},
        attrs={"alpha": 0.1},
    )
    obs = xr.DataArray(actual, dims=("row",), coords={"row": np.arange(n)})
    want, skipped = compute_metrics(pred, obs, R.INTERVAL, ERROR_NAMES)
    assert not skipped, skipped

    got = error_metrics_of(
        list(zip(point.tolist(), actual.tolist(), strict=True)),
        list(zip(lower.tolist(), upper.tolist(), strict=True)),
    )
    assert got.n == n
    for name in ERROR_NAMES[:-1]:
        _close(getattr(got, name), want[name], name)
    hit = want["coverage_90"]
    _close(got.band_hit_rate, None if hit is None else hit * 100, "band_hit_rate")


# ============================================================================= binary


def _binary_case(seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(100 + seed)
    n = int(rng.integers(4, 600))
    p = rng.beta(2, 2, n)
    if seed % 3 == 0:
        p = np.round(p, 1)  # tied scores (AUC average ranks) and exact 0 / 1
    y = (rng.random(n) < p).astype(np.float64)
    if seed == 7:
        y[:] = 1.0  # one class only: no AUC
    return p, y


@pytest.mark.parametrize("seed", SEEDS)
def test_binary_metrics_match_compute_metrics(seed: int) -> None:
    p, y = _binary_case(seed)
    n = p.size
    pred = xr.DataArray(p, dims=("row",), coords={"row": np.arange(n)})
    obs = xr.DataArray(y, dims=("row",), coords={"row": np.arange(n)})
    want, skipped = compute_metrics(pred, obs, R.POINT, CLASS_NAMES)
    assert set(skipped) <= {"auc"}, skipped

    labels = ["1" if v == 1 else "0" for v in y.tolist()]
    got = classification_metrics_of(p.tolist(), labels, "1", 0.5)
    assert got.n == n
    for name in CLASS_NAMES:
        _close(getattr(got, name), want.get(name), name)
    assert got.rate_obs is not None
    _close(got.rate_obs, float(y.mean()), "rate_obs")


# ========================================================================= multiclass


def _multiclass_case(seed: int) -> tuple[list[str], np.ndarray, list[str]]:
    """``(classes, probs[n, k], observed labels)`` with a few unknown labels, empty labels,
    all-zero rows and probabilities that need rescaling."""
    rng = np.random.default_rng(200 + seed)
    k = int(rng.integers(2, 7))
    classes = [f"Class {j}" for j in range(k)]
    n = int(rng.integers(5, 500))
    probs = rng.dirichlet(np.ones(k), n)
    if seed % 2:
        probs = probs * rng.uniform(0.3, 1.0, (n, 1))  # rows below 1 (rescaled)
    if seed % 3 == 0:
        probs[:2] = 0.0  # no choice set: does not count
    codes = np.array([rng.choice(k, p=row / row.sum()) if row.sum() else 0 for row in probs])
    labels = [classes[c] for c in codes]
    if seed % 4 == 0:
        labels[2] = "class_0"  # loose match ("Class 0")
        labels[3] = "nothing like it"  # matches no class: a miss with probability 0
    if seed % 5 == 0:
        labels[4] = ""  # empty label: does not count
    return classes, probs, labels


@pytest.mark.parametrize("seed", SEEDS)
def test_multiclass_metrics_match_compute_metrics(seed: int) -> None:
    classes, probs, labels = _multiclass_case(seed)
    n = len(labels)
    pred = xr.DataArray(
        probs,
        dims=("row", "class"),
        coords={"row": np.arange(n), "class": classes},
        attrs={"meaning": "class_probability"},
    )
    obs = xr.DataArray(np.array(labels, dtype=object), dims=("row",), coords={"row": np.arange(n)})
    want, skipped = compute_metrics(pred, obs, R.POINT, CLASS_NAMES)
    assert set(skipped) <= {"auc"}, skipped

    rows = [dict(zip(classes, r, strict=True)) for r in probs.tolist()]
    got = multiclass_metrics_of(rows, labels)
    for name in CLASS_NAMES:
        _close(getattr(got, name), want.get(name), name)


def test_the_datasets_cover_the_edge_cases() -> None:
    """The seeds above reach ties, zero actuals, one-class holdouts and unknown labels."""
    assert any((_error_case(s)[3] == 0).any() for s in SEEDS)
    assert any(np.unique(_binary_case(s)[0]).size < _binary_case(s)[0].size for s in SEEDS)
    assert any(np.unique(_binary_case(s)[1]).size == 1 for s in SEEDS)
    assert any("nothing like it" in _multiclass_case(s)[2] for s in SEEDS)
