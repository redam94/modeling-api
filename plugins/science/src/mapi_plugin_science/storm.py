"""``sci.storm_localisation``: single-molecule localisation in raw STORM frames (E58).

Canonical inputs::

    dims  frame: integer, fixed (camera frames)
          row, col: integer, fixed (camera pixels; consecutive integers)
    vars  frames(frame, row, col): target, int32, ADU - the raw camera counts

**Camera.** An EMCCD at high gain turns ``n`` photons into a charge with mean ``g n`` and
variance ``2 g^2 n``. Its photon-transfer curve is the straight line ``var = s (mean -
offset)`` with slope ``s = 2 g``, so ``z = round((ADU - offset) / s)`` (clipped at 0) are
*effective counts*: Poisson-like with mean photons / 2. The offset and slope come from the
settings (``offset_adu``, ``scale_adu``) or, when both are unset, from the photon-transfer
curve of the movie itself: the per-pixel time median against a robust per-pixel variance of
frame-to-frame differences, with the brightest 10 % of pixels left out (``np.polyfit``, as in
the notebook).

**Detection** (NumPy / ``scipy.ndimage``, at fit time). Each frame minus the per-pixel time
median of ``z`` is smoothed with a Gaussian of ``smooth_px``; connected regions (4-connected,
within one frame) above ``threshold_sd`` robust noise sds are blobs. Every blob gets a
``patch_px`` x ``patch_px`` patch around it. A blob is *isolated* when no pixel of another
blob (dilated by 2 px) falls in its patch and its size is ``min_blob_px`` to ``max_blob_px``
pixels. ``max_spots`` isolated blobs are fitted, evenly spaced in frame order, optionally
only those whose amplitude lies in a quantile band (``amplitude_band``).

**Model.** One emitter per patch, every spot with its own parameters (a batched version of
the notebook's ``spot_model`` with ``K = 1``). For pixel ``(i, j)`` of patch ``k``::

    mu[k, i, j] = b[k, i, j] + N_k * E_i(y_k, s_k) * E_j(x_k, s_k)
    E_i(u, s)   = (erf((i + 1/2 - u) / (sqrt2 s)) - erf((i - 1/2 - u) / (sqrt2 s))) / 2
    z[k, i, j] ~ Poisson(mu[k, i, j] / 2)

a pixel-integrated Gaussian PSF on a background ``b`` (photons per pixel). Priors (notebook):
``log b ~ N(log(2 median z), 0.5)``, ``log s ~ N(log psf_sd_px, 0.2)``, ``x, y ~ N(blob
centre, 2 px)``, ``log N ~ N(log 1500, 1)``. ``kind`` picks the background: ``flat`` (the
notebook) or ``plane`` (``log b + gx (j - c) + gy (i - c)``, the notebook's first "try it
yourself" fix for real tissue frames), so a workflow can compare the two by ``elpd_loo`` on
the same patches (``fit.keep.log_likelihood: true`` stores the pointwise values per pixel).

**Outputs** (representation ``draws``, ``score_draws`` evenly spaced posterior draws):

* ``x``, ``y`` (spot) - positions in nm in the movie (column / row label x ``pixel_nm``);
  ``photons``, ``background`` (photons per pixel, the patch centre) and ``psf_sd`` (nm);
* ``render(render_y, render_x)`` - the super-resolved image: each draw places every spot as
  a small Gaussian (half a render pixel) at its drawn position, so the posterior mean is the
  notebook's "each localisation a Gaussian of its posterior sd" rendering (grid);
* ``patch_fit(spot, py, px)`` - expected effective counts of each patch (or Poisson draws);
* ``frames_hat(frame, row, col)`` - the camera image in ADU of every scored frame: inside a
  fitted patch ``offset + scale x patch_fit``, elsewhere the background (the per-pixel time
  median, plus Poisson noise for ``kind: predictive``). ``evaluate`` compares it with the
  observed frames; spots that were not fitted count as misfit. A score
  is refused (``PAYLOAD_TOO_LARGE``) above :data:`MAX_FRAME_VALUES` values: score a window of
  frames, for example the frames with a fitted spot (``constant_data/spot_frame``).

The bundle also keeps ``posterior_predictive/patch_fit`` (``ppc_draws`` Poisson draws) and
``observed_data/patch_fit`` so ``sci.image_residuals`` shows data, fit and residual per spot,
and ``predictions/render`` (``score_draws`` draws) so ``sci.point_pattern`` and
``sci.field_map`` draw the super-resolved image from the fit alone (a whole long movie is too
large for the in-sample evaluate the runner would otherwise draw them from).
A score uses only the fitted spots: it cannot localise spots in frames it has not seen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.checkpoint import CheckpointSink, fit_key
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import EngineHooks, EnginePlan, run_engine
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field
from scipy import ndimage as ndi
from scipy.special import erf, gammaln

__all__ = [
    "COL_DIM",
    "FRAME_DIM",
    "PATCH_X",
    "PATCH_Y",
    "RENDER_X",
    "RENDER_Y",
    "ROW_DIM",
    "SPOT_DIM",
    "TARGET",
    "Spots",
    "StormLocalisationModel",
    "StormLocalisationSettings",
    "blob_patches",
    "detect",
    "expected_photons",
    "find_spots",
    "frame_statistics",
    "photon_transfer",
    "pixel_gauss",
    "render_points",
    "to_counts",
]

FRAME_DIM = "frame"
ROW_DIM = "row"
COL_DIM = "col"
TARGET = "frames"
SPOT_DIM = "spot"
PATCH_Y = "py"
PATCH_X = "px"
RENDER_Y = "render_y"
RENDER_X = "render_x"
PATCH = "patch_fit"
FREE = ("x_px", "y_px", "logN", "logb", "logs")
PLANE = ("grad_x", "grad_y")
SQ2 = math.sqrt(2.0)
Kind = Literal["flat", "plane"]
MAX_FRAME_VALUES = 50_000_000
"""Most values of ``frames_hat`` in one score (frames x rows x cols x draws)."""
#: the bundle group with ``render`` (``score_draws`` draws), found by the sci plots
RENDER_GROUP = "predictions"

# 4-connected blobs within one frame, never across frames
_FOUR = np.array([[[0, 0, 0]] * 3, [[0, 1, 0], [1, 1, 1], [0, 1, 0]], [[0, 0, 0]] * 3])


# ============================================================================== camera


def frame_statistics(adu: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel time median and robust temporal variance of a movie ``(frame, row, col)``.

    The variance is ``(1.4826 MAD(diff))^2 / 2`` of the frame-to-frame differences, so
    blinks and slow drifts barely move it (the notebook's ``field_diff_var_adu2``).
    """
    a = np.asarray(adu, dtype=float)
    med = np.median(a, axis=0)
    d = np.diff(a, axis=0)
    mad = np.median(np.abs(d - np.median(d, axis=0)), axis=0)
    return med, (1.4826 * mad) ** 2 / 2.0


def photon_transfer(
    median: np.ndarray, variance: np.ndarray, keep_quantile: float = 0.9
) -> tuple[float, float]:
    """``(offset, slope)`` of the line ``variance = slope (median - offset)`` fitted to the
    pixels below the ``keep_quantile`` quantile of the median (bright pixels hold spots)."""
    med = np.asarray(median, dtype=float).ravel()
    var = np.asarray(variance, dtype=float).ravel()
    keep = med < np.quantile(med, keep_quantile)
    if keep.sum() < 3 or np.ptp(med[keep]) <= 0:
        raise ValueError("the pixel medians do not vary: the photon-transfer slope is undefined")
    slope, icpt = np.polyfit(med[keep], var[keep], 1)
    if not slope > 0:
        raise ValueError(f"the photon-transfer slope is {slope:.3g}; it must be positive")
    return float(-icpt / slope), float(slope)


def to_counts(adu: np.ndarray, offset: float, scale: float) -> np.ndarray:
    """ADU -> effective counts (Poisson-like, mean = photons / 2)."""
    return np.maximum(np.round((np.asarray(adu, dtype=float) - offset) / scale), 0.0)


# ============================================================================== forward model


def pixel_gauss(u: np.ndarray, sig: np.ndarray, n: int) -> np.ndarray:
    """Pixel-integrated 1-D Gaussian at pixels ``0..n-1`` for centres ``u (...)`` and widths
    ``sig (...)``: an ``(..., n)`` array (NumPy twin of the PyTensor graph)."""
    i = np.arange(n)
    u = np.asarray(u, dtype=float)[..., None]
    s = np.asarray(sig, dtype=float)[..., None]
    return 0.5 * (erf((i + 0.5 - u) / (SQ2 * s)) - erf((i - 0.5 - u) / (SQ2 * s)))


def expected_photons(
    x: np.ndarray,
    y: np.ndarray,
    logN: np.ndarray,
    logb: np.ndarray,
    logs: np.ndarray,
    P: int,
    grad_x: np.ndarray | None = None,
    grad_y: np.ndarray | None = None,
) -> np.ndarray:
    """Expected photons ``(..., P, P)`` of one emitter per patch (patch pixel units).

    Every argument has the same leading shape ``(...)``; ``grad_x``/``grad_y`` add the plane
    background ``exp(logb + gx (j - c) + gy (i - c))`` with ``c = (P - 1) / 2``.
    """
    s = np.exp(logs)
    ex = pixel_gauss(x, s, P)
    ey = pixel_gauss(y, s, P)
    img = np.exp(logN)[..., None, None] * ey[..., :, None] * ex[..., None, :]
    lb = np.asarray(logb, dtype=float)[..., None, None]
    if grad_x is not None and grad_y is not None:
        c = (P - 1) / 2.0
        idx = np.arange(P) - c
        lb = (
            lb
            + np.asarray(grad_x)[..., None, None] * idx[None, :]
            + np.asarray(grad_y)[..., None, None] * idx[:, None]
        )
    return np.exp(lb) + img


def _pixel_gauss_pt(u: Any, s: Any, P: int) -> Any:
    i = np.arange(P, dtype=float)
    return 0.5 * (
        pt.erf((i + 0.5 - u[:, None]) / (SQ2 * s[:, None]))
        - pt.erf((i - 0.5 - u[:, None]) / (SQ2 * s[:, None]))
    )


# ============================================================================== detection


def detect(
    z: np.ndarray, bg: np.ndarray, thr_sd: float = 5.0, smooth: float = 1.0
) -> tuple[np.ndarray, np.ndarray, int, float]:
    """``(smoothed movie, blob labels, number of blobs, threshold)``: connected blobs of the
    smoothed, background-subtracted movie above ``thr_sd`` robust noise sds."""
    sm = ndi.gaussian_filter(z - bg, sigma=(0, smooth, smooth))
    thr = thr_sd * 1.4826 * float(np.median(np.abs(sm - np.median(sm))))
    lab, n = ndi.label(sm > thr, structure=_FOUR)
    return sm, lab, int(n), thr


def blob_patches(
    z: np.ndarray, lab: np.ndarray, n: int, P: int = 11
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """A ``P x P`` patch around each blob ``(n, P, P)``, a mask that excludes pixels of
    *other* blobs dilated by 2 px ``(n, P, P)``, the patch origin ``(frame, row, col)`` as
    positions ``(n, 3)`` and the blob centre in patch coordinates ``(x, y)`` ``(n, 2)``."""
    _, H, W = z.shape
    Z = np.zeros((n, P, P))
    Wm = np.zeros((n, P, P), dtype=bool)
    org = np.zeros((n, 3), dtype=np.int64)
    cen = np.zeros((n, 2))
    st = ndi.generate_binary_structure(2, 1)
    for i, sl in enumerate(ndi.find_objects(lab)):
        if sl is None:  # pragma: no cover - labels are consecutive
            continue
        t = sl[0].start
        cy = (sl[1].start + sl[1].stop - 1) / 2
        cx = (sl[2].start + sl[2].stop - 1) / 2
        r0 = int(np.clip(round(cy) - P // 2, 0, H - P))
        c0 = int(np.clip(round(cx) - P // 2, 0, W - P))
        Z[i] = z[t, r0 : r0 + P, c0 : c0 + P]
        near = lab[t, r0 : r0 + P, c0 : c0 + P]
        Wm[i] = ~ndi.binary_dilation((near > 0) & (near != i + 1), st, iterations=2)
        org[i] = (t, r0, c0)
        cen[i] = (cx - c0, cy - r0)
    return Z, Wm, org, cen


@dataclass(slots=True)
class Spots:
    """The calibrated movie's detected, isolated spots that the model fits."""

    patches: np.ndarray  # (B, P, P) effective counts
    origin: np.ndarray  # (B, 3) positions (frame, row, col) of the patch corner
    centre: np.ndarray  # (B, 2) blob centre (x, y) in patch pixels
    amplitude: np.ndarray  # (B,) peak of the smoothed blob (effective counts)
    background: np.ndarray  # (row, col) per-pixel time median of the effective counts
    offset: float
    scale: float
    calibration: str  # "settings" or "frames"
    n_blobs: int
    n_isolated: int
    threshold: float


def find_spots(adu: np.ndarray, s: StormLocalisationSettings) -> Spots:
    """Calibrate, detect and select the spots of a movie ``(frame, row, col)`` in ADU."""
    P = s.patch_px
    if s.offset_adu is not None and s.scale_adu is not None:
        offset, scale, how = float(s.offset_adu), float(s.scale_adu), "settings"
    else:
        try:
            offset, scale = photon_transfer(*frame_statistics(adu))
        except ValueError as exc:
            raise MapiError(
                f"the camera cannot be calibrated from the frames ({exc}); set offset_adu "
                "and scale_adu",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            ) from exc
        how = "frames"
    z = to_counts(adu, offset, scale)
    bg = np.median(z, axis=0)
    S, lab, n, thr = detect(z, bg, s.threshold_sd, s.smooth_px)
    if n == 0:
        raise MapiError(
            f"no spot in the frames is brighter than {s.threshold_sd:g} noise sds",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
        )
    Z, Wm, org, cen = blob_patches(z, lab, n, P)
    size = np.bincount(lab.ravel(), minlength=n + 1)[1:]
    amp = np.array([S[sl].max() for sl in ndi.find_objects(lab)])
    alone = Wm.reshape(n, -1).all(axis=1) & (size >= s.min_blob_px) & (size <= s.max_blob_px)
    idx = np.flatnonzero(alone)
    if idx.size and s.amplitude_band != (0.0, 1.0):
        lo, hi = np.quantile(amp[idx], s.amplitude_band)
        band = (amp[idx] > lo) & (amp[idx] < hi)
        idx = idx[band]
    if not idx.size:
        raise MapiError(
            f"the frames have {n} blobs but none is isolated in its {P} x {P} patch with "
            f"{s.min_blob_px}-{s.max_blob_px} pixels (and in the amplitude band)",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
        )
    if idx.size > s.max_spots:
        idx = idx[np.unique(np.linspace(0, idx.size - 1, s.max_spots).round().astype(np.int64))]
    return Spots(
        patches=Z[idx],
        origin=org[idx],
        centre=cen[idx],
        amplitude=amp[idx],
        background=bg,
        offset=offset,
        scale=scale,
        calibration=how,
        n_blobs=n,
        n_isolated=int(alone.sum()),
        threshold=thr,
    )


def render_points(
    x_nm: np.ndarray,
    y_nm: np.ndarray,
    x_edges: np.ndarray,
    y_edges: np.ndarray,
) -> np.ndarray:
    """Points ``(..., n)`` -> images ``(..., ny, nx)``: a histogram on the render grid
    blurred by half a render pixel (each point carries weight 1)."""
    lead = np.shape(x_nm)[:-1]
    xs = np.reshape(x_nm, (-1, np.shape(x_nm)[-1]))
    ys = np.reshape(y_nm, (-1, np.shape(y_nm)[-1]))
    out = np.empty((xs.shape[0], y_edges.size - 1, x_edges.size - 1), dtype=np.float32)
    for k in range(xs.shape[0]):
        h, _, _ = np.histogram2d(ys[k], xs[k], bins=[y_edges, x_edges])
        out[k] = ndi.gaussian_filter(h, 0.5, mode="constant")
    return out.reshape(*lead, y_edges.size - 1, x_edges.size - 1)


# ============================================================================== settings


class StormLocalisationSettings(ModelSettings):
    """Settings of ``sci.storm_localisation``: camera, detection, priors and outputs."""

    pixel_nm: float = Field(
        100.0, gt=0, description="Size of one camera pixel in the sample (nm) (x-ui-level: basic)"
    )
    kind: Kind = Field(
        "flat",
        description="Background in each patch: flat (one value) or plane (a value and a "
        "slope in x and y) (x-ui-level: basic)",
    )
    offset_adu: float | None = Field(
        None,
        description="Camera offset (ADU). Set it with scale_adu; unset, both come from the "
        "photon-transfer curve of the frames",
    )
    scale_adu: float | None = Field(
        None,
        gt=0,
        description="ADU per effective count: twice the EM gain (the photon-transfer slope)",
    )
    max_spots: int = Field(
        200,
        ge=1,
        le=5000,
        description="Isolated spots fitted, evenly spaced in frame order (x-ui-level: basic)",
    )
    threshold_sd: float = Field(5.0, gt=0, description="Detection threshold (robust noise sds)")
    smooth_px: float = Field(1.0, gt=0, description="Gaussian smoothing before detection (px)")
    patch_px: int = Field(11, ge=5, le=31, description="Side of each fitted patch (odd, px)")
    min_blob_px: int = Field(13, ge=1, description="Fewest pixels of a fitted blob")
    max_blob_px: int = Field(39, ge=1, description="Most pixels of a fitted blob")
    amplitude_band: tuple[float, float] = Field(
        (0.0, 1.0),
        description="Quantile band of blob amplitude that is fitted (0, 1: every isolated blob)",
    )
    photons_median: float = Field(1500.0, gt=0, description="Prior median of the photons per spot")
    photons_log_sd: float = Field(1.0, gt=0, description="Log-scale prior sd of the photons")
    psf_sd_px: float = Field(1.0, gt=0, description="Prior median of the PSF sd (px)")
    psf_log_sd: float = Field(0.2, gt=0, description="Log-scale prior sd of the PSF sd")
    position_sd_px: float = Field(
        2.0, gt=0, description="Prior sd of the position around the blob centre (px)"
    )
    background_photons: float | None = Field(
        None,
        gt=0,
        description="Prior median of the background (photons per pixel); unset: twice the "
        "median effective count of the movie",
    )
    background_log_sd: float = Field(0.5, gt=0, description="Log-scale prior sd of the background")
    gradient_sd: float = Field(
        0.05, gt=0, description="Prior sd of the background slopes (log per px; kind plane)"
    )
    score_draws: int = Field(
        40, ge=1, le=1000, description="Posterior draws in each score (over all chains)"
    )
    ppc_draws: int = Field(
        20, ge=0, le=200, description="Predictive patch draws kept in the bundle (0: none)"
    )
    render_px_nm: float = Field(
        16.0, gt=0, description="Pixel size of the super-resolved render (nm)"
    )


_TEMPLATE: dict[str, Any] = {
    "template": {
        "dims": {
            FRAME_DIM: {"kind": "integer", "policy": "fixed", "description": "Camera frame"},
            ROW_DIM: {"kind": "integer", "policy": "fixed", "description": "Pixel row"},
            COL_DIM: {"kind": "integer", "policy": "fixed", "description": "Pixel column"},
        },
        "vars": {
            TARGET: {
                "dims": [FRAME_DIM, ROW_DIM, COL_DIM],
                "dtype": "int32",
                "role": "target",
                "units": "ADU",
                "bounds": [0, None],
                "description": "Raw camera counts of each frame",
            }
        },
    }
}


# ============================================================================== the model


class StormLocalisationModel(CustomModel[StormLocalisationSettings]):
    """Bayesian localisation of single molecules in raw EMCCD STORM frames (E58)."""

    id = "sci.storm_localisation"
    version = "1.0.0"
    Settings = StormLocalisationSettings
    description = (
        "Single-molecule localisation microscopy (STORM/PALM): finds isolated spots in raw "
        "EMCCD frames and fits each with a pixel-integrated Gaussian PSF on a background, "
        "with Poisson counts after the camera's photon-transfer calibration. Outputs are "
        "positions with nm error bars, photons, the PSF width, a super-resolved render and "
        "the fitted patches."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["x", "y", "photons", "psf_sd"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "x": {"dims": [SPOT_DIM], "units": "nm", "description": "Column position of each spot"},
        "y": {"dims": [SPOT_DIM], "units": "nm", "description": "Row position of each spot"},
        "photons": {
            "dims": [SPOT_DIM],
            "units": "photons",
            "description": "Photons that each molecule emitted into the frame",
        },
        "background": {
            "dims": [SPOT_DIM],
            "units": "photons/px",
            "description": "Background at the centre of each patch",
        },
        "psf_sd": {"dims": [SPOT_DIM], "units": "nm", "description": "PSF sd of each spot"},
        "render": {
            "dims": [RENDER_Y, RENDER_X],
            "units": "localisations",
            "description": "Super-resolved image: the spots at the drawn positions, blurred "
            "by half a render pixel",
            "grid": {"y": RENDER_Y, "x": RENDER_X, "origin": "upper", "colormap": "magma"},
        },
        PATCH: {
            "dims": [SPOT_DIM, PATCH_Y, PATCH_X],
            "units": "counts",
            "description": "Expected effective counts (photons / 2) of each fitted patch, or "
            "Poisson draws",
            "grid": {
                "y": PATCH_Y,
                "x": PATCH_X,
                "frame": SPOT_DIM,
                "origin": "upper",
                "colormap": "magma",
            },
        },
        "frames_hat": {
            "dims": [FRAME_DIM, ROW_DIM, COL_DIM],
            "units_of": TARGET,
            "description": "Camera image (ADU): the fitted spots on the background",
            "grid": {
                "y": ROW_DIM,
                "x": COL_DIM,
                "frame": FRAME_DIM,
                "origin": "upper",
                "colormap": "gray",
            },
        },
    }
    plots = ("core.convergence", "sci.point_pattern", "sci.image_residuals", "sci.field_map")
    # 200 spots x 5 parameters: about a minute of NUTS per 1000 draws and chain
    resources = ResourceHint(est_fit_seconds=120)
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== data
    @staticmethod
    def movie(ds: xr.Dataset) -> xr.DataArray:
        """The target ``frames(frame, row, col)`` sorted, with typed checks."""
        for d in (FRAME_DIM, ROW_DIM, COL_DIM):
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        if TARGET not in ds:
            raise MapiError(f"the data has no variable {TARGET!r}", code=ErrorCode.SCHEMA_INVALID)
        da = ds[TARGET].sortby([FRAME_DIM, ROW_DIM, COL_DIM]).transpose(FRAME_DIM, ROW_DIM, COL_DIM)
        if not np.isfinite(np.asarray(da.values, dtype=float)).all():
            raise MapiError(
                "the frames have missing values; the detector needs every pixel of every frame",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        for d in (ROW_DIM, COL_DIM):
            lab = np.asarray(da[d].values)
            if lab.size > 1 and not np.array_equal(
                np.diff(lab.astype(np.int64)), np.ones(lab.size - 1)
            ):
                raise MapiError(
                    f"the labels of {d!r} must be consecutive integers (camera pixels)",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{d}",
                )
        if da.sizes[FRAME_DIM] < 3:
            raise MapiError(
                f"the movie has {da.sizes[FRAME_DIM]} frames; the background and the camera "
                "calibration need at least 3",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{FRAME_DIM}",
            )
        return da

    @staticmethod
    def check_settings(s: StormLocalisationSettings, shape: tuple[int, int] | None = None) -> None:
        """Typed refusals of settings that pydantic cannot check alone."""
        if (s.offset_adu is None) != (s.scale_adu is None):
            missing = "scale_adu" if s.scale_adu is None else "offset_adu"
            raise MapiError(
                "set offset_adu and scale_adu together (or neither, to calibrate from the frames)",
                code=ErrorCode.SETTINGS_INVALID,
                pointer=f"/model/settings/{missing}",
            )
        if s.patch_px % 2 == 0:
            raise MapiError(
                f"patch_px must be odd, got {s.patch_px}",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/patch_px",
            )
        if s.min_blob_px > s.max_blob_px:
            raise MapiError(
                "min_blob_px must not be larger than max_blob_px",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/min_blob_px",
            )
        lo, hi = s.amplitude_band
        if not 0.0 <= lo < hi <= 1.0:
            raise MapiError(
                "amplitude_band must be two quantiles 0 <= low < high <= 1",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/amplitude_band",
            )
        if shape is not None and min(shape) < s.patch_px:
            raise MapiError(
                f"the frames are {shape[0]} x {shape[1]} px, smaller than one patch "
                f"({s.patch_px} px)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )

    # ================================================================== model
    def build_model(self, spots: Spots, s: StormLocalisationSettings) -> pm.Model:
        """The batched single-emitter model over the selected patches."""
        P = s.patch_px
        B = spots.patches.shape[0]
        b_med = (
            s.background_photons
            if s.background_photons is not None
            else max(2.0 * float(np.median(spots.background)), 1.0)
        )
        coords = {SPOT_DIM: np.arange(B), PATCH_Y: np.arange(P), PATCH_X: np.arange(P)}
        with pm.Model(coords=coords) as model:
            x = pm.Normal("x_px", spots.centre[:, 0], s.position_sd_px, dims=SPOT_DIM)
            y = pm.Normal("y_px", spots.centre[:, 1], s.position_sd_px, dims=SPOT_DIM)
            logN = pm.Normal("logN", math.log(s.photons_median), s.photons_log_sd, dims=SPOT_DIM)
            logb = pm.Normal("logb", math.log(b_med), s.background_log_sd, dims=SPOT_DIM)
            logs = pm.Normal("logs", math.log(s.psf_sd_px), s.psf_log_sd, dims=SPOT_DIM)
            sig = pt.exp(logs)
            img = (
                pt.exp(logN)[:, None, None]
                * _pixel_gauss_pt(y, sig, P)[:, :, None]
                * _pixel_gauss_pt(x, sig, P)[:, None, :]
            )
            lb = logb[:, None, None]
            if s.kind == "plane":
                gx = pm.Normal("grad_x", 0.0, s.gradient_sd, dims=SPOT_DIM)
                gy = pm.Normal("grad_y", 0.0, s.gradient_sd, dims=SPOT_DIM)
                idx = np.arange(P, dtype=float) - (P - 1) / 2.0
                lb = (
                    lb
                    + gx[:, None, None] * idx[None, None, :]
                    + gy[:, None, None] * idx[None, :, None]
                )
            mu = pt.exp(lb) + img
            pm.Poisson(PATCH, mu / 2.0, observed=spots.patches, dims=(SPOT_DIM, PATCH_Y, PATCH_X))
        return model

    @staticmethod
    def free_names(s: StormLocalisationSettings | str) -> tuple[str, ...]:
        kind = s if isinstance(s, str) else s.kind
        return FREE + (PLANE if kind == "plane" else ())

    # ================================================================== fit
    def fit(
        self, ds: xr.Dataset, s: StormLocalisationSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        da = self.movie(ds)
        self.check_settings(s, (da.sizes[ROW_DIM], da.sizes[COL_DIM]))
        adu = np.asarray(da.values, dtype=float)
        spots = find_spots(adu, s)
        ctx.progress(0.05, f"{spots.patches.shape[0]} spots of {spots.n_blobs} blobs selected")
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = self.build_model(spots, s)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        sink = ctx.checkpoint_sink if isinstance(ctx.checkpoint_sink, CheckpointSink) else None
        key = None
        if sink is not None:
            key = fit_key(
                model_id=self.id,
                version=self.version,
                settings=s.model_dump(mode="json"),
                fit_config=cfg.model_dump(mode="json"),
                data=ds,
            )
        report = None
        if ctx.on_progress is not None:

            def report(fraction: float, message: str) -> None:
                ctx.progress(0.05 + 0.85 * fraction, message)

        with _quiet():
            result = run_engine(
                model,
                plan,
                EngineHooks(progress=report, cancel=ctx.cancel, log=ctx.log, sink=sink, key=key),
            )
        ctx.check_cancelled()
        posterior = result.group("posterior")
        if posterior is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        names = list(self.free_names(s))
        posterior = posterior[names].assign_coords({SPOT_DIM: np.arange(spots.patches.shape[0])})
        geo = _Geometry.of(da, spots, s)
        posterior = posterior.assign(geo.physical(posterior))
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, result.engine)}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], result.engine)
        groups["observed_data"] = _clean(xr.Dataset({PATCH: geo.observed_patches()}), result.engine)
        groups["constant_data"] = _clean(geo.constant_data(), result.engine)
        if cfg.keep.log_likelihood:
            groups["log_likelihood"] = _clean(
                _log_likelihood(posterior, spots.patches, s), result.engine
            )
        if s.ppc_draws > 0:
            groups["posterior_predictive"] = _clean(
                _ppc(posterior, spots.patches.shape[1], s, seed), result.engine
            )
        # the super-resolved render (score_draws draws), so the fit's own plots
        # (sci.point_pattern, sci.field_map) draw it without a score: the runner's in-sample
        # evaluate of a long movie is over MAX_FRAME_VALUES and refused
        render = self._render_group(posterior, geo, s)
        groups[RENDER_GROUP] = _clean(render, result.engine)
        groups[RENDER_GROUP]["render"].attrs = render["render"].attrs  # keep kind and grid
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=names, random_seed=seed
            )
        prior_ds = prior["prior"].to_dataset()
        if SPOT_DIM in prior_ds.dims:
            prior_ds = prior_ds.assign_coords({SPOT_DIM: np.arange(spots.patches.shape[0])})
        groups["prior"] = _clean(prior_ds, result.engine)
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "kind": s.kind,
            "offset_adu": spots.offset,
            "scale_adu": spots.scale,
            "em_gain_adu_per_photon": spots.scale / 2.0,
            "calibration": spots.calibration,
            "background_photons_median": float(2.0 * np.median(spots.background)),
            "n_blobs": spots.n_blobs,
            "n_isolated": spots.n_isolated,
            "n_spots": int(spots.patches.shape[0]),
            "detection_threshold": float(spots.threshold),
            "pixel_nm": s.pixel_nm,
        }
        return FitOutput(groups=groups, learned=learned)

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        s = self.parse_settings(fitted.settings)
        names = [n for n in self.free_names(s) if n in post]
        out = convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)
        check = _misfit_check(fitted, s)
        if check is not None:
            out.append(check)
        return out

    def _render_group(
        self, posterior: xr.Dataset, geo: _Geometry, s: StormLocalisationSettings
    ) -> xr.Dataset:
        """``render`` of ``score_draws`` evenly spaced posterior draws (as a score has it)."""
        chains, draws = _draw_index(posterior, s.score_draws)
        sub = posterior.isel(chain=chains, draw=draws)
        dims_s = ("chain", "draw", SPOT_DIM)
        render = render_points(
            np.asarray(sub["x"].transpose(*dims_s).values),
            np.asarray(sub["y"].transpose(*dims_s).values),
            geo.render_x_edges,
            geo.render_y_edges,
        )
        decl = self.outputs["render"]
        attrs = {"kind": "posterior", "units": decl["units"], "grid": json.dumps(decl["grid"])}
        return xr.Dataset(
            {"render": (("chain", "draw", RENDER_Y, RENDER_X), render, attrs)},
            coords={
                "chain": sub["chain"].values,
                "draw": sub["draw"].values,
                RENDER_Y: geo.render_y,
                RENDER_X: geo.render_x,
            },
        )

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        geo = _Geometry.from_fitted(fitted, s)
        post = fitted.posterior
        chains, draws = _draw_index(post, s.score_draws)
        sub = post.isel(chain=chains, draw=draws)
        params = {
            n: np.asarray(sub[n].transpose("chain", "draw", SPOT_DIM).values)
            for n in self.free_names(s)
        }
        P = s.patch_px
        rate = _photons(params, P) / 2.0
        predictive = ctx.kind == "predictive"
        if predictive:
            patch = np.empty_like(rate)
            for i, c in enumerate(chains.tolist()):
                for j, d in enumerate(draws.tolist()):
                    patch[i, j] = _rng(ctx.seed, f"patch:{c}:{d}").poisson(rate[i, j])
        else:
            patch = rate
        ctx.check_cancelled()
        phys = geo.physical(sub)
        wanted = _requested(ds, FRAME_DIM, geo.frame_labels)
        size = rate.shape[0] * rate.shape[1] * wanted.size * geo.rows.size * geo.cols.size
        if size > MAX_FRAME_VALUES:
            raise MapiError(
                f"frames_hat for {wanted.size} frames would hold {size:,} values (more than "
                f"{MAX_FRAME_VALUES:,}); score fewer frames or lower score_draws",
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                pointer="/data",
            )
        frames_hat = geo.frames_hat(patch, wanted, chains, draws, ctx.seed, predictive)
        render = render_points(
            np.asarray(phys["x"].transpose("chain", "draw", SPOT_DIM).values),
            np.asarray(phys["y"].transpose("chain", "draw", SPOT_DIM).values),
            geo.render_x_edges,
            geo.render_y_edges,
        )
        dims_s = ("chain", "draw", SPOT_DIM)
        noise = "seeded_poisson" if predictive else "none"
        values: dict[str, tuple[tuple[str, ...], np.ndarray, str]] = {
            n: (dims_s, np.asarray(phys[n].transpose(*dims_s).values), "posterior")
            for n in ("x", "y", "photons", "background", "psf_sd")
        }
        values["render"] = (("chain", "draw", RENDER_Y, RENDER_X), render, "posterior")
        values[PATCH] = (
            ("chain", "draw", SPOT_DIM, PATCH_Y, PATCH_X),
            patch.astype(np.float64),
            ctx.kind,
        )
        values["frames_hat"] = (
            ("chain", "draw", FRAME_DIM, ROW_DIM, COL_DIM),
            frames_hat,
            ctx.kind,
        )
        data_vars = {}
        for name, (dims, arr, kind) in values.items():
            decl = self.outputs[name]
            attrs: dict[str, Any] = {"kind": kind}
            if "units" in decl:
                attrs["units"] = decl["units"]
            if "grid" in decl:
                attrs["grid"] = json.dumps(decl["grid"])
            if kind != "posterior":
                attrs["noise"] = noise
            data_vars[name] = xr.DataArray(arr, dims=dims, attrs=attrs)
        pred = xr.Dataset(
            data_vars,
            coords={
                "chain": np.asarray(post["chain"].values)[chains],
                "draw": np.asarray(post["draw"].values)[draws],
                SPOT_DIM: np.arange(geo.n_spots),
                PATCH_Y: np.arange(P),
                PATCH_X: np.arange(P),
                RENDER_Y: geo.render_y,
                RENDER_X: geo.render_x,
                FRAME_DIM: wanted,
                ROW_DIM: geo.rows,
                COL_DIM: geo.cols,
            },
        )
        sel = {d: np.asarray(ds[d].values) for d in (ROW_DIM, COL_DIM) if d in ds.coords}
        pred = pred.sel(sel)
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ============================================================================== helpers


@dataclass(slots=True)
class _Geometry:
    """Where the patches sit in the movie, and the render grid (fit and score)."""

    origin: np.ndarray  # (B, 3) positions (frame, row, col); the frame position is unused
    frames: np.ndarray  # (B,) frame label of each spot
    rows: np.ndarray  # row labels of the movie
    cols: np.ndarray  # col labels of the movie
    background: np.ndarray  # (row, col) effective counts
    patches: np.ndarray | None  # (B, P, P) observed counts (fit only)
    offset: float
    scale: float
    pixel_nm: float
    render_px_nm: float
    P: int

    @property
    def n_spots(self) -> int:
        return int(self.origin.shape[0])

    @property
    def frame_labels(self) -> np.ndarray:
        return np.unique(self.frames)

    @property
    def render_x_edges(self) -> np.ndarray:
        return _edges(self.cols, self.pixel_nm, self.render_px_nm)

    @property
    def render_y_edges(self) -> np.ndarray:
        return _edges(self.rows, self.pixel_nm, self.render_px_nm)

    @property
    def render_x(self) -> np.ndarray:
        e = self.render_x_edges
        return (e[:-1] + e[1:]) / 2

    @property
    def render_y(self) -> np.ndarray:
        e = self.render_y_edges
        return (e[:-1] + e[1:]) / 2

    @classmethod
    def of(cls, da: xr.DataArray, spots: Spots, s: StormLocalisationSettings) -> _Geometry:
        frame_lab = np.asarray(da[FRAME_DIM].values).astype(np.int64)
        return cls(
            origin=spots.origin,
            frames=frame_lab[spots.origin[:, 0]],
            rows=np.asarray(da[ROW_DIM].values).astype(np.int64),
            cols=np.asarray(da[COL_DIM].values).astype(np.int64),
            background=spots.background,
            patches=spots.patches,
            offset=spots.offset,
            scale=spots.scale,
            pixel_nm=s.pixel_nm,
            render_px_nm=s.render_px_nm,
            P=s.patch_px,
        )

    @classmethod
    def from_fitted(cls, fitted: Fitted, s: StormLocalisationSettings) -> _Geometry:
        const = fitted.groups.get("constant_data")
        need = ("spot_frame", "spot_row0", "spot_col0", "background_counts")
        if const is None or any(n not in const for n in need):
            raise MapiError(
                f"the bundle of 'sci.storm_localisation' has no constant_data {list(need)}; "
                "refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        bg = const["background_counts"].transpose(ROW_DIM, COL_DIM)
        rows = np.asarray(bg[ROW_DIM].values).astype(np.int64)
        cols = np.asarray(bg[COL_DIM].values).astype(np.int64)
        r0 = np.asarray(const["spot_row0"].values).astype(np.int64)
        c0 = np.asarray(const["spot_col0"].values).astype(np.int64)
        frames = np.asarray(const["spot_frame"].values).astype(np.int64)
        # positions of the patch corners in the movie (the bundle keeps their labels)
        origin = np.stack([np.zeros_like(frames), r0 - rows[0], c0 - cols[0]], axis=1)
        return cls(
            origin=origin,
            frames=frames,
            rows=rows,
            cols=cols,
            background=np.asarray(bg.values, dtype=float),
            patches=None,
            offset=float(fitted.learned["offset_adu"]),  # type: ignore[arg-type]
            scale=float(fitted.learned["scale_adu"]),  # type: ignore[arg-type]
            pixel_nm=s.pixel_nm,
            render_px_nm=s.render_px_nm,
            P=s.patch_px,
        )

    def physical(self, post: xr.Dataset) -> dict[str, xr.DataArray]:
        """Positions (nm, movie labels x pixel_nm), photons, background and PSF sd (nm)."""
        c0 = xr.DataArray(self.cols[0] + self.origin[:, 2], dims=SPOT_DIM)
        r0 = xr.DataArray(self.rows[0] + self.origin[:, 1], dims=SPOT_DIM)
        c0 = c0.assign_coords({SPOT_DIM: post[SPOT_DIM].values})
        r0 = r0.assign_coords({SPOT_DIM: post[SPOT_DIM].values})
        return {
            "x": ((c0 + post["x_px"]) * self.pixel_nm).assign_attrs(units="nm"),
            "y": ((r0 + post["y_px"]) * self.pixel_nm).assign_attrs(units="nm"),
            "photons": xr.ufuncs.exp(post["logN"]).assign_attrs(units="photons"),
            "background": xr.ufuncs.exp(post["logb"]).assign_attrs(units="photons/px"),
            "psf_sd": (xr.ufuncs.exp(post["logs"]) * self.pixel_nm).assign_attrs(units="nm"),
        }

    def observed_patches(self) -> xr.DataArray:
        assert self.patches is not None
        P = self.P
        return xr.DataArray(
            self.patches.astype(np.int64),
            dims=(SPOT_DIM, PATCH_Y, PATCH_X),
            coords={
                SPOT_DIM: np.arange(self.n_spots),
                PATCH_Y: np.arange(P),
                PATCH_X: np.arange(P),
            },
        )

    def constant_data(self) -> xr.Dataset:
        spot = {SPOT_DIM: np.arange(self.n_spots)}
        return xr.Dataset(
            {
                "spot_frame": xr.DataArray(self.frames, dims=SPOT_DIM, coords=spot),
                "spot_row0": xr.DataArray(self.rows[self.origin[:, 1]], dims=SPOT_DIM, coords=spot),
                "spot_col0": xr.DataArray(self.cols[self.origin[:, 2]], dims=SPOT_DIM, coords=spot),
                "background_counts": xr.DataArray(
                    self.background,
                    dims=(ROW_DIM, COL_DIM),
                    coords={ROW_DIM: self.rows, COL_DIM: self.cols},
                ),
            }
        )

    def frames_hat(
        self,
        patch: np.ndarray,
        frames: np.ndarray,
        chains: np.ndarray,
        draws: np.ndarray,
        seed: int,
        predictive: bool,
    ) -> np.ndarray:
        """``(chain, draw, frame, row, col)`` ADU images of ``frames``.

        Inside each fitted patch: ``offset + scale x patch``. Elsewhere: the background,
        the per-pixel time median of the effective counts (plus Poisson noise for predictive
        draws, seeded by chain, draw and frame label, so a subset of frames gets the same
        values).
        """
        where = {int(f): i for i, f in enumerate(frames.tolist())}
        nc, nd = patch.shape[:2]
        H, W = self.rows.size, self.cols.size
        out = np.empty((nc, nd, frames.size, H, W), dtype=np.float32)
        for i, c in enumerate(chains.tolist()):
            for j, d in enumerate(draws.tolist()):
                for f, lab in enumerate(frames.tolist()):
                    if predictive:
                        img = _rng(seed, f"frame:{c}:{d}:{lab}").poisson(self.background)
                    else:
                        img = self.background
                    out[i, j, f] = self.offset + self.scale * img
        P = self.P
        for k in range(self.n_spots):
            pos = where.get(int(self.frames[k]))
            if pos is None:
                continue
            r0, c0 = int(self.origin[k, 1]), int(self.origin[k, 2])
            out[:, :, pos, r0 : r0 + P, c0 : c0 + P] = self.offset + self.scale * patch[:, :, k]
        return out


def _requested(ds: xr.Dataset, dim: str, default: np.ndarray) -> np.ndarray:
    """The labels of ``dim`` the request asked for (the platform pads a fixed dim to the
    fitted labels and flags the real ones in ``<dim>__requested``)."""
    if dim not in ds.coords:
        return default
    labels = np.asarray(ds[dim].values).astype(np.int64)
    flag = f"{dim}__requested"
    if flag in ds.coords:
        labels = labels[np.asarray(ds[flag].values, dtype=bool)]
    return np.unique(labels)


def _photons(p: dict[str, np.ndarray], P: int) -> np.ndarray:
    """:func:`expected_photons` of a dict of parameter arrays (free variable names)."""
    return expected_photons(
        p["x_px"], p["y_px"], p["logN"], p["logb"], p["logs"], P, p.get("grad_x"), p.get("grad_y")
    )


def _edges(labels: np.ndarray, pixel_nm: float, render_nm: float) -> np.ndarray:
    lo = (float(labels[0]) - 0.5) * pixel_nm
    hi = (float(labels[-1]) + 0.5) * pixel_nm
    n = max(1, round((hi - lo) / render_nm))
    return np.linspace(lo, hi, n + 1)


def _log_likelihood(
    post: xr.Dataset, patches: np.ndarray, s: StormLocalisationSettings
) -> xr.Dataset:
    """Pointwise Poisson log-likelihood ``(chain, draw, spot, py, px)``, one chain at a time."""
    P = s.patch_px
    z = patches[None]
    lgz = gammaln(z + 1.0)
    rows = []
    for c in range(post.sizes["chain"]):
        p = post.isel(chain=c)
        get = {
            n: np.asarray(p[n].transpose("draw", SPOT_DIM).values)
            for n in StormLocalisationModel.free_names(s)
        }
        m = _photons(get, P) / 2.0
        rows.append(z * np.log(m) - m - lgz)
    ll = np.stack(rows)
    return xr.Dataset(
        {PATCH: (("chain", "draw", SPOT_DIM, PATCH_Y, PATCH_X), ll)},
        coords={
            "chain": post["chain"].values,
            "draw": post["draw"].values,
            SPOT_DIM: post[SPOT_DIM].values,
            PATCH_Y: np.arange(P),
            PATCH_X: np.arange(P),
        },
    )


def _ppc(post: xr.Dataset, P: int, s: StormLocalisationSettings, seed: int) -> xr.Dataset:
    """``ppc_draws`` Poisson patch draws (evenly spaced posterior draws of every chain)."""
    chains, draws = _draw_index(post, s.ppc_draws)
    sub = post.isel(chain=chains, draw=draws)
    get = {
        n: np.asarray(sub[n].transpose("chain", "draw", SPOT_DIM).values)
        for n in StormLocalisationModel.free_names(s)
    }
    rate = _photons(get, P) / 2.0
    rng = _rng(seed, "ppc")
    return xr.Dataset(
        {
            PATCH: (
                ("chain", "draw", SPOT_DIM, PATCH_Y, PATCH_X),
                rng.poisson(rate).astype(np.int64),
            )
        },
        coords={
            "chain": sub["chain"].values,
            "draw": sub["draw"].values,
            SPOT_DIM: sub[SPOT_DIM].values,
            PATCH_Y: np.arange(P),
            PATCH_X: np.arange(P),
        },
    )


def _misfit_check(fitted: Fitted, s: StormLocalisationSettings) -> Diagnostic | None:
    """Pearson chi^2 per pixel of the patches at the posterior mean (about 1 when the PSF
    and background model describe the spots)."""
    obs = fitted.groups.get("observed_data")
    post = fitted.groups.get("posterior")
    if obs is None or post is None or PATCH not in obs:
        return None
    mean = {
        n: np.asarray(post[n].mean(("chain", "draw")).values)
        for n in StormLocalisationModel.free_names(s)
        if n in post
    }
    if len(mean) < len(FREE):
        return None
    m = _photons(mean, s.patch_px) / 2.0
    z = np.asarray(obs[PATCH].transpose(SPOT_DIM, PATCH_Y, PATCH_X).values, dtype=float)
    ratio = float(np.mean((z - m) ** 2 / m))
    if ratio <= 1.5:
        return None
    return Diagnostic(
        code="PATCH_MISFIT",
        severity="warning",
        metric="pearson_chi2_per_pixel",
        value=ratio,
        threshold=1.5,
        message_expert=f"the Pearson chi^2 per patch pixel at the posterior mean is {ratio:.2f} "
        "(about 1 for Poisson counts): the PSF or background model, or the camera "
        "calibration, does not describe the spots",
        message_plain="The model does not agree well with the pixels of the spots. The ranges "
        "of the positions are possibly too narrow.",
        suggested_action="Try kind: plane, check the camera offset and gain, or fit only "
        "isolated, in-focus spots.",
    )


def _draw_index(post: xr.Dataset, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Every chain and ``n // chains`` evenly spaced draw positions (at least one)."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    per = min(draws, max(1, n // chains))
    idx = np.unique(np.linspace(0, draws - 1, per).round().astype(np.int64))
    return np.arange(chains), idx


def _rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {k: a for k, a in out[v].attrs.items() if k == "units"}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


@contextmanager
def _quiet() -> Iterator[None]:
    logger = logging.getLogger("pymc")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)
