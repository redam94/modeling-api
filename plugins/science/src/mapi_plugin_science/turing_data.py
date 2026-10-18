"""Data for ``sci.turing_pattern`` (E61 part A): schema, a tiny spec, synthetic images.

* :data:`TURING_SCHEMA` - the raw schema: ``activator(t, y, x)`` (the canonical names);
* :data:`TURING_SPEC_YAML` - a tiny fit (32 x 32 raster, the last image held out);
* :func:`simulate_turing` / :func:`turing_dataset` - early images of a pattern grown by the
  model's own forward process (the semi-implicit spectral solver from the uniform state plus
  white noise), plus white camera noise; :data:`TURING_TRUTH` holds the defaults;
* :func:`from_challenges` / :func:`notebook_dataset` - the E61 notebook's own data. Part A
  of E61 has no data file: the notebook simulates its four 128 x 128 images (t = 4, 8, 12,
  16, the initial noise from seed 1, a fine step of 0.005 and camera noise of sd 5e-4 from
  seed 613). :func:`notebook_dataset` repeats that simulation;
  :data:`NOTEBOOK_SPEC_YAML` fits it with the notebook's sampler settings and
  :func:`challenges_reference` holds the notebook's printed estimates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.turing import (
    SpectralGrid,
    fastest_mode,
    initial_state,
    is_turing,
    simulate_pattern,
)

__all__ = [
    "CHALLENGES_FILE",
    "NOTEBOOK",
    "NOTEBOOK_SPEC_YAML",
    "TURING_SCHEMA",
    "TURING_SPEC_YAML",
    "TURING_TRUTH",
    "challenges_reference",
    "from_challenges",
    "notebook_dataset",
    "simulate_turing",
    "turing_dataset",
]

RAW_TIME, RAW_ROW, RAW_COL, RAW_VAR = "t", "y", "x", "activator"
"""Dataset names: the same as the canonical names, so no ``data.bind`` is needed."""

CHALLENGES_FILE: Final[str | None] = None
"""E61 part A reads no file from the pymc-challenges ``data/`` directory: the notebook
simulates its images (see :func:`notebook_dataset`)."""

NOTEBOOK: Final = "E61_turing_patterns_kinetic_proofreading.py"
"""The pymc-challenges notebook (``notebook_src/examples/``)."""

TURING_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            RAW_TIME: {"kind": "float", "policy": "extend", "ordered": True},
            RAW_ROW: {"kind": "integer", "policy": "fixed"},
            RAW_COL: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            RAW_VAR: {
                "dims": [RAW_TIME, RAW_ROW, RAW_COL],
                "dtype": "float64",
                "role": "target",
                "units": "1",
                "description": "Activator concentration (dimensionless) in each pixel",
            }
        },
    }
)
"""Raw schema of :func:`turing_dataset` and :func:`from_challenges`."""

TURING_TRUTH: dict[str, Any] = {
    "s": 1.0,
    "rho": 0.1,
    "d": 20.0,
    "g": 1.0,
    "s0": 1e-3,
    "sigma": 5e-4,
    "box_length": 100.0,
    "times": (4.0, 8.0, 12.0, 16.0),
}
"""The notebook's truth: ``a = 0.1``, ``b = 0.9`` (``s = 1``, ``rho = 0.1``), ``d = 20``,
``g = 1``; initial noise sd 1e-3 and camera noise sd 5e-4; images at t = 4, 8, 12, 16."""

TURING_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: turing-synthetic, project: science}
data:
  dataset: dataset://science/turing_synthetic@latest
  split: {kind: time_holdout, dim: t, last: 1}
model:
  use: sci.turing_pattern@^1
  settings: {box_length: 100.0, k2_max: 1.5, field_draws: 8, simulate_draws: 2, settle_time: 60}
  fit:
    mode: evaluate_then_refit
    seed: 61
    tune: 150
    draws: 100
    chains: 2
    engine: pymc
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, mae, coverage_90, elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.field_map, sci.ppc_image_grid,
          sci.profile_bands]
"""
"""A tiny fit of :func:`turing_dataset` (32 x 32, four images; the last one held out)."""

NOTEBOOK_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: e61-turing-mode-growth, project: biology}
data:
  dataset: dataset://biology/e61_turing_early_images@latest
model:
  use: sci.turing_pattern@^1
  settings: {box_length: 100.0, k2_max: 1.5}
  fit:
    seed: 61
    tune: 1500
    draws: 1000
    chains: 4
    target_accept: 0.95
    engine: pymc
    keep: {log_likelihood: true}
evaluate: {metrics: [elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.field_map, sci.ppc_image_grid,
          sci.profile_bands]
"""
"""E61 section 4: the four early notebook images, the notebook's sampler settings."""


def _dataset(images: np.ndarray, times: tuple[float, ...], attrs: dict[str, Any]) -> xr.Dataset:
    _, ny, nx = images.shape
    return xr.Dataset(
        {RAW_VAR: ((RAW_TIME, RAW_ROW, RAW_COL), images.astype(np.float64), {"units": "1"})},
        coords={
            RAW_TIME: np.asarray(times, dtype=np.float64),
            RAW_ROW: np.arange(ny, dtype=np.int64),
            RAW_COL: np.arange(nx, dtype=np.int64),
        },
        attrs=attrs,
    )


def simulate_turing(
    *,
    n: int = 32,
    box_length: float = TURING_TRUTH["box_length"],
    times: tuple[float, ...] = TURING_TRUTH["times"],
    truth: dict[str, Any] | None = None,
    seed: int = 0,
    dt: float = 0.005,
    camera_seed: int | None = None,
) -> tuple[xr.Dataset, dict[str, Any]]:
    """Early images of a growing pattern and the truth, from the model's forward process.

    The uniform state plus white noise (sd ``s0``, generator ``seed``) is integrated by the
    semi-implicit spectral solver with step ``dt``; the activator at ``times`` gets white
    camera noise (sd ``sigma``, generator ``camera_seed``, default ``seed + 612``). Returns
    ``(data, truth)``; ``truth`` adds ``a``, ``b``, ``wavelength``, ``max_growth`` and
    ``p_turing`` (1 or 0) of the generating parameters.

    Unless ``truth`` sets them, ``s0`` and ``sigma`` are the notebook's values times
    ``n / 128``: a coarser raster of the same square then has the same Fourier-mode
    amplitudes per pixel as the notebook, so its images stay as small (a few percent) and
    the linearised likelihood stays exact.
    """
    scale = n / 128
    p = {
        **TURING_TRUTH,
        "s0": TURING_TRUTH["s0"] * scale,
        "sigma": TURING_TRUTH["sigma"] * scale,
        **(truth or {}),
    }
    grid = SpectralGrid.of(n, box_length)
    state = initial_state(grid, p["s"], p["rho"], p["s0"], np.random.default_rng(seed))
    _, early = simulate_pattern(
        state, grid, p["s"], p["rho"], p["d"], p["g"], max(times), dt=dt, snaps=times
    )
    cam = np.random.default_rng(seed + 612 if camera_seed is None else camera_seed)
    images = np.stack([early[t] + p["sigma"] * cam.standard_normal((n, n)) for t in times])
    km, lm = fastest_mode(p["s"], p["rho"], p["d"], p["g"])
    out = {
        **{k: p[k] for k in ("s", "rho", "d", "g", "s0", "sigma")},
        "a": p["rho"] * p["s"],
        "b": (1 - p["rho"]) * p["s"],
        "wavelength": float(2 * np.pi / km[0]),
        "max_growth": float(lm[0]),
        "p_turing": float(is_turing(p["s"], p["rho"], p["d"])),
        "box_length": box_length,
    }
    attrs = {"source": "sci.turing_pattern synthetic", "seed": seed, **out}
    return _dataset(images, tuple(times), attrs), out


def turing_dataset(*, n: int = 32, seed: int = 0, **kw: Any) -> xr.Dataset:
    """The data of :func:`simulate_turing` (without the truth)."""
    return simulate_turing(n=n, seed=seed, **kw)[0]


def notebook_dataset() -> xr.Dataset:
    """The four early images of E61 section 4, as the notebook makes them.

    ``initial_state(s=1, rho=0.1, seed=1)`` on a 128 x 128 square of side 100, the
    spectral solver with ``dt = 0.005`` to t = 16 (snapshots at 4, 8, 12, 16), then camera
    noise of sd 5e-4 from ``default_rng(613)``, one image after the other.
    """
    ds, _ = simulate_turing(n=128, seed=1, camera_seed=613, dt=0.005)
    ds.attrs = {
        "source": f"pymc-challenges {NOTEBOOK} (part A, section 4): simulated in the notebook",
        "box_length": 100.0,
        "camera_noise_sd": TURING_TRUTH["sigma"],
        "truth_s": 1.0,
        "truth_rho": 0.1,
        "truth_d": 20.0,
        "truth_g": 1.0,
    }
    return ds


def from_challenges(path: str | Path | None = None) -> xr.Dataset:
    """The E61 part A data in :data:`TURING_SCHEMA`.

    E61 has no data file for part A, so with ``path`` ``None`` or a directory (the
    pymc-challenges ``data/`` directory) this is :func:`notebook_dataset`. With a file
    (``.nc``, for example written by ``python -m mapi_plugin_science.export e61``) it reads
    that file and checks the variables. Raises ``ValueError`` when the file has no
    ``activator(t, y, x)``.
    """
    if path is None or Path(path).is_dir():
        return notebook_dataset()
    ds = xr.open_dataset(Path(path), engine="h5netcdf").load()
    if RAW_VAR not in ds or set(ds[RAW_VAR].dims) != {RAW_TIME, RAW_ROW, RAW_COL}:
        raise ValueError(f"{path}: needs a variable {RAW_VAR}({RAW_TIME}, {RAW_ROW}, {RAW_COL})")
    ds = ds[[RAW_VAR]].transpose(RAW_TIME, RAW_ROW, RAW_COL)
    ds[RAW_VAR] = ds[RAW_VAR].astype(np.float64)
    return ds


def challenges_reference() -> dict[str, Any]:
    """The estimates E61 prints for the mode-growth fit (section 4, 89% intervals) and the
    truth they recover."""
    return {
        "truth": {"a": 0.1, "b": 0.9, "d": 20.0, "g": 1.0, "s0": 1e-3, "sigma": 5e-4},
        "eti89": {
            "d": (17.7, 25.2),
            "g": (0.90, 1.08),
            "b": (0.84, 1.04),
            "a": (0.054, 0.133),
        },
        "max_growth_90": (0.262, 0.264),
        "truth_max_growth": 0.263,
        "truth_wavenumber": 0.485,
        "sampler": {"tune": 1500, "target_accept": 0.95, "seconds": 93},
    }
