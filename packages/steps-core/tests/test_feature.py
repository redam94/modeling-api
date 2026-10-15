"""Targeted tests for the FEATURE steps."""

from __future__ import annotations

from typing import Any

import mapi_steps as ms
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_sdk.steps import StepContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_steps import feature as fs
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def fitted(step: Any, ds: xr.Dataset, settings: dict[str, Any], **ctx: Any) -> tuple[Any, Any, Any]:
    inst = step()
    s = inst.parse_settings(settings)
    return inst, s, inst.fit(ds, s, StepContext(step_id="t", **ctx))


def run(step: Any, ds: xr.Dataset, settings: dict[str, Any], **ctx: Any) -> xr.Dataset:
    inst, s, st = fitted(step, ds, settings, **ctx)
    return inst.transform(ds, s, st, StepContext(step_id="t", **ctx))


# --------------------------------------------------------------------------- time features


def test_fourier_counts_grid_steps_and_continues_past_the_fit(weekly: xr.Dataset) -> None:
    inst, s, st = fitted(ms.FourierStep, weekly, {"period": 52.0, "order": 2})
    assert st.freq == "W-SUN" and st.origin.startswith("2024-01-07")
    future = weekly.isel(time=slice(-5, None)).assign_coords(
        time=weekly["time"].values[-5:] + np.timedelta64(7 * 30, "D")
    )
    out = inst.transform(future, s, st, StepContext(step_id="t"))
    pos = np.arange(weekly.sizes["time"] - 5, weekly.sizes["time"]) + 30
    np.testing.assert_allclose(
        out["fourier"].sel(fourier_mode="sin_1").values, np.sin(2 * np.pi * pos / 52.0)
    )
    np.testing.assert_allclose(
        out["fourier"].sel(fourier_mode="cos_2").values, np.cos(4 * np.pi * pos / 52.0)
    )
    off = future.assign_coords(time=future["time"].values + np.timedelta64(1, "D"))
    with pytest.raises(MapiError, match="not on the"):
        inst.transform(off, s, st, StepContext(step_id="t"))


def test_fourier_monthly_is_exact_calendar_arithmetic(data: Any) -> None:
    months = pd.date_range("2020-01-01", periods=30, freq="MS").as_unit("ns")
    ds = xr.Dataset({"y": (("time",), np.arange(30.0))}, coords={"time": months})
    out = run(ms.FourierStep, ds, {"period": 12, "order": 1, "out": "yearly"})
    np.testing.assert_allclose(
        out["yearly"].sel(yearly_mode="sin_1").values,
        np.sin(2 * np.pi * np.arange(30) / 12),
        atol=1e-12,
    )
    numeric = xr.Dataset({"y": (("t",), np.zeros(5))}, coords={"t": np.arange(10, 15)})
    out = run(ms.FourierStep, numeric, {"along": "t", "period": 4, "order": 1})
    np.testing.assert_allclose(
        out["fourier"].sel(fourier_mode="cos_1").values,
        np.cos(2 * np.pi * np.arange(5) / 4),
        atol=1e-12,
    )


def test_fourier_days_and_missing_frequency(data: Any) -> None:
    irregular = pd.DatetimeIndex(["2024-01-01", "2024-01-03", "2024-01-10"]).as_unit("ns")
    ds = xr.Dataset({"y": (("time",), np.zeros(3))}, coords={"time": irregular})
    with pytest.raises(MapiError, match="frequency"):
        run(ms.FourierStep, ds, {"period": 7, "order": 1})
    out = run(ms.FourierStep, ds, {"period": 7, "order": 1, "unit": "days"})
    np.testing.assert_allclose(
        out["fourier"].sel(fourier_mode="sin_1").values,
        np.sin(2 * np.pi * np.array([0, 2, 9]) / 7),
        atol=1e-12,
    )


def test_trend_scaled_to_the_fit_range(weekly: xr.Dataset) -> None:
    inst, s, st = fitted(ms.TrendStep, weekly, {"along": "time"})
    out = inst.transform(weekly, s, st, StepContext(step_id="t"))
    assert float(out["trend"][0]) == 0.0 and float(out["trend"][-1]) == 1.0
    later = weekly.isel(time=[-1]).assign_coords(
        time=weekly["time"].values[-1:] + np.timedelta64(7 * 29, "D")
    )
    assert float(
        inst.transform(later, s, st, StepContext(step_id="t"))["trend"][0]
    ) == pytest.approx(2.0)
    raw = run(ms.TrendStep, weekly, {"along": "time", "scale_to_fit": False, "power": 2})
    assert float(raw["trend"][3]) == 9.0


def test_calendar_values(data: Any) -> None:
    daily = data.daily(days=40)
    out = run(
        ms.CalendarStep,
        daily,
        {
            "features": [
                "day_of_week",
                "is_weekend",
                "month",
                "week_of_year",
                "day_of_year",
                "year",
                "day_of_month",
                "is_month_start",
            ]
        },
    )
    cal = out["calendar"]
    assert cal.dims == ("time", "calendar_feature")
    assert cal.sel(calendar_feature="day_of_week").values[0] == 0  # 2024-01-01 is a Monday
    assert cal.sel(calendar_feature="is_weekend").values[5] == 1
    assert cal.sel(calendar_feature="month").values[-1] == 2
    assert cal.sel(calendar_feature="week_of_year").values[0] == 1
    assert cal.sel(calendar_feature="is_month_start").values[31] == 1
    with pytest.raises(MapiError, match="datetime"):
        run(ms.CalendarStep, xr.Dataset(coords={"time": np.arange(3)}), {"features": ["month"]})
    with pytest.raises(ValidationError):
        fs.CalendarSettings.model_validate({"features": ["month", "month"]})


# ------------------------------------------------------------------------------- holidays


def test_holidays_count_days_per_week(weekly: xr.Dataset, holidays: xr.Dataset) -> None:
    out = run(
        ms.HolidaysStep,
        weekly,
        {"table": "holidays", "table_along": "date"},
        side_tables={"holidays": holidays},
    )
    h = out["holiday"]
    assert h.dims == ("time",) and h.attrs == {}
    # 2024-01-01 (Mon) is in the week ending Sun 2024-01-07; 2024-04-01 in the week ending 04-07
    assert float(h.sel(time="2024-01-07")) == 1.0 and float(h.sel(time="2024-04-07")) == 1.0
    assert float(h.sum()) == 3.0  # Jan 1, Apr 1, May 1 within the 30 weeks
    anyday = run(
        ms.HolidaysStep,
        weekly,
        {"table": "holidays", "table_along": "date", "how": "max", "out": "is_hol"},
        side_tables={"holidays": holidays},
    )
    assert float(anyday["is_hol"].max()) == 1.0


def test_holidays_daily_by_dim_and_coverage(data: Any, holidays: xr.Dataset) -> None:
    daily = data.daily(days=10)
    per_geo = xr.concat(
        [holidays["holiday"], holidays["holiday"] * 0], dim=pd.Index(["NL", "DE"], name="geo")
    )
    table = xr.Dataset({"holiday": per_geo})
    with pytest.raises(MapiError) as err:
        run(
            ms.HolidaysStep,
            daily,
            {"table": "h", "table_along": "date", "by": ["geo"]},
            side_tables={"h": table},
        )
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    out = run(
        ms.HolidaysStep,
        daily,
        {"table": "h", "table_along": "date", "by": ["geo"], "missing_levels": "zero"},
        side_tables={"h": table},
    )
    assert out["holiday"].dims == ("time", "geo")
    assert float(out["holiday"].sel(geo="NL", time="2024-01-01")) == 1.0
    assert float(out["holiday"].sel(geo="UK").sum()) == 0.0
    late = data.weekly().assign_coords(
        time=data.weekly()["time"].values + np.timedelta64(7 * 80, "D")
    )
    with pytest.raises(MapiError) as err:
        run(
            ms.HolidaysStep,
            late,
            {"table": "h", "table_along": "date"},
            side_tables={"h": holidays},
        )
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    with pytest.raises(MapiError) as err:
        run(
            ms.HolidaysStep,
            daily,
            {"table": "h", "table_along": "date", "table_var": "x"},
            side_tables={"h": holidays},
        )
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE


# ------------------------------------------------------------------------------------ lags


def test_lag_dense_values_fill_and_mask(weekly: xr.Dataset) -> None:
    out = run(ms.LagStep, weekly, {"vars": ["y"], "within": ["geo"], "lags": [1, 2]})
    lagged = out["y_lagged"]
    assert lagged.dims == ("geo", "time", "lag")
    xr.testing.assert_allclose(lagged.sel(lag=2, drop=True), weekly["y"].shift(time=2))
    filled = run(ms.LagStep, weekly, {"vars": ["y"], "lags": [1], "fill": 0.0, "lag_dim": "k"})
    assert float(filled["y_lagged"].isel(time=0).sum()) == 0.0
    masked = weekly.assign(ok=weekly["y"] > float(weekly["y"].median()))
    out = run(ms.LagStep, masked, {"vars": ["y"], "lags": [1], "mask": "ok"})
    lag1 = out["y_lagged"].sel(lag=1, drop=True)
    assert bool(lag1.where(~masked["ok"]).isnull().all())  # masked cells stay missing
    src_masked = (~masked["ok"]).shift(time=1, fill_value=True)
    assert bool(lag1.where(src_masked & masked["ok"]).isnull().all())  # masked sources are missing
    filled = run(ms.LagStep, masked, {"vars": ["y"], "lags": [1], "mask": "ok", "fill": 0.0})
    lag1 = filled["y_lagged"].sel(lag=1, drop=True)
    assert bool(lag1.where(~masked["ok"]).isnull().all())  # fill never un-masks a cell
    assert bool(lag1.where(masked["ok"]).notnull().sum() == masked["ok"].sum())


def test_holidays_needs_a_known_frequency(weekly: xr.Dataset, holidays: xr.Dataset) -> None:
    two = weekly.isel(time=slice(0, 2))
    settings = {"table": "h", "table_along": "date"}
    with pytest.raises(MapiError, match="frequency"):
        run(ms.HolidaysStep, two, settings, side_tables={"h": holidays})
    out = run(ms.HolidaysStep, two, {**settings, "freq": "W-SUN"}, side_tables={"h": holidays})
    assert float(out["holiday"].isel(time=0)) == 1.0  # Jan 1 falls in the week ending Jan 7


def test_lag_long_layout_groups_rows(data: Any) -> None:
    long = data.long()
    out = run(ms.LagStep, long, {"vars": ["y"], "along": "obs", "within": ["hh_id"], "lags": [1]})
    lagged = out["y_lagged"].sel(lag=1).values
    for hh in ("h1", "h2", "h3"):
        rows = np.flatnonzero(long["hh_id"].values == hh)
        assert np.isnan(lagged[rows[0]])
        np.testing.assert_allclose(lagged[rows[1:]], long["y"].values[rows[:-1]])
    with pytest.raises(MapiError):
        run(ms.LagStep, long, {"vars": ["y"], "along": "obs", "within": ["nope"], "lags": [1]})
    with pytest.raises(ValidationError):
        fs.LagSettings.model_validate({"vars": ["y"], "lags": [0]})


def test_lag_tensor_pads_before_the_series_start(weekly: xr.Dataset) -> None:
    for pad, expect in (("first", lambda x0: x0), ("zero", lambda x0: 0.0)):
        out = run(ms.LagTensorStep, weekly, {"vars": ["spend"], "l_max": 3, "pad": pad})
        t = out["spend"]
        assert t.dims == ("geo", "channel", "time", "lag") and list(t["lag"].values) == [0, 1, 2]
        x0 = weekly["spend"].isel(time=0)
        xr.testing.assert_allclose(
            t.isel(time=0).sel(lag=2, drop=True), xr.zeros_like(x0) + expect(x0)
        )
        xr.testing.assert_allclose(
            t.sel(lag=1, drop=True).isel(time=slice(1, None)),
            weekly["spend"].shift(time=1).isel(time=slice(1, None)),
        )
    out = run(ms.LagTensorStep, weekly, {"vars": ["spend"], "l_max": 2, "pad": "nan"})
    assert bool(out["spend"].isel(time=0).sel(lag=1).isnull().all())
    # leading missing rows (e.g. context rows of an unseen group) are "before the start"
    gap = weekly.copy()
    gap["spend"] = gap["spend"].where(gap["time"] > gap["time"][2])
    out = run(ms.LagTensorStep, gap, {"vars": ["spend"], "l_max": 3, "pad": "first"})
    t = out["spend"]
    assert bool(t.isel(time=slice(0, 3)).isnull().all())
    xr.testing.assert_allclose(t.isel(time=3).sel(lag=2, drop=True), gap["spend"].isel(time=3))


# -------------------------------------------------------------------------- categoricals


def test_one_hot_levels_and_unseen_policies(weekly: xr.Dataset) -> None:
    inst, s, st = fitted(ms.OneHotStep, weekly, {"vars": ["promo_type"]})
    assert st.learned["levels"] == {"promo_type": ["bogo", "discount", "none"]}
    out = inst.transform(weekly, s, st, StepContext(step_id="t"))
    oh = out["promo_type_onehot"]
    assert oh.dims == ("time", "promo_type_level")
    np.testing.assert_allclose(oh.sum("promo_type_level").values, 1.0)
    new = weekly.assign(
        promo_type=weekly["promo_type"].where(weekly["time"] != weekly["time"][0], "flash")
    )
    with pytest.raises(MapiError) as err:
        inst.transform(new, s, st, StepContext(step_id="t"))
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    for policy in ("fill", "pooled"):
        ps = inst.parse_settings({"vars": ["promo_type"], "unseen_levels": policy})
        row = inst.transform(new, ps, st, StepContext(step_id="t"))["promo_type_onehot"].isel(
            time=0
        )
        if policy == "fill":
            assert float(row.sum()) == 0.0
        else:
            np.testing.assert_allclose(row.values, st.learned["frequencies"]["promo_type"])
    dropped = run(ms.OneHotStep, weekly, {"vars": ["promo_type"], "drop_first": True})
    assert list(dropped["promo_type_level"].values) == ["discount", "none"]
    with pytest.raises(MapiError, match="levels"):
        run(ms.OneHotStep, weekly, {"vars": ["promo_type"], "max_levels": 2})


def test_encode_levels_codes_and_policies(weekly: xr.Dataset) -> None:
    inst, s, st = fitted(ms.EncodeLevelsStep, weekly, {"vars": ["promo_type"], "dims": ["geo"]})
    out = inst.transform(weekly, s, st, StepContext(step_id="t"))
    assert list(out["geo_code"].values) == [1, 0, 2]  # sorted levels DE, NL, UK
    assert out["promo_type_code"].dtype == np.int64
    more = xr.concat(
        [weekly, weekly.sel(geo=["NL"]).assign_coords(geo=["FR"])],
        dim="geo",
        data_vars="minimal",
        coords="minimal",
        compat="override",
    )
    with pytest.raises(MapiError) as err:
        inst.transform(more, s, st, StepContext(step_id="t"))
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    pooled = inst.parse_settings(
        {"vars": ["promo_type"], "dims": ["geo"], "unseen_levels": "pooled"}
    )
    assert (
        int(inst.transform(more, pooled, st, StepContext(step_id="t"))["geo_code"].sel(geo="FR"))
        == 3
    )
    filled = inst.parse_settings({"dims": ["geo"], "unseen_levels": "fill", "fill_code": -7})
    _, _, st2 = fitted(ms.EncodeLevelsStep, weekly, {"dims": ["geo"]})
    assert (
        int(inst.transform(more, filled, st2, StepContext(step_id="t"))["geo_code"].sel(geo="FR"))
        == -7
    )
    with pytest.raises(ValidationError):
        fs.EncodeLevelsSettings.model_validate({})
    numeric = weekly.assign(code=(("time",), np.where(np.arange(30) % 2 == 0, 1.0, np.nan)))
    out = run(ms.EncodeLevelsStep, numeric, {"vars": ["code"]})
    assert set(out["code_code"].values.tolist()) == {0, -1}


def test_missing_indicator(weekly: xr.Dataset) -> None:
    out = run(ms.MissingIndicatorStep, weekly, {"vars": ["price"]})
    np.testing.assert_array_equal(
        out["price_missing"].values, weekly["price"].isnull().values.astype(float)
    )
    schema = infer_schema(out)
    assert schema.vars["price_missing"].dtype == "float64"
