"""Statistical test nodes (``test.*``) on statsmodels and scipy.

Every test returns a :class:`~mapi_spec.stattests.StatTestResult`: statistic, p-value,
decision at ``alpha`` and the plain-language verdict of :data:`TEST_DEFINITIONS`. Panels are
tested per series (``per_series``; the headline p-value is the median, and the null is
rejected when at least half of the series reject it) or as their total.

Implementation notes (what the reference tests check against):

* ADF, KPSS, Zivot-Andrews, Engle-Granger, Johansen, Ljung-Box, Jarque-Bera, Breusch-Pagan,
  White, ARCH-LM, Granger, ACF/PACF, VIF and CUSUM call statsmodels directly;
* Phillips-Perron is computed here (statsmodels has none): the Z-tau statistic of an OLS
  regression of ``y_t`` on ``y_{t-1}`` and deterministic terms, with a Newey-West
  (Bartlett) long-run variance and the default bandwidth ``ceil(12 (n/100)^(1/4))``;
  p-values and critical values are MacKinnon's for the ADF tau distribution - the same as
  the ``arch`` package's ``PhillipsPerron``;
* seasonal strength is Wang, Smith & Hyndman's ``F_s = max(0, 1 - Var(R) / Var(S + R))`` on
  a robust STL decomposition;
* Chow is the textbook F test of pooled vs split OLS fits.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.stattests import (
    TEST_DEFINITIONS,
    SeriesTestResult,
    StatTestResult,
    make_test_result,
)
from mapi_workflows.payloads import clean_number
from mapi_workflows.runtime.data import (
    Series,
    data_error,
    iter_series,
    label_of,
    schema_of,
    season_length,
    target_var,
    time_dim,
)
from mapi_workflows.runtime.values import ModelValue, NodeContext, NodeOutcome
from pydantic import JsonValue
from scipy import stats as sps

__all__ = [
    "TEST_NODES",
    "SeriesOutcome",
    "acf_significance",
    "chow_test",
    "phillips_perron",
    "seasonal_strength",
]

MIN_OBS: Final = 10


@dataclass(slots=True)
class SeriesOutcome:
    """A test on one series."""

    statistic: float | None
    p_value: float | None
    critical_values: dict[str, float] = field(default_factory=dict)
    lags: int | None = None
    n_obs: int | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)
    reject: bool | None = None
    """For tests decided without a p-value (Johansen, seasonal strength, VIF)."""


def _f(x: Any) -> float | None:
    return clean_number(x)


def _crit(d: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            f = _f(v)
            if f is not None:
                out[str(k)] = f
    return out


def _p(p: Any) -> float:
    f = _f(p)
    if f is None:
        raise MapiError("the test gave no p-value", code=ErrorCode.USER_CODE_ERROR)
    return min(max(f, 0.0), 1.0)


def _alpha(ctx: NodeContext, default: float = 0.05) -> float:
    a = ctx.params.get("alpha", default)
    if isinstance(a, str):
        a = float(a)
    f = _f(a)
    return float(f) if f is not None and 0 < f < 1 else default


# =============================================================================== univariate


def _run_univariate(
    ctx: NodeContext,
    test_id: str,
    compute: Callable[[np.ndarray, np.ndarray], SeriesOutcome],
    *,
    min_n: int = MIN_OBS,
    var: str | None = None,
    values: Sequence[Series] | None = None,
    extra_result: Callable[[SeriesOutcome, np.ndarray], dict[str, JsonValue]] | None = None,
) -> NodeOutcome:
    alpha = _alpha(ctx)
    warnings_plain: list[str] = []
    if values is None:
        dv = ctx.dataset("data")
        assert dv is not None
        schema = schema_of(dv)
        ds = dv.data
        name = target_var(ds, schema, var if var is not None else _str(ctx.params.get("var")))
        tdim = time_dim(ds, schema)
        per_series = bool(ctx.params.get("per_series", True))
        series = list(iter_series(ds, name, tdim, per_series=per_series))
    else:
        series = list(values)
        name = var or "residuals"
    results: list[tuple[Series, SeriesOutcome | str]] = []
    for s in series:
        ctx.check()
        v, lab = s.clean()
        if s.gaps:
            warnings_plain.append(f"{s.name}: {s.gaps} missing value(s) were interpolated")
        if len(v) < min_n:
            results.append((s, f"needs at least {min_n} observations (has {len(v)})"))
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                results.append((s, compute(v, lab)))
        except (ValueError, np.linalg.LinAlgError, ZeroDivisionError) as exc:
            results.append((s, f"could not be computed ({str(exc)[:200]})"))
    defn = TEST_DEFINITIONS.get(test_id)
    ok = [(s, o) for s, o in results if isinstance(o, SeriesOutcome)]
    if not ok:
        why = next((o for _, o in results if isinstance(o, str)), "no series")
        raise data_error(f"{test_id}: {why}")
    if len(results) == 1:
        s1, o1 = ok[0]
        assert isinstance(o1, SeriesOutcome)
        res = _one_result(test_id, o1, alpha, name)
        result = res.model_dump(mode="json")
        if extra_result is not None:
            result.update(extra_result(o1, s1.labels))
        return _outcome(result, res, warnings_plain)
    per: list[SeriesTestResult] = []
    rejects: list[bool] = []
    pvals: list[float] = []
    stats_: list[float] = []
    for s, o in results:
        if isinstance(o, str):
            per.append(
                SeriesTestResult(coords=s.coords, details={"status": "skipped", "message_plain": o})
            )
            continue
        reject = o.reject if o.reject is not None else (o.p_value is not None and o.p_value < alpha)
        decision = (defn.reject_decision if reject else defn.keep_decision) if defn else None
        per.append(
            SeriesTestResult(
                coords=s.coords,
                statistic=o.statistic,
                p_value=o.p_value,
                reject=reject,
                decision=decision,
                details=o.details,
            )
        )
        rejects.append(reject)
        if o.p_value is not None:
            pvals.append(o.p_value)
        if o.statistic is not None:
            stats_.append(o.statistic)
    share = sum(rejects) / len(rejects)
    reject = share >= 0.5
    assert defn is not None
    decision = defn.reject_decision if reject else defn.keep_decision
    verdict = defn.reject_plain if reject else defn.keep_plain
    verdict = (
        f"{verdict} ({sum(rejects)} of {len(rejects)} series"
        f" {'reject' if reject else 'do not reject'} the null at {alpha:g}.)"
    )
    res = StatTestResult(
        test=test_id,
        var=name,
        statistic=float(np.median(stats_)) if stats_ else None,
        p_value=float(np.median(pvals)) if pvals else None,
        alpha=alpha,
        reject=reject,
        decision=decision,
        verdict_plain=verdict[:500],
        null_plain=defn.null_plain,
        per_series=per,
        reject_share=share,
        n_obs=int(sum(o.n_obs or 0 for _, o in ok if isinstance(o, SeriesOutcome))) or None,
    )
    return _outcome(res.model_dump(mode="json"), res, warnings_plain)


def _one_result(test_id: str, o: SeriesOutcome, alpha: float, var: str | None) -> StatTestResult:
    defn = TEST_DEFINITIONS[test_id]
    if o.p_value is not None and o.reject is None:
        return make_test_result(
            test_id,
            statistic=o.statistic,
            p_value=o.p_value,
            alpha=alpha,
            var=var,
            critical_values=dict(o.critical_values),
            n_obs=o.n_obs,
            lags=o.lags,
            details=o.details,
        )
    reject = bool(o.reject)
    return StatTestResult(
        test=test_id,
        var=var,
        statistic=o.statistic,
        p_value=o.p_value,
        alpha=alpha,
        reject=reject,
        decision=defn.reject_decision if reject else defn.keep_decision,
        verdict_plain=defn.reject_plain if reject else defn.keep_plain,
        null_plain=defn.null_plain,
        critical_values=o.critical_values,
        n_obs=o.n_obs,
        lags=o.lags,
        details=o.details,
    )


def _outcome(result: dict[str, Any], res: StatTestResult, warns: list[str]) -> NodeOutcome:
    # the details fields are also top-level result fields (nodes.<id>.rank, .break_at, ...)
    for k, v in res.details.items():
        result.setdefault(k, v)
    return NodeOutcome(
        result=result, outputs={"result": res.model_dump(mode="json")}, warnings_plain=warns[:50]
    )


def _str(v: JsonValue) -> str | None:
    return v if isinstance(v, str) and v else None


def _int(v: JsonValue) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# ===================================================================================== ADF


def _adf(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.stattools import adfuller

    regression = str(ctx.params.get("regression") or "c")
    autolag = str(ctx.params.get("autolag") or "AIC")
    maxlag = _int(ctx.params.get("maxlag"))

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        out = adfuller(
            x,
            maxlag=maxlag,
            regression=regression,
            autolag=None if autolag == "none" else autolag,
        )
        stat, p, lags, nobs, crit = out[0], out[1], out[2], out[3], out[4]
        return SeriesOutcome(
            _f(stat),
            _p(p),
            _crit(crit),
            int(lags),
            int(nobs),
            {"regression": regression, "autolag": autolag},
        )

    return _run_univariate(ctx, "test.adf", compute, min_n=12)


def _kpss(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.stattools import kpss

    regression = str(ctx.params.get("regression") or "c")
    nlags = str(ctx.params.get("nlags") or "auto")

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        stat, p, lags, crit = kpss(x, regression=regression, nlags=nlags)
        return SeriesOutcome(
            _f(stat),
            _p(p),
            _crit(crit),
            int(lags),
            len(x),
            {"regression": regression, "p_value_bounded": bool(p <= 0.01 or p >= 0.1)},
        )

    return _run_univariate(ctx, "test.kpss", compute, min_n=12)


# ============================================================================ Phillips-Perron


def phillips_perron(
    y: np.ndarray, *, trend: str = "c", lags: int | None = None
) -> tuple[float, float, dict[str, float], int, int]:
    """Phillips-Perron Z-tau: ``(statistic, p_value, critical_values, lags, nobs)``.

    Matches ``arch.unitroot.PhillipsPerron(y, trend=trend, lags=lags, test_type="tau")``.
    """
    from statsmodels.tsa.adfvalues import mackinnoncrit, mackinnonp

    y = np.asarray(y, dtype=float)
    nobs = y.shape[0]
    if lags is None:
        lags = math.ceil(12.0 * (nobs / 100.0) ** 0.25)
    lhs = y[1:]
    n = lhs.shape[0]
    cols = [y[:-1]]
    if trend in ("c", "ct"):
        cols.append(np.ones(n))
    if trend == "ct":
        cols.append(np.arange(1, n + 1, dtype=float))
    rhs = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(rhs, lhs, rcond=None)
    resid = lhs - rhs @ beta
    k = rhs.shape[1]
    ssr = float(resid @ resid)
    s2 = ssr / (n - k)
    cov = s2 * np.linalg.inv(rhs.T @ rhs)
    sigma = math.sqrt(cov[0, 0])
    gamma0 = ssr / n
    lam2 = gamma0
    for j in range(1, lags + 1):
        w = 1.0 - j / (lags + 1.0)
        lam2 += 2.0 * w * float(resid[j:] @ resid[:-j]) / n
    lam = math.sqrt(lam2)
    t = (beta[0] - 1.0) / sigma
    stat = math.sqrt(gamma0) / lam * t - 0.5 * ((lam2 - gamma0) / lam) * (n * sigma / math.sqrt(s2))
    reg = {"n": "n", "c": "c", "ct": "ct"}[trend]
    p = float(mackinnonp(stat, regression=reg, N=1))
    cv = mackinnoncrit(N=1, regression=reg, nobs=n)
    crit = {"1%": float(cv[0]), "5%": float(cv[1]), "10%": float(cv[2])}
    return float(stat), p, crit, int(lags), int(n)


def _pp(ctx: NodeContext) -> NodeOutcome:
    trend = str(ctx.params.get("trend") or "c")
    lags = _int(ctx.params.get("lags"))

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        stat, p, crit, used, n = phillips_perron(x, trend=trend, lags=lags)
        return SeriesOutcome(stat, _p(p), crit, used, n, {"trend": trend})

    return _run_univariate(ctx, "test.pp", compute, min_n=12)


def _zivot_andrews(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.stattools import zivot_andrews

    regression = str(ctx.params.get("regression") or "c")
    trim = float(_f(ctx.params.get("trim")) or 0.15)

    def compute(x: np.ndarray, labels: np.ndarray) -> SeriesOutcome:
        stat, p, crit, lags, bp = zivot_andrews(x, trim=trim, regression=regression)
        bp = int(bp)
        details: dict[str, JsonValue] = {
            "break_index": bp,
            "break_at": str(label_of(labels[bp])) if 0 <= bp < len(labels) else None,
            "regression": regression,
        }
        return SeriesOutcome(_f(stat), _p(p), _crit(crit), int(lags), len(x), details)

    return _run_univariate(ctx, "test.zivot_andrews", compute, min_n=30)


# ============================================================================ cointegration


def _series_dim(ds: xr.Dataset, schema: DataSchema, tdim: str) -> tuple[str, str] | None:
    """``(variable, dim)`` when the data keeps several series along one dim of its target
    (``value(series, quarter)``, the layout of the multivariate models)."""
    try:
        var = target_var(ds, schema)
    except MapiError:
        return None
    others = [str(d) for d in ds[var].dims if d != tdim]
    if len(others) != 1:
        return None
    return var, others[0]


def system_names(ctx: NodeContext, key: str = "vars", min_n: int = 2) -> list[str]:
    """The series a multivariate node works on: ``params[key]`` (variable names or labels
    along the series dim), else every label along the target's series dim."""
    raw = ctx.params.get(key)
    names = [str(v) for v in raw] if isinstance(raw, list) else []
    if not names:
        dv = ctx.dataset("data")
        assert dv is not None
        schema = schema_of(dv)
        found = _series_dim(dv.data, schema, time_dim(dv.data, schema))
        if found is not None:
            names = [str(label_of(v)) for v in dv.data[found[1]].values]
    if len(names) < min_n:
        raise data_error(
            f"name at least {min_n} series in {key!r} (variables, or labels along the data's "
            "series dimension)"
        )
    return names


def variable_matrix(ctx: NodeContext, names: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
    """Columns of ``names`` along time: data variables (a panel summed), or labels along the
    series dim of the target (long layout)."""
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    ds = dv.data
    tdim = time_dim(ds, schema)
    long = _series_dim(ds, schema, tdim)
    cols = []
    labels: np.ndarray | None = None
    for n in names:
        if n in ds.data_vars:
            (s,) = list(iter_series(ds, n, tdim, per_series=False))
        elif long is not None and n in {str(label_of(v)) for v in ds[long[1]].values}:
            var, dim = long
            pos = [str(label_of(v)) for v in ds[dim].values].index(n)
            sub = ds[[var]].isel({dim: pos}, drop=True)
            (s,) = list(iter_series(sub, var, tdim, per_series=False))
        else:
            where = f" or a label of {long[1]!r}" if long is not None else ""
            raise data_error(f"the data has no variable{where} named {n!r}")
        cols.append(s.values)
        labels = s.labels
    mat = np.column_stack(cols)
    ok = np.all(np.isfinite(mat), axis=1)
    assert labels is not None
    return mat[ok], labels[ok]


def _vars(ctx: NodeContext, key: str = "vars", min_n: int = 2) -> list[str]:
    raw = ctx.params.get(key)
    names = [str(v) for v in raw] if isinstance(raw, list) else []
    if len(names) < min_n:
        raise data_error(f"name at least {min_n} variable(s) in {key!r}")
    return names


def _johansen(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.vector_ar.vecm import coint_johansen

    names = system_names(ctx)
    det = int(_int(ctx.params.get("det_order")) or 0)
    k_raw = _int(ctx.params.get("k_ar_diff"))
    k = 1 if k_raw is None else k_raw
    method = str(ctx.params.get("method") or "trace")
    level = str(ctx.params.get("alpha") or "0.05")
    col = {"0.1": 0, "0.05": 1, "0.01": 2}[level]
    mat, _ = variable_matrix(ctx, names)
    if mat.shape[0] < 3 * len(names) + k + 10:
        raise data_error(f"test.johansen: too few observations ({mat.shape[0]})")
    res = coint_johansen(mat, det, k)
    stats_ = res.lr1 if method == "trace" else res.lr2
    crit = res.cvt if method == "trace" else res.cvm
    rank = johansen_rank(stats_, crit[:, col])
    details: dict[str, JsonValue] = {
        "rank": rank,
        "method": method,
        "vars": list(names),
        "trace_stats": [float(x) for x in res.lr1],
        "max_eig_stats": [float(x) for x in res.lr2],
        "trace_crit": [[float(x) for x in row] for row in res.cvt],
        "max_eig_crit": [[float(x) for x in row] for row in res.cvm],
        "eigenvalues": [float(x) for x in res.eig],
    }
    o = SeriesOutcome(
        float(stats_[0]),
        None,
        {"90%": float(crit[0, 0]), "95%": float(crit[0, 1]), "99%": float(crit[0, 2])},
        k,
        int(mat.shape[0]),
        details,
        reject=rank > 0,
    )
    res_ = _one_result("test.johansen", o, float(level), None)
    return _outcome(res_.model_dump(mode="json"), res_, [])


def johansen_rank(stats_: Sequence[float], crit: Sequence[float]) -> int:
    """The cointegration rank by sequential testing: the first ``r`` whose statistic is
    below its critical value (``len(stats_)`` when every null is rejected)."""
    for r, (s, c) in enumerate(zip(stats_, crit, strict=True)):
        if s < c:
            return r
    return len(stats_)


def _engle_granger(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.stattools import coint

    y, x = _str(ctx.params.get("y")), _str(ctx.params.get("x"))
    if not y or not x:
        raise data_error("test.engle_granger needs 'y' and 'x'")
    trend = str(ctx.params.get("trend") or "c")
    mat, _ = variable_matrix(ctx, [y, x])
    if mat.shape[0] < 20:
        raise data_error("test.engle_granger needs at least 20 observations")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stat, p, crit = coint(mat[:, 0], mat[:, 1], trend=trend)
    o = SeriesOutcome(
        _f(stat),
        _p(p),
        {"1%": float(crit[0]), "5%": float(crit[1]), "10%": float(crit[2])},
        None,
        int(mat.shape[0]),
        {"y": y, "x": x, "trend": trend},
    )
    res = _one_result("test.engle_granger", o, _alpha(ctx), y)
    return _outcome(res.model_dump(mode="json"), res, [])


# ================================================================================ residuals


def _residual_series(ctx: NodeContext) -> tuple[list[Series], str, ModelValue | None]:
    mv = ctx.model("model", required=False)
    if mv is not None:
        from mapi_workflows.runtime.models import model_residuals

        res = model_residuals(mv, ctx.registry)
        return [Series({}, res.resid, res.labels)], f"residuals of {res.target}", mv
    dv = ctx.dataset("data", required=False)
    if dv is None:
        raise data_error("the test needs a model (its residuals) or a dataset")
    schema = schema_of(dv)
    name = target_var(dv.data, schema, _str(ctx.params.get("var")))
    tdim = time_dim(dv.data, schema)
    return list(iter_series(dv.data, name, tdim, per_series=True)), name, None


def _ljung_box(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.stats.diagnostic import acorr_ljungbox

    series, name, mv = _residual_series(ctx)
    lags = int(_int(ctx.params.get("lags")) or 10)
    model_df = _int(ctx.params.get("model_df"))
    if model_df is None and mv is not None:
        model_df = arma_terms(mv.settings)

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        use = min(lags, max(1, len(x) // 2 - 1))
        df = min(model_df or 0, use - 1)
        tab = acorr_ljungbox(x, lags=list(range(1, use + 1)), model_df=df, return_df=True)
        stat = float(tab["lb_stat"].iloc[-1])
        p = float(tab["lb_pvalue"].iloc[-1])
        by_lag: dict[str, JsonValue] = {
            str(i + 1): _f(v) for i, v in enumerate(tab["lb_pvalue"].tolist())
        }
        return SeriesOutcome(stat, _p(p), {}, use, len(x), {"p_values": by_lag, "model_df": df})

    return _run_univariate(ctx, "test.ljung_box", compute, values=series, var=name)


def arma_terms(settings: dict[str, JsonValue]) -> int:
    """``p + q (+ P + Q)`` from SARIMAX-style ``order``/``seasonal_order`` settings."""
    total = 0
    order = settings.get("order")
    if isinstance(order, list) and len(order) == 3:
        total += int(order[0] or 0) + int(order[2] or 0)  # type: ignore[arg-type]
    so = settings.get("seasonal_order")
    if isinstance(so, list) and len(so) == 4:
        total += int(so[0] or 0) + int(so[2] or 0)  # type: ignore[arg-type]
    return total


def _jarque_bera(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.stats.stattools import jarque_bera

    series, name, _ = _residual_series(ctx)

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        jb, p, skew, kurt = jarque_bera(x)
        return SeriesOutcome(
            _f(jb), _p(p), {}, None, len(x), {"skew": _f(skew), "kurtosis": _f(kurt)}
        )

    return _run_univariate(ctx, "test.jarque_bera", compute, values=series, var=name, min_n=8)


def _arch_lm(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.stats.diagnostic import het_arch

    series, name, _ = _residual_series(ctx)
    nlags = int(_int(ctx.params.get("nlags")) or 4)

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        lm, p, f, fp = het_arch(x, nlags=nlags)
        return SeriesOutcome(
            _f(lm), _p(p), {}, nlags, len(x), {"f_statistic": _f(f), "f_p_value": _f(fp)}
        )

    return _run_univariate(ctx, "test.arch_lm", compute, values=series, var=name, min_n=nlags + 10)


def _het(ctx: NodeContext, test_id: str) -> NodeOutcome:
    from statsmodels.stats.diagnostic import het_breuschpagan, het_white

    mv = ctx.model("model")
    assert mv is not None
    from mapi_workflows.runtime.models import model_residuals

    res = model_residuals(mv, ctx.registry)
    if res.exog is None or res.exog.shape[1] == 0:
        raise data_error(f"{test_id} needs a model with drivers (inputs besides the result)")
    exog = np.column_stack([np.ones(len(res.resid)), res.exog])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if test_id == "test.breusch_pagan":
            lm, p, f, fp = het_breuschpagan(
                res.resid, exog, robust=bool(ctx.params.get("robust", True))
            )
        else:
            lm, p, f, fp = het_white(res.resid, exog)
    o = SeriesOutcome(
        _f(lm),
        _p(p),
        {},
        None,
        len(res.resid),
        {"f_statistic": _f(f), "f_p_value": _f(fp), "drivers": list(res.exog_names)},
    )
    r = _one_result(test_id, o, _alpha(ctx), res.target)
    return _outcome(r.model_dump(mode="json"), r, [])


def _cusum(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.stats.diagnostic import breaks_cusumolsresid

    mv = ctx.model("model")
    assert mv is not None
    from mapi_workflows.runtime.models import model_residuals

    res = model_residuals(mv, ctx.registry)
    ddof = 1 + (res.exog.shape[1] if res.exog is not None else 0)
    sup_b, p, crit = breaks_cusumolsresid(res.resid, ddof=ddof)
    cs = np.cumsum(res.resid - res.resid.mean())
    at = int(np.argmax(np.abs(cs)))
    crit_d = {f"{int(lvl)}%": float(v) for lvl, v in crit} if crit is not None else {}
    o = SeriesOutcome(
        _f(sup_b),
        _p(p),
        crit_d,
        None,
        len(res.resid),
        {
            "break_detected": bool(p < _alpha(ctx)),
            "break_at": str(label_of(res.labels[at])) if len(res.labels) > at else None,
            "break_index": at,
        },
    )
    r = _one_result("test.cusum", o, _alpha(ctx), res.target)
    return _outcome(r.model_dump(mode="json"), r, [])


# ==================================================================================== chow


def chow_test(y: np.ndarray, x: np.ndarray, split: int) -> tuple[float, float, int, int]:
    """Chow's F test of one break at position ``split``: ``(F, p, df1, df2)``. ``x``
    excludes the constant (it is added)."""
    n = len(y)
    X = np.column_stack([np.ones(n), x]) if x.size else np.ones((n, 1))
    k = X.shape[1]
    if split < k + 1 or n - split < k + 1:
        raise ValueError(f"each side of the break needs more than {k} observations")

    def ssr(a: np.ndarray, b: np.ndarray) -> float:
        beta, *_ = np.linalg.lstsq(a, b, rcond=None)
        r = b - a @ beta
        return float(r @ r)

    pooled = ssr(X, y)
    split_ssr = ssr(X[:split], y[:split]) + ssr(X[split:], y[split:])
    df1, df2 = k, n - 2 * k
    f = ((pooled - split_ssr) / df1) / (split_ssr / df2)
    p = float(sps.f.sf(f, df1, df2))
    return float(f), p, df1, df2


def _chow(ctx: NodeContext) -> NodeOutcome:
    y = _str(ctx.params.get("y"))
    xs = _vars(ctx, "x", 1)
    if not y:
        raise data_error("test.chow needs 'y'")
    mat, labels = variable_matrix(ctx, [y, *xs])
    at = ctx.params.get("break_at")
    split = _position(labels, at)
    f, p, df1, df2 = chow_test(mat[:, 0], mat[:, 1:], split)
    o = SeriesOutcome(
        f,
        _p(p),
        {},
        None,
        int(mat.shape[0]),
        {
            "break_index": split,
            "break_at": str(label_of(labels[split])),
            "df1": df1,
            "df2": df2,
        },
    )
    r = _one_result("test.chow", o, _alpha(ctx), y)
    return _outcome(r.model_dump(mode="json"), r, [])


def _position(labels: np.ndarray, at: JsonValue) -> int:
    if isinstance(at, int) and not isinstance(at, bool):
        if not 0 < at < len(labels):
            raise data_error(f"break_at {at} is outside the data (0..{len(labels) - 1})")
        return at
    if isinstance(at, str):
        texts = [str(label_of(v)) for v in labels]
        if at in texts:
            return texts.index(at)
        if np.issubdtype(labels.dtype, np.datetime64):
            try:
                ts = np.datetime64(at)
            except ValueError:
                raise data_error(f"break_at {at!r} is not a date") from None
            pos = int(np.searchsorted(labels, ts))
            if 0 < pos < len(labels):
                return pos
        raise data_error(f"break_at {at!r} is not a label of the time dim")
    raise data_error("break_at must be a date label or a position")


# ================================================================================== granger


def _granger(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.stattools import grangercausalitytests

    y, x = _str(ctx.params.get("y")), _str(ctx.params.get("x"))
    if not y or not x:
        raise data_error("test.granger needs 'y' and 'x'")
    maxlag = int(_int(ctx.params.get("maxlag")) or 4)
    one_lag = _int(ctx.params.get("lag"))
    if one_lag is not None and one_lag > maxlag:
        maxlag = one_lag
    mat, _ = variable_matrix(ctx, [y, x])
    if mat.shape[0] < 3 * maxlag + 10:
        raise data_error(f"test.granger needs more observations for {maxlag} lags")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = grangercausalitytests(mat, maxlag=maxlag)
    p_values: dict[str, JsonValue] = {}
    raw: dict[int, tuple[float, float]] = {}
    best_lag, best_p = 1, 1.0
    for lag, (tests, _models) in out.items():
        f, p = float(tests["ssr_ftest"][0]), float(tests["ssr_ftest"][1])
        p_values[str(lag)] = p
        raw[int(lag)] = (f, p)
        if p < best_p:
            best_lag, best_p = int(lag), p
    if one_lag is not None:
        # one prespecified lag: its own F test, exact at alpha
        tested = int(one_lag)
        stat, p_value = raw[tested]
        correction = "none"
    else:
        # every lag 1..maxlag was tried: the smallest p-value, Holm-corrected for the number
        # of lags (for "does any lag help" Holm's first step is Bonferroni), so the verdict
        # keeps its size instead of about doubling it
        tested, stat = best_lag, raw[best_lag][0]
        p_value, correction = min(1.0, best_p * len(raw)), "holm"
    o = SeriesOutcome(
        stat,
        _p(p_value),
        {},
        tested,
        int(mat.shape[0]),
        {
            "min_p_value": best_p,
            "best_lag": best_lag,
            "p_values": p_values,
            "correction": correction,
            "y": y,
            "x": x,
        },
    )
    r = _one_result("test.granger", o, _alpha(ctx), y)
    return _outcome(r.model_dump(mode="json"), r, [])


# ================================================================================= ACF/PACF


def acf_significance(x: np.ndarray, nlags: int, max_order: int) -> dict[str, Any]:
    """Sample ACF (Bartlett bands) and PACF (Yule-Walker-MLE, ``1.96/sqrt(n)`` bands), the
    significant lags and the suggested AR and MA orders (the cut-off lag of the PACF and of
    the ACF), plus a Ljung-Box test over ``nlags``."""
    from statsmodels.stats.diagnostic import acorr_ljungbox
    from statsmodels.tsa.stattools import acf, pacf

    n = len(x)
    nlags = min(nlags, n // 2 - 1)
    if nlags < 1:
        raise ValueError("too few observations")
    r = acf(x, nlags=nlags, fft=True)
    pr = pacf(x, nlags=nlags, method="ywm")
    z = float(sps.norm.ppf(0.975))
    se_acf = np.sqrt((1 + 2 * np.concatenate([[0.0], np.cumsum(r[1:-1] ** 2)])) / n)
    sig_acf = [k for k in range(1, nlags + 1) if abs(r[k]) > z * se_acf[k - 1]]
    sig_pacf = [k for k in range(1, nlags + 1) if abs(pr[k]) > z / math.sqrt(n)]

    def cutoff(sig: list[int]) -> int:
        k = 0
        while k + 1 in sig:
            k += 1
        return min(k, max_order)

    tab = acorr_ljungbox(x, lags=[nlags], return_df=True)
    return {
        "acf": [float(v) for v in r],
        "pacf": [float(v) for v in pr],
        "acf_band": [float(z * s) for s in se_acf],
        "pacf_band": z / math.sqrt(n),
        "significant_acf_lags": sig_acf,
        "significant_pacf_lags": sig_pacf,
        "suggested_p": cutoff(sig_pacf),
        "suggested_q": cutoff(sig_acf),
        "q_stat": float(tab["lb_stat"].iloc[0]),
        "q_p_value": float(tab["lb_pvalue"].iloc[0]),
        "nlags": nlags,
    }


def _acf_pacf(ctx: NodeContext) -> NodeOutcome:
    nlags = int(_int(ctx.params.get("nlags")) or 24)
    mo = _int(ctx.params.get("max_order"))
    max_order = 5 if mo is None else mo

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        out = acf_significance(x, nlags, max_order)
        return SeriesOutcome(
            out["q_stat"],
            _p(out["q_p_value"]),
            {},
            out["nlags"],
            len(x),
            {k: v for k, v in out.items() if k not in ("q_stat", "q_p_value", "nlags")},
        )

    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    name = target_var(dv.data, schema, _str(ctx.params.get("var")))
    tdim = time_dim(dv.data, schema)
    series = list(iter_series(dv.data, name, tdim, per_series=False))
    return _run_univariate(ctx, "test.acf_pacf", compute, values=series, var=name)


# ========================================================================= seasonal strength


def seasonal_strength(x: np.ndarray, period: int) -> tuple[float, float]:
    """``(F_s, F_t)``: seasonal and trend strength from a robust STL decomposition."""
    from statsmodels.tsa.seasonal import STL

    res = STL(x, period=period, robust=True).fit()
    r, s, t = res.resid, res.seasonal, res.trend
    vr = float(np.var(r))
    fs = max(0.0, 1.0 - vr / float(np.var(s + r))) if np.var(s + r) > 0 else 0.0
    ft = max(0.0, 1.0 - vr / float(np.var(t + r))) if np.var(t + r) > 0 else 0.0
    return fs, ft


def _seasonal_strength(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    tdim = time_dim(dv.data, schema)
    period = season_length(dv.data, schema, tdim, _int(ctx.params.get("period")))
    threshold = float(_f(ctx.params.get("threshold")) or 0.64)

    def compute(x: np.ndarray, _: np.ndarray) -> SeriesOutcome:
        if len(x) < 2 * period + 1:
            raise ValueError(f"needs at least two seasons ({2 * period + 1} periods)")
        fs, ft = seasonal_strength(x, period)
        return SeriesOutcome(
            fs,
            None,
            {},
            None,
            len(x),
            {
                "strength": fs,
                "trend_strength": ft,
                "period": period,
                "seasonal": fs >= threshold,
                "threshold": threshold,
            },
            reject=fs >= threshold,
        )

    return _run_univariate(ctx, "test.seasonal_strength", compute)


# ====================================================================================== VIF


def _vif(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    raw = ctx.params.get("vars")
    given = [str(v) for v in raw] if isinstance(raw, list) else []
    if not given:
        raise data_error("name the drivers to check in 'vars'")
    limit = float(_f(ctx.params.get("limit")) or 10.0)
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    ds = dv.data
    tdim = time_dim(ds, schema)
    try:  # rows are the target's cells (periods x panel); other dims become columns
        rows_dims = [str(d) for d in ds[target_var(ds, schema)].dims]
    except MapiError:
        rows_dims = [tdim]
    names: list[str] = []
    cols = []
    for n in given:
        if n not in ds.data_vars:
            raise data_error(f"the data has no variable {n!r}")
        da = ds[n]
        lead = [d for d in rows_dims if d in da.dims] or [tdim]
        extra = [str(d) for d in da.dims if d not in lead]
        if not extra:
            names.append(n)
            cols.append(np.asarray(da.transpose(*lead).values, dtype=float).reshape(-1))
            continue
        # spend(channel, ...) -> one column per channel: spend[tv], spend[search]
        stacked = da.transpose(*lead, *extra).stack(_col=extra)
        for j in range(stacked.sizes["_col"]):
            label = stacked["_col"].values[j]
            parts = label if isinstance(label, tuple) else (label,)
            names.append(f"{n}[{','.join(str(label_of(x)) for x in parts)}]")
            cols.append(np.asarray(stacked.isel(_col=j).values, dtype=float).reshape(-1))
    if len(names) < 2:
        raise data_error(
            "test.vif needs at least two drivers (variables, or a variable with a channel dim)"
        )
    mat = np.column_stack(cols)
    mat = mat[np.all(np.isfinite(mat), axis=1)]
    if mat.shape[0] <= len(names) + 1:
        raise data_error("test.vif needs more observations than drivers")
    exog = np.column_stack([np.ones(mat.shape[0]), mat])
    table: dict[str, JsonValue] = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for i, n in enumerate(names):
            v = float(variance_inflation_factor(exog, i + 1))
            table[n] = v if math.isfinite(v) else None
    finite = [v for v in table.values() if isinstance(v, float)]
    max_vif = max(finite) if finite else math.inf
    high = [n for n, v in table.items() if v is None or (isinstance(v, float) and v > limit)]
    o = SeriesOutcome(
        max_vif if math.isfinite(max_vif) else None,
        None,
        {},
        None,
        int(mat.shape[0]),
        {
            "max_vif": max_vif if math.isfinite(max_vif) else None,
            "table": table,
            "high_vif_vars": list(high),
            "limit": limit,
        },
        reject=bool(high),
    )
    r = _one_result("test.vif", o, _alpha(ctx), None)
    return _outcome(r.model_dump(mode="json"), r, [])


# ============================================================================ registration

TEST_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "test.adf": _adf,
    "test.kpss": _kpss,
    "test.pp": _pp,
    "test.zivot_andrews": _zivot_andrews,
    "test.johansen": _johansen,
    "test.engle_granger": _engle_granger,
    "test.ljung_box": _ljung_box,
    "test.jarque_bera": _jarque_bera,
    "test.breusch_pagan": lambda ctx: _het(ctx, "test.breusch_pagan"),
    "test.white": lambda ctx: _het(ctx, "test.white"),
    "test.arch_lm": _arch_lm,
    "test.granger": _granger,
    "test.acf_pacf": _acf_pacf,
    "test.seasonal_strength": _seasonal_strength,
    "test.vif": _vif,
    "test.cusum": _cusum,
    "test.chow": _chow,
}
"""Every ``test.*`` node implementation."""
