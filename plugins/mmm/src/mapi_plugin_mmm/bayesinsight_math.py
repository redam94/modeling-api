"""The curves, carry-over weights and design matrices of ``mmm.bayesinsight``.

The same functions build the PyMC graph (``xp=pytensor.tensor``) and evaluate it in NumPy
(tests, lift-test twins), so both always agree. The semantics are BayesInsight's
(``mapi_spec.bayesinsight_model`` documents them); where BayesInsight's code cannot run a
feature (Weibull and geometric carry-over, the S-shaped curve, linear media) the choice
made here is stated on the function.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any, Literal

import numpy as np

__all__ = [
    "adstock_weights",
    "bspline_basis",
    "deterministic",
    "fourier_features",
    "media_curve",
    "nonzero_index",
]

_EPS = 1e-12
CurveKind = Literal["hill", "linear", "sshaped"]
AdstockKind = Literal["delayed", "geometric", "weibull", "none"]


def deterministic(x: np.ndarray, form: str, params: Mapping[str, float] | None) -> np.ndarray:
    """BayesInsight's deterministic transforms (``linear``, ``log``, ``exp``, ``indexp``,
    ``power``) on data; missing values stay missing."""
    p = dict(params or {})
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if form == "linear":
            return np.asarray(x, dtype=np.float64)
        if form == "log":
            return np.log(x)
        if form == "exp":
            return 1.0 - np.exp(-x / p["alpha"])
        if form == "indexp":
            return 1.0 - np.exp(-x / p["mean"] * p["alpha"])
        if form == "power":
            return np.power(x, p["alpha"])
    raise ValueError(f"unknown deterministic transform {form!r}")  # pragma: no cover


def nonzero_index(series: np.ndarray, how: Literal["median", "mean"]) -> np.ndarray:
    """Per cell (every axis but the last, time): the median or mean of the positive values
    over time - BayesInsight's media index. Cells without a positive value get 1."""
    pos = np.where(series > 0, series, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN cells
        out = np.nanmedian(pos, axis=-1) if how == "median" else np.nanmean(pos, axis=-1)
    return np.where(np.isfinite(out) & (out > 0), out, 1.0)


def media_curve(
    z: Any,
    kind: CurveKind,
    params: Mapping[str, Any],
    *,
    xp: Any = np,
) -> Any:
    """The response curve of indexed media ``z`` (``z >= 0``):

    * ``hill`` - ``z^n / (K^n + z^n)`` (BayesInsight's ``hill`` on ``x / index``);
    * ``linear`` - ``z`` (BayesInsight's ``linear`` cannot take the index it is called with;
      the indexed series keeps the default coefficient prior meaningful);
    * ``sshaped`` - ``(b^(alpha^(100 z)) - b) / (1 - b)`` with ``b = beta / 1e8`` and ``z``
      relative to the series' fit-window maximum: BayesInsight's ``saturation`` curve with
      ``maxper = 100`` (percent), anchored at zero so no spend has no effect.
    """
    if kind == "hill":
        k, n = params["K"], params["n"]
        zs = (z + _EPS) ** n
        return zs / (zs + k**n)
    if kind == "linear":
        return z
    if kind == "sshaped":
        log_a = xp.log(params["alpha"])
        log_b = xp.log(params["beta"]) - np.log(1e8)
        b = xp.exp(log_b)
        inner = xp.exp(100.0 * z * log_a)
        return (xp.exp(log_b * inner) - b) / (1.0 - b)
    raise ValueError(f"unknown media curve {kind!r}")  # pragma: no cover


def _weibull_logpdf(t: Any, lam: Any, k: Any, xp: Any) -> Any:
    return xp.log(k) - xp.log(lam) + (k - 1) * (xp.log(t) - xp.log(lam)) - (t / lam) ** k


def adstock_weights(
    kind: AdstockKind,
    l_max: int,
    params: Mapping[str, Any],
    *,
    weibull: Literal["PDF", "CDF"] = "PDF",
    xp: Any = np,
) -> Any:
    """Carry-over weights over lags ``0 .. l_max - 1``, normalised to sum to one (scalar
    parameters):

    * ``delayed`` - ``alpha^((l - theta)^2)`` (BayesInsight's ``delayed_adstock``);
    * ``geometric`` - ``alpha^l``;
    * ``weibull`` - pymc-marketing's weights (which BayesInsight copies): ``PDF``, the
      Weibull density at ``1 .. l_max`` scaled to ``[0, 1]``; ``CDF``, ``1`` then the
      products of the survival function ``1 - F(1), ...``;
    * ``none`` - no carry-over (``1, 0, 0, ...``).
    """
    lags = np.arange(l_max, dtype=np.float64)
    if kind == "none":
        w: Any = np.eye(1, l_max)[0]
        return xp.as_tensor_variable(w) if xp is not np else w
    if kind == "delayed":
        w = params["alpha"] ** ((lags - params["theta"]) ** 2)
    elif kind == "geometric":
        w = params["alpha"] ** lags
    elif kind == "weibull":
        lam, k = params["lam"], params["k"]
        t = lags + 1.0
        if weibull == "PDF":
            dens = xp.exp(_weibull_logpdf(t, lam, k, xp))
            lo, hi = dens.min(), dens.max()
            w = (dens - lo) / (hi - lo + _EPS)
        else:
            surv = xp.exp(-((t[:-1] / lam) ** k))  # 1 - F(1), ..., 1 - F(l_max - 1)
            first = xp.ones((1,)) if xp is not np else np.ones(1)
            w = xp.concatenate([first, xp.cumprod(surv)]) if l_max > 1 else first
    else:  # pragma: no cover - guarded by the settings model
        raise ValueError(f"unknown adstock {kind!r}")
    return w / (w.sum() + _EPS)


def fourier_features(positions: np.ndarray, n_fit: int, period: float, n: int) -> np.ndarray:
    """``(time, 2 n)`` features ``cos_1, sin_1, cos_2, ...`` at period ``positions``
    (0 = the first fit period), as BayesInsight computes them: ``t = linspace(0, 2 pi
    n_fit / period, n_fit)`` over the fit window, continued past it at the same step."""
    if n_fit < 2:
        raise ValueError("seasonality needs at least two fit periods")
    step = 2 * np.pi * n_fit / period / (n_fit - 1)
    t = np.asarray(positions, dtype=np.float64) * step
    cols = []
    for freq in range(1, n + 1):
        cols.append(np.cos(t * freq))
        cols.append(np.sin(t * freq))
    return np.stack(cols, axis=-1)


def fourier_labels(n: int) -> list[str]:
    return [f"{c}_{f}" for f in range(1, n + 1) for c in ("cos", "sin")]


def bspline_basis(t: np.ndarray, num_knots: int, order: int) -> np.ndarray:
    """``(len(t), num_knots + order + 1)`` B-spline basis on ``[0, 1]`` with ``num_knots``
    evenly spaced interior knots and degree ``order`` (patsy's ``bs(t, knots, degree,
    include_intercept=True)``, BayesInsight's spline matrix). ``t`` outside ``[0, 1]`` is
    clamped, so a forecast holds the trend's last fitted level."""
    tt = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    interior = np.linspace(0.0, 1.0, num_knots + 2)[1:-1]
    knots = np.concatenate([np.zeros(order + 1), interior, np.ones(order + 1)])
    n_basis = len(knots) - order - 1
    # degree-0 basis; the last non-empty interval is closed on the right
    basis = np.zeros((tt.size, len(knots) - 1))
    for i in range(len(knots) - 1):
        lo, hi = knots[i], knots[i + 1]
        if hi > lo:
            basis[:, i] = (tt >= lo) & (tt < hi)
    last = int(np.flatnonzero(np.diff(knots) > 0)[-1])
    basis[tt >= 1.0, last] = 1.0
    for d in range(1, order + 1):
        nxt = np.zeros((tt.size, len(knots) - 1 - d))
        for i in range(len(knots) - 1 - d):
            left = knots[i + d] - knots[i]
            right = knots[i + d + 1] - knots[i + 1]
            term = np.zeros(tt.size)
            if left > 0:
                term += (tt - knots[i]) / left * basis[:, i]
            if right > 0:
                term += (knots[i + d + 1] - tt) / right * basis[:, i + 1]
            nxt[:, i] = term
        basis = nxt
    assert basis.shape[1] == n_basis
    return basis
