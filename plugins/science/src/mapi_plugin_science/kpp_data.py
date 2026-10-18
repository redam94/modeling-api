"""Raw schema, example spec, synthetic data and the real E50 data for ``sci.fisher_kpp``.

* :func:`kpp_dataset` - a scratch assay simulated from the model's own forward process
  (:func:`~mapi_plugin_science.kpp.simulate_kpp`, explicit Euler with zero-flux edges) plus
  Poisson counting noise, with known truth :data:`KPP_TRUTH`;
* :func:`from_challenges` - the PC-3 scratch assay of pymc-challenges E50 (Jin et al. 2016:
  38 columns of 50 um, counted at 0, 12, 24, 36 and 48 h) in the same schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.kpp import simulate_kpp

__all__ = [
    "KPP_SCHEMA",
    "KPP_SPEC_YAML",
    "KPP_TRUTH",
    "from_challenges",
    "kpp_dataset",
]

KPP_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "hours": {"kind": "integer", "policy": "extend"},
            "position": {"kind": "float", "policy": "fixed"},
        },
        "vars": {
            "cells": {
                "dims": ["hours", "position"],
                "dtype": "int64",
                "role": "target",
                "units": "cells",
            }
        },
    }
)
"""Raw schema of :func:`kpp_dataset` and :func:`from_challenges` (``cells(hours, position)``)."""

KPP_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: scratch-assay-kpp, project: biology}
data:
  dataset: dataset://biology/scratch_assay@latest
  bind:
    dims: {t: hours, x: position}
    vars: {count: cells}
  split: {kind: time_holdout, dim: t, last: 2}
model:
  use: sci.fisher_kpp@^1
  settings: {steps_per_hour: 3, delay: false, max_extension: 2}
  fit: {mode: evaluate_then_refit, seed: 5, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, core.posterior_forest]}
"""
"""A tiny fit on :func:`kpp_dataset` (coarse grid: 100 um columns, 3 Euler steps per hour)."""

KPP_TRUTH: dict[str, Any] = {"D": 1500.0, "lam": 0.06, "K": 120.0, "beta": None}
"""Generating parameters of :func:`kpp_dataset` (``beta=None``: no start-up delay)."""


def kpp_dataset(
    *,
    columns: int = 16,
    dx: float = 100.0,
    times: tuple[int, ...] = (0, 8, 16, 24, 32),
    wound: float = 0.4,
    steps_per_hour: int = 3,
    truth: dict[str, Any] | None = None,
    seed: int = 0,
) -> xr.Dataset:
    """A simulated scratch assay ``cells(hours, position)``.

    The sheet starts at about 70 % of the capacity with a wound (``wound`` of the field,
    centred) at about 5 % of it; :func:`simulate_kpp` moves it to ``times`` (hours) and each
    column count is Poisson. The initial counts themselves are Poisson draws of that profile.
    """
    p = {**KPP_TRUTH, **(truth or {})}
    rng = np.random.default_rng(seed)
    x = (np.arange(columns) + 0.5) * dx
    centre, half = x.mean(), wound * columns * dx / 2
    edge = 1.0 / (1.0 + np.exp(-(np.abs(x - centre) - half) / (0.5 * dx)))
    profile = p["K"] * (0.05 + 0.65 * edge)
    initial = rng.poisson(profile).astype(float)
    hours = np.asarray(times, dtype=float) - times[0]
    mean = simulate_kpp(
        initial,
        hours,
        diffusivity=p["D"],
        growth=p["lam"],
        capacity=p["K"],
        delay_rate=p["beta"],
        dx=dx,
        steps_per_hour=steps_per_hour,
    )
    counts = rng.poisson(np.clip(mean, 1e-6, None)).astype(np.int64)
    counts[0] = initial.astype(np.int64)
    return xr.Dataset(
        {"cells": (("hours", "position"), counts, {"units": "cells"})},
        coords={"hours": np.asarray(times, dtype=np.int64), "position": x},
    )


def from_challenges(path: str | Path) -> xr.Dataset:
    """Read pymc-challenges' ``data/scratch_assay_pc3.csv`` (long: ``x_um, t_h, count``) and
    pivot it into ``cells(hours, position)`` (:data:`KPP_SCHEMA`), as the E50 notebook does."""
    df = pd.read_csv(path)
    missing = {"x_um", "t_h", "count"} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    wide = df.pivot(index="t_h", columns="x_um", values="count").sort_index().sort_index(axis=1)
    if wide.isna().to_numpy().any():
        raise ValueError(f"{path}: the assay is not a full grid of times x columns")
    return xr.Dataset(
        {"cells": (("hours", "position"), wide.to_numpy().astype(np.int64), {"units": "cells"})},
        coords={
            "hours": wide.index.to_numpy().astype(np.int64),
            "position": wide.columns.to_numpy().astype(float),
        },
    )
