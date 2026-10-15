"""Targeted tests for the RESHAPE steps."""

from __future__ import annotations

from typing import Any

import mapi_steps as ms
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_sdk.steps import StepContext, StepState
from mapi_sdk.wire import read_zarr_zip, write_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_steps import reshape as rs
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def run(step: Any, ds: xr.Dataset, settings: dict[str, Any], **ctx: Any) -> xr.Dataset:
    inst = step()
    s = inst.parse_settings(settings)
    c = StepContext(step_id="t", **ctx)
    st = inst.fit(ds, s, c)
    return inst.transform(ds, s, st, c)


# ------------------------------------------------------------------------------- resample


def test_resample_drops_partial_weeks_and_sums(data: Any) -> None:
    daily = data.daily(days=7 * 6 + 3).isel(time=slice(2, None))  # starts Wed, ends Wed
    out = run(ms.ResampleStep, daily, {"freq": "W-SUN"})
    labels = pd.DatetimeIndex(out["time"].values)
    assert (labels.dayofweek == 6).all()
    assert len(labels) == 5  # partial first and last weeks are gone
    week = daily.sel(time=slice(labels[0] - pd.Timedelta(days=6), labels[0]))
    np.testing.assert_allclose(out["y"].isel(time=0), week["y"].sum("time"))
    assert out["y"].attrs["units"] == "LCU"


def test_resample_monthly_keeps_short_full_months(data: Any) -> None:
    daily = data.daily(days=31 + 29 + 31 + 10)  # 2024: Jan, Feb (29d), Mar full; Apr partial
    out = run(ms.ResampleStep, daily, {"freq": "MS", "how": "mean"})
    assert [str(t)[:7] for t in out["time"].values] == ["2024-01", "2024-02", "2024-03"]
    feb = daily.sel(time="2024-02")
    np.testing.assert_allclose(out["spend"].isel(time=1), feb["spend"].mean("time"))
    keep_all = run(ms.ResampleStep, daily, {"freq": "MS", "full_periods_only": False})
    assert keep_all.sizes["time"] == 4


def test_resample_how_vars_skipna_and_static_vars(data: Any) -> None:
    daily = data.daily(days=14)
    daily["y"][0, 3] = np.nan
    daily = daily.assign(static=(("geo",), np.arange(3.0)))
    out = run(ms.ResampleStep, daily, {"freq": "W-SUN", "how_vars": {"spend": "max"}})
    assert bool(np.isnan(out["y"].sel(geo="NL").isel(time=0)))
    np.testing.assert_allclose(
        out["spend"].isel(time=1), daily["spend"].isel(time=slice(7, 14)).max("time")
    )
    xr.testing.assert_identical(out["static"], daily["static"])
    skip = run(ms.ResampleStep, daily, {"freq": "W-SUN", "skipna": True})
    assert bool(np.isfinite(skip["y"]).all())


def test_resample_coordinate_only_frame_and_errors(data: Any) -> None:
    frame = xr.Dataset(coords={"time": pd.date_range("2024-01-01", periods=14, freq="D")})
    out = run(ms.ResampleStep, frame, {"freq": "W-SUN"})
    assert out.sizes["time"] == 2 and not out.data_vars
    with pytest.raises(MapiError, match="datetime"):
        run(ms.ResampleStep, xr.Dataset(coords={"time": np.arange(5)}), {"freq": "W-SUN"})
    tiny = data.daily(days=2)
    with pytest.raises(MapiError, match="frequency"):
        run(ms.ResampleStep, tiny, {"freq": "W-SUN"})
    labelled = tiny.assign(kind=(("time",), np.array(["a", "b"])))
    sch = infer_schema(data.daily(days=7))
    with pytest.raises(MapiError, match="not numeric"):
        run(ms.ResampleStep, labelled, {"freq": "W-SUN"}, schema=sch)
    with pytest.raises(MapiError):
        ms.ResampleStep().parse_settings({"freq": "fortnightly"})


# --------------------------------------------------------------------------- count_events


def test_count_events_matches_pandas(data: Any) -> None:
    ev = data.events()
    out = run(ms.CountEventsStep, ev, {"freq": "D", "by": ["store"], "sum_vars": ["amount"]})
    assert out["count"].dims == ("store", "time")
    frame = pd.DataFrame(
        {"t": ev["timestamp"].values, "store": ev["store"].values, "amount": ev["amount"].values}
    )
    expect = frame.groupby(["store", pd.Grouper(key="t", freq="D")]).size()
    for (store, day), n in expect.items():
        assert out["count"].sel(store=store, time=day).item() == n
    assert out["count"].sum().item() == ev.sizes["event"]
    np.testing.assert_allclose(out["amount"].sum(), ev["amount"].sum())
    assert "event" not in out.dims


def test_count_events_grid_levels_and_errors(data: Any) -> None:
    ev = data.events().drop_vars("amount")
    out = run(
        ms.CountEventsStep,
        ev,
        {
            "freq": "W-SUN",
            "by": ["store"],
            "levels": {"store": ["s1", "s2", "s3", "s4"]},
            "start": "2024-02-01",
            "end": "2024-03-10",
        },
    )
    assert list(out["store"].values) == ["s1", "s2", "s3", "s4"]
    assert out["count"].sel(store="s4").sum().item() == 0
    assert str(out["time"].values[0])[:10] == "2024-02-04"
    assert pd.Timestamp(out["time"].values[-1]) == pd.Timestamp("2024-03-10")
    with pytest.raises(MapiError) as err:
        run(ms.CountEventsStep, ev, {"freq": "D", "by": ["store"], "levels": {"store": ["s1"]}})
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    with pytest.raises(MapiError, match="neither counted"):
        run(ms.CountEventsStep, data.events(), {"freq": "D", "by": ["store"]})
    with pytest.raises(ValidationError):
        rs.CountEventsSettings.model_validate({"freq": "D", "levels": {"store": ["a"]}})


# -------------------------------------------------------------------------- filter_coords


def test_filter_coords(weekly: xr.Dataset) -> None:
    out = run(ms.FilterCoordsStep, weekly, {"dim": "geo", "drop": ["DE"]})
    assert list(out["geo"].values) == ["NL", "UK"]
    out = run(
        ms.FilterCoordsStep, weekly, {"dim": "time", "start": "2024-03-01", "end": "2024-03-31"}
    )
    assert all(str(t)[:7] == "2024-03" for t in out["time"].values) and out.sizes["time"] == 5
    with pytest.raises(MapiError, match="ordered"):
        run(ms.FilterCoordsStep, weekly, {"dim": "geo", "start": "A"})
    with pytest.raises(ValidationError):
        rs.FilterCoordsSettings.model_validate({"dim": "geo", "keep": ["a"], "drop": ["b"]})
    with pytest.raises(ValidationError):
        rs.FilterCoordsSettings.model_validate({"dim": "geo"})


# --------------------------------------------------------------------------- ragged forms


def test_to_long_then_to_padded_round_trips(data: Any) -> None:
    padded = data.padded()
    long = run(
        ms.ToLongStep,
        padded,
        {"group_dim": "hh", "within_dim": "trip", "vars": ["y", "x", "mask"], "mask": "mask"},
    )
    assert long.sizes["obs"] == int(padded["mask"].sum())
    assert bool(long["mask"].all())
    np.testing.assert_array_equal(
        padded["y"].values[long["hh_idx"].values, long["trip_idx"].values], long["y"].values
    )
    back = run(
        ms.ToPaddedStep,
        long.drop_vars(["mask", "trip_idx"]),
        {"group": "hh_idx", "group_dim": "hh", "within_dim": "trip", "vars": ["y", "x"]},
    )
    xr.testing.assert_allclose(back["y"], padded["y"].transpose(*back["y"].dims))
    np.testing.assert_array_equal(back["mask"].values, padded["mask"].values)


def test_to_padded_from_labels_keeps_row_order(data: Any) -> None:
    long = data.long()
    out = run(
        ms.ToPaddedStep,
        long,
        {"group": "hh_id", "group_dim": "hh", "within_dim": "trip", "vars": ["y", "x"]},
    )
    assert list(out["hh"].values) == ["h1", "h2", "h3"]
    h1 = long["y"].values[long["hh_id"].values == "h1"]
    np.testing.assert_allclose(out["y"].sel(hh="h1").values[: h1.size], h1)
    assert int(out["mask"].sel(hh="h1").sum()) == h1.size
    with pytest.raises(MapiError, match="list them"):
        run(
            ms.ToPaddedStep,
            long,
            {"group": "hh_id", "group_dim": "hh", "within_dim": "trip", "vars": ["y"]},
        )


def test_to_long_drops_all_missing_rows_without_a_mask(data: Any) -> None:
    padded = data.padded().drop_vars("mask")
    long = run(ms.ToLongStep, padded, {"group_dim": "hh", "within_dim": "trip", "vars": ["y", "x"]})
    assert long.sizes["obs"] == int(np.isfinite(padded["y"]).sum())
    with pytest.raises(ValidationError):
        rs.ToLongSettings.model_validate(
            {"group_dim": "a", "within_dim": "b", "vars": ["y"], "mask": "m"}
        )


# --------------------------------------------------------------------------- stack/unstack


def test_stack_unstack_round_trip_including_the_wire(weekly: xr.Dataset) -> None:
    spend = weekly[["spend"]]
    stacked = run(ms.StackStep, spend, {"dims": ["geo", "channel"], "new_dim": "cell"})
    assert stacked["spend"].dims == ("time", "cell") or set(stacked["spend"].dims) == {
        "time",
        "cell",
    }
    back = run(ms.UnstackStep, stacked, {"dim": "cell", "into": ["geo", "channel"]})
    xr.testing.assert_identical(back["spend"].transpose(*spend["spend"].dims), spend["spend"])
    # after a zarr round trip the MultiIndex is reset to level coordinates
    from mapi_sdk._util import reset_multiindex

    flat = reset_multiindex(stacked)
    flat.attrs = {}
    wired = read_zarr_zip(write_zarr_zip(flat) or b"")
    back2 = run(ms.UnstackStep, wired, {"dim": "cell", "into": ["geo", "channel"]})
    np.testing.assert_allclose(
        back2["spend"].sel(geo=spend.geo, channel=spend.channel).transpose(*spend["spend"].dims),
        spend["spend"],
    )
    with pytest.raises(MapiError, match="not"):
        run(ms.UnstackStep, stacked, {"dim": "cell", "into": ["channel", "geo"]})
    with pytest.raises(MapiError, match="only some"):
        run(ms.StackStep, weekly, {"dims": ["geo", "channel"], "new_dim": "cell"})


def test_reshape_steps_are_stateless() -> None:
    for step in (
        ms.ResampleStep,
        ms.CountEventsStep,
        ms.FilterCoordsStep,
        ms.ToLongStep,
        ms.ToPaddedStep,
        ms.StackStep,
        ms.UnstackStep,
    ):
        assert not step.stateful()
        assert step.kind.value == "reshape"
        assert isinstance(step().new_state(), StepState)


@pytest.mark.parametrize("how", ["sum", "mean", "max", "min", "first", "last"])
@pytest.mark.parametrize("skipna", [False, True])
def test_resample_aggregations_match_pandas(data: Any, how: str, skipna: bool) -> None:
    daily = data.daily(days=21)
    daily["y"][0, 2] = np.nan
    out = run(ms.ResampleStep, daily, {"freq": "W-SUN", "how": how, "skipna": skipna})
    weeks = [daily["y"].sel(geo="NL").isel(time=slice(7 * w, 7 * w + 7)).values for w in range(3)]

    def reduce(week: np.ndarray) -> float:
        vals = week[~np.isnan(week)] if skipna else week
        if how in ("first", "last"):
            vals = vals if skipna else week
            return float(vals[0] if how == "first" else vals[-1])
        if vals.size == 0:
            return float("nan")
        fn = {"sum": np.sum, "mean": np.mean, "max": np.max, "min": np.min}[how]
        return float(fn(vals))

    np.testing.assert_allclose(out["y"].sel(geo="NL").values, [reduce(w) for w in weeks])


@pytest.mark.filterwarnings("error::FutureWarning")
def test_value_and_feature_steps_work_on_stacked_dims(weekly: xr.Dataset) -> None:
    """Steps after ``core.stack`` keep the MultiIndex (no single-level deletions) and their
    state survives the bundle round trip."""
    from mapi_sdk.pipeline import Pipeline
    from mapi_sdk.testing import check_step

    spend = weekly[["spend"]]
    pipe = Pipeline.build(
        [
            ("st", ms.StackStep, {"dims": ["geo", "channel"], "new_dim": "cell"}),
            ("sc", ms.MeanScaleStep, {"vars": ["spend"], "over": ["time"]}),
            ("lg", ms.LagTensorStep, {"vars": ["spend"], "l_max": 2}),
            ("un", ms.UnstackStep, {"dim": "cell", "into": ["geo", "channel"]}),
        ]
    )
    trace = pipe.fit(spend, keep_stages=True)
    scale = trace.states["sc"].array("scale__spend")
    assert list(scale.indexes["cell"].names) == ["geo", "channel"]
    out = trace.data["spend"]
    assert set(out.dims) == {"geo", "channel", "time", "lag"}
    xr.testing.assert_allclose(
        out.sel(lag=0, drop=True).transpose(*spend["spend"].dims),
        spend["spend"] / abs(spend["spend"]).mean("time"),
    )
    stacked = trace.stages["st"]
    # (the kit's own unseen-level probe cannot relabel a MultiIndex dim; not checked here)
    report = check_step(
        ms.MeanScaleStep, stacked, {"vars": ["spend"], "over": ["time"]}, raise_on_failure=False
    )
    for name in ("transform_equals_fit_output", "effect_matches_output", "inverse_roundtrip"):
        assert report.get(name).passed, report.summary()
    assert report.get("state_bundle_roundtrip").passed, report.summary()


TO_PADDED = {"group": "hh_id", "group_dim": "hh", "within_dim": "trip", "vars": ["y", "x"]}


def test_to_padded_group_code_paths(data: Any) -> None:
    long = data.long()
    with_dim = long.assign_coords(hh=["h1", "h2", "h3", "h4"])
    out = run(ms.ToPaddedStep, with_dim, TO_PADDED)
    assert list(out["hh"].values) == ["h1", "h2", "h3", "h4"]
    assert not bool(out["mask"].sel(hh="h4").any())  # a declared group without rows
    n = with_dim.sizes["obs"]
    bad = with_dim.assign(hh_id=(("obs",), np.array(["zz"] * n)))
    with pytest.raises(MapiError, match="not levels"):
        run(ms.ToPaddedStep, bad, TO_PADDED)
    idx = with_dim.assign(hh_id=(("obs",), np.full(n, 9)))
    with pytest.raises(MapiError, match="indexes outside"):
        run(ms.ToPaddedStep, idx, TO_PADDED)
    holes = long.assign(hh_id=(("obs",), np.array([None] + ["h1"] * (n - 1), dtype=object)))
    with pytest.raises(MapiError, match="missing values"):
        run(ms.ToPaddedStep, holes, TO_PADDED)
    two_d = long.assign(hh_id=long["y"].expand_dims(k=2))
    with pytest.raises(MapiError, match="1-D"):
        run(ms.ToPaddedStep, two_d, TO_PADDED)
