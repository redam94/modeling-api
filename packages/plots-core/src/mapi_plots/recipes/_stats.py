"""Small numpy implementations of the statistics the recipes plot.

* rank-normalised split R-hat and bulk/tail ESS (Vehtari, Gelman, Simpson, Carpenter and
  Bürkner 2021), matching ArviZ's ``rhat(method="rank")`` and ``ess(method="bulk"|"tail")``;
* HDI / ETI intervals, a Gaussian KDE (Silverman bandwidth), PIT values, central-interval
  coverage and WAIC (with pointwise values for paired standard errors);
* Pareto smoothed importance sampling (PSIS, Vehtari, Simpson, Gelman, Yao and Gabry 2024)
  with the Pareto k diagnostic, matching ArviZ's ``psislw`` with ``r_eff = 1``;
* the simultaneous envelope of an ECDF difference plot (Säilynoja, Bürkner and Vehtari 2022).

They exist so the recipes need only numpy (no scipy/ArviZ in the ``recipes`` extra).
"""

from __future__ import annotations

from statistics import NormalDist

import numpy as np

__all__ = [
    "coverage_curve",
    "ecdf_envelope",
    "ess_bulk",
    "ess_tail",
    "eti",
    "hdi",
    "kde",
    "pit_values",
    "psis",
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
    z: np.ndarray = _ppf((ranks - 3 / 8) / (x.size + 1 / 4))
    return z


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
    out: np.ndarray = acov / n
    return out


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
    pit: np.ndarray = less + 0.5 * equal
    return pit


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


def ecdf_envelope(
    n: int, z: np.ndarray, prob: float, n_sim: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Simultaneous ``prob`` envelope of ``ECDF(z) - z`` for ``n`` uniform values.

    Simulated: the ECDF counts at the grid ``z`` of ``n`` uniform values are cumulative
    multinomial counts. The band is ``±c sqrt(z (1 - z) / n)`` where ``c`` is the ``prob``
    quantile of the largest standardised deviation over the grid (so all points stay inside
    together with probability ``prob``).
    """
    rng = np.random.default_rng(seed)
    edges = np.r_[0.0, z, 1.0]
    probs = np.diff(edges)
    counts = rng.multinomial(n, probs, size=n_sim)[:, :-1].cumsum(axis=1)
    sd = np.sqrt(z * (1 - z) / n)
    dev = np.abs(counts / n - z[None, :]) / sd[None, :]
    c = float(np.quantile(dev.max(axis=1), prob))
    return -c * sd, c * sd


def _logsumexp(x: np.ndarray) -> float:
    m = float(np.max(x))
    return m + float(np.log(np.sum(np.exp(x - m))))


def _gpdfit(x: np.ndarray) -> tuple[float, float]:
    """``(k, sigma)`` of a generalized Pareto fit to sorted exceedances ``x`` (Zhang and
    Stephens 2009, with the weakly informative prior on k of Vehtari et al. 2024)."""
    prior_bs, prior_k = 3, 10
    n = x.size
    m_est = 30 + int(n**0.5)
    xstar = x[int(n / 4 + 0.5) - 1]  # first quartile
    if xstar <= x[0]:
        return float("nan"), float("nan")
    b = 1 - np.sqrt(m_est / (np.arange(1, m_est + 1, dtype=float) - 0.5))
    b /= prior_bs * xstar
    b += 1 / x[-1]
    k_ary = np.mean(np.log1p(-b[:, None] * x[None, :]), axis=1)
    len_scale = n * (np.log(-b / k_ary) - k_ary - 1)
    w = np.exp(len_scale - _logsumexp(len_scale))
    real = w >= 10 * np.finfo(float).eps
    w, b = w[real], b[real]
    w /= w.sum()
    b_post = float(np.sum(b * w))
    k = float(np.mean(np.log1p(-b_post * x)))
    sigma = -k / b_post
    k = (n * k + prior_k * 0.5) / (n + prior_k)
    if np.isnan(k):
        return float("inf"), float("nan")
    return k, sigma


def _gpinv(p: np.ndarray, k: float, sigma: float, mu: float) -> np.ndarray:
    """Quantiles of a generalized Pareto distribution."""
    if not sigma > 0:
        return np.full_like(p, np.nan)
    if k == 0:
        q: np.ndarray = mu - sigma * np.log1p(-p)
    else:
        q = mu + sigma * np.expm1(-k * np.log1p(-p)) / k
    return q


def psis(log_ratios: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pareto smoothed importance sampling of ``(S, N)`` log importance ratios.

    For leave-one-out, ``log_ratios = -log_lik``. Returns ``(log_weights, k)``: the smoothed
    log weights, normalised per column (``exp`` sums to 1), and the Pareto k of each column.
    The largest ``M = floor(min(0.2 S, 3 sqrt(S)))`` ratios (the tail length of ArviZ and
    loo) are replaced by the expected order statistics of a generalized Pareto fit, truncated
    at the largest raw ratio. k is ``inf`` when the tail is too short to fit (fewer than 5
    draws) or the fit fails.
    """
    lr = np.asarray(log_ratios, dtype=float)
    if lr.ndim == 1:
        lr = lr[:, None]
    s, n = lr.shape
    m = int(np.floor(min(0.2 * s, 3 * np.sqrt(s))))
    lw = lr - lr.max(axis=0, keepdims=True)
    k = np.full(n, np.inf)
    for i in range(n):
        col = lw[:, i]
        if m >= 5 and s > m and np.all(np.isfinite(col)):
            order = np.argsort(col, kind="mergesort")
            tail_ids = order[s - m :]
            tail = col[tail_ids]
            cutoff = col[order[s - m - 1]]
            if np.ptp(tail) > np.finfo(float).tiny:
                ki, sigma = _gpdfit(np.exp(tail) - np.exp(cutoff))
                if np.isfinite(ki):
                    smoothed = _gpinv((np.arange(m) + 0.5) / m, ki, sigma, float(np.exp(cutoff)))
                    if np.all(smoothed > 0):
                        col[tail_ids] = np.minimum(np.log(smoothed), tail.max())
                    k[i] = ki
        if np.all(np.isfinite(col)):
            col -= _logsumexp(col)
    return lw, k
