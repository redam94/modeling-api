"""E33-like end to end: daily local-currency spend (market × channel) → fx_convert →
resample weekly → mean_scale → lag_tensor → fourier → holidays, through static validation,
pipeline fit/replay with per-group context tails, a new market with pooled fallback, and the
full fit/score harness with a toy model."""

# ruff: noqa: E501

from __future__ import annotations

import json
import tempfile
from typing import Any

import mapi_steps
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.bundle import Bundle
from mapi_sdk.context import drop_context
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.pipeline import Pipeline
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import toy
from mapi_spec import DataSchema, ModelSpec, load_model_spec, load_score_request
from mapi_spec.validate import validate_spec

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: e33-core, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx:       {ref: dataset://growth/fx_daily@latest, score_policy: pinned}
    holidays: {ref: dataset://shared/holidays_eu@v3, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: to_eur,      use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo, currencies: {UK: GBP}, default_currency: EUR}}
  - {id: weekly,      use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y,     use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags,        use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 4, pad: first}}
  - {id: season,      use: core.fourier@^1,    with: {along: time, period: 52.18, order: 2}}
  - {id: hols,        use: core.holidays@^1,   with: {table: holidays, table_along: date, along: time}}
model:
  use: __MODEL__
  fit: {mode: evaluate_then_refit, seed: 7, draws: 40, chains: 2}
evaluate: {metrics: [rmse, mape]}
"""

BIND = {
    "dims": {"geo": "market", "channel": "channel", "time": "date"},
    "vars": {"y": "revenue_local", "spend": "spend_local"},
}
WEEKS = 26
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/e33\n"


def _raw(days: int = 7 * WEEKS, start: str = "2024-01-01", seed: int = 0) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=days, freq="D").as_unit("ns")
    markets, channels = ["NL", "DE", "UK"], ["tv", "search", "social"]
    spend = rng.gamma(2.0, 30.0, size=(3, 3, days))
    spend[2] *= 0.85  # UK spends in GBP
    revenue = 200 + np.einsum("mcd,c->md", spend, [0.9, 1.4, 0.6])
    revenue *= 1 + 0.03 * rng.standard_normal(revenue.shape)
    return xr.Dataset(
        {
            "revenue_local": (("market", "date"), revenue, {"units": "LCU"}),
            "spend_local": (("market", "channel", "date"), spend, {"units": "LCU"}),
        },
        coords={"market": markets, "channel": channels, "date": dates},
    )


def _tables(data: Any) -> dict[str, xr.Dataset]:
    return {"fx": data.fx(), "holidays": data.holidays()}


@pytest.fixture
def registry() -> Registry:
    return mapi_steps.registry(toy.toy_registry())


@pytest.fixture
def raw_schema() -> DataSchema:
    schema = infer_schema(_raw(), targets=["revenue_local"], policies={"market": "new_levels"})
    market = schema.dims["market"].model_copy(update={"new_level_strategy": "pooled"})
    return schema.model_copy(update={"dims": {**schema.dims, "market": market}})


def _spec(model: str = "toy.linear@^1") -> ModelSpec:
    return load_model_spec(SPEC.replace("__MODEL__", model))


def _pipeline(registry: Registry, raw_schema: DataSchema) -> Pipeline:
    spec = _spec()
    resolved = validate_spec(spec, registry.catalog(), raw_schema).raise_for_errors()
    return Pipeline.from_spec(spec, registry, resolved)


def _future(weeks: int = 4, *, extra_market: bool = False) -> xr.Dataset:
    """Daily spend for the ``weeks`` after the fitted range (no revenue)."""
    nxt = _raw(days=7 * (WEEKS + weeks), seed=0).isel(date=slice(7 * WEEKS, None))
    out = nxt.drop_vars("revenue_local")
    if extra_market:
        fr = out.sel(market=["NL"]).assign_coords(market=["FR"]) * 1.1
        out = xr.concat([out, fr], dim="market")
    return out


# ------------------------------------------------------------------------- static checks


def test_spec_validates_statically_against_the_real_catalog(
    registry: Registry, raw_schema: DataSchema
) -> None:
    res = validate_spec(_spec(), registry.catalog(), raw_schema)
    assert res.ok, res.issues
    rs = res.raise_for_errors()
    assert rs.split_stage == "weekly"
    st = rs.stages
    assert st["to_eur"] is not None and st["to_eur"].vars["spend"].units == "EUR"
    assert st["weekly"] is not None and st["weekly"].dims["time"].freq == "W-SUN"
    assert st["scale_y"] is not None and st["scale_y"].vars["y"].units == "1"
    assert st["lags"] is not None and st["lags"].vars["spend"].dims == (
        "geo",
        "channel",
        "time",
        "lag",
    )
    assert st["hols"] is not None and st["hols"].vars["holiday"].role.value == "control"
    lags = next(s for s in rs.steps if s.id == "lags")
    assert lags.context is not None and lags.context.n == 4 and lags.context.within == ["geo"]
    assert not lags.depends_on_target


# ------------------------------------------------------------------ fit / replay / context


def test_context_tails_are_recorded_per_step_at_the_input_stage(
    registry: Registry, raw_schema: DataSchema, data: Any
) -> None:
    pipe = _pipeline(registry, raw_schema)
    raw = BindingMap(BIND).apply(_raw())
    trace = pipe.fit(raw, side_tables=_tables(data), keep_stages=True)
    ctx = trace.states["lags"].context
    assert ctx is not None and "spend" in ctx.data_vars
    weekly_labels = trace.stages["scale_y"]["time"].values
    np.testing.assert_array_equal(ctx["time"].values, weekly_labels[-4:])
    # the tail is stored at the lag step's *input* stage: weekly, EUR, mean-scaled
    xr.testing.assert_allclose(
        ctx["spend"], trace.stages["scale_y"]["spend"].isel(time=slice(-4, None))
    )
    assert trace.states["season"].context is None
    assert trace.states["scale_spend"].arrays is not None
    assert set(trace.states["scale_spend"].array("scale__spend").dims) == {"geo", "channel"}
    # full_periods_only: 26 full weeks; the first Sunday label is 2024-01-07
    assert trace.data.sizes["time"] == WEEKS
    assert str(trace.data["time"].values[0])[:10] == "2024-01-07"


def test_replay_with_context_equals_transform_of_the_full_series(
    registry: Registry, raw_schema: DataSchema, data: Any
) -> None:
    pipe = _pipeline(registry, raw_schema)
    bind = BindingMap(BIND)
    fit_data = bind.apply(_raw())
    tables = _tables(data)
    states = pipe.fit(fit_data, side_tables=tables).states
    future = bind.apply(_future(), missing="ignore")
    replay = pipe.transform(future, states, side_tables=tables)
    assert {m.step_id for m in replay.marks} == {"lags"}
    got = drop_context(replay.data, replay.marks)
    full = xr.concat([fit_data.drop_vars("y"), future], dim="time")
    reference = pipe.transform(full, states, side_tables=tables, replay=False).data
    want = reference.sel(time=got["time"].values)
    assert got.sizes["time"] == 4
    for v in ("spend", "fourier", "holiday"):
        xr.testing.assert_allclose(got[v], want[v].transpose(*got[v].dims))
    # the first forecast week's lags come from the fitted tail, not from padding
    first = got["spend"].isel(time=0)
    assert not np.allclose(first.sel(lag=1), first.sel(lag=0))


def test_new_market_uses_pooled_scale_and_pads_its_lag_history(
    registry: Registry, raw_schema: DataSchema, data: Any
) -> None:
    pipe = _pipeline(registry, raw_schema)
    bind = BindingMap(BIND)
    tables = _tables(data)
    trace = pipe.fit(bind.apply(_raw()), side_tables=tables)
    future = bind.apply(_future(extra_market=True), missing="ignore")
    replay = pipe.transform(future, trace.states, side_tables=tables, keep_stages=True)
    out = drop_context(replay.data, replay.marks)
    assert "FR" in out["geo"].values
    fr = out["spend"].sel(geo="FR")
    assert bool(np.isfinite(fr).all())
    # pooled fallback: FR is divided by the mean of the fitted per-market scales
    pooled = trace.states["scale_spend"].array("scale__spend").mean("geo")
    weekly_eur = drop_context(replay.stages["weekly"], replay.marks)["spend"].sel(geo="FR")
    scaled = drop_context(replay.stages["scale_spend"], replay.marks)["spend"].sel(geo="FR")
    xr.testing.assert_allclose(scaled, (weekly_eur / pooled).transpose(*scaled.dims))
    # no fitted history for FR: lags before its first week are padded with its first value
    first = fr.isel(time=0)
    for k in (1, 2, 3):
        xr.testing.assert_allclose(first.sel(lag=k, drop=True), first.sel(lag=0, drop=True))


# ---------------------------------------------------------------------- harness end to end


@pytest.mark.integration
def test_fit_spec_and_forecast_with_a_new_market(
    registry: Registry, raw_schema: DataSchema, data: Any
) -> None:
    bundle = fit_spec(_spec(), _raw(), _tables(data), registry, raw_schema=raw_schema)
    assert bundle.evaluation is not None
    assert set(bundle.metrics.values) == {"rmse", "mape"}
    assert bundle.metrics.output_space == "weekly"
    assert bundle.states["lags"].context is not None
    request = load_score_request(REQ + "mode: forecast\nhorizon: {dim: time, periods: 4}\n")
    out = score_bundle(
        bundle, request, _future(extra_market=True), registry=registry, return_full=True
    )
    pred = out["predictions"].to_dataset()["y_hat"]
    assert set(pred.dims) == {"market", "date"} and pred.sizes["date"] == 4
    assert bool(np.isfinite(pred).all())
    assert pred.attrs["units"] == "EUR" and pred.attrs["output_space"] == "weekly"
    issues = json.loads(out.attrs["issues"])
    assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)
    # the bundle round-trips (states, context tails, side tables) and scores identically
    with tempfile.TemporaryDirectory() as tmp:
        bundle.write(tmp)
        back = Bundle.read(tmp, registry)
    again = score_bundle(
        back, request, _future(extra_market=True), registry=registry, return_full=True
    )
    xr.testing.assert_allclose(
        again["predictions"].to_dataset()["y_hat"], out["predictions"].to_dataset()["y_hat"]
    )


@pytest.mark.integration
def test_weekly_plan_enters_after_resample(
    registry: Registry, raw_schema: DataSchema, data: Any
) -> None:
    """A weekly EUR plan enters at the ``weekly`` stage, skipping fx and resampling."""
    bundle = fit_spec(_spec(), _raw(), _tables(data), registry, raw_schema=raw_schema)
    last = bundle.fit_coords["weekly"]["time"].values[-1]
    weeks = np.array([last + np.timedelta64(7 * k, "D") for k in range(1, 4)])
    plan = xr.Dataset(
        {"spend": (("geo", "channel", "time"), np.full((3, 3, 3), 250.0))},
        coords={"geo": ["NL", "DE", "UK"], "channel": ["tv", "search", "social"], "time": weeks},
    )
    request = load_score_request(
        REQ
        + "mode: forecast\nhorizon: {dim: time, periods: 3}\ninputs_stage: weekly\noutput: {names: canonical}\n"
    )
    out = score_bundle(bundle, request, plan, registry=registry, return_full=True)
    pred = out["predictions"].to_dataset()["y_hat"]
    assert pred.sizes["time"] == 3 and bool(np.isfinite(pred).all())
    assert pred.attrs["output_space"] == "weekly"
