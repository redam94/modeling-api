"""Search nodes (``search.*``): orders and lags chosen from the data.

* ``search.arma_order`` / ``search.sarima_order``: every ``(p, q)`` (and ``(P, Q)``) on the
  grid is fitted as a statsmodels SARIMAX with the given differencing, drivers and trend,
  and scored by AIC, BIC, HQIC or AICc - the approach of statsmodels'
  ``arma_order_select_ic`` and pmdarima's grid search. Fits that fail are skipped and
  counted. The ACF/PACF of the differenced series is reported as a hint
  (``suggested_p``/``suggested_q``).
* ``search.var_lag_order``: ``VAR.select_order`` (AIC, BIC, HQIC, FPE).
* ``search.coint_rank``: ``select_coint_rank`` (Johansen trace or maximum eigenvalue).

Panels are searched on their total series.
"""

from __future__ import annotations

import itertools
import math
import warnings
from collections.abc import Callable, Sequence
from typing import Any, Final

import numpy as np
from mapi_spec.errors import ErrorCode, MapiError
from mapi_workflows.payloads import TablePayload, clean_number
from mapi_workflows.runtime.data import (
    data_error,
    iter_series,
    schema_of,
    target_var,
    time_dim,
)
from mapi_workflows.runtime.stattests import acf_significance
from mapi_workflows.runtime.values import NodeContext, NodeOutcome
from pydantic import JsonValue

__all__ = ["SEARCH_NODES", "arma_grid", "sarimax_trend"]

MAX_FITS: Final = 400


def sarimax_trend(trend: str | None, d: int) -> str | None:
    """A trend statsmodels accepts with ``d`` (total) differences: terms of lower order than
    the differencing are removed by it, so they are dropped."""
    if trend in (None, "n"):
        return None
    if d <= 0:
        return trend
    if d == 1:
        return {"c": None, "t": "t", "ct": "t"}.get(trend)
    return None


def _series(ctx: NodeContext) -> tuple[np.ndarray, np.ndarray | None, list[str]]:
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    ds = dv.data
    var = ctx.params.get("var")
    name = target_var(ds, schema, var if isinstance(var, str) else None)
    tdim = time_dim(ds, schema)
    (s,) = list(iter_series(ds, name, tdim, per_series=False))
    y = np.asarray(s.values, float)
    raw = ctx.params.get("exog") or []
    exog_names = [str(v) for v in raw] if isinstance(raw, list) else []
    exog = None
    if exog_names:
        cols = []
        for n in exog_names:
            if n not in ds.data_vars:
                raise data_error(f"the data has no driver {n!r}")
            (es,) = list(iter_series(ds, n, tdim, per_series=False))
            cols.append(np.asarray(es.values, float))
        exog = np.column_stack(cols)
    ok = np.isfinite(y)
    if exog is not None:
        ok &= np.all(np.isfinite(exog), axis=1)
    y = y[ok]
    exog = exog[ok] if exog is not None else None
    if len(y) < 20:
        raise data_error("the order search needs at least 20 observations")
    return y, exog, exog_names


def _ic(res: Any, criterion: str) -> float:
    value = getattr(res, criterion)
    return float(value)


def arma_grid(
    y: np.ndarray,
    exog: np.ndarray | None,
    *,
    ps: Sequence[int],
    qs: Sequence[int],
    d: int = 0,
    seasonal: Sequence[tuple[int, int, int, int]] = ((0, 0, 0, 0),),
    trend: str | None = "c",
    criterion: str = "aic",
    check: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fit every order on the grid; rows sorted best first and the number of failures."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    rows: list[dict[str, Any]] = []
    failed = 0
    points = list(itertools.product(ps, qs, seasonal))
    if len(points) > MAX_FITS:
        raise MapiError(
            f"the grid has {len(points)} fits (at most {MAX_FITS})", code=ErrorCode.WORKFLOW_INVALID
        )
    for p, q, so in points:
        if check is not None:
            check()
        tr = sarimax_trend(trend, d + so[1])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = SARIMAX(y, exog=exog, order=(p, d, q), seasonal_order=so, trend=tr).fit(
                    disp=False
                )
            score = _ic(res, criterion)
            if not math.isfinite(score):
                raise ValueError("non-finite criterion")
        except (ValueError, np.linalg.LinAlgError, IndexError):
            failed += 1
            rows.append(
                {"p": p, "q": q, "P": so[0], "Q": so[2], criterion: None, "status": "failed"}
            )
            continue
        rows.append(
            {
                "p": p,
                "q": q,
                "P": so[0],
                "Q": so[2],
                criterion: score,
                "aic": float(res.aic),
                "bic": float(res.bic),
                "hqic": float(res.hqic),
                "status": "ok",
            }
        )
    rows.sort(
        key=lambda r: (r[criterion] is None, r[criterion] if r[criterion] is not None else 0.0)
    )
    return rows, failed


def _arma(ctx: NodeContext) -> NodeOutcome:
    y, exog, names = _series(ctx)
    max_p = int(clean_number(ctx.params.get("max_p")) or 0)
    max_q = int(clean_number(ctx.params.get("max_q")) or 0)
    d = int(clean_number(ctx.params.get("d")) or 0)
    criterion = str(ctx.params.get("criterion") or "aic")
    trend = ctx.params.get("trend")
    rows, failed = arma_grid(
        y,
        exog,
        ps=range(max_p + 1),
        qs=range(max_q + 1),
        d=d,
        trend=str(trend) if isinstance(trend, str) else "c",
        criterion=criterion,
        check=ctx.check,
    )
    best = next((r for r in rows if r.get("status") == "ok"), None)
    if best is None:
        raise data_error("every order on the grid failed to fit")
    dy = np.diff(y, n=d) if d else y
    hints = acf_significance(dy, nlags=min(24, len(dy) // 2 - 1), max_order=max(max_p, max_q))
    p, q = int(best["p"]), int(best["q"])
    table = TablePayload(
        title=f"AR/MA orders by {criterion.upper()}",
        kind="order_search",
        columns=["p", "q", criterion, "aic", "bic", "hqic", "status"],
        rows=[{k: _js(v) for k, v in r.items() if k not in ("P", "Q")} for r in rows],
        meta={
            "criterion": criterion,
            "d": d,
            "exog": list(names),
            "summary_plain": f"ARMA({p},{q}) has the best {criterion.upper()} "
            f"({best[criterion]:.1f}) of {len(rows) - failed} fits.",
        },
    )
    result: dict[str, JsonValue] = {
        "best_p": p,
        "best_q": q,
        "best_order": [p, d, q],
        "best_score": float(best[criterion]),
        "n_failed": failed,
        "criterion": criterion,
        "suggested_p": int(hints["suggested_p"]),
        "suggested_q": int(hints["suggested_q"]),
    }
    return NodeOutcome(result=result, outputs={"table": table.model_dump(mode="json")})


def _sarima(ctx: NodeContext) -> NodeOutcome:
    y, exog, names = _series(ctx)
    period = int(clean_number(ctx.params.get("period")) or 0)
    if period < 2:
        raise data_error("search.sarima_order needs a season length (period)")
    if len(y) < 2 * period + 10:
        raise data_error(f"the seasonal search needs at least {2 * period + 10} observations")

    def num(key: str) -> int:
        return int(clean_number(ctx.params.get(key)) or 0)

    d, big_d = num("d"), num("D")
    criterion = str(ctx.params.get("criterion") or "aic")
    seasonal = [
        (sp, big_d, sq, period) for sp in range(num("max_P") + 1) for sq in range(num("max_Q") + 1)
    ]
    rows, failed = arma_grid(
        y,
        exog,
        ps=range(num("max_p") + 1),
        qs=range(num("max_q") + 1),
        d=d,
        seasonal=seasonal,
        trend="c",
        criterion=criterion,
        check=ctx.check,
    )
    best = next((r for r in rows if r.get("status") == "ok"), None)
    if best is None:
        raise data_error("every order on the grid failed to fit")
    order = [int(best["p"]), d, int(best["q"])]
    sorder = [int(best["P"]), big_d, int(best["Q"]), period]
    table = TablePayload(
        title=f"Seasonal orders by {criterion.upper()}",
        kind="order_search",
        columns=["p", "q", "P", "Q", criterion, "aic", "bic", "hqic", "status"],
        rows=[{k: _js(v) for k, v in r.items()} for r in rows],
        meta={"criterion": criterion, "d": d, "D": big_d, "period": period, "exog": list(names)},
    )
    result: dict[str, JsonValue] = {
        "best_order": list(order),
        "best_seasonal_order": list(sorder),
        "best_score": float(best[criterion]),
        "n_failed": failed,
        "criterion": criterion,
    }
    return NodeOutcome(result=result, outputs={"table": table.model_dump(mode="json")})


def variable_matrix(ctx: NodeContext) -> tuple[np.ndarray, list[str]]:
    from mapi_workflows.runtime.stattests import variable_matrix

    raw = ctx.params.get("vars")
    names = [str(v) for v in raw] if isinstance(raw, list) else []
    if len(names) < 2:
        raise data_error("name at least two series in 'vars'")
    mat, _ = variable_matrix(ctx, names)
    return mat, names


def _var_lag(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.api import VAR

    mat, names = variable_matrix(ctx)
    maxlags = int(clean_number(ctx.params.get("maxlags")) or 8)
    criterion = str(ctx.params.get("criterion") or "aic")
    if criterion == "aicc":
        criterion = "aic"
    if mat.shape[0] <= maxlags * len(names) + 10:
        raise data_error(f"too few observations ({mat.shape[0]}) for {maxlags} lags")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sel = VAR(mat).select_order(maxlags=maxlags)
    by: dict[str, JsonValue] = {k: int(v) for k, v in sel.selected_orders.items()}
    best = int(sel.selected_orders[criterion])
    rows: list[dict[str, JsonValue]] = []
    for lag in range(maxlags + 1):
        row: dict[str, JsonValue] = {"lag": lag}
        for crit in ("aic", "bic", "hqic", "fpe"):
            vals = sel.ics.get(crit)
            row[crit] = clean_number(vals[lag]) if vals is not None and lag < len(vals) else None
        rows.append(row)
    table = TablePayload(
        title="Lag order of the series together",
        kind="lag_search",
        columns=["lag", "aic", "bic", "hqic", "fpe"],
        rows=rows,
        meta={
            "criterion": criterion,
            "vars": list(names),
            "summary_plain": f"{criterion.upper()} picks {best} lag(s).",
        },
    )
    return NodeOutcome(
        result={"best_lag": best, "by_criterion": by, "criterion": criterion},
        outputs={"table": table.model_dump(mode="json")},
    )


def _coint_rank(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.tsa.vector_ar.vecm import select_coint_rank

    mat, names = variable_matrix(ctx)
    det = int(clean_number(ctx.params.get("det_order")) or 0)
    k = ctx.params.get("k_ar_diff")
    k_ar = int(k) if isinstance(k, int) else 1
    method = str(ctx.params.get("method") or "trace")
    signif = float(str(ctx.params.get("signif") or "0.05"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = select_coint_rank(mat, det, k_ar, method=method, signif=signif)
    rows: list[dict[str, JsonValue]] = [
        {"r_0": int(r0), "r_1": int(r1), "statistic": float(s), "critical_value": float(c)}
        for r0, r1, s, c in zip(res.r_0, res.r_1, res.test_stats, res.crit_vals, strict=True)
    ]
    table = TablePayload(
        title="Cointegration rank tests",
        kind="rank_search",
        columns=["r_0", "r_1", "statistic", "critical_value"],
        rows=rows,
        meta={
            "method": method,
            "signif": signif,
            "vars": list(names),
            "summary_plain": f"Selected rank {int(res.rank)}.",
        },
    )
    return NodeOutcome(
        result={"rank": int(res.rank), "method": method},
        outputs={"table": table.model_dump(mode="json")},
    )


def _js(v: Any) -> JsonValue:
    if isinstance(v, float):
        return clean_number(v)
    return v  # type: ignore[no-any-return]


SEARCH_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "search.arma_order": _arma,
    "search.sarima_order": _sarima,
    "search.var_lag_order": _var_lag,
    "search.coint_rank": _coint_rank,
}
