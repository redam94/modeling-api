"""Synthetic panels and example specs of ``causal.synthetic_control`` and ``causal.did``.

* :func:`geo_lift_dataset` - weekly ``sales(geo, week)`` of markets that share a yearly
  cycle and two slow trends; each treated market is a convex mix of a few untreated markets
  (the donors) plus noise, and its sales rise by ``lift`` during the test weeks.
  :class:`GeoTruth` carries the true donor weights, counterfactual and effect.
* :func:`did_dataset` - monthly ``sales(store, month)`` with store and month effects and a
  ``treatment(store, month)`` indicator (1 from adoption on); adoption is common (one date)
  or staggered (three cohorts). :class:`DiDTruth` carries the true effect per cell.

They double as test fixtures, conformance-corpus generators and documentation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "DID_SCHEMA",
    "DID_SPEC_YAML",
    "DID_START_DATE",
    "GEO_LIFT_SCHEMA",
    "GEO_LIFT_SPEC_YAML",
    "GEO_START_DATE",
    "DiDTruth",
    "GeoTruth",
    "did_dataset",
    "geo_lift_dataset",
]

GEO_START_DATE = "2023-01-02"
"""The first week of :func:`geo_lift_dataset` (a Monday)."""
DID_START_DATE = "2023-01-01"
"""The first month of :func:`did_dataset`."""

GEO_LIFT_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "geo": {"kind": "categorical", "policy": "fixed"},
            "week": {"kind": "time", "policy": "extend", "freq": "W-MON"},
        },
        "vars": {
            "sales": {
                "dims": ["geo", "week"],
                "dtype": "float64",
                "role": "target",
                "units": "units",
                "missing": "allow",
            }
        },
    }
)

DID_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "store": {"kind": "categorical", "policy": "fixed"},
            "month": {"kind": "time", "policy": "fixed", "freq": "MS"},
        },
        "vars": {
            "sales": {
                "dims": ["store", "month"],
                "dtype": "float64",
                "role": "target",
                "units": "units",
                "missing": "allow",
            },
            "treatment": {
                "dims": ["store", "month"],
                "dtype": "float64",
                "role": "covariate",
                "bounds": [0.0, 1.0],
            },
        },
    }
)


def _iso(value: np.datetime64 | pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _weeks(n: int) -> pd.DatetimeIndex:
    return pd.date_range(GEO_START_DATE, periods=n, freq="W-MON").as_unit("ns")


def _months(n: int) -> pd.DatetimeIndex:
    return pd.date_range(DID_START_DATE, periods=n, freq="MS").as_unit("ns")


GEO_LIFT_SPEC_YAML = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: geo-lift-test, project: marketing}}
data:
  dataset: dataset://marketing/weekly_sales_by_geo@latest
  bind:
    dims: {{unit: geo, time: week}}
    vars: {{y: sales}}
model:
  use: causal.synthetic_control@^1
  settings:
    treated: [geo_03]
    intervention_start: "{_iso(_weeks(78)[60])}"
    intervention_end: "{_iso(_weeks(78)[71])}"
  fit: {{mode: train_only, seed: 7, tune: 500, draws: 500, chains: 4}}
outputs: {{plots: [causal.impact, causal.donor_weights, causal.pre_fit, core.convergence]}}
"""
"""The example spec on :func:`geo_lift_dataset` with its defaults (test weeks 60 to 71)."""

DID_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: store-remodel-did, project: retail}
data:
  dataset: dataset://retail/monthly_sales_by_store@latest
  bind:
    dims: {unit: store, time: month}
    vars: {y: sales}
model:
  use: causal.did@^1
  settings: {design: event_study, leads: 4, lags: 6}
  fit: {mode: train_only, seed: 7, tune: 500, draws: 500, chains: 4}
outputs: {plots: [causal.event_study, causal.impact, causal.pre_fit, core.convergence]}
"""
"""The example spec on :func:`did_dataset` (an event study with 4 leads and 6 lags)."""


# ======================================================================================= geo lift


@dataclass(frozen=True, slots=True)
class GeoTruth:
    """The generating values of :func:`geo_lift_dataset`."""

    treated: tuple[str, ...]
    start: str
    """ISO date of the first test week."""
    end: str
    """ISO date of the last test week."""
    lift: float
    weights: dict[str, dict[str, float]]
    """``{treated: {donor: weight}}`` (each row sums to 1; donors not listed have weight 0)."""
    counterfactual: np.ndarray
    """``(treated, weeks)`` sales without the test (noise included)."""
    effect: np.ndarray
    """``(treated, weeks)`` true effect (0 outside the test weeks)."""

    @property
    def effect_total(self) -> float:
        return float(self.effect.sum())


def geo_lift_dataset(
    geos: int = 12,
    weeks: int = 78,
    treated: Sequence[str] = ("geo_03",),
    start_week: int = 60,
    lift: float = 0.08,
    seed: int = 0,
    *,
    test_weeks: int | None = 12,
) -> tuple[xr.Dataset, GeoTruth]:
    """Weekly ``sales(geo, week)`` of ``geos`` markets ``geo_00``, ``geo_01``, ...

    An untreated market is ``base·(1 + a·season + b·trend_1 + c·trend_2) + noise`` with its
    own base level and loadings. A treated market is a convex mix of three random untreated
    markets (noise-free) plus noise, times ``1 + lift`` during the test: ``test_weeks`` weeks
    from ``start_week`` (0-based position; ``None``: until the end of the data).
    """
    labels = [f"geo_{i:02d}" for i in range(geos)]
    treated = tuple(treated)
    if not treated or not set(treated) <= set(labels):
        raise ValueError(f"treated must be labels among {labels[0]}..{labels[-1]}")
    donors = [g for g in labels if g not in treated]
    if len(donors) < 3:
        raise ValueError("geo_lift_dataset needs at least 3 untreated markets")
    if not 3 <= start_week < weeks:
        raise ValueError("start_week must leave at least 3 weeks before it and 1 after it")
    end_week = weeks - 1 if test_weeks is None else min(weeks - 1, start_week + test_weeks - 1)
    rng = np.random.default_rng(seed)
    t = np.arange(weeks, dtype=float)
    season = np.sin(2 * np.pi * t / 52.18)
    trend_1 = np.cumsum(rng.normal(0, 0.02, weeks))
    trend_2 = np.cumsum(rng.normal(0, 0.02, weeks))
    clean: dict[str, np.ndarray] = {}
    sales: dict[str, np.ndarray] = {}
    for g in donors:
        base = rng.uniform(80, 160)
        a, b, c = rng.uniform(0.05, 0.25), rng.normal(0, 1), rng.normal(0, 1)
        clean[g] = base * (1 + a * season + b * trend_1 + c * trend_2)
        sales[g] = clean[g] + rng.normal(0, 0.015 * base, weeks)
    in_test = (t >= start_week) & (t <= end_week)
    weights: dict[str, dict[str, float]] = {}
    cf_rows, eff_rows = [], []
    for g in treated:
        pick = rng.choice(len(donors), size=3, replace=False)
        w = rng.dirichlet(np.full(3, 3.0))
        weights[g] = {donors[i]: float(wi) for i, wi in zip(pick, w, strict=True)}
        mix = sum(wi * clean[donors[i]] for i, wi in zip(pick, w, strict=True))
        cf = mix + rng.normal(0, 0.015 * float(np.mean(mix)), weeks)
        eff = np.where(in_test, lift * cf, 0.0)
        sales[g] = cf + eff
        cf_rows.append(cf)
        eff_rows.append(eff)
    week = _weeks(weeks)
    ds = xr.Dataset(
        {
            "sales": (
                ("geo", "week"),
                np.stack([sales[g] for g in labels]),
                {"units": "units"},
            )
        },
        coords={"geo": labels, "week": week},
        attrs={"mapi_schema": GEO_LIFT_SCHEMA.model_dump_json()},
    )
    truth = GeoTruth(
        treated=treated,
        start=_iso(week[start_week]),
        end=_iso(week[end_week]),
        lift=lift,
        weights=weights,
        counterfactual=np.stack(cf_rows),
        effect=np.stack(eff_rows),
    )
    return ds, truth


# ======================================================================================= DiD


@dataclass(frozen=True, slots=True)
class DiDTruth:
    """The generating values of :func:`did_dataset`."""

    effect: float
    """The effect of the treatment on every treated cell (units of sales)."""
    adoption: dict[str, str | None]
    """``{store: ISO date of the first treated month | None (never treated)}``."""
    cell_effect: np.ndarray = field(repr=False)
    """``(store, month)`` true effect (0 on untreated cells)."""

    @property
    def att(self) -> float:
        """The average effect over the treated cells."""
        treated = self.cell_effect != 0
        return float(self.cell_effect[treated].mean()) if treated.any() else 0.0


def did_dataset(
    units: int = 30,
    periods: int = 24,
    treated_share: float = 0.4,
    adoption: Literal["common", "staggered"] = "common",
    seed: int = 0,
    *,
    effect: float = 5.0,
) -> tuple[xr.Dataset, DiDTruth]:
    """Monthly ``sales(store, month)`` and ``treatment(store, month)`` (0/1).

    ``sales = 100 + store effect + month effect + effect·treatment + noise`` with store
    effects ~ Normal(0, 10), month effects a trend plus a yearly cycle and noise ~
    Normal(0, 2). ``round(treated_share·units)`` stores adopt: all at period ``0.6·periods``
    (``common``) or in three equal cohorts at 0.4, 0.6 and 0.8 of the periods (``staggered``).
    """
    n_treated = round(treated_share * units)
    if not 1 <= n_treated < units:
        raise ValueError("treated_share must leave at least one treated and one untreated store")
    if periods < 8:
        raise ValueError("did_dataset needs at least 8 periods")
    rng = np.random.default_rng(seed)
    labels = [f"store_{i:02d}" for i in range(units)]
    month = _months(periods)
    t = np.arange(periods, dtype=float)
    store_fx = rng.normal(0, 10, units)
    month_fx = 0.5 * t + 3.0 * np.sin(2 * np.pi * t / 12) + rng.normal(0, 1, periods)
    if adoption == "common":
        starts = np.full(n_treated, int(0.6 * periods))
    elif adoption == "staggered":
        cohorts = np.array([int(0.4 * periods), int(0.6 * periods), int(0.8 * periods)])
        starts = cohorts[np.arange(n_treated) % 3]
    else:
        raise ValueError("adoption must be 'common' or 'staggered'")
    treated_rows = rng.choice(units, size=n_treated, replace=False)
    first = np.full(units, -1)
    first[treated_rows] = starts
    treatment = ((first[:, None] >= 0) & (t[None, :] >= first[:, None])).astype(np.float64)
    cell_effect = effect * treatment
    sales = (
        100.0
        + store_fx[:, None]
        + month_fx[None, :]
        + cell_effect
        + rng.normal(0, 2.0, (units, periods))
    )
    ds = xr.Dataset(
        {
            "sales": (("store", "month"), sales, {"units": "units"}),
            "treatment": (("store", "month"), treatment),
        },
        coords={"store": labels, "month": month},
        attrs={"mapi_schema": DID_SCHEMA.model_dump_json()},
    )
    truth = DiDTruth(
        effect=effect,
        adoption={
            lab: (_iso(month[f]) if f >= 0 else None)
            for lab, f in zip(labels, first.tolist(), strict=True)
        },
        cell_effect=cell_effect,
    )
    return ds, truth
