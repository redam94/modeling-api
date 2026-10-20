"""Draws from a trained network: last-layer Laplace or MC dropout.

**Last-layer Laplace** (default). The head is ``logits = x~ @ W~`` with ``x~ = [phi, 1]`` the
penultimate features plus a bias column and ``W~`` of shape ``(F + 1, K)``; the parameter
vector is ``theta = W~.reshape(-1)`` (index ``f * K + k``). With the trained head as the mean,
the posterior precision is ``H + lambda I`` where ``H`` is the generalised Gauss-Newton
Hessian of the training loss on at most ``max_rows`` training rows:

    classification   H = sum_n kron(x~_n x~_n^T, diag(p_n) - p_n p_n^T)
    regression       H = X~^T X~ / sigma^2          (sigma^2: residual variance)

``hessian: full`` keeps the whole ``(P, P)`` matrix (``auto`` when ``P <= 2048``) and stores
the lower Cholesky factor of the covariance (``chol``); ``diag`` keeps its diagonal and stores
the variances (``var``). The prior precision ``lambda`` maximises the Laplace evidence
``-lambda |theta|^2 / 2 + P log(lambda) / 2 - logdet(H + lambda I) / 2`` on a log grid
``1e-4 .. 1e4`` unless given. Draw ``d`` is ``theta + L z_d`` with ``z_d`` from
``default_rng([seed, 101, d])``, so any subset of draws is reproducible on its own.

**MC dropout**: draw ``d`` is a forward pass with the dropout key ``fold_in(key(seed + 2), d)``.
**none**: one deterministic forward pass.

All draws are ``chain = 1``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np

__all__ = [
    "FULL_HESSIAN_MAX",
    "LaplacePosterior",
    "draw_positions",
    "fit_laplace",
    "gauss_newton",
    "laplace_logits",
    "laplace_thetas",
    "n_draws",
]

FULL_HESSIAN_MAX = 2048
_GRID = np.logspace(-4, 4, 33)
_CHUNK = 2048
_GGN_CELLS = 4 * 1024**2
"""Largest per-chunk work array of the full GGN (float64 values: 32 MiB)."""
_JITTER = 1e-10


@dataclass(frozen=True, slots=True)
class LaplacePosterior:
    mean: np.ndarray  # (P,)
    prior_precision: float
    hessian: Literal["full", "diag"]
    n_features: int  # F (without the bias column)
    n_out: int  # K
    chol: np.ndarray | None = None  # (P, P) lower Cholesky factor of the covariance
    var: np.ndarray | None = None  # (P,) variances

    def tensors(self) -> dict[str, np.ndarray]:
        """The ``laplace.safetensors`` tensors (float64)."""
        out = {"mean": self.mean.astype(np.float64)}
        if self.chol is not None:
            out["chol"] = self.chol.astype(np.float64)
        if self.var is not None:
            out["var"] = self.var.astype(np.float64)
        return out

    @classmethod
    def from_tensors(
        cls, tensors: Mapping[str, np.ndarray], *, prior_precision: float, n_out: int
    ) -> LaplacePosterior:
        mean = np.asarray(tensors["mean"], dtype=np.float64)
        chol = tensors.get("chol")
        var = tensors.get("var")
        if (chol is None) == (var is None):
            raise ValueError("a Laplace posterior holds either chol or var")
        p = mean.size
        if p % n_out:
            raise ValueError("the Laplace mean does not match the number of outputs")
        return cls(
            mean=mean,
            prior_precision=float(prior_precision),
            hessian="full" if chol is not None else "diag",
            n_features=p // n_out - 1,
            n_out=n_out,
            chol=None if chol is None else np.asarray(chol, dtype=np.float64),
            var=None if var is None else np.asarray(var, dtype=np.float64),
        )


def n_draws(method: str, draws: int | None, fit_draws: int | None) -> int:
    """Draws per prediction: ``draws``, else the fit's draws (at most 1000), else 100;
    ``none`` gives one."""
    if method == "none":
        return 1
    if draws is not None:
        return draws
    if fit_draws is not None:
        return min(fit_draws, 1000)
    return 100


def draw_positions(n: int, draw_index: np.ndarray | None) -> np.ndarray:
    """The draws to return, ascending: all ``n``, or those a bundle thinning index names
    (pairs ``(chain, draw)`` with chain 0, or plain draw numbers). Ascending, so that when
    every draw is asked for (the harness passes the whole thinning index at score) position
    ``i`` of the output is draw ``i``, as ``select_draws`` expects."""
    if draw_index is None:
        return np.arange(n, dtype=np.int64)
    idx = np.asarray(draw_index)
    if idx.ndim == 2:
        idx = idx[idx[:, 0] == 0, 1]
    idx = idx.astype(np.int64).reshape(-1)
    idx = idx[(idx >= 0) & (idx < n)]
    out = np.unique(idx)
    return out if out.size else np.arange(n, dtype=np.int64)


def _augment(features: np.ndarray) -> np.ndarray:
    return np.concatenate([features, np.ones((features.shape[0], 1))], axis=1)


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return np.asarray(e / e.sum(axis=-1, keepdims=True))


def gauss_newton(
    features: np.ndarray,
    theta: np.ndarray,
    n_out: int,
    *,
    task: Literal["classification", "regression"],
    full: bool,
    noise_var: float = 1.0,
) -> np.ndarray:
    """The GGN Hessian (``(P, P)`` when ``full``, else its diagonal ``(P,)``)."""
    x = _augment(np.asarray(features, dtype=np.float64))
    f1 = x.shape[1]
    p = f1 * n_out
    if task == "regression":
        if full:
            return np.asarray((x.T @ x) / noise_var)
        return np.asarray((x**2).sum(axis=0) / noise_var)
    w = theta.reshape(f1, n_out)
    h = np.zeros((p, p)) if full else np.zeros((f1, n_out))
    # sum_n (x_n x_n^T) kron (diag(p_n) - p_n p_n^T), without the (n, F^2) outer products:
    # the diag(p) part is one F x F block per output, the p p^T part is G^T G with
    # G[n, (i, a)] = x_ni p_na; chunks keep G at most _GGN_CELLS values
    rows = max(1, min(_CHUNK, _GGN_CELLS // p)) if full else _CHUNK
    blocks = h.reshape(f1, n_out, f1, n_out) if full else None
    for a in range(0, x.shape[0], rows):
        xc = x[a : a + rows]
        prob = _softmax(xc @ w)
        if blocks is not None:
            for k in range(n_out):
                blocks[:, k, :, k] += (xc * prob[:, k : k + 1]).T @ xc
            g = (xc[:, :, None] * prob[:, None, :]).reshape(xc.shape[0], p)
            h -= g.T @ g
        else:
            h += (xc**2).T @ (prob - prob**2)
    return h if full else h.reshape(-1)


def _evidence(theta2: float, p: int, eig: np.ndarray, lam: float) -> float:
    return -0.5 * lam * theta2 + 0.5 * p * math.log(lam) - 0.5 * float(np.log(eig + lam).sum())


def fit_laplace(
    features: np.ndarray,
    kernel: np.ndarray,
    bias: np.ndarray,
    *,
    task: Literal["classification", "regression"],
    hessian: Literal["auto", "full", "diag"] = "auto",
    prior_precision: float | None = None,
    noise_var: float = 1.0,
) -> LaplacePosterior:
    """The last-layer Laplace posterior of a head ``(kernel (F, K), bias (K,))`` around its
    trained values, from the training rows' penultimate ``features`` ``(N, F)``."""
    k = int(kernel.shape[1])
    theta = np.concatenate([kernel, bias[None, :]], axis=0).astype(np.float64).reshape(-1)
    p = theta.size
    full = hessian == "full" or (hessian == "auto" and p <= FULL_HESSIAN_MAX)
    h = gauss_newton(features, theta, k, task=task, full=full, noise_var=noise_var)
    if full:
        h = 0.5 * (h + h.T)
        eig, vec = np.linalg.eigh(h)
        eig = np.clip(eig, 0.0, None)
    else:
        eig, vec = np.clip(h, 0.0, None), None
    theta2 = float(theta @ theta)
    if prior_precision is None:
        scores = [_evidence(theta2, p, eig, float(lam)) for lam in _GRID]
        lam = float(_GRID[int(np.argmax(scores))])
    else:
        lam = float(prior_precision)
    if full:
        assert vec is not None
        cov = (vec / (eig + lam)) @ vec.T
        cov = 0.5 * (cov + cov.T) + _JITTER * np.eye(p)
        chol = np.linalg.cholesky(cov)
        return LaplacePosterior(theta, lam, "full", features.shape[1], k, chol=chol)
    return LaplacePosterior(theta, lam, "diag", features.shape[1], k, var=1.0 / (eig + lam))


def laplace_thetas(post: LaplacePosterior, positions: np.ndarray, seed: int) -> np.ndarray:
    """``(D, P)`` parameter draws for the draw numbers ``positions``."""
    p = post.mean.size
    out = np.empty((positions.size, p))
    for i, d in enumerate(positions):
        z = np.random.default_rng([seed, 101, int(d)]).standard_normal(p)
        if post.chol is not None:
            out[i] = post.mean + post.chol @ z
        else:
            assert post.var is not None
            out[i] = post.mean + np.sqrt(post.var) * z
    return out


def laplace_logits(features: np.ndarray, thetas: np.ndarray, n_out: int) -> np.ndarray:
    """``(D, N, K)`` head outputs of ``features`` ``(N, F)`` for each parameter draw."""
    x = _augment(np.asarray(features, dtype=np.float64))
    w = thetas.reshape(thetas.shape[0], x.shape[1], n_out)
    return np.einsum("nf,dfk->dnk", x, w)
