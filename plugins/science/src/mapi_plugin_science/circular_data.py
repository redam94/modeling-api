"""Raw schemas, example specs, synthetic data and the real E81 data for
``sci.circular_regression``.

* :func:`wind_dataset` - wind directions simulated from the model's own von Mises
  regression (canonical link on harmonics of the hour and the day of year), with known
  coefficients :data:`WIND_TRUTH`;
* :func:`activity_dataset` - camera-trap times of day for three made-up species, drawn
  from von Mises mixtures (:data:`ACTIVITY_TRUTH`) whose densities, activity levels and
  overlaps :func:`activity_truth` gives on any grid;
* :func:`from_challenges` - the Santa Barbara airport (KSBA) hourly wind reports of
  pymc-challenges E81, 2021-2023, calms dropped, in Pacific standard time;
* :func:`from_challenges_activity` - the Barro Colorado Island camera-trap times of the
  ocelot and five of its prey (Rowcliffe et al. 2014).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema
from scipy import special

from mapi_plugin_science.circular import TWO_PI, harmonic_features

__all__ = [
    "ACTIVITY_FILE",
    "ACTIVITY_SCHEMA",
    "ACTIVITY_SPEC_YAML",
    "ACTIVITY_TRUTH",
    "BCI_SPEC_YAML",
    "BCI_SPECIES",
    "CHALLENGES_FILE",
    "WIND_SCHEMA",
    "WIND_SPEC_YAML",
    "WIND_TRUTH",
    "activity_dataset",
    "activity_truth",
    "from_challenges",
    "from_challenges_activity",
    "ksba_spec_yaml",
    "wind_dataset",
]

CHALLENGES_FILE = "asos_ksba_2021_2023.csv.gz"
"""Hourly ASOS wind reports of Santa Barbara airport, 2021-2023 (pymc-challenges ``data/``)."""
ACTIVITY_FILE = "bci_camera_trap_times.csv"
"""Camera-trap times of day on Barro Colorado Island (pymc-challenges ``data/``)."""
BCI_SPECIES: tuple[str, ...] = ("ocelot", "agouti", "paca", "rat", "brocket", "peccary")
"""The species of E81 part E, the ocelot first."""

WIND_SCHEMA = DataSchema.model_validate(
    {
        "layout": "long",
        "dims": {"report": {"kind": "integer", "policy": "extend"}},
        "vars": {
            "wind_from": {
                "dims": ["report"],
                "dtype": "float64",
                "role": "target",
                "units": "deg",
                "description": "Direction the wind blows from, clockwise from true north",
            },
            "hour_lst": {
                "dims": ["report"],
                "dtype": "float64",
                "role": "covariate",
                "units": "h",
                "description": "Hours since midnight, local standard time",
            },
            "day_of_year": {
                "dims": ["report"],
                "dtype": "float64",
                "role": "covariate",
                "units": "d",
                "description": "Days since 1 January (with the fraction of the day)",
            },
            "year": {"dims": ["report"], "dtype": "int64", "role": "constant"},
        },
    }
)
"""Raw schema of :func:`wind_dataset` and :func:`from_challenges`."""

ACTIVITY_SCHEMA = DataSchema.model_validate(
    {
        "layout": "long",
        "dims": {
            "record": {"kind": "integer", "policy": "extend"},
            "species": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "clock_time": {
                "dims": ["record"],
                "dtype": "float64",
                "role": "target",
                "units": "h",
                "description": "Time of day of the photograph",
            },
            "species_idx": {
                "dims": ["record"],
                "dtype": "int64",
                "role": "index",
                "index_of": "species",
            },
        },
    }
)
"""Raw schema of :func:`activity_dataset` and :func:`from_challenges_activity`."""

WIND_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: wind-direction-circular, project: meteorology}
data:
  dataset: dataset://meteorology/wind_reports@latest
  bind:
    dims: {obs: report}
    vars: {direction: wind_from, hour: hour_lst, day: day_of_year}
  split: {kind: time_holdout, dim: obs, last: 60}
model:
  use: sci.circular_regression@^1
  settings:
    kind: von_mises
    units: degrees
    daily_harmonics: 1
    annual_harmonics: 1
    interaction_harmonics: 0
    frames: 4
    score_draws: 100
  fit:
    mode: evaluate_then_refit
    seed: 81
    tune: 150
    draws: 100
    chains: 2
    engine: pymc
    keep: {log_likelihood: true, posterior_predictive: {thin: 5}}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest, sci.rose, sci.field_map]}
"""
"""A tiny fit on :func:`wind_dataset` (one daily and one annual harmonic)."""

ACTIVITY_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: camera-trap-activity, project: ecology}
data:
  dataset: dataset://ecology/camera_trap_times@latest
  bind:
    dims: {obs: record}
    vars: {direction: clock_time, species_idx: species_idx}
model:
  use: sci.circular_regression@^1
  settings: {kind: activity, units: hours, group_dim: species, harmonics: 8, grid_points: 128,
             score_draws: 100}
  fit:
    seed: 81
    tune: 150
    draws: 100
    chains: 2
    engine: pymc
    keep: {log_likelihood: true}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, sci.field_map, sci.profile_bands]}
"""
"""A tiny fit of the log-Fourier activity densities on :func:`activity_dataset`."""

BCI_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: bci-ocelot-activity, project: ecology}
data:
  dataset: dataset://ecology/bci_camera_traps@latest
  bind:
    dims: {obs: record}
    vars: {direction: clock_time, species_idx: species_idx}
model:
  use: sci.circular_regression@^1
  settings: {kind: activity, units: hours, group_dim: species, harmonics: 16, tau_sd: 2.0,
             grid_points: 256}
  fit: {seed: 81, tune: 1000, draws: 1000, chains: 4, engine: nutpie,
        keep: {log_likelihood: true}}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, sci.field_map, sci.profile_bands]}
"""
"""E81 part E: the log-Fourier densities of the six species (K = 16, tau ~ HalfNormal(2))."""

WIND_TRUTH: dict[str, tuple[float, float]] = {
    "1": (0.6, -0.5),
    "cos1h": (-1.1, 0.4),
    "sin1h": (0.5, 0.9),
    "cos1d": (0.4, 0.3),
    "sin1d": (-0.3, 0.2),
}
"""Generating coefficients ``(eta_1, eta_2)`` per feature of :func:`wind_dataset` (one
daily and one annual harmonic, no interaction)."""

ACTIVITY_TRUTH: dict[str, dict[str, list[float]]] = {
    "owl": {"mu_h": [22.0, 3.0], "kappa": [3.0, 4.0], "w": [0.6, 0.4]},
    "lark": {"mu_h": [7.0, 17.0], "kappa": [6.0, 5.0], "w": [0.55, 0.45]},
    "drifter": {"mu_h": [12.0], "kappa": [0.6], "w": [1.0]},
}
"""Von Mises mixtures (peak hours, concentrations, weights) of :func:`activity_dataset`."""


def wind_dataset(
    *, n: int = 480, truth: dict[str, tuple[float, float]] | None = None, seed: int = 0
) -> xr.Dataset:
    """``n`` wind reports at random times of one year, sorted in time, with directions in
    degrees from the von Mises canonical-link regression with coefficients ``truth``."""
    t = dict(truth or WIND_TRUTH)
    rng = np.random.default_rng(seed)
    clock = np.sort(rng.uniform(0.0, 365.0, n))  # days since 1 January
    hour = (clock % 1.0) * 24.0
    day = clock
    feats, names = harmonic_features(hour, day, daily=1, annual=1, interactions=0)
    coef = np.asarray([t.get(nm, (0.0, 0.0)) for nm in names])
    eta = feats @ coef
    theta = rng.vonmises(np.arctan2(eta[:, 1], eta[:, 0]), np.hypot(eta[:, 0], eta[:, 1]))
    deg = np.mod(np.rad2deg(theta), 360.0)
    return xr.Dataset(
        {
            "wind_from": (("report",), deg, {"units": "deg"}),
            "hour_lst": (("report",), hour, {"units": "h"}),
            "day_of_year": (("report",), day, {"units": "d"}),
            "year": (("report",), np.full(n, 2021, dtype=np.int64)),
        },
        coords={"report": np.arange(n, dtype=np.int64)},
    )


def _mixture_density(spec: dict[str, list[float]], theta: np.ndarray) -> np.ndarray:
    mu = TWO_PI * np.asarray(spec["mu_h"]) / 24.0
    k = np.asarray(spec["kappa"])
    w = np.asarray(spec["w"])
    dens = np.exp(k[:, None] * (np.cos(theta[None] - mu[:, None]) - 1)) / (
        TWO_PI * special.i0e(k)[:, None]
    )
    return np.asarray((w[:, None] * dens).sum(0))


def activity_truth(
    grid_points: int = 256, truth: dict[str, dict[str, list[float]]] | None = None
) -> dict[str, Any]:
    """The true densities (per radian) on ``grid_points`` angles, the activity levels and the
    overlap matrix of :func:`activity_dataset` (species in :data:`ACTIVITY_TRUTH` order)."""
    t = truth or ACTIVITY_TRUTH
    grid = np.arange(grid_points) * TWO_PI / grid_points
    f = np.stack([_mixture_density(v, grid) for v in t.values()])
    ov = np.minimum(f[:, None], f[None]).sum(-1) * TWO_PI / grid_points
    return {
        "species": list(t),
        "density": f,
        "activity_level": 1.0 / (TWO_PI * f.max(-1)),
        "overlap": ov,
    }


def activity_dataset(
    *,
    sizes: tuple[int, ...] = (240, 300, 160),
    truth: dict[str, dict[str, list[float]]] | None = None,
    seed: int = 0,
) -> xr.Dataset:
    """Times of day (hours) of camera-trap records of the species in ``truth``, drawn from
    their von Mises mixtures; records are shuffled across species."""
    t = truth or ACTIVITY_TRUTH
    rng = np.random.default_rng(seed)
    times, idx = [], []
    for j, (spec, n) in enumerate(zip(t.values(), sizes, strict=True)):
        comp = rng.choice(len(spec["w"]), size=n, p=np.asarray(spec["w"]))
        mu = TWO_PI * np.asarray(spec["mu_h"])[comp] / 24.0
        th = rng.vonmises(mu, np.asarray(spec["kappa"])[comp])
        times.append(np.mod(th, TWO_PI) / TWO_PI * 24.0)
        idx.append(np.full(n, j, dtype=np.int64))
    order = rng.permutation(sum(sizes))
    clock = np.concatenate(times)[order]
    sp = np.concatenate(idx)[order]
    return xr.Dataset(
        {
            "clock_time": (("record",), clock, {"units": "h"}),
            "species_idx": (("record",), sp),
        },
        coords={"record": np.arange(clock.size, dtype=np.int64), "species": list(t)},
    )


def from_challenges(path: str | Path) -> xr.Dataset:
    """Read pymc-challenges' ``data/asos_ksba_2021_2023.csv.gz`` the way E81 does.

    Calm reports (speed 0, direction undefined) and reports without a direction are dropped.
    The clock is Pacific *standard* time all year (UTC - 8), so the sea breeze follows the
    sun. ``day_of_year`` is ``dayofyear - 1 + hour / 24``. The reports stay in time order:
    the 2023 reports are the last ones (the notebook's held-out year).
    """
    df = pd.read_csv(path)
    missing = {"valid", "drct", "sknt"} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    lst = pd.to_datetime(df["valid"]) - pd.Timedelta(hours=8)
    keep = (df["sknt"] > 0) & df["drct"].notna()
    lst = lst[keep].reset_index(drop=True)
    drct = df.loc[keep, "drct"].to_numpy(dtype=float)
    hour = (lst.dt.hour + lst.dt.minute / 60.0).to_numpy(dtype=float)
    day = (lst.dt.dayofyear - 1).to_numpy(dtype=float) + hour / 24.0
    order = np.argsort(lst.to_numpy(), kind="stable")
    n = int(keep.sum())
    return xr.Dataset(
        {
            "wind_from": (("report",), np.mod(drct[order], 360.0), {"units": "deg"}),
            "hour_lst": (("report",), hour[order], {"units": "h"}),
            "day_of_year": (("report",), day[order], {"units": "d"}),
            "year": (("report",), lst.dt.year.to_numpy(dtype=np.int64)[order]),
        },
        coords={"report": np.arange(n, dtype=np.int64)},
    )


def ksba_spec_yaml(data: xr.Dataset, *, holdout_year: int = 2023) -> str:
    """E81 part D: the von Mises regression with 19 harmonic features, fitted on the years
    before ``holdout_year``; that year is held out (``train_only``, as in the notebook)."""
    last = int((np.asarray(data["year"].values) >= holdout_year).sum())
    split = f"\n  split: {{kind: time_holdout, dim: obs, last: {last}}}" if last else ""
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: ksba-wind-direction, project: meteorology}}
data:
  dataset: dataset://meteorology/ksba_wind_2021_2023@latest
  bind:
    dims: {{obs: report}}
    vars: {{direction: wind_from, hour: hour_lst, day: day_of_year}}{split}
model:
  use: sci.circular_regression@^1
  settings: {{kind: von_mises, units: degrees, daily_harmonics: 3, annual_harmonics: 2,
             interaction_harmonics: 2}}
  fit: {{mode: train_only, seed: 81, tune: 1000, draws: 1000, chains: 4, engine: nutpie,
        keep: {{log_likelihood: true, posterior_predictive: {{thin: 20}}}}}}
evaluate: {{metrics: [elpd_loo]}}
outputs: {{plots: [core.convergence, core.posterior_forest, sci.rose, sci.field_map]}}
"""


def from_challenges_activity(
    path: str | Path, species: tuple[str, ...] = BCI_SPECIES
) -> xr.Dataset:
    """Read pymc-challenges' ``data/bci_camera_trap_times.csv`` (``species``, ``time`` as a
    fraction of 24 hours) and keep the records of ``species`` (default: the ocelot and the
    five prey of E81), as hours since midnight."""
    df = pd.read_csv(path)
    missing = {"species", "time"} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    df = df[df["species"].isin(species)].reset_index(drop=True)
    absent = sorted(set(species) - set(df["species"]))
    if absent:
        raise ValueError(f"{path}: no records of {absent}")
    lookup = {s: i for i, s in enumerate(species)}
    return xr.Dataset(
        {
            "clock_time": (
                ("record",),
                np.mod(df["time"].to_numpy(dtype=float) * 24.0, 24.0),
                {"units": "h"},
            ),
            "species_idx": (
                ("record",),
                df["species"].map(lookup).to_numpy(dtype=np.int64),
            ),
        },
        coords={"record": np.arange(len(df), dtype=np.int64), "species": list(species)},
    )
