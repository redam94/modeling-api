"""Design matrices: column planning, coordinate encodings, row masks and unstacking."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sdk.schema import infer_schema
from mapi_sklearn.design import MAX_COLUMNS, build_design, plan_layout
from mapi_sklearn.estimators import get_entry
from mapi_sklearn.settings import PointSettings
from mapi_spec.errors import ErrorCode, MapiError

RIDGE = get_entry("sklearn.Ridge")
XGB = get_entry("xgboost.XGBRegressor")
HIST = get_entry("sklearn.HistGradientBoostingRegressor")


def _settings(**kw: Any) -> PointSettings:
    base: dict[str, Any] = {
        "estimator": {"kind": "sklearn.Ridge"},
        "rows": ["store", "time"],
        "coord_features": {},
    }
    return PointSettings.model_validate({**base, **kw})


def _panel(stores: int = 3, weeks: int = 6, seed: int = 0) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    time = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN").as_unit("ns")
    return xr.Dataset(
        {
            "y": (("store", "time"), rng.normal(size=(stores, weeks))),
            "price": (("store", "time"), rng.uniform(1, 2, (stores, weeks))),
            "spend": (("store", "channel", "time"), rng.uniform(0, 1, (stores, 2, weeks))),
            "trend": (("time",), np.arange(weeks, dtype=float)),
            "label": (("store",), np.array([f"L{i}" for i in range(stores)])),
            "w": (("store", "time"), np.ones((stores, weeks))),
        },
        coords={
            "store": [f"s{i}" for i in range(stores)],
            "channel": ["tv", "search"],
            "time": time,
        },
    )


def test_columns_from_roles_flatten_extra_dims_and_broadcast() -> None:
    ds = _panel()
    stage = infer_schema(ds, targets=["y"], roles={"w": "weight"})
    layout = plan_layout(ds, _settings(weight="w"), entry=RIDGE, stage_schema=stage)
    assert layout.feature_names == ["price", "spend[tv]", "spend[search]", "trend"]
    assert layout.extra_levels == {"channel": ["tv", "search"]}
    design = build_design(ds, layout, phase="fit", allow_nan=False)
    assert design.X.shape == (18, 4) and design.n_cells == 18
    np.testing.assert_allclose(
        design.X[:, 1], ds["spend"].sel(channel="tv").transpose("store", "time").values.ravel()
    )
    np.testing.assert_allclose(design.X[:6, 3], np.arange(6.0))  # broadcast over store
    assert design.w is not None and design.y is not None
    # without a stage schema every numeric non-reserved variable is a candidate
    logs: list[str] = []
    fallback = plan_layout(ds, _settings(weight="w"), entry=RIDGE, log=logs.append)
    assert fallback.feature_names == layout.feature_names
    assert any("label" in m for m in logs)


def test_explicit_vars_exclude_and_errors() -> None:
    ds = _panel()
    s = _settings(columns={"vars": ["price", "trend"], "exclude": ["trend"]})
    assert plan_layout(ds, s, entry=RIDGE).feature_names == ["price"]
    for vars_, code in (
        (["nope"], ErrorCode.SCHEMA_INVALID),
        (["label"], ErrorCode.SETTINGS_INVALID),
    ):
        with pytest.raises(MapiError) as err:
            plan_layout(ds, _settings(columns={"vars": vars_}), entry=RIDGE)
        assert err.value.code is code
    no_rows = ds.assign(const=(("channel",), np.ones(2)))
    with pytest.raises(MapiError, match="none of the row dims"):
        plan_layout(no_rows, _settings(columns={"vars": ["const"]}), entry=RIDGE)
    with pytest.raises(MapiError, match="no feature columns"):
        plan_layout(ds, _settings(columns={"vars": []}), entry=RIDGE)
    with pytest.raises(MapiError, match="row dims"):
        plan_layout(ds, _settings(rows=["store", "region"], time_kind="none"), entry=RIDGE)
    with pytest.raises(MapiError, match="target"):
        plan_layout(ds.drop_vars("y"), _settings(), entry=RIDGE)


def test_too_many_columns_is_rejected() -> None:
    big = xr.Dataset(
        {"y": (("store",), np.ones(2)), "x": (("store", "k"), np.ones((2, MAX_COLUMNS + 1)))},
        coords={"store": ["a", "b"], "k": np.arange(MAX_COLUMNS + 1)},
    )
    s = _settings(rows=["store"], time_kind="none")
    with pytest.raises(MapiError, match="more than"):
        plan_layout(big, s, entry=RIDGE)
    half = MAX_COLUMNS // 2 + 1
    two = xr.Dataset(
        {
            "y": (("store",), np.ones(2)),
            "x": (("store", "k"), np.ones((2, half))),
            "z": (("store", "k"), np.ones((2, half))),
        },
        coords={"store": ["a", "b"], "k": np.arange(half)},
    )
    with pytest.raises(MapiError, match="at most"):
        plan_layout(two, s, entry=RIDGE)


def test_coordinate_encodings() -> None:
    ds = _panel()
    s = _settings(coord_features={"store": "category", "time": "numeric"}, columns={"vars": []})
    native = plan_layout(ds, s, entry=XGB)
    assert native.encodings["store"].encoding == "category"
    assert native.feature_names == ["store", "time"] and native.categorical == [0]
    onehot = plan_layout(ds, s, entry=RIDGE)
    assert onehot.encodings["store"].encoding == "onehot"
    assert onehot.feature_names == ["store=s0", "store=s1", "store=s2", "time"]
    d = build_design(ds, onehot, phase="fit", allow_nan=False)
    np.testing.assert_allclose(d.X[:6, 0], 1.0)
    np.testing.assert_allclose(d.X[:6, 3], np.arange(6) * 7.0)  # days since first week
    ordinal = plan_layout(ds, _settings(coord_features={"store": "ordinal"}), entry=RIDGE)
    od = build_design(ds, ordinal, phase="fit", allow_nan=False)
    np.testing.assert_allclose(od.X[:, -1], np.repeat([0.0, 1.0, 2.0], 6))
    with pytest.raises(MapiError, match="datetime labels"):
        plan_layout(ds, _settings(coord_features={"time": "onehot"}), entry=RIDGE)
    with pytest.raises(MapiError, match="numeric"):
        plan_layout(ds, _settings(coord_features={"store": "numeric"}), entry=RIDGE)
    many = xr.Dataset(
        {"y": (("store",), np.ones(300)), "x": (("store",), np.ones(300))},
        coords={"store": [f"s{i}" for i in range(300)]},
    )
    logs: list[str] = []
    hist = plan_layout(
        many,
        _settings(rows=["store"], time_kind="none", coord_features={"store": "category"}),
        entry=HIST,
        log=logs.append,
    )
    assert hist.encodings["store"].encoding == "onehot" and logs


def test_fit_drops_rows_and_records_reasons() -> None:
    ds = _panel()
    y = ds["y"].values.copy()
    y[0, 0] = np.nan
    price = ds["price"].values.copy()
    price[1, 2] = np.nan
    w = ds["w"].values.copy()
    w[2, 3] = 0.0
    mask = np.ones((3, 6), dtype=bool)
    mask[2, 5] = False
    ds = ds.assign(
        y=(("store", "time"), y),
        price=(("store", "time"), price),
        w=(("store", "time"), w),
        m=(("store", "time"), mask),
    )
    s = _settings(weight="w", mask="m", columns={"vars": ["price"]})
    layout = plan_layout(ds, s, entry=RIDGE)
    d = build_design(ds, layout, phase="fit", allow_nan=False)
    assert d.dropped == {
        "masked": 1,
        "missing_target": 1,
        "bad_weight": 1,
        "missing_feature": 1,
    }
    assert d.n == 14
    tolerant = build_design(ds, layout, phase="fit", allow_nan=True)
    assert "missing_feature" not in tolerant.dropped and tolerant.n == 15
    for missing in ("w", "m"):
        with pytest.raises(MapiError, match="missing"):
            build_design(ds.drop_vars(missing), layout, phase="fit", allow_nan=False)


def test_score_time_checks() -> None:
    ds = _panel()
    layout = plan_layout(
        ds, _settings(coord_features={"store": "onehot"}), entry=RIDGE, stage_schema=None
    )
    with pytest.raises(MapiError) as err:
        build_design(ds.drop_vars("price"), layout, phase="score", allow_nan=False)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
    with pytest.raises(MapiError) as err:
        build_design(ds.sel(channel=["tv"]), layout, phase="score", allow_nan=False)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
    extra = xr.concat(
        [ds, ds.isel(channel=[0]).assign_coords(channel=["radio"])],
        dim="channel",
        data_vars="minimal",
        coords="minimal",
        compat="override",
    )
    with pytest.raises(MapiError) as err:
        build_design(extra, layout, phase="score", allow_nan=False)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    with pytest.raises(MapiError) as err:
        build_design(ds.isel(store=0, drop=True), layout, phase="score", allow_nan=False)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    # padded labels are not predicted; new levels are flagged
    new = ds.isel(store=[0]).assign_coords(store=["zz"])
    both = xr.concat([ds, new], dim="store", data_vars="minimal", coords="minimal")
    flags = np.array([True, True, False, True])
    both = both.assign_coords(store__requested=("store", flags))
    d = build_design(both.drop_vars("y"), layout, phase="score", allow_nan=False)
    assert d.dropped == {"padding": 6} and d.n == 18
    assert set(d.new_rows) == {"store"} and int(d.new_rows["store"].sum()) == 6


@settings(max_examples=40, deadline=None)
@given(
    stores=st.integers(1, 4),
    weeks=st.integers(1, 6),
    data=st.data(),
)
def test_unstack_inverts_stacking_with_dropped_rows(stores: int, weeks: int, data: Any) -> None:
    """Every kept cell comes back to its label; every dropped cell comes back missing."""
    ds = _panel(stores, weeks)
    holes = data.draw(
        st.lists(st.booleans(), min_size=stores * weeks, max_size=stores * weeks), label="holes"
    )
    price = ds["price"].values.copy().reshape(-1)
    price[np.asarray(holes)] = np.nan
    ds = ds.assign(price=(("store", "time"), price.reshape(stores, weeks)))
    layout = plan_layout(ds, _settings(columns={"vars": ["price"]}), entry=RIDGE)
    d = build_design(ds, layout, phase="fit", allow_nan=False)
    out = d.unstack(
        {
            "a": ((), {}, d.X[:, 0]),
            "q": (("quantile",), {"quantile": [0.1, 0.9]}, np.stack([d.X[:, 0], -d.X[:, 0]])),
        }
    )
    expected = ds["price"].transpose("store", "time")
    np.testing.assert_array_equal(out["a"].transpose("store", "time").values, expected.values)
    np.testing.assert_array_equal(out["q"].sel(quantile=0.9).values, -expected.values)
    assert out["q"].dims == ("quantile", "store", "time")
    with pytest.raises(ValueError, match="rows"):
        d.unstack({"bad": ((), {}, np.zeros(d.n + 1))})
