"""Raw schema, example specs, synthetic data and the E51 calibration experiment for
``sci.heat_plate``.

* :func:`heat_plate_dataset` - a calibration experiment simulated from the model's own
  forward process (:func:`~mapi_plugin_science.heat_plate.steady_rise`): one heater per run,
  the same thermocouples in every run, Gaussian sensor noise, known truth
  :data:`HEAT_TRUTH`;
* :func:`from_challenges` - the calibration experiment of pymc-challenges E51 (a 40 x 20 cm
  plate on a 24 x 12 grid, three runs with one 10 W test heater each, twelve thermocouples
  at +-0.2 degC). E51 simulates its data in the notebook, so there is no data file
  (:data:`CHALLENGES_FILE` is ``None``): the loader rebuilds the notebook's experiment with
  the notebook's generator and seed, so the readings are the notebook's ``T_cal``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.heat_plate import steady_rise

__all__ = [
    "CHALLENGES_FILE",
    "E51_SPEC_YAML",
    "HEAT_SCHEMA",
    "HEAT_SPEC_YAML",
    "HEAT_TRUTH",
    "NOTEBOOK_HEATERS",
    "NOTEBOOK_REFERENCE",
    "challenges_settings",
    "from_challenges",
    "heat_plate_dataset",
]

HEAT_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "run": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
            "y_cm": {"kind": "float", "policy": "fixed"},
            "x_cm": {"kind": "float", "policy": "fixed"},
        },
        "vars": {
            "heater_w": {
                "dims": ["run", "y_cm", "x_cm"],
                "dtype": "float64",
                "role": "covariate",
                "units": "W",
            },
            "reading_c": {
                "dims": ["run", "y_cm", "x_cm"],
                "dtype": "float64",
                "role": "target",
                "units": "degC",
                "missing": "allow",
            },
        },
    }
)
"""Raw schema of :func:`heat_plate_dataset` and :func:`from_challenges`."""

HEAT_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: heat-plate-calibration, project: engineering}
data:
  dataset: dataset://engineering/heat_plate_calibration@latest
  bind:
    dims: {run: run, y: y_cm, x: x_cm}
    vars: {heater_power: heater_w, temperature: reading_c}
  split: {kind: leave_group_out, dim: run, groups: [cal3]}
model:
  use: sci.heat_plate@^1
  settings: {thickness_mm: 5.0, room_temperature: 20.0}
  fit: {mode: evaluate_then_refit, seed: 5, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, core.posterior_forest]}
"""
"""A tiny fit on :func:`heat_plate_dataset` (12 x 6 grid); the third run is held out."""

E51_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: heat-plate-e51, project: engineering}
data:
  dataset: dataset://engineering/heat_plate_e51@latest
  bind:
    dims: {run: run, y: y_cm, x: x_cm}
    vars: {heater_power: heater_w, temperature: reading_c}
model:
  use: sci.heat_plate@^1
  settings: {thickness_mm: 5.0, room_temperature: 20.0}
  fit:
    seed: 51
    tune: 1000
    draws: 1000
    chains: 4
    engine: nutpie
    target_accept: 0.9
    keep: {log_likelihood: true}
evaluate: {metrics: [elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, core.pair, sci.field_map, sci.image_residuals]
"""
"""The E51 calibration with the notebook's sampler settings (4 x 1000 draws after 1000
tuning steps, ``target_accept=0.9``, seed 51), fitted in sample on all three runs (about
45 s with nutpie). Three runs leave no run to hold out, so the spec asks for LOO only."""

HEAT_TRUTH: dict[str, float] = {"k": 60.0, "h": 9.0, "h_edge": 15.0, "eta": 0.93, "sigma": 0.2}
"""The notebook's ``THETA_TRUE`` plus the thermocouple noise sd (degC)."""

NOTEBOOK_HEATERS: tuple[tuple[float, float], ...] = ((6.0, 10.0), (20.0, 5.0), (33.0, 14.0))
"""``(x, y)`` in cm of the test heater of each calibration run (E51 ``test_sites``)."""

NOTEBOOK_REFERENCE: dict[str, Any] = {
    "summary": {
        "k": {"mean": 59.288, "sd": 2.820, "eti89": (54.443, 63.176)},
        "h": {"mean": 9.074, "sd": 0.441, "eti89": (8.324, 9.678)},
        "h_edge": {"mean": 6.774, "sd": 3.511, "eti89": (2.355, 13.295)},
        "eta": {"mean": 0.912, "sd": 0.042, "eti89": (0.839, 0.967)},
        "sigma": {"mean": 0.189, "sd": 0.023, "eti89": (0.156, 0.230)},
    },
    "correlations": {"k-h": 0.91, "k-eta": 0.96, "h-eta": 0.95},
    "spread_length_cm_truth": 12.9,
    "divergences": 2,
}
"""What the executed E51 notebook printed for the calibration (PyMC 6.3.2, 4 x 1000 draws)."""

CHALLENGES_FILE: str | None = None
"""E51 has no data file: the notebook simulates its calibration experiment."""


def heat_plate_dataset(
    *,
    nx: int = 12,
    ny: int = 6,
    length_cm: float = 40.0,
    heaters: Sequence[tuple[float, float]] = NOTEBOOK_HEATERS,
    power_w: float = 10.0,
    n_sensors: int = 12,
    thickness_mm: float = 5.0,
    room_temperature: float = 20.0,
    truth: dict[str, float] | None = None,
    seed: int = 0,
) -> xr.Dataset:
    """A simulated calibration experiment ``reading_c(run, y_cm, x_cm)``.

    A ``length_cm`` plate of ``nx x ny`` square cells; run ``i`` puts ``power_w`` into the
    cell under ``heaters[i]`` (``(x, y)`` in cm). ``n_sensors`` thermocouples sit at random
    distinct cells, the same in every run, and read the steady field plus N(0, sigma) noise.
    With the defaults ``nx=24, ny=12, seed=0`` this is the E51 notebook experiment exactly
    (the same calls on ``default_rng(seed)`` in the same order).
    """
    p = {**HEAT_TRUTH, **(truth or {})}
    n = nx * ny
    length_m = length_cm / 100.0
    dx_m = length_m / nx
    rng = np.random.default_rng(seed)
    sensors = rng.choice(n, n_sensors, replace=False)
    power = np.zeros((len(heaters), n))
    for r, (x, y) in enumerate(heaters):
        i, j = int((x / 100.0) / dx_m), int((y / 100.0) / dx_m)
        if not (0 <= i < nx and 0 <= j < ny):
            raise ValueError(f"heater {r} at ({x}, {y}) cm is outside the plate")
        power[r, j * nx + i] = power_w
    rise = steady_rise(
        power.reshape(-1, ny, nx),
        k=p["k"], h=p["h"], h_edge=p["h_edge"], eta=p["eta"], dx_m=dx_m,
        thickness_m=thickness_mm / 1000.0,
    ).reshape(-1, n)  # fmt: skip
    true_t = room_temperature + rise[:, sensors]
    readings = true_t + p["sigma"] * rng.standard_normal(true_t.shape)
    grid = np.full((len(heaters), n), np.nan)
    grid[:, sensors] = readings
    dx_cm = length_cm / nx
    return xr.Dataset(
        {
            "heater_w": (("run", "y_cm", "x_cm"), power.reshape(-1, ny, nx), {"units": "W"}),
            "reading_c": (("run", "y_cm", "x_cm"), grid.reshape(-1, ny, nx), {"units": "degC"}),
        },
        coords={
            "run": [f"cal{r + 1}" for r in range(len(heaters))],
            "y_cm": (np.arange(ny) + 0.5) * dx_cm,
            "x_cm": (np.arange(nx) + 0.5) * dx_cm,
        },
    )


def from_challenges(path: str | Path | None = None) -> xr.Dataset:
    """The E51 calibration experiment (:data:`HEAT_SCHEMA`): 3 runs on the notebook's
    24 x 12 grid, 36 thermocouple readings, identical to the notebook's ``T_cal``.

    E51 has no data file, so ``path`` (a pymc-challenges ``data/`` directory or file, as for
    the other loaders) is accepted and ignored.
    """
    del path
    return heat_plate_dataset(nx=24, ny=12, seed=0)


def challenges_settings() -> dict[str, Any]:
    """Model settings of the E51 calibration (the notebook's plate and priors)."""
    return {"thickness_mm": 5.0, "room_temperature": 20.0}
