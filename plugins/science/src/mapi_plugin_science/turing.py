"""``sci.turing_pattern``: Turing pattern kinetics from images of a growing pattern (E61 A).

Canonical inputs (the dataset names equal the canonical names; the specs need no
``data.bind``)::

    dims  t: float, policy extend, max_extension = settings.max_extension  (time since the
             start, in the time unit of the reaction rates)
          y, x: integer, policy fixed  (pixel rows and columns of a square periodic raster)
    vars  activator(t, y, x): target, float64 - the activator concentration in each pixel

**Kinetics.** The Schnakenberg activator-substrate system in dimensionless form (lengths in
units of ``sqrt(D_u / k)``, so the activator diffuses with ``D_u = 1``)::

    du/dt = g (a - u + u^2 v) + lap u,     dv/dt = g (b - u^2 v) + d lap v

with the supply ``s = a + b``, the activator share ``rho = a / s``, the diffusion ratio
``d = D_v / D_u`` and the reaction scale ``g``. The uniform state is ``u* = s``,
``v* = b / s^2``. A Fourier mode with ``q = |k|^2`` grows as ``exp(A(q) t)`` with
``A = g J - q diag(1, d)``; the largest real eigenvalue of ``A(q)`` is the dispersion
relation ``growth(k)``. The Turing conditions (stable without diffusion, unstable for some
``k > 0``) hold when ``1 - 2 rho < s^2`` and ``d > d_c = s^2 ((1 + sqrt(2 - 2 rho)) /
(1 - 2 rho))^2``.

**Likelihood (exact for the linearised dynamics).** The pattern starts at ``t = 0`` from the
uniform state plus white noise of sd ``s0`` in both species, and each image has white camera
noise of sd ``sigma``. While the perturbation is small every Fourier mode evolves on its
own, so the vector of the activator's mode coefficients at the image times is complex
Gaussian with ``C(q) = s0^2 U U^T + sigma^2 I`` (``U[i]`` = first row of
``exp(A(q) t_i)``) and modes are independent. The FFT of the images is computed once in
NumPy; modes are grouped by their integer ``|k|^2`` (in units of ``(2 pi / L)^2``) into
summed outer products, and a Woodbury identity leaves only 2 x 2 algebra per group in the
graph (a ``pm.Potential``). Only modes with ``q < k2_max`` enter (above it every mode has
decayed below the camera noise by the first image). The ``k = 0`` column and the Nyquist
column of the half-plane are left out, so every kept mode has no conjugate partner.

Priors (the notebook's): ``s ~ LogNormal(0, 0.5)``, ``rho ~ Beta(2, 5)``,
``d ~ LogNormal(log 10, 1)``, ``g ~ LogNormal(0, 0.7)``, ``s0`` and ``sigma`` ~
``LogNormal(log 1e-3, 1.5)``. NUTS (pymc, ``target_accept`` 0.95 unless the fit sets it).

**Pointwise log-likelihood.** With ``fit.keep.log_likelihood`` the bundle stores the exact
log-likelihood of each kept Fourier mode (``log_likelihood/activator(chain, draw, mode)``);
modes are independent given the parameters, so PSIS-LOO over modes is valid.

**Outputs** (representation ``draws``; a subset of ``field_draws`` evenly spaced draws):

* ``pattern(t, y, x)`` - in ``predict``/``evaluate``: an exact draw of the
  noiseless activator field at the requested times given the fitted images (Gaussian
  conditioning, mode by mode); ``kind: predictive`` adds the camera noise. In ``simulate``:
  a new pattern grown by the nonlinear semi-implicit spectral solver from fresh seeded
  noise, plus camera noise (``simulate_draws`` runs);
* ``settled(y, x)`` - ``simulate`` only: the noiseless pattern of each run at
  ``settle_time`` (spots or stripes);
* ``growth(wavenumber)`` - the dispersion relation ``Re lambda(k)``;
* ``p_turing`` - 1 when the draw meets the Turing conditions, else 0 (its mean is the
  probability);
* ``wavelength`` - ``2 pi / k_m`` of the fastest-growing mode;
* ``turing_space(b_supply, a_supply)`` - 1 where the draw's diffusion ratio makes the
  supply rates ``(a, b)`` Turing unstable (the mean is a map of P(Turing)).

The posterior also holds ``a``, ``b``, ``wavelength``, ``max_growth`` and ``p_turing`` for
every draw. Scores ignore any activator values in the payload: predictions condition on
the fitted images only.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

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
from scipy.optimize import minimize

from mapi_plugin_science._common import (
    clean_attrs as _clean,
)
from mapi_plugin_science._common import (
    draw_index as _draw_index,
)
from mapi_plugin_science._common import (
    quiet as _quiet,
)
from mapi_plugin_science._common import (
    rng as _rng,
)

__all__ = [
    "A_DIM",
    "B_DIM",
    "MODE_DIM",
    "PARAMETERS",
    "Q_GRID",
    "TARGET",
    "TIME_DIM",
    "WAVENUMBER_DIM",
    "X_DIM",
    "Y_DIM",
    "ModeStatistics",
    "SpectralGrid",
    "TuringPatternModel",
    "TuringPatternSettings",
    "conditional_pattern",
    "d_critical",
    "expm2",
    "fastest_mode",
    "growth_rate",
    "initial_state",
    "is_turing",
    "jacobian",
    "mode_statistics",
    "mode_terms",
    "simulate_pattern",
]

TIME_DIM = "t"
Y_DIM = "y"
X_DIM = "x"
TARGET = "activator"
WAVENUMBER_DIM = "wavenumber"
A_DIM = "a_supply"
B_DIM = "b_supply"
MODE_DIM = "mode"
PARAMETERS = ("s", "rho", "d", "g", "s0", "sigma")
"""The sampled parameters (supply, activator share, diffusion ratio, reaction scale, initial
noise sd, camera noise sd)."""
FIELD_DIMS = (TIME_DIM, Y_DIM, X_DIM)

Q_GRID = np.linspace(1e-4, 3.0, 600)
"""The ``q = k^2`` grid on which the fastest-growing mode is found (as in the notebook)."""


# ============================================================================== kinetics


def jacobian(s: Any, rho: Any) -> tuple[Any, Any, Any, Any]:
    """Schnakenberg Jacobian ``(f_u, f_v, g_u, g_v)`` at the uniform steady state."""
    return 1 - 2 * rho, s**2, -2 * (1 - rho), -(s**2)


def growth_rate(q: Any, s: Any, rho: Any, d: Any, g: Any) -> Any:
    """Largest real part of the eigenvalues of ``g J - q diag(1, d)`` (broadcasts)."""
    fu, fv, gu, gv = jacobian(s, rho)
    a11, a22 = g * fu - q, g * gv - d * q
    tr, det = a11 + a22, a11 * a22 - g**2 * fv * gu
    disc = tr**2 / 4 - det
    return tr / 2 + np.sqrt(np.maximum(disc, 0))


def d_critical(s: Any, rho: Any) -> Any:
    """Smallest diffusion ratio for a Turing instability (inf without self-activation)."""
    fu = 1 - 2 * np.asarray(rho, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        dc = np.asarray(s) ** 2 * ((1 + np.sqrt(2 - 2 * np.asarray(rho))) / fu) ** 2
    return np.where(fu > 0, dc, np.inf)


def is_turing(s: Any, rho: Any, d: Any) -> Any:
    """True where the Turing conditions hold (stable without diffusion, ``d > d_c``)."""
    return (1 - 2 * np.asarray(rho) < np.asarray(s) ** 2) & (np.asarray(d) > d_critical(s, rho))


def fastest_mode(
    s: Any, rho: Any, d: Any, g: Any, chunk: int = 5000
) -> tuple[np.ndarray, np.ndarray]:
    """Wavenumber ``k_m`` of the fastest-growing mode and its growth rate, per parameter set
    (the maximum over :data:`Q_GRID`)."""
    arrs = [np.atleast_1d(np.asarray(v, dtype=float)).ravel() for v in (s, rho, d, g)]
    n = max(a.size for a in arrs)
    s_, rho_, d_, g_ = (np.broadcast_to(a, (n,)) for a in arrs)
    km, lm = np.empty(n), np.empty(n)
    for i in range(0, n, chunk):
        sl = slice(i, i + chunk)
        lam = growth_rate(Q_GRID[None, :], s_[sl, None], rho_[sl, None], d_[sl, None], g_[sl, None])
        j = lam.argmax(axis=1)
        km[sl] = np.sqrt(Q_GRID[j])
        lm[sl] = lam.max(axis=1)
    return km, lm


def expm2(a11: Any, a12: Any, a21: Any, a22: Any, t: float, *, lib: Any = np) -> tuple[Any, ...]:
    """``exp(A t)`` for a batch of 2 x 2 matrices, overflow-safe and NaN-free in both branches
    (NumPy, or pytensor with ``lib=pt``)."""
    where = pt.switch if lib is pt else np.where
    tau = (a11 + a22) / 2
    q = tau**2 - (a11 * a22 - a12 * a21)
    rq = lib.sqrt(lib.maximum(q, 1e-12))
    wq = lib.sqrt(lib.maximum(-q, 1e-12))
    ep, em = lib.exp((tau + rq) * t), lib.exp((tau - rq) * t)
    c = where(q > 0, (ep + em) / 2, lib.exp(tau * t) * lib.cos(wq * t))
    sh = where(q > 0, (ep - em) / (2 * rq), lib.exp(tau * t) * lib.sin(wq * t) / wq)
    return c + sh * (a11 - tau), sh * a12, sh * a21, c + sh * (a22 - tau)


def _response(
    s: Any, rho: Any, d: Any, g: Any, q: Any, times: Sequence[float] | np.ndarray, lib: Any = np
) -> list[tuple[Any, Any]]:
    """``U[i] = (e11, e12)``: the first row of ``exp(A(q) t_i)`` for each time."""
    fu, fv, gu, gv = jacobian(s, rho)
    ones = q * 0 + 1
    return [
        expm2(g * fu - q, g * fv * ones, g * gu * ones, g * gv - d * q, float(t), lib=lib)[:2]
        for t in times
    ]


def mode_terms(
    s: Any,
    rho: Any,
    d: Any,
    g: Any,
    s0: Any,
    sigma: Any,
    q: Any,
    stats: np.ndarray,
    times: Sequence[float],
    *,
    lib: Any = np,
) -> tuple[Any, Any]:
    """``(log det C, tr(C^-1 S))`` per wavenumber, by the Woodbury identity.

    ``q`` has the wavenumbers (``(..., G)``), ``stats`` the real parts of the summed outer
    products ``S = sum x x^H`` (``(G, T, T)``). The parameters broadcast against ``q``.
    """
    n_t = len(times)
    u = _response(s, rho, d, g, q, times, lib)
    r = s0**2 / sigma**2
    g00 = sum(u0 * u0 for u0, _ in u)
    g01 = sum(u0 * u1 for u0, u1 in u)
    g11 = sum(u1 * u1 for _, u1 in u)
    h00, h01, h11 = 1 + r * g00, r * g01, 1 + r * g11
    det_h = h00 * h11 - h01**2
    # W = U^T S U per wavenumber (S is symmetric, so W is too)
    u0s = lib.stack([u0 for u0, _ in u], axis=-1)  # (..., G, T)
    u1s = lib.stack([u1 for _, u1 in u], axis=-1)
    su0 = (stats * u0s[..., None, :]).sum(axis=-1)  # S u0: (..., G, T)
    su1 = (stats * u1s[..., None, :]).sum(axis=-1)
    w00 = (u0s * su0).sum(axis=-1)
    w01 = (u0s * su1).sum(axis=-1)
    w11 = (u1s * su1).sum(axis=-1)
    tr_hinv_w = (h11 * w00 - 2 * h01 * w01 + h00 * w11) / det_h
    tr_s = np.trace(stats, axis1=-2, axis2=-1)
    logdet = n_t * lib.log(sigma**2) + lib.log(det_h)
    quad = (tr_s - r * tr_hinv_w) / sigma**2
    return logdet, quad


# ============================================================================== spectral grid


@dataclass(frozen=True, slots=True)
class SpectralGrid:
    """Wave numbers of an ``n x n`` periodic square of side ``box`` in the rfft2 layout."""

    n: int
    box: float
    k2: np.ndarray  # (n, n // 2 + 1)

    @classmethod
    def of(cls, n: int, box: float) -> SpectralGrid:
        kx = 2 * np.pi * np.fft.fftfreq(n, box / n)
        ky = 2 * np.pi * np.fft.rfftfreq(n, box / n)
        return cls(n=n, box=float(box), k2=kx[:, None] ** 2 + ky[None, :] ** 2)


def initial_state(
    grid: SpectralGrid, s: float, rho: float, s0: float, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """The uniform steady state plus white noise of sd ``s0`` in both species."""
    b = (1 - rho) * s
    n = grid.n
    return (
        s + s0 * rng.standard_normal((n, n)),
        b / s**2 + s0 * rng.standard_normal((n, n)),
    )


def simulate_pattern(
    state: tuple[np.ndarray, np.ndarray],
    grid: SpectralGrid,
    s: float,
    rho: float,
    d: float,
    g: float,
    t_end: float,
    *,
    dt: float | None = None,
    snaps: Sequence[float] = (),
) -> tuple[tuple[np.ndarray, np.ndarray], dict[float, np.ndarray]]:
    """Semi-implicit spectral integration (diffusion implicit, reactions explicit) from
    ``state`` for ``t_end``. Returns the final ``(u, v)`` and ``{t: u}`` at ``snaps``."""
    a, b = rho * s, (1 - rho) * s
    dt = dt or min(0.025, 0.1 / (g * (1 + s**2)))
    n = round(t_end / dt)
    iu, iv = 1 / (1 + dt * grid.k2), 1 / (1 + dt * d * grid.k2)
    want = {round(t / dt): t for t in snaps}
    u, v = state
    out: dict[float, np.ndarray] = {}
    if 0 in want:
        out[want[0]] = u.copy()
    for i in range(1, n + 1):
        uuv = u * u * v
        u = np.fft.irfft2(np.fft.rfft2(u + dt * g * (a - u + uuv)) * iu, s=u.shape)
        v = np.fft.irfft2(np.fft.rfft2(v + dt * g * (b - uuv)) * iv, s=v.shape)
        if i in want:
            out[want[i]] = u.copy()
    return (u, v), out


# ============================================================================== Fourier data


@dataclass(frozen=True, slots=True)
class ModeStatistics:
    """The kept Fourier modes of the images: per mode and summed per integer ``|k|^2``."""

    times: tuple[float, ...]
    q_mode: np.ndarray  # (M,) |k|^2 of each kept mode
    s_mode: np.ndarray  # (M, T, T) Re(x x^H) of each mode
    q_group: np.ndarray  # (G,)
    s_group: np.ndarray  # (G, T, T)
    n_group: np.ndarray  # (G,) modes per group
    mode_ky: np.ndarray  # (M,) integer wave numbers of each mode (row, column)
    mode_kx: np.ndarray

    @property
    def n_modes(self) -> int:
        return int(self.q_mode.size)


def mode_statistics(
    images: np.ndarray, times: Sequence[float], box: float, k2_max: float
) -> ModeStatistics:
    """Sufficient statistics of images ``(T, n, n)`` for the mode-growth likelihood.

    Coefficients are ``rfft2 / n`` (variance-preserving for white noise). Only the columns
    ``1 .. n/2 - 1`` of the half-plane are used (no conjugate partner inside it) and only
    modes with ``q < k2_max``.
    """
    n = images.shape[-1]
    coef = np.fft.rfft2(images) / n
    m_int = (np.fft.fftfreq(n) * n)[:, None] ** 2 + np.arange(n // 2 + 1)[None, :] ** 2
    inner = np.zeros(m_int.shape, dtype=bool)
    inner[:, 1 : n // 2] = True
    unit = (2 * np.pi / box) ** 2
    keep = inner & (m_int * unit < k2_max)
    rows, cols = np.nonzero(keep)
    x = coef[:, rows, cols]  # (T, M)
    k2_int = m_int[rows, cols].astype(np.int64)
    s_mode = np.real(x.T[:, :, None] * np.conj(x.T[:, None, :]))  # (M, T, T)
    k2_group, grp = np.unique(k2_int, return_inverse=True)
    n_t = len(times)
    s_group = np.zeros((k2_group.size, n_t, n_t))
    np.add.at(s_group, grp, s_mode)
    ky = np.rint(np.fft.fftfreq(n) * n).astype(np.int64)[rows]
    return ModeStatistics(
        times=tuple(float(t) for t in times),
        q_mode=k2_int * unit,
        s_mode=s_mode,
        q_group=k2_group * unit,
        s_group=s_group,
        n_group=np.bincount(grp, minlength=k2_group.size).astype(float),
        mode_ky=ky,
        mode_kx=cols.astype(np.int64),
    )


def pointwise_loglik(post: dict[str, np.ndarray], stats: ModeStatistics) -> np.ndarray:
    """Exact log-likelihood of each kept mode for each draw: ``(draws, modes)``."""
    n_t = len(stats.times)
    n = post["s"].size
    out = np.empty((n, stats.n_modes))
    step = max(1, 2_000_000 // max(1, stats.n_modes * n_t * n_t))
    for lo in range(0, n, step):
        sl = slice(lo, lo + step)
        p = {k: v[sl, None] for k, v in post.items()}
        logdet, quad = mode_terms(
            p["s"], p["rho"], p["d"], p["g"], p["s0"], p["sigma"],
            stats.q_mode[None, :], stats.s_mode, stats.times,
        )  # fmt: skip
        out[sl] = -n_t * math.log(math.pi) - logdet - quad
    return out


def conditional_pattern(
    images: np.ndarray,
    fit_times: Sequence[float],
    times: Sequence[float] | np.ndarray,
    grid: SpectralGrid,
    p: dict[str, float],
    rng: np.random.Generator,
    *,
    noise: bool = False,
) -> np.ndarray:
    """One exact draw of the activator at ``times`` given ``images`` at ``fit_times``.

    Per Fourier mode the initial perturbation ``z = (u0, v0)`` has the prior
    ``CN(0, s0^2 I)`` and the images are ``U_F z + e``; the draw is
    ``U_P z`` with ``z`` from its Gaussian conditional, back on the pixel grid. The level
    is the mean of ``images``, not ``u* = s``: the likelihood leaves out the ``k = 0``
    mode (concentrations are seldom calibrated), so ``s`` does not pin the mean level.
    With ``noise`` the camera noise ``sigma`` is added.
    """
    n = grid.n
    q = grid.k2
    s, rho, d, g, s0, sig = (p[k] for k in PARAMETERS)
    level = float(np.mean(images))
    x = np.fft.rfft2(np.asarray(images, dtype=float) - level) / n  # (T_F, n, n/2+1)
    uf = _response(s, rho, d, g, q, fit_times)
    up = _response(s, rho, d, g, q, times)
    # precision of z: I / s0^2 + U_F^T U_F / sigma^2
    l00 = 1 / s0**2 + sum(u0 * u0 for u0, _ in uf) / sig**2
    l01 = sum(u0 * u1 for u0, u1 in uf) / sig**2
    l11 = 1 / s0**2 + sum(u1 * u1 for _, u1 in uf) / sig**2
    det = l00 * l11 - l01**2
    c00, c01, c11 = l11 / det, -l01 / det, l00 / det  # covariance of z
    b0 = sum(u0 * x[i] for i, (u0, _) in enumerate(uf)) / sig**2
    b1 = sum(u1 * x[i] for i, (_, u1) in enumerate(uf)) / sig**2
    m0, m1 = c00 * b0 + c01 * b1, c01 * b0 + c11 * b1
    # Cholesky factor of the covariance; white noise from real images keeps the symmetry
    r00 = np.sqrt(c00)
    r10 = c01 / r00
    r11 = np.sqrt(np.maximum(c11 - r10**2, 0.0))
    w0 = np.fft.rfft2(rng.standard_normal((n, n))) / n
    w1 = np.fft.rfft2(rng.standard_normal((n, n))) / n
    z0 = m0 + r00 * w0
    z1 = m1 + r10 * w0 + r11 * w1
    out = np.empty((len(times), n, n))
    for i, (e0, e1) in enumerate(up):
        out[i] = np.fft.irfft2((e0 * z0 + e1 * z1) * n, s=(n, n)) + level
    if noise:
        out += sig * rng.standard_normal(out.shape)
    return out


# ============================================================================== settings


class TuringPatternSettings(ModelSettings):
    """Settings of ``sci.turing_pattern`` (dimensionless Schnakenberg units)."""

    box_length: float = Field(
        100.0,
        gt=0,
        description="Side of the square image in the length unit of the model (the activator "
        "diffuses with D = 1 in these units) (x-ui-level: basic)",
    )
    k2_max: float = Field(
        1.5,
        gt=0,
        description="Use only the Fourier modes with |k|^2 below this value; faster modes "
        "have decayed below the camera noise (x-ui-level: basic)",
    )
    max_extension: int = Field(
        2,
        ge=0,
        le=100,
        description="Image times a prediction may reach past the last fitted time (the "
        "linear theory holds only while the pattern is small)",
    )
    d_median: float = Field(10.0, gt=0, description="Prior median of the diffusion ratio d")
    d_log_sd: float = Field(1.0, gt=0, description="Log-scale sd of the prior of d")
    initial_noise_median: float = Field(
        1e-3, gt=0, description="Prior median of the initial noise sd s0"
    )
    camera_noise_median: float = Field(
        1e-3, gt=0, description="Prior median of the camera noise sd sigma"
    )
    field_draws: int = Field(
        40, ge=1, le=1000, description="Posterior draws per score (over all chains)"
    )
    simulate_draws: int = Field(
        4,
        ge=1,
        le=200,
        description="Nonlinear simulations per simulate score (each one is a full PDE run)",
    )
    simulate_dt: float | None = Field(
        None,
        gt=0,
        le=0.1,
        description="Time step of the spectral solver in simulate (default: min(0.025, "
        "0.1 / (g (1 + s^2))))",
    )
    settle_time: float = Field(
        300.0, gt=0, le=5000.0, description="Time of the settled pattern in simulate"
    )
    wavenumber_max: float = Field(
        1.2, gt=0, description="Largest wavenumber of the dispersion relation output"
    )
    wavenumber_points: int = Field(
        120, ge=10, le=2000, description="Points of the dispersion relation output"
    )
    a_max: float = Field(0.6, gt=0, description="Largest supply rate a of the Turing-space map")
    b_max: float = Field(3.0, gt=0, description="Largest supply rate b of the Turing-space map")
    supply_points: int = Field(
        60, ge=5, le=300, description="Grid points along a and b of the Turing-space map"
    )
    precondition: bool = Field(
        True,
        description="Sample a parameterisation whitened at the posterior mode (Laplace "
        "approximation); the model is the same. False samples the notebook's parameters "
        "directly (needs a long warm-up, for example tune 1500)",
    )


_TEMPLATE: dict[str, Any] = {
    "params": {"max_extension": {"type": "integer", "default": 2}},
    "template": {
        "dims": {
            TIME_DIM: {
                "kind": "float",
                "policy": "extend",
                "ordered": True,
                "max_extension": "{max_extension}",
                "description": "Time since the uniform state was perturbed",
            },
            Y_DIM: {"kind": "integer", "policy": "fixed", "description": "Pixel row"},
            X_DIM: {"kind": "integer", "policy": "fixed", "description": "Pixel column"},
        },
        "vars": {
            TARGET: {
                "dims": list(FIELD_DIMS),
                "dtype": "float64",
                "role": "target",
                "description": "Activator concentration in each pixel (early, small pattern)",
            }
        },
    },
}

_GRID = {"y": Y_DIM, "x": X_DIM, "origin": "lower"}


# ============================================================================== the model


class TuringPatternModel(CustomModel[TuringPatternSettings]):
    """Schnakenberg kinetics from the exact Fourier-mode likelihood of early images (E61)."""

    id = "sci.turing_pattern"
    version = "1.0.0"
    Settings = TuringPatternSettings
    description = (
        "Turing patterns: recovers the reaction and diffusion rates of a Schnakenberg "
        "activator-substrate system from early images of a growing pattern. The likelihood is "
        "exact for the linearised dynamics (independent complex Gaussian Fourier modes, a "
        "Woodbury identity per wavenumber). Gives the dispersion relation, the pattern "
        "wavelength, P(Turing conditions hold) and simulated patterns from a spectral solver."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        loo_valid=True,
        identified_quantities=["d", "g", "s", "s0", "sigma"],
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "pattern": {
            "dims": list(FIELD_DIMS),
            "units_of": TARGET,
            "description": "Activator field at the requested times: a draw given the fitted "
            "images (predict), or a new pattern from the nonlinear solver (simulate)",
            "grid": {**_GRID, "frame": TIME_DIM, "colormap": "magma"},
        },
        "settled": {
            "dims": [Y_DIM, X_DIM],
            "units_of": TARGET,
            "description": "Simulate only: the pattern of each run at settle_time",
            "grid": {**_GRID, "colormap": "magma"},
        },
        "growth": {
            "dims": [WAVENUMBER_DIM],
            "units": "1/time",
            "description": "Dispersion relation: growth rate Re lambda(k) of a mode with "
            "wavenumber k",
        },
        "p_turing": {
            "dims": [],
            "units": "probability",
            "description": "1 when the Turing conditions hold for the draw (the mean is the "
            "probability)",
        },
        "wavelength": {
            "dims": [],
            "units": "length",
            "description": "Wavelength 2 pi / k_m of the fastest-growing mode",
        },
        "turing_space": {
            "dims": [B_DIM, A_DIM],
            "units": "probability",
            "description": "1 where the supply rates (a, b) are Turing unstable at the draw's "
            "diffusion ratio (the mean is P(Turing))",
            "grid": {"y": B_DIM, "x": A_DIM, "origin": "lower", "colormap": "viridis"},
        },
    }
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "sci.field_map",
        "sci.ppc_image_grid",
        "sci.profile_bands",
    )
    # notebook geometry (128 x 128, 4 images): about 90 s of NUTS at tune 1500
    resources = ResourceHint(est_fit_seconds=120)
    pad_extend = False
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== fit
    def build_model(self, stats: ModeStatistics, s: TuringPatternSettings) -> pm.Model:
        """The notebook's model: priors plus the mode-growth likelihood as a Potential."""
        n_t = len(stats.times)
        with pm.Model() as model:
            sup = pm.LogNormal("s", 0.0, 0.5)
            rho = pm.Beta("rho", 2.0, 5.0)
            d = pm.LogNormal("d", np.log(s.d_median), s.d_log_sd)
            g = pm.LogNormal("g", 0.0, 0.7)
            s0 = pm.LogNormal("s0", np.log(s.initial_noise_median), 1.5)
            sig = pm.LogNormal("sigma", np.log(s.camera_noise_median), 1.5)
            pm.Deterministic("a", rho * sup)
            pm.Deterministic("b", (1 - rho) * sup)
            logdet, quad = mode_terms(
                sup, rho, d, g, s0, sig, stats.q_group, stats.s_group, stats.times, lib=pt
            )
            const = n_t * math.log(math.pi)
            pm.Potential("mode_growth", pt.sum(-stats.n_group * (logdet + const) - quad))
        return model

    def build_whitened(
        self,
        stats: ModeStatistics,
        s: TuringPatternSettings,
        centre: np.ndarray,
        chol: np.ndarray,
    ) -> pm.Model:
        """The same model in unconstrained coordinates ``u = centre + chol z`` with a flat
        ``z``: log of the positive parameters, logit of ``rho``. The priors (with their
        Jacobians) and the likelihood are Potentials."""
        n_t = len(stats.times)
        with pm.Model() as model:
            z = pm.Flat("z", shape=len(PARAMETERS), initval=np.zeros(len(PARAMETERS)))
            u = pt.as_tensor(np.asarray(centre, dtype=float)) + pt.as_tensor(chol) @ z
            p = _natural(u)
            for name in PARAMETERS:
                pm.Deterministic(name, p[name])
            pm.Deterministic("a", p["rho"] * p["s"])
            pm.Deterministic("b", (1 - p["rho"]) * p["s"])
            pm.Potential("prior", _log_prior(u, s))
            logdet, quad = mode_terms(
                p["s"],
                p["rho"],
                p["d"],
                p["g"],
                p["s0"],
                p["sigma"],
                stats.q_group,
                stats.s_group,
                stats.times,
                lib=pt,
            )
            const = n_t * math.log(math.pi)
            pm.Potential("mode_growth", pt.sum(-stats.n_group * (logdet + const) - quad))
        return model

    def find_mode(
        self, stats: ModeStatistics, s: TuringPatternSettings, seed: int, starts: int = 4
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        """The posterior mode in unconstrained coordinates (best of ``starts`` L-BFGS runs
        from the prior medians and seeded prior draws) and the Cholesky factor of the
        Laplace covariance there."""
        k = len(PARAMETERS)
        model = self.build_whitened(stats, s, np.zeros(k), np.eye(k))
        with _quiet():
            logp = model.compile_logp()
            dlogp = model.compile_dlogp()

        def fun(u: np.ndarray) -> tuple[float, np.ndarray]:
            v = float(logp({"z": u}))
            if not np.isfinite(v):
                return 1e300, np.zeros(k)
            return -v, -np.asarray(dlogp({"z": u}), dtype=float)

        rng = _rng(seed, "turing-mode")
        centre0 = _prior_centre(s)
        inits = [centre0] + [
            centre0 + _prior_sd(s) * rng.standard_normal(k) for _ in range(max(0, starts - 1))
        ]
        best = None
        for u0 in inits:
            res = minimize(fun, u0, jac=True, method="L-BFGS-B", options={"maxiter": 2000})
            if best is None or res.fun < best.fun:
                best = res
        assert best is not None
        u_map = np.asarray(best.x, dtype=float)
        h = 1e-4
        hess = np.empty((k, k))
        for i in range(k):
            e = np.zeros(k)
            e[i] = h
            hess[:, i] = (fun(u_map + e)[1] - fun(u_map - e)[1]) / (2 * h)
        hess = (hess + hess.T) / 2
        w, v = np.linalg.eigh(hess)
        w = np.where(np.isfinite(w), np.maximum(w, 1e-6), 1.0)
        cov = (v / w) @ v.T
        chol = np.linalg.cholesky((cov + cov.T) / 2)
        info = {
            "neg_logpost": float(best.fun),
            "starts": len(inits),
            "values": {
                name: float(val)
                for name, val in zip(PARAMETERS, _natural(u_map, np).values(), strict=True)
            },
            "laplace_sd_log_d": float(np.sqrt(cov[2, 2])),
        }
        return u_map, chol, info

    def fit(
        self, ds: xr.Dataset, s: TuringPatternSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        data = _Data.from_dataset(ds)
        stats = data.statistics(s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = self.build_model(stats, s)
        mode_info: dict[str, Any] | None = None
        sample_model = model
        if s.precondition:
            ctx.progress(0.02, "finding the mode")
            with _quiet():
                centre, chol, mode_info = self.find_mode(stats, s, seed)
            sample_model = self.build_whitened(stats, s, centre, chol)
        ctx.check_cancelled()
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        if plan.target_accept is None:
            plan = replace(plan, target_accept=0.95)
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
                ctx.progress(0.9 * fraction, message)

        with _quiet():
            result = run_engine(
                sample_model,
                plan,
                EngineHooks(progress=report, cancel=ctx.cancel, log=ctx.log, sink=sink, key=key),
            )
        ctx.check_cancelled()
        raw = result.group("posterior")
        if raw is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        posterior = raw[[*PARAMETERS, "a", "b"]].copy()
        flat = {k: np.asarray(posterior[k].values, dtype=float).ravel() for k in PARAMETERS}
        shape = (posterior.sizes["chain"], posterior.sizes["draw"])
        km, lm = fastest_mode(flat["s"], flat["rho"], flat["d"], flat["g"])
        posterior["wavelength"] = (("chain", "draw"), (2 * np.pi / km).reshape(shape))
        posterior["max_growth"] = (("chain", "draw"), lm.reshape(shape))
        turing = is_turing(flat["s"], flat["rho"], flat["d"]).astype(np.float64)
        posterior["p_turing"] = (("chain", "draw"), turing.reshape(shape))
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, result.engine)}
        stats_grp = result.group("sample_stats")
        if stats_grp is not None:
            keep = [
                v
                for v in stats_grp.data_vars
                if stats_grp[v].dtype.kind in "biuf" and set(stats_grp[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats_grp[keep], result.engine)
        groups["observed_data"] = _clean(xr.Dataset({TARGET: data.field}), result.engine)
        if cfg.keep.log_likelihood:
            ll = pointwise_loglik(flat, stats).reshape(*shape, stats.n_modes)
            groups["log_likelihood"] = _clean(
                xr.Dataset(
                    {TARGET: (("chain", "draw", MODE_DIM), ll)},
                    coords={
                        "chain": posterior["chain"].values,
                        "draw": posterior["draw"].values,
                        MODE_DIM: np.arange(stats.n_modes),
                        "mode_ky": (MODE_DIM, stats.mode_ky),
                        "mode_kx": (MODE_DIM, stats.mode_kx),
                    },
                ),
                result.engine,
            )
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=[*PARAMETERS, "a", "b"], random_seed=seed
            )
        groups["prior"] = _clean(prior["prior"].to_dataset(), result.engine)
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "target_accept": plan.target_accept,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "notes": [str(n) for n in result.notes],
            "n_pixels": int(data.n),
            "n_modes": stats.n_modes,
            "n_wavenumbers": int(stats.q_group.size),
            "times": list(stats.times),
            "max_relative_deviation": data.max_relative_deviation,
            "p_turing": float(turing.mean()),
            "precondition": s.precondition,
            **({"mode": mode_info} if mode_info is not None else {}),
        }
        return FitOutput(groups=groups, learned=learned)

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        out = convergence_diagnostics(
            post, fitted.groups.get("sample_stats"), var_names=list(PARAMETERS)
        )
        dev = fitted.learned.get("max_relative_deviation")
        if isinstance(dev, int | float) and dev > 0.1:
            out.append(
                Diagnostic(
                    code="NONLINEAR_REGIME",
                    severity="warning",
                    metric="max_relative_deviation",
                    value=float(dev),
                    threshold=0.1,
                    message_expert=f"the images deviate up to {dev:.1%} from their mean; the "
                    "mode-growth likelihood is exact only for the linearised dynamics (small "
                    "perturbations), so the estimates can be biased",
                    message_plain="The pattern in the images is already large. The model is "
                    "correct only for early images of a small pattern.",
                    suggested_action="Fit only the early images, while the pattern is a few "
                    "percent of the mean level.",
                )
            )
        return out

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        data = _Data.from_fitted(fitted)
        post = fitted.posterior
        simulate = ctx.mode == "simulate"
        chains, draws = _draw_index(post, s.simulate_draws if simulate else s.field_draws)
        values = {
            k: np.asarray(post[k].transpose("chain", "draw").values, dtype=float)
            for k in PARAMETERS
        }
        times = (
            np.asarray(ds[TIME_DIM].values, dtype=float)
            if TIME_DIM in ds.coords
            else np.asarray(data.times, dtype=float)
        )
        if times.size and float(times.min()) < 0:
            raise MapiError(
                "score times must not be negative (t counts from the start of the pattern)",
                code=ErrorCode.COORD_NOT_ALLOWED,
            )
        grid = SpectralGrid.of(data.n, s.box_length)
        n = data.n
        shape = (chains.size, draws.size)
        pattern = np.empty((*shape, times.size, n, n), dtype=np.float32)
        settled = np.empty((*shape, n, n), dtype=np.float32) if simulate else None
        noisy = simulate or ctx.kind == "predictive"
        for i, c in enumerate(chains.tolist()):
            for j, dd in enumerate(draws.tolist()):
                ctx.check_cancelled()
                p = {k: float(v[c, dd]) for k, v in values.items()}
                rng = _rng(ctx.seed, f"{ctx.mode}:{c}:{dd}")
                if simulate:
                    imgs, final = _simulate_draw(grid, p, times, s, rng)
                    pattern[i, j] = imgs
                    assert settled is not None
                    settled[i, j] = final
                else:
                    pattern[i, j] = conditional_pattern(
                        data.images, data.times, times, grid, p, rng, noise=noisy
                    )
        coords: dict[str, Any] = {
            "chain": np.asarray(post["chain"].values)[chains],
            "draw": np.asarray(post["draw"].values)[draws],
            TIME_DIM: times,
            Y_DIM: data.y,
            X_DIM: data.x,
        }
        sub = {k: v[np.ix_(chains, draws)] for k, v in values.items()}
        kq = np.linspace(0.0, s.wavenumber_max, s.wavenumber_points)
        lam = growth_rate(kq**2, *(sub[k][..., None] for k in ("s", "rho", "d", "g")))  # (C, D, K)
        km, _ = fastest_mode(*(sub[k].ravel() for k in ("s", "rho", "d", "g")))
        a_grid = np.linspace(s.a_max / s.supply_points, s.a_max, s.supply_points)
        b_grid = np.linspace(s.b_max / s.supply_points, s.b_max, s.supply_points)
        aa, bb = np.meshgrid(a_grid, b_grid)  # (B, A)
        space = is_turing(aa + bb, aa / (aa + bb), sub["d"][..., None, None]).astype(np.float32)
        kind = "predictive" if noisy else "expected"
        # growth first: without a manifest, curve recipes take the first variable with draws
        arrays: dict[str, xr.DataArray] = {
            "growth": xr.DataArray(
                lam,
                dims=("chain", "draw", WAVENUMBER_DIM),
                coords={WAVENUMBER_DIM: kq},
                attrs={"kind": "posterior", "units": "1/time"},
            ),
            "pattern": xr.DataArray(
                pattern,
                dims=("chain", "draw", *FIELD_DIMS),
                attrs={"kind": kind, "noise": "seeded_white" if noisy else "none"},
            ),
            "p_turing": xr.DataArray(
                is_turing(sub["s"], sub["rho"], sub["d"]).astype(np.float64),
                dims=("chain", "draw"),
                attrs={"kind": "posterior", "units": "probability"},
            ),
            "wavelength": xr.DataArray(
                (2 * np.pi / km).reshape(shape),
                dims=("chain", "draw"),
                attrs={"kind": "posterior", "units": "length"},
            ),
            "turing_space": xr.DataArray(
                space,
                dims=("chain", "draw", B_DIM, A_DIM),
                coords={B_DIM: b_grid, A_DIM: a_grid},
                attrs={"kind": "posterior", "units": "probability"},
            ),
        }
        if settled is not None:
            arrays["settled"] = xr.DataArray(
                settled,
                dims=("chain", "draw", Y_DIM, X_DIM),
                attrs={"kind": "simulated", "noise": "none", "time": s.settle_time},
            )
        for name, da in arrays.items():
            # the grid travels with the array: score trees carry no manifest to look it up
            grid_decl = self.outputs[name].get("grid")
            if grid_decl is not None:
                da.attrs["grid"] = json.dumps(grid_decl)
        pred = xr.Dataset(arrays, coords=coords)
        sel = {d: np.asarray(ds[d].values) for d in (Y_DIM, X_DIM) if d in ds.coords}
        if sel:
            pred = pred.sel(sel)
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ============================================================================== helpers


def _natural(u: Any, lib: Any = pt) -> dict[str, Any]:
    """Parameters from unconstrained coordinates (log, logit for ``rho``)."""
    sig = (lambda x: 1 / (1 + np.exp(-x))) if lib is np else pt.sigmoid
    return {
        "s": lib.exp(u[0]),
        "rho": sig(u[1]),
        "d": lib.exp(u[2]),
        "g": lib.exp(u[3]),
        "s0": lib.exp(u[4]),
        "sigma": lib.exp(u[5]),
    }


def _log_normals(s: TuringPatternSettings) -> list[tuple[int, float, float]]:
    """``(index, mean, sd)`` of the normal priors of the log parameters (LogNormal priors)."""
    return [
        (0, 0.0, 0.5),
        (2, math.log(s.d_median), s.d_log_sd),
        (3, 0.0, 0.7),
        (4, math.log(s.initial_noise_median), 1.5),
        (5, math.log(s.camera_noise_median), 1.5),
    ]


def _log_prior(u: Any, s: TuringPatternSettings) -> Any:
    """Log prior density of the unconstrained coordinates (Jacobians included)."""
    out = 0.0
    for i, mu, sd in _log_normals(s):
        out = out - 0.5 * ((u[i] - mu) / sd) ** 2
    # Beta(2, 5) on rho, times the Jacobian rho (1 - rho) of the logit
    out = out - 2 * pm.math.log1pexp(-u[1]) - 5 * pm.math.log1pexp(u[1])
    return out


def _prior_centre(s: TuringPatternSettings) -> np.ndarray:
    """The prior medians in unconstrained coordinates (``rho`` at the Beta(2, 5) mean)."""
    out = np.zeros(len(PARAMETERS))
    for i, mu, _ in _log_normals(s):
        out[i] = mu
    out[1] = math.log(2 / 5)
    return out


def _prior_sd(s: TuringPatternSettings) -> np.ndarray:
    """Prior sds in unconstrained coordinates (about 0.8 for the logit of ``rho``)."""
    out = np.full(len(PARAMETERS), 0.8)
    for i, _, sd in _log_normals(s):
        out[i] = sd
    return out


def _simulate_draw(
    grid: SpectralGrid,
    p: dict[str, float],
    times: np.ndarray,
    s: TuringPatternSettings,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """One nonlinear run: noisy images at ``times`` and the clean field at settle_time."""
    state = initial_state(grid, p["s"], p["rho"], p["s0"], rng)
    t_end = max(float(times.max(initial=0.0)), s.settle_time)
    snaps = [float(t) for t in times]
    (u_end, _), out = simulate_pattern(
        state, grid, p["s"], p["rho"], p["d"], p["g"], t_end, dt=s.simulate_dt, snaps=snaps
    )
    if not np.isfinite(u_end).all():
        raise MapiError(
            "the spectral solver diverged; set a smaller simulate_dt",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/simulate_dt",
        )
    imgs = np.stack([out[t] for t in snaps]) if snaps else np.empty((0, grid.n, grid.n))
    imgs = imgs + p["sigma"] * rng.standard_normal(imgs.shape)
    return imgs, u_end


@dataclass(slots=True)
class _Data:
    """The fitted images as the model needs them (time, row and column order)."""

    field: xr.DataArray  # activator(t, y, x)
    images: np.ndarray  # (T, n, n)
    times: tuple[float, ...]
    y: np.ndarray
    x: np.ndarray

    @property
    def n(self) -> int:
        return int(self.images.shape[-1])

    @property
    def max_relative_deviation(self) -> float:
        mean = self.images.mean(axis=(1, 2), keepdims=True)
        dev = np.abs(self.images - mean) / np.maximum(np.abs(mean), 1e-12)
        return float(dev.max())

    def statistics(self, s: TuringPatternSettings) -> ModeStatistics:
        stats = mode_statistics(self.images, self.times, s.box_length, s.k2_max)
        if stats.n_modes == 0:
            raise MapiError(
                f"no Fourier mode has |k|^2 < k2_max = {s.k2_max:g} for box_length = "
                f"{s.box_length:g}; raise k2_max",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/k2_max",
            )
        return stats

    @classmethod
    def build(cls, field: xr.DataArray) -> _Data:
        field = field.sortby([TIME_DIM, Y_DIM, X_DIM]).transpose(*FIELD_DIMS)
        values = np.asarray(field.values, dtype=np.float64)
        if not np.isfinite(values).all():
            raise MapiError(
                "the images have missing or non-finite values; the Fourier model needs the "
                "full raster at every time (do not use a pixel-level split)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        n_t, ny, nx = values.shape
        if ny != nx or ny < 8 or ny % 2:
            raise MapiError(
                f"the raster is {ny} x {nx}; the model needs a square raster with an even "
                "number of at least 8 pixels per side",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        times = np.asarray(field[TIME_DIM].values, dtype=float)
        if n_t < 1 or float(times.min()) <= 0 or np.unique(times).size != n_t:
            raise MapiError(
                "the image times must be distinct and after the start of the pattern (t > 0)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        return cls(
            field=field.copy(data=values),
            images=values,
            times=tuple(float(t) for t in times),
            y=np.asarray(field[Y_DIM].values),
            x=np.asarray(field[X_DIM].values),
        )

    @classmethod
    def from_dataset(cls, ds: xr.Dataset) -> _Data:
        for d in FIELD_DIMS:
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        if TARGET not in ds:
            raise MapiError(f"the data has no variable {TARGET!r}", code=ErrorCode.SCHEMA_INVALID)
        return cls.build(ds[TARGET])

    @classmethod
    def from_fitted(cls, fitted: Fitted) -> _Data:
        obs = fitted.groups.get("observed_data")
        if obs is None or TARGET not in obs:
            raise MapiError(
                "the fitted model has no observed images; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        return cls.build(obs[TARGET])
