"""Synthetic data and the example spec of ``causal.impact`` (tests, conformance, docs).

:func:`impact_dataset` simulates weekly ``sales`` that follow two control series (for
example the sales of a region without the campaign and a search-interest index) plus a slow
local level and noise; from ``start_week`` on, sales rise by ``lift`` (a share of what they
would have been). :class:`ImpactTruth` carries the true counterfactual and effect.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "IMPACT_SCHEMA",
    "IMPACT_SPEC_YAML",
    "IMPACT_START_DATE",
    "ImpactTruth",
    "impact_dataset",
]

IMPACT_START_DATE = "2023-01-02"
"""The first week of :func:`impact_dataset` (a Monday)."""

IMPACT_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"week": {"kind": "time", "policy": "extend", "freq": "W-MON"}},
        "vars": {
            "sales": {
                "dims": ["week"],
                "dtype": "float64",
                "role": "target",
                "units": "units",
                "missing": "allow",
            },
            "control_a": {"dims": ["week"], "dtype": "float64", "role": "covariate"},
            "control_b": {"dims": ["week"], "dtype": "float64", "role": "covariate"},
        },
    }
)

IMPACT_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: campaign-impact, project: marketing}
data:
  dataset: dataset://marketing/weekly_sales_with_controls@latest
  bind:
    dims: {time: week}
    vars: {y: sales}
model:
  use: causal.impact@^1
  settings:
    controls: [control_a, control_b]
    intervention_start: "2024-07-15"
    trend: level
  fit: {mode: train_only, seed: 7, tune: 300, draws: 300, chains: 2, engine: nutpie}
outputs: {plots: [causal.impact, causal.pre_fit, core.convergence]}
"""
"""The example spec on :func:`impact_dataset` with its defaults (the start is week 80)."""

_BETA = {"control_a": 1.0, "control_b": 0.8}


@dataclass(frozen=True, slots=True)
class ImpactTruth:
    """The generating values of :func:`impact_dataset`."""

    start: str
    """ISO date of the first week with the lift."""
    lift: float
    beta: dict[str, float]
    counterfactual: np.ndarray
    """``(weeks,)`` sales without the intervention (noise included)."""
    effect: np.ndarray
    """``(weeks,)`` true effect (0 before the start)."""

    @property
    def effect_total(self) -> float:
        return float(self.effect.sum())


def impact_dataset(
    weeks: int = 104, start_week: int = 80, lift: float = 0.1, seed: int = 0
) -> tuple[xr.Dataset, ImpactTruth]:
    """Weekly ``sales(week)``, ``control_a(week)`` and ``control_b(week)``.

    ``sales = 20 + 1.0·control_a + 0.8·control_b + level + noise`` before ``start_week``
    (position, 0-based) and ``(1 + lift)`` times that from it on. The controls are random
    walks with a yearly cycle; ``level`` is a slow random walk.
    """
    if not 3 <= start_week < weeks:
        raise ValueError("start_week must leave at least 3 weeks before it and 1 after it")
    rng = np.random.default_rng(seed)
    t = np.arange(weeks, dtype=float)
    week = pd.date_range(IMPACT_START_DATE, periods=weeks, freq="W-MON").as_unit("ns")
    control_a = 50.0 + np.cumsum(rng.normal(0, 0.6, weeks)) + 5.0 * np.sin(2 * np.pi * t / 52.18)
    control_b = 30.0 + np.cumsum(rng.normal(0, 0.4, weeks)) + 3.0 * np.cos(2 * np.pi * t / 52.18)
    level = np.cumsum(rng.normal(0, 0.15, weeks))
    base = (
        20.0
        + _BETA["control_a"] * control_a
        + _BETA["control_b"] * control_b
        + level
        + rng.normal(0, 1.0, weeks)
    )
    effect = np.where(t >= start_week, lift * base, 0.0)
    ds = xr.Dataset(
        {
            "sales": (("week",), base + effect, {"units": "units"}),
            "control_a": (("week",), control_a),
            "control_b": (("week",), control_b),
        },
        coords={"week": week},
        attrs={"mapi_schema": IMPACT_SCHEMA.model_dump_json()},
    )
    start = str(np.datetime_as_string(week[start_week].to_datetime64(), unit="D"))
    truth = ImpactTruth(
        start=start, lift=lift, beta=dict(_BETA), counterfactual=base, effect=effect
    )
    return ds, truth
