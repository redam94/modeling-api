"""Linear Gaussian state-space recursions in numpy, vectorised over posterior draws.

Every function works on ``S`` draws of one time-invariant system at once (leading axis
``S``) and one observed series. The conventions follow pymc-extras' Kalman filter, so the
log-likelihood here equals the likelihood NUTS sampled (the parity tests check it):

* ``x0``/``P0`` are the **predicted** state of the first observation (``a_{0|-1}``): the
  filter updates first, then predicts;
* a missing observation (NaN) skips the update and adds nothing to the log-likelihood;
* the regression term ``d_t`` (``x_t . beta`` per draw) enters the observation only - the
  regression states of pymc-extras are static coefficients and stay outside the system.

:func:`simulate_ahead` and :func:`one_step_draws` take their normals from
:func:`crn_normal`, keyed by ``(seed, name)``: a scenario and its baseline that use the
same key share the noise (common random numbers), so their difference is free of it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "JITTER",
    "FilterResult",
    "StructuralSystem",
    "crn_normal",
    "kalman_filter",
    "one_step_draws",
    "rts_smoother",
    "simulate_ahead",
]

JITTER: Final = 1e-9
"""Added to the diagonal of covariances before a Cholesky factorisation."""

_LOG_2PI: Final = float(np.log(2.0 * np.pi))
_COV_JITTER: Final = 1e-8
"""pymc-extras' ``cov_jitter``: added to ``F_t`` and to every filtered covariance, as there."""


@dataclass(frozen=True, slots=True)
class StructuralSystem:
    """``S`` draws of a time-invariant system of the structural states (no regression)."""

    T: np.ndarray  # (S, k, k) transition
    Z: np.ndarray  # (S, k) design row (one series)
    RQR: np.ndarray  # (S, k, k) R Q R'
    H: np.ndarray  # (S,) observation variance
    x0: np.ndarray  # (S, k) predicted state of the first observation (a_{0|-1})
    P0: np.ndarray  # (S, k, k)

    @property
    def S(self) -> int:
        return int(self.T.shape[0])

    @property
    def k(self) -> int:
        return int(self.T.shape[-1])

    def take(self, idx: np.ndarray) -> StructuralSystem:
        """The draws ``idx`` (positions along ``S``)."""
        return StructuralSystem(
            T=self.T[idx],
            Z=self.Z[idx],
            RQR=self.RQR[idx],
            H=self.H[idx],
            x0=self.x0[idx],
            P0=self.P0[idx],
        )


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Output of :func:`kalman_filter` (standardised scale)."""

    mean: np.ndarray  # (S, n) one-step-ahead E[y_t | y_<t] without d_t
    var: np.ndarray  # (S, n) its variance F_t (H included)
    last_mean: np.ndarray  # (S, k) a_{n|n}
    last_cov: np.ndarray  # (S, k, k) P_{n|n}
    loglik: np.ndarray  # (S,) over the observed steps
    pred_mean: np.ndarray | None = None  # (S, n, k) a_{t|t-1}, keep=True only
    pred_cov: np.ndarray | None = None  # (S, n, k, k)
    filt_mean: np.ndarray | None = None  # (S, n, k) a_{t|t}
    filt_cov: np.ndarray | None = None  # (S, n, k, k)


def _sym(m: np.ndarray) -> np.ndarray:
    return 0.5 * (m + np.swapaxes(m, -1, -2))


def _quad(a: np.ndarray, p: np.ndarray) -> np.ndarray:
    """``a p a'`` over the leading draw axis."""
    return _sym(a @ p @ np.swapaxes(a, -1, -2))


def _sqrt_psd(m: np.ndarray) -> np.ndarray:
    """A square root ``L`` with ``L L' = m`` (Cholesky; eigen-decomposition if it fails)."""
    k = m.shape[-1]
    sym = _sym(m)
    try:
        return np.asarray(np.linalg.cholesky(sym + JITTER * np.eye(k)))
    except np.linalg.LinAlgError:
        w, v = np.linalg.eigh(sym)
        return np.asarray(v * np.sqrt(np.clip(w, 0.0, None))[..., None, :])


def kalman_filter(
    sys: StructuralSystem,
    y: np.ndarray,
    d: np.ndarray | None = None,
    *,
    start: tuple[np.ndarray, np.ndarray] | None = None,
    keep: bool = False,
) -> FilterResult:
    """Run the Kalman filter over ``y`` (``(n,)``, standardised, NaN = missing) for every draw.

    ``d`` (``(S, n)``) is the regression term added to the observation. ``start`` is the
    filtered state ``(mean (S, k), cov (S, k, k))`` one step before ``y[0]`` (to continue a
    fit); without it ``(x0, P0)`` is the predicted state of ``y[0]``. ``keep=True`` stores
    the per-step predicted and filtered moments (needed by :func:`rts_smoother`).
    """
    y = np.asarray(y, dtype=float).reshape(-1)
    s_len, k, n = sys.S, sys.k, y.size
    T, Z, RQR, H = sys.T, sys.Z, sys.RQR, sys.H
    Tt = np.swapaxes(T, -1, -2)
    if start is None:
        a = np.array(sys.x0, dtype=float)
        P = np.array(sys.P0, dtype=float)
    else:
        a = np.einsum("sij,sj->si", T, start[0])
        P = _quad(T, start[1]) + RQR
    dd = np.zeros((s_len, n)) if d is None else np.asarray(d, dtype=float)
    mean = np.empty((s_len, n))
    var = np.empty((s_len, n))
    loglik = np.zeros(s_len)
    pred_mean = np.empty((s_len, n, k)) if keep else None
    pred_cov = np.empty((s_len, n, k, k)) if keep else None
    filt_mean = np.empty((s_len, n, k)) if keep else None
    filt_cov = np.empty((s_len, n, k, k)) if keep else None
    eye = np.eye(k)
    a_f, P_f = a, P
    if start is not None and n == 0:
        a_f, P_f = np.asarray(start[0], dtype=float), np.asarray(start[1], dtype=float)
    for t in range(n):
        if keep:
            assert pred_mean is not None and pred_cov is not None
            pred_mean[:, t] = a
            pred_cov[:, t] = P
        y_hat = np.einsum("sk,sk->s", Z, a)
        PZ = np.einsum("sij,sj->si", P, Z)  # P Z'
        F = np.einsum("sk,sk->s", Z, PZ) + H + _COV_JITTER
        mean[:, t] = y_hat
        var[:, t] = F
        if np.isfinite(y[t]):
            v = y[t] - y_hat - dd[:, t]
            K = PZ / F[:, None]
            a_f = a + K * v[:, None]
            IKZ = eye - K[:, :, None] * Z[:, None, :]
            P_f = _quad(IKZ, P) + (K[:, :, None] * K[:, None, :]) * H[:, None, None]
            loglik += -0.5 * (_LOG_2PI + np.log(F) + v * v / F)
        else:
            a_f, P_f = a, P
        P_f = P_f + _COV_JITTER * eye
        if keep:
            assert filt_mean is not None and filt_cov is not None
            filt_mean[:, t] = a_f
            filt_cov[:, t] = P_f
        a = np.einsum("sij,sj->si", T, a_f)
        P = _sym(T @ P_f @ Tt) + RQR
    if n == 0 and start is None:
        a_f, P_f = np.array(sys.x0, dtype=float), np.array(sys.P0, dtype=float)
    return FilterResult(
        mean=mean,
        var=var,
        last_mean=np.asarray(a_f),
        last_cov=np.asarray(P_f),
        loglik=loglik,
        pred_mean=pred_mean,
        pred_cov=pred_cov,
        filt_mean=filt_mean,
        filt_cov=filt_cov,
    )


def rts_smoother(sys: StructuralSystem, fr: FilterResult) -> np.ndarray:
    """Smoothed state means ``E[x_t | y_{1..n}]`` ``(S, n, k)`` (Rauch-Tung-Striebel).

    Needs a :class:`FilterResult` from ``kalman_filter(..., keep=True)``.
    """
    if fr.filt_mean is None or fr.filt_cov is None or fr.pred_mean is None or fr.pred_cov is None:
        raise ValueError("rts_smoother needs kalman_filter(..., keep=True)")
    n = fr.filt_mean.shape[1]
    out = np.empty_like(fr.filt_mean)
    if n == 0:
        return out
    T = sys.T
    out[:, -1] = fr.filt_mean[:, -1]
    for t in range(n - 2, -1, -1):
        P_f = fr.filt_cov[:, t]
        # J = P_f T' P_pred^+ ; pinv keeps singular predictions (no state noise) well defined
        P_pred_inv = np.linalg.pinv(_sym(fr.pred_cov[:, t + 1]), hermitian=True)
        J = P_f @ np.swapaxes(T, -1, -2) @ P_pred_inv
        out[:, t] = fr.filt_mean[:, t] + np.einsum(
            "sij,sj->si", J, out[:, t + 1] - fr.pred_mean[:, t + 1]
        )
    return out


def crn_normal(seed: int, name: str, shape: tuple[int, ...]) -> np.ndarray:
    """Standard normals keyed by ``(seed, name)``: the same key gives the same draws."""
    digest = hashlib.sha256(name.encode()).digest()
    rng = np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])
    return rng.standard_normal(shape)


def one_step_draws(fr: FilterResult, d: np.ndarray | None, *, seed: int, name: str) -> np.ndarray:
    """Predictive draws ``(S, n)`` of the one-step-ahead predictions ``mean + d + sqrt(var) z``."""
    loc = fr.mean if d is None else fr.mean + d
    z = crn_normal(seed, name, fr.mean.shape)
    return np.asarray(loc + np.sqrt(fr.var) * z)


def simulate_ahead(
    sys: StructuralSystem,
    mean: np.ndarray,
    cov: np.ndarray,
    h: int,
    d: np.ndarray | None,
    *,
    seed: int,
    name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Expected values and predictive draws ``(S, h)`` of ``h`` steps after a filtered state.

    ``(mean, cov)`` is the filtered state ``a_{n|n}``/``P_{n|n}``; step ``j`` (1-based) has the
    expected value ``Z T^j mean + d_j``. The predictive path draws the start state from
    ``N(mean, cov)``, then ``x_{t+1} = T x_t + chol(RQR) z_t`` and ``y = Z x + d + sqrt(H) e``.
    The normals are generated horizon-major, so a longer horizon repeats the first steps of
    a shorter one with the same ``(seed, name)``.
    """
    s_len, k = sys.S, sys.k
    dd = np.zeros((s_len, h)) if d is None else np.asarray(d, dtype=float).reshape(s_len, h)
    T, Z = sys.T, sys.Z
    expected = np.empty((s_len, h))
    predictive = np.empty((s_len, h))
    L_state = _sqrt_psd(sys.RQR)
    L0 = _sqrt_psd(np.asarray(cov, dtype=float))
    z0 = crn_normal(seed, f"{name}:state0", (s_len, k))
    zs = crn_normal(seed, f"{name}:state", (h, s_len, k))
    eo = crn_normal(seed, f"{name}:obs", (h, s_len))
    m = np.asarray(mean, dtype=float)
    x = m + np.einsum("sij,sj->si", L0, z0)
    obs_sd = np.sqrt(np.clip(sys.H, 0.0, None))
    for j in range(h):
        m = np.einsum("sij,sj->si", T, m)
        x = np.einsum("sij,sj->si", T, x) + np.einsum("sij,sj->si", L_state, zs[j])
        expected[:, j] = np.einsum("sk,sk->s", Z, m) + dd[:, j]
        predictive[:, j] = np.einsum("sk,sk->s", Z, x) + dd[:, j] + obs_sd * eo[j]
    return expected, predictive
