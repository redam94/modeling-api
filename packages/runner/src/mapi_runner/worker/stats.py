"""Statistical building blocks of the explorers and the governance checks (M3.5).

Thin, validated wrappers over statsmodels and scipy that return the platform's JSON shapes
(:class:`~mapi_spec.stattests.StatTestResult`, lag profiles) with plain-language verdicts,
plus a few pieces statsmodels does not ship in the form we need: cross-correlation by lag
with the platform's sign convention, prewhitening, robust outlier scores, block
permutations, naive benchmark forecasts and the Diebold-Mariano test with the
Harvey-Leybourne-Newbold correction. Reference values are checked in
``tests/test_stats.py`` against statsmodels/scipy and hand computations.

Every function takes plain 1-D/2-D numpy arrays (NaNs are dropped where a test needs a
complete series) and never raises on degenerate input it can describe: it returns a
``status="skipped"`` result with the reason instead.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
from mapi_spec.stattests import TEST_DEFINITIONS, StatTestResult, make_test_result

__all__ = [
    "MIN_TEST_OBS",
    "Lagged",
    "acf_profile",
    "adf_test",
    "arch_lm_test",
    "block_permutation",
    "breusch_pagan_test",
    "ccf_profile",
    "condition_number",
    "diebold_mariano",
    "finite",
    "jarque_bera_test",
    "kpss_test",
    "ljung_box_test",
    "naive_forecasts",
    "outlier_scores",
    "pacf_profile",
    "prewhiten",
    "qq_points",
    "season_strengths",
    "skipped",
    "stationarity_verdict",
    "stl_decompose",
    "suggest_arma_orders",
    "vif_table",
]

MIN_TEST_OBS: Final = 8
"""Fewer complete observations than this and a test is skipped (it would mean nothing)."""


def finite(x: Any) -> np.ndarray:
    """``x`` as a 1-D float array without NaN/inf."""
    a = np.asarray(x, dtype=float).reshape(-1)
    out: np.ndarray = a[np.isfinite(a)]
    return out


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def skipped(
    test_id: str, why: str, *, var: str | None = None, alpha: float = 0.05
) -> StatTestResult:
    """A ``skipped`` result of ``test_id`` with the plain-language reason."""
    d = TEST_DEFINITIONS[test_id]
    return StatTestResult(
        test=test_id,
        var=var,
        alpha=alpha,
        reject=False,
        decision=d.keep_decision,
        verdict_plain=why[:500],
        null_plain=d.null_plain,
        status="skipped",
        message_plain=why[:500],
    )


def _p(p: float) -> float:
    return float(min(max(p, 0.0), 1.0))


# ------------------------------------------------------------------------- unit roots


def adf_test(
    x: Any,
    *,
    alpha: float = 0.05,
    regression: Literal["c", "ct", "ctt", "n"] = "c",
    autolag: Literal["AIC", "BIC", "t-stat"] | None = "AIC",
    maxlag: int | None = None,
    var: str | None = None,
) -> StatTestResult:
    """Augmented Dickey-Fuller (null: a unit root)."""
    from statsmodels.tsa.stattools import adfuller

    y = finite(x)
    if y.size < MIN_TEST_OBS + 4:
        return skipped("test.adf", "Too few values to test for a unit root.", var=var, alpha=alpha)
    if np.ptp(y) == 0:
        return skipped("test.adf", "The series is constant.", var=var, alpha=alpha)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)  # statsmodels' result-object switch
        res = adfuller(y, maxlag=maxlag, regression=regression, autolag=autolag)
    stat, p, lags, nobs, crit = res[0], res[1], res[2], res[3], res[4]
    return make_test_result(
        "test.adf",
        statistic=float(stat),
        p_value=_p(p),
        alpha=alpha,
        var=var,
        critical_values={k: float(v) for k, v in crit.items()},
        n_obs=int(nobs),
        lags=int(lags),
        details={"regression": regression},
    )


def kpss_test(
    x: Any,
    *,
    alpha: float = 0.05,
    regression: Literal["c", "ct"] = "c",
    nlags: str | int = "auto",
    var: str | None = None,
) -> StatTestResult:
    """KPSS (null: stationary). statsmodels interpolates p within ``[0.01, 0.1]``; values
    outside the table are clipped there (``details.p_value_bounded``)."""
    from statsmodels.tsa.stattools import kpss

    y = finite(x)
    if y.size < MIN_TEST_OBS + 4:
        return skipped("test.kpss", "Too few values to test stationarity.", var=var, alpha=alpha)
    if np.ptp(y) == 0:
        return skipped("test.kpss", "The series is constant.", var=var, alpha=alpha)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        stat, p, lags, crit = kpss(y, regression=regression, nlags=nlags)
    bounded = any("p-value" in str(w.message) for w in caught)
    return make_test_result(
        "test.kpss",
        statistic=float(stat),
        p_value=_p(p),
        alpha=alpha,
        var=var,
        critical_values={k: float(v) for k, v in crit.items()},
        n_obs=int(y.size),
        lags=int(lags),
        details={"regression": regression, "p_value_bounded": bounded},
    )


Verdict = Literal["stable", "difference", "unclear", "trend_stable"]


def stationarity_verdict(adf: StatTestResult, kpss: StatTestResult) -> tuple[Verdict, str]:
    """Combine ADF and KPSS (their nulls are opposite) into one plain verdict.

    * ADF rejects a unit root and KPSS keeps stationarity: ``stable``;
    * ADF keeps the unit root and KPSS rejects stationarity: ``difference``;
    * both reject: ``trend_stable`` when the KPSS was run around a trend, else
      ``unclear`` (often a trend or a break: look at the plot);
    * neither rejects: ``unclear`` (too little evidence either way)."""
    if adf.status != "ok" or kpss.status != "ok":
        return "unclear", "Not enough data to judge whether the series is stable."
    a, k = adf.reject, kpss.reject
    if a and not k:
        return "stable", "The series varies around a stable level: no differencing needed."
    if not a and k:
        return "difference", "The series wanders: difference it (use changes, not levels)."
    if a and k:
        if kpss.details.get("regression") == "ct":
            return "trend_stable", "The series is stable around a trend: model the trend."
        return (
            "unclear",
            "The tests disagree (often a trend or a break): look at the plot before deciding.",
        )
    return "unclear", "The tests cannot tell whether the series is stable (too little data?)."


# -------------------------------------------------------------------- residual tests


def ljung_box_test(
    x: Any, *, lags: int = 10, model_df: int = 0, alpha: float = 0.05, var: str | None = None
) -> StatTestResult:
    """Ljung-Box Q at ``lags`` (null: no autocorrelation up to that lag); ``model_df``
    removes the degrees of freedom of fitted ARMA terms."""
    from statsmodels.stats.diagnostic import acorr_ljungbox

    y = finite(x)
    lags = int(min(lags, max(1, y.size // 2 - 1)))
    if y.size < MIN_TEST_OBS or lags <= model_df:
        return skipped(
            "test.ljung_box", "Too few values to test for leftover patterns.", var=var, alpha=alpha
        )
    if np.ptp(y) == 0:
        return skipped("test.ljung_box", "The values are constant.", var=var, alpha=alpha)
    out = acorr_ljungbox(y, lags=[lags], model_df=model_df, return_df=True)
    stat = float(out["lb_stat"].iloc[0])
    p = float(out["lb_pvalue"].iloc[0])
    return make_test_result(
        "test.ljung_box",
        statistic=stat,
        p_value=_p(p),
        alpha=alpha,
        var=var,
        n_obs=int(y.size),
        lags=lags,
        details={"model_df": model_df},
    )


def jarque_bera_test(x: Any, *, alpha: float = 0.05, var: str | None = None) -> StatTestResult:
    """Jarque-Bera normality (null: normal)."""
    from scipy import stats

    y = finite(x)
    if y.size < MIN_TEST_OBS:
        return skipped(
            "test.jarque_bera", "Too few values to test the shape.", var=var, alpha=alpha
        )
    res = stats.jarque_bera(y)
    return make_test_result(
        "test.jarque_bera",
        statistic=float(res.statistic),
        p_value=_p(float(res.pvalue)),
        alpha=alpha,
        var=var,
        n_obs=int(y.size),
        details={"skew": _f(stats.skew(y)), "kurtosis": _f(stats.kurtosis(y))},
    )


def breusch_pagan_test(
    resid: Any, fitted: Any, *, alpha: float = 0.05, var: str | None = None
) -> StatTestResult:
    """Breusch-Pagan LM test of the residuals' spread against the fitted values (null:
    constant spread)."""
    from statsmodels.stats.diagnostic import het_breuschpagan

    r = np.asarray(resid, dtype=float).reshape(-1)
    f = np.asarray(fitted, dtype=float).reshape(-1)
    ok = np.isfinite(r) & np.isfinite(f)
    r, f = r[ok], f[ok]
    if r.size < MIN_TEST_OBS or np.ptp(f) == 0:
        return skipped(
            "test.breusch_pagan", "Too few values to test the spread.", var=var, alpha=alpha
        )
    exog = np.column_stack([np.ones_like(f), f])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        res = het_breuschpagan(r, exog)
    lm, lm_p = res[0], res[1]
    return make_test_result(
        "test.breusch_pagan",
        statistic=float(lm),
        p_value=_p(lm_p),
        alpha=alpha,
        var=var,
        n_obs=int(r.size),
    )


def arch_lm_test(
    x: Any, *, lags: int = 4, alpha: float = 0.05, var: str | None = None
) -> StatTestResult:
    """Engle's ARCH-LM test (null: no volatility clustering)."""
    from statsmodels.stats.diagnostic import het_arch

    y = finite(x)
    if y.size < MIN_TEST_OBS + lags or np.ptp(y) == 0:
        return skipped(
            "test.arch_lm", "Too few values to test volatility clustering.", var=var, alpha=alpha
        )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)  # statsmodels' result-object switch
        res = het_arch(y, nlags=lags)
    lm, lm_p = res[0], res[1]
    return make_test_result(
        "test.arch_lm",
        statistic=float(lm),
        p_value=_p(lm_p),
        alpha=alpha,
        var=var,
        n_obs=int(y.size),
        lags=lags,
    )


# ------------------------------------------------------------------- lag structure


@dataclass(frozen=True, slots=True)
class Lagged:
    """Values by lag with the 95% bound and the significant lags."""

    lags: list[int]
    values: list[float | None]
    bound: float | None
    significant: list[int]

    @property
    def peak_lag(self) -> int | None:
        best: tuple[float, int] | None = None
        for lag, v in zip(self.lags, self.values, strict=True):
            if v is None or lag == 0:
                continue
            if best is None or abs(v) > best[0]:
                best = (abs(v), lag)
        return None if best is None else best[1]


def _bound(n: int) -> float | None:
    return 1.96 / math.sqrt(n) if n > 0 else None


def _significant(
    lags: Sequence[int], values: Sequence[float | None], bound: float | None
) -> list[int]:
    if bound is None:
        return []
    return [
        k for k, v in zip(lags, values, strict=True) if k != 0 and v is not None and abs(v) > bound
    ]


def acf_profile(x: Any, nlags: int) -> Lagged:
    """Autocorrelations for lags ``0..nlags`` (statsmodels ``acf``, unadjusted)."""
    from statsmodels.tsa.stattools import acf

    y = finite(x)
    nlags = int(min(nlags, max(1, y.size - 2)))
    if y.size < 3 or np.ptp(y) == 0:
        return Lagged(list(range(nlags + 1)), [None] * (nlags + 1), None, [])
    vals = [_f(v) for v in acf(y, nlags=nlags, fft=True)]
    lags = list(range(len(vals)))
    b = _bound(y.size)
    return Lagged(lags, vals, b, _significant(lags, vals, b))


def pacf_profile(x: Any, nlags: int) -> Lagged:
    """Partial autocorrelations for lags ``0..nlags`` (Yule-Walker, bias-adjusted)."""
    from statsmodels.tsa.stattools import pacf

    y = finite(x)
    nlags = int(min(nlags, max(1, y.size // 2 - 1)))
    if y.size < 4 or np.ptp(y) == 0:
        return Lagged(list(range(nlags + 1)), [None] * (nlags + 1), None, [])
    vals = [_f(v) for v in pacf(y, nlags=nlags, method="ywadjusted")]
    lags = list(range(len(vals)))
    b = _bound(y.size)
    return Lagged(lags, vals, b, _significant(lags, vals, b))


def suggest_arma_orders(acf_: Lagged, pacf_: Lagged, *, max_order: int = 5) -> dict[str, int]:
    """Box-Jenkins rule of thumb: ``p`` is the last lag of the leading run of significant
    partial autocorrelations, ``q`` that of the autocorrelations (capped at ``max_order``)."""

    def cutoff(prof: Lagged) -> int:
        sig = set(prof.significant)
        k = 0
        while k + 1 <= max_order and (k + 1) in sig:
            k += 1
        return k

    return {"p": cutoff(pacf_), "q": cutoff(acf_)}


def _bartlett_ccf_bound(x: np.ndarray, y: np.ndarray) -> float | None:
    """The 95% bound of a cross-correlation of two *independent* series under Bartlett's
    formula, ``Var(r_k) ~ (1 / n) * sum_j rho_xx(j) rho_yy(j)``: ``1.96 / sqrt(n)`` when
    either series is white, much wider when both are persistent (a raw CCF of two
    smooth series is otherwise "significant" at half of its lags)."""
    ok = np.isfinite(x) & np.isfinite(y)
    a, b = x[ok], y[ok]
    n = int(a.size)
    if n < 3:
        return None
    m = int(min(n // 4, max(1, round(10 * math.log10(n)))))

    def acf(s: np.ndarray) -> np.ndarray:
        s = s - s.mean()
        den = float(np.dot(s, s))
        if den <= 0:
            return np.zeros(m + 1)
        return np.array([1.0] + [float(np.dot(s[j:], s[:-j])) / den for j in range(1, m + 1)])

    ra, rb = acf(a), acf(b)
    factor = 1.0 + 2.0 * float(np.dot(ra[1:], rb[1:]))
    return 1.96 * math.sqrt(max(factor, 1.0) / n)


def ccf_profile(driver: Any, kpi: Any, max_lag: int) -> Lagged:
    """Correlation of ``driver`` at time ``t`` with ``kpi`` at ``t + k`` for
    ``k = -max_lag..max_lag`` (positive ``k``: the driver leads the KPI), on the
    overlapping complete pairs; the bound follows Bartlett's formula for two independent
    autocorrelated series (``1.96 / sqrt(n)`` when either one is white, e.g. after
    :func:`prewhiten`)."""
    x = np.asarray(driver, dtype=float).reshape(-1)
    y = np.asarray(kpi, dtype=float).reshape(-1)
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    max_lag = int(min(max_lag, max(0, n - 3)))
    lags = list(range(-max_lag, max_lag + 1))
    values: list[float | None] = []
    for k in lags:
        a, b = (x[: n - k], y[k:]) if k >= 0 else (x[-k:], y[: n + k])
        ok = np.isfinite(a) & np.isfinite(b)
        a, b = a[ok], b[ok]
        if a.size < 3 or np.ptp(a) == 0 or np.ptp(b) == 0:
            values.append(None)
        else:
            values.append(_f(np.corrcoef(a, b)[0, 1]))
    bound = _bartlett_ccf_bound(x, y)
    return Lagged(lags, values, bound, _significant(lags, values, bound))


def prewhiten(driver: Any, kpi: Any, *, max_ar: int = 8) -> tuple[np.ndarray, np.ndarray, int]:
    """Filter both series with the AR(p) filter fitted to the driver (``p`` by AIC up to
    ``max_ar``), so cross-correlations are not inflated by the driver's own persistence.
    Returns ``(driver_filtered, kpi_filtered, p)``; NaNs are kept where a value is
    missing."""
    from statsmodels.tsa.ar_model import AutoReg, ar_select_order

    x = np.asarray(driver, dtype=float).reshape(-1)
    y = np.asarray(kpi, dtype=float).reshape(-1)
    xs = finite(x)
    if xs.size < 3 * max_ar or np.ptp(xs) == 0 or not np.isfinite(x).all():
        return x - np.nanmean(x), y - np.nanmean(y), 0
    sel = ar_select_order(x - x.mean(), maxlag=max_ar, ic="aic", trend="n")
    p = max(sel.ar_lags or [0])
    if p == 0:
        return x - x.mean(), y - np.nanmean(y), 0
    params = AutoReg(x - x.mean(), lags=p, trend="n").fit().params
    phi = np.asarray(params, dtype=float)

    def filt(s: np.ndarray) -> np.ndarray:
        s = s - np.nanmean(s)
        out = np.full_like(s, np.nan)
        for t in range(p, s.size):
            out[t] = s[t] - float(np.dot(phi, s[t - p : t][::-1]))
        return out

    return filt(x), filt(y), p


# -------------------------------------------------------------------- decomposition


def stl_decompose(x: Any, period: int) -> dict[str, np.ndarray] | None:
    """Robust STL of a complete series (``None`` with fewer than two seasons)."""
    from statsmodels.tsa.seasonal import STL

    y = np.asarray(x, dtype=float).reshape(-1)
    if not np.isfinite(y).all() or y.size < 2 * period or period < 2 or np.ptp(y) == 0:
        return None
    res = STL(y, period=period, robust=True).fit()
    return {
        "trend": np.asarray(res.trend, dtype=float),
        "seasonal": np.asarray(res.seasonal, dtype=float),
        "resid": np.asarray(res.resid, dtype=float),
    }


def season_strengths(parts: dict[str, np.ndarray]) -> tuple[float, float]:
    """``(seasonal_strength, trend_strength)`` in ``[0, 1]`` (Wang, Smith & Hyndman 2006):
    ``F_s = max(0, 1 - Var(R) / Var(S + R))`` and ``F_t = max(0, 1 - Var(R) / Var(T + R))``."""
    r, s, t = parts["resid"], parts["seasonal"], parts["trend"]
    vr = float(np.var(r))

    def strength(other: np.ndarray) -> float:
        v = float(np.var(other + r))
        return 0.0 if v <= 0 else float(min(1.0, max(0.0, 1.0 - vr / v)))

    return strength(s), strength(t)


# ------------------------------------------------------------- collinearity, outliers


def vif_table(columns: dict[str, np.ndarray]) -> dict[str, float | None]:
    """Variance inflation factor of each column against the others (with a constant), on
    the rows where every column is finite. A constant column gets ``None``."""
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    names = list(columns)
    if len(names) < 2:
        return dict.fromkeys(names)
    x = np.column_stack([np.asarray(columns[n], dtype=float).reshape(-1) for n in names])
    x = x[np.isfinite(x).all(axis=1)]
    out: dict[str, float | None] = {}
    if x.shape[0] <= len(names) + 1:
        return dict.fromkeys(names)
    keep = [i for i in range(len(names)) if np.ptp(x[:, i]) > 0]
    design = np.column_stack([np.ones(x.shape[0]), x[:, keep]])
    for n in names:
        out[n] = None
    with np.errstate(divide="ignore", invalid="ignore"):
        for j, i in enumerate(keep, start=1):
            v = float(variance_inflation_factor(design, j))
            out[names[i]] = v if math.isfinite(v) else 1e12
    return out


def condition_number(columns: dict[str, np.ndarray]) -> float | None:
    """Condition number of the standardised design (above ~30: strong collinearity)."""
    if len(columns) < 2:
        return None
    x = np.column_stack([np.asarray(c, dtype=float).reshape(-1) for c in columns.values()])
    x = x[np.isfinite(x).all(axis=1)]
    sd = x.std(axis=0)
    x = x[:, sd > 0]
    if x.shape[1] < 2 or x.shape[0] < 3:
        return None
    z = (x - x.mean(axis=0)) / x.std(axis=0)
    return _f(np.linalg.cond(z))


def outlier_scores(x: Any, method: Literal["iqr", "mad", "zscore"] = "mad") -> np.ndarray:
    """A robust z-like score per value (NaN stays NaN): ``mad``: ``0.6745 (x - median) /
    MAD``; ``iqr``: ``(x - median) / (IQR / 1.349)``; ``zscore``: ``(x - mean) / sd``.
    Under a normal distribution all three are standard normal, so one threshold (3.5 by
    default) means the same for each."""
    a = np.asarray(x, dtype=float).reshape(-1)
    y = a[np.isfinite(a)]
    out = np.full_like(a, np.nan)
    if y.size < 3:
        return out
    if method == "zscore":
        sd = float(y.std(ddof=1))
        if sd > 0:
            out = (a - y.mean()) / sd
        return out
    med = float(np.median(y))
    if method == "mad":
        mad = float(np.median(np.abs(y - med)))
        scale = mad / 0.6745 if mad > 0 else float(np.mean(np.abs(y - med))) * 1.2533
    else:
        q1, q3 = np.percentile(y, [25, 75])
        scale = float(q3 - q1) / 1.349
    if scale > 0:
        out = (a - med) / scale
    return out


def qq_points(x: Any, *, max_points: int = 200) -> tuple[list[float], list[float]]:
    """Standardised sample quantiles against normal quantiles (Blom plotting positions)."""
    from scipy import stats

    y = np.sort(finite(x))
    if y.size < 3 or float(y.std()) == 0:
        return [], []
    z = (y - y.mean()) / y.std(ddof=1)
    n = y.size
    theo = stats.norm.ppf((np.arange(1, n + 1) - 0.375) / (n + 0.25))
    if n > max_points:
        idx = np.linspace(0, n - 1, max_points).round().astype(int)
        theo, z = theo[idx], z[idx]
    return [float(v) for v in theo], [float(v) for v in z]


# ---------------------------------------------------------------- null-test helpers


def block_permutation(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """A permutation of ``range(n)`` that shuffles contiguous blocks of length ``block``
    (the last block may be shorter), keeping short-range structure within blocks."""
    block = max(1, min(int(block), n))
    starts = list(range(0, n, block))
    order = rng.permutation(len(starts))
    return np.concatenate([np.arange(starts[i], min(starts[i] + block, n)) for i in order])


Method = Literal["naive", "seasonal_naive", "mean", "drift"]


def naive_forecasts(
    history: Any, horizon: int, *, season: int | None = None, methods: Sequence[Method] = ("naive",)
) -> dict[str, np.ndarray]:
    """Benchmark forecasts of the next ``horizon`` values from ``history`` (Hyndman &
    Athanasopoulos, FPP3 §5.2): ``naive`` repeats the last value, ``seasonal_naive`` the
    value one season earlier, ``mean`` the historical mean, ``drift`` extends the line from
    the first to the last value. Methods that need more history are omitted."""
    h = np.asarray(history, dtype=float).reshape(-1)
    y = h[np.isfinite(h)]
    out: dict[str, np.ndarray] = {}
    if y.size == 0:
        return out
    steps = np.arange(1, horizon + 1)
    for m in methods:
        if m == "naive":
            out[m] = np.full(horizon, y[-1])
        elif m == "mean":
            out[m] = np.full(horizon, y.mean())
        elif m == "drift" and y.size >= 2:
            out[m] = y[-1] + steps * (y[-1] - y[0]) / (y.size - 1)
        elif m == "seasonal_naive" and season and season >= 2 and h.size >= season:
            last = h[-season:]
            if np.isfinite(last).all():
                out[m] = np.array([last[(k - 1) % season] for k in steps], dtype=float)
    return out


def diebold_mariano(
    e1: Any, e2: Any, *, h: int = 1, loss: Literal["se", "ae"] = "se"
) -> tuple[float, float] | None:
    """Diebold-Mariano test of equal accuracy of two forecast-error series, with the
    Harvey-Leybourne-Newbold (1997) small-sample correction and a ``t(n-1)`` reference.
    Returns ``(statistic, two-sided p)``; a negative statistic favours the first model.
    ``None`` when there are too few pairs or the loss difference is constant."""
    from scipy import stats

    a = np.asarray(e1, dtype=float).reshape(-1)
    b = np.asarray(e2, dtype=float).reshape(-1)
    n0 = min(a.size, b.size)
    a, b = a[:n0], b[:n0]
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    n = a.size
    if n < 3:
        return None
    d = (a**2 - b**2) if loss == "se" else (np.abs(a) - np.abs(b))
    dbar = float(d.mean())
    gamma0 = float(np.mean((d - dbar) ** 2))
    var = gamma0
    for k in range(1, h):
        if k >= n:
            break
        gk = float(np.mean((d[k:] - dbar) * (d[:-k] - dbar)))
        var += 2 * gk
    if var <= 0:
        return None
    dm = dbar / math.sqrt(var / n)
    correction = math.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    stat = dm * correction
    p = float(2 * stats.t.sf(abs(stat), df=n - 1))
    return float(stat), _p(p)
