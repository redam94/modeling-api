# ruff: noqa: E501
"""Synthetic E33 data with known parameters, plus the matching spec (tests, docs, corpus).

:func:`e33_dataset` simulates daily local-currency spend and revenue for a few markets
and channels. The ground truth lives on the weekly EUR grid that the E33 pipeline
(fx_convert → weekly resample → mean_scale → lag_tensor → fourier → holidays) produces,
and it uses exactly the model's structure in the pipeline's scaled space, so a fit should
recover the adstock decays and saturation rates (unit-free) and the channel contributions
in EUR:

* weekly EUR spend ``S[m, c, w]``; scaled spend ``x = S / mean_w S`` (the pipeline's
  mean scaling over the full data);
* ``contrib = beta[m, c] * logistic(adstock(x; alpha_c); lam_c)``;
* ``y = intercept[m] + sum_c contrib + fourier + holiday + noise``;
* weekly revenue ``= y * revenue_scale[m]`` (EUR), spread over the days of the week and
  converted to local currency with the daily FX rate, so the pipeline's weekly EUR sums
  are exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_mmm.transforms import adstock_weights, saturate

__all__ = [
    "E33_SCORE_HORIZON_YAML",
    "E33_SPEC_YAML",
    "E33Truth",
    "e33_dataset",
    "e33_raw_schema",
    "e33_side_tables",
    "future_daily",
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
  - {id: to_eur,      use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo, currencies: {UK: GBP}, default_currency: EUR}}
  - {id: weekly,      use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y,     use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags,        use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 4, pad: first}}
  - {id: season,      use: core.fourier@^1,    with: {along: time, period: 52.18, order: 2, out: fourier}}
  - {id: hols,        use: core.holidays@^1,   with: {table: holidays, table_along: date, along: time}}
model:
  use: mmm.hierarchical_geo@^1.3
  settings:
    pooling: partial
    centred: false
    adstock: {kind: geometric, l_max: 4}
    saturation: {kind: logistic}
    priors:
      beta_channel: {distribution: HalfNormal, sigma: 1.0, dims: [channel]}
      lam:          {distribution: Gamma, alpha: 3, beta: 1, dims: [channel]}
  fit:
    mode: evaluate_then_refit
    engine: pymc
    tune: 150
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
"""The plan's §6.1 spec scaled down (``l_max: 4``, tiny ``pymc`` fit, no ``geo: fixed``
tightening so new markets can be scored)."""

E33_SCORE_HORIZON_YAML = """
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://growth/eu-geo-mmm-prod
mode: forecast
horizon: {dim: time, periods: 8}
output: {kind: expected, summary: [mean, hdi_90], draws: 50, units: original}
"""

MARKETS = ("NL", "DE", "IT", "UK")
CHANNELS = ("tv", "search", "social")
CURRENCY = {"UK": "GBP"}


@dataclass(frozen=True, slots=True)
class E33Truth:
    """The simulation's parameters (pipeline-scaled space) and EUR contributions."""

    markets: tuple[str, ...]
    channels: tuple[str, ...]
    alpha: np.ndarray  # (channel,)
    lam: np.ndarray  # (channel,)
    beta: np.ndarray  # (market, channel), in units of the market's revenue scale
    intercept: np.ndarray  # (market,)
    gamma_fourier: np.ndarray  # (4,) sin1, cos1, sin2, cos2
    gamma_holiday: float
    sigma: float
    l_max: int
    revenue_scale: np.ndarray  # (market,) EUR per model unit before mean scaling
    weekly: xr.Dataset = field(repr=False)  # weekly EUR truth: spend, revenue, contributions

    def channel_contribution_eur(self) -> xr.DataArray:
        """Total EUR contribution per (market, channel) over the simulated weeks."""
        out = self.weekly["contribution_eur"].sum("week")
        return out


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

    # weekly truth in the pipeline's scaled space (scaled by the first `weeks` weeks' means,
    # which is the production fit's scaling when the data spans `weeks` weeks)
    x = week_eur / week_eur[..., :weeks].mean(-1, keepdims=True)
    alpha = np.array([0.6, 0.2, 0.4, 0.5, 0.3][:n_c])
    lam = np.array([1.2, 2.5, 1.8, 1.5, 2.0][:n_c])
    beta_c = np.array([0.35, 0.25, 0.15, 0.2, 0.1][:n_c])
    beta = beta_c[None, :] * np.exp(0.15 * rng.standard_normal((n_m, n_c)))
    w = adstock_weights("geometric", np.arange(l_max), alpha=alpha[:, None], normalize=True)
    lagged = np.stack(
        [
            np.concatenate([np.repeat(x[..., :1], k, -1), x[..., : total_weeks - k]], -1)
            for k in range(l_max)
        ],
        axis=-1,
    )  # lag tensor with pad=first
    ad = np.einsum("mcwl,cl->mcw", lagged, w)
    contrib = beta[:, :, None] * saturate(ad, "logistic", lam=lam[None, :, None])
    intercept = rng.uniform(0.4, 0.7, n_m)
    pos = np.arange(total_weeks, dtype=float)
    fourier = np.stack(
        [
            np.sin(2 * np.pi * pos / 52.18),
            np.cos(2 * np.pi * pos / 52.18),
            np.sin(4 * np.pi * pos / 52.18),
            np.cos(4 * np.pi * pos / 52.18),
        ],
        -1,
    )
    gamma_f = np.array([0.08, -0.05, 0.03, 0.02])
    hol = np.asarray(tables["holidays"]["holiday"].values).reshape(total_weeks, 7).sum(-1)
    gamma_h = 0.04
    sigma = 0.03
    mu = intercept[:, None] + contrib.sum(1) + (fourier @ gamma_f)[None, :] + gamma_h * hol[None, :]
    y = mu + sigma * rng.standard_normal(mu.shape)
    revenue_scale = 50_000.0 * size
    week_rev = y * revenue_scale[:, None]
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
            "contribution_eur": (
                ("market", "channel", "week"),
                contrib * revenue_scale[:, None, None],
            ),
        },
        coords={"market": list(markets), "channel": list(channels), "week": week_index},
    )
    truth = E33Truth(
        markets=markets,
        channels=channels,
        alpha=alpha,
        lam=lam,
        beta=beta,
        intercept=intercept,
        gamma_fourier=gamma_f,
        gamma_holiday=gamma_h,
        sigma=sigma,
        l_max=l_max,
        revenue_scale=revenue_scale,
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
