"""Data for ``sci.cmb_field`` (E79): schema, a tiny spec, synthetic skies, the Planck patches.

* :data:`CMB_SCHEMA` - the raw schema (dataset names equal the canonical names):
  ``temperature(row, col)`` in uK with NaN where masked, ``noise_sd(row, col)``,
  ``reference_dl(multipole)`` and ``pixel_window(multipole)``;
* :data:`CMB_SPEC_YAML` - a tiny fit (32 x 32 map, 6 bins, a few seconds);
  :data:`PLANCK_SPEC_YAML` - the notebook's fit of the LMC patch;
* :func:`simulate_cmb` / :func:`cmb_dataset` - a sky drawn from the model's own prior on
  the torus with a known spectrum (the reference times :func:`truth_scale`), cut to the
  raster, plus white noise and a mask; :data:`CMB_TRUTH` holds the defaults;
* :func:`from_challenges` - a Planck 2018 SMICA patch of pymc-challenges
  (``data/planck_smica_patches.npz`` and ``data/planck_theory_tt.txt``) prepared as the
  notebook does: 3.75' pixels averaged to 7.5', a pixel is usable only if all its samples
  are inside the common mask, and the noise sd is the local rms of the half-mission
  half-difference map; :func:`challenges_reference` gives SMICA's inpainted map and the
  Planck binned full-sky spectrum.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema
from scipy import ndimage

from mapi_plugin_science.cmb import (
    ARCMIN,
    COL_DIM,
    MULTIPOLE_DIM,
    NOISE,
    PIXEL_WINDOW,
    REFERENCE,
    ROW_DIM,
    TARGET,
    FieldSampler,
    Instrument,
    hartley,
    projection_stretch,
)

__all__ = [
    "CHALLENGES_FILE",
    "CMB_SCHEMA",
    "CMB_SPEC_YAML",
    "CMB_TRUTH",
    "NOTEBOOK_REFERENCE",
    "PLANCK_SPEC_YAML",
    "THEORY_FILE",
    "TINY_EDGES",
    "challenges_reference",
    "cmb_dataset",
    "from_challenges",
    "simulate_cmb",
    "toy_spectrum",
    "truth_scale",
]

CHALLENGES_FILE = "planck_smica_patches.npz"
"""The Planck SMICA patches in the pymc-challenges ``data/`` directory."""
THEORY_FILE = "planck_theory_tt.txt"
"""The Planck 2018 best-fit theory spectra, next to :data:`CHALLENGES_FILE`."""
BINNED_FILE = "planck_tt_binned.txt"

NOTEBOOK_REFERENCE: dict[str, Any] = {
    "masked_fraction": 0.165,
    "log_amplitude": (-0.014, 0.023),
    "tilt": (-0.009, 0.030),
    "sd_observed_uK": 2.5,
    "sd_lmc_hole_uK": 92.0,
    "q_mean_bin_80": 0.334,
    "q_mean_bin_1950": 0.165,
}
"""Headline numbers the E79 notebook prints for the LMC patch (Gibbs, 4 x 80 iterations)."""

CMB_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            ROW_DIM: {"kind": "integer", "policy": "fixed"},
            COL_DIM: {"kind": "integer", "policy": "fixed"},
            MULTIPOLE_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": [ROW_DIM, COL_DIM],
                "dtype": "float64",
                "role": "target",
                "units": "uK",
                "missing": "allow",
                "description": "CMB temperature of each pixel; NaN where the mask removes it",
            },
            NOISE: {
                "dims": [ROW_DIM, COL_DIM],
                "dtype": "float64",
                "role": "weight",
                "units": "uK",
                "description": "Noise sd of each pixel",
            },
            REFERENCE: {
                "dims": [MULTIPOLE_DIM],
                "dtype": "float64",
                "role": "constant",
                "units": "uK^2",
                "description": "Reference spectrum D_ell = ell (ell + 1) C_ell / 2 pi",
            },
            PIXEL_WINDOW: {
                "dims": [MULTIPOLE_DIM],
                "dtype": "float64",
                "role": "constant",
                "description": "Pixel window of the source map at each multipole",
            },
        },
    }
)

TINY_EDGES: tuple[float, ...] = (0, 300, 600, 900, 1200, 1600, 2100)
"""Bandpower bins of the tiny synthetic fit."""

CMB_TRUTH: dict[str, Any] = {
    "n": 32,
    "border": 4,
    "pixel_arcmin": 7.5,
    "beam_fwhm_arcmin": 5.0,
    "pixel_subsamples": 1,
    "amplitude": 1.15,
    "tilt": -0.1,
    "pivot": 500.0,
    "noise_uK": (3.0, 6.0),
    "hole_radius": 0.18,
    "edges": TINY_EDGES,
}
"""The geometry and the true spectrum of :func:`simulate_cmb` by default."""

CMB_SPEC_YAML = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: cmb-synthetic, project: science}}
data:
  dataset: dataset://science/cmb_synthetic@latest
model:
  use: sci.cmb_field@^1
  settings:
    pixel_arcmin: 7.5
    beam_fwhm_arcmin: 5.0
    pixel_subsamples: 1
    pixel_window: false
    bin_edges: {list(TINY_EDGES)}
    border: 4
    field_draws: 8
  fit: {{seed: 79, tune: 10, draws: 60, chains: 2, engine: gibbs}}
evaluate: {{metrics: [rmse, mae, coverage_90]}}
outputs: {{plots: [core.convergence, sci.power_spectrum, sci.field_map, sci.image_residuals]}}
"""
"""A tiny fit of :func:`cmb_dataset` (32 x 32 map on a 40 x 40 torus): a few seconds."""

PLANCK_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: planck-smica-lmc, project: astronomy}
data:
  dataset: dataset://astronomy/planck_smica_lmc@latest
model:
  use: sci.cmb_field@^1
  settings:
    pixel_arcmin: 7.5
    beam_fwhm_arcmin: 5.0
    pixel_subsamples: 8
    pixel_window: true
    projection: gnomonic
    border: 8
    field_draws: 40
  fit: {seed: 79, tune: 16, draws: 64, chains: 4, engine: gibbs}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, sci.power_spectrum, sci.field_map, sci.image_residuals]}
"""
"""The notebook's fit of the LMC patch: 4 chains x 80 Gibbs iterations, the first 16 burn-in."""


# ------------------------------------------------------------------------------ synthetic


def toy_spectrum(lmax: int = 3000) -> np.ndarray:
    """A smooth CMB-like ``D_ell`` (uK^2) on ``ell = 0..lmax``: acoustic wiggles and a
    damping tail (a test spectrum, not a cosmology)."""
    ell = np.arange(lmax + 1, dtype=float)
    rise = ell / (ell + 60.0)
    peaks = 1.0 + 0.45 * np.cos(np.pi * (ell - 220.0) / 300.0) * np.exp(-ell / 1500.0)
    out = 5500.0 * rise * peaks * np.exp(-((ell / 1300.0) ** 2))
    out[:2] = 0.0
    return out


def truth_scale(
    ell: np.ndarray,
    amplitude: float = CMB_TRUTH["amplitude"],
    tilt: float = CMB_TRUTH["tilt"],
    pivot: float = CMB_TRUTH["pivot"],
) -> np.ndarray:
    """The true spectrum over the reference: ``amplitude (ell / pivot)^tilt``."""
    return np.asarray(amplitude * (np.maximum(ell, 1.0) / pivot) ** tilt)


def _mask(n: int, hole_radius: float) -> np.ndarray:
    yy, xx = np.meshgrid(np.arange(n) - (n - 1) / 2, np.arange(n) - (n - 1) / 2, indexing="ij")
    keep = np.hypot(yy, xx + 0.1 * n) > hole_radius * n
    keep &= np.hypot(yy - 0.3 * n, xx - 0.3 * n) > 1.2  # a small hole (a point source)
    return np.asarray(keep)


def _dataset(
    temperature: np.ndarray,
    sigma: np.ndarray,
    reference: np.ndarray,
    window: np.ndarray,
    attrs: dict[str, Any],
) -> xr.Dataset:
    ny, nx = temperature.shape
    return xr.Dataset(
        {
            TARGET: ((ROW_DIM, COL_DIM), temperature.astype(np.float64), {"units": "uK"}),
            NOISE: ((ROW_DIM, COL_DIM), sigma.astype(np.float64), {"units": "uK"}),
            REFERENCE: ((MULTIPOLE_DIM,), reference.astype(np.float64), {"units": "uK^2"}),
            PIXEL_WINDOW: ((MULTIPOLE_DIM,), window.astype(np.float64)),
        },
        coords={
            ROW_DIM: np.arange(ny, dtype=np.int64),
            COL_DIM: np.arange(nx, dtype=np.int64),
            MULTIPOLE_DIM: np.arange(reference.size, dtype=np.int64),
        },
        attrs=attrs,
    )


def simulate_cmb(
    *,
    n: int = CMB_TRUTH["n"],
    seed: int = 0,
    border: int = CMB_TRUTH["border"],
    pixel_arcmin: float = CMB_TRUTH["pixel_arcmin"],
    beam_fwhm_arcmin: float = CMB_TRUTH["beam_fwhm_arcmin"],
    pixel_subsamples: int = CMB_TRUTH["pixel_subsamples"],
    amplitude: float = CMB_TRUTH["amplitude"],
    tilt: float = CMB_TRUTH["tilt"],
    noise_uK: tuple[float, float] = CMB_TRUTH["noise_uK"],
    hole_radius: float = CMB_TRUTH["hole_radius"],
    edges: tuple[float, ...] = TINY_EDGES,
) -> tuple[xr.Dataset, xr.Dataset]:
    """A masked ``n x n`` map and its truth, simulated from the model's own prior.

    The sky is a Gaussian field on the ``(n + 2 border)^2`` torus whose power is the folded
    :func:`toy_spectrum` times :func:`truth_scale` (so the true ``q_b`` is not 0), cut to
    the central ``n x n`` raster. White noise has an sd that rises from ``noise_uK[0]`` at
    the top row to ``noise_uK[1]`` at the bottom; a disc near the centre and one small
    hole are masked (NaN).

    Returns ``(data, truth)``: ``truth`` holds the noiseless ``sky(row, col)`` and the
    true ``q(bin)`` (the log ratio of the true to the reference power, averaged over the
    torus modes of each bin, as in the notebook's part G).
    """
    rng = np.random.default_rng(seed)
    ref = toy_spectrum()
    window = np.ones_like(ref)
    observed = _mask(n, hole_radius)
    sigma = np.repeat(np.linspace(noise_uK[1], noise_uK[0], n)[:, None], n, axis=1)
    inst = Instrument.from_dl(ref, None, beam_fwhm_arcmin, pixel_arcmin, pixel_subsamples)
    stretch = projection_stretch(n, n, pixel_arcmin * ARCMIN)
    smp = FieldSampler(
        observed,
        sigma,
        inst,
        edges,
        pad=border,
        stretch=stretch,
        ell_dense=0.0,
        nalias=3,
        mean_sd=100.0,
    )
    scale = truth_scale(smp.ell, amplitude, tilt)
    a = np.sqrt(smp.S0 * scale) * rng.standard_normal(smp.S0.shape)
    a[0, 0] = 0.0
    sky = smp.cut(hartley(a))
    y = sky + sigma * rng.standard_normal(sky.shape)
    y = np.where(observed, y, np.nan)
    q_true = np.log(
        [
            np.sum((smp.S0 * scale)[smp.bin == b]) / np.sum(smp.S0[smp.bin == b])
            for b in range(smp.nb)
        ]
    )
    attrs = {
        "source": "sci.cmb_field synthetic",
        "seed": seed,
        "pixel_arcmin": pixel_arcmin,
        "beam_fwhm_arcmin": beam_fwhm_arcmin,
        "amplitude": amplitude,
        "tilt": tilt,
    }
    data = _dataset(y, sigma, ref, window, attrs)
    e = np.asarray(edges, dtype=float)
    truth = xr.Dataset(
        {
            "sky": ((ROW_DIM, COL_DIM), sky, {"units": "uK"}),
            "q": (("bin",), q_true),
        },
        coords={
            ROW_DIM: data[ROW_DIM].values,
            COL_DIM: data[COL_DIM].values,
            "bin": np.arange(len(e) - 1),
            "ell": ("bin", 0.5 * (e[1:] + e[:-1])),
        },
        attrs=attrs,
    )
    return data, truth


def cmb_dataset(*, n: int = CMB_TRUTH["n"], seed: int = 0, **kw: Any) -> xr.Dataset:
    """The data of :func:`simulate_cmb` (without the truth)."""
    return simulate_cmb(n=n, seed=seed, **kw)[0]


# ------------------------------------------------------------------------------ real data


def _dir_and_npz(path: str | Path) -> tuple[Path, Path]:
    p = Path(path)
    if p.is_dir():
        return p, p / CHALLENGES_FILE
    return p.parent, p


def _block(a: np.ndarray, f: int = 2) -> np.ndarray:
    n0, n1 = a.shape[0] // f, a.shape[1] // f
    return np.asarray(a[: n0 * f, : n1 * f].reshape(n0, f, n1, f).mean(axis=(1, 3)))


def _theory_dl(path: Path, lmax: int) -> np.ndarray:
    try:
        raw = np.loadtxt(path, comments="#")
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the theory spectrum {path}: {exc}") from exc
    if raw.ndim != 2 or raw.shape[1] < 2:
        raise ValueError(f"{path} must have the columns L and TT")
    out = np.zeros(lmax + 1)
    ell = raw[:, 0].astype(int)
    ok = (ell >= 2) & (ell <= lmax)
    out[ell[ok]] = raw[ok, 1]
    return out


def from_challenges(path: str | Path, *, patch: str = "lmc") -> xr.Dataset:
    """A Planck SMICA patch (``planck_smica_patches.npz``, or the ``data/`` dir holding it).

    ``patch`` is ``lmc`` (around the Large Magellanic Cloud) or ``north``. The map is
    averaged to 7.5' pixels (128 x 128); a pixel is usable only if all its samples are
    inside the common mask (else ``temperature`` is NaN). ``noise_sd`` is the local rms of
    the half-mission half-difference map, smoothed over 1 degree on the usable pixels, as in
    the notebook. ``reference_dl`` is the Planck 2018 best-fit TT spectrum (read from
    ``planck_theory_tt.txt`` in the same directory) and ``pixel_window`` the HEALPix
    Nside-2048 window, both on ``multipole = 0..4096``.
    """
    root, npz = _dir_and_npz(path)
    if patch not in ("lmc", "north"):
        raise ValueError(f"patch must be 'lmc' or 'north', not {patch!r}")
    raw = np.load(npz, allow_pickle=False)
    need = [f"{patch}_{k}" for k in ("I", "hm1", "hm2", "mask")] + [
        "pixwin_2048",
        "beam_fwhm_arcmin",
        "reso_arcmin",
    ]
    missing = [k for k in need if k not in raw.files]
    if missing:
        raise ValueError(f"{npz} has no {', '.join(missing)}")
    y = _block(raw[f"{patch}_I"]).astype(float)
    observed = _block(raw[f"{patch}_mask"]) == 1
    noise = _block((raw[f"{patch}_hm1"] - raw[f"{patch}_hm2"]) / 2).astype(float)
    num = ndimage.gaussian_filter(np.where(observed, noise**2, 0.0), 8)
    den = ndimage.gaussian_filter(observed.astype(float), 8)
    sigma = np.sqrt(num / np.maximum(den, 1e-3))
    window = np.asarray(raw["pixwin_2048"], dtype=float)
    reference = _theory_dl(root / THEORY_FILE, window.size - 1)
    attrs: dict[str, Any] = {
        "source": "Planck 2018 SMICA (PR3), common mask; via pymc-challenges E79",
        "patch": patch,
        "pixel_arcmin": 2 * float(raw["reso_arcmin"]),
        "beam_fwhm_arcmin": float(raw["beam_fwhm_arcmin"]),
        "pixel_subsamples": 8,
    }
    if f"{patch}_centre" in raw.files:
        attrs["centre_lb_deg"] = [float(v) for v in np.asarray(raw[f"{patch}_centre"]).ravel()]
    return _dataset(np.where(observed, y, np.nan), sigma, reference, window, attrs)


def challenges_reference(path: str | Path, *, patch: str = "lmc") -> xr.Dataset:
    """SMICA's inpainted map ``inpainted(row, col)`` (uK, 7.5' pixels) and Planck's binned
    full-sky spectrum ``planck_dl(planck_bin)`` with ``planck_dl_sd`` (uK^2)."""
    root, npz = _dir_and_npz(path)
    raw = np.load(npz, allow_pickle=False)
    inp = _block(raw[f"{patch}_inp"]).astype(float)
    ny, nx = inp.shape
    out = xr.Dataset(
        {"inpainted": ((ROW_DIM, COL_DIM), inp, {"units": "uK"})},
        coords={ROW_DIM: np.arange(ny, dtype=np.int64), COL_DIM: np.arange(nx, dtype=np.int64)},
    )
    binned = root / BINNED_FILE
    if binned.is_file():
        b = np.loadtxt(binned, comments="#")
        out = out.assign(
            planck_dl=(("planck_bin",), b[:, 1], {"units": "uK^2"}),
            planck_dl_sd=(("planck_bin",), b[:, 3], {"units": "uK^2"}),
        ).assign_coords(planck_ell=("planck_bin", b[:, 0]))
    return out
