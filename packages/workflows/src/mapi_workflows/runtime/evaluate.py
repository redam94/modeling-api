"""Evaluation nodes: metrics, simple benchmarks, residual checks and Diebold-Mariano.

* ``evaluate.baseline`` scores naive (last value), seasonal naive (the value one season
  earlier), mean and drift forecasts on exactly the folds of a split plan, with the same
  error measures as ``model.backtest`` (so ``compare.models`` compares like with like).
* ``evaluate.metrics`` reads a backtest's or a fitted model's holdout metrics.
* ``evaluate.residuals`` runs Ljung-Box, Jarque-Bera and a spread test (Breusch-Pagan on the
  drivers, else ARCH-LM) on a model's in-sample residuals.
* ``compare.diebold_mariano`` tests whether two candidates' backtest errors at one horizon
  differ, with the Harvey-Leybourne-Newbold small-sample correction.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import numpy as np
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.stattests import StatTestResult
from mapi_workflows.payloads import (
    MetricsCandidate,
    MetricsPayload,
    TablePayload,
    clean_number,
)
from mapi_workflows.runtime.data import data_error, schema_of, season_length, target_var, time_dim
from mapi_workflows.runtime.models import (
    FoldForecast,
    candidate,
    cells_of,
    mase_scale,
    model_residuals,
    plan_dim,
    plan_fold_list,
    split_payload,
)
from mapi_workflows.runtime.values import NodeContext, NodeOutcome
from pydantic import JsonValue, ValidationError
from scipy import stats as sps

__all__ = ["EVALUATE_NODES", "baseline_forecast", "diebold_mariano"]


# ================================================================================ baselines


def baseline_forecast(train: np.ndarray, steps: int, method: str, season: int) -> np.ndarray:
    """Forecasts ``1..steps`` ahead of a training block ``(t, cells)``."""
    t = train.shape[0]
    out = np.empty((steps, train.shape[1]))
    if method == "naive":
        out[:] = train[-1]
    elif method == "mean":
        out[:] = np.nanmean(train, axis=0)
    elif method == "drift":
        slope = (train[-1] - train[0]) / max(t - 1, 1)
        for h in range(1, steps + 1):
            out[h - 1] = train[-1] + h * slope
    elif method == "seasonal_naive":
        m = season if season < t else 1
        for h in range(1, steps + 1):
            k = (h - 1) // m + 1
            out[h - 1] = train[t + h - 1 - k * m]
    else:
        raise MapiError(f"unknown benchmark {method!r}", code=ErrorCode.WORKFLOW_INVALID)
    return out


def _baseline(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    plan = split_payload(ctx)
    if plan is None:
        raise data_error("evaluate.baseline needs a split plan")
    schema = schema_of(dv)
    ds = dv.data
    tdim = time_dim(ds, schema, plan_dim(plan))
    var = ctx.params.get("var")
    target = target_var(ds, schema, var if isinstance(var, str) else None)
    season_p = clean_number(ctx.params.get("season"))
    try:
        season = season_length(ds, schema, tdim, int(season_p) if season_p else None)
    except MapiError:
        season = 1
    raw_methods = ctx.params.get("methods") or ["naive", "seasonal_naive"]
    methods = [str(m) for m in raw_methods] if isinstance(raw_methods, list) else ["naive"]
    y = cells_of(ds[target], tdim)
    folds = plan_fold_list(plan)
    cands: list[MetricsCandidate] = []
    for method in methods:
        fcs: list[FoldForecast] = []
        for f in folds:
            if f.get("test_groups") is not None:
                raise data_error("benchmarks need time folds (not held-out groups)")
            s0, s1 = int(f["test_start"]), int(f["test_stop"])
            kfold = f.get("train_stop") is not None and int(f["train_stop"]) > s0
            origin = s0 - _gap_before(f, s0) if kfold else int(f["train_stop"])
            start = 0 if kfold else int(f.get("train_start") or 0)
            train = y[start:origin]
            train = train[np.all(np.isfinite(train), axis=1)] if train.size else train
            if train.shape[0] < 2:
                raise data_error("a fold has fewer than two training periods for the benchmark")
            steps = s1 - origin
            fc = baseline_forecast(train, steps, method, season)[s0 - origin :]
            fcs.append(FoldForecast(y[s0:s1], fc, None, None, mase_scale(train, season)))
        cands.append(candidate(method, ctx.node_id, fcs, plan, kind="baseline"))
    payload = MetricsPayload(
        candidates=cands, horizons=[int(h) for h in cands[0].by_horizon] if cands else []
    )
    by_method: dict[str, JsonValue] = {c.name: dict(c.metrics) for c in cands}
    key = "mase" if all(c.metrics.get("mase") is not None for c in cands) else "mae"
    best = min(cands, key=lambda c: c.metrics.get(key) or math.inf).name
    return NodeOutcome(
        result={
            "by_method": by_method,
            "best_method": best,
            "season": season,
            "n_folds": len(folds),
        },
        outputs={"metrics": payload.model_dump(mode="json")},
    )


def _gap_before(f: Mapping[str, Any], s0: int) -> int:
    """Periods excluded just before a k-fold test block (its purge/gap)."""
    gap = 0
    for a, b in f.get("excluded") or []:
        if int(b) == s0:
            gap = max(gap, int(b) - int(a))
    return gap


# ================================================================================== metrics


def _metrics(ctx: NodeContext) -> NodeOutcome:
    wanted_raw = ctx.params.get("metrics")
    wanted = [str(m) for m in wanted_raw] if isinstance(wanted_raw, list) else []
    payloads = ctx.json("backtest")
    cands: list[MetricsCandidate] = []
    in_sample = False
    if payloads:
        try:
            mp = MetricsPayload.model_validate(payloads[0])
        except ValidationError as exc:
            raise data_error(f"not a metrics payload: {str(exc)[:200]}") from None
        cands = list(mp.candidates)
    else:
        mv = ctx.model("model", required=False)
        if mv is None:
            raise data_error("evaluate.metrics needs a model or backtest metrics")
        metrics = {k: clean_number(v) for k, v in (mv.bundle.metrics.values or {}).items()}
        if not metrics:
            res = model_residuals(mv, ctx.registry)
            e = res.resid
            metrics = {"mae": float(np.mean(np.abs(e))), "rmse": float(np.sqrt(np.mean(e**2)))}
            in_sample = True
        cands = [
            MetricsCandidate(name=ctx.node_id, group=ctx.node_id, model=mv.model, metrics=metrics)
        ]
    if wanted:
        cands = [
            c.model_copy(update={"metrics": {k: v for k, v in c.metrics.items() if k in wanted}})
            for c in cands
        ]
    first = cands[0] if cands else None
    result: dict[str, JsonValue] = dict(first.metrics) if first else {}
    if first is not None and first.by_horizon:
        result["by_horizon"] = {h: dict(m) for h, m in first.by_horizon.items()}
    result["in_sample"] = in_sample
    return NodeOutcome(
        result=result, outputs={"metrics": MetricsPayload(candidates=cands).model_dump(mode="json")}
    )


# ================================================================================ residuals


def _residuals(ctx: NodeContext) -> NodeOutcome:
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch, het_breuschpagan
    from statsmodels.stats.stattools import jarque_bera

    mv = ctx.model("model")
    assert mv is not None
    res = model_residuals(mv, ctx.registry)
    e = res.resid
    if len(e) < 12:
        raise data_error("too few residuals to check (at least 12)")
    alpha = float(clean_number(ctx.params.get("alpha")) or 0.05)
    lags = int(clean_number(ctx.params.get("lags")) or 10)
    use = min(lags, len(e) // 2 - 1)
    lb = acorr_ljungbox(e, lags=[use], return_df=True)
    lb_p = float(lb["lb_pvalue"].iloc[0])
    _jb, jb_p, skew, kurt = jarque_bera(e)
    if res.exog is not None and res.exog.shape[1]:
        x = np.column_stack([np.ones(len(e)), res.exog])
        _, sp_p, _, _ = het_breuschpagan(e, x)
        spread = "Breusch-Pagan"
    else:
        _, sp_p, _, _ = het_arch(e, nlags=min(4, len(e) // 4))
        spread = "ARCH-LM"
    white, normal, equal = lb_p >= alpha, float(jb_p) >= alpha, float(sp_p) >= alpha
    rows: list[dict[str, JsonValue]] = [
        {
            "check": "Ljung-Box",
            "p_value": lb_p,
            "passed": white,
            "plain": "No pattern over time is left."
            if white
            else "A pattern over time is left: the model misses some structure.",
        },
        {
            "check": "Jarque-Bera",
            "p_value": float(jb_p),
            "passed": normal,
            "plain": "Errors are consistent with a bell curve."
            if normal
            else "Errors are skewed or heavy-tailed: ranges may be off.",
        },
        {
            "check": spread,
            "p_value": float(sp_p),
            "passed": equal,
            "plain": "Errors have a steady spread."
            if equal
            else "Errors are bigger in some places than others.",
        },
    ]
    table = TablePayload(
        title="Residual checks",
        kind="residuals",
        columns=["check", "p_value", "passed", "plain"],
        rows=rows,
        meta={"skew": clean_number(skew), "kurtosis": clean_number(kurt), "n": len(e), "lags": use},
    )
    return NodeOutcome(
        result={
            "white_noise": white,
            "normal": normal,
            "equal_spread": equal,
            "ljung_box_p": lb_p,
            "jarque_bera_p": float(jb_p),
            "spread_p": float(sp_p),
        },
        outputs={"table": table.model_dump(mode="json")},
    )


# ============================================================================ Diebold-Mariano


def diebold_mariano(
    e1: Sequence[float], e2: Sequence[float], *, h: int = 1, loss: str = "squared"
) -> tuple[float, float, int]:
    """``(statistic, p_value, n)`` of the Diebold-Mariano test with the Harvey, Leybourne
    and Newbold (1997) correction; p-values from Student's t with ``n - 1`` df. A negative
    statistic means the first forecast has the smaller loss."""
    a, b = np.asarray(e1, float), np.asarray(e2, float)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    n = len(a)
    if n < 3:
        raise ValueError("needs at least three paired errors")
    d = (a**2 - b**2) if loss == "squared" else (np.abs(a) - np.abs(b))
    dbar = float(d.mean())
    gamma = [float(np.mean((d[k:] - dbar) * (d[: n - k] - dbar))) for k in range(h)]
    var = (gamma[0] + 2 * sum(gamma[1:])) / n
    if var <= 0:
        raise ValueError("the loss differences have no variance")
    dm = dbar / math.sqrt(var)
    hln = math.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    stat = dm * hln
    p = float(2 * sps.t.sf(abs(stat), df=n - 1))
    return float(stat), p, n


def _errors(payload: Mapping[str, Any], h: int) -> tuple[str, list[float]]:
    mp = MetricsPayload.model_validate(payload)
    cands = [c for c in mp.candidates if c.errors]
    if not cands:
        raise data_error(
            "the metrics carry no forecast errors (use model.backtest or evaluate.baseline)"
        )
    c = min(cands, key=lambda c: c.metrics.get("mae") or math.inf)
    out = []
    for fold in c.errors or []:
        if len(fold) >= h and fold[h - 1] is not None:
            out.append(float(fold[h - 1]))  # type: ignore[arg-type]
    return c.key, out


def _dm(ctx: NodeContext) -> NodeOutcome:
    pa, pb = ctx.json("a"), ctx.json("b")
    if not pa or not pb:
        raise data_error("compare.diebold_mariano needs two metrics inputs")
    h = int(clean_number(ctx.params.get("horizon")) or 1)
    loss = str(ctx.params.get("loss") or "squared")
    alpha = float(clean_number(ctx.params.get("alpha")) or 0.05)
    name_a, ea = _errors(pa[0], h)
    name_b, eb = _errors(pb[0], h)
    try:
        stat, p, n = diebold_mariano(ea, eb, h=h, loss=loss)
    except ValueError as exc:
        raise data_error(f"compare.diebold_mariano: {exc}") from None
    reject = p < alpha
    if reject:
        better = name_a if stat < 0 else name_b
        decision = "a_better" if stat < 0 else "b_better"
        verdict = f"{better}'s forecasts are more accurate at horizon {h}, beyond luck."
    else:
        decision = "no_difference"
        verdict = f"The difference between {name_a} and {name_b} at horizon {h} may be luck."
    res = StatTestResult(
        test="test.diebold_mariano",
        statistic=stat,
        p_value=p,
        alpha=alpha,
        reject=reject,
        decision=decision,
        verdict_plain=verdict[:500],
        null_plain="The two forecasts are equally accurate.",
        n_obs=n,
        details={"a": name_a, "b": name_b, "loss": loss, "horizon": h},
    )
    body = res.model_dump(mode="json")
    return NodeOutcome(result=body, outputs={"result": body})


EVALUATE_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "evaluate.metrics": _metrics,
    "evaluate.baseline": _baseline,
    "evaluate.residuals": _residuals,
    "compare.diebold_mariano": _dm,
}
