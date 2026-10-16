"""Future covariates over a forecast horizon (``mapi_sdk.future``)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.future import fill_future_covariates, season_length
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FutureCovariates

pytestmark = pytest.mark.unit

SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "geo": {"kind": "categorical"},
            "time": {"kind": "time", "policy": "extend", "freq": "MS"},
        },
        "vars": {
            "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target"},
            "spend": {"dims": ["geo", "time"], "dtype": "float64", "role": "spend"},
            "price": {"dims": ["time"], "dtype": "float64"},
        },
    }
)


def history(months: int = 30) -> xr.Dataset:
    t = pd.date_range("2022-01-01", periods=months, freq="MS")
    spend = np.arange(2 * months, dtype=float).reshape(2, months)
    spend[1, -1] = np.nan  # the last month of 'b' is missing
    return xr.Dataset(
        {
            "y": (("geo", "time"), np.ones((2, months))),
            "spend": (("geo", "time"), spend),
            "price": (("time",), 10.0 + np.arange(months) % 12),
        },
        coords={"geo": ["a", "b"], "time": t},
    )


def horizon(periods: int = 14, start: str = "2024-07-01") -> xr.Dataset:
    return xr.Dataset(
        coords={"geo": ["a", "b"], "time": pd.date_range(start, periods=periods, freq="MS")}
    )


def test_season_lengths() -> None:
    assert season_length("W-SUN") == 52 and season_length("MS") == 12
    assert season_length("D") == 365 and season_length("2W") == 26
    assert season_length("QS") == 4 and season_length(None) is None
    assert season_length("bogus") is None


def test_carry_last_repeats_the_last_finite_value_per_series() -> None:
    hist = history()
    out, issues = fill_future_covariates(
        horizon(), FutureCovariates.CARRY_LAST, dim="time", schema=SCHEMA, history=hist,
        targets=["y"],
    )  # fmt: skip
    assert set(out.data_vars) == {"spend", "price"}  # never the target
    np.testing.assert_array_equal(out["spend"].sel(geo="a").values, 29.0)
    np.testing.assert_array_equal(out["spend"].sel(geo="b").values, 58.0)  # skips the NaN
    assert out["spend"].dims == ("geo", "time") and out["price"].dims == ("time",)
    assert issues[0].severity is Severity.INFO and "carry_last" in issues[0].message


def test_seasonal_naive_repeats_the_last_year() -> None:
    hist = history()
    out, issues = fill_future_covariates(
        horizon(14), FutureCovariates.SEASONAL_NAIVE, dim="time", schema=SCHEMA, history=hist
    )
    last_year = hist["price"].values[-12:]
    np.testing.assert_array_equal(out["price"].values[:12], last_year)
    np.testing.assert_array_equal(out["price"].values[12:], last_year[:2])
    assert len(issues) == 1
    short, warn = fill_future_covariates(
        horizon(3), FutureCovariates.SEASONAL_NAIVE, dim="time", schema=SCHEMA,
        history=history(6),
    )  # fmt: skip
    assert any(i.severity is Severity.WARNING for i in warn)
    np.testing.assert_array_equal(short["price"].values, history(6)["price"].values[-1])


def test_seasonal_naive_aligns_by_date_and_skips_missing_values() -> None:
    """A horizon that starts later than the period after the history (a gap, or a payload of
    future months only) used to shift the pattern by the gap; a missing value in the last
    season was copied forward as NaN."""
    hist = history()  # 2022-01 .. 2024-06; price follows the month of the year
    out, issues = fill_future_covariates(
        horizon(6, start="2024-10-01"), FutureCovariates.SEASONAL_NAIVE, dim="time",
        schema=SCHEMA, history=hist,
    )  # fmt: skip
    months = pd.DatetimeIndex(out["time"].values).month
    np.testing.assert_array_equal(out["price"].values, 10.0 + (months - 1))
    # 'b' has no spend in 2024-06: 2025-06 takes the value one more season back (2023-06)
    june, _ = fill_future_covariates(
        horizon(1, start="2025-06-01"), FutureCovariates.SEASONAL_NAIVE, dim="time",
        schema=SCHEMA, history=hist,
    )  # fmt: skip
    b = hist["spend"].sel(geo="b")
    assert june["spend"].sel(geo="b").values[0] == b.sel(time="2023-06-01").item()
    assert not any(i.severity is Severity.WARNING for i in issues)
    # a value missing in every season falls back to the last finite one, with a warning
    gappy = hist.copy(deep=True)
    gappy["spend"].loc[{"geo": "b", "time": [t for t in gappy["time"].values
                                             if pd.Timestamp(t).month == 6]}] = np.nan  # fmt: skip
    filled, warns = fill_future_covariates(
        horizon(1, start="2025-06-01"), FutureCovariates.SEASONAL_NAIVE, dim="time",
        schema=SCHEMA, history=gappy,
    )  # fmt: skip
    assert filled["spend"].sel(geo="b").values[0] == b.sel(time="2024-05-01").item()
    assert any(i.severity is Severity.WARNING and "spend" in i.message for i in warns)


def test_zero_needs_no_history_and_supplied_values_are_kept() -> None:
    payload = horizon(4).assign(price=("time", np.full(4, 9.0)))
    out, _ = fill_future_covariates(payload, FutureCovariates.ZERO, dim="time", schema=SCHEMA)
    np.testing.assert_array_equal(out["spend"].values, 0.0)
    np.testing.assert_array_equal(out["price"].values, 9.0)  # supplied: untouched
    same, issues = fill_future_covariates(
        payload, FutureCovariates.SUPPLIED, dim="time", schema=SCHEMA
    )
    assert same is payload and not issues
    assert fill_future_covariates(payload, None, dim="time", schema=SCHEMA)[0] is payload


def test_history_is_needed_to_carry_values_forward() -> None:
    with pytest.raises(MapiError) as err:
        fill_future_covariates(
            horizon(), FutureCovariates.CARRY_LAST, dim="time", schema=SCHEMA, history=None
        )
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
