"""Synthetic national data with mediators and known parameters, plus the matching
``mmm.nested`` spec (tests, docs, the conformance corpus).

:func:`nested_dataset` simulates weekly spend, two mediators and sales on the structure of
mmm-framework's ``NestedMMM`` (:mod:`mapi_plugin_mmm.nested`):

* ``x = spend / max spend`` per channel, ``sat_c = 1 - exp(-lam_c * adstock(x; alpha_c))``
  (normalised geometric adstock over 8 weeks, the extension models' media transform);
* ``visits = 20000 + sum_c 6000 * b_visits[c] * sat_c + noise`` (every channel), measured
  every week;
* ``awareness = 0.3 + 0.2 * b_aware * sat_tv + noise`` (TV only), from a survey every
  fourth week (missing otherwise);
* ``sales = 4000 + g_visits * visits_effect + g_aware * awareness_effect + sum_c
  direct_c * sat_c + trend + seasonality + noise`` - the mediators' effects are linear in
  their channel-driven parts, so each channel's contribution is known.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "NESTED_CHANNELS",
    "NESTED_MEDIATORS",
    "NESTED_SPEC_YAML",
    "NestedTruth",
    "nested_dataset",
    "nested_raw_schema",
]

NESTED_CHANNELS = ("tv", "search", "social")
NESTED_MEDIATORS = ("visits", "awareness")

NESTED_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: mediated-mmm, project: growth, labels: {family: mmm}}
data:
  dataset: dataset://growth/national_mediators_weekly@latest
  bind:
    dims: {channel: channel, mediator: mediator, time: week}
    vars: {y: sales, spend: spend, mediators: mediators}
  split: {kind: time_holdout, dim: time, last: 6}
model:
  use: mmm.nested@^1
  settings:
    mediators:
      - {name: visits}
      - {name: awareness, channels: [tv], outcome_sign: positive}
    trend: {kind: linear}
    seasonality: {yearly_order: 1}
  fit: {mode: evaluate_then_refit, engine: nutpie, tune: 100, draws: 100, chains: 2, seed: 7}
outputs:
  derived: [contributions, response_curves, marginal_cost, adstock, mediation]
  ui_draws: 100
evaluate: {metrics: [coverage_90, mape, rmse]}
"""
"""A national weekly MMM with visits (every channel) and a four-weekly awareness survey
(TV only) between spend and sales."""

_L_MAX = 8


@dataclass(frozen=True, slots=True)
class NestedTruth:
    """The simulation's parameters and its weekly truth (sales units)."""

    channels: tuple[str, ...]
    alpha: np.ndarray  # (channel,) geometric decay
    lam: np.ndarray  # (channel,) logistic rate on max-normalised adstocked spend
    weekly: xr.Dataset = field(repr=False)  # direct, via (channel, mediator, week)

    def channel_contribution(self) -> xr.DataArray:
        """Total contribution per channel over the simulated weeks (direct + mediated)."""
        w = self.weekly
        return (w["direct"] + w["via"].sum("mediator")).sum("week")

    def share_mediated(self) -> xr.DataArray:
        """Share of each channel's total contribution that goes through the mediators."""
        w = self.weekly
        via = w["via"].sum(("mediator", "week"))
        return via / (via + w["direct"].sum("week"))


def _saturated(spend: np.ndarray, alpha: np.ndarray, lam: np.ndarray) -> np.ndarray:
    """``(channel, week)`` saturated media of raw ``(channel, week)`` spend."""
    x = spend / spend.max(axis=1, keepdims=True)
    w = alpha[:, None] ** np.arange(_L_MAX)[None, :]
    w = w / w.sum(axis=1, keepdims=True)
    ad = np.zeros_like(x)
    for k in range(_L_MAX):
        ad[:, k:] += x[:, : x.shape[1] - k] * w[:, k, None]
    return np.asarray(1.0 - np.exp(-lam[:, None] * ad))


def nested_dataset(*, weeks: int = 104, seed: int = 0) -> tuple[xr.Dataset, NestedTruth]:
    """A national weekly dataset ``sales(week)``, ``spend(channel, week)``,
    ``mediators(mediator, week)`` (awareness measured every fourth week) and its truth."""
    rng = np.random.default_rng(seed)
    channels = NESTED_CHANNELS
    n_c = len(channels)
    week = pd.date_range("2023-01-01", periods=weeks, freq="W-SUN").as_unit("ns")
    spend = rng.gamma(4.0, 250.0, (n_c, weeks)) * rng.binomial(1, 0.85, (n_c, weeks))
    alpha = np.array([0.6, 0.2, 0.4])
    lam = np.array([2.0, 3.0, 2.5])
    sat = _saturated(spend, alpha, lam)
    # media -> mediators (mediator units) and mediators -> sales (sales per mediator unit)
    b_visits = np.array([0.8, 1.2, 0.6]) * 6000.0
    b_aware = 0.2 * 1.0
    g_visits, g_aware = 0.25, 4000.0
    visits_drive = b_visits[:, None] * sat  # (channel, week)
    aware_drive = np.zeros_like(sat)
    aware_drive[0] = b_aware * sat[0]
    visits = 20_000.0 + visits_drive.sum(axis=0) + rng.normal(0.0, 300.0, weeks)
    awareness = 0.3 + aware_drive.sum(axis=0) + rng.normal(0.0, 0.01, weeks)
    survey = np.full(weeks, np.nan)
    survey[::4] = awareness[::4]
    direct_coef = np.array([300.0, 500.0, 0.0])
    direct = direct_coef[:, None] * sat
    via = np.stack([g_visits * visits_drive, g_aware * aware_drive], axis=1)  # (c, m, w)
    t = np.arange(weeks, dtype=float)
    sales = (
        4_000.0
        + g_visits * (visits - visits_drive.sum(axis=0))
        + g_aware * (awareness - aware_drive.sum(axis=0))
        + via.sum(axis=(0, 1))
        + direct.sum(axis=0)
        + 2.0 * t
        + 200.0 * np.sin(2 * np.pi * t / 52.0)
        + rng.normal(0.0, 60.0, weeks)
    )
    ds = xr.Dataset(
        {
            "sales": (("week",), sales, {"units": "units"}),
            "spend": (("channel", "week"), spend, {"units": "EUR"}),
            "mediators": (("mediator", "week"), np.stack([visits, survey])),
        },
        coords={"channel": list(channels), "mediator": list(NESTED_MEDIATORS), "week": week},
    )
    weekly = xr.Dataset(
        {
            "direct": (("channel", "week"), direct),
            "via": (("channel", "mediator", "week"), via),
        },
        coords={"channel": list(channels), "mediator": list(NESTED_MEDIATORS), "week": week},
    )
    return ds, NestedTruth(channels, alpha, lam, weekly)


def nested_raw_schema() -> DataSchema:
    """Raw schema of :func:`nested_dataset` (weekly ``week``, fixed ``channel`` and
    ``mediator``; the mediator series may have missing weeks)."""
    return DataSchema.model_validate(
        {
            "dims": {
                "channel": {"kind": "categorical", "policy": "fixed"},
                "mediator": {"kind": "categorical", "policy": "fixed"},
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
                "mediators": {
                    "dims": ["mediator", "week"],
                    "dtype": "float64",
                    "role": "covariate",
                    "missing": "allow",
                },
            },
        }
    )
