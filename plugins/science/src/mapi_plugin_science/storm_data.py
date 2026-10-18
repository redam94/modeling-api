"""Data for ``sci.storm_localisation`` (E58): schema, a tiny spec, synthetic movies, real frames.

* :data:`STORM_SCHEMA` - the raw schema (dataset names equal the canonical names):
  ``frames(frame, row, col)`` in ADU;
* :data:`STORM_SPEC_YAML` - a tiny fit (16 spots of a 60-frame 24 x 24 movie, pymc NUTS);
* :func:`simulate_storm` / :func:`storm_dataset` - a movie simulated from the model's own
  forward process (pixel-integrated Gaussian PSF on a sloped background, Poisson photons,
  an EMCCD gain register, read noise and the offset), with the true blinks;
* :func:`from_challenges` - the 2,000 raw EMCCD frames (40 x 40 px, 127 nm) of the Unc-13
  dSTORM recording of pymc-challenges E58 (Dannhauser et al. 2022) in the same schema;
  :func:`challenges_settings` gives the camera calibration of the notebook (the
  photon-transfer curve of the full 200 x 200 field) as settings and
  :func:`challenges_reference` the numbers the notebook prints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.storm import (
    COL_DIM,
    FRAME_DIM,
    ROW_DIM,
    TARGET,
    photon_transfer,
    pixel_gauss,
)

__all__ = [
    "CHALLENGES_FILE",
    "STORM_SCHEMA",
    "STORM_SPEC_YAML",
    "STORM_TRUTH",
    "challenges_reference",
    "challenges_settings",
    "challenges_spec",
    "from_challenges",
    "simulate_emccd",
    "simulate_storm",
    "storm_dataset",
]

CHALLENGES_FILE = "dannhauser2022_unc13_dstorm_roi.npz"
"""The file name of the real recording in the pymc-challenges ``data/`` directory."""

STORM_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            FRAME_DIM: {"kind": "integer", "policy": "fixed"},
            ROW_DIM: {"kind": "integer", "policy": "fixed"},
            COL_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": [FRAME_DIM, ROW_DIM, COL_DIM],
                "dtype": "int32",
                "role": "target",
                "units": "ADU",
                "description": "Raw EMCCD counts of each frame",
            }
        },
    }
)
"""Raw schema of :func:`storm_dataset` and :func:`from_challenges`."""

STORM_TRUTH: dict[str, Any] = {
    "pixel_nm": 127.0,
    "background": 110.0,
    "background_slope": 0.3,
    "psf_sd_px": 1.05,
    "photons_median": 1500.0,
    "photons_log_sd": 0.4,
    "gain": 6.9,
    "offset": 457.0,
    "read_sd": 5.0,
}
"""The camera, PSF and brightness of :func:`simulate_storm` by default (the real recording's:
EM gain 6.9 ADU per photon, so ``scale_adu = 13.8``; offset 457 ADU; 110 photons of
background per pixel; a PSF sd of 1.05 px = 133 nm)."""

STORM_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: storm-synthetic, project: microscopy}
data:
  dataset: dataset://microscopy/storm_synthetic@latest
model:
  use: sci.storm_localisation@^1
  settings:
    pixel_nm: 127
    offset_adu: 457
    scale_adu: 13.8
    max_spots: 16
    score_draws: 20
    ppc_draws: 10
    render_px_nm: 16
  fit: {seed: 58, tune: 150, draws: 100, chains: 2, engine: pymc, keep: {log_likelihood: true}}
evaluate: {metrics: [rmse, mae, coverage_90, elpd_loo]}
outputs: {plots: [core.convergence, sci.point_pattern, sci.image_residuals]}
"""
"""A tiny fit of :func:`storm_dataset`: 16 spots, the simulated camera's offset and gain."""


def simulate_emccd(
    photons: np.ndarray,
    rng: np.random.Generator,
    *,
    gain: float = STORM_TRUTH["gain"],
    offset: float = STORM_TRUTH["offset"],
    read_sd: float = STORM_TRUTH["read_sd"],
) -> np.ndarray:
    """Expected photons per pixel -> ADU: Poisson photons, a Gamma gain register, Gaussian
    read noise, the offset, rounded to integers (the notebook's camera)."""
    n = rng.poisson(photons)
    charge = np.where(n > 0, rng.gamma(np.maximum(n, 1), gain), 0.0)
    return np.round(charge + offset + rng.normal(0.0, read_sd, n.shape))


def simulate_storm(
    *,
    frames: int = 60,
    size: int = 24,
    rate: float = 0.6,
    margin: float = 4.0,
    seed: int = 0,
    pixel_nm: float = STORM_TRUTH["pixel_nm"],
    background: float = STORM_TRUTH["background"],
    background_slope: float = STORM_TRUTH["background_slope"],
    psf_sd_px: float = STORM_TRUTH["psf_sd_px"],
    photons_median: float = STORM_TRUTH["photons_median"],
    photons_log_sd: float = STORM_TRUTH["photons_log_sd"],
    gain: float = STORM_TRUTH["gain"],
    offset: float = STORM_TRUTH["offset"],
    read_sd: float = STORM_TRUTH["read_sd"],
) -> tuple[xr.Dataset, xr.Dataset]:
    """A raw STORM movie ``frames(frame, row, col)`` and its true blinks.

    Each frame shows a Poisson number (mean ``rate``) of blinking molecules at uniform
    positions at least ``margin`` px from the edges, each with a lognormal number of photons
    and the pixel-integrated Gaussian PSF of sd ``psf_sd_px``. The background (photons per
    pixel) is ``background`` times a linear ramp of total relative change
    ``background_slope`` across the field, so the pixels span a range of brightness and the
    photon-transfer curve of the frames is defined. The camera is :func:`simulate_emccd`.

    Returns ``(data, truth)``: ``truth`` holds ``frame``, ``x`` and ``y`` (pixel positions:
    column and row) and ``photons`` along dim ``emitter``, with the settings in attrs.
    """
    rng = np.random.default_rng(seed)
    ramp = (np.arange(size) - (size - 1) / 2) / max(size - 1, 1)
    bg = background * (1.0 + background_slope * (ramp[None, :] + 0.5 * ramp[:, None]))
    mu = np.broadcast_to(bg, (frames, size, size)).copy()
    counts = rng.poisson(rate, frames)
    t = np.repeat(np.arange(frames), counts)
    n = t.size
    x = rng.uniform(margin, size - 1 - margin, n)
    y = rng.uniform(margin, size - 1 - margin, n)
    N = rng.lognormal(np.log(photons_median), photons_log_sd, n)
    s = np.full(n, psf_sd_px)
    ex, ey = pixel_gauss(x, s, size), pixel_gauss(y, s, size)
    np.add.at(mu, t, N[:, None, None] * ey[:, :, None] * ex[:, None, :])
    adu = simulate_emccd(mu, rng, gain=gain, offset=offset, read_sd=read_sd)
    hyper = {
        "seed": seed,
        "pixel_nm": pixel_nm,
        "background": background,
        "background_slope": background_slope,
        "psf_sd_px": psf_sd_px,
        "photons_median": photons_median,
        "gain": gain,
        "offset": offset,
        "scale": 2.0 * gain,
        "read_sd": read_sd,
    }
    data = xr.Dataset(
        {TARGET: ((FRAME_DIM, ROW_DIM, COL_DIM), adu.astype(np.int32), {"units": "ADU"})},
        coords={
            FRAME_DIM: np.arange(frames, dtype=np.int64),
            ROW_DIM: np.arange(size, dtype=np.int64),
            COL_DIM: np.arange(size, dtype=np.int64),
        },
        attrs={"source": "sci.storm_localisation synthetic", "pixel_nm": pixel_nm},
    )
    truth = xr.Dataset(
        {
            "frame": ("emitter", t.astype(np.int64)),
            "x": ("emitter", x),
            "y": ("emitter", y),
            "photons": ("emitter", N),
        },
        coords={"emitter": np.arange(n)},
        attrs=hyper,
    )
    return data, truth


def storm_dataset(*, frames: int = 60, size: int = 24, seed: int = 0, **kw: Any) -> xr.Dataset:
    """The data of :func:`simulate_storm` (without the truth)."""
    return simulate_storm(frames=frames, size=size, seed=seed, **kw)[0]


def _load(path: str | Path) -> Any:
    p = Path(path)
    if p.is_dir():
        p = p / CHALLENGES_FILE
    return np.load(p, allow_pickle=False)


def from_challenges(path: str | Path) -> xr.Dataset:
    """The Unc-13 dSTORM frames (``dannhauser2022_unc13_dstorm_roi.npz``, or the ``data/``
    directory holding it) as ``frames(frame, row, col)`` (int32 ADU).

    Frames, rows and columns are numbered from 0 within the crop, as in the notebook. The
    attrs keep the crop origin, the pixel size, the exposure and the camera calibration of
    the notebook: ``offset_adu`` and ``scale_adu`` from the photon-transfer curve of the full
    200 x 200 field (see :func:`challenges_settings`).
    """
    raw = _load(path)
    need = {"frames", "pixel_nm", "field_median_adu", "field_diff_var_adu2"}
    missing = need - set(raw.files)
    if missing:
        raise ValueError(f"{path}: missing arrays {sorted(missing)}")
    frames = np.asarray(raw["frames"])
    if frames.ndim != 3:
        raise ValueError(f"{path}: frames must be (frame, row, col), got {frames.shape}")
    offset, scale = photon_transfer(raw["field_median_adu"], raw["field_diff_var_adu2"])
    T, H, W = frames.shape
    attrs: dict[str, Any] = {
        "source": "Dannhaeuser et al. (2022), Unc-13 dSTORM (Zenodo 7328805, CC BY 4.0), via "
        "pymc-challenges E58",
        "pixel_nm": float(raw["pixel_nm"]),
        "offset_adu": offset,
        "scale_adu": scale,
    }
    for k in ("exposure_s", "first_frame", "roi_row", "roi_col"):
        if k in raw.files:
            attrs[k] = float(raw[k]) if k == "exposure_s" else int(raw[k])
    return xr.Dataset(
        {TARGET: ((FRAME_DIM, ROW_DIM, COL_DIM), frames.astype(np.int32), {"units": "ADU"})},
        coords={
            FRAME_DIM: np.arange(T, dtype=np.int64),
            ROW_DIM: np.arange(H, dtype=np.int64),
            COL_DIM: np.arange(W, dtype=np.int64),
        },
        attrs=attrs,
    )


def challenges_settings(ds: xr.Dataset, **overrides: Any) -> dict[str, Any]:
    """Model settings for :func:`from_challenges` data: the notebook's camera calibration
    and pixel size, and 200 isolated spots."""
    out: dict[str, Any] = {
        "pixel_nm": float(ds.attrs["pixel_nm"]),
        "offset_adu": float(ds.attrs["offset_adu"]),
        "scale_adu": float(ds.attrs["scale_adu"]),
        "max_spots": 200,
    }
    out.update(overrides)
    return out


def challenges_spec(ds: xr.Dataset) -> str:
    """The suggested spec for the real recording (the notebook's NUTS settings)."""
    s = challenges_settings(ds)
    settings = ", ".join(f"{k}: {v!r}" for k, v in s.items())
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: unc13-dstorm-localisation, project: microscopy}}
data:
  dataset: dataset://microscopy/dannhauser2022_unc13_dstorm@latest
model:
  use: sci.storm_localisation@^1
  settings: {{{settings}}}
  fit: {{seed: 58, tune: 1000, draws: 1000, chains: 4, engine: nutpie}}
evaluate: {{metrics: [rmse, mae]}}
outputs: {{plots: [core.convergence, sci.point_pattern, sci.image_residuals, sci.field_map]}}
"""


def challenges_reference() -> dict[str, Any]:
    """The numbers E58 prints for the real recording (sections 1, 3 and 7).

    ``spot`` is the notebook's single-spot NUTS fit of blob 12 (frame 5): patch-pixel
    posterior means and sds and the physical summaries.
    """
    return {
        "scale_adu": 13.9,
        "em_gain": 6.9,
        "offset_adu": 457.0,
        "background_photons": 110.0,
        "n_blobs": 4192,
        "spot": {
            "blob": 12,
            "frame": 5,
            "x_px": (4.461, 0.056),
            "y_px": (4.399, 0.053),
            "logN": (7.675, 0.053),
            "logb": (4.720, 0.014),
            "logs": (-0.017, 0.041),
            "x_nm_sd": 7.1,
            "y_nm_sd": 6.7,
            "photons": 2157.0,
            "background": 112.0,
            "psf_sd_nm": 125.0,
        },
        "median_localisation_sd_nm": 9.5,
        "median_photons": 2328.0,
    }
