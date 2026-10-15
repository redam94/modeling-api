"""Numerics of the Gaussian hidden Markov model (numpy filters and a pytensor likelihood).

Conventions (``S`` posterior samples, ``N`` sequences, ``T`` steps, ``K`` states)::

    pi     (S, K)       initial state distribution at the first fitted step
    A      (S, K, K)    A[s, i, j] = P(z_t = j | z_{t-1} = i)
    mu     (S, K)       emission means;  sigma (S, K) emission standard deviations
    y      (N, T)       observations;    mask (N, T) True where y is observed

The forward filter runs in probability space with per-step normalisation, which is exact
and numerically stable: ``predicted_t = filtered_{t-1} @ A`` (the one-step-ahead state
distribution), ``filtered_t ∝ predicted_t * N(y_t | mu, sigma)`` where ``y_t`` is observed,
and the log normalisers add up to the log-likelihood. Masked steps only propagate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytensor
import pytensor.tensor as pt

__all__ = [
    "FilterResult",
    "forward_filter",
    "gaussian_logpdf",
    "hmm_loglik",
    "propagate",
    "relabel_permutation",
    "stationary",
]

_LOG_2PI = math.log(2.0 * math.pi)


def gaussian_logpdf(y: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """Elementwise Normal log density (broadcasting)."""
    return -0.5 * _LOG_2PI - np.log(sigma) - 0.5 * ((y - mu) / sigma) ** 2


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Output of :func:`forward_filter` (state axes last)."""

    predicted: np.ndarray
    """``(S, N, T, K)``: ``P(z_t | y_{<t})``."""
    filtered: np.ndarray
    """``(S, N, T, K)``: ``P(z_t | y_{<=t})``."""
    loglik: np.ndarray
    """``(S, N)``: ``log p(y)`` of each sequence (observed steps only)."""


def forward_filter(
    prior: np.ndarray,
    A: np.ndarray,
    mu: np.ndarray,
    sigma: np.ndarray,
    y: np.ndarray,
    mask: np.ndarray,
) -> FilterResult:
    """Run the forward filter from ``prior``, the state distribution at the first step.

    ``prior`` is ``(S, K)`` (shared by every sequence, e.g. ``pi``) or ``(S, N, K)`` (per
    sequence, e.g. a terminal state propagated to the first new step).
    """
    y = np.asarray(y, dtype=float)
    mask = np.asarray(mask, dtype=bool) & np.isfinite(y)
    n, t_len = y.shape
    s_len, k = mu.shape
    if prior.ndim == 2:
        prior = np.broadcast_to(prior[:, None, :], (s_len, n, k))
    predicted = np.empty((s_len, n, t_len, k))
    filtered = np.empty((s_len, n, t_len, k))
    loglik = np.zeros((s_len, n))
    yf = np.where(mask, y, 0.0)
    state = np.asarray(prior, dtype=float)
    for t in range(t_len):
        pred = state if t == 0 else np.einsum("snk,skj->snj", state, A)
        predicted[:, :, t] = pred
        logl = gaussian_logpdf(yf[None, :, t, None], mu[:, None, :], sigma[:, None, :])
        top = logl.max(axis=-1, keepdims=True)
        lik = np.exp(logl - top)
        post = pred * lik
        norm = post.sum(axis=-1, keepdims=True)
        obs = mask[None, :, t, None]
        new = np.where(obs, post / np.where(norm > 0, norm, 1.0), pred)
        loglik += np.where(mask[None, :, t], np.log(norm[..., 0]) + top[..., 0], 0.0)
        filtered[:, :, t] = new
        state = new
    return FilterResult(predicted, filtered, loglik)


def propagate(alpha: np.ndarray, A: np.ndarray, steps: np.ndarray) -> np.ndarray:
    """``alpha @ A^h`` for every ``h`` in ``steps`` (non-negative ints).

    ``alpha`` is ``(S, ..., K)``; the result is ``(S, ..., H, K)``.
    """
    steps = np.asarray(steps, dtype=np.int64)
    if steps.size and steps.min() < 0:
        raise ValueError("propagation steps must be non-negative")
    out = np.empty((*alpha.shape[:-1], steps.size, alpha.shape[-1]))
    if steps.size == 0:
        return out
    order = np.argsort(steps, kind="stable")
    cur = np.asarray(alpha, dtype=float)
    done = 0
    for pos in order:
        h = int(steps[pos])
        while done < h:
            cur = np.einsum("s...k,skj->s...j", cur, A)
            done += 1
        out[..., pos, :] = cur
    return out


def stationary(A: np.ndarray) -> np.ndarray:
    """Stationary distribution of each transition matrix (``(S, K)``)."""
    k = A.shape[-1]
    vals, vecs = np.linalg.eig(np.swapaxes(A, -1, -2))
    idx = np.argmin(np.abs(vals - 1.0), axis=-1)
    v = np.real(np.take_along_axis(vecs, idx[..., None, None], axis=-1))[..., 0]
    v = np.abs(v)
    return v / v.sum(axis=-1, keepdims=True).clip(min=1e-300) if k else v


def relabel_permutation(mu: np.ndarray) -> np.ndarray:
    """Per-sample permutation sorting the state means ascending (``(S, K)`` indices)."""
    return np.argsort(mu, axis=-1, kind="stable")


def hmm_loglik(
    log_pi: Any,
    log_A: Any,
    mu: Any,
    sigma: Any,
    y: np.ndarray,
    mask: np.ndarray,
) -> Any:
    """Total log-likelihood of ``N`` sequences as a pytensor graph (log-space forward pass).

    The step function receives every parameter explicitly (``non_sequences``) so that PyMC
    can replace random variables by their value variables inside the scan.
    """
    m = np.asarray(mask, dtype=bool) & np.isfinite(y)
    yf = pt.as_tensor_variable(np.where(m, y, 0.0).T)  # (T, N)
    mf = pt.as_tensor_variable(m.T.astype("float64"))

    def emission(yt: Any, mt: Any, mu_: Any, sigma_: Any) -> Any:
        lp = -0.5 * _LOG_2PI - pt.log(sigma_) - 0.5 * ((yt[:, None] - mu_) / sigma_) ** 2
        return lp * mt[:, None]

    first = log_pi[None, :] + emission(yf[0], mf[0], mu, sigma)
    if y.shape[1] == 1:
        return pt.logsumexp(first, axis=1).sum()

    def step(yt: Any, mt: Any, prev: Any, mu_: Any, sigma_: Any, log_a: Any) -> Any:
        return pt.logsumexp(prev[:, :, None] + log_a[None], axis=1) + emission(yt, mt, mu_, sigma_)

    seq = pytensor.scan(
        step,
        sequences=[yf[1:], mf[1:]],
        outputs_info=[first],
        non_sequences=[mu, sigma, log_A],
        return_updates=False,
    )
    return pt.logsumexp(seq[-1], axis=1).sum()
