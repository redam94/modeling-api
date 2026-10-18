"""``sci.strong_lens``: a pixel forward model of a galaxy-scale strong gravitational lens (E80).

Canonical inputs (dense layout)::

    dims  row, col: integer, fixed           (the image; row 0 is the bottom row)
          psf_row, psf_col: integer, fixed   (the PSF kernel, odd sides)
    vars  image(row, col): target, float64, counts per second, NaN allowed (not fitted)
          weight(row, col): weight, float64, inverse variance of each pixel (0 = unused)
          psf(psf_row, psf_col): constant, float64, the point-spread function
          mask(row, col): mask, bool, True where a pixel is used (setting use_mask)

The lens galaxy sits near the centre of the cutout. Every pixel is predicted: rays traced
through an ``subsample`` x ``subsample`` sub-grid of each pixel are bent by a singular
isothermal ellipsoid (SIE) plus external shear (:mod:`mapi_plugin_science.lens_math`) and
read an elliptical Sersic source; the lens galaxy's own light is ``lens_light_components``
concentric Sersics, plus a constant sky. Each component is binned to pixels and convolved
with the PSF (a separable SVD expansion: matrix products only), and the residuals are
whitened for a separable lag-one noise correlation (drizzled images) before the Gaussian
likelihood, with the noise map scaled by a free ``noise_scale``.

**Linear amplitudes integrated out.** The image is linear in the brightness of each
component. With flat priors they are integrated out exactly (closed-form Gaussian marginal,
in PyTensor linear algebra: a Cholesky factor of the ``K x K`` normal matrix), as in the
notebook, which removes the strongest correlations (brightness against size) from the
sampler. After sampling, each draw gets an amplitude vector drawn from its exact
conditional Gaussian, stored as the ``amplitude`` posterior variable, so the joint
posterior is complete.

**Wrong modes and preconditioning.** Lens posteriors have many local optima. The fit
first finds the mode in stages (as the notebook does): the lens light alone, with the
ring masked by sigma clipping; the Einstein radius from the peak of the azimuthal profile
of the light-subtracted image; then a few starts of the full model at the best points of
a scan of the source position near the lens axis (each first with the Einstein radius and
the lens light held), keeping the best. A Laplace approximation at that mode (finite
differences of the exact PyTensor gradient) gives ``u = u_map + L z``; NUTS samples the
standardised ``z`` (flat-prior variable, the priors are a potential on ``u``), so the
sampler starts at the mode, a unit of initial jitter is one posterior sd, and the strong
correlations (mass ellipticity against shear, lens centre against source position) are
removed before the warm-up - the job nutpie's low-rank mass matrix did in the notebook.

**Outputs** (``draws``):

* ``model_image(chain, draw, row, col)`` - the expected image (``kind: expected``) or a
  simulated observation with correlated noise (``kind: predictive``, and ``simulate``);
* ``source(chain, draw, source_row, source_col)`` - the unlensed source surface
  brightness (the parametric Sersic of each draw, in image units per pixel) on a
  ``source_grid`` x ``source_grid`` source-plane grid centred on the posterior-mean
  source position (``learned["source_grid"]`` holds its geometry);
* ``einstein_radius(chain, draw)`` (arcsec) and, with ``z_lens``/``z_source``,
  ``einstein_mass(chain, draw)`` (solar masses, flat LCDM).

The fit always caches the derived group ``critical_curves(sample, curve_point)``: the
analytic tangential critical curve (image plane), its caustic and the cut (source plane)
for up to ``curve_draws`` draws of the bundle thinning order, in arcsec and in fractional
pixel indices of the ``model_image`` and ``source`` grids.

Pointwise LOO is not offered: the amplitudes are integrated out over all pixels and the
whitened noise couples neighbours (``loo_valid = False``).
"""

from __future__ import annotations

import functools
import hashlib
import logging
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor
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
from mapi_sdk.summarise import thin_index
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, model_validator
from pytensor.tensor.linalg import cholesky, solve_triangular
from scipy import ndimage, optimize

from mapi_plugin_science import lens_math as lm

__all__ = [
    "CRITICAL_CURVES",
    "IMAGE",
    "PSF",
    "WEIGHT",
    "LensLayout",
    "StrongLensModel",
    "StrongLensSettings",
]

ROW, COL = "row", "col"
PSF_ROW, PSF_COL = "psf_row", "psf_col"
SRC_ROW, SRC_COL = "source_row", "source_col"
IMAGE, WEIGHT, PSF, MASK = "image", "weight", "psf", "mask"
CRITICAL_CURVES = "critical_curves"
AXIS, ECOMP, LIGHT, COMPONENT = "axis", "ecomp", "light", "component"
_EMAX = 0.7
"""Ellipticity components are ``0.7 tanh(u / 0.7)``: |e| < 0.99 keeps q > 0."""
_LOG = logging.getLogger(__name__)


class StrongLensSettings(ModelSettings):
    """Settings of ``sci.strong_lens`` (angles in arcsec)."""

    pixel_scale: float = Field(0.05, gt=0, le=10, description="Pixel size in arcsec")
    subsample: int = Field(
        2, ge=1, le=4, description="Rays per pixel along each axis (sub-pixel sampling)"
    )
    lens_light_components: int = Field(
        2, ge=0, le=2, description="Concentric Sersic profiles for the lens galaxy's light"
    )
    external_shear: bool = Field(True, description="Add an external shear to the lens")
    noise_correlation: float = Field(
        0.0,
        ge=0.0,
        lt=0.45,
        description="Lag-one correlation of the noise between neighbouring pixels (separable "
        "rows x columns; drizzled images have about 0.2). 0 treats pixels as independent",
    )
    fit_noise_scale: bool = Field(True, description="Scale the noise map by a free factor")
    use_mask: bool = Field(False, description="The data has a bool mask(row, col) of pixels")
    psf_tolerance: float = Field(
        1e-3, gt=0, lt=1, description="Relative error allowed in the separable PSF expansion"
    )
    psf_max_rank: int = Field(12, ge=1, le=40, description="Most separable PSF terms")
    theta_e_median: float = Field(1.0, gt=0, description="Prior median of the Einstein radius")
    theta_e_log_sd: float = Field(0.5, gt=0, description="Prior sd of log Einstein radius")
    mass_center_sd: float = Field(0.1, gt=0, description="Prior sd of the mass centre")
    light_center_sd: float = Field(0.1, gt=0, description="Prior sd of the light centre")
    ellipticity_sd: float = Field(0.2, gt=0, description="Prior sd of ellipticity components")
    shear_sd: float = Field(0.05, gt=0, description="Prior sd of the shear components")
    light_size_medians: tuple[float, float] = Field(
        (0.5, 1.5), description="Prior medians of the lens-light half-light radii"
    )
    source_size_median: float = Field(0.2, gt=0, description="Prior median source radius")
    source_offset_sd: float = Field(0.3, gt=0, description="Prior sd of the source position")
    noise_scale_log_sd: float = Field(0.2, gt=0, description="Prior sd of log noise scale")
    theta_e_start: float | None = Field(
        None,
        gt=0,
        description="Start the optimisation at this Einstein radius instead of the radius of "
        "the ring found in the light-subtracted image",
    )
    n_starts: int = Field(
        5, ge=1, le=9, description="Starts of the full-model optimisation (source positions)"
    )
    max_iter: int = Field(3000, ge=10, le=100_000, description="Iterations per optimisation")
    source_grid: int = Field(30, ge=4, le=128, description="Source-plane grid side (pixels)")
    source_half_width: float = Field(
        0.45, gt=0, description="Half-width of the source-plane grid (arcsec)"
    )
    curve_points: int = Field(128, ge=16, le=2048, description="Points per critical curve")
    curve_draws: int = Field(
        100, ge=1, le=2000, description="Posterior draws with cached critical curves"
    )
    z_lens: float | None = Field(None, gt=0, description="Lens redshift (for the mass)")
    z_source: float | None = Field(None, gt=0, description="Source redshift (for the mass)")
    h0: float = Field(70.0, gt=0, description="Hubble constant (km/s/Mpc)")
    omega_m: float = Field(0.3, gt=0, lt=1, description="Matter density (flat LCDM)")

    @model_validator(mode="after")
    def _redshifts(self) -> StrongLensSettings:
        if (self.z_lens is None) != (self.z_source is None):
            raise ValueError("give both z_lens and z_source, or neither")
        if self.z_lens is not None and self.z_source is not None and self.z_source <= self.z_lens:
            raise ValueError("z_source must be larger than z_lens")
        return self


_TEMPLATE: dict[str, Any] = {
    "params": {"use_mask": {"type": "boolean", "default": False}},
    "template": {
        "dims": {
            ROW: {"kind": "integer", "description": "Image rows (row 0 at the bottom)"},
            COL: {"kind": "integer", "description": "Image columns"},
            PSF_ROW: {"kind": "integer", "description": "Rows of the PSF kernel"},
            PSF_COL: {"kind": "integer", "description": "Columns of the PSF kernel"},
        },
        "vars": {
            IMAGE: {
                "dims": [ROW, COL],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
                "description": "Surface brightness of each pixel (for example electrons/s)",
            },
            WEIGHT: {
                "dims": [ROW, COL],
                "dtype": "float64",
                "role": "weight",
                "description": "Inverse variance of each pixel; 0 leaves the pixel out",
            },
            PSF: {
                "dims": [PSF_ROW, PSF_COL],
                "dtype": "float64",
                "role": "constant",
                "description": "The point-spread function (odd sides, centred)",
            },
        },
    },
    "cases": [
        {
            "when": {"use_mask": True},
            "merge": {
                "vars": {
                    MASK: {
                        "dims": [ROW, COL],
                        "dtype": "bool",
                        "role": "mask",
                        "description": "True where the pixel is used in the fit",
                    }
                }
            },
        }
    ],
}


# ============================================================================== layout


Prior = Literal["normal", "lognormal", "uniform", "ellipticity"]


@dataclass(frozen=True, slots=True)
class Block:
    """One parameter block on the unconstrained vector ``u``.

    ``normal``: value = u, Normal(a, b). ``lognormal``: value = exp(u), log value ~
    Normal(a, b). ``uniform``: value = a + (b - a) sigmoid(u), uniform on (a, b).
    ``ellipticity``: value = 0.7 tanh(u / 0.7), Normal(0, a) on the value.
    """

    name: str
    shape: tuple[int, ...]
    dims: tuple[str, ...]
    prior: Prior
    a: float
    b: float = 0.0

    @property
    def size(self) -> int:
        return int(np.prod(self.shape)) if self.shape else 1

    def value(self, u: Any, xp: Any) -> Any:
        if self.prior == "normal":
            return u
        if self.prior == "lognormal":
            return xp.exp(u)
        if self.prior == "uniform":
            return self.a + (self.b - self.a) * _sigmoid(u, xp)
        return _EMAX * xp.tanh(u / _EMAX)

    def logp(self, u: Any, xp: Any) -> Any:
        """Log prior density of ``u`` (constants dropped; Jacobians included)."""
        if self.prior in ("normal", "lognormal"):
            return -0.5 * xp.sum(((u - self.a) / self.b) ** 2)
        if self.prior == "uniform":
            return -xp.sum(_softplus(-u, xp) + _softplus(u, xp))
        t = xp.tanh(u / _EMAX)
        return xp.sum(-0.5 * (_EMAX * t / self.a) ** 2 + xp.log(1 - t**2 + 1e-300))

    def bounds(self) -> list[tuple[float, float]]:
        """Optimiser box on ``u``: five prior sds (normal, lognormal), |e| <= 0.6 and
        a logit of 8 (uniform). The sampler is not bounded."""
        if self.prior in ("normal", "lognormal"):
            lo, hi = self.a - 5 * self.b, self.a + 5 * self.b
        elif self.prior == "uniform":
            lo, hi = -8.0, 8.0
        else:
            hi = float(_EMAX * np.arctanh(0.6 / _EMAX))
            lo = -hi
        return [(lo, hi)] * self.size

    def unconstrained(self, value: np.ndarray | float) -> np.ndarray:
        """Inverse of :meth:`value` (NumPy, for starting points)."""
        v = np.asarray(value, dtype=float).reshape(-1)
        if self.prior == "normal":
            return v
        if self.prior == "lognormal":
            return np.log(v)
        if self.prior == "uniform":
            t = np.clip((v - self.a) / (self.b - self.a), 1e-6, 1 - 1e-6)
            return np.asarray(np.log(t / (1 - t)))
        return np.asarray(_EMAX * np.arctanh(np.clip(v / _EMAX, -0.999, 0.999)))


def _sigmoid(u: Any, xp: Any) -> Any:
    return 1.0 / (1.0 + xp.exp(-u))


def _softplus(u: Any, xp: Any) -> Any:
    if xp is np:
        return np.logaddexp(0.0, u)
    return pt.softplus(u)


@dataclass(frozen=True, slots=True)
class LensLayout:
    """The parameter blocks of one model configuration, in their order on ``u``."""

    blocks: tuple[Block, ...]
    n_light: int
    shear: bool
    noise: bool

    @classmethod
    def from_settings(cls, s: StrongLensSettings) -> LensLayout:
        k = s.lens_light_components
        blocks: list[Block] = []
        if k:
            med = np.log(np.asarray(s.light_size_medians[:k], dtype=float))
            blocks += [
                Block("xy_light", (2,), (AXIS,), "normal", 0.0, s.light_center_sd),
                *[Block(f"R_light_{j}", (), (), "lognormal", float(med[j]), 1.0) for j in range(k)],
                *[Block(f"n_light_{j}", (), (), "uniform", *lm.SERSIC_N_RANGE) for j in range(k)],
                *[
                    Block(f"e_light_{j}", (2,), (ECOMP,), "ellipticity", s.ellipticity_sd)
                    for j in range(k)
                ],
            ]
        blocks += [
            Block(
                "theta_E", (), (), "lognormal", float(np.log(s.theta_e_median)), s.theta_e_log_sd
            ),
            Block("e_mass", (2,), (ECOMP,), "ellipticity", s.ellipticity_sd),
            Block("xy_mass", (2,), (AXIS,), "normal", 0.0, s.mass_center_sd),
        ]
        if s.external_shear:
            blocks.append(Block("shear", (2,), (ECOMP,), "normal", 0.0, s.shear_sd))
        blocks += [
            Block("R_src", (), (), "lognormal", float(np.log(s.source_size_median)), 0.5),
            Block("n_src", (), (), "uniform", *lm.SERSIC_N_RANGE),
            Block("e_src", (2,), (ECOMP,), "ellipticity", s.ellipticity_sd),
            Block("xy_src", (2,), (AXIS,), "normal", 0.0, s.source_offset_sd),
        ]
        if s.fit_noise_scale:
            blocks.append(Block("noise_scale", (), (), "lognormal", 0.0, s.noise_scale_log_sd))
        return cls(tuple(blocks), k, s.external_shear, s.fit_noise_scale)

    @property
    def size(self) -> int:
        return sum(b.size for b in self.blocks)

    @property
    def light_blocks(self) -> tuple[Block, ...]:
        return tuple(
            b for b in self.blocks if b.name.startswith(("xy_light", "R_light", "n_li", "e_li"))
        )

    def light_only(self) -> LensLayout:
        return LensLayout(self.light_blocks, self.n_light, False, False)

    def slices(self) -> dict[str, slice]:
        out: dict[str, slice] = {}
        i = 0
        for b in self.blocks:
            out[b.name] = slice(i, i + b.size)
            i += b.size
        return out

    def split(self, u: Any) -> dict[str, Any]:
        """``u`` cut into its blocks (scalars as 0-d, vectors in block shape)."""
        out: dict[str, Any] = {}
        for b, sl in zip(self.blocks, self.slices().values(), strict=True):
            out[b.name] = u[sl.start] if not b.shape else u[sl]
        return out

    def values(self, u: Any, xp: Any) -> dict[str, Any]:
        parts = self.split(u)
        return {b.name: b.value(parts[b.name], xp) for b in self.blocks}

    def logprior(self, u: Any, xp: Any) -> Any:
        parts = self.split(u)
        return sum(b.logp(parts[b.name], xp) for b in self.blocks)

    def bounds(self) -> list[tuple[float, float]]:
        return [b for block in self.blocks for b in block.bounds()]

    def pack(self, values: dict[str, Any]) -> np.ndarray:
        """Unconstrained ``u`` from physical values (NumPy)."""
        return np.concatenate([b.unconstrained(values[b.name]) for b in self.blocks])

    @property
    def n_components(self) -> int:
        """Linear components: the lens-light Sersics, the source and the sky constant."""
        return self.n_light + 2

    def component_names(self) -> list[str]:
        return [f"lens_light_{j + 1}" for j in range(self.n_light)] + ["source", "sky"]


# ============================================================================== forward model


@dataclass(slots=True)
class Problem:
    """Numeric constants of one image: rays, PSF factors, whitening and data."""

    n_row: int
    n_col: int
    pixel_scale: float
    subsample: int
    x: np.ndarray
    y: np.ndarray
    psf: lm.PsfFactors
    w_row: np.ndarray | None
    w_col: np.ndarray | None
    l_row: np.ndarray | None
    l_col: np.ndarray | None
    image: np.ndarray
    isig: np.ndarray
    sigma: np.ndarray
    """Noise sd of every pixel with a weight (0 elsewhere), also where the image is
    missing: predictive draws cover held-out pixels."""

    @classmethod
    def build(
        cls,
        image: np.ndarray,
        weight: np.ndarray,
        psf: np.ndarray,
        s: StrongLensSettings,
        mask: np.ndarray | None = None,
    ) -> Problem:
        n_row, n_col = image.shape
        x, y = lm.ray_grid(n_row, n_col, s.pixel_scale, s.subsample)
        factors = lm.psf_factors(psf, n_row, n_col, tol=s.psf_tolerance, max_rank=s.psf_max_rank)
        usable = np.isfinite(weight) & (weight > 0)
        if mask is not None:
            usable &= mask.astype(bool)
        valid = usable & np.isfinite(image)
        isig = np.where(valid, np.sqrt(np.where(valid, weight, 0.0)), 0.0)
        sigma = np.where(usable, 1.0 / np.sqrt(np.where(usable, weight, 1.0)), 0.0)
        rho = s.noise_correlation
        w_row = w_col = l_row = l_col = None
        if rho > 0:
            w_row, l_row = lm.whitening(n_row, rho)
            w_col, l_col = lm.whitening(n_col, rho)
        return cls(
            n_row,
            n_col,
            s.pixel_scale,
            s.subsample,
            x,
            y,
            factors,
            w_row,
            w_col,
            l_row,
            l_col,
            np.where(valid, image, 0.0),
            isig,
            sigma,
        )

    @property
    def n_valid(self) -> int:
        return int((self.isig > 0).sum())

    def whiten(self, r: Any, wrap: Callable[[np.ndarray], Any] | None = None) -> Any:
        if self.w_row is None or self.w_col is None:
            return r
        f = wrap or (lambda a: a)
        return f(self.w_row) @ r @ f(np.ascontiguousarray(self.w_col.T))


def components(p: dict[str, Any], prob: Problem, layout: LensLayout, xp: Any) -> list[Any]:
    """Unit-amplitude component images ``(n_row, n_col)``: lens-light Sersics and the
    lensed source (binned and PSF-convolved), then the sky constant (``xp`` = np or pt)."""
    wrap = pt.constant if xp is pt else None
    x = pt.constant(prob.x) if xp is pt else prob.x
    y = pt.constant(prob.y) if xp is pt else prob.y
    out: list[Any] = []

    def pixels(img: Any) -> Any:
        binned = lm.bin_pixels(img, prob.n_row, prob.n_col, prob.subsample)
        return lm.convolve(binned, prob.psf, constants=wrap)

    for j in range(layout.n_light):
        e = p[f"e_light_{j}"]
        out.append(
            pixels(
                lm.sersic(
                    x,
                    y,
                    p[f"R_light_{j}"],
                    p[f"n_light_{j}"],
                    e[0],
                    e[1],
                    p["xy_light"][0],
                    p["xy_light"][1],
                    xp,
                )
            )
        )
    em, xm = p["e_mass"], p["xy_mass"]
    g = p.get("shear")
    ax, ay = lm.deflection(
        x,
        y,
        p["theta_E"],
        em[0],
        em[1],
        xm[0],
        xm[1],
        g[0] if g is not None else 0.0,
        g[1] if g is not None else 0.0,
        xp,
    )
    es, xs = p["e_src"], p["xy_src"]
    out.append(
        pixels(lm.sersic(x - ax, y - ay, p["R_src"], p["n_src"], es[0], es[1], xs[0], xs[1], xp))
    )
    out.append(xp.ones((prob.n_row, prob.n_col)))
    return out


def marginal_loglik(
    comps: Sequence[Any],
    prob: Problem,
    isig: Any,
    b: Any,
    bb: Any,
    n_valid: Any,
    noise_scale: Any = 1.0,
) -> Any:
    """``log p(image | nonlinear parameters)`` with the linear amplitudes integrated out
    (flat priors): ``-|r|^2 / (2 s^2) - (N - K) log s - log det chol(A^T A)`` for whitened
    data ``b`` and whitened component images ``A`` (PyTensor)."""
    cols = [prob.whiten(c * isig, pt.constant).ravel() for c in comps]
    a = pt.stack(cols, axis=1)
    k = len(comps)
    ftf = a.T @ a
    ftf = ftf + 1e-10 * pt.mean(pt.diag(ftf)) * pt.eye(k)
    atb = a.T @ b
    lc = cholesky(ftf, lower=True)
    v = solve_triangular(lc, atb, lower=True)
    rr = bb - v @ v
    s = noise_scale
    return -0.5 * rr / s**2 - (n_valid - k) * pt.log(s) - pt.sum(pt.log(pt.diag(lc)))


def amplitude_stats(comps: np.ndarray, prob: Problem) -> tuple[np.ndarray, np.ndarray, float]:
    """``(a_hat, chol(A^T A), |r|^2)`` of the linear amplitudes given component images
    ``(K, n_row, n_col)`` (NumPy)."""
    k = comps.shape[0]
    a = np.stack([prob.whiten(c * prob.isig).ravel() for c in comps], axis=1)
    b = prob.whiten(prob.image * prob.isig).ravel()
    ftf = a.T @ a
    ftf = ftf + 1e-10 * np.mean(np.diag(ftf)) * np.eye(k)
    lc = np.linalg.cholesky(ftf)
    a_hat = np.linalg.solve(ftf, a.T @ b)
    r = b - a @ a_hat
    return a_hat, lc, float(r @ r)


# ============================================================================== compiled


class Compiled:
    """Compiled PyTensor functions for one problem (C backend, compiled lazily)."""

    def __init__(self, prob: Problem, layout: LensLayout) -> None:
        self.prob, self.layout = prob, layout
        self._objective: dict[str, Callable[..., Any]] = {}
        self._forward: Callable[..., Any] | None = None

    def objective(self, which: Literal["full", "light"]) -> Callable[..., Any]:
        """``f(u, isig, b, bb, n_valid) -> (-log posterior, gradient)`` on ``u``."""
        if which not in self._objective:
            layout = self.layout if which == "full" else self.layout.light_only()
            u = pt.vector("u")
            isig = pt.matrix("isig")
            b = pt.vector("b")
            bb = pt.scalar("bb")
            nv = pt.scalar("n_valid")
            p = layout.values(u, pt)
            comps = components_subset(p, self.prob, layout, which)
            s = p.get("noise_scale", 1.0)
            obj = -(layout.logprior(u, pt) + marginal_loglik(comps, self.prob, isig, b, bb, nv, s))
            grad: Any = pytensor.grad(obj, u)
            self._objective[which] = pytensor.function(
                [u, isig, b, bb, nv], [obj, grad], on_unused_input="ignore"
            )
        return self._objective[which]

    def forward(self) -> Callable[..., np.ndarray]:
        """``f(*physical blocks) -> (K, n_row, n_col)`` component images."""
        if self._forward is None:
            inputs = [
                pt.tensor(b.name, shape=b.shape, dtype="float64")
                for b in self.layout.blocks
                if b.name != "noise_scale"
            ]
            p: dict[str, Any] = {str(v.name): v for v in inputs}
            comps = components(p, self.prob, self.layout, pt)
            self._forward = pytensor.function(inputs, pt.stack(comps), on_unused_input="ignore")
        return self._forward

    def forward_values(self, values: dict[str, np.ndarray]) -> np.ndarray:
        names = [b.name for b in self.layout.blocks if b.name != "noise_scale"]
        return np.asarray(self.forward()(*[np.asarray(values[n], dtype=float) for n in names]))


def components_subset(
    p: dict[str, Any], prob: Problem, layout: LensLayout, which: str
) -> list[Any]:
    """All components (``full``) or the lens light and the sky only (``light``)."""
    if which == "full":
        return components(p, prob, layout, pt)
    wrap = pt.constant
    x, y = pt.constant(prob.x), pt.constant(prob.y)
    out: list[Any] = []
    for j in range(layout.n_light):
        e = p[f"e_light_{j}"]
        img = lm.sersic(
            x,
            y,
            p[f"R_light_{j}"],
            p[f"n_light_{j}"],
            e[0],
            e[1],
            p["xy_light"][0],
            p["xy_light"][1],
            pt,
        )
        binned = lm.bin_pixels(img, prob.n_row, prob.n_col, prob.subsample)
        out.append(lm.convolve(binned, prob.psf, constants=wrap))
    out.append(pt.ones((prob.n_row, prob.n_col)))
    return out


# ============================================================================== mode finding


@dataclass(slots=True)
class ModeResult:
    """The staged optimisation: the best mode, its Laplace factor and the start table."""

    u_map: np.ndarray
    chol: np.ndarray
    neg_logpost: float
    theta_e_start: float
    starts: list[dict[str, float]]
    light_map: dict[str, list[float]]
    seconds: float


def _call(fun: Callable[..., Any], extra: tuple[Any, ...], u: np.ndarray) -> Any:
    return fun(u, *extra)


def _minimise(
    fun: Callable[[np.ndarray], Any],
    u0: np.ndarray,
    max_iter: int,
    bounds: list[tuple[float, float]],
) -> Any:
    def f(u: np.ndarray) -> tuple[float, np.ndarray]:
        v, g = fun(u)
        v, g = float(v), np.asarray(g, dtype=float)
        if not np.isfinite(v) or not np.all(np.isfinite(g)):
            return 1e30, np.zeros_like(u)
        return v, g

    with np.errstate(all="ignore"):
        lo, hi = np.array(bounds).T
        return optimize.minimize(
            f,
            np.where(lo == hi, lo, np.clip(u0, lo + 1e-6, hi - 1e-6)),  # inf-safe
            jac=True,
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": max_iter},
        )


def find_mode(
    comp: Compiled, s: StrongLensSettings, log: Callable[[str], None] | None = None
) -> ModeResult:
    """Staged start (light, Einstein radius, starts of the full model) and Laplace factor."""
    tic = time.perf_counter()
    prob, layout = comp.prob, comp.layout
    isig = prob.isig
    b_full = prob.whiten(prob.image * isig).ravel()

    def args(isig_: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
        bw = prob.whiten(prob.image * isig_).ravel()
        return isig_, bw, float(bw @ bw), float((isig_ > 0).sum())

    light_vals: dict[str, np.ndarray] = {}
    light_model = np.zeros_like(prob.image)
    if layout.n_light:
        lo = layout.light_only()
        start = {
            "xy_light": np.zeros(2),
            **{f"R_light_{j}": float(np.exp(lo.blocks[1 + j].a)) for j in range(layout.n_light)},
            **{f"n_light_{j}": [2.0, 1.0][j] for j in range(layout.n_light)},
            **{f"e_light_{j}": np.array([-0.05, 0.01]) for j in range(layout.n_light)},
        }
        u_l = lo.pack(start)
        fun = comp.objective("light")
        cur = isig
        for _ in range(3):
            a = args(cur)
            res = _minimise(
                functools.partial(_call, fun, a), u_l, s.max_iter, [(-np.inf, np.inf)] * lo.size
            )
            u_l = res.x
            light_vals = {k: np.asarray(v) for k, v in lo.values(u_l, np).items()}
            light_model = _light_image(comp, light_vals, cur)
            resid = (prob.image - light_model) * isig
            ring = ndimage.binary_dilation(resid > 3.0, iterations=1)
            cur = np.where(ring, 0.0, isig)
        if log:
            log(f"lens light fitted ({res.nit} iterations)")
    # Einstein radius from the azimuthal profile of the light-subtracted image
    yy, xx = np.indices(prob.image.shape)
    cx, cy = light_vals["xy_light"] if layout.n_light else np.zeros(2)
    rpix = np.hypot(
        (xx - (prob.n_col - 1) / 2) * prob.pixel_scale - cx,
        (yy - (prob.n_row - 1) / 2) * prob.pixel_scale - cy,
    )
    sub = prob.image - light_model
    ok = isig > 0
    # search the ring within the central 95% of the theta_E prior (and inside the cutout),
    # so the residual core of the lens light cannot pass for the ring
    te_block = next(b for b in layout.blocks if b.name == "theta_E")
    r_lo = max(2 * prob.pixel_scale, float(np.exp(te_block.a - 2 * te_block.b)))
    r_hi = min(
        0.48 * min(prob.n_row, prob.n_col) * prob.pixel_scale,
        float(np.exp(te_block.a + 2 * te_block.b)),
    )
    edges = np.arange(r_lo, max(r_hi, r_lo + prob.pixel_scale), prob.pixel_scale)
    prof = np.array(
        [
            np.nanmean(np.where(ok & (rpix >= r) & (rpix < r + prob.pixel_scale), sub, np.nan))
            if np.any(ok & (rpix >= r) & (rpix < r + prob.pixel_scale))
            else -np.inf
            for r in edges
        ]
    )
    te0 = float(edges[int(np.argmax(prof))] + prob.pixel_scale / 2)
    if s.theta_e_start is not None:
        te0 = s.theta_e_start
    # starts of the full model around the lens axis
    fun = comp.objective("full")
    a = (isig, b_full, float(b_full @ b_full), float(prob.n_valid))
    best: Any = None
    table: list[dict[str, float]] = []
    light_names = {b.name for b in layout.light_blocks}
    # a box only on theta_E (the ring must lie in the cutout): wider boxes trap L-BFGS-B
    # in corners, and the priors already keep the other parameters in range
    box: list[tuple[float, float]] = [(-np.inf, np.inf)] * layout.size
    te_at = layout.slices()["theta_E"].start
    half = 0.5 * min(prob.n_row, prob.n_col) * prob.pixel_scale
    box[te_at] = (-np.inf, float(np.log(half)))
    prior_box = layout.bounds()
    prior_box[te_at] = (prior_box[te_at][0], min(prior_box[te_at][1], float(np.log(half))))

    def start_u(ox: float, oy: float) -> np.ndarray:
        # a source twice the prior median: a wide source smooths the likelihood surface
        vals: dict[str, Any] = {
            **light_vals,
            "theta_E": te0,
            "e_mass": np.array([0.01, 0.01]),
            "xy_mass": np.asarray(light_vals.get("xy_light", np.zeros(2))),
            "shear": np.zeros(2),
            "R_src": 2.0 * s.source_size_median,
            "n_src": 1.0,
            "e_src": np.array([0.01, 0.01]),
            "xy_src": np.array([cx + ox, cy + oy]),
            "noise_scale": 1.0,
        }
        return layout.pack(vals)

    offsets = _scan_starts(lambda o: float(fun(start_u(*o), *a)[0]), te0, s.n_starts)
    for ox, oy in offsets:
        u0 = start_u(ox, oy)
        res = _minimise(lambda u: fun(u, *a), u0, s.max_iter, box)
        # also, inside five prior sds: first the source and the mass shape with theta_E held
        # at the ring radius and the lens light at its stage-1 fit (a badly placed source
        # otherwise drags theta_E away, and the light can absorb the ring), then with
        # theta_E free, then everything together; keep the best
        held = list(prior_box)
        for name, sl in layout.slices().items():
            if name in light_names:
                for i in range(sl.start, sl.stop):
                    held[i] = (float(u0[i]), float(u0[i]))
        held_te = list(held)
        held_te[te_at] = (float(u0[te_at]), float(u0[te_at]))
        u1 = _minimise(lambda u: fun(u, *a), u0, s.max_iter, held_te).x
        if layout.n_light:
            u1 = _minimise(lambda u: fun(u, *a), u1, s.max_iter, held).x
        res1 = _minimise(lambda u: fun(u, *a), u1, s.max_iter, prior_box)
        if res1.fun < res.fun:
            res = res1
        got = layout.values(res.x, np)
        table.append(
            {
                "start_src_x": float(cx + ox),
                "start_src_y": float(cy + oy),
                "theta_E": float(got["theta_E"]),
                "neg_logpost": float(res.fun),
            }
        )
        if best is None or res.fun < best.fun:
            best = res
    assert best is not None
    if log:
        log(f"best of {len(table)} starts: -log posterior {best.fun:.1f}")
    chol = laplace_factor(lambda u: fun(u, *a), best.x)
    return ModeResult(
        u_map=np.asarray(best.x),
        chol=chol,
        neg_logpost=float(best.fun),
        theta_e_start=te0,
        starts=table,
        light_map={k: np.atleast_1d(v).tolist() for k, v in light_vals.items()},
        seconds=time.perf_counter() - tic,
    )


def _scan_starts(
    neg_logpost: Callable[[tuple[float, float]], float], te0: float, n: int, side: int = 9
) -> list[tuple[float, float]]:
    """Source offsets for the ``n`` starts: the best points of a ``side`` x ``side`` scan
    of the source position within 0.3 theta_E of the lens axis (the other parameters at
    their start), at least 1.5 scan steps apart. The marginal likelihood of a wrongly
    placed source is flat, so a local optimiser cannot find the arcs from far away."""
    step = 0.6 * te0 / (side - 1)
    grid = [
        ((i - side // 2) * step, (j - side // 2) * step) for i in range(side) for j in range(side)
    ]
    scores = []
    for o in grid:
        v = neg_logpost(o)
        scores.append(v if np.isfinite(v) else np.inf)
    chosen: list[tuple[float, float]] = []
    for k in np.argsort(scores, kind="stable"):
        o = grid[int(k)]
        if all(np.hypot(o[0] - c[0], o[1] - c[1]) > 1.5 * step for c in chosen):
            chosen.append(o)
        if len(chosen) == n:
            break
    return chosen


def _light_image(comp: Compiled, light: dict[str, np.ndarray], isig: np.ndarray) -> np.ndarray:
    """The best-fit lens light plus sky for given light parameters and pixel weights."""
    prob, layout = comp.prob, comp.layout
    p = {k: np.asarray(v) for k, v in light.items()}
    imgs = []
    for j in range(layout.n_light):
        e = p[f"e_light_{j}"]
        img = lm.sersic(
            prob.x,
            prob.y,
            p[f"R_light_{j}"],
            p[f"n_light_{j}"],
            e[0],
            e[1],
            p["xy_light"][0],
            p["xy_light"][1],
            np,
        )
        imgs.append(
            lm.convolve(lm.bin_pixels(img, prob.n_row, prob.n_col, prob.subsample), prob.psf)
        )
    imgs.append(np.ones((prob.n_row, prob.n_col)))
    c = np.stack(imgs)
    sub = Problem(**{**{f: getattr(prob, f) for f in Problem.__dataclass_fields__}, "isig": isig})
    a_hat, _, _ = amplitude_stats(c, sub)
    out: np.ndarray = np.tensordot(a_hat, c, 1)
    return out


def laplace_factor(
    fun: Callable[[np.ndarray], Any], u: np.ndarray, rel: float = 1e-4
) -> np.ndarray:
    """Cholesky factor of the inverse Hessian of ``fun`` at ``u`` (central differences of
    the exact gradient; eigenvalues floored so the factor always exists)."""
    n = u.size
    h = np.zeros((n, n))
    for i in range(n):
        e = np.zeros(n)
        e[i] = rel * max(abs(u[i]), 1.0)
        h[i] = (np.asarray(fun(u + e)[1]) - np.asarray(fun(u - e)[1])) / (2 * e[i])
    h = (h + h.T) / 2
    w, v = np.linalg.eigh(h)
    floor = max(float(np.max(np.abs(w))) * 1e-8, 1e-6)
    w = np.where(np.isfinite(w), np.maximum(w, floor), 1.0)
    cov = (v / w) @ v.T
    out: np.ndarray = np.linalg.cholesky((cov + cov.T) / 2)
    return out


# ============================================================================== the plugin


class StrongLensModel(CustomModel[StrongLensSettings]):
    """Galaxy-scale strong lens: SIE + shear, Sersic light and source, PSF, every pixel."""

    id = "sci.strong_lens"
    version = "1.0.0"
    Settings = StrongLensSettings
    description = (
        "A pixel forward model of a galaxy-scale strong gravitational lens: a singular "
        "isothermal ellipsoid with external shear traces every pixel to a Sersic source; the "
        "lens light, a PSF and correlated noise are modelled; the light amplitudes are "
        "integrated out. Gives the Einstein radius, the unlensed source and critical curves."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate", "simulate"}),
        loo_valid=False,
        loo_invalid_reason="the light amplitudes are integrated out over all pixels and the "
        "whitened noise couples neighbouring pixels, so pointwise leave-one-out is not defined",
        identified_quantities=["theta_E", "e_mass", "xy_mass", "shear"],
        deterministic_given_seed=False,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "model_image": {
            "dims": [ROW, COL],
            "units_of": IMAGE,
            "description": "Expected image (lens light, lensed source, sky, PSF), or a "
            "simulated observation with noise",
            "grid": {"y": ROW, "x": COL, "origin": "lower", "colormap": "gray"},
        },
        "source": {
            "dims": [SRC_ROW, SRC_COL],
            "units_of": IMAGE,
            "description": "The unlensed source (parametric Sersic of each draw) on a "
            "source-plane grid",
            "grid": {"y": SRC_ROW, "x": SRC_COL, "origin": "lower", "colormap": "magma"},
        },
        "einstein_radius": {
            "dims": [],
            "units": "arcsec",
            "description": "Einstein radius of the isothermal ellipsoid",
        },
        "einstein_mass": {
            "dims": [],
            "units": "solar_mass",
            "description": "Mass inside the Einstein radius (needs z_lens and z_source)",
        },
    }
    derived: ClassVar[list[dict[str, Any]]] = [
        {
            "name": CRITICAL_CURVES,
            "dims": ["sample", "curve_point"],
            "units": "arcsec",
            "cache": "fit",
            "description": "Tangential critical curve (image plane), its caustic and the cut "
            "(source plane) for posterior draws, in arcsec and grid pixel indices",
        }
    ]
    plots = ("core.convergence", "core.posterior_forest", "sci.field_map")
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=600
    )
    pad_extend = False

    # ================================================================== fit
    def build_model(self, comp: Compiled, mode: ModeResult) -> pm.Model:
        """The preconditioned PyMC model: ``u = u_map + L z`` with a flat ``z``."""
        prob, layout = comp.prob, comp.layout
        coords = _coords(layout)
        b = prob.whiten(prob.image * prob.isig).ravel()
        with pm.Model(coords=coords) as model:
            z = pm.Flat("z", shape=layout.size, initval=np.zeros(layout.size))
            u = pt.constant(mode.u_map) + pt.constant(mode.chol) @ z
            p = layout.values(u, pt)
            comps = components(p, prob, layout, pt)
            ll = marginal_loglik(
                comps,
                prob,
                pt.constant(prob.isig),
                pt.constant(b),
                float(b @ b),
                float(prob.n_valid),
                p.get("noise_scale", 1.0),
            )
            pm.Potential("prior", layout.logprior(u, pt))
            pm.Potential("image", ll)
            for name, dims in _physical(layout).items():
                pm.Deterministic(name, _stack(p, name, layout), dims=dims or None)
        return model

    def fit(
        self, ds: xr.Dataset, s: StrongLensSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        image, weight, psf, mask = _arrays(ds, s, require_target=True)
        prob = Problem.build(image, weight, psf, s, mask)
        if prob.n_valid < 10:
            raise MapiError("fewer than 10 usable pixels to fit", code=ErrorCode.SCHEMA_INVALID)
        layout = LensLayout.from_settings(s)
        comp = Compiled(prob, layout)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        ctx.progress(0.02, "finding the mode in stages")
        with _quiet():
            mode = find_mode(comp, s, ctx.log)
        ctx.check_cancelled()
        ctx.progress(0.15, "sampling the posterior")
        model = self.build_model(comp, mode)
        plan = EnginePlan.from_config(cfg, default_engine=None, seed=seed)
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
                ctx.progress(0.15 + 0.7 * fraction, message)

        tic = time.perf_counter()
        with _quiet():
            result = run_engine(
                model,
                plan,
                EngineHooks(progress=report, cancel=ctx.cancel, log=ctx.log, sink=sink, key=key),
            )
        sample_seconds = time.perf_counter() - tic
        ctx.check_cancelled()
        raw = result.group("posterior")
        if raw is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        names = list(_physical(layout))
        posterior = raw[names].assign_coords(coords_of(layout))
        ctx.progress(0.87, "drawing the light amplitudes")
        posterior["amplitude"] = _amplitude_draws(comp, posterior, layout, seed)
        posterior = _clean(posterior, result.engine)
        groups: dict[str, xr.Dataset] = {"posterior": posterior}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], result.engine)
        groups["observed_data"] = _clean(
            xr.Dataset({IMAGE: ds[IMAGE].transpose(ROW, COL)}), result.engine
        )
        const = {
            WEIGHT: ds[WEIGHT].transpose(ROW, COL),
            PSF: ds[PSF].transpose(PSF_ROW, PSF_COL),
        }
        if mask is not None:
            const[MASK] = ds[MASK].transpose(ROW, COL)
        groups["constant_data"] = _clean(xr.Dataset(const), result.engine)
        grid = _source_grid(posterior, s)
        ctx.progress(0.95, "tracing the critical curves")
        curves = _curves(posterior, s, prob, grid, seed)
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "notes": [str(n) for n in result.notes],
            "source_grid": grid,
            "components": layout.component_names(),
            "psf_rank": prob.psf.rank,
            "psf_error": prob.psf.error,
            "n_pixels": prob.n_valid,
            "mode": {
                "neg_logpost": mode.neg_logpost,
                "theta_E_start": mode.theta_e_start,
                "starts": mode.starts,
                "values": {
                    k: np.atleast_1d(v).tolist() for k, v in layout.values(mode.u_map, np).items()
                },
                "laplace_sd_theta_E": _laplace_sd(layout, mode, "theta_E"),
                "seconds": mode.seconds,
            },
            "sample_seconds": sample_seconds,
        }
        ctx.progress(1.0, "posterior ready")
        return FitOutput(groups=groups, learned=learned, derived={CRITICAL_CURVES: curves})

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None or int(post.sizes.get("draw", 1)) < 4:
            return []
        names = [str(v) for v in post.data_vars if v != "amplitude"]
        return convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        obs = fitted.group("observed_data")
        const = fitted.group("constant_data")
        image = np.asarray(obs[IMAGE].transpose(ROW, COL).values, dtype=float)
        weight = np.asarray(const[WEIGHT].transpose(ROW, COL).values, dtype=float)
        psf = np.asarray(const[PSF].transpose(PSF_ROW, PSF_COL).values, dtype=float)
        mask = (
            np.asarray(const[MASK].transpose(ROW, COL).values, dtype=bool)
            if MASK in const
            else None
        )
        prob = Problem.build(image, weight, psf, s, mask)
        layout = LensLayout.from_settings(s)
        comp = Compiled(prob, layout)
        post = fitted.posterior
        chain, draw = post["chain"].values, post["draw"].values
        n = chain.size * draw.size
        values = _flat_values(post, layout)
        amp = np.asarray(
            post["amplitude"].transpose("chain", "draw", COMPONENT).values, dtype=float
        ).reshape(n, -1)
        imgs = np.empty((n, prob.n_row, prob.n_col))
        with _quiet():
            for i in range(n):
                if i % 64 == 0:
                    ctx.check_cancelled()
                c = comp.forward_values({k: v[i] for k, v in values.items()})
                imgs[i] = np.tensordot(amp[i], c, 1)
        noisy = ctx.mode == "simulate" or ctx.kind == "predictive"
        noise = "none"
        if noisy:
            imgs = imgs + _noise(prob, values.get("noise_scale"), n, ctx.seed)
            noise = "crn_seeded"
        rows = np.asarray(fitted.coords.get(ROW, obs[ROW].values))
        cols = np.asarray(fitted.coords.get(COL, obs[COL].values))
        shape = (chain.size, draw.size)
        model_image = xr.DataArray(
            imgs.reshape(*shape, prob.n_row, prob.n_col),
            dims=("chain", "draw", ROW, COL),
            coords={"chain": chain, "draw": draw, ROW: rows, COL: cols},
            attrs={"kind": "predictive" if noisy else "expected", "noise": noise},
        )
        if ROW in ds.coords and COL in ds.coords:
            model_image = model_image.sel({ROW: ds[ROW].values, COL: ds[COL].values})
        grid = fitted.learned.get("source_grid")
        src = _source_images(values, amp, layout, s, grid if isinstance(grid, dict) else None)
        g = src.shape[-1]
        arrays: dict[str, xr.DataArray] = {
            "model_image": model_image,
            "source": xr.DataArray(
                src.reshape(*shape, g, g),
                dims=("chain", "draw", SRC_ROW, SRC_COL),
                coords={SRC_ROW: np.arange(g), SRC_COL: np.arange(g)},
                attrs={"kind": "expected", "noise": "none"},
            ),
            "einstein_radius": xr.DataArray(
                values["theta_E"].reshape(shape),
                dims=("chain", "draw"),
                attrs={"kind": "expected", "noise": "none", "units": "arcsec"},
            ),
        }
        if s.z_lens is not None and s.z_source is not None:
            mass = lm.einstein_mass(
                values["theta_E"], s.z_lens, s.z_source, h0=s.h0, omega_m=s.omega_m
            )
            arrays["einstein_mass"] = xr.DataArray(
                mass.reshape(shape),
                dims=("chain", "draw"),
                attrs={"kind": "expected", "noise": "none", "units": "solar_mass"},
            )
        pred = xr.Dataset(arrays, coords={"chain": chain, "draw": draw})
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ============================================================================== helpers


def _coords(layout: LensLayout) -> dict[str, list[Any]]:
    return {
        AXIS: ["x", "y"],
        ECOMP: ["e1", "e2"],
        LIGHT: list(range(1, layout.n_light + 1)),
        COMPONENT: layout.component_names(),
    }


def coords_of(layout: LensLayout) -> dict[str, list[Any]]:
    """Coordinates of the posterior's parameter dims."""
    c = _coords(layout)
    if not layout.n_light:
        c.pop(LIGHT)
    c.pop(COMPONENT)
    return c


def _physical(layout: LensLayout) -> dict[str, tuple[str, ...]]:
    """Posterior variables (physical parameters) and their dims."""
    out: dict[str, tuple[str, ...]] = {}
    if layout.n_light:
        out.update(
            {
                "xy_light": (AXIS,),
                "R_light": (LIGHT,),
                "n_light": (LIGHT,),
                "e_light": (LIGHT, ECOMP),
            }
        )
    out.update({"theta_E": (), "e_mass": (ECOMP,), "xy_mass": (AXIS,)})
    if layout.shear:
        out["shear"] = (ECOMP,)
    out.update({"R_src": (), "n_src": (), "e_src": (ECOMP,), "xy_src": (AXIS,)})
    if layout.noise:
        out["noise_scale"] = ()
    return out


def _stack(p: dict[str, Any], name: str, layout: LensLayout) -> Any:
    k = layout.n_light
    if name in ("R_light", "n_light"):
        return pt.stack([p[f"{name}_{j}"] for j in range(k)])
    if name == "e_light":
        return pt.stack([p[f"e_light_{j}"] for j in range(k)])
    return p[name]


def _flat_values(post: xr.Dataset, layout: LensLayout) -> dict[str, np.ndarray]:
    """Per-draw block values ``{block: (n, *shape)}`` from the posterior."""
    out: dict[str, np.ndarray] = {}
    for name, dims in _physical(layout).items():
        arr = np.asarray(post[name].transpose("chain", "draw", *dims).values, dtype=float)
        arr = arr.reshape(-1, *arr.shape[2:])
        if name in ("R_light", "n_light", "e_light"):
            for j in range(layout.n_light):
                out[f"{name}_{j}"] = arr[:, j]
        else:
            out[name] = arr
    return out


def _amplitude_draws(
    comp: Compiled, post: xr.Dataset, layout: LensLayout, seed: int
) -> xr.DataArray:
    """One draw of the linear amplitudes per posterior draw from their exact conditional
    Gaussian ``N(a_hat, s^2 (A^T A)^{-1})``."""
    values = _flat_values(post, layout)
    chain, draw = post["chain"].values, post["draw"].values
    n = chain.size * draw.size
    rng = _rng(seed, "amplitude")
    k = layout.n_components
    out = np.empty((n, k))
    scale = values.get("noise_scale", np.ones(n))
    for i in range(n):
        c = comp.forward_values({kk: v[i] for kk, v in values.items()})
        a_hat, lc, _ = amplitude_stats(c, comp.prob)
        z = rng.standard_normal(k)
        out[i] = a_hat + float(scale[i]) * np.linalg.solve(lc.T, z)
    return xr.DataArray(
        out.reshape(chain.size, draw.size, k),
        dims=("chain", "draw", COMPONENT),
        coords={"chain": chain, "draw": draw, COMPONENT: layout.component_names()},
    )


def _noise(prob: Problem, scale: np.ndarray | None, n: int, seed: int) -> np.ndarray:
    """Seeded correlated noise ``s sigma (L_r Z L_c^T)`` (zero where a pixel has no weight)."""
    rng = _rng(seed, "model_image")
    z = rng.standard_normal((n, prob.n_row, prob.n_col))
    if prob.l_row is not None and prob.l_col is not None:
        z = prob.l_row @ z @ prob.l_col.T
    sigma = prob.sigma
    s = np.ones(n) if scale is None else np.asarray(scale, dtype=float).reshape(n)
    out: np.ndarray = s[:, None, None] * sigma * z
    return out


def _source_grid(post: xr.Dataset, s: StrongLensSettings) -> dict[str, float | int]:
    """Source-plane grid geometry: centred on the posterior-mean source position."""
    xy = post["xy_src"].mean(("chain", "draw")).values
    g = s.source_grid
    step = 2 * s.source_half_width / (g - 1)
    return {
        "x0": float(xy[0]) - s.source_half_width,
        "y0": float(xy[1]) - s.source_half_width,
        "step": step,
        "size": g,
    }


def _source_axes(
    grid: dict[str, Any] | None, s: StrongLensSettings
) -> tuple[np.ndarray, np.ndarray]:
    g = int(grid["size"]) if grid else s.source_grid
    step = float(grid["step"]) if grid else 2 * s.source_half_width / (g - 1)
    x0 = float(grid["x0"]) if grid else -s.source_half_width
    y0 = float(grid["y0"]) if grid else -s.source_half_width
    return x0 + step * np.arange(g), y0 + step * np.arange(g)


def _source_images(
    values: dict[str, np.ndarray],
    amp: np.ndarray,
    layout: LensLayout,
    s: StrongLensSettings,
    grid: dict[str, Any] | None,
) -> np.ndarray:
    """``amplitude_source * Sersic`` on the source grid for every draw, ``(n, g, g)``."""
    ax, ay = _source_axes(grid, s)
    bx, by = np.meshgrid(ax, ay)
    j = layout.component_names().index("source")
    es, xs = values["e_src"], values["xy_src"]
    prof = lm.sersic(
        bx[None],
        by[None],
        values["R_src"][:, None, None],
        values["n_src"][:, None, None],
        es[:, 0, None, None],
        es[:, 1, None, None],
        xs[:, 0, None, None],
        xs[:, 1, None, None],
    )
    out: np.ndarray = amp[:, j, None, None] * prof
    return out


def _curves(
    post: xr.Dataset,
    s: StrongLensSettings,
    prob: Problem,
    grid: dict[str, Any],
    seed: int,
) -> xr.Dataset:
    """Critical curves, caustics and cuts for the first ``curve_draws`` thinned draws."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    pairs = thin_index(chains, draws, seed=seed)[: s.curve_draws]
    keys = ("critical_x", "critical_y", "caustic_x", "caustic_y", "cut_x", "cut_y")
    acc: dict[str, list[np.ndarray]] = {k: [] for k in keys}
    for c, d in pairs:
        pt_ = post.isel(chain=int(c), draw=int(d))
        em, xm = pt_["e_mass"].values, pt_["xy_mass"].values
        g = pt_["shear"].values if "shear" in pt_ else np.zeros(2)
        cc = lm.critical_curves(
            float(pt_["theta_E"]),
            *map(float, em),
            *map(float, xm),
            *map(float, g),
            n=s.curve_points,
        )
        for k in keys:
            acc[k].append(getattr(cc, k))
    data = {k: (("sample", "curve_point"), np.stack(v)) for k, v in acc.items()}
    ds = xr.Dataset(
        data,
        coords={
            "sample": np.arange(len(pairs)),
            "curve_point": np.arange(s.curve_points),
            "chain": ("sample", post["chain"].values[pairs[:, 0]]),
            "draw": ("sample", post["draw"].values[pairs[:, 1]]),
        },
    )
    ps = prob.pixel_scale
    ds["critical_col"] = ds["critical_x"] / ps + (prob.n_col - 1) / 2
    ds["critical_row"] = ds["critical_y"] / ps + (prob.n_row - 1) / 2
    ds["caustic_col"] = (ds["caustic_x"] - grid["x0"]) / grid["step"]
    ds["caustic_row"] = (ds["caustic_y"] - grid["y0"]) / grid["step"]
    ds["cut_col"] = (ds["cut_x"] - grid["x0"]) / grid["step"]
    ds["cut_row"] = (ds["cut_y"] - grid["y0"]) / grid["step"]
    ds.attrs = {
        "pixel_scale": ps,
        "image_plane": "critical_* (model_image grid: critical_col, critical_row)",
        "source_plane": "caustic_*, cut_* (source grid: caustic_col, caustic_row)",
    }
    return ds


def _laplace_sd(layout: LensLayout, mode: ModeResult, name: str) -> float:
    """Laplace sd of a lognormal block's physical value (delta method)."""
    sl = layout.slices()[name]
    cov = mode.chol @ mode.chol.T
    sd_u = float(np.sqrt(cov[sl.start, sl.start]))
    return float(np.exp(mode.u_map[sl.start]) * sd_u)


def _arrays(
    ds: xr.Dataset, s: StrongLensSettings, *, require_target: bool
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    for d in (ROW, COL, PSF_ROW, PSF_COL):
        if d not in ds.dims:
            raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
    for v in (IMAGE, WEIGHT, PSF):
        if v not in ds and (v != IMAGE or require_target):
            raise MapiError(f"the data has no variable {v!r}", code=ErrorCode.SCHEMA_INVALID)
    image = np.asarray(ds[IMAGE].transpose(ROW, COL).values, dtype=float)
    weight = np.asarray(ds[WEIGHT].transpose(ROW, COL).values, dtype=float)
    psf = np.asarray(ds[PSF].transpose(PSF_ROW, PSF_COL).values, dtype=float)
    if psf.shape[0] % 2 == 0 or psf.shape[1] % 2 == 0:
        raise MapiError("the PSF kernel must have odd sides", code=ErrorCode.SCHEMA_INVALID)
    if not np.all(np.isfinite(psf)) or psf.sum() <= 0:
        raise MapiError(
            "the PSF kernel must be finite with a positive sum", code=ErrorCode.SCHEMA_INVALID
        )
    mask = None
    if s.use_mask:
        if MASK not in ds:
            raise MapiError(
                f"use_mask is set but the data has no {MASK!r}", code=ErrorCode.SCHEMA_INVALID
            )
        mask = np.asarray(ds[MASK].transpose(ROW, COL).values, dtype=bool)
    return image, weight, psf, mask


def _rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
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
