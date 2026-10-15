"""Every built-in step passes the SDK contract kit (``mapi_sdk.testing.check_step``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import mapi_steps as ms
import pytest
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import check_step

pytestmark = pytest.mark.mapi_contract


@dataclass
class Case:
    id: str
    step: type[Any]
    data: str
    settings: dict[str, Any]
    tables: dict[str, str] = field(default_factory=dict)
    prep: Callable[[xr.Dataset], xr.Dataset] | None = None
    must_run: tuple[str, ...] = ()


def _stacked(ds: xr.Dataset) -> xr.Dataset:
    return ds[["spend"]].stack(cell=["geo", "channel"])


def _spend_only(ds: xr.Dataset) -> xr.Dataset:
    return ds[["spend"]]


CASES: list[Case] = [
    # reshape
    Case("resample", ms.ResampleStep, "daily", {"dim": "time", "freq": "W-SUN"}),
    Case("resample_monthly_mean", ms.ResampleStep, "daily", {"freq": "MS", "how": "mean"}),
    Case(
        "count_events",
        ms.CountEventsStep,
        "events",
        {"freq": "D", "by": ["store"], "sum_vars": ["amount"]},
    ),
    Case("filter_coords", ms.FilterCoordsStep, "weekly", {"dim": "geo", "keep": ["NL", "UK"]}),
    Case(
        "filter_time",
        ms.FilterCoordsStep,
        "weekly",
        {"dim": "time", "start": "2024-02-01", "end": "2024-06-30"},
    ),
    Case(
        "to_long",
        ms.ToLongStep,
        "padded",
        {"group_dim": "hh", "within_dim": "trip", "vars": ["y", "x", "mask"], "mask": "mask"},
    ),
    Case(
        "to_padded",
        ms.ToPaddedStep,
        "long",
        {"group": "hh_id", "group_dim": "hh", "within_dim": "trip", "vars": ["y", "x"]},
    ),
    Case(
        "stack",
        ms.StackStep,
        "weekly",
        {"dims": ["geo", "channel"], "new_dim": "cell"},
        prep=_spend_only,
    ),
    Case(
        "unstack",
        ms.UnstackStep,
        "weekly",
        {"dim": "cell", "into": ["geo", "channel"]},
        prep=_stacked,
    ),
    # value
    Case(
        "fx_convert",
        ms.FxConvertStep,
        "daily",
        {
            "vars": ["y", "spend"],
            "table": "fx",
            "table_along": "date",
            "currency_dim": "geo",
            "currencies": {"UK": "GBP"},
            "default_currency": "EUR",
        },
        tables={"fx": "fx"},
        must_run=("inverse_roundtrip",),
    ),
    Case(
        "mean_scale",
        ms.MeanScaleStep,
        "weekly",
        {"vars": ["spend", "y"], "over": ["time"], "unseen_levels": "pooled"},
        must_run=("inverse_roundtrip", "unseen_level_policies"),
    ),
    Case(
        "max_abs_scale",
        ms.MaxAbsScaleStep,
        "weekly",
        {"vars": ["spend"], "over": ["time", "geo"]},
        must_run=("inverse_roundtrip", "unseen_level_policies"),
    ),
    Case(
        "standardize",
        ms.StandardizeStep,
        "weekly",
        {"vars": ["y", "spend"], "over": ["time"]},
        must_run=("inverse_roundtrip", "unseen_level_policies"),
    ),
    Case("log1p", ms.Log1pStep, "weekly", {"vars": ["y"]}, must_run=("inverse_roundtrip",)),
    Case("logit", ms.LogitStep, "weekly", {"vars": ["share"]}, must_run=("inverse_roundtrip",)),
    Case(
        "impute_mean",
        ms.ImputeStep,
        "weekly",
        {"vars": ["price"], "method": "mean", "over": ["time"]},
        must_run=("unseen_level_policies",),
    ),
    Case(
        "impute_median",
        ms.ImputeStep,
        "weekly",
        {"vars": ["price"], "method": "median", "over": ["time"], "unseen_levels": "fill"},
    ),
    Case(
        "impute_ffill",
        ms.ImputeStep,
        "weekly",
        {"vars": ["price"], "method": "ffill", "along": "time", "within": ["geo"], "limit": 4},
    ),
    Case("impute_constant", ms.ImputeStep, "weekly", {"vars": ["price"], "method": "constant"}),
    Case(
        "replace_sentinel",
        ms.ReplaceSentinelStep,
        "weekly",
        {"vars": ["price_raw"], "sentinels": [-99]},
    ),
    Case("clip", ms.ClipStep, "weekly", {"vars": ["spend"], "lower": 10, "upper": 150}),
    # feature
    Case("fourier", ms.FourierStep, "weekly", {"along": "time", "period": 52.18, "order": 3}),
    Case(
        "fourier_days",
        ms.FourierStep,
        "daily",
        {"period": 7, "order": 2, "unit": "days", "out": "weekly_season"},
    ),
    Case("trend", ms.TrendStep, "weekly", {"along": "time", "power": 2}),
    Case(
        "calendar",
        ms.CalendarStep,
        "weekly",
        {"features": ["week_of_year", "month", "quarter", "is_month_end"]},
    ),
    Case(
        "holidays",
        ms.HolidaysStep,
        "weekly",
        {"table": "holidays", "table_along": "date"},
        tables={"holidays": "holidays"},
    ),
    Case(
        "lag",
        ms.LagStep,
        "weekly",
        {"vars": ["spend", "y"], "within": ["geo"], "lags": [1, 3]},
        must_run=("context_window_equivalence",),
    ),
    Case(
        "lag_tensor",
        ms.LagTensorStep,
        "weekly",
        {"vars": ["spend"], "along": "time", "within": ["geo"], "l_max": 4, "pad": "first"},
        must_run=("context_window_equivalence",),
    ),
    Case(
        "lag_tensor_zero",
        ms.LagTensorStep,
        "weekly",
        {"vars": ["spend"], "l_max": 3, "pad": "zero", "dim": "l"},
        must_run=("context_window_equivalence",),
    ),
    Case("one_hot", ms.OneHotStep, "weekly", {"vars": ["promo_type"], "drop_first": True}),
    Case(
        "encode_levels",
        ms.EncodeLevelsStep,
        "weekly",
        {"vars": ["promo_type"], "dims": ["geo"], "unseen_levels": "pooled"},
    ),
    Case("missing_indicator", ms.MissingIndicatorStep, "weekly", {"vars": ["price"]}),
    Case(
        "formula",
        ms.FormulaStep,
        "weekly",
        {
            "out": "spend_total",
            "expr": "log1p(sum(spend, 'channel')) * where(price > 6, 1, 0.5)",
            "dims": ["geo", "time"],
        },
    ),
    Case(
        "formula_broadcast",
        ms.FormulaStep,
        "weekly",
        {"out": "one", "expr": "1", "dims": ["geo", "time"], "units": "1"},
    ),
    # precompute
    Case(
        "adstock",
        ms.AdstockStep,
        "weekly",
        {"vars": ["spend"], "within": ["geo"], "alpha": 0.6, "l_max": 5, "normalize": True},
        must_run=("context_window_equivalence",),
    ),
    Case(
        "adstock_weibull_by_channel",
        ms.AdstockStep,
        "weekly",
        {
            "vars": ["spend"],
            "kind": "weibull_cdf",
            "lam": {"tv": 3.0, "search": 1.0, "social": 2.0},
            "k": 1.5,
            "by": "channel",
            "l_max": 6,
        },
        must_run=("context_window_equivalence",),
    ),
    Case("saturation", ms.SaturationStep, "weekly", {"vars": ["spend"], "lam": 0.01}),
    Case(
        "saturation_hill",
        ms.SaturationStep,
        "weekly",
        {
            "vars": ["spend"],
            "kind": "hill",
            "slope": 1.2,
            "half": {"tv": 100.0, "search": 80.0, "social": 60.0},
            "by": "channel",
        },
    ),
]


def test_every_step_has_a_contract_case() -> None:
    covered = {c.step for c in CASES}
    assert covered == set(ms.STEPS)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_contract(case: Case, data: Any) -> None:
    ds = getattr(data, case.data)()
    if case.prep is not None:
        ds = case.prep(ds)
    tables = {name: getattr(data, builder)() for name, builder in case.tables.items()}
    schema = infer_schema(ds, targets=[t for t in ("y",) if t in ds])
    report = check_step(case.step, ds, case.settings, schema=schema, side_tables=tables)
    assert report.ok, report.summary()
    for name in ("catalog_entry", "effect_matches_output", *case.must_run):
        result = report.get(name)
        assert result.passed and not result.skipped, report.summary()
