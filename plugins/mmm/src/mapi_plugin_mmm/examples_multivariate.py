"""Synthetic weekly sales of two related products with known parameters, plus the matching
``mmm.multivariate`` spec (tests, docs, the conformance corpus).

:func:`multivariate_dataset` follows mmm-framework's ``MultivariateMMM`` structure, so the
plugin should recover the truth:

* spend ``S[c, w]`` scaled by the channel's largest weekly spend, ``x = S / max S``;
* ``sat[c, w] = 1 - exp(-lam_c * adstock(x; alpha))`` (normalised geometric adstock over
  8 weeks, one carry-over rate for every channel);
* ``premium = base_p + sum_c beta[p, c] * sat_c + season_p + e_p``;
* ``basic = base_b + sum_c beta[b, c] * sat_c + season_b + psi * premium + e_b`` - a halo:
  every extra premium sale adds ``psi`` basic sales;
* ``(e_p, e_b)`` correlated normal noise.

The true contribution of a channel to ``basic`` includes the halo of its premium sales
(``beta[b, c] * sat_c + psi * beta[p, c] * sat_c``): the reduced form the plugin reports.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "MULTIVARIATE_SPEC_YAML",
    "MultivariateTruth",
    "multivariate_dataset",
    "multivariate_raw_schema",
]

MULTIVARIATE_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: multi-kpi-mmm, project: growth, labels: {family: mmm}}
data:
  dataset: dataset://growth/product_sales_weekly@latest
  bind:
    dims: {outcome: product, channel: channel, time: week}
    vars: {y: sales, spend: spend}
  split: {kind: time_holdout, dim: time, last: 6}
model:
  use: mmm.multivariate@^1
  settings:
    outcomes:
      - {name: premium, weight: 3.0}
      - {name: basic, weight: 1.0}
    cross_effects:
      - {source: premium, target: basic, kind: halo}
    trend: {kind: linear}
    seasonality: {yearly_order: 1}
  fit: {mode: evaluate_then_refit, engine: nutpie, tune: 100, draws: 100, chains: 2, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost, adstock, cross_effects]
  ui_draws: 100
evaluate: {metrics: [coverage_90, mape, rmse]}
"""
"""Two products (premium is worth three times basic per sale) with a premium -> basic halo."""

CHANNELS = ("tv", "search", "social")
OUTCOMES = ("premium", "basic")
L_MAX = 8


@dataclass(frozen=True, slots=True)
class MultivariateTruth:
    """The simulation's parameters and its true reduced-form contributions."""

    channels: tuple[str, ...]
    outcomes: tuple[str, ...]
    alpha: float  # shared geometric decay
    lam: np.ndarray  # (channel,) logistic rate on max-normalised adstocked spend
    beta: np.ndarray  # (outcome, channel) sales at full saturation, direct effect
    base: np.ndarray  # (outcome,) sales per week
    psi: float  # premium -> basic halo (basic sales per premium sale)
    contributions: xr.DataArray  # (outcome, channel, week), halo included


def _adstock(x: np.ndarray, alpha: float) -> np.ndarray:
    """Normalised geometric adstock of ``x (C, T)`` over :data:`L_MAX` lags."""
    w = alpha ** np.arange(L_MAX, dtype=float)
    w = w / w.sum()
    out = np.zeros_like(x)
    for lag in range(L_MAX):
        out[:, lag:] += w[lag] * x[:, : x.shape[1] - lag]
    return out


def multivariate_dataset(weeks: int = 104, seed: int = 0) -> tuple[xr.Dataset, MultivariateTruth]:
    """Weekly ``sales(product, week)`` and ``spend(channel, week)`` with a premium -> basic
    halo, and the truth (see the module doc)."""
    rng = np.random.default_rng(seed)
    n_c = len(CHANNELS)
    week = pd.date_range("2023-01-01", periods=weeks, freq="W-SUN").as_unit("ns")
    spend = rng.gamma(4.0, 250.0, (n_c, weeks)) * rng.binomial(1, 0.85, (n_c, weeks))
    x = spend / (spend.max(axis=1, keepdims=True) + 1e-8)
    alpha = 0.4
    lam = np.array([2.0, 3.0, 2.5])
    beta = np.array([[400.0, 250.0, 150.0], [200.0, 300.0, 250.0]])
    base = np.array([3_000.0, 5_000.0])
    psi = 0.15
    sat = 1.0 - np.exp(-lam[:, None] * _adstock(x, alpha))  # (C, T)
    direct = beta[:, :, None] * sat[None]  # (K, C, T)
    t = np.arange(weeks, dtype=float)
    season = np.array([120.0, 80.0])[:, None] * np.sin(2 * np.pi * t / 52.0)[None]
    cov = np.array([[60.0**2, 0.4 * 60 * 50], [0.4 * 60 * 50, 50.0**2]])
    noise = rng.multivariate_normal(np.zeros(2), cov, size=weeks).T  # (K, T)
    premium = base[0] + direct[0].sum(0) + season[0] + noise[0]
    basic = base[1] + direct[1].sum(0) + season[1] + psi * premium + noise[1]
    contrib = direct.copy()
    contrib[1] += psi * direct[0]
    ds = xr.Dataset(
        {
            "sales": (("product", "week"), np.stack([premium, basic]), {"units": "units"}),
            "spend": (("channel", "week"), spend, {"units": "EUR"}),
        },
        coords={"product": list(OUTCOMES), "channel": list(CHANNELS), "week": week},
    )
    truth = MultivariateTruth(
        channels=CHANNELS,
        outcomes=OUTCOMES,
        alpha=alpha,
        lam=lam,
        beta=beta,
        base=base,
        psi=psi,
        contributions=xr.DataArray(
            contrib,
            dims=["product", "channel", "week"],
            coords={"product": list(OUTCOMES), "channel": list(CHANNELS), "week": week},
        ),
    )
    return ds, truth


def multivariate_raw_schema() -> DataSchema:
    """Raw schema of :func:`multivariate_dataset` (weekly ``week``, fixed ``channel`` and
    ``product``)."""
    return DataSchema.model_validate(
        {
            "dims": {
                "product": {"kind": "categorical", "policy": "fixed"},
                "channel": {"kind": "categorical", "policy": "fixed"},
                "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            },
            "vars": {
                "sales": {
                    "dims": ["product", "week"],
                    "dtype": "float64",
                    "role": "target",
                    "units": "units",
                },
                "spend": {
                    "dims": ["channel", "week"],
                    "dtype": "float64",
                    "role": "spend",
                    "units": "EUR",
                    "future_required": True,
                },
            },
        }
    )
