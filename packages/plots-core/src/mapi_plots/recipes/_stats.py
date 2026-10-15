"""Small numpy implementations of the statistics the recipes plot.

* rank-normalised split R-hat and bulk/tail ESS (Vehtari, Gelman, Simpson, Carpenter and
  Bürkner 2021), matching ArviZ's ``rhat(method="rank")`` and ``ess(method="bulk"|"tail")``;
* HDI / ETI intervals, a Gaussian KDE (Silverman bandwidth), PIT values, central-interval
  coverage and WAIC (with pointwise values for paired standard errors).

They exist so the recipes need only numpy (no scipy/ArviZ in the ``recipes`` extra).
"""

from __future__ import annotations

from statistics import NormalDist

import numpy as np

__all__ = [
    "coverage_curve",
    "ess_bulk",
    "ess_tail",
    "eti",
    "hdi",
    "kde",
    "pit_values",
    "rhat",
    "waic",
]

_ppf = np.vectorize(NormalDist().inv_cdf, otypes=[float])


def _rankdata(x: np.ndarray) -> np.ndarray:
    """Average ranks (1-based) of a flat array, ties averaged (like scipy's ``rankdata``)."""
    flat = x.ravel()
    order = np.argsort(flat, kind="mergesort")
    sorted_vals = flat[order]
    new = np.r_[True, sorted_vals[1:] != sorted_vals[:-1]]
    dense = np.cumsum(new)
    counts = np.bincount(dense)
    ends = np.cumsum(counts)
    avg = ends[dense] - (counts[dense] - 1) / 2.0
    ranks = np.empty_like(avg, dtype=float)
    ranks[order] = avg
    return ranks.reshape(x.shape)


def _z_scale(x: np.ndarray) -> np.ndarray:
    ranks = _rankdata(x)
    return _ppf((ranks - 3 / 8) / (x.size + 1 / 4))  # type: ignore[no-any-return]


def _split_chains(x: np.ndarray) -> np.ndarray:
    half = x.shape[1] // 2
    return np.concatenate([x[:, :half], x[:, -half:]], axis=0)


def _rhat_basic(x: np.ndarray) -> float:
    n = x.shape[1]
    chain_mean = x.mean(axis=1)
    chain_var = x.var(axis=1, ddof=1)
    between = n * np.var(chain_mean, ddof=1)
    within = np.mean(chain_var)
    if within == 0:
        return float("nan")
    return float(np.sqrt((between / within + n - 1) / n))


def _valid(x: np.ndarray) -> bool:
    return x.ndim == 2 and x.shape[1] >= 4 and bool(np.all(np.isfinite(x)))


def rhat(x: np.ndarray) -> float:
    """Rank-normalised split R-hat of a ``(chain, draw)`` array (max of bulk and tail)."""
    x = np.asarray(x, dtype=float)
    if not _valid(x) or np.ptp(x) == 0:
        return float("nan")
    bulk = _rhat_basic(_z_scale(_split_chains(x)))
    folded = np.abs(x - np.median(x))
    tail = _rhat_basic(_z_scale(_split_chains(folded)))
    return float(max(bulk, tail))


def _autocov(x: np.ndarray) -> np.ndarray:
    n = x.shape[1]
    centred = x - x.mean(axis=1, keepdims=True)
    m = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(centred, n=m, axis=1)
    acov = np.fft.irfft(f * np.conjugate(f), n=m, axis=1)[:, :n]
    return acov / n  # type: ignore[no-any-return]


def _ess(x: np.ndarray) -> float:
    chains, n = x.shape
    acov = _autocov(x)
    chain_mean = x.mean(axis=1)
    mean_var = np.mean(acov[:, 0]) * n / (n - 1.0)
    var_plus = mean_var * (n - 1.0) / n
    if chains > 1:
        var_plus += np.var(chain_mean, ddof=1)
    if var_plus == 0:
        return float("nan")
    rho = np.zeros(n)
    rho_even = 1.0
    rho[0] = rho_even
    rho_odd = 1.0 - (mean_var - np.mean(acov[:, 1])) / var_plus
    rho[1] = rho_odd
    t = 1
    while t < n - 3 and (rho_even + rho_odd) > 0.0:
        rho_even = 1.0 - (mean_var - np.mean(acov[:, t + 1])) / var_plus
        rho_odd = 1.0 - (mean_var - np.mean(acov[:, t + 2])) / var_plus
        if (rho_even + rho_odd) >= 0:
            rho[t + 1] = rho_even
            rho[t + 2] = rho_odd
        t += 2
    max_t = t - 2
    if rho_even > 0:
        rho[max_t + 1] = rho_even
    t = 1
    while t <= max_t - 2:  # Geyer's initial monotone sequence
        if (rho[t + 1] + rho[t + 2]) > (rho[t - 1] + rho[t]):
            rho[t + 1] = (rho[t - 1] + rho[t]) / 2.0
            rho[t + 2] = rho[t + 1]
        t += 2
    total = chains * n
    tau = -1.0 + 2.0 * np.sum(rho[: max_t + 1]) + np.sum(rho[max_t + 1 : max_t + 2])
    tau = max(tau, 1.0 / np.log10(total))
    return float(total / tau)


def ess_bulk(x: np.ndarray) -> float:
    """Bulk effective sample size of a ``(chain, draw)`` array."""
    x = np.asarray(x, dtype=float)
    if not _valid(x) or np.ptp(x) == 0:
        return float("nan")
    return _ess(_z_scale(_split_chains(x)))


def ess_tail(x: np.ndarray, prob: float = 0.05) -> float:
    """Tail ESS: the smaller ESS of the ``prob`` and ``1 - prob`` quantile indicators."""
    x = np.asarray(x, dtype=float)
    if not _valid(x) or np.ptp(x) == 0:
        return float("nan")
    lo, hi = np.quantile(x, [prob, 1 - prob])
    out = []
    for ind in (x <= lo, x <= hi):
        split = _split_chains(ind.astype(float))
        out.append(_ess(split) if np.ptp(split) > 0 else float("nan"))
    return float(np.nanmin(out)) if not all(np.isnan(out)) else float("nan")


def hdi(samples: np.ndarray, prob: float, axis: int = -1) -> tuple[np.ndarray, np.ndarray]:
    """Narrowest interval holding ``prob`` of the samples along ``axis``."""
    if not 0 < prob < 1:
        raise ValueError("prob must be in (0, 1)")
    x = np.sort(np.moveaxis(np.asarray(samples, dtype=float), axis, -1), axis=-1)
    n = x.shape[-1]
    k = max(1, min(n - 1, int(np.floor(prob * n))))
    widths = x[..., k:] - x[..., : n - k]
    best = np.argmin(widths, axis=-1)[..., None]
    lo = np.take_along_axis(x[..., : n - k], best, axis=-1)[..., 0]
    hi = np.take_along_axis(x[..., k:], best, axis=-1)[..., 0]
    return lo, hi


def eti(samples: np.ndarray, prob: float, axis: int = -1) -> tuple[np.ndarray, np.ndarray]:
    """Equal-tailed interval (central quantiles)."""
    a = (1 - prob) / 2
    lo, hi = np.nanquantile(np.asarray(samples, dtype=float), [a, 1 - a], axis=axis)
    return lo, hi


def kde(
    values: np.ndarray, grid: np.ndarray | None = None, points: int = 200
) -> tuple[np.ndarray, np.ndarray]:
    """Gaussian KDE with Silverman's bandwidth; returns ``(grid, density)``."""
    x = np.asarray(values, dtype=float).ravel()
    x = x[np.isfinite(x)]
    if x.size == 0:
        g = np.linspace(0, 1, points) if grid is None else grid
        return g, np.zeros_like(g)
    sd = np.std(x, ddof=1) if x.size > 1 else 0.0
    iqr = np.subtract(*np.percentile(x, [75, 25]))
    spread = min(sd, iqr / 1.34) if iqr > 0 else sd
    bw = 0.9 * spread * x.size ** (-0.2) if spread > 0 else max(abs(x[0]) * 0.05, 1e-3)
    if grid is None:
        grid = np.linspace(x.min() - 3 * bw, x.max() + 3 * bw, points)
    if x.size > 2000:  # bin to keep the cost bounded; exact enough for display
        counts, edges = np.histogram(x, bins=2000)
        centres = (edges[:-1] + edges[1:]) / 2
        weights = counts / counts.sum()
        z = (grid[:, None] - centres[None, :]) / bw
        dens = (np.exp(-0.5 * z**2) * weights[None, :]).sum(axis=1) / (bw * np.sqrt(2 * np.pi))
    else:
        z = (grid[:, None] - x[None, :]) / bw
        dens = np.exp(-0.5 * z**2).sum(axis=1) / (x.size * bw * np.sqrt(2 * np.pi))
    return grid, dens


def pit_values(draws: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """PIT of each observation: P(pred < y) + 0.5 P(pred == y). ``draws`` is ``(S, N)``."""
    d = np.asarray(draws, dtype=float)
    y = np.asarray(observed, dtype=float)
    less = (d < y[None, :]).mean(axis=0)
    equal = (d == y[None, :]).mean(axis=0)
    return less + 0.5 * equal  # type: ignore[no-any-return]


def coverage_curve(
    draws: np.ndarray, observed: np.ndarray, levels: list[float]
) -> list[tuple[float, float]]:
    """Empirical coverage of central intervals at each nominal level. ``draws`` is ``(S, N)``."""
    out = []
    for level in levels:
        lo, hi = eti(draws, level, axis=0)
        inside = (observed >= lo) & (observed <= hi)
        out.append((float(level), float(np.mean(inside))))
    return out


def waic(log_lik: np.ndarray) -> tuple[float, float, float, np.ndarray]:
    """WAIC on the elpd scale from ``(S, N)`` pointwise log-likelihood.

    Returns ``(elpd_waic, se, p_waic, pointwise_elpd)``.
    """
    ll = np.asarray(log_lik, dtype=float)
    m = ll.max(axis=0)
    lppd = m + np.log(np.mean(np.exp(ll - m[None, :]), axis=0))
    p = np.var(ll, axis=0, ddof=1)
    pointwise = lppd - p
    n = pointwise.size
    se = float(np.sqrt(n * np.var(pointwise))) if n > 1 else float("nan")
    return float(pointwise.sum()), se, float(p.sum()), pointwise
