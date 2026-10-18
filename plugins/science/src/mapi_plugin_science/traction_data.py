"""Data for ``sci.traction_force`` (E60): schema, a tiny spec, synthetic data, the real colony.

* :data:`TRACTION_SCHEMA` - the raw schema (dataset names equal the canonical names):
  ``displacement(component, row, col)`` in um and ``force_mask(row, col)``;
* :data:`TRACTION_SPEC_YAML` - a tiny fit (32 x 32 grid, pymc NUTS, seed-deterministic);
* :func:`simulate_traction` / :func:`traction_dataset` - a traction field drawn from the
  model's own Matern prior spectrum, pushed through the Boussinesq Green's function, plus
  white tracking noise and a constant drift; :data:`TRACTION_TRUTH` holds the defaults;
* :func:`from_challenges` - the pyTFM colony of pymc-challenges
  (``data/pytfm_colony_ko04.npz``: 7 cells on a 49 kPa gel, 189 x 193 PIV grid) in the same
  schema; :func:`challenges_settings` gives the gel constants as model settings and
  :func:`challenges_reference` pyTFM's own tractions and force summaries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.traction import (
    COL_DIM,
    COMPONENT_DIM,
    COMPONENTS,
    FORCE_MASK,
    ROW_DIM,
    TARGET,
    FourierGrid,
    contractility,
    epicentre,
    forward,
    from_modes,
    prior_spectrum,
    strain_energy,
    to_modes,
)

__all__ = [
    "CHALLENGES_FILE",
    "TRACTION_SCHEMA",
    "TRACTION_SPEC_YAML",
    "TRACTION_TRUTH",
    "challenges_reference",
    "challenges_settings",
    "from_challenges",
    "simulate_traction",
    "traction_dataset",
]

CHALLENGES_FILE = "pytfm_colony_ko04.npz"
"""The file name of the real colony in the pymc-challenges ``data/`` directory."""

TRACTION_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            COMPONENT_DIM: {"kind": "categorical", "policy": "fixed"},
            ROW_DIM: {"kind": "integer", "policy": "fixed"},
            COL_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": [COMPONENT_DIM, ROW_DIM, COL_DIM],
                "dtype": "float64",
                "role": "target",
                "units": "um",
                "description": "Displacement of the gel surface (bead tracking or PIV)",
            },
            FORCE_MASK: {
                "dims": [ROW_DIM, COL_DIM],
                "dtype": "bool",
                "role": "mask",
                "description": "Region under the cell (or colony) that forces are summed over",
            },
        },
    }
)

TRACTION_TRUTH: dict[str, Any] = {
    "grid_um": 1.0,
    "young_pa": 8000.0,
    "poisson": 0.49,
    "noise_sd": 0.01,
    "traction_sd": 400.0,
    "lengthscale": 3.0,
    "smoothness": 1.5,
    "drift_um": (0.05, -0.03),
}
"""The hyperparameters and gel constants of :func:`simulate_traction` by default."""

TRACTION_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: traction-synthetic, project: science}
data:
  dataset: dataset://science/tfm_synthetic@latest
model:
  use: sci.traction_force@^1
  settings:
    grid_um: 1.0
    young_pa: 8000
    poisson: 0.49
    prior: matern
    noise_um: 0.01
    traction_pa: 300
    lengthscale_um: 3.0
    field_draws: 40
    force_mask: true
  fit: {seed: 7, tune: 300, draws: 200, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae]}
outputs: {plots: [core.convergence, core.posterior_forest]}
"""
"""A tiny fit of :func:`traction_dataset` (32 x 32 grid): about ten seconds."""


def _dataset(ux: np.ndarray, uy: np.ndarray, mask: np.ndarray, attrs: dict[str, Any]) -> xr.Dataset:
    ny, nx = ux.shape
    return xr.Dataset(
        {
            TARGET: (
                (COMPONENT_DIM, ROW_DIM, COL_DIM),
                np.stack([ux, uy]).astype(np.float64),
                {"units": "um"},
            ),
            FORCE_MASK: ((ROW_DIM, COL_DIM), np.asarray(mask, dtype=bool)),
        },
        coords={
            COMPONENT_DIM: list(COMPONENTS),
            ROW_DIM: np.arange(ny, dtype=np.int64),
            COL_DIM: np.arange(nx, dtype=np.int64),
        },
        attrs=attrs,
    )


def simulate_traction(
    *,
    ny: int = 32,
    nx: int = 32,
    seed: int = 0,
    grid_um: float = TRACTION_TRUTH["grid_um"],
    young_pa: float = TRACTION_TRUTH["young_pa"],
    poisson: float = TRACTION_TRUTH["poisson"],
    noise_sd: float = TRACTION_TRUTH["noise_sd"],
    traction_sd: float = TRACTION_TRUTH["traction_sd"],
    lengthscale: float = TRACTION_TRUTH["lengthscale"],
    smoothness: float = TRACTION_TRUTH["smoothness"],
    drift_um: tuple[float, float] = TRACTION_TRUTH["drift_um"],
    mask_radius: float = 0.35,
) -> tuple[xr.Dataset, xr.Dataset]:
    """A displacement dataset and its truth, simulated from the model's own forward process.

    The traction field is an exact draw from the Matern prior spectrum
    ``traction_sd^2 (1 + k^2 lengthscale^2)^-smoothness`` (the ``k = 0`` mode removed, so
    the net force is zero). The displacement is the Boussinesq response plus white noise
    (``noise_sd``, um) and a constant ``drift_um``. ``force_mask`` is a disc at the centre
    with radius ``mask_radius`` times the smaller side.

    Returns ``(data, truth)``: ``truth`` holds ``traction(component, row, col)`` in Pa,
    the noiseless ``displacement`` and the scalars ``strain_energy`` (fJ) and
    ``contractility`` (uN) as the model defines them, with the hyperparameters in attrs.
    """
    rng = np.random.default_rng(seed)
    g = FourierGrid.of(ny, nx, grid_um)
    s2 = np.asarray(prior_spectrum(g.k, "matern", traction_sd, lengthscale, smoothness))
    zl, zt = to_modes(rng.standard_normal((ny, nx)), rng.standard_normal((ny, nx)), g)
    tx, ty = from_modes(np.sqrt(s2) * zl, np.sqrt(s2) * zt, g)
    ux0, uy0 = forward(tx, ty, g, young_pa, poisson)
    ux = ux0 + noise_sd * rng.standard_normal((ny, nx)) + drift_um[0]
    uy = uy0 + noise_sd * rng.standard_normal((ny, nx)) + drift_um[1]
    yy, xx = np.meshgrid(np.arange(ny) - (ny - 1) / 2, np.arange(nx) - (nx - 1) / 2, indexing="ij")
    mask = np.hypot(yy, xx) <= mask_radius * min(ny, nx)
    hyper = {
        "seed": seed,
        "grid_um": grid_um,
        "young_pa": young_pa,
        "poisson": poisson,
        "noise_sd": noise_sd,
        "traction_sd": traction_sd,
        "lengthscale": lengthscale,
        "smoothness": smoothness,
    }
    data = _dataset(ux, uy, mask, {"source": "sci.traction_force synthetic", **hyper})
    centre = epicentre(tx, ty, mask, grid_um)
    truth = xr.Dataset(
        {
            "traction": ((COMPONENT_DIM, ROW_DIM, COL_DIM), np.stack([tx, ty]), {"units": "Pa"}),
            "displacement": (
                (COMPONENT_DIM, ROW_DIM, COL_DIM),
                np.stack([ux0, uy0]),
                {"units": "um"},
            ),
            "strain_energy": ((), float(strain_energy(tx, ty, ux0, uy0, mask, grid_um))),
            "contractility": ((), float(contractility(tx, ty, mask, grid_um, centre))),
        },
        coords={k: data[k].values for k in (COMPONENT_DIM, ROW_DIM, COL_DIM)},
        attrs=hyper,
    )
    return data, truth


def traction_dataset(*, ny: int = 32, nx: int = 32, seed: int = 0, **kw: Any) -> xr.Dataset:
    """The data of :func:`simulate_traction` (without the truth)."""
    return simulate_traction(ny=ny, nx=nx, seed=seed, **kw)[0]


def _load(path: str | Path) -> Any:
    p = Path(path)
    if p.is_dir():
        p = p / CHALLENGES_FILE
    return np.load(p, allow_pickle=False)


def from_challenges(path: str | Path) -> xr.Dataset:
    """The pyTFM colony (``pytfm_colony_ko04.npz``, or the ``data/`` dir holding it).

    ``displacement`` is the PIV field in um (row = y, col = x on a ``grid_um`` grid),
    ``force_mask`` the region pyTFM sums forces over. The gel constants are in ``attrs``
    (see :func:`challenges_settings`).
    """
    raw = _load(path)
    attrs = {
        "source": "pyTFM example data (Bauer et al. 2021), via pymc-challenges E60",
        "grid_um": float(raw["grid_um"]),
        "young_pa": float(raw["young_pa"]),
        "poisson": float(raw["poisson"]),
        "thickness_um": float(raw["thickness_um"]),
        "piv_window_um": float(raw["piv_window_um"]),
    }
    return _dataset(
        np.asarray(raw["ux_um"], dtype=np.float64),
        np.asarray(raw["uy_um"], dtype=np.float64),
        np.asarray(raw["mask_force"], dtype=bool),
        attrs,
    )


def challenges_settings(ds: xr.Dataset, **overrides: Any) -> dict[str, Any]:
    """Model settings for :func:`from_challenges` data (the E60 Matern fit by default)."""
    out: dict[str, Any] = {
        "grid_um": float(ds.attrs["grid_um"]),
        "young_pa": float(ds.attrs["young_pa"]),
        "poisson": float(ds.attrs["poisson"]),
        "prior": "matern",
        "noise_um": 0.003,
        "traction_pa": 1000.0,
        "lengthscale_um": 20.0,
        "force_mask": True,
    }
    out.update(overrides)
    return out


def challenges_reference(path: str | Path) -> xr.Dataset:
    """pyTFM's own result for the colony: ``traction(component, row, col)`` in Pa, the
    colony outline ``colony_mask(row, col)`` and the reported strain energy (fJ) and
    contractility (uN) in ``attrs``."""
    raw = _load(path)
    ny, nx = raw["tx_pytfm_pa"].shape
    return xr.Dataset(
        {
            "traction": (
                (COMPONENT_DIM, ROW_DIM, COL_DIM),
                np.stack([raw["tx_pytfm_pa"], raw["ty_pytfm_pa"]]).astype(np.float64),
                {"units": "Pa"},
            ),
            "colony_mask": ((ROW_DIM, COL_DIM), np.asarray(raw["mask_colony"], dtype=bool)),
        },
        coords={
            COMPONENT_DIM: list(COMPONENTS),
            ROW_DIM: np.arange(ny, dtype=np.int64),
            COL_DIM: np.arange(nx, dtype=np.int64),
        },
        attrs={
            "strain_energy_fj": float(raw["pytfm_strain_energy_j"]) * 1e15,
            "contractility_un": float(raw["pytfm_contractility_n"]) * 1e6,
        },
    )
