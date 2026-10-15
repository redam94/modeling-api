"""Per-group context tails (dense and long layouts) and train/holdout splits."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sdk.context import context_layout, drop_context, extract_context, prepend_context
from mapi_sdk.split import split_dataset
from mapi_sdk.steps import StepContext
from mapi_sdk.testing import toy
from mapi_spec import ContextNeed, DataSchema
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError, Severity
from mapi_spec.spec import (
    AsOfSplit,
    LastPerGroupSplit,
    LeaveGroupOutSplit,
    MaskBlockSplit,
    TimeHoldoutSplit,
)

NEED = ContextNeed(along="time", n=3, within=["geo"])


def _long(n_hh: int = 3, trips: int = 5) -> xr.Dataset:
    hh = np.repeat(np.arange(n_hh), trips)
    return xr.Dataset(
        {"x": (("obs",), np.arange(n_hh * trips, dtype=float)), "hh": (("obs",), hh)},
        coords={"obs": np.arange(n_hh * trips)},
    )


# --------------------------------------------------------------------------------- context


def test_extract_dense_and_long(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=10)
    ctx = extract_context(ds, NEED)
    assert ctx is not None and ctx.sizes["time"] == 3
    assert ctx.time.values[-1] == ds.time.values[-1]
    assert extract_context(ds, None) is None
    assert extract_context(ds, ContextNeed(along="time", n=0)) is None
    assert extract_context(ds, ContextNeed(along="nope", n=2)) is None
    big = extract_context(ds, ContextNeed(along="time", n=50))
    assert big is not None and big.sizes["time"] == 10
    long = _long()
    lneed = ContextNeed(along="obs", n=2, within=["hh"])
    assert context_layout(long, lneed) == "long"
    lctx = extract_context(long, lneed)
    assert lctx is not None and lctx.sizes["obs"] == 6
    assert lctx["x"].values.tolist() == [3, 4, 8, 9, 13, 14]
    with pytest.raises(CoordPlanError):
        context_layout(long, ContextNeed(along="obs", n=1, within=["nope"]))
    with pytest.raises(CoordPlanError):
        context_layout(long, ContextNeed(along="nope", n=1))


def test_prepend_dense_contiguity(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=12)
    fit, future = ds.isel(time=slice(0, 8)), ds.isel(time=slice(8, None)).drop_vars("y")
    ctx = extract_context(fit, NEED)
    out, mark, issues = prepend_context(future, ctx, NEED, freq="W-SUN", step_id="lags")
    assert mark is not None and len(mark.labels) == 3 and not issues
    assert out.sizes["time"] == 7
    assert np.isnan(out["x"].isel(time=0).values).sum() == 0
    back = drop_context(out, [mark])
    xr.testing.assert_identical(back, future)
    gap = ds.isel(time=slice(9, None)).drop_vars("y")
    with pytest.raises(CoordPlanError) as err:
        prepend_context(gap, ctx, NEED, freq="W-SUN", step_id="lags")
    assert err.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION
    # in-sample: only the stored rows strictly before the score start are prepended
    tail = ds.isel(time=slice(6, 8)).drop_vars("y")
    _out2, mark2, _ = prepend_context(tail, ctx, NEED, freq="W-SUN")
    assert mark2 is not None and len(mark2.labels) == 1
    early = ds.isel(time=slice(0, 3))
    _, mark3, issues3 = prepend_context(early, ctx, NEED, freq="W-SUN")
    assert mark3 is None and issues3[0].severity is Severity.INFO
    # new and missing groups: context reindexed to the score groups (NaN for new ones)
    regroup = future.isel(geo=[0]).assign_coords(geo=["zz"])
    regroup = xr.concat([future.isel(geo=[1]), regroup], dim="geo")
    out4, _, _ = prepend_context(regroup, ctx, NEED, freq="W-SUN")
    assert np.isnan(out4["x"].sel(geo="zz").isel(time=0)).item()
    assert not np.isnan(out4["x"].sel(geo="b").isel(time=0)).item()
    # variables the context lacks are padded with NaN
    extra = future.assign(z=future["x"] * 2)
    out5, _, _ = prepend_context(extra, ctx, NEED, freq="W-SUN")
    assert np.isnan(out5["z"].isel(time=0)).all()
    # coordinate-only frames (forecast horizons without a payload) keep their axis
    coords_only = future.drop_vars(["x"])
    out6, mark6, _ = prepend_context(coords_only, ctx, NEED, freq="W-SUN")
    assert mark6 is not None and out6.sizes["time"] == 7
    assert list(out6["time"].values[3:]) == list(future["time"].values)
    xr.testing.assert_identical(drop_context(out6, [mark6]), coords_only)


def test_prepend_noops_and_errors(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=6)
    ctx = extract_context(ds, NEED)
    assert prepend_context(ds, None, NEED)[1] is None
    assert prepend_context(ds, ctx, None)[1] is None
    assert prepend_context(ds.isel(time=slice(0, 0)), ctx, NEED)[1] is None
    unlabeled = ds.drop_vars("time")
    with pytest.raises(CoordPlanError, match="labelled"):
        prepend_context(unlabeled, ctx, NEED)
    ints = ds.assign_coords(time=np.arange(6))
    ictx = extract_context(ints.isel(time=slice(0, 4)), NEED)
    out, mark, _ = prepend_context(ints.isel(time=slice(4, None)), ictx, NEED)
    assert mark is not None and list(out.time.values) == [1, 2, 3, 4, 5]
    with pytest.raises(CoordPlanError):
        prepend_context(ints.isel(time=slice(5, None)), ictx, NEED, step_id="s")


def test_prepend_long_layout() -> None:
    long = _long()
    need = ContextNeed(along="obs", n=2, within=["hh"])
    ctx = extract_context(long, need)
    score = xr.Dataset(
        {"x": (("obs",), [100.0, 101.0]), "hh": (("obs",), [1, 7])},
        coords={"obs": [0, 1]},
    )
    out, mark, _ = prepend_context(score, ctx, need)
    assert mark is not None and list(mark.labels) == [-2, -1]
    assert out["x"].values.tolist() == [8.0, 9.0, 100.0, 101.0]
    assert out["hh"].values.tolist() == [1, 1, 1, 7]
    back = drop_context(out, [mark])
    xr.testing.assert_identical(back, score)
    none = score.assign(hh=(("obs",), [8, 9]))
    assert prepend_context(none, ctx, need)[1] is None
    strs = score.assign_coords(obs=["r1", "r2"])
    _out_s, mark_s, _ = prepend_context(strs, ctx, need)
    assert mark_s is not None and str(mark_s.labels[0]).startswith("__context__")
    no_coord = score.drop_vars("obs")
    out_n, mark_n, _ = prepend_context(no_coord, ctx, need)
    assert mark_n is not None and out_n.sizes["obs"] == 4
    with pytest.raises(CoordPlanError):
        prepend_context(score.assign_coords(obs=[0.5, 1.5]), ctx, need)


def test_drop_context_by_position_after_reshape(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=6)
    fit, future = ds.isel(time=slice(0, 3)), ds.isel(time=slice(3, None))
    out, mark, _ = prepend_context(future, extract_context(fit, NEED), NEED, freq="W-SUN")
    assert mark is not None
    shifted = out.assign_coords(time=out.time + np.timedelta64(1, "D"))
    kept = drop_context(shifted, [mark])
    assert kept.sizes["time"] == 3
    assert drop_context(out.drop_dims("time"), [mark]).sizes == out.drop_dims("time").sizes


@settings(max_examples=40, deadline=None)
@given(
    split_at=st.integers(4, 14),
    lags=st.lists(st.integers(1, 4), min_size=1, max_size=3, unique=True),
)
def test_property_context_window_equivalence(split_at: int, lags: list[int]) -> None:
    """Transforming fit-tail context + new rows equals transforming the full series."""
    rng = np.random.default_rng(split_at)
    time = pd.date_range("2024-01-07", periods=18, freq="W-SUN").as_unit("ns")
    ds = xr.Dataset(
        {"x": (("geo", "time"), rng.normal(size=(2, 18)))}, coords={"geo": ["a", "b"], "time": time}
    )
    step = toy.LagStep()
    s = step.parse_settings({"vars": ["x"], "within": ["geo"], "lags": lags})
    need = step.context(s)
    st_ = step.fit(ds, s, StepContext("lag"))
    full = step.transform(ds, s, st_, StepContext("lag"))
    head, tail = ds.isel(time=slice(0, split_at)), ds.isel(time=slice(split_at, None))
    combined, mark, _ = prepend_context(tail, extract_context(head, need), need, freq="W-SUN")
    out = step.transform(combined, s, st_, StepContext("lag"))
    out = drop_context(out, [mark] if mark else [])
    xr.testing.assert_allclose(out, full.isel(time=slice(split_at, None)))


# ----------------------------------------------------------------------------------- splits

SCHEMA = DataSchema.model_validate(
    {
        "dims": {"geo": {"kind": "categorical"}, "time": {"kind": "time", "policy": "extend"}},
        "vars": {
            "y": {
                "dims": ["geo", "time"],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            },
            "x": {"dims": ["geo", "time"], "dtype": "float64"},
        },
    }
)


def test_time_holdout_and_as_of(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=10)
    r = split_dataset(ds, TimeHoldoutSplit(kind="time_holdout", dim="time", last=3))
    assert (r.train.sizes["time"], r.holdout.sizes["time"]) == (7, 3)
    assert not r.is_masked and r.kind == "time_holdout"
    r = split_dataset(ds, TimeHoldoutSplit(kind="time_holdout", dim="time", fraction=0.25))
    assert r.holdout.sizes["time"] == 3
    r = split_dataset(ds, TimeHoldoutSplit(kind="time_holdout", dim="time", after="2024-02-04"))
    assert r.train.time.values[-1] == np.datetime64("2024-02-04")
    r = split_dataset(ds, AsOfSplit(kind="as_of", dim="time", as_of="2024-02-04", gap=2))
    # the gap weeks stay in the holdout (scoring continues the fit) but are never evaluated
    assert r.lead == 2 and r.holdout.time.values[0] == np.datetime64("2024-02-11")
    assert r.holdout.time.values[r.lead] == np.datetime64("2024-02-25")
    with pytest.raises(MapiError) as err:
        split_dataset(ds, TimeHoldoutSplit(kind="time_holdout", dim="time", last=10))
    assert err.value.code is ErrorCode.SPLIT_INVALID
    with pytest.raises(MapiError, match="no holdout"):
        split_dataset(ds, TimeHoldoutSplit(kind="time_holdout", dim="time", after="2030-01-01"))
    with pytest.raises(MapiError, match="does not exist"):
        split_dataset(ds, TimeHoldoutSplit(kind="time_holdout", dim="week", last=1))


def test_leave_group_out(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=4, groups=("a", "b", "c", "d"))
    r = split_dataset(ds, LeaveGroupOutSplit(kind="leave_group_out", dim="geo", groups=["b"]))
    assert list(r.holdout.geo.values) == ["b"] and r.train.sizes["geo"] == 3
    r1 = split_dataset(ds, LeaveGroupOutSplit(kind="leave_group_out", dim="geo", n=2, seed=3))
    r2 = split_dataset(ds, LeaveGroupOutSplit(kind="leave_group_out", dim="geo", n=2, seed=3))
    assert list(r1.holdout.geo.values) == list(r2.holdout.geo.values)
    with pytest.raises(MapiError, match="do not exist"):
        split_dataset(ds, LeaveGroupOutSplit(kind="leave_group_out", dim="geo", groups=["zz"]))
    with pytest.raises(MapiError, match="every group"):
        split_dataset(ds, LeaveGroupOutSplit(kind="leave_group_out", dim="geo", n=4))


def test_last_per_group_dense_and_long(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=6)
    ds["y"][2, 3:] = np.nan  # group c only has 3 valid rows
    split = LastPerGroupSplit(kind="last_per_group", dim="time", within=["geo"], last=2, min_len=2)
    r = split_dataset(ds, split, schema=SCHEMA)
    assert r.is_masked and r.holdout_mask is not None
    assert r.holdout_mask.sel(geo="a").values.tolist() == [False] * 4 + [True] * 2
    assert not r.holdout_mask.sel(geo="c").any()
    assert np.isnan(r.train["y"].sel(geo="a").values[-2:]).all()
    assert not np.isnan(r.train["x"].values).any()
    xr.testing.assert_identical(r.holdout, ds)
    long = _long(trips=4).assign(y=lambda d: d["x"])
    long_split = LastPerGroupSplit(
        kind="last_per_group", dim="obs", within=["hh"], last=1, min_len=3
    )
    lr = split_dataset(long, long_split)
    assert lr.holdout["obs"].values.tolist() == [3, 7, 11]
    with pytest.raises(MapiError, match="must be a dim or a variable"):
        split_dataset(long, long_split.model_copy(update={"within": ["nope"]}))
    with pytest.raises(MapiError, match="target-role"):
        split_dataset(ds, split, schema=None)


def test_mask_block(weekly_panel: Callable[..., xr.Dataset]) -> None:
    ds = weekly_panel(weeks=8, groups=("a", "b", "c", "d"))
    split = MaskBlockSplit(
        kind="mask_block", dims=["geo", "time"], fraction=0.25, block={"time": 2}, seed=1
    )
    r = split_dataset(ds, split, schema=SCHEMA)
    assert r.holdout_mask is not None
    frac = float(r.holdout_mask.mean())
    assert 0.1 < frac < 0.4
    held = r.train["y"].where(r.holdout_mask, drop=False)
    assert bool(held.isnull().all())
    assert bool(r.train["y"].where(~r.holdout_mask).count() == (~r.holdout_mask).sum())
    again = split_dataset(ds, split, schema=SCHEMA)
    assert again.holdout_mask is not None and again.holdout_mask.equals(r.holdout_mask)
    with pytest.raises(MapiError, match="every block"):
        split_dataset(ds, split.model_copy(update={"fraction": 0.99}), schema=SCHEMA)
