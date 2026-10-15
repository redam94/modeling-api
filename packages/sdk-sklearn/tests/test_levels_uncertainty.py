"""Unseen-level strategies and the uncertainty building blocks (quantile, bootstrap, conformal)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sklearn.design import build_design, plan_layout
from mapi_sklearn.estimators import get_entry, resolve_choice
from mapi_sklearn.levels import expand_new_levels
from mapi_sklearn.settings import PointSettings
from mapi_sklearn.uncertainty import (
    bootstrap_samples,
    calibration_mask,
    conformal_quantile,
    managed,
    quantile_overrides,
    rearrange,
)
from mapi_spec.errors import ErrorCode, MapiError
from sklearn.linear_model import LinearRegression

RIDGE = get_entry("sklearn.Ridge")


def _ds(stores: list[str], skus: list[str], seed: int = 0) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    shape = (len(stores), len(skus))
    return xr.Dataset(
        {
            "y": (("store", "sku"), rng.normal(size=shape)),
            "x": (("store", "sku"), rng.normal(size=shape)),
        },
        coords={"store": stores, "sku": skus},
    )


def _layout(ds: xr.Dataset, coord: dict[str, str]) -> Any:
    s = PointSettings.model_validate(
        {
            "estimator": {"kind": "sklearn.Ridge"},
            "rows": ["store", "sku"],
            "time_kind": "none",
            "coord_features": coord,
        }
    )
    layout = plan_layout(ds, s, entry=RIDGE)
    from mapi_sklearn.model import _with_counts

    return _with_counts(layout, build_design(ds, layout, phase="fit", allow_nan=False))


def test_pooled_onehot_equals_mean_encoding_for_linear_models() -> None:
    fit_ds = _ds(["a", "a2", "b"], ["k0", "k1"]).isel(store=[0, 2])
    fit_ds = xr.concat([fit_ds, fit_ds.isel(store=[0]).assign_coords(store=["c"])], dim="store")
    layout = _layout(fit_ds, {"store": "onehot"})
    d = build_design(fit_ds, layout, phase="fit", allow_nan=False)
    model = LinearRegression().fit(d.X, d.y)
    new = fit_ds.isel(store=[0]).assign_coords(store=["new"]).drop_vars("y")
    score = build_design(new, layout, phase="score", allow_nan=False)
    exp = expand_new_levels(score, layout, lambda d: "pooled", native_nan=False, max_levels=10)
    assert exp.pooled == ("store",) and exp.X.shape[0] == 2 * 3
    pooled = exp.reduce(model.predict(exp.X))
    mean_enc = score.X.copy()
    mean_enc[:, layout.columns_of("store")] = 1 / 3  # equal training counts per store
    np.testing.assert_allclose(pooled, model.predict(mean_enc))
    assert exp.weights is not None
    np.testing.assert_allclose(np.asarray(exp.weights.sum(axis=1)).ravel(), 1.0)


def test_two_pooled_dims_and_level_cap() -> None:
    fit_ds = _ds(["a", "b", "c"], ["k0", "k1"])
    layout = _layout(fit_ds, {"store": "ordinal", "sku": "onehot"})
    new = _ds(["a", "zz"], ["k0", "kk"]).drop_vars("y")
    d = build_design(new, layout, phase="score", allow_nan=False)
    assert {k: int(v.sum()) for k, v in d.new_rows.items()} == {"store": 2, "sku": 2}
    exp = expand_new_levels(d, layout, lambda _: "pooled", native_nan=False, max_levels=2)
    # rows: (a,k0) x1, (a,kk) x2 skus, (zz,k0) x2 stores, (zz,kk) x2 x2
    assert exp.X.shape[0] == 1 + 2 + 2 + 4
    vals = exp.reduce(np.arange(exp.X.shape[0], dtype=float)[None])
    assert vals.shape == (1, 4)
    assert exp.weights is not None
    np.testing.assert_allclose(np.asarray(exp.weights.sum(axis=1)).ravel(), 1.0)
    ordinal = exp.X[:, layout.columns_of("store")[0]]
    assert set(np.unique(ordinal)) <= {0.0, 1.0}  # capped to the two most frequent stores


def test_zero_error_and_hyperprior_strategies() -> None:
    fit_ds = _ds(["a", "b"], ["k0"])
    new = _ds(["zz"], ["k0"]).drop_vars("y")
    onehot = _layout(fit_ds, {"store": "onehot"})
    d = build_design(new, onehot, phase="score", allow_nan=False)
    exp = expand_new_levels(d, onehot, lambda _: "zero", native_nan=False, max_levels=5)
    assert exp.weights is None and exp.X[0, onehot.columns_of("store")].tolist() == [0.0, 0.0]
    np.testing.assert_array_equal(exp.reduce(np.ones(1)), np.ones(1))
    with pytest.raises(MapiError) as err:
        expand_new_levels(d, onehot, lambda _: "error", native_nan=False, max_levels=5)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    logs: list[str] = []
    hyper = expand_new_levels(
        d, onehot, lambda _: "hyperprior", native_nan=False, max_levels=5, log=logs.append
    )
    assert hyper.pooled == ("store",) and logs
    ordinal = _layout(fit_ds, {"store": "ordinal"})
    od = build_design(new, ordinal, phase="score", allow_nan=False)
    with pytest.raises(MapiError) as err:
        expand_new_levels(od, ordinal, lambda _: "zero", native_nan=False, max_levels=5)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    nan_ok = expand_new_levels(od, ordinal, lambda _: "zero", native_nan=True, max_levels=5)
    assert math.isnan(nan_ok.X[0, -1])


def test_expansion_size_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import mapi_sklearn.levels as levels

    fit_ds = _ds(["a", "b"], ["k0"])
    layout = _layout(fit_ds, {"store": "onehot"})
    d = build_design(_ds(["zz"], ["k0"]).drop_vars("y"), layout, phase="score", allow_nan=False)
    monkeypatch.setattr(levels, "MAX_EXPANDED_CELLS", 1)
    with pytest.raises(MapiError) as err:
        expand_new_levels(d, layout, lambda _: "pooled", native_nan=False, max_levels=5)
    assert err.value.code is ErrorCode.RESOURCE_EXHAUSTED


# ------------------------------------------------------------------------------ uncertainty


@settings(max_examples=100, deadline=None)
@given(
    scores=st.lists(st.floats(0, 1e6, allow_nan=False), min_size=1, max_size=200),
    alpha=st.floats(0.01, 0.6),
)
def test_conformal_radius_is_the_finite_sample_quantile(scores: list[float], alpha: float) -> None:
    n = len(scores)
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        with pytest.raises(MapiError) as err:
            conformal_quantile(np.asarray(scores), alpha)
        assert err.value.code is ErrorCode.SETTINGS_INVALID
        return
    q = conformal_quantile(np.asarray(scores), alpha)
    arr = np.asarray(scores)
    assert np.count_nonzero(arr <= q) >= k
    assert np.count_nonzero(arr < q) < k
    assert np.count_nonzero(arr <= q) / (n + 1) >= 1 - alpha - 1e-12


def test_conformal_marginal_coverage_on_exchangeable_data() -> None:
    """Over many calibration/test draws the interval covers >= 1 - alpha of new points."""
    rng = np.random.default_rng(0)
    alpha, hits, total = 0.2, 0, 0
    for _ in range(200):
        cal = np.abs(rng.standard_t(3, size=30))
        test = np.abs(rng.standard_t(3, size=50))
        q = conformal_quantile(cal, alpha)
        hits += int(np.count_nonzero(test <= q))
        total += test.size
    assert hits / total >= 1 - alpha - 0.01


def test_calibration_masks() -> None:
    times = np.repeat(np.arange(10), 3)
    last = calibration_mask(30, 0.2, by="last", seed=0, time_labels=times)
    assert last.sum() == 6 and set(times[last]) == {8, 9}
    tiny = calibration_mask(30, 0.99, by="last", seed=0, time_labels=times)
    assert set(times[~tiny]) == {0}
    rand = calibration_mask(30, 0.2, by="random", seed=1)
    assert rand.sum() == 6
    np.testing.assert_array_equal(rand, calibration_mask(30, 0.2, by="random", seed=1))
    with pytest.raises(MapiError):
        calibration_mask(1, 0.2, by="random", seed=0)
    with pytest.raises(MapiError) as err:
        calibration_mask(4, 0.2, by="last", seed=0, time_labels=np.zeros(4))
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_bootstrap_samples_rows_and_clusters() -> None:
    rows = bootstrap_samples(10, 5, seed=3)
    assert len(rows) == 5 and all(r.size == 10 and r.max() < 10 for r in rows)
    assert not all(np.array_equal(rows[0], r) for r in rows[1:])
    groups = np.repeat(np.array(["a", "b", "c"]), [2, 3, 5])
    clusters = bootstrap_samples(10, 20, seed=3, groups=groups)
    for idx in clusters:
        picked = groups[idx]
        for g in "abc":
            count = int(np.count_nonzero(picked == g))
            size = int(np.count_nonzero(groups == g))
            assert count % size == 0  # whole clusters only


def test_rearrange_sorts_crossing_quantiles() -> None:
    q = np.array([[1.0, 5.0, 2.0], [2.0, 4.0, 3.0], [3.0, 3.0, 1.0]])
    srt, crossed = rearrange(q, axis=0)
    assert crossed == 2
    assert np.all(np.diff(srt, axis=0) >= 0)
    assert rearrange(srt, axis=0)[1] == 0


def test_quantile_overrides_and_managed_params() -> None:
    xgb = get_entry("xgboost.XGBRegressor")
    choice = resolve_choice({"kind": "xgboost.XGBRegressor"})
    (ov,) = quantile_overrides(xgb, choice, [0.1, 0.9])
    assert ov["objective"] == "reg:quantileerror"
    np.testing.assert_allclose(ov["quantile_alpha"], [0.1, 0.9])
    hist = get_entry("sklearn.HistGradientBoostingRegressor")
    per = quantile_overrides(hist, resolve_choice({"kind": hist.kind}), [0.1, 0.5])
    assert per == [{"loss": "quantile", "quantile": 0.1}, {"loss": "quantile", "quantile": 0.5}]
    qr = get_entry("sklearn.QuantileRegressor")
    assert quantile_overrides(qr, resolve_choice({"kind": qr.kind}), [0.3]) == [{"quantile": 0.3}]
    with pytest.raises(MapiError, match="cannot fit quantile"):
        quantile_overrides(RIDGE, resolve_choice({"kind": "sklearn.Ridge"}), [0.5])
    poisson = resolve_choice({"kind": hist.kind, "params": {"loss": "poisson"}})
    with pytest.raises(MapiError, match="managed"):
        quantile_overrides(hist, poisson, [0.5])
    already = resolve_choice({"kind": hist.kind, "params": {"loss": "quantile"}})
    assert managed(hist, already, "loss", "quantile", "x") == {"loss": "quantile"}
    fixed_alpha = resolve_choice({"kind": xgb.kind, "params": {"quantile_alpha": 0.5}})
    with pytest.raises(MapiError, match="quantile_alpha"):
        quantile_overrides(xgb, fixed_alpha, [0.5])
