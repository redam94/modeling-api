"""Synthetic datasets, raw schemas and example specs for the ``ts.*`` plugins.

They double as documentation, test fixtures and conformance-corpus generators:

* :func:`co2_dataset` - a Mauna-Loa-like monthly CO2 series (C08): quadratic growth, a
  yearly cycle, noise and **sentinel codes** (``-99.99``) for missing months;
* :func:`tracks_dataset` - E24-like animal movement: step lengths from a 2-state
  (resting/travelling) Markov chain, **padded** to a common length with a mask.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "CO2_SCHEMA",
    "CO2_SENTINEL",
    "CO2_SPEC_YAML",
    "TRACKS_SCHEMA",
    "TRACKS_SPEC_YAML",
    "TRACKS_TRUTH",
    "co2_dataset",
    "tracks_dataset",
]

CO2_SENTINEL = -99.99
"""The code the raw series uses for months without a measurement."""

CO2_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"date": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {
            "co2": {
                "dims": ["date"],
                "dtype": "float64",
                "role": "target",
                "units": "ppm",
                "missing_sentinel": CO2_SENTINEL,
            }
        },
    }
)

CO2_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: mauna-loa-co2, project: climate}
data:
  dataset: dataset://climate/mauna_loa_monthly@latest
  bind:
    dims: {t: date}
    vars: {y: co2}
  split: {kind: time_holdout, dim: t, last: 12}
pipeline:
  - {id: clean, use: core.replace_sentinel@^1, with: {vars: [y], below: 0}}
  - {id: scale, use: core.standardize@^1, with: {vars: [y], over: [t]}}
model:
  use: ts.hsgp_trend@^1
  settings: {max_extension: 24, m: 20, season_order: 2, lengthscale_mu: 0.5}
  fit: {mode: evaluate_then_refit, seed: 11, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mape, coverage_90]}
outputs: {plots: [core.ppc_fan, core.convergence]}
"""


def co2_dataset(
    *,
    years: int = 10,
    seed: int = 0,
    start: str = "1958-03-01",
    missing_fraction: float = 0.03,
    sentinel: float = CO2_SENTINEL,
) -> xr.Dataset:
    """Monthly ``co2(date)`` in ppm: ``315 + 0.07·m + 8e-5·m² + 3·sin(2πm/12) + noise``.

    A ``missing_fraction`` of months (never the first or last two) hold ``sentinel``.
    """
    rng = np.random.default_rng(seed)
    n = 12 * years
    m = np.arange(n, dtype=float)
    date = pd.date_range(start, periods=n, freq="MS").as_unit("ns")
    season = 3.0 * np.sin(2 * np.pi * m / 12.0) + 0.8 * np.cos(4 * np.pi * m / 12.0)
    co2 = 315.0 + 0.07 * m + 8e-5 * m**2 + season + 0.3 * rng.standard_normal(n)
    n_missing = round(missing_fraction * n)
    if n_missing:
        holes = rng.choice(np.arange(2, n - 2), size=n_missing, replace=False)
        co2[holes] = sentinel
    return xr.Dataset({"co2": (("date",), co2, {"units": "ppm"})}, coords={"date": date})


TRACKS_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "animal": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            "step": {"kind": "integer", "policy": "extend"},
        },
        "vars": {
            "step_length": {
                "dims": ["animal", "step"],
                "dtype": "float64",
                "role": "target",
                "units": "km",
                "missing": "allow",
            },
            "observed": {"dims": ["animal", "step"], "dtype": "bool", "role": "mask"},
        },
        "ragged": [
            {"group_dim": "animal", "within_dim": "step", "form": "padded", "mask_var": "observed"}
        ],
    }
)

TRACKS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: animal-tracks-hmm, project: ecology}
data:
  dataset: dataset://ecology/tracks@latest
  bind:
    vars: {y: step_length, mask: observed}
  split: {kind: time_holdout, dim: step, last: 6}
pipeline:
  - {id: logstep, use: core.log1p@^1, with: {vars: [y]}}
model:
  use: ts.gaussian_hmm@^1
  settings: {n_states: 2, mu_center: 0.8, mu_sigma: 1.0, sigma_scale: 0.5}
  fit: {mode: evaluate_then_refit, seed: 5, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, core.posterior_forest]}
"""

TRACKS_TRUTH: dict[str, Any] = {
    "pi": [0.5, 0.5],
    "transition": [[0.85, 0.15], [0.2, 0.8]],
    "mu": [0.25, 1.4],
    "sigma": [0.12, 0.25],
}
"""Generating parameters of :func:`tracks_dataset` on the ``log1p(step_length)`` scale."""


def tracks_dataset(
    *, animals: int = 4, steps: int = 40, seed: int = 0, ragged: bool = True
) -> xr.Dataset:
    """Padded ``step_length(animal, step)`` (km) with an ``observed`` mask.

    States follow :data:`TRACKS_TRUTH`; ``log1p(step_length) ~ Normal(mu_z, sigma_z)``. With
    ``ragged`` every other animal's track ends early (padding is NaN and ``observed=False``).
    """
    rng = np.random.default_rng(seed)
    pi = np.asarray(TRACKS_TRUTH["pi"])
    a = np.asarray(TRACKS_TRUTH["transition"])
    mu = np.asarray(TRACKS_TRUTH["mu"])
    sd = np.asarray(TRACKS_TRUTH["sigma"])
    z = np.zeros((animals, steps), dtype=int)
    for i in range(animals):
        z[i, 0] = rng.choice(2, p=pi)
        for t in range(1, steps):
            z[i, t] = rng.choice(2, p=a[z[i, t - 1]])
    length = np.expm1(rng.normal(mu[z], sd[z]))
    observed = np.ones((animals, steps), dtype=bool)
    if ragged:
        for i in range(1, animals, 2):
            observed[i, steps - 3 - i :] = False
    length = np.where(observed, length, np.nan)
    return xr.Dataset(
        {
            "step_length": (("animal", "step"), length, {"units": "km"}),
            "observed": (("animal", "step"), observed),
        },
        coords={"animal": [f"a{i}" for i in range(animals)], "step": np.arange(steps)},
    )
