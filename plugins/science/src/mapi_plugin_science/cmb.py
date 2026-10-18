"""``sci.cmb_field``: field-level inference of a masked CMB temperature map (E79).

The cosmic microwave background (CMB) is a statistically isotropic Gaussian random field.
This model treats every pixel of the sky on a flat patch as a parameter and samples it
jointly with the power spectrum, with a mask and a noise level per pixel.

Canonical inputs::

    dims  row, col:   integer, fixed (the raster; row 0 at the bottom)
          multipole:  integer, fixed (0, 1, 2, ... for the reference spectrum)
    vars  temperature(row, col): target, float64, uK; NaN where the pixel is masked
          noise_sd(row, col):    weight, float64, uK; the noise sd of each pixel
          reference_dl(multipole): constant, float64, uK^2; the reference spectrum
            D_ell = ell (ell + 1) C_ell / 2 pi (for example the Planck best fit)
          pixel_window(multipole): constant, float64 (optional, setting
            ``pixel_window``): the window of the map's own pixels (HEALPix)

**Model.** The sky lives on a periodic torus: the raster plus an unobserved ``border`` of
pixels on each side (a real patch is not periodic). Its Hartley coefficients are
independent, ``a_k ~ N(0, S_k exp(q_b(k)))``, and the map is ``H(a)``. ``S_k`` is the
reference spectrum seen through the beam, the map's pixel window and the box average of the
model pixels, with the power aliased from beyond the Nyquist frequency added
(:func:`folded_power`); the multipoles of the plane are multiplied by the mean
magnification of a gnomonic projection (``projection: gnomonic``; ``flat`` turns this
off). ``q_b`` is the log ratio of the bandpower in bin ``b`` to the reference, with a flat
prior. Each usable pixel has ``y_x ~ N(H(a)_x, sigma_x^2)``; masked pixels have no term.

**Sampler.** The Wandelt-Eriksen Gibbs sampler (:class:`FieldSampler`), as in the
notebook: (1) a constrained realisation of the sky given ``q`` by preconditioned conjugate
gradients (dense preconditioner below ``ell_dense``, diagonal above); (2) ``exp(q_b)``
from its inverse-gamma conditional; (3) a non-centred rescaling move per bin (Metropolis),
which helps in the noise-dominated bins. ``tune`` iterations are burn-in and ``draws`` are
kept per chain; all chains run together, from dispersed starts. NumPy and ``scipy.fft``
only; no NUTS.

**Stored.** ``posterior``: ``q(bin)``, ``bandpower(bin)`` (uK^2), ``bandpower_ratio(bin)``
and an amplitude-and-tilt summary of the bandpowers (``log_amplitude``, ``tilt``: the exact
Gaussian posterior of the notebook's part H model, one draw per Gibbs draw).
``sample_stats``: ``cg_iterations`` and ``rescale_acceptance``. The fit also caches the
derived quantity ``sky_moments``: the posterior mean and sd of each pixel over every kept
Gibbs draw.

**Scores** (``predict``, ``evaluate``, ``simulate``): ``sky(row, col)`` is a constrained
realisation of the sky for each of ``field_draws`` posterior draws of ``q`` (an exact
posterior draw of the sky); ``temperature_hat`` is the same plus white noise with
``noise_sd`` for ``kind: predictive``. ``simulate`` gives unconstrained skies with the
spectrum of each posterior draw (another patch of the same sky), plus noise. Each field
draw has its own generator seeded by ``(seed, chain, draw)``. Pointwise LOO is not offered:
the pixels share one latent field.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, ClassVar, Literal

import numpy as np
import scipy.fft as sf
import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics
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
from pydantic import Field, field_validator

__all__ = [
    "ARCMIN",
    "BIN_DIM",
    "COL_DIM",
    "MULTIPOLE_DIM",
    "NOISE",
    "NOTEBOOK_EDGES",
    "PIXEL_WINDOW",
    "REFERENCE",
    "ROW_DIM",
    "SKY_MOMENTS",
    "TARGET",
    "CMBFieldModel",
    "CMBFieldSettings",
    "FieldSampler",
    "Instrument",
    "amplitude_tilt",
    "folded_power",
    "hartley",
    "projection_stretch",
]

ROW_DIM = "row"
COL_DIM = "col"
MULTIPOLE_DIM = "multipole"
BIN_DIM = "bin"
TARGET = "temperature"
NOISE = "noise_sd"
REFERENCE = "reference_dl"
PIXEL_WINDOW = "pixel_window"
SKY_MOMENTS = "sky_moments"
ARCMIN = np.pi / 180 / 60
NOTEBOOK_EDGES: tuple[float, ...] = (
    0, 60, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800,
    850, 900, 1000, 1100, 1200, 1300, 1450, 1600, 1800, 2100,
)  # fmt: skip
"""The 26 bandpower bins of the notebook (multipole edges)."""
MAX_TORUS = 512 * 512
MAX_DENSE = 2500
_WORKERS = 4
POSTERIOR_VARS = ("q", "bandpower", "bandpower_ratio", "log_amplitude", "tilt")


# ============================================================================== flat-sky maths


def hartley(a: np.ndarray) -> np.ndarray:
    """2-D Hartley transform over the last two axes (``H(H(a)) = n a``, ``n`` pixels)."""
    f = sf.fft2(a, axes=(-2, -1), workers=_WORKERS)
    return np.asarray(f.real - f.imag)


def kgrid(ny: int, nx: int, dx: float) -> tuple[np.ndarray, np.ndarray]:
    """Wave numbers (1/rad) along rows (``ky``, column vector) and columns (``kx``, row)."""
    ky = 2 * np.pi * np.fft.fftfreq(ny, dx)
    kx = 2 * np.pi * np.fft.fftfreq(nx, dx)
    return ky[:, None], kx[None, :]


def projection_stretch(ny: int, nx: int, dx: float) -> float:
    """Mean magnification ``sec^1.5 theta`` of a gnomonic patch of ``ny x nx`` pixels."""
    hy, hx = ny * dx / 2, nx * dx / 2
    yy = np.linspace(-hy, hy, 401)
    xx = np.linspace(-hx, hx, 401)
    theta = np.arctan(np.hypot(yy[:, None], xx[None, :]))
    return float(np.mean(np.cos(theta) ** -1.5))


def box(k: np.ndarray, dx: float, nsub: int) -> np.ndarray:
    """Transfer function of the mean of ``nsub`` point samples spaced ``dx / nsub``."""
    x = k * dx / nsub / 2
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.sin(nsub * x) / (nsub * np.sin(x))
    return np.asarray(np.where(np.abs(np.sin(x)) < 1e-12, np.cos(nsub * x) / np.cos(x), out))


@dataclass(frozen=True, slots=True)
class Instrument:
    """The reference spectrum and what the beam and the pixels do to it."""

    cl: np.ndarray  # C_ell (uK^2 sr) on ell = 0, 1, ..., len - 1
    window: np.ndarray  # pixel window of the source map on the same ell grid (or ones)
    beam_sigma: float  # rad
    dx: float  # rad, model pixel
    nsub: int  # point samples per model pixel side (box window)

    @classmethod
    def from_dl(
        cls,
        dl: np.ndarray,
        window: np.ndarray | None,
        beam_fwhm_arcmin: float,
        pixel_arcmin: float,
        nsub: int,
    ) -> Instrument:
        ell = np.arange(dl.size, dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            cl = np.where(ell >= 2, dl * 2 * np.pi / (ell * (ell + 1)), 0.0)
        w = np.ones_like(cl) if window is None else np.asarray(window, dtype=float)
        sigma = beam_fwhm_arcmin * ARCMIN / np.sqrt(8 * np.log(2))
        return cls(cl, w, float(sigma), pixel_arcmin * ARCMIN, int(nsub))

    def cl_of(self, ell: np.ndarray) -> np.ndarray:
        grid = np.arange(self.cl.size, dtype=float)
        return np.asarray(np.interp(ell, grid, self.cl, right=0.0))

    def beam_window(self, ell: np.ndarray) -> np.ndarray:
        lc = np.clip(ell, 0, self.window.size - 1)
        w = np.interp(lc, np.arange(self.window.size, dtype=float), self.window)
        return np.asarray(np.exp(-0.5 * lc**2 * self.beam_sigma**2) * w)


def folded_power(ny: int, nx: int, inst: Instrument, stretch: float, nalias: int = 3) -> np.ndarray:
    """Expected power of the beamed, pixel-averaged sky at every mode of an ``ny x nx`` grid,
    with the power aliased from beyond the Nyquist frequency (sum over ``k + m 2 pi / dx``)."""
    ky, kx = kgrid(ny, nx, inst.dx)
    big_k = 2 * np.pi / inst.dx
    tot = np.zeros((ny, nx))
    for mx in range(-nalias, nalias + 1):
        for my in range(-nalias, nalias + 1):
            qx, qy = kx + mx * big_k, ky + my * big_k
            ell = np.hypot(qx, qy) * stretch
            w = inst.beam_window(ell) * box(qx, inst.dx, inst.nsub) * box(qy, inst.dx, inst.nsub)
            tot += stretch**2 * inst.cl_of(ell) * w**2
    return tot


def dl_of(ell: np.ndarray, cl: np.ndarray) -> np.ndarray:
    return np.asarray(ell * (ell + 1) * cl / (2 * np.pi))


# ============================================================================== Gibbs sampler


class FieldSampler:
    """Gibbs sampler for (sky, bandpowers) of a masked map, several chains at once.

    A port of the notebook's sampler to rectangular rasters. ``S0`` is the Hartley variance
    of each torus mode at ``q = 0`` (the patch mean gets a prior sd ``mean_sd``).
    """

    def __init__(
        self,
        observed: np.ndarray,
        sigma: np.ndarray,
        inst: Instrument,
        edges: Sequence[float],
        *,
        pad: int,
        stretch: float,
        ell_dense: float,
        nalias: int,
        mean_sd: float,
    ) -> None:
        ny, nx = observed.shape
        self.ny, self.nx, self.pad = ny, nx, pad
        ty, tx = self.ty, self.tx = ny + 2 * pad, nx + 2 * pad
        ky, kx = kgrid(ty, tx, inst.dx)
        self.ell = np.hypot(ky, kx) * stretch
        self.S0 = folded_power(ty, tx, inst, stretch, nalias) / (inst.dx**2 * ty * tx)
        self.S0[0, 0] = mean_sd**2
        self.bin = np.digitize(self.ell, np.asarray(edges, dtype=float)) - 1
        self.nb = len(edges) - 1
        self.bin[self.bin >= self.nb] = -1
        self.bin[0, 0] = -1
        self.nk = np.bincount(self.bin[self.bin >= 0], minlength=self.nb)
        self.Ninv = np.zeros((ty, tx))
        self.Ninv[pad : pad + ny, pad : pad + nx] = np.where(observed, sigma**-2.0, 0.0)
        self.data_diag = float(self.Ninv.sum())
        # outside the bins (above the top edge) the power stays at the reference
        self.low = np.flatnonzero(self.ell.ravel() < ell_dense)
        self.AtNA_low = self._dense_block()
        sn = self.S0 * self.data_diag
        self.step = np.array(
            [
                np.sqrt(2 / max(self.nk[b], 1)) * (1 + 1 / np.median(sn[self.bin == b]))
                if self.nk[b]
                else 0.0
                for b in range(self.nb)
            ]
        )

    def _dense_block(self) -> np.ndarray:
        """``A^T N^-1 A`` on the large-scale modes (does not depend on ``q``)."""
        n_low = len(self.low)
        if n_low == 0:
            return np.zeros((0, 0))
        out = np.empty((n_low, n_low))
        for start in range(0, n_low, 128):
            idx = self.low[start : start + 128]
            e = np.zeros((len(idx), self.ty * self.tx))
            e[np.arange(len(idx)), idx] = 1.0
            block = self.AtNA(e.reshape(-1, self.ty, self.tx)).reshape(len(idx), -1)
            out[start : start + len(idx)] = block[:, self.low]
        return out

    def AtNA(self, a: np.ndarray) -> np.ndarray:
        return hartley(self.Ninv * hartley(a))

    def embed(self, y: np.ndarray) -> np.ndarray:
        out = np.zeros((*y.shape[:-2], self.ty, self.tx))
        out[..., self.pad : self.pad + self.ny, self.pad : self.pad + self.nx] = y
        return out

    def cut(self, a: np.ndarray) -> np.ndarray:
        return a[..., self.pad : self.pad + self.ny, self.pad : self.pad + self.nx]

    def prior_var(self, q: np.ndarray) -> np.ndarray:
        scale = np.exp(q)[:, np.maximum(self.bin, 0)]
        return np.asarray(self.S0[None] * np.where(self.bin < 0, 1.0, scale))

    def solve(
        self,
        q: np.ndarray,
        Y: np.ndarray,
        noise: tuple[np.ndarray, np.ndarray] | None = None,
        tol: float = 1e-5,
        maxit: int = 5000,
    ) -> tuple[np.ndarray, int]:
        """Wiener filter (``noise=None``) or constrained realisation by PCG.

        ``noise`` holds the two standard-normal fields ``(w1, w2)`` of shape ``(n, ty, tx)``.
        """
        S = self.prior_var(q)
        nc = len(S)
        b = hartley(self.Ninv * Y)
        if noise is not None:
            b = b + noise[0] / np.sqrt(S) + hartley(np.sqrt(self.Ninv) * noise[1])
        diag = 1 / (1 / S + self.data_diag)
        low = self.low
        inv_low = np.array(
            [np.linalg.inv(self.AtNA_low + np.diag(1 / S[c].ravel()[low])) for c in range(nc)]
        )

        def precondition(r: np.ndarray) -> np.ndarray:
            z = diag * r
            if len(low):
                z.reshape(nc, -1)[:, low] = np.matmul(inv_low, r.reshape(nc, -1)[:, low, None])[
                    ..., 0
                ]
            return z

        x = np.zeros_like(S)
        r = b.copy()
        z = precondition(r)
        p = z.copy()
        rz = np.sum(r * z, axis=(1, 2), keepdims=True)
        bnorm = np.maximum(np.linalg.norm(b.reshape(nc, -1), axis=1), 1e-300)
        it = 0
        while it < maxit:
            it += 1
            Ap = p / S + self.AtNA(p)
            alpha = rz / np.sum(p * Ap, axis=(1, 2), keepdims=True)
            x += alpha * p
            r -= alpha * Ap
            if np.max(np.linalg.norm(r.reshape(nc, -1), axis=1) / bnorm) < tol:
                break
            z = precondition(r)
            rz_new = np.sum(r * z, axis=(1, 2), keepdims=True)
            p = z + rz_new / rz * p
            rz = rz_new
        return x, it

    def draw_q(self, a: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """``exp(q_b)`` from its inverse-gamma conditional (flat prior on ``q_b``)."""
        keep = self.bin.ravel() >= 0
        r = (a**2 / self.S0[None]).reshape(len(a), -1)[:, keep]
        ssum = np.array(
            [np.bincount(self.bin.ravel()[keep], weights=rc, minlength=self.nb) for rc in r]
        )
        return np.asarray(np.log(ssum / 2 / rng.gamma(self.nk / 2, 1.0, size=ssum.shape)))

    def rescale(
        self,
        a: np.ndarray,
        q: np.ndarray,
        Y: np.ndarray,
        rng: np.random.Generator,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The non-centred move: rescale the modes of one bin with a new ``q_b``."""
        Ha = hartley(a)
        loglik = -0.5 * np.sum(self.Ninv * (Y - Ha) ** 2, axis=(1, 2))
        acc = np.zeros((len(a), self.nb))
        for b in rng.permutation(self.nb):
            inb = self.bin == b
            step = self.step[b] * rng.standard_normal(len(a))
            f = np.exp(step / 2) - 1
            dHa = f[:, None, None] * hartley(np.where(inb, a, 0.0))
            new = -0.5 * np.sum(self.Ninv * (Y - Ha - dHa) ** 2, axis=(1, 2))
            ok = np.log(rng.random(len(a))) < new - loglik
            a[ok] = a[ok] * np.where(inb, 1 + f[ok, None, None], 1.0)
            Ha[ok] += dHa[ok]
            q[ok, b] += step[ok]
            loglik[ok] = new[ok]
            acc[:, b] = ok
        return a, q, acc

    def run(
        self,
        y: np.ndarray,
        tune: int,
        draws: int,
        rng: np.random.Generator,
        q0: np.ndarray,
        *,
        tol: float = 1e-5,
        maxit: int = 5000,
        rescale: bool = True,
        on_iteration: Callable[[int, int], None] | None = None,
    ) -> GibbsResult:
        """``tune`` burn-in and ``draws`` kept iterations for ``len(q0)`` chains."""
        Y = np.broadcast_to(self.embed(y), (len(q0), self.ty, self.tx))
        q = q0.copy()
        nc = len(q0)
        qs = np.empty((draws, nc, self.nb))
        cg = np.empty(draws, dtype=np.int64)
        acc = np.zeros((draws, nc, self.nb))
        s1 = np.zeros((nc, self.ny, self.nx))
        s2 = np.zeros((nc, self.ny, self.nx))
        max_cg = 0
        for i in range(tune + draws):
            w = (rng.standard_normal(Y.shape), rng.standard_normal(Y.shape))
            a, it = self.solve(q, Y, w, tol=tol, maxit=maxit)
            max_cg = max(max_cg, it)
            q = self.draw_q(a, rng)
            if rescale:
                a, q, ok = self.rescale(a, q, Y, rng)
            else:
                ok = np.zeros((nc, self.nb))
            j = i - tune
            if j >= 0:
                qs[j], cg[j], acc[j] = q, it, ok
                t = self.cut(hartley(a))
                s1 += t
                s2 += t**2
            if on_iteration is not None:
                on_iteration(i + 1, tune + draws)
        return GibbsResult(qs, cg, acc, s1 / max(draws, 1), s2 / max(draws, 1), max_cg)


@dataclass(frozen=True, slots=True)
class GibbsResult:
    q: np.ndarray  # (draw, chain, bin)
    cg: np.ndarray  # (draw,) CG iterations of the batched solve
    acc: np.ndarray  # (draw, chain, bin) rescaling move accepted
    mean: np.ndarray  # (chain, row, col) mean of the sky over the kept draws
    sq: np.ndarray  # (chain, row, col) mean of its square
    max_cg: int


def amplitude_tilt(
    q: np.ndarray,
    ell: np.ndarray,
    *,
    pivot: float,
    max_ell: float,
    prior_sd: float,
) -> tuple[np.ndarray, np.ndarray]:
    """The exact posterior of ``(log A, tilt)`` in ``q_b ~ N(log A + tilt log(ell_b / pivot),
    var_b)`` with independent ``N(0, prior_sd^2)`` priors (the notebook's part H model).

    ``q`` holds draws ``(..., bin)``; its per-bin mean and variance are the data. Bins with
    a centre at or above ``max_ell`` are left out. Returns ``(mean, covariance)``.
    """
    flat = q.reshape(-1, q.shape[-1])
    sel = ell < max_ell
    qm = flat[:, sel].mean(0)
    var = flat[:, sel].var(0)
    x = np.stack([np.ones(int(sel.sum())), np.log(ell[sel] / pivot)], axis=1)
    w = 1 / np.maximum(var, 1e-12)
    prec = np.eye(2) / prior_sd**2 + (x * w[:, None]).T @ x
    cov = np.linalg.inv(prec)
    return cov @ (x.T @ (w * qm)), cov


# ============================================================================== settings


class CMBFieldSettings(ModelSettings):
    """Settings of ``sci.cmb_field``: the instrument, the bins and the Gibbs sampler."""

    pixel_arcmin: float = Field(
        7.5, gt=0, le=600, description="Side of a map pixel (arcmin) (x-ui-level: basic)"
    )
    beam_fwhm_arcmin: float = Field(
        5.0, ge=0, le=600, description="FWHM of the Gaussian beam of the map (arcmin)"
    )
    pixel_subsamples: int = Field(
        8,
        ge=1,
        le=64,
        description="Point samples per pixel side that the map pixels average (box window; "
        "1 = none)",
    )
    pixel_window: bool = Field(
        False,
        description="The data has a pixel_window(multipole) variable (for example the "
        "HEALPix window of the source map)",
    )
    bin_edges: list[float] = Field(
        default_factory=lambda: list(NOTEBOOK_EDGES),
        min_length=2,
        max_length=101,
        description="Multipole edges of the bandpower bins (x-ui-level: basic)",
    )
    projection: Literal["gnomonic", "flat"] = Field(
        "gnomonic",
        description="gnomonic: correct the multipoles for the mean stretch of a tangent-plane "
        "projection; flat: no correction (x-ui-level: basic)",
    )
    border: int = Field(
        8, ge=0, le=128, description="Unobserved pixels around the raster on the periodic torus"
    )
    ell_dense: float = Field(
        300.0, ge=0, description="Modes below this multipole get the dense CG preconditioner"
    )
    n_alias: int = Field(3, ge=0, le=6, description="Aliased replicas folded into the prior power")
    mean_prior_sd: float = Field(100.0, gt=0, description="Prior sd of the patch mean (uK)")
    cg_tol: float = Field(1e-5, gt=0, le=0.1, description="Relative residual that stops CG")
    cg_max_iter: int = Field(5000, ge=10, le=100_000, description="Most CG iterations per solve")
    rescale: bool = Field(True, description="Use the non-centred rescaling move")
    q_init_sd: float = Field(0.3, ge=0, le=3, description="Sd of the dispersed chain starts")
    tilt_pivot: float = Field(500.0, gt=0, description="Pivot multipole of the tilt")
    tilt_max_ell: float = Field(
        1450.0, gt=0, description="Bins with a centre at or above this are left out of the tilt"
    )
    tilt_prior_sd: float = Field(0.5, gt=0, description="Prior sd of log amplitude and tilt")
    field_draws: int = Field(
        40, ge=1, le=1000, description="Sky maps drawn per score (over all chains)"
    )

    @field_validator("bin_edges")
    @classmethod
    def _increasing(cls, v: list[float]) -> list[float]:
        if any(b <= a for a, b in pairwise(v)) or v[0] < 0:
            raise ValueError("bin_edges must be non-negative and strictly increasing")
        return v


_TEMPLATE: dict[str, Any] = {
    "params": {"pixel_window": {"type": "boolean", "default": False}},
    "template": {
        "dims": {
            ROW_DIM: {"kind": "integer", "policy": "fixed", "description": "Map rows (bottom up)"},
            COL_DIM: {"kind": "integer", "policy": "fixed", "description": "Map columns"},
            MULTIPOLE_DIM: {
                "kind": "integer",
                "policy": "fixed",
                "description": "Multipole ell = 0, 1, 2, ... of the reference spectrum",
            },
        },
        "vars": {
            TARGET: {
                "dims": [ROW_DIM, COL_DIM],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
                "description": "CMB temperature of each pixel (uK); NaN where masked",
            },
            NOISE: {
                "dims": [ROW_DIM, COL_DIM],
                "dtype": "float64",
                "role": "weight",
                "description": "Noise sd of each pixel (uK)",
            },
            REFERENCE: {
                "dims": [MULTIPOLE_DIM],
                "dtype": "float64",
                "role": "constant",
                "description": "Reference spectrum D_ell = ell (ell + 1) C_ell / 2 pi (uK^2)",
            },
        },
    },
    "cases": [
        {
            "when": {"pixel_window": True},
            "merge": {
                "vars": {
                    PIXEL_WINDOW: {
                        "dims": [MULTIPOLE_DIM],
                        "dtype": "float64",
                        "role": "constant",
                        "description": "Pixel window of the source map at each multipole",
                    }
                }
            },
        }
    ],
}

_GRID = {"y": ROW_DIM, "x": COL_DIM, "origin": "lower", "colormap": "diverging"}


# ============================================================================== the model


class CMBFieldModel(CustomModel[CMBFieldSettings]):
    """Field-level CMB inference with a Wandelt-Eriksen Gibbs sampler (E79)."""

    id = "sci.cmb_field"
    version = "1.0.0"
    Settings = CMBFieldSettings
    description = (
        "Field-level inference of a masked CMB temperature map: every pixel of the sky on a "
        "periodic flat patch is sampled jointly with the bandpowers by a Wandelt-Eriksen Gibbs "
        "sampler (constrained realisations by preconditioned conjugate gradients, an "
        "inverse-gamma bandpower step and a rescaling move). Gives the Wiener-filtered map, "
        "sky draws behind the mask, a per-pixel sd and the bandpowers."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate", "simulate"}),
        loo_valid=False,
        loo_invalid_reason="every pixel depends on one latent sky field with tens of thousands "
        "of parameters, so pointwise leave-one-out is not defined; compare variants by their "
        "bandpowers, amplitude and tilt",
        identified_quantities=["q", "bandpower", "log_amplitude", "tilt"],
        deterministic_given_seed=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "sky": {
            "dims": [ROW_DIM, COL_DIM],
            "units": "uK",
            "description": "Posterior draws of the beamed sky (constrained realisations; "
            "unconstrained skies with the posterior spectrum for simulate)",
            "grid": _GRID,
        },
        "temperature_hat": {
            "dims": [ROW_DIM, COL_DIM],
            "units_of": TARGET,
            "description": "The sky draw (expected), plus noise with noise_sd (predictive)",
            "grid": _GRID,
        },
        "bandpower": {
            "dims": [BIN_DIM],
            "units": "uK^2",
            "description": "Bandpower D_ell of each bin",
        },
        "bandpower_ratio": {
            "dims": [BIN_DIM],
            "units": "1",
            "description": "Bandpower over the reference bandpower, exp(q)",
        },
        "log_amplitude": {
            "dims": [],
            "units": "1",
            "description": "Log amplitude of the bandpowers relative to the reference",
        },
        "tilt": {
            "dims": [],
            "units": "1",
            "description": "Tilt of the bandpowers relative to the reference, at tilt_pivot",
        },
    }
    derived: ClassVar[list[dict[str, Any]]] = [
        {
            "name": SKY_MOMENTS,
            "dims": ["stat", ROW_DIM, COL_DIM],
            "units": "uK",
            "cache": "fit",
            "description": "Posterior mean (the Wiener filter averaged over the spectrum) and "
            "sd of each pixel over every kept Gibbs draw",
        }
    ]
    plots = (
        "core.convergence",
        "sci.power_spectrum",
        "sci.field_map",
        "sci.image_residuals",
        "sci.ppc_image_grid",
    )
    # 128 x 128 map on a 144 x 144 torus: about 30 s for 4 chains x 80 iterations
    resources = ResourceHint(est_fit_seconds=60)

    # ================================================================== fit
    def fit(
        self, ds: xr.Dataset, s: CMBFieldSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        data = _Data.from_dataset(ds, s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        if cfg.engine not in (None, "gibbs"):
            ctx.info(f"engine {cfg.engine!r} ignored: sci.cmb_field always uses its Gibbs sampler")
        tune = 20 if cfg.tune is None else int(cfg.tune)
        draws = 80 if cfg.draws is None else int(cfg.draws)
        chains = 4 if cfg.chains is None else int(cfg.chains)
        sampler = data.sampler(s)
        rng = np.random.default_rng(seed)
        q0 = rng.normal(0.0, s.q_init_sd, (chains, sampler.nb))

        def tick(i: int, n: int) -> None:
            ctx.check_cancelled()
            if i % max(1, n // 20) == 0:
                ctx.progress(0.95 * i / n, f"Gibbs iteration {i} of {n}")

        res = sampler.run(
            data.y,
            tune,
            draws,
            rng,
            q0,
            tol=s.cg_tol,
            maxit=s.cg_max_iter,
            rescale=s.rescale,
            on_iteration=tick,
        )
        q = res.q.transpose(1, 0, 2)  # (chain, draw, bin)
        ell_b = data.bin_centres
        ref_b = data.reference_bandpower(s)
        mean, cov = amplitude_tilt(
            q, ell_b, pivot=s.tilt_pivot, max_ell=s.tilt_max_ell, prior_sd=s.tilt_prior_sd
        )
        z = _rng(seed, "amplitude_tilt").standard_normal((chains, draws, 2))
        at = mean + z @ np.linalg.cholesky(cov).T
        bin_coords = data.bin_coords()
        cd = {"chain": np.arange(chains), "draw": np.arange(draws)}
        posterior = xr.Dataset(
            {
                "q": (("chain", "draw", BIN_DIM), q),
                "bandpower": (("chain", "draw", BIN_DIM), np.exp(q) * ref_b),
                "bandpower_ratio": (("chain", "draw", BIN_DIM), np.exp(q)),
                "log_amplitude": (("chain", "draw"), at[..., 0]),
                "tilt": (("chain", "draw"), at[..., 1]),
            },
            coords={**cd, **bin_coords},
        )
        stats = xr.Dataset(
            {
                "cg_iterations": (
                    ("chain", "draw"),
                    np.broadcast_to(res.cg[None, :], (chains, draws)).copy(),
                ),
                "rescale_acceptance": (("chain", "draw"), res.acc.mean(-1).T),
            },
            coords=cd,
        )
        sky_mean = res.mean.mean(0)
        sky_sd = np.sqrt(np.maximum(res.sq.mean(0) - sky_mean**2, 0.0))
        moments = xr.Dataset(
            {
                "sky_summary": (
                    ("stat", ROW_DIM, COL_DIM),
                    np.stack([sky_mean, sky_sd]),
                    {"units": "uK"},
                )
            },
            coords={"stat": ["mean", "sd"], **data.raster_coords},
        )
        groups: dict[str, xr.Dataset] = {
            "posterior": _clean(posterior),
            "sample_stats": _clean(stats),
            "observed_data": _clean(xr.Dataset({TARGET: data.target_da})),
            "constant_data": _clean(data.constant_dataset(s, ref_b)),
        }
        ctx.progress(1.0, "posterior ready")
        acc_bin = res.acc.mean((0, 1))
        learned: dict[str, Any] = {
            "engine": "gibbs",
            "tune": tune,
            "draws": draws,
            "chains": chains,
            "torus": [sampler.ty, sampler.tx],
            "sky_parameters": int(sampler.ty * sampler.tx),
            "bins": int(sampler.nb),
            "modes_per_bin": [int(n) for n in sampler.nk],
            "dense_modes": len(sampler.low),
            "stretch": float(data.stretch(s)),
            "cg_median": float(np.median(res.cg)) if res.cg.size else 0.0,
            "cg_max": int(res.max_cg),
            "rescale_acceptance": [round(float(v), 4) for v in acc_bin],
            "masked_fraction": float(1 - data.observed.mean()),
            "log_amplitude_mean": float(mean[0]),
            "log_amplitude_sd": float(np.sqrt(cov[0, 0])),
            "tilt_mean": float(mean[1]),
            "tilt_sd": float(np.sqrt(cov[1, 1])),
        }
        if cfg.keep.log_likelihood:
            learned["notes"] = [
                "log_likelihood not stored: the pixels share one latent sky field, so "
                "pointwise LOO is not valid"
            ]
        return FitOutput(groups=groups, learned=learned, derived={SKY_MOMENTS: moments})

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        out = convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=["q"])
        s = self.parse_settings(fitted.settings)
        cg_max = fitted.learned.get("cg_max")
        if isinstance(cg_max, int) and cg_max >= s.cg_max_iter:
            out.append(
                Diagnostic(
                    code="CG_NOT_CONVERGED",
                    severity="warning",
                    metric="cg_iterations",
                    value=float(cg_max),
                    threshold=float(s.cg_max_iter),
                    message_expert=f"a constrained realisation stopped at cg_max_iter="
                    f"{s.cg_max_iter} before the residual reached cg_tol={s.cg_tol}; unconverged "
                    "directions inside the mask bias the large-scale bandpowers low",
                    message_plain="The solver for the sky maps did not finish. The large-scale "
                    "values can be too low.",
                    suggested_action="Increase cg_max_iter or ell_dense.",
                )
            )
        return out

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        data = _Data.from_fitted(fitted, s)
        post = fitted.posterior
        chains, draws = _field_draw_index(post, s.field_draws)
        q = np.asarray(post["q"].transpose("chain", "draw", BIN_DIM).values)
        sel = q[np.ix_(chains, draws)]  # (c, d, bin)
        nc, nd = len(chains), len(draws)
        flat_q = sel.reshape(nc * nd, -1)
        keys = [
            (int(post["chain"].values[c]), int(post["draw"].values[d]))
            for c in chains.tolist()
            for d in draws.tolist()
        ]
        simulate = ctx.mode == "simulate"
        sampler = data.sampler(s)
        skies = np.empty((nc * nd, data.ny, data.nx), dtype=np.float64)
        Y = sampler.embed(data.y)
        for start in range(0, len(keys), 8):
            ctx.check_cancelled()
            part = keys[start : start + 8]
            gens = [_rng(ctx.seed, f"sky:{c}:{d}") for c, d in part]
            w1 = np.stack([g.standard_normal((sampler.ty, sampler.tx)) for g in gens])
            w2 = np.stack([g.standard_normal((sampler.ty, sampler.tx)) for g in gens])
            qq = flat_q[start : start + len(part)]
            if simulate:
                a = np.sqrt(sampler.prior_var(qq)) * w1
            else:
                yy = np.broadcast_to(Y, (len(part), sampler.ty, sampler.tx))
                a, _ = sampler.solve(qq, yy, (w1, w2), tol=s.cg_tol, maxit=s.cg_max_iter)
            skies[start : start + len(part)] = sampler.cut(hartley(a))
        noisy = simulate or ctx.kind == "predictive"
        temp = skies
        if noisy:
            eps = np.stack(
                [
                    _rng(ctx.seed, f"noise:{c}:{d}").standard_normal((data.ny, data.nx))
                    for c, d in keys
                ]
            )
            temp = skies + data.sigma[None] * eps
        shape4 = (nc, nd, data.ny, data.nx)
        dims4 = ("chain", "draw", ROW_DIM, COL_DIM)
        dims_b = ("chain", "draw", BIN_DIM)
        pv = {n: np.asarray(post[n].values)[np.ix_(chains, draws)] for n in POSTERIOR_VARS}
        pred = xr.Dataset(
            {
                "sky": xr.DataArray(
                    skies.reshape(shape4).astype(np.float32),
                    dims=dims4,
                    attrs={"kind": "posterior", "units": "uK"},
                ),
                "temperature_hat": xr.DataArray(
                    temp.reshape(shape4).astype(np.float32),
                    dims=dims4,
                    attrs={
                        "kind": "predictive" if noisy else "expected",
                        "noise": "seeded_white" if noisy else "none",
                    },
                ),
                "bandpower": xr.DataArray(
                    pv["bandpower"], dims=dims_b, attrs={"kind": "posterior", "units": "uK^2"}
                ),
                "bandpower_ratio": xr.DataArray(
                    pv["bandpower_ratio"], dims=dims_b, attrs={"kind": "posterior", "units": "1"}
                ),
                "log_amplitude": xr.DataArray(
                    pv["log_amplitude"], dims=("chain", "draw"), attrs={"kind": "posterior"}
                ),
                "tilt": xr.DataArray(
                    pv["tilt"], dims=("chain", "draw"), attrs={"kind": "posterior"}
                ),
            },
            coords={
                "chain": np.asarray(post["chain"].values)[chains],
                "draw": np.asarray(post["draw"].values)[draws],
                **data.raster_coords,
                **{k: (BIN_DIM, np.asarray(post[k].values)) for k in post[BIN_DIM].coords},
            },
        )
        pred = pred.sel({d: np.asarray(ds[d].values) for d in (ROW_DIM, COL_DIM) if d in ds.coords})
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ============================================================================== helpers


@dataclass(slots=True)
class _Data:
    """The map, its mask and noise, and the reference spectrum, as the model needs them."""

    target_da: xr.DataArray  # temperature(row, col), NaN where masked
    y: np.ndarray  # temperature with 0 where masked
    observed: np.ndarray  # bool (row, col)
    sigma: np.ndarray  # (row, col)
    reference: np.ndarray  # D_ell on ell = 0..L
    window: np.ndarray | None
    edges: np.ndarray  # bandpower bin edges

    @property
    def ny(self) -> int:
        return int(self.y.shape[0])

    @property
    def nx(self) -> int:
        return int(self.y.shape[1])

    @property
    def raster_coords(self) -> dict[str, np.ndarray]:
        return {d: np.asarray(self.target_da[d].values) for d in (ROW_DIM, COL_DIM)}

    def stretch(self, s: CMBFieldSettings) -> float:
        if s.projection == "flat":
            return 1.0
        return projection_stretch(self.ny, self.nx, s.pixel_arcmin * ARCMIN)

    def instrument(self, s: CMBFieldSettings) -> Instrument:
        return Instrument.from_dl(
            self.reference, self.window, s.beam_fwhm_arcmin, s.pixel_arcmin, s.pixel_subsamples
        )

    def sampler(self, s: CMBFieldSettings) -> FieldSampler:
        ty, tx = self.ny + 2 * s.border, self.nx + 2 * s.border
        if ty * tx > MAX_TORUS:
            raise MapiError(
                f"the torus is {ty} x {tx} pixels; at most {MAX_TORUS} are supported (cut a "
                "smaller patch or use coarser pixels)",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/border",
            )
        self._check_dense(s)
        smp = FieldSampler(
            self.observed,
            self.sigma,
            self.instrument(s),
            s.bin_edges,
            pad=s.border,
            stretch=self.stretch(s),
            ell_dense=s.ell_dense,
            nalias=s.n_alias,
            mean_sd=s.mean_prior_sd,
        )
        empty = [b for b in range(smp.nb) if smp.nk[b] == 0]
        if empty:
            lo, hi = s.bin_edges[empty[0]], s.bin_edges[empty[0] + 1]
            raise MapiError(
                f"bandpower bin {empty[0]} ({lo:g} to {hi:g}) has no Fourier mode on the "
                f"{ty} x {tx} torus; widen the bins or use a larger patch",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/bin_edges",
            )
        return smp

    def _check_dense(self, s: CMBFieldSettings) -> None:
        ty, tx = self.ny + 2 * s.border, self.nx + 2 * s.border
        ky, kx = kgrid(ty, tx, s.pixel_arcmin * ARCMIN)
        n = int((np.hypot(ky, kx) * self.stretch(s) < s.ell_dense).sum())
        if n > MAX_DENSE:
            raise MapiError(
                f"ell_dense={s.ell_dense:g} puts {n} modes in the dense preconditioner; at most "
                f"{MAX_DENSE} are supported",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/ell_dense",
            )

    @property
    def bin_centres(self) -> np.ndarray:
        return np.asarray(0.5 * (self.edges[1:] + self.edges[:-1]))

    def reference_bandpower(self, s: CMBFieldSettings) -> np.ndarray:
        """The reference ``D_ell`` averaged over the modes of the raster in each bin."""
        inst = self.instrument(s)
        st = self.stretch(s)
        ky, kx = kgrid(self.ny, self.nx, inst.dx)
        ell = np.hypot(ky, kx) * st
        b = np.digitize(ell, self.edges) - 1
        dl = dl_of(ell, inst.cl_of(ell))
        nb = len(self.edges) - 1
        out = np.empty(nb)
        for i in range(nb):
            sel = b == i
            out[i] = dl[sel].mean() if sel.any() else np.nan
        return out

    def bin_coords(self) -> dict[str, Any]:
        e = self.edges
        return {
            BIN_DIM: np.arange(len(e) - 1),
            "ell": (BIN_DIM, 0.5 * (e[1:] + e[:-1])),
            "ell_low": (BIN_DIM, e[:-1]),
            "ell_high": (BIN_DIM, e[1:]),
        }

    def constant_dataset(self, s: CMBFieldSettings, ref_b: np.ndarray) -> xr.Dataset:
        mp = np.arange(self.reference.size)
        out = xr.Dataset(
            {
                NOISE: ((ROW_DIM, COL_DIM), self.sigma),
                REFERENCE: ((MULTIPOLE_DIM,), self.reference),
                "reference_bandpower": ((BIN_DIM,), ref_b),
            },
            coords={**self.raster_coords, MULTIPOLE_DIM: mp, **self.bin_coords()},
        )
        if self.window is not None:
            out[PIXEL_WINDOW] = ((MULTIPOLE_DIM,), self.window)
        return out

    @classmethod
    def build(
        cls,
        target: xr.DataArray,
        sigma: xr.DataArray,
        reference: xr.DataArray,
        window: xr.DataArray | None,
        s: CMBFieldSettings,
    ) -> _Data:
        target = target.sortby([ROW_DIM, COL_DIM]).transpose(ROW_DIM, COL_DIM)
        values = np.asarray(target.values, dtype=np.float64)
        ny, nx = values.shape
        if ny < 8 or nx < 8:
            raise MapiError(
                f"the map is {ny} x {nx} pixels; the model needs at least 8 x 8",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        observed = np.isfinite(values)
        if not observed.any():
            raise MapiError(
                f"every pixel of {TARGET!r} is masked (NaN); nothing to fit",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        sig = np.asarray(
            sigma.sortby([ROW_DIM, COL_DIM]).transpose(ROW_DIM, COL_DIM).values, dtype=np.float64
        )
        bad = observed & ~(np.isfinite(sig) & (sig > 0))
        if bad.any():
            raise MapiError(
                f"{NOISE!r} must be positive and finite at every unmasked pixel ({int(bad.sum())} "
                "are not)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{NOISE}",
            )
        sig = np.where(np.isfinite(sig) & (sig > 0), sig, 1.0)
        ref = reference.sortby(MULTIPOLE_DIM)
        mp = np.asarray(ref[MULTIPOLE_DIM].values)
        if mp.size < 3 or not np.array_equal(mp, np.arange(mp.size)):
            raise MapiError(
                f"dim {MULTIPOLE_DIM!r} must hold 0, 1, 2, ... (at least 3 multipoles)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{REFERENCE}",
            )
        dl = np.asarray(ref.values, dtype=np.float64)
        if not np.isfinite(dl).all() or (dl < 0).any() or not (dl > 0).any():
            raise MapiError(
                f"{REFERENCE!r} must be finite, non-negative and not all zero",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{REFERENCE}",
            )
        win = None
        if window is not None:
            win = np.asarray(window.sortby(MULTIPOLE_DIM).values, dtype=np.float64)
            if not np.isfinite(win).all():
                raise MapiError(
                    f"{PIXEL_WINDOW!r} has non-finite values",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{PIXEL_WINDOW}",
                )
        return cls(
            target.copy(data=np.where(observed, values, np.nan)),
            np.where(observed, values, 0.0),
            observed,
            sig,
            dl,
            win,
            np.asarray(s.bin_edges, dtype=float),
        )

    @classmethod
    def from_dataset(cls, ds: xr.Dataset, s: CMBFieldSettings) -> _Data:
        for d in (ROW_DIM, COL_DIM, MULTIPOLE_DIM):
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        for v in (TARGET, NOISE, REFERENCE):
            if v not in ds:
                raise MapiError(f"the data has no variable {v!r}", code=ErrorCode.SCHEMA_INVALID)
        window = None
        if s.pixel_window:
            if PIXEL_WINDOW not in ds:
                raise MapiError(
                    f"pixel_window is true but the data has no {PIXEL_WINDOW!r} variable",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{PIXEL_WINDOW}",
                )
            window = ds[PIXEL_WINDOW]
        return cls.build(ds[TARGET], ds[NOISE], ds[REFERENCE], window, s)

    @classmethod
    def from_fitted(cls, fitted: Fitted, s: CMBFieldSettings) -> _Data:
        obs = fitted.groups.get("observed_data")
        const = fitted.groups.get("constant_data")
        if obs is None or TARGET not in obs or const is None or NOISE not in const:
            raise MapiError(
                "the fitted model has no stored map; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        window = const.get(PIXEL_WINDOW)
        return cls.build(obs[TARGET], const[NOISE], const[REFERENCE], window, s)


def _field_draw_index(post: xr.Dataset, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Every chain and ``n // chains`` evenly spaced draw positions (at least one)."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    per = min(draws, max(1, n // chains))
    idx = np.unique(np.linspace(0, draws - 1, per).round().astype(np.int64))
    return np.arange(chains), idx


def _rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


def _clean(ds: xr.Dataset) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {"inference_library": "mapi_plugin_science.cmb", "engine": "gibbs"}
    return out
