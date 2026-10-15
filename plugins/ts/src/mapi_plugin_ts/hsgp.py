"""Hilbert-space Gaussian-process (HSGP) building blocks (Solin & Särkkä; Riutort-Mayol et al.).

A stationary GP ``f ~ GP(0, k)`` on the interval ``[-L, L]`` is approximated by ``m``
Laplacian eigenfunctions::

    f(x) ≈ Σ_j  phi_j(x) · sqrt(S(sqrt(lambda_j))) · beta_j,     beta_j ~ Normal(0, 1)
    phi_j(x) = sqrt(1/L) · sin(pi j (x + L) / (2L)),     sqrt(lambda_j) = pi j / (2L)

where ``S`` is the kernel's spectral density. The basis depends only on ``x``, ``L`` and
``m``, never on the parameters, so it is computed with numpy and handed to the model as
data. That is what makes HSGP forecasting exact in this platform: :class:`HSGPDomain`
fixes the centre and half-width from the *anticipated* coordinates (the fit range plus the
dim's ``max_extension``), so the basis evaluated at a forecast position is the same
function as at fit time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pytensor.tensor as pt
from pytensor.graph.basic import Variable

__all__ = [
    "HSGPDomain",
    "Kernel",
    "hsgp_basis",
    "kernel_covariance",
    "spectral_density",
    "sqrt_eigenvalues",
]

Kernel = Literal["expquad", "matern52", "matern32"]


@dataclass(frozen=True, slots=True)
class HSGPDomain:
    """Maps positions onto the HSGP's scaled axis ``z = (x - centre) / half_width``.

    ``half_width`` is half of the anticipated range, so the anticipated coordinates map onto
    ``[-1, 1]`` and the approximation interval is ``[-c, c]`` (``L = c``).
    """

    centre: float
    half_width: float
    c: float

    @classmethod
    def from_positions(cls, positions: np.ndarray, c: float) -> HSGPDomain:
        """The domain spanning ``positions`` (e.g. the anticipated coordinates)."""
        pos = np.asarray(positions, dtype=float)
        if pos.size == 0:
            raise ValueError("an HSGP domain needs at least one position")
        if c <= 1.0:
            raise ValueError("the boundary factor c must be greater than 1")
        lo, hi = float(pos.min()), float(pos.max())
        half = (hi - lo) / 2.0
        return cls(centre=(lo + hi) / 2.0, half_width=half if half > 0 else 1.0, c=float(c))

    @property
    def L(self) -> float:
        return self.c

    def scale(self, positions: np.ndarray) -> np.ndarray:
        """Positions on the scaled axis (``[-1, 1]`` over the anticipated range)."""
        return (np.asarray(positions, dtype=float) - self.centre) / self.half_width

    def contains(self, positions: np.ndarray) -> bool:
        """Whether every position lies strictly inside the approximation interval."""
        z = self.scale(positions)
        return bool(z.size == 0 or np.abs(z).max() < self.L)


def sqrt_eigenvalues(m: int, L: float) -> np.ndarray:
    """``sqrt(lambda_j) = pi j / (2L)`` for ``j = 1..m``."""
    if m < 1:
        raise ValueError("m must be at least 1")
    return np.pi * np.arange(1, m + 1) / (2.0 * L)


def hsgp_basis(z: np.ndarray, m: int, L: float) -> np.ndarray:
    """The ``(len(z), m)`` eigenfunction matrix ``phi_j(z)`` on ``[-L, L]``."""
    z = np.asarray(z, dtype=float)
    if z.size and np.abs(z).max() >= L:
        raise ValueError("positions must lie strictly inside [-L, L]")
    j = np.arange(1, m + 1)
    return np.sqrt(1.0 / L) * np.sin(np.pi * j[None, :] * (z[:, None] + L) / (2.0 * L))


def spectral_density(kernel: Kernel, omega: Any, lengthscale: Any, eta: Any) -> Any:
    """One-dimensional spectral density ``S(omega)`` of a unit-input kernel.

    Works on numpy arrays and on pytensor tensors (the model passes tensors for the
    lengthscale and amplitude). ``eta`` is the marginal standard deviation.

    * expquad:  ``eta² sqrt(2 pi) l exp(-l² w² / 2)``
    * Matérn nu: ``eta² 2 sqrt(pi) Γ(nu+1/2) (2nu)^nu / (Γ(nu) l^(2nu)) (2nu/l² + w²)^-(nu+1/2)``
    """
    lib: Any = pt if _is_tensor(omega, lengthscale, eta) else np
    ls, w = lengthscale, omega
    if kernel == "expquad":
        return eta**2 * math.sqrt(2.0 * math.pi) * ls * lib.exp(-0.5 * ls**2 * w**2)
    nu = 2.5 if kernel == "matern52" else 1.5 if kernel == "matern32" else None
    if nu is None:
        raise ValueError(f"unknown kernel {kernel!r}")
    log_const = (
        math.log(2.0)
        + 0.5 * math.log(math.pi)
        + math.lgamma(nu + 0.5)
        + nu * math.log(2.0 * nu)
        - math.lgamma(nu)
    )
    return (
        eta**2
        * math.exp(log_const)
        * ls ** (-2.0 * nu)
        * (2.0 * nu / ls**2 + w**2) ** (-(nu + 0.5))
    )


def kernel_covariance(
    kernel: Kernel, x1: np.ndarray, x2: np.ndarray, lengthscale: float, eta: float
) -> np.ndarray:
    """The exact covariance matrix ``k(x1, x2)`` (used to test the approximation)."""
    r = np.abs(np.asarray(x1, dtype=float)[:, None] - np.asarray(x2, dtype=float)[None, :])
    s = r / lengthscale
    if kernel == "expquad":
        return eta**2 * np.exp(-0.5 * s**2)
    if kernel == "matern52":
        a = math.sqrt(5.0) * s
        return eta**2 * (1.0 + a + a**2 / 3.0) * np.exp(-a)
    if kernel == "matern32":
        a = math.sqrt(3.0) * s
        return eta**2 * (1.0 + a) * np.exp(-a)
    raise ValueError(f"unknown kernel {kernel!r}")


def _is_tensor(*values: Any) -> bool:
    return any(isinstance(v, Variable) for v in values)
