"""Physics of ``sci.strong_lens`` (E80): ray tracing, light profiles, PSF and noise algebra.

The forward-model functions take an array namespace ``xp`` - :mod:`numpy` or
:mod:`pytensor.tensor` - so the same code builds the PyTensor likelihood that NUTS samples
and the NumPy reference the tests check it against. Angles are in arcsec; the image plane
has ``x`` along columns and ``y`` along rows, with the lens near ``(0, 0)`` at the centre of
the cutout (row 0 is the bottom row, as in an astronomical image drawn with
``origin="lower"``).

* :func:`sie_deflection` - the singular isothermal ellipsoid (SIE) in closed form
  (Kormann, Schneider & Bartelmann 1994; intermediate-axis normalisation, as SLACS);
  :func:`shear_deflection` - external shear;
* :func:`sersic` - an elliptical Sersic profile written in the ellipticity components, so
  the round profile is not a singular point;
* :func:`psf_factors` / :func:`convolve` - PSF convolution as a short sum of separable
  terms (a truncated SVD of the kernel): ``conv(X) = sum_k T_k^row X (T_k^col)^T`` with
  banded Toeplitz matrices. Exact for a Gaussian PSF (rank 1), a few terms for an HST PSF,
  and only matrix products, so it runs on every PyTensor backend (C, Numba for nutpie,
  JAX) without an FFT;
* :func:`whitening` - drizzled images have correlated noise between neighbouring pixels.
  A separable lag-one correlation ``rho`` (rows x columns) is whitened exactly by the
  inverse Cholesky factors of the two tridiagonal correlation matrices;
* :func:`critical_curves` - the tangential critical curve, its caustic and the cut of an
  SIE plus shear, analytic per direction (the isothermal Hessian is rank one), so they are
  cheap for every posterior draw;
* :func:`einstein_mass` - the mass inside the Einstein radius for a flat LCDM cosmology.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import integrate

__all__ = [
    "SERSIC_N_RANGE",
    "CriticalCurves",
    "PsfFactors",
    "angular_distances",
    "bin_pixels",
    "convolve",
    "critical_curves",
    "deflection",
    "einstein_mass",
    "ellipticity_to_q_phi",
    "pixel_axes",
    "psf_factors",
    "ray_grid",
    "regularisation_matrix",
    "sersic",
    "shear_deflection",
    "sie_deflection",
    "sie_kappa",
    "whitening",
]

SERSIC_N_RANGE = (0.5, 8.0)
"""Sersic indices the Capaccioli ``b_n`` approximation is good for."""

_C_KM_S = 299_792.458
_G_SI = 6.674_30e-11
_MPC_M = 3.085_677_581e22
_MSUN_KG = 1.988_47e30
_ARCSEC = np.pi / 180.0 / 3600.0


# ============================================================================ grids


def pixel_axes(n: int, pixel_scale: float) -> np.ndarray:
    """Pixel-centre positions (arcsec) along one axis of ``n`` pixels, centred on zero."""
    return (np.arange(n) - (n - 1) / 2.0) * pixel_scale


def ray_grid(
    n_row: int, n_col: int, pixel_scale: float, subsample: int = 1
) -> tuple[np.ndarray, np.ndarray]:
    """``(x, y)`` of the rays traced through every pixel: ``subsample`` x ``subsample``
    rays per pixel on a regular sub-grid, shape ``(n_row * s, n_col * s)``."""
    s = int(subsample)
    gx = (np.arange(n_col * s) - (n_col * s - 1) / 2.0) / s * pixel_scale
    gy = (np.arange(n_row * s) - (n_row * s - 1) / 2.0) / s * pixel_scale
    x, y = np.meshgrid(gx, gy)
    return x, y


def bin_pixels(img: Any, n_row: int, n_col: int, subsample: int) -> Any:
    """Average the sub-pixel rays of each pixel (``xp``-agnostic: NumPy or PyTensor)."""
    if subsample == 1:
        return img
    s = int(subsample)
    return img.reshape((n_row, s, n_col, s)).mean(axis=(1, 3))


# ============================================================================ lens


def ellipticity_to_q_phi(e1: Any, e2: Any, xp: Any = np) -> tuple[Any, Any]:
    """Axis ratio ``q`` and major-axis angle ``phi`` (radians from +x) of ``(e1, e2)``."""
    e = xp.sqrt(e1**2 + e2**2 + 1e-14)
    return (1 - e) / (1 + e), 0.5 * xp.arctan2(e2, e1)


def sie_deflection(
    x: Any, y: Any, theta_e: Any, e1: Any, e2: Any, x0: Any, y0: Any, xp: Any = np
) -> tuple[Any, Any]:
    """Deflection (arcsec) of a singular isothermal ellipsoid (Kormann et al. 1994)."""
    q, phi = ellipticity_to_q_phi(e1, e2, xp)
    c, s = xp.cos(phi), xp.sin(phi)
    xr = c * (x - x0) + s * (y - y0)
    yr = -s * (x - x0) + c * (y - y0)
    f = xp.sqrt(xp.maximum(1 - q**2, 1e-8))
    psi = xp.sqrt(q**2 * xr**2 + yr**2 + 1e-12)
    pre = theta_e * xp.sqrt(q) / f
    ax, ay = pre * xp.arctan(f * xr / psi), pre * xp.arctanh(f * yr / psi)
    return c * ax - s * ay, s * ax + c * ay


def sie_kappa(
    x: np.ndarray, y: np.ndarray, theta_e: float, e1: float, e2: float, x0: float, y0: float
) -> np.ndarray:
    """Convergence of the SIE: ``theta_E / (2 sqrt(q x_r^2 + y_r^2 / q))``."""
    q, phi = ellipticity_to_q_phi(e1, e2)
    xr = np.cos(phi) * (x - x0) + np.sin(phi) * (y - y0)
    yr = -np.sin(phi) * (x - x0) + np.cos(phi) * (y - y0)
    return theta_e / (2 * np.sqrt(q * xr**2 + yr**2 / q))


def shear_deflection(x: Any, y: Any, g1: Any, g2: Any) -> tuple[Any, Any]:
    """External shear ``(gamma1, gamma2)`` about the origin."""
    return g1 * x + g2 * y, g2 * x - g1 * y


def deflection(
    x: Any,
    y: Any,
    theta_e: Any,
    e1: Any,
    e2: Any,
    x0: Any,
    y0: Any,
    g1: Any = 0.0,
    g2: Any = 0.0,
    xp: Any = np,
) -> tuple[Any, Any]:
    """SIE plus external shear."""
    ax, ay = sie_deflection(x, y, theta_e, e1, e2, x0, y0, xp)
    sx, sy = shear_deflection(x, y, g1, g2)
    return ax + sx, ay + sy


# ============================================================================ light


def sersic(x: Any, y: Any, R: Any, n: Any, e1: Any, e2: Any, x0: Any, y0: Any, xp: Any = np) -> Any:
    """Unit-amplitude elliptical Sersic profile (value 1 on the half-light ellipse).

    The elliptical radius is a quadratic form in ``(e1, e2)``:
    ``r^2 = ((1 + e^2 - 2 e1) dx^2 - 4 e2 dx dy + (1 + e^2 + 2 e1) dy^2) / (1 - e^2)``;
    ``b_n = 1.9992 n - 0.3271`` (Capaccioli 1989).
    """
    dx, dy = x - x0, y - y0
    ee = e1**2 + e2**2
    r2 = ((1 + ee - 2 * e1) * dx**2 - 4 * e2 * dx * dy + (1 + ee + 2 * e1) * dy**2) / (1 - ee)
    bn = 1.9992 * n - 0.3271
    return xp.exp(-bn * ((xp.sqrt(r2 + 1e-10) / R) ** (1 / n) - 1))


# ============================================================================ PSF


@dataclass(frozen=True, slots=True)
class PsfFactors:
    """A separable expansion of a PSF kernel on an ``n_row`` x ``n_col`` image.

    ``rows[k]`` (``n_row x n_row``) and ``cols[k]`` (``n_col x n_col``) are banded Toeplitz
    matrices; ``conv(X) = sum_k rows[k] @ X @ cols[k].T`` is the zero-padded ("same")
    convolution of ``X`` with the rank-``k`` kernel. ``error`` is the relative Frobenius
    norm of the truncated part of the kernel.
    """

    rows: np.ndarray
    cols: np.ndarray
    error: float

    @property
    def rank(self) -> int:
        return int(self.rows.shape[0])


def _toeplitz(kernel_1d: np.ndarray, n: int) -> np.ndarray:
    """``T @ v`` = ``v`` convolved with the centred 1-D kernel (zero padding, same size)."""
    h = kernel_1d.size // 2
    out = np.zeros((n, n))
    for i in range(n):
        for j in range(max(0, i - h), min(n, i + h + 1)):
            out[i, j] = kernel_1d[i - j + h]
    return out


def psf_factors(
    psf: np.ndarray, n_row: int, n_col: int, *, tol: float = 1e-3, max_rank: int = 12
) -> PsfFactors:
    """Truncated SVD of a (normalised, odd-sized) PSF as separable Toeplitz factors.

    Keeps the fewest terms whose discarded singular values hold less than ``tol`` of the
    kernel's Frobenius norm (at most ``max_rank`` terms).
    """
    k = np.asarray(psf, dtype=float)
    if k.ndim != 2 or k.shape[0] % 2 == 0 or k.shape[1] % 2 == 0:
        raise ValueError("the PSF kernel must be a 2-D array with odd sides")
    k = k / k.sum()
    u, s, vt = np.linalg.svd(k)
    total = float(np.sqrt((s**2).sum()))
    rank = 1
    while rank < min(max_rank, s.size):
        if float(np.sqrt((s[rank:] ** 2).sum())) <= tol * total:
            break
        rank += 1
    err = float(np.sqrt((s[rank:] ** 2).sum())) / total
    rows = np.stack([_toeplitz(u[:, i] * np.sqrt(s[i]), n_row) for i in range(rank)])
    cols = np.stack([_toeplitz(vt[i] * np.sqrt(s[i]), n_col) for i in range(rank)])
    return PsfFactors(rows=rows, cols=cols, error=err)


def convolve(img: Any, factors: PsfFactors, *, constants: Any = None) -> Any:
    """``sum_k rows[k] @ img @ cols[k].T`` (``xp``-agnostic).

    ``constants`` wraps the NumPy factor matrices for a symbolic backend (for example
    ``pytensor.tensor.constant``); NumPy uses them as they are.
    """
    wrap = constants or (lambda a: a)
    out = None
    for r, c in zip(factors.rows, factors.cols, strict=True):
        term = wrap(r) @ img @ wrap(np.ascontiguousarray(c.T))
        out = term if out is None else out + term
    return out


# ============================================================================ noise


def whitening(n: int, rho: float) -> tuple[np.ndarray, np.ndarray]:
    """``(W, L)`` for a lag-one correlation ``rho`` along an axis of ``n`` pixels.

    ``K`` is tridiagonal (1 on the diagonal, ``rho`` next to it), ``L = chol(K)`` and
    ``W = L^{-1}``: if a noise image ``E`` has correlation ``K_r (x) K_c`` then
    ``W_r E W_c^T`` is white, and ``L_r Z L_c^T`` turns white noise ``Z`` into such ``E``.
    ``|rho| < 0.5`` keeps ``K`` positive definite for every ``n``.
    """
    if not abs(rho) < 0.5:
        raise ValueError("the lag-one noise correlation must be in (-0.5, 0.5)")
    k = np.eye(n) + rho * (np.eye(n, k=1) + np.eye(n, k=-1))
    lo = np.linalg.cholesky(k)
    return np.linalg.inv(lo), lo


# ============================================================================ caustics


@dataclass(frozen=True, slots=True)
class CriticalCurves:
    """The tangential critical curve (image plane), its caustic and the cut (source plane)
    of one lens, sampled at ``n`` directions around the lens centre (arcsec)."""

    critical_x: np.ndarray
    critical_y: np.ndarray
    caustic_x: np.ndarray
    caustic_y: np.ndarray
    cut_x: np.ndarray
    cut_y: np.ndarray


def critical_curves(
    theta_e: float,
    e1: float,
    e2: float,
    x0: float,
    y0: float,
    g1: float = 0.0,
    g2: float = 0.0,
    *,
    n: int = 128,
) -> CriticalCurves:
    """Analytic critical curve, caustic and cut of an SIE plus external shear.

    For a homogeneous isothermal potential ``psi = r F(phi)`` about the lens centre, the
    Hessian is ``2 kappa(theta) e_phi e_phi^T`` (rank one), so with ``M = I - Gamma`` (the
    shear part of the lens Jacobian) the determinant lemma gives the critical radius along
    each direction in closed form: ``r(phi) = 2 kappa_1(phi) e^T adj(M) e / det M`` with
    ``kappa_1`` the convergence at unit distance. The caustic is the image of that curve
    under the lens equation; the cut is the image of an infinitesimal circle around the
    singular centre. The radial critical curve of a singular lens shrinks to the centre.
    """
    phi = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    q, pa = ellipticity_to_q_phi(e1, e2)
    ux, uy = np.cos(phi), np.sin(phi)
    xr, yr = np.cos(phi - pa), np.sin(phi - pa)
    kappa1 = theta_e / (2.0 * np.sqrt(q * xr**2 + yr**2 / q))
    ex, ey = -uy, ux
    adj = (1 + g1) * ex**2 + 2 * g2 * ex * ey + (1 - g1) * ey**2
    det = (1 - g1) * (1 + g1) - g2**2
    r = 2.0 * kappa1 * adj / det
    cx, cy = x0 + r * ux, y0 + r * uy
    ax, ay = deflection(cx, cy, theta_e, e1, e2, x0, y0, g1, g2)
    # the cut is the limit r -> 0 of the lens equation: the SIE deflection depends only on
    # the direction (sampled well above its 1e-6 softening), the shear on the centre
    eps = 1e-3 * theta_e
    bx, by = sie_deflection(x0 + eps * ux, y0 + eps * uy, theta_e, e1, e2, x0, y0)
    sx, sy = shear_deflection(x0, y0, g1, g2)
    return CriticalCurves(
        critical_x=cx,
        critical_y=cy,
        caustic_x=cx - ax,
        caustic_y=cy - ay,
        cut_x=x0 - bx - sx,
        cut_y=y0 - by - sy,
    )


# ============================================================================ pixelated source


def regularisation_matrix(g: int, kind: str = "curvature") -> np.ndarray:
    """``H`` of the Gaussian smoothness prior ``exp(-lambda/2 s^T H s)`` on a ``g x g``
    source grid: squared neighbour differences (``gradient``) or squared discrete
    Laplacians (``curvature``), as in Suyu et al. (2006). Zero outside the grid."""
    eye = np.eye(g)
    if kind == "gradient":
        d = np.eye(g) - np.eye(g, k=1)
    elif kind == "curvature":
        d = 2 * np.eye(g) - np.eye(g, k=1) - np.eye(g, k=-1)
    else:
        raise ValueError(f"unknown regularisation {kind!r}")
    dx, dy = np.kron(eye, d), np.kron(d, eye)
    out: np.ndarray = dx.T @ dx + dy.T @ dy
    return out


# ============================================================================ mass


def angular_distances(
    z_lens: float, z_source: float, *, h0: float = 70.0, omega_m: float = 0.3
) -> tuple[float, float, float]:
    """``(D_l, D_s, D_ls)`` in Mpc for a flat LCDM universe."""
    if not 0 < z_lens < z_source:
        raise ValueError("the redshifts must satisfy 0 < z_lens < z_source")

    def chi(z: float) -> float:
        val, _ = integrate.quad(lambda t: 1.0 / np.sqrt(omega_m * (1 + t) ** 3 + 1 - omega_m), 0, z)
        return _C_KM_S / h0 * float(val)

    cl, cs = chi(z_lens), chi(z_source)
    return cl / (1 + z_lens), cs / (1 + z_source), (cs - cl) / (1 + z_source)


def einstein_mass(
    theta_e: np.ndarray | float,
    z_lens: float,
    z_source: float,
    *,
    h0: float = 70.0,
    omega_m: float = 0.3,
) -> np.ndarray:
    """Mass inside the Einstein radius (solar masses): ``c^2/(4G) D_l D_s / D_ls theta_E^2``."""
    d_l, d_s, d_ls = angular_distances(z_lens, z_source, h0=h0, omega_m=omega_m)
    theta = np.asarray(theta_e, dtype=float) * _ARCSEC
    kg = (_C_KM_S * 1e3) ** 2 / (4 * _G_SI) * d_l * d_s / d_ls * _MPC_M * theta**2
    out: np.ndarray = kg / _MSUN_KG
    return out
