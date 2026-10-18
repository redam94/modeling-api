"""Data for ``sci.strong_lens`` (E80): raw schema, example specs, mock lenses, the HST image.

* :data:`LENS_SCHEMA` - ``image(row, col)`` in electrons/s with its inverse-variance
  ``weight(row, col)`` and a ``psf(psf_row, psf_col)`` kernel (all integer, fixed dims);
* :data:`LENS_SPEC_YAML` - a tiny spec (24 x 24 mock, PyMC engine, few draws, a block
  holdout of pixels) for tests and the docs; :data:`SLACS_SPEC_YAML` - the real image;
* :func:`lens_dataset` - a mock observation simulated from the model's own forward process
  (SIE + shear lens, Sersic lens light and source, a Gaussian PSF, sky and photon noise)
  with known truth :data:`LENS_TRUTH`;
* :func:`from_challenges` - the pymc-challenges HST image of SDSS J1627-0053
  (``data/slacs_j1627_f814w.npz``) prepared like the E80 notebook: the 81 x 81 cutout, a
  noise map from the blank sky and the exposure map, the median PSF of the unsaturated
  stars, and the lag-one noise correlation of the drizzled image (``attrs``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema
from scipy import ndimage

from mapi_plugin_science import lens_math as lm

__all__ = [
    "CHALLENGES_NPZ",
    "LENS_SCHEMA",
    "LENS_SPEC_YAML",
    "LENS_TRUTH",
    "SLACS_SPEC_YAML",
    "from_challenges",
    "gaussian_psf",
    "lens_dataset",
    "render_lens",
]

CHALLENGES_NPZ = "slacs_j1627_f814w.npz"
"""File name of the real image in the pymc-challenges ``data/`` directory (see
:func:`mapi_plugin_science.challenges.challenges_file`)."""

LENS_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "row": {"kind": "integer", "policy": "fixed"},
            "col": {"kind": "integer", "policy": "fixed"},
            "psf_row": {"kind": "integer", "policy": "fixed"},
            "psf_col": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            "image": {
                "dims": ["row", "col"],
                "dtype": "float64",
                "role": "target",
                "units": "electron/s",
                "missing": "allow",
            },
            "weight": {
                "dims": ["row", "col"],
                "dtype": "float64",
                "role": "weight",
                "units": "s**2/electron**2",
            },
            "psf": {"dims": ["psf_row", "psf_col"], "dtype": "float64", "role": "constant"},
        },
    }
)

LENS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: mock-strong-lens, project: astronomy}
data:
  dataset: dataset://astronomy/mock_lens@latest
  split: {kind: mask_block, dims: [row, col], fraction: 0.1, block: {row: 4, col: 4}, seed: 3}
model:
  use: sci.strong_lens@^1
  settings:
    pixel_scale: 0.1
    subsample: 1
    lens_light_components: 1
    external_shear: false
    n_starts: 2
    theta_e_median: 0.7
    light_size_medians: [0.4, 1.0]
    source_size_median: 0.15
    source_grid: 16
    source_half_width: 0.4
    curve_points: 64
    curve_draws: 20
  fit: {mode: train_only, seed: 7, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, core.posterior_forest, sci.field_map]}
"""

SLACS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: slacs-j1627-lens, project: astronomy}
data:
  dataset: dataset://astronomy/slacs_j1627_f814w@latest
model:
  use: sci.strong_lens@^1
  settings:
    pixel_scale: 0.05
    subsample: 2
    lens_light_components: 2
    noise_correlation: 0.2
    z_lens: 0.2076
    z_source: 0.5241
  fit: {seed: 80, tune: 400, draws: 600, chains: 4, target_accept: 0.9}
outputs: {plots: [core.convergence, core.posterior_forest, sci.field_map]}
"""

LENS_TRUTH: dict[str, Any] = {
    "theta_E": 0.7,
    "e_mass": (0.08, -0.05),
    "xy_mass": (0.02, -0.01),
    "shear": (0.02, 0.01),
    "R_src": 0.12,
    "n_src": 1.0,
    "e_src": (0.1, 0.05),
    "xy_src": (0.06, -0.04),
    "xy_light": (0.02, -0.01),
    "R_light": (0.4, 1.0),
    "n_light": (2.5, 1.0),
    "e_light": ((0.06, -0.03), (0.03, 0.0)),
    "amplitude": {"lens_light": (1.0, 0.15), "source": 2.0, "sky": 0.0},
}
"""Parameters of :func:`lens_dataset` (arcsec; amplitudes in electrons/s per pixel)."""


def gaussian_psf(fwhm_pixels: float = 1.8, size: int = 7) -> np.ndarray:
    """A normalised circular Gaussian PSF (``size`` x ``size``, odd)."""
    h = size // 2
    yy, xx = np.mgrid[-h : h + 1, -h : h + 1]
    sd = fwhm_pixels / 2.3548
    k = np.exp(-(xx**2 + yy**2) / (2 * sd**2))
    out: np.ndarray = k / k.sum()
    return out


def render_lens(
    truth: dict[str, Any],
    n_row: int,
    n_col: int,
    psf: np.ndarray,
    *,
    pixel_scale: float,
    subsample: int = 2,
    light_components: int = 2,
    shear: bool = True,
) -> np.ndarray:
    """The noiseless image of ``truth`` (NumPy forward model, the same as the plugin's)."""
    x, y = lm.ray_grid(n_row, n_col, pixel_scale, subsample)
    factors = lm.psf_factors(psf, n_row, n_col, tol=1e-6, max_rank=40)

    def pixels(img: np.ndarray) -> np.ndarray:
        out: np.ndarray = lm.convolve(lm.bin_pixels(img, n_row, n_col, subsample), factors)
        return out

    amp = truth["amplitude"]
    xl, yl = truth["xy_light"]
    image = np.full((n_row, n_col), float(amp["sky"]))
    for j in range(light_components):
        e1, e2 = truth["e_light"][j]
        prof = lm.sersic(x, y, truth["R_light"][j], truth["n_light"][j], e1, e2, xl, yl)
        image += amp["lens_light"][j] * pixels(prof)
    g1, g2 = truth["shear"] if shear else (0.0, 0.0)
    (e1m, e2m), (xm, ym) = truth["e_mass"], truth["xy_mass"]
    ax, ay = lm.deflection(x, y, truth["theta_E"], e1m, e2m, xm, ym, g1, g2)
    se1, se2 = truth["e_src"]
    sx, sy = truth["xy_src"]
    src = lm.sersic(x - ax, y - ay, truth["R_src"], truth["n_src"], se1, se2, sx, sy)
    image += amp["source"] * pixels(src)
    return image


def lens_dataset(
    *,
    size: int = 24,
    pixel_scale: float = 0.1,
    subsample: int = 2,
    light_components: int = 1,
    shear: bool = False,
    psf_fwhm_pixels: float = 1.8,
    psf_size: int = 7,
    sky_sd: float = 0.05,
    exptime: float = 1000.0,
    seed: int = 0,
    truth: dict[str, Any] | None = None,
) -> xr.Dataset:
    """A mock observation ``image(row, col)`` of :data:`LENS_TRUTH` with sky and photon noise.

    The noise variance is ``sky_sd^2 + max(model, 0) / exptime`` (the weight is its
    inverse); the PSF is a Gaussian of ``psf_fwhm_pixels``. ``subsample`` is the ray
    sub-grid of the simulation (2 by default, so a fit with 1 is slightly misspecified,
    like real data).
    """
    t = truth or LENS_TRUTH
    rng = np.random.default_rng(seed)
    psf = gaussian_psf(psf_fwhm_pixels, psf_size)
    clean = render_lens(
        t,
        size,
        size,
        psf,
        pixel_scale=pixel_scale,
        subsample=subsample,
        light_components=light_components,
        shear=shear,
    )
    var = sky_sd**2 + np.clip(clean, 0, None) / exptime
    image = clean + np.sqrt(var) * rng.standard_normal(clean.shape)
    h = psf_size // 2
    return xr.Dataset(
        {
            "image": (("row", "col"), image, {"units": "electron/s"}),
            "weight": (("row", "col"), 1.0 / var, {"units": "s**2/electron**2"}),
            "psf": (("psf_row", "psf_col"), psf),
        },
        coords={
            "row": np.arange(size),
            "col": np.arange(size),
            "psf_row": np.arange(-h, h + 1),
            "psf_col": np.arange(-h, h + 1),
        },
        attrs={"pixel_scale": pixel_scale, "truth": "LENS_TRUTH"},
    )


# ================================================================================== real data


def _recentre(stamp: np.ndarray, half: int = 15) -> np.ndarray:
    """A star stamp shifted (Fourier, sub-pixel) to its light-weighted centre and cut."""
    yy, xx = np.indices(stamp.shape) - stamp.shape[0] // 2
    w = np.clip(stamp, 0, None) * (np.hypot(xx, yy) < 4)
    cx, cy = (w * xx).sum() / w.sum(), (w * yy).sum() / w.sum()
    shifted = np.real(np.fft.ifft2(ndimage.fourier_shift(np.fft.fft2(stamp), (-cy, -cx))))
    m = stamp.shape[0] // 2
    cut = shifted[m - half : m + half + 1, m - half : m + half + 1]
    out: np.ndarray = cut / cut.sum()
    return out


def from_challenges(path: str | Path, *, size: int = 81) -> xr.Dataset:
    """The SLACS J1627-0053 HST/ACS F814W image in :data:`LENS_SCHEMA` (as the notebook).

    * the ``size`` x ``size`` cutout around the lens (81: 4.05 arcsec of 0.05 arcsec);
    * the sky sd from the blank patch (objects masked), and the noise map
      ``sqrt(sky^2 + max(smoothed image, 0) / exposure)``; ``weight`` is its inverse square;
    * the PSF: the median of the recentred, normalised stars whose peak is below 60,000
      electrons per exposure (the brightest one is near saturation), 31 x 31;
    * ``attrs``: ``pixel_scale``, the lag-one ``noise_correlation`` of the blank sky (mean
      of the row and column lags; drizzling correlates neighbours), the SLACS redshifts
      ``z_lens``/``z_source`` and the exposure time.
    """
    raw = np.load(Path(path))
    pix = float(raw["pixel_scale"])
    full = np.asarray(raw["sci"], dtype=float)
    c, h = full.shape[0] // 2, size // 2
    sci = full[c - h : c + h + 1, c - h : c + h + 1]
    wht = np.asarray(raw["wht"], dtype=float)[c - h : c + h + 1, c - h : c + h + 1]
    blank = np.asarray(raw["blank_sci"], dtype=float)
    bres = blank - ndimage.median_filter(blank, 25)
    s0 = 1.4826 * np.median(np.abs(bres))
    bmask = ndimage.binary_dilation(
        np.abs(ndimage.uniform_filter(bres, 3)) > 1.5 * s0, iterations=3
    )
    sky_sd = float(bres[~bmask].std())
    good = np.where(bmask, np.nan, bres)
    lag_x = np.nanmean(good[:, 1:] * good[:, :-1]) / np.nanmean(good**2)
    lag_y = np.nanmean(good[1:, :] * good[:-1, :]) / np.nanmean(good**2)
    rho = float(np.clip(0.5 * (lag_x + lag_y), 0.0, 0.44))
    sig = np.sqrt(sky_sd**2 + np.clip(ndimage.uniform_filter(sci, 3), 0, None) / wht)
    stars = np.asarray(raw["star_stamps"], dtype=float)
    exptime = float(raw["exptime"])
    peak_e = stars.max(axis=(1, 2)) * exptime / 4
    stack = np.array([_recentre(st) for st in stars[peak_e < 60000]])
    psf = np.clip(np.median(stack, axis=0), 0, None)
    psf /= psf.sum()
    hp = psf.shape[0] // 2
    return xr.Dataset(
        {
            "image": (("row", "col"), sci, {"units": "electron/s"}),
            "weight": (("row", "col"), 1.0 / sig**2, {"units": "s**2/electron**2"}),
            "psf": (("psf_row", "psf_col"), psf),
        },
        coords={
            "row": np.arange(size),
            "col": np.arange(size),
            "psf_row": np.arange(-hp, hp + 1),
            "psf_col": np.arange(-hp, hp + 1),
        },
        attrs={
            "pixel_scale": pix,
            "noise_correlation": rho,
            "sky_sd": sky_sd,
            "exptime": exptime,
            "z_lens": 0.2076,
            "z_source": 0.5241,
            "source": "HST/ACS F814W, SDSS J1627-0053 (programme 10494, MAST)",
        },
    )
