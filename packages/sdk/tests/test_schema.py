"""Array-level schema validation, inference and coordinate summaries."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.errors import SchemaValidationError
from mapi_sdk.schema import (
    check_dataset,
    data_summary,
    infer_freq,
    infer_schema,
    summarize_coords,
    validate_dataset,
)
from mapi_spec import DataSchema, Role
from mapi_spec.errors import ErrorCode, Severity

SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "market": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
        },
        "vars": {
            "revenue": {"dims": ["market", "week"], "dtype": "float64", "role": "target"},
            "spend": {"dims": ["market", "week"], "dtype": "float64", "bounds": [0, None]},
        },
    }
)


def _ds(weeks: int = 12, markets: tuple[str, ...] = ("NL", "DE", "FR", "BE")) -> xr.Dataset:
    rng = np.random.default_rng(0)
    time = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN")
    shape = (len(markets), weeks)
    return xr.Dataset(
        {
            "revenue": (("market", "week"), rng.uniform(1, 2, shape)),
            "spend": (("market", "week"), rng.uniform(0, 1, shape)),
        },
        coords={"market": list(markets), "week": time},
    )


def test_valid_dataset_has_no_issues() -> None:
    assert validate_dataset(SCHEMA, _ds()) == []
    assert check_dataset(SCHEMA, _ds()) == []


def test_missing_values_are_described_in_plain_language() -> None:
    ds = _ds()
    march = (ds.week.dt.month == 3).values
    ds["revenue"].values[:3, np.flatnonzero(march)[:2]] = np.nan
    issues = validate_dataset(SCHEMA, ds)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.message == "3 markets are missing 2 weeks of revenue in March 2024"
    assert issue.count == 6
    assert issue.context["groups"] == ["NL", "DE", "FR"]
    assert issue.to_issue().pointer == "/vars/revenue"


def test_missing_values_spanning_months_and_unequal_groups() -> None:
    ds = _ds()
    ds["revenue"].values[0, [1, 9]] = np.nan
    ds["revenue"].values[1, 1] = np.nan
    msg = validate_dataset(SCHEMA, ds)[0].message
    assert msg.startswith("2 markets are missing up to 2 weeks of revenue between 2024-01-14")


def test_missing_values_without_time_dim() -> None:
    schema = DataSchema.model_validate(
        {
            "dims": {"g": {"kind": "categorical"}},
            "vars": {"x": {"dims": ["g"], "dtype": "float64"}},
        }
    )
    ds = xr.Dataset({"x": (("g",), [1.0, np.nan, np.nan])}, coords={"g": ["a", "b", "c"]})
    msg = validate_dataset(schema, ds)[0].message
    assert msg == "2 values of x are missing (g: b, c)"
    schema_t = DataSchema.model_validate(
        {
            "dims": {"t": {"kind": "time", "freq": "D", "policy": "extend"}},
            "vars": {"x": {"dims": ["t"], "dtype": "float64"}},
        }
    )
    ds_t = xr.Dataset(
        {"x": (("t",), [1.0, np.nan, 2.0])},
        coords={"t": pd.date_range("2024-05-01", periods=3)},
    )
    assert validate_dataset(schema_t, ds_t)[0].message == "x is missing for 1 day in May 2024"


def test_structural_problems() -> None:
    ds = _ds().drop_vars("spend")
    ds["extra"] = (("market",), np.ones(4))
    ds["revenue"] = ds["revenue"].astype(str)
    issues = validate_dataset(SCHEMA, ds)
    msgs = [i.message for i in issues]
    assert "the data has no variable named 'spend'" in msgs
    assert any("should hold float64 values" in m for m in msgs)
    # the target may be optional at score time
    ok = _ds().drop_vars("revenue")
    assert validate_dataset(SCHEMA, ok, optional_roles=[Role.TARGET]) == []
    wrong_dims = _ds()
    wrong_dims["spend"] = wrong_dims["spend"].isel(week=0)
    assert any("should have dims" in i.message for i in validate_dataset(SCHEMA, wrong_dims))
    no_dim = xr.Dataset({"revenue": (("x",), [1.0])})
    msgs = [i.message for i in validate_dataset(SCHEMA, no_dim)]
    assert "the data has no dim named 'market'" in msgs


def test_transposed_and_int_data_are_info_only() -> None:
    ds = _ds()
    ds["spend"] = ds["spend"].transpose("week", "market").round().astype(int)
    issues = validate_dataset(SCHEMA, ds)
    assert issues and all(i.severity is Severity.INFO for i in issues)
    with pytest.raises(SchemaValidationError) as err:
        check_dataset(SCHEMA, _ds().drop_vars("spend"))
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.issues[0].pointer == "/vars/spend"


def test_coordinate_problems() -> None:
    ds = _ds()
    shuffled = ds.isel(week=[1, 0, 2, 3])
    assert any("increasing order" in i.message for i in validate_dataset(SCHEMA, shuffled))
    gappy = ds.isel(week=[0, 1, 5, 6])
    gaps = [
        i for i in validate_dataset(SCHEMA, gappy) if i.code is ErrorCode.NON_CONTIGUOUS_EXTENSION
    ]
    assert gaps[0].message == "3 weeks are missing from 'week' between 2024-01-21 and 2024-02-04"
    off = ds.assign_coords(week=ds.week + np.timedelta64(1, "D"))
    assert any("W-SUN grid" in i.message for i in validate_dataset(SCHEMA, off))
    dup = ds.assign_coords(market=["NL", "NL", "FR", "BE"])
    assert any("duplicated label" in i.message for i in validate_dataset(SCHEMA, dup))
    not_dates = ds.assign_coords(week=np.arange(12))
    assert any("not dates" in i.message for i in validate_dataset(SCHEMA, not_dates))


def test_bounds_sentinels_infinite_and_index_vars() -> None:
    schema = DataSchema.model_validate(
        {
            "layout": "long",
            "dims": {"obs": {"kind": "index"}, "county": {"kind": "categorical"}},
            "vars": {
                "y": {
                    "dims": ["obs"],
                    "dtype": "float64",
                    "bounds": [0, 10],
                    "missing_sentinel": -9,
                },
                "county_idx": {"dims": ["obs"], "dtype": "int64", "index_of": "county"},
            },
        }
    )
    ds = xr.Dataset(
        {
            "y": (("obs",), [1.0, -9.0, 11.0, -1.0, np.inf]),
            "county_idx": (("obs",), [0, 1, 2, -1, 0]),
        },
        coords={"county": ["a", "b"]},
    )
    msgs = {i.message for i in validate_dataset(schema, ds)}
    assert "1 value of 'y' equals the missing-data code -9; replace them before modelling" in msgs
    assert "1 value of 'y' is below 0" in msgs
    assert "2 values of 'y' are above 10" in msgs
    assert "1 value of 'y' is infinite" in msgs
    assert any("point outside 'county'" in m for m in msgs)


def test_infer_schema_and_freq() -> None:
    ds = _ds()
    ds["revenue"].values[0, 0] = np.nan
    ds["flag"] = (("market",), np.array([True, False, True, False]))
    ds["code"] = (("market",), np.array(["a", "b", "c", "d"]))
    ds["k"] = (("market",), np.array([1, 2, 3, 4], dtype=np.int32))
    ds["ts"] = (("market",), pd.date_range("2020-01-01", periods=4).values)
    ds["revenue"].attrs["units"] = "EUR"
    schema = infer_schema(ds, targets=["revenue"], policies={"market": "new_levels"})
    assert schema.dims["week"].kind == "time"
    assert schema.dims["week"].policy.value == "extend"
    assert schema.dims["week"].freq == "W-SUN"
    assert schema.dims["market"].policy.value == "new_levels"
    assert schema.vars["revenue"].role is Role.TARGET
    assert schema.vars["revenue"].missing == "allow"
    assert schema.vars["revenue"].units == "EUR"
    assert schema.vars["spend"].missing == "forbid"
    assert [schema.vars[v].dtype for v in ("flag", "code", "k", "ts")] == [
        "bool",
        "str",
        "int32",
        "datetime64[ns]",
    ]
    assert validate_dataset(schema, ds) == []
    assert infer_freq(pd.date_range("2024-01-01", periods=5, freq="MS")) == "MS"
    assert infer_freq(pd.date_range("2024-01-01", periods=2)) is None
    assert infer_freq(pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-05"])) is None
    with pytest.raises(ValueError, match="unsupported dtype"):
        infer_schema(xr.Dataset({"c": (("a",), np.array([1j]))}))
    plain = infer_schema(
        xr.Dataset({"x": (("i", "f"), np.zeros((2, 2)))}, coords={"f": [0.5, 1.5]})
    )
    assert plain.dims["i"].kind == "index"
    assert plain.dims["f"].kind == "float"


def test_summaries_feed_coordplan() -> None:
    ds = _ds(weeks=5)
    ds = ds.assign_coords(lag=("lag", [0, 2, 4]))
    ds["x"] = (("lag",), [1.0, 2.0, 3.0])
    cs = summarize_coords(ds, SCHEMA)
    assert cs["market"].values == ["NL", "DE", "FR", "BE"]
    assert cs["week"].kind == "time"
    assert cs["week"].start == "2024-01-07T00:00:00"
    assert cs["week"].end == "2024-02-04T00:00:00"
    assert cs["week"].freq == "W-SUN"
    assert cs["week"].contiguous
    assert cs["lag"].step == 2 and cs["lag"].contiguous
    gappy = summarize_coords(ds.isel(week=[0, 2]), SCHEMA)
    assert not gappy["week"].contiguous
    summary = data_summary(ds, SCHEMA)
    assert summary.vars == ["revenue", "spend", "x"]
    nocoord = summarize_coords(xr.Dataset({"x": (("i",), [1.0, 2.0])}))
    assert nocoord["i"].kind == "index" and nocoord["i"].end == 1
    floats = summarize_coords(xr.Dataset(coords={"f": [0.5]}))
    assert floats["f"].kind == "float"


def test_uint8_pixels_infer_as_uint8_and_validate_without_conversion() -> None:
    """v0.14: image pixels keep their 8-bit dtype; other unsigned ints infer as int64."""
    px = np.arange(2 * 4 * 3 * 3, dtype=np.uint8).reshape(2, 4, 3, 3)
    ds = xr.Dataset(
        {
            "pixels": (("image", "y", "x", "channel"), px),
            "count": (("image",), np.array([1, 2], dtype=np.uint16)),
        },
        coords={
            "image": ["a", "b"],
            "y": np.arange(4),
            "x": np.arange(3),
            "channel": ["r", "g", "b"],
        },
    )
    schema = infer_schema(ds)
    assert schema.vars["pixels"].dtype == "uint8" and schema.vars["count"].dtype == "int64"
    assert schema.vars["pixels"].missing == "forbid"
    assert validate_dataset(schema, ds) == []
    # a float array against a uint8 schema is refused; int64 values are compatible
    bad = ds.assign(pixels=ds["pixels"].astype(float))
    assert [i.message for i in validate_dataset(schema, bad)] == [
        "'pixels' should hold uint8 values but holds float64"
    ]
    assert validate_dataset(schema, ds.assign(pixels=ds["pixels"].astype(np.int64))) == []
    # bounds on integer pixels are still checked
    bounded = schema.model_copy(
        update={
            "vars": {
                **schema.vars,
                "pixels": schema.vars["pixels"].model_copy(update={"bounds": (None, 50)}),
            }
        }
    )
    issues = validate_dataset(bounded, ds)
    assert len(issues) == 1 and issues[0].count == int((px > 50).sum())
