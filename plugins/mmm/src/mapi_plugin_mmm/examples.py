# ruff: noqa: E501
"""Synthetic geo marketing-mix data with known parameters, plus the matching spec (tests,
docs, the conformance corpus).

:func:`e33_dataset` simulates daily local-currency spend and revenue for a few markets and
channels (the plan's E33 example). The ground truth lives on the weekly EUR grid the
example pipeline (``fx_convert`` → weekly ``resample`` → ``holidays``) produces, and it
uses mmm-framework's own structure, so ``mmm.bayesian`` should recover the channel
contributions in EUR:

* weekly EUR spend ``S[m, c, w]``, normalised by the channel's largest weekly spend in any
  market over the fitted weeks: ``x = S / max S`` (mmm-framework's media scaling);
* ``contrib[m, c, w] = beta[m, c] * (1 - exp(-lam_c * adstock(x; alpha_c)))`` in EUR
  (normalised geometric adstock over ``l_max`` weeks, logistic saturation);
* weekly revenue ``= base[m] + sum_c contrib + seasonality + holiday effect + noise``,
  spread over the days of the week and converted to local currency with the daily FX rate,
  so the pipeline's weekly EUR sums are exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "CHANNELS",
    "E33_SCORE_HORIZON_YAML",
    "E33_SPEC_YAML",
    "MARKETS",
    "NATIONAL_SPEC_YAML",
    "E33Truth",
    "e33_dataset",
    "e33_raw_schema",
    "e33_side_tables",
    "future_daily",
    "national_dataset",
    "national_raw_schema",
]

E33_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: eu-geo-mmm, project: growth, labels: {family: mmm}}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx:       {ref: dataset://growth/fx_daily@latest, score_policy: supplied}
    holidays: {ref: dataset://shared/holidays_eu@v3, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}
pipeline:
  - {id: to_eur,  use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo, currencies: {UK: GBP}, default_currency: EUR}}
  - {id: weekly,  use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: hols,    use: core.holidays@^1,   with: {table: holidays, table_along: date, along: time}}
model:
  use: mmm.bayesian@^1
  settings:
    hierarchy: {pool_over: [geo]}
    adstock: {kind: geometric, l_max: 4}
    saturation: {kind: logistic}
    trend: {kind: none}
    seasonality: {yearly_order: 2}
  fit:
    mode: evaluate_then_refit
    engine: pymc
    tune: 100
    draws: 100
    chains: 2
    target_accept: 0.9
    seed: 20261014
outputs:
  derived: [contributions, response_curves, marginal_cost]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.response_curves]
  ui_draws: 100
evaluate: {metrics: [coverage_90, mape, rmse]}
execution: {resource_class: M, timeout: 45m}
"""
"""The plan's §6.1 example on mmm-framework (the model owns scaling and carry-over, so the
pipeline only converts currencies, aggregates to weeks and adds the holiday control; tiny
``pymc`` fit)."""

E33_SCORE_HORIZON_YAML = """
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://growth/eu-geo-mmm-prod
mode: forecast
horizon: {dim: time, periods: 8}
output: {kind: expected, summary: [mean, hdi_90], draws: 50, units: original}
"""

NATIONAL_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: national-mmm, project: growth, labels: {family: mmm}}
data:
  dataset: dataset://growth/national_weekly@latest
  bind:
    dims: {channel: channel, time: week}
    vars: {y: sales, spend: spend}
  split: {kind: time_holdout, dim: time, last: 6}
model:
  use: mmm.bayesian@^1
  settings:
    adstock: {kind: geometric, l_max: 4}
    saturation: {kind: logistic}
    trend: {kind: linear}
    seasonality: {yearly_order: 1}
  fit: {mode: evaluate_then_refit, engine: pymc, tune: 100, draws: 100, chains: 2, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost]
  ui_draws: 100
evaluate: {metrics: [coverage_90, mape, rmse]}
"""
"""A national weekly MMM (no pipeline): the smallest mmm-framework example."""

MARKETS = ("NL", "DE", "IT", "UK")
CHANNELS = ("tv", "search", "social")
CURRENCY = {"UK": "GBP"}


@dataclass(frozen=True, slots=True)
class E33Truth:
    """The simulation's parameters and its weekly EUR truth."""

    markets: tuple[str, ...]
    channels: tuple[str, ...]
    alpha: np.ndarray  # (channel,) geometric decay
    lam: np.ndarray  # (channel,) logistic rate on max-normalised adstocked spend
    beta: np.ndarray  # (market, channel) EUR at full saturation
    base: np.ndarray  # (market,) EUR per week
    l_max: int
    weekly: xr.Dataset = field(repr=False)  # spend_eur, revenue_eur, contribution_eur

    def channel_contribution_eur(self) -> xr.DataArray:
        """Total EUR contribution per (market, channel) over the simulated weeks."""
        return self.weekly["contribution_eur"].sum("week")


def _fx_table(dates: pd.DatetimeIndex) -> xr.Dataset:
    # a function of the calendar date only, so every table slice agrees on a day's rate
    t = np.asarray((dates - pd.Timestamp("2000-01-01")).days, dtype=float)
    rate = 1.15 + 0.03 * np.sin(t / 45.0)  # EUR per GBP
    return xr.Dataset(
        {"rate": (("date", "currency"), rate[:, None])},
        coords={"date": dates, "currency": ["GBP"]},
    )


_HOLIDAYS = {(1, 1), (4, 1), (5, 1), (12, 25), (12, 26)}


def _holiday_table(dates: pd.DatetimeIndex) -> xr.Dataset:
    flag = np.array([float((d.month, d.day) in _HOLIDAYS) for d in dates])
    return xr.Dataset({"holiday": (("date",), flag)}, coords={"date": dates})


def e33_side_tables(start: str = "2022-12-01", days: int = 1200) -> dict[str, xr.Dataset]:
    """Daily FX (``rate(date, currency)``, EUR per GBP) and holiday tables covering the data
    and a forecast horizon."""
    dates = pd.date_range(start, periods=days, freq="D").as_unit("ns")
    return {"fx": _fx_table(dates), "holidays": _holiday_table(dates)}


def _geometric(alpha: np.ndarray, l_max: int) -> np.ndarray:
    """``(channel, lag)`` normalised geometric weights."""
    w = alpha[:, None] ** np.arange(l_max)[None, :]
    return w / w.sum(axis=1, keepdims=True)


def _adstock(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """``x (m, c, t)`` carried over with ``w (c, l)`` (zero history before the start)."""
    out = np.zeros_like(x)
    for k in range(w.shape[1]):
        out[..., k:] += x[..., : x.shape[-1] - k] * w[None, :, k, None]
    return out


def _simulate(
    *,
    markets: tuple[str, ...],
    channels: tuple[str, ...],
    weeks: int,
    total_weeks: int,
    seed: int,
    start: str,
    l_max: int,
) -> tuple[xr.Dataset, E33Truth]:
    rng = np.random.default_rng(seed)
    n_m, n_c = len(markets), len(channels)
    dates = pd.date_range(start, periods=7 * total_weeks, freq="D").as_unit("ns")
    tables = e33_side_tables(start=str(dates[0].date()), days=dates.size)
    rate = np.asarray(tables["fx"]["rate"].sel(currency="GBP").values)
    fx = np.ones((n_m, dates.size))
    for i, m in enumerate(markets):
        if CURRENCY.get(m) == "GBP":
            fx[i] = rate

    # daily spend in EUR: channel level x market size x weekly flighting x daily noise
    size = rng.uniform(0.6, 1.6, n_m)
    level = np.array([900.0, 450.0, 300.0, 600.0, 350.0][:n_c])
    on = rng.uniform(0.3, 1.7, (n_m, n_c, total_weeks))
    on[:, 0, :] *= rng.binomial(1, 0.7, (n_m, total_weeks))  # tv flights on and off
    daily_eur = (
        size[:, None, None, None]
        * level[None, :, None, None]
        * on[..., None]
        * rng.gamma(20.0, 1 / 20.0, (n_m, n_c, total_weeks, 7))
    ).reshape(n_m, n_c, -1)
    spend_local = daily_eur / fx[:, None, :]
    week_eur = daily_eur.reshape(n_m, n_c, total_weeks, 7).sum(-1)

    x = week_eur / week_eur[..., :weeks].max(axis=(0, 2), keepdims=True)
    alpha = np.array([0.6, 0.2, 0.4, 0.5, 0.3][:n_c])
    lam = np.array([1.5, 3.0, 2.0, 1.8, 2.5][:n_c])
    beta_eur = np.array([60_000.0, 35_000.0, 20_000.0, 30_000.0, 15_000.0][:n_c])
    beta = beta_eur[None, :] * size[:, None] * np.exp(0.1 * rng.standard_normal((n_m, n_c)))
    ad = _adstock(x, _geometric(alpha, l_max))
    contrib = beta[:, :, None] * (1.0 - np.exp(-lam[None, :, None] * ad))
    base = 150_000.0 * size
    pos = np.arange(total_weeks, dtype=float)
    season = 8_000.0 * np.sin(2 * np.pi * pos / 52.0) - 5_000.0 * np.cos(2 * np.pi * pos / 52.0)
    hol = np.asarray(tables["holidays"]["holiday"].values).reshape(total_weeks, 7).sum(-1)
    noise = 4_000.0 * rng.standard_normal((n_m, total_weeks))
    week_rev = (
        base[:, None]
        + contrib.sum(1)
        + size[:, None] * (season[None, :] + 6_000.0 * hol[None, :])
        + noise
    )
    daily_rev_eur = np.repeat(week_rev / 7.0, 7, axis=-1)
    revenue_local = daily_rev_eur / fx

    data = xr.Dataset(
        {
            "revenue_local": (("market", "date"), revenue_local, {"units": "LCU"}),
            "spend_local": (("market", "channel", "date"), spend_local, {"units": "LCU"}),
        },
        coords={"market": list(markets), "channel": list(channels), "date": dates},
    )
    week_index = pd.date_range(dates[6], periods=total_weeks, freq="W-SUN").as_unit("ns")
    weekly = xr.Dataset(
        {
            "spend_eur": (("market", "channel", "week"), week_eur),
            "revenue_eur": (("market", "week"), week_rev),
            "contribution_eur": (("market", "channel", "week"), contrib),
        },
        coords={"market": list(markets), "channel": list(channels), "week": week_index},
    )
    truth = E33Truth(
        markets=markets,
        channels=channels,
        alpha=alpha,
        lam=lam,
        beta=beta,
        base=base,
        l_max=l_max,
        weekly=weekly,
    )
    return data, truth


def e33_dataset(
    *,
    markets: tuple[str, ...] = MARKETS,
    channels: tuple[str, ...] = CHANNELS,
    weeks: int = 104,
    future_weeks: int = 0,
    seed: int = 0,
    start: str = "2023-01-02",
    l_max: int = 4,
) -> tuple[xr.Dataset, E33Truth]:
    """Daily local-currency E33 data (``weeks`` full W-SUN weeks from Monday ``start``).

    ``future_weeks`` extends the simulation (same draws) beyond the returned data; use
    :func:`future_daily` to get those rows for forecast and scenario requests.
    """
    if pd.Timestamp(start).dayofweek != 0:
        raise ValueError("start must be a Monday so the data holds whole W-SUN weeks")
    data, truth = _simulate(
        markets=markets,
        channels=channels,
        weeks=weeks,
        total_weeks=weeks + future_weeks,
        seed=seed,
        start=start,
        l_max=l_max,
    )
    return data.isel(date=slice(0, 7 * weeks)), truth


def future_daily(
    *,
    weeks: int,
    future_weeks: int,
    markets: tuple[str, ...] = MARKETS,
    channels: tuple[str, ...] = CHANNELS,
    seed: int = 0,
    start: str = "2023-01-02",
    l_max: int = 4,
    with_target: bool = False,
) -> xr.Dataset:
    """The ``future_weeks`` daily rows after an :func:`e33_dataset` of ``weeks`` weeks."""
    data, _ = _simulate(
        markets=markets,
        channels=channels,
        weeks=weeks,
        total_weeks=weeks + future_weeks,
        seed=seed,
        start=start,
        l_max=l_max,
    )
    out = data.isel(date=slice(7 * weeks, None))
    return out if with_target else out.drop_vars("revenue_local")


def e33_raw_schema(data: xr.Dataset) -> DataSchema:
    """Raw schema: ``market`` new_levels (hyperprior), ``channel`` fixed, daily ``date``."""
    return DataSchema.model_validate(
        {
            "dims": {
                "market": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "hyperprior",
                },
                "channel": {"kind": "categorical", "policy": "fixed"},
                "date": {"kind": "time", "policy": "extend", "freq": "D"},
            },
            "vars": {
                "revenue_local": {
                    "dims": ["market", "date"],
                    "dtype": "float64",
                    "role": "target",
                    "units": "LCU",
                },
                "spend_local": {
                    "dims": ["market", "channel", "date"],
                    "dtype": "float64",
                    "role": "spend",
                    "units": "LCU",
                    "future_required": True,
                },
            },
        }
    )


def national_dataset(
    *, weeks: int = 78, channels: tuple[str, ...] = ("tv", "search"), seed: int = 0
) -> tuple[xr.Dataset, xr.DataArray]:
    """A national weekly dataset (``sales(week)``, ``spend(channel, week)``, ``price(week)``)
    and its true channel contributions ``(channel, week)``."""
    rng = np.random.default_rng(seed)
    n_c = len(channels)
    week = pd.date_range("2023-01-01", periods=weeks, freq="W-SUN").as_unit("ns")
    spend = rng.gamma(4.0, 250.0, (n_c, weeks)) * rng.binomial(1, 0.85, (n_c, weeks))
    x = spend / spend.max(axis=1, keepdims=True)
    alpha = np.array([0.5, 0.2, 0.35][:n_c])
    lam = np.array([2.0, 3.0, 2.5][:n_c])
    beta = np.array([900.0, 600.0, 400.0][:n_c])
    ad = _adstock(x[None], _geometric(alpha, 4))[0]
    contrib = beta[:, None] * (1.0 - np.exp(-lam[:, None] * ad))
    price = 10.0 + rng.normal(0.0, 0.5, weeks)
    t = np.arange(weeks, dtype=float)
    sales = (
        5_000.0
        + 3.0 * t
        + 150.0 * np.sin(2 * np.pi * t / 52.0)
        - 80.0 * (price - 10.0)
        + contrib.sum(0)
        + rng.normal(0.0, 40.0, weeks)
    )
    ds = xr.Dataset(
        {
            "sales": (("week",), sales, {"units": "units"}),
            "spend": (("channel", "week"), spend, {"units": "EUR"}),
            "price": (("week",), price),
        },
        coords={"channel": list(channels), "week": week},
    )
    truth = xr.DataArray(
        contrib, dims=["channel", "week"], coords={"channel": list(channels), "week": week}
    )
    return ds, truth


def national_raw_schema() -> DataSchema:
    """Raw schema of :func:`national_dataset` (weekly ``week``, fixed ``channel``)."""
    return DataSchema.model_validate(
        {
            "dims": {
                "channel": {"kind": "categorical", "policy": "fixed"},
                "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            },
            "vars": {
                "sales": {"dims": ["week"], "dtype": "float64", "role": "target", "units": "units"},
                "spend": {
                    "dims": ["channel", "week"],
                    "dtype": "float64",
                    "role": "spend",
                    "units": "EUR",
                    "future_required": True,
                },
                "price": {"dims": ["week"], "dtype": "float64", "role": "control"},
            },
        }
    )
