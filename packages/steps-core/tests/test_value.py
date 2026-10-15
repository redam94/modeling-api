"""Targeted tests for the VALUE steps."""

from __future__ import annotations

from typing import Any

import mapi_steps as ms
import numpy as np
import pytest
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_sdk.steps import StepContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_steps import value as vs
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def fitted(step: Any, ds: xr.Dataset, settings: dict[str, Any], **ctx: Any) -> tuple[Any, Any, Any]:
    inst = step()
    s = inst.parse_settings(settings)
    st = inst.fit(ds, s, StepContext(step_id="t", **ctx))
    return inst, s, st


def run(step: Any, ds: xr.Dataset, settings: dict[str, Any], **ctx: Any) -> xr.Dataset:
    inst, s, st = fitted(step, ds, settings, **ctx)
    return inst.transform(ds, s, st, StepContext(step_id="t", **ctx))


# ------------------------------------------------------------------------------ fx_convert

FX = {
    "vars": ["y", "spend"],
    "table": "fx",
    "table_along": "date",
    "currency_dim": "geo",
    "currencies": {"UK": "GBP"},
    "default_currency": "EUR",
}


def test_fx_convert_per_currency(daily: xr.Dataset, fx: xr.Dataset) -> None:
    out = run(ms.FxConvertStep, daily, FX, side_tables={"fx": fx})
    xr.testing.assert_allclose(out["y"].sel(geo="NL"), daily["y"].sel(geo="NL"))  # EUR → EUR
    gbp = fx["rate"].sel(currency="GBP", date=daily["time"].values).values
    np.testing.assert_allclose(out["y"].sel(geo="UK").values, daily["y"].sel(geo="UK").values * gbp)
    assert out["spend"].attrs["units"] == "EUR"
    inst, s, st = fitted(ms.FxConvertStep, daily, FX, side_tables={"fx": fx})
    back = inst.inverse(out[["y"]], s, st, StepContext(step_id="t", side_tables={"fx": fx}))
    xr.testing.assert_allclose(back["y"], daily["y"].transpose(*back["y"].dims))


def test_fx_convert_single_series_table(daily: xr.Dataset, fx: xr.Dataset) -> None:
    single = xr.Dataset({"rate": fx["rate"].sel(currency="SEK", drop=True)})
    out = run(
        ms.FxConvertStep,
        daily,
        {"vars": ["spend"], "table": "fx", "table_along": "date"},
        side_tables={"fx": single},
    )
    rate = single["rate"].sel(date=daily["time"].values).values
    np.testing.assert_allclose(
        out["spend"].isel(geo=0, channel=0).values,
        daily["spend"].isel(geo=0, channel=0).values * rate,
    )


def test_fx_convert_coverage_errors(daily: xr.Dataset, fx: xr.Dataset) -> None:
    short = fx.isel(date=slice(0, 40))
    with pytest.raises(MapiError) as err:
        run(ms.FxConvertStep, daily, FX, side_tables={"fx": short})
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    with pytest.raises(MapiError) as err:
        run(ms.FxConvertStep, daily, FX, side_tables={})
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    no_default = {**FX, "default_currency": None}
    with pytest.raises(MapiError) as err:
        run(ms.FxConvertStep, daily, no_default, side_tables={"fx": fx})
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    unknown = {**FX, "currencies": {"UK": "JPY"}}
    with pytest.raises(MapiError, match="JPY"):
        run(ms.FxConvertStep, daily, unknown, side_tables={"fx": fx})
    with pytest.raises(MapiError) as err:
        run(
            ms.FxConvertStep,
            daily,
            {"vars": ["y"], "table": "fx", "table_along": "date"},
            side_tables={"fx": fx},
        )
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    bad = fx.copy()
    bad["rate"] = bad["rate"].where(bad["date"] != daily["time"].values[3])
    with pytest.raises(MapiError, match="non-positive"):
        run(ms.FxConvertStep, daily, FX, side_tables={"fx": bad})
    with pytest.raises(ValidationError):
        vs.FxConvertSettings.model_validate(
            {"vars": ["y"], "table": "fx", "currencies": {"a": "EUR"}}
        )
    with pytest.raises(ValidationError):
        vs.FxConvertSettings.model_validate(
            {"vars": ["y"], "table": "fx", "currency_dim": "geo", "currencies": {"a": "eur"}}
        )


# ------------------------------------------------------------------------------- scaling


def test_mean_scale_state_is_labelled_by_remaining_dims(weekly: xr.Dataset) -> None:
    inst, s, st = fitted(ms.MeanScaleStep, weekly, {"vars": ["spend", "y"], "over": ["time"]})
    scale = st.array("scale__spend")
    assert set(scale.dims) == {"geo", "channel"}
    xr.testing.assert_allclose(scale, abs(weekly["spend"]).mean("time"))
    out = inst.transform(weekly, s, st, StepContext(step_id="t"))
    np.testing.assert_allclose(out["spend"].mean("time").values, 1.0)
    assert out["y"].attrs["units"] == "1"


def test_scale_unseen_level_policies(weekly: xr.Dataset) -> None:
    train = weekly.sel(geo=["NL", "DE"])
    for policy, expect in (
        ("pooled", lambda sc: sc.mean("geo")),
        ("fill", lambda sc: xr.full_like(sc.isel(geo=0, drop=True), 2.0)),
    ):
        inst, s, st = fitted(
            ms.MeanScaleStep,
            train,
            {"vars": ["spend"], "over": ["time"], "unseen_levels": policy, "fill_value": 2.0},
        )
        out = inst.transform(weekly, s, st, StepContext(step_id="t"))
        want = weekly["spend"].sel(geo="UK") / expect(st.array("scale__spend"))
        xr.testing.assert_allclose(
            out["spend"].sel(geo="UK"), want.transpose(*out["spend"].sel(geo="UK").dims)
        )
    inst, s, st = fitted(ms.MeanScaleStep, train, {"vars": ["spend"], "over": ["time"]})
    with pytest.raises(MapiError) as err:
        inst.transform(weekly, s, st, StepContext(step_id="t"))
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


def test_scale_guards(weekly: xr.Dataset) -> None:
    zero = weekly.assign(spend=weekly["spend"] * 0)
    _, _, st = fitted(ms.MaxAbsScaleStep, zero, {"vars": ["spend"], "over": ["time"]})
    assert bool((st.array("scale__spend") == 1.0).all())
    with pytest.raises(MapiError, match="none of the dims"):
        fitted(ms.MeanScaleStep, weekly, {"vars": ["promo_type"], "over": ["geo"]})
    with pytest.raises(MapiError, match="missing"):
        fitted(ms.MeanScaleStep, weekly, {"vars": ["ghost"], "over": ["time"]})
    with pytest.raises(MapiError) as err:
        ms.MeanScaleStep().parse_settings({"vars": ["a", "a"], "over": ["time"]})
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_standardize_moments_and_inverse(weekly: xr.Dataset) -> None:
    inst, s, st = fitted(ms.StandardizeStep, weekly, {"vars": ["y"], "over": ["time"], "ddof": 1})
    out = inst.transform(weekly, s, st, StepContext(step_id="t"))
    np.testing.assert_allclose(out["y"].mean("time").values, 0.0, atol=1e-12)
    np.testing.assert_allclose(out["y"].std("time", ddof=1).values, 1.0)
    centred_only = run(
        ms.StandardizeStep, weekly, {"vars": ["y"], "over": ["time"], "with_sd": False}
    )
    xr.testing.assert_allclose(
        centred_only["y"], weekly["y"] - weekly["y"].mean("time"), check_dim_order=False
    )
    plain = run(
        ms.StandardizeStep,
        weekly,
        {"vars": ["y"], "over": ["time"], "with_mean": False, "with_sd": False},
    )
    xr.testing.assert_allclose(plain["y"], weekly["y"].astype(float), check_dim_order=False)


# ---------------------------------------------------------------------- pointwise transforms


def test_log1p_and_logit_reject_out_of_domain(weekly: xr.Dataset) -> None:
    with pytest.raises(MapiError, match="> -1"):
        run(ms.Log1pStep, weekly.assign(y=weekly["y"] * 0 - 2), {"vars": ["y"]})
    with pytest.raises(MapiError, match=r"\[0, 1\]"):
        run(ms.LogitStep, weekly, {"vars": ["y"]})
    edges = weekly.assign(share=weekly["share"].where(weekly["share"] > 0.5, 0.0))
    out = run(ms.LogitStep, edges, {"vars": ["share"], "eps": 0.01})
    assert bool(np.isfinite(out["share"]).all())
    assert float(out["share"].min()) == pytest.approx(np.log(0.01 / 0.99))


def test_replace_sentinel_and_clip(weekly: xr.Dataset) -> None:
    out = run(ms.ReplaceSentinelStep, weekly, {"vars": ["price_raw"], "sentinels": [-99]})
    np.testing.assert_array_equal(out["price_raw"].isnull().values, weekly["price"].isnull().values)
    out = run(ms.ReplaceSentinelStep, weekly, {"vars": ["price_raw"], "below": 0, "above": 9.5})
    assert float(out["price_raw"].max()) <= 9.5 and float(out["price_raw"].min()) >= 0
    with pytest.raises(ValidationError):
        vs.ReplaceSentinelSettings.model_validate({"vars": ["a"]})
    clipped = run(ms.ClipStep, weekly, {"vars": ["spend"], "upper": 50})
    assert float(clipped["spend"].max()) == 50.0
    with pytest.raises(ValidationError):
        vs.ClipSettings.model_validate({"vars": ["a"], "lower": 2, "upper": 1})
    with pytest.raises(ValidationError):
        vs.ClipSettings.model_validate({"vars": ["a"]})


# ---------------------------------------------------------------------------------- impute


def test_impute_statistics_and_constant(weekly: xr.Dataset) -> None:
    out = run(ms.ImputeStep, weekly, {"vars": ["price"], "method": "mean", "over": ["time"]})
    assert bool(out["price"].notnull().all())
    nl = weekly["price"].sel(geo="NL")
    assert float(out["price"].sel(geo="NL").isel(time=4)) == pytest.approx(float(nl.mean()))
    med = run(
        ms.ImputeStep, weekly, {"vars": ["price"], "method": "median", "over": ["time", "geo"]}
    )
    assert float(med["price"].sel(geo="NL").isel(time=4)) == pytest.approx(
        float(weekly["price"].median())
    )
    const = run(ms.ImputeStep, weekly, {"vars": ["price"], "method": "constant", "value": -1})
    assert float(const["price"].sel(geo="NL").isel(time=4)) == -1
    with pytest.raises(ValidationError):
        vs.ImputeSettings.model_validate({"vars": ["a"], "method": "mean"})


def test_impute_ffill_limit_and_carry_past_the_fit(weekly: xr.Dataset) -> None:
    price = weekly["price"].copy()
    price[2, -6:] = np.nan  # UK: last six weeks missing
    ds = weekly.assign(price=price)
    settings = {"vars": ["price"], "method": "ffill", "along": "time", "within": ["geo"]}
    full = run(ms.ImputeStep, ds, settings)
    assert bool(full["price"].notnull().all())
    limited = run(ms.ImputeStep, ds, {**settings, "limit": 2})
    assert int(limited["price"].sel(geo="UK").isnull().sum()) == 4
    # fit on the head, replay on the tail: the carried value continues the fitted series
    head, tail = ds.isel(time=slice(0, 26)), ds.isel(time=slice(26, None))
    for extra in ({}, {"limit": 5}):
        inst, s, st = fitted(ms.ImputeStep, head, {**settings, **extra})
        got = inst.transform(tail, s, st, StepContext(step_id="t"))
        want = run(ms.ImputeStep, ds, {**settings, **extra}).isel(time=slice(26, None))
        xr.testing.assert_identical(got["price"], want["price"])
    # unseen groups: error by default, pooled carries the mean last value
    inst, s, st = fitted(ms.ImputeStep, head.sel(geo=["NL", "DE"]), settings)
    with pytest.raises(MapiError) as err:
        inst.transform(tail, s, st, StepContext(step_id="t"))
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    inst, s, st = fitted(
        ms.ImputeStep, head.sel(geo=["NL", "DE"]), {**settings, "unseen_levels": "pooled"}
    )
    got = inst.transform(tail, s, st, StepContext(step_id="t"))
    assert float(got["price"].sel(geo="UK").isel(time=-1)) == pytest.approx(
        float(st.array("carry__price").mean())
    )


def test_impute_ffill_long_layout(data: Any) -> None:
    long = data.long()
    y = long["y"].values.copy()
    y[[2, 5]] = np.nan  # h1's 2nd and 3rd rows
    long = long.assign(y=(("obs",), y))
    out = run(
        ms.ImputeStep, long, {"vars": ["y"], "method": "ffill", "along": "obs", "within": ["hh_id"]}
    )
    assert out["y"].values[2] == pytest.approx(y[0]) and out["y"].values[5] == pytest.approx(y[0])
    assert bool(np.isfinite(out["y"]).all())


def test_float_effects_match_int_inputs(weekly: xr.Dataset) -> None:
    ints = weekly.assign(
        count=(
            ("geo", "time"),
            np.arange(weekly.sizes["geo"] * weekly.sizes["time"]).reshape(3, -1),
        )
    )
    out = run(ms.ClipStep, ints, {"vars": ["count"], "upper": 10})
    assert out["count"].dtype == np.float64
    schema = infer_schema(out)
    assert schema.vars["count"].dtype == "float64"
