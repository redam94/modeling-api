"""Nodes on posterior draws (M5): ``model.summary``, ``evaluate.ppc`` and
``compare.stacking``.

* ``model.summary`` summarises the posterior of a fitted model: mean, sd, the HDI
  (``hdi_prob``, 0.94 by default: the 3 % and 97 % bounds), rank-normalised split R-hat and
  bulk ESS of each selected parameter (vectors flattened to ``name[label]``), plus the
  number of divergent transitions. Conditions read ``nodes.<id>.params.<name>.hdi_low``.
* ``evaluate.ppc`` draws from the posterior predictive distribution through the score path
  (``predict`` with ``output.kind: predictive``) on the fit data and checks it against the
  observed target: central 50 % and 90 % interval coverage, the Kolmogorov-Smirnov distance
  of the PIT values from uniform, and a posterior predictive p-value of a statistic
  (``mean``, ``sd``, ``max`` or ``min``).
* ``compare.stacking`` weighs candidate models by their leave-one-out predictive densities
  (Yao, Vehtari, Simpson and Gelman 2018): stacking maximises the log score of the weighted
  mixture over the simplex; pseudo-BMA+ averages Akaike-type weights over a Bayesian
  bootstrap of the pointwise elpd. Both read the pointwise elpd that ``model.fit`` stores
  when its ``evaluate`` asks for ``elpd_loo`` (or ``elpd_waic``). Without pointwise values
  the node falls back to pseudo-BMA weights from elpd and its standard error and says so.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_spec.capabilities import Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_workflows.payloads import (
    ElpdPayload,
    MetricsCandidate,
    MetricsPayload,
    TablePayload,
    clean_number,
)
from mapi_workflows.runtime.data import data_error
from mapi_workflows.runtime.models import _registry, _request, _target_names
from mapi_workflows.runtime.values import ModelValue, NodeContext, NodeOutcome
from pydantic import JsonValue, ValidationError

__all__ = [
    "MAX_CONVERGENCE_ENTRIES",
    "MAX_SUMMARY_ENTRIES",
    "POSTERIOR_NODES",
    "convergence",
    "posterior_summary",
    "ppc_checks",
    "pseudo_bma_weights",
    "stacking_weights",
]

MAX_SUMMARY_ENTRIES: Final = 200
MAX_CONVERGENCE_ENTRIES: Final = 20_000
"""Values ``max_rhat``/``min_ess`` of ``model.summary`` check (all of the selected
variables up to this many; the table keeps only :data:`MAX_SUMMARY_ENTRIES`)."""
"""The most parameter entries (vector elements included) ``model.summary`` reports."""
_DRAW_DIMS: Final = ("chain", "draw")


def _stats() -> Any:
    # rank-normalised R-hat, bulk ESS, HDI and PIT: the numpy implementations the plot
    # recipes and the runner's scorecard use (no ArviZ needed)
    from mapi_plots.recipes import _stats

    return _stats


def _num(v: float) -> float | None:
    return clean_number(v)


def _param(ctx: NodeContext, name: str, default: float) -> float:
    """A numeric parameter (``0`` is a value, not a missing one)."""
    v = clean_number(ctx.params.get(name))
    return default if v is None else v


# ============================================================================ model.summary


def _label(value: Any) -> str:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _entries(
    name: str, da: xr.DataArray
) -> list[tuple[str, np.ndarray]]:  # (entry name, (chain, draw) array)
    other = [str(d) for d in da.dims if d not in _DRAW_DIMS]
    da = da.transpose("chain", "draw", *other)
    values = np.asarray(da.values, dtype=float)
    if not other:
        return [(name, values)]
    flat = values.reshape(values.shape[0], values.shape[1], -1)
    labels = [
        [_label(v) for v in (da[d].values if d in da.coords else range(da.sizes[d]))] for d in other
    ]
    out: list[tuple[str, np.ndarray]] = []
    for k, idx in enumerate(np.ndindex(*[da.sizes[d] for d in other])):
        label = ", ".join(labels[j][i] for j, i in enumerate(idx))
        out.append((f"{name}[{label}]", flat[..., k]))
    return out


def posterior_summary(
    posterior: xr.Dataset,
    names: Sequence[str] | None = None,
    *,
    hdi_prob: float = 0.94,
    max_entries: int = MAX_SUMMARY_ENTRIES,
) -> tuple[dict[str, dict[str, float | None]], bool]:
    """``({entry: {mean, sd, hdi_low, hdi_high, rhat, ess_bulk}}, truncated)`` of the
    posterior variables ``names`` (default: all), vectors flattened to ``name[label]``."""
    st = _stats()
    wanted = list(names) if names else [str(v) for v in posterior.data_vars]
    missing = [v for v in wanted if v not in posterior.data_vars]
    if missing:
        have = ", ".join(sorted(str(v) for v in posterior.data_vars)[:30])
        raise data_error(
            f"the posterior has no variable {', '.join(missing)!s}. The variables are: {have}"
        )
    out: dict[str, dict[str, float | None]] = {}
    truncated = False
    for var in wanted:
        da = posterior[var]
        if not set(_DRAW_DIMS) <= set(map(str, da.dims)):
            continue
        if not np.issubdtype(da.dtype, np.number):
            continue
        for entry, x in _entries(var, da):
            if len(out) >= max_entries:
                truncated = True
                break
            flat = x.ravel()
            ok = flat[np.isfinite(flat)]
            if ok.size == 0:
                out[entry] = dict.fromkeys(
                    ("mean", "sd", "hdi_low", "hdi_high", "rhat", "ess_bulk")
                )
                continue
            lo, hi = st.hdi(ok, hdi_prob) if ok.size >= 2 else (ok[0], ok[0])
            out[entry] = {
                "mean": _num(float(ok.mean())),
                "sd": _num(float(ok.std(ddof=1))) if ok.size > 1 else None,
                "hdi_low": _num(float(lo)),
                "hdi_high": _num(float(hi)),
                "rhat": _num(st.rhat(x)) if x.shape[0] > 1 else None,
                "ess_bulk": _num(st.ess_bulk(x)),
            }
        if truncated:
            break
    return out, truncated


def convergence(
    posterior: xr.Dataset,
    names: Sequence[str] | None = None,
    *,
    max_entries: int = MAX_CONVERGENCE_ENTRIES,
) -> tuple[float | None, float | None, bool]:
    """``(max R-hat, min bulk ESS, complete)`` over every value of ``names`` (default: all
    variables), not only the ones a truncated summary table shows. ``complete`` is False
    when more than ``max_entries`` values exist and only the first ones were checked."""
    st = _stats()
    wanted = list(names) if names else [str(v) for v in posterior.data_vars]
    rhats: list[float] = []
    esss: list[float] = []
    seen = 0
    for var in wanted:
        da = posterior[var]
        if not set(_DRAW_DIMS) <= set(map(str, da.dims)) or not np.issubdtype(da.dtype, np.number):
            continue
        for _, x in _entries(var, da):
            if seen >= max_entries:
                return _max(rhats), _min(esss), False
            seen += 1
            if x.shape[0] > 1 and (r := _num(st.rhat(x))) is not None:
                rhats.append(r)
            if (e := _num(st.ess_bulk(x))) is not None:
                esss.append(e)
    return _max(rhats), _min(esss), True


def _max(v: list[float]) -> float | None:
    return max(v) if v else None


def _min(v: list[float]) -> float | None:
    return min(v) if v else None


def _divergences(bundle: Bundle) -> int | None:
    stats = bundle.model.groups.get("sample_stats")
    if stats is None or "diverging" not in stats:
        return None
    return int(np.asarray(stats["diverging"].values).astype(bool).sum())


def _posterior(mv: ModelValue, node: str) -> xr.Dataset:
    post = mv.bundle.model.groups.get("posterior")
    if post is None or not set(_DRAW_DIMS) <= {str(d) for d in post.dims}:
        raise MapiError(
            f"{node} needs posterior draws, but the model gives only point estimates. Use a "
            "Bayesian model.",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    return post


def _summary_node(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    post = _posterior(mv, "model.summary")
    raw = ctx.params.get("vars")
    names = [str(v) for v in raw] if isinstance(raw, list) else None
    prob = _param(ctx, "hdi_prob", 0.94)
    params, truncated = posterior_summary(post, names, hdi_prob=prob)
    if not params:
        raise data_error("the posterior has no numeric variables with draws to summarise")
    if truncated:
        # the gate must see every value, not only the ones the table keeps
        max_rhat, min_ess, complete = convergence(post, names)
    else:
        rhats = [v["rhat"] for v in params.values() if v["rhat"] is not None]
        esss = [v["ess_bulk"] for v in params.values() if v["ess_bulk"] is not None]
        max_rhat, min_ess, complete = _max(rhats), _min(esss), True
    n_div = _divergences(mv.bundle)
    cols = ["param", "mean", "sd", "hdi_low", "hdi_high", "rhat", "ess_bulk"]
    rows: list[dict[str, JsonValue]] = [{"param": k, **v} for k, v in params.items()]
    table = TablePayload(
        title="Posterior summary",
        kind="posterior_summary",
        columns=cols,
        rows=rows,
        meta={
            "hdi_prob": prob,
            "n_chains": int(post.sizes["chain"]),
            "n_draws": int(post.sizes["draw"]),
            "truncated": truncated,
            "model": mv.model,
        },
    )
    warns: list[str] = []
    if truncated:
        warns.append(
            f"the summary shows only the first {MAX_SUMMARY_ENTRIES} values. Select fewer "
            "variables in vars."
        )
    if not complete:
        warns.append(
            f"max_rhat and min_ess check only the first {MAX_CONVERGENCE_ENTRIES} values. "
            "Select fewer variables in vars."
        )
    result: dict[str, JsonValue] = {
        "params": {k: dict(v) for k, v in params.items()},
        "max_rhat": max_rhat,
        "min_ess": min_ess,
        "n_divergent": n_div,
        "hdi_prob": prob,
        "n_params": len(params),
        "truncated": truncated,
    }
    return NodeOutcome(
        result=result, outputs={"table": table.model_dump(mode="json")}, warnings_plain=warns
    )


# ============================================================================== evaluate.ppc

_STATISTICS: Final[dict[str, Callable[..., Any]]] = {
    "mean": np.mean,
    "sd": lambda a, axis=None: np.std(a, axis=axis, ddof=1),
    "max": np.max,
    "min": np.min,
}


def _ks_uniform(u: np.ndarray) -> float:
    """The Kolmogorov-Smirnov distance between the ECDF of ``u`` and the uniform CDF."""
    x = np.sort(np.clip(u, 0.0, 1.0))
    n = x.size
    i = np.arange(1, n + 1)
    return float(max(np.max(i / n - x), np.max(x - (i - 1) / n)))


def ppc_checks(
    draws: np.ndarray, observed: np.ndarray, statistic: str = "mean"
) -> dict[str, float]:
    """Coverage of the central 50 % and 90 % intervals, the PIT KS distance and the
    posterior predictive p-value ``P(T(y_rep) >= T(y))`` (ties count half) of ``statistic``.

    ``draws`` is ``(S, N)`` predictive draws and ``observed`` ``(N,)``; cells with a missing
    observation are dropped."""
    st = _stats()
    d = np.asarray(draws, dtype=float)
    y = np.asarray(observed, dtype=float)
    ok = np.isfinite(y) & np.all(np.isfinite(d), axis=0)
    d, y = d[:, ok], y[ok]
    if y.size < 2 or d.shape[0] < 10:
        raise data_error(
            "the predictive check needs at least 2 observed values and 10 predictive draws"
        )
    out: dict[str, float] = {}
    for level, key in ((0.5, "coverage_50"), (0.9, "coverage_90")):
        lo, hi = np.quantile(d, [(1 - level) / 2, (1 + level) / 2], axis=0)
        out[key] = float(np.mean((y >= lo) & (y <= hi)))
    out["pit_ks"] = _ks_uniform(st.pit_values(d, y))
    fn = _STATISTICS[statistic]
    t_obs = float(fn(y))
    t_rep = np.asarray(fn(d, axis=1), dtype=float)
    out["p_value"] = float(np.mean(t_rep > t_obs) + 0.5 * np.mean(t_rep == t_obs))
    out["n_obs"] = float(y.size)
    out["n_draws"] = float(d.shape[0])
    return out


def _draw_matrix(da: xr.DataArray, obs: xr.DataArray, n_draws: int) -> tuple[np.ndarray, ...]:
    """``(S, N)`` predictive draws and ``(N,)`` observations aligned on their shared dims."""
    if "sample" in da.dims:
        da = da.rename(sample="_s")
    elif set(_DRAW_DIMS) <= set(da.dims):
        da = da.stack(_s=_DRAW_DIMS)
    else:
        raise MapiError(
            "evaluate.ppc needs predictive draws, but the model returned summaries only.",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    shared = [str(d) for d in obs.dims if d in da.dims]
    extra = [str(d) for d in da.dims if d != "_s" and d not in shared]
    if extra or len(shared) != len(obs.dims):
        raise data_error(
            "the predictions and the observed target have different dimensions "
            f"({', '.join(map(str, da.dims))} vs {', '.join(map(str, obs.dims))})"
        )
    a, b = xr.align(da, obs, join="inner")
    s = a.sizes["_s"]
    pick = np.unique(np.linspace(0, s - 1, min(s, n_draws)).round().astype(int))
    a = a.isel(_s=pick).transpose("_s", *shared)
    mat = np.asarray(a.values, dtype=float).reshape(len(pick), -1)
    return mat, np.asarray(b.transpose(*shared).values, dtype=float).ravel()


def _ppc_node(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    if mv.data is None:
        raise data_error("the fit data of the model is not available for the predictive check")
    bundle = mv.bundle
    caps = bundle.model.manifest.capabilities
    if caps.representation is not Representation.DRAWS or "predict" not in caps.score_modes:
        raise MapiError(
            "evaluate.ppc needs a model that predicts with draws (representation 'draws' and "
            "the 'predict' score mode).",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    statistic = str(ctx.params.get("statistic") or "mean")
    if statistic not in _STATISTICS:
        raise data_error(f"unknown statistic {statistic!r}: use mean, sd, max or min")
    n_draws = int(_param(ctx, "n_draws", 400))
    p_min = _param(ctx, "p_min", 0.025)
    tol = _param(ctx, "coverage_tolerance", 0.1)
    ks_max = clean_number(ctx.params.get("pit_ks_max"))
    tree = score_bundle(
        bundle,
        _request(
            bundle.spec,
            "predict",
            output={"kind": "predictive", "draws": min(n_draws, 10_000)},
            seed=int(ctx.seed) % (2**32),
        ),
        data=mv.data,
        registry=_registry(ctx),
        return_full=True,
        tree=mv.tree,
    )
    pred = tree["predictions"].to_dataset()
    out_var, _, target = _target_names(bundle)
    if out_var not in pred or target not in mv.data:
        raise data_error(f"the predictions have no {out_var!r} for the target {target!r}")
    draws, observed = _draw_matrix(pred[out_var], mv.data[target], n_draws)
    checks = ppc_checks(draws, observed, statistic)
    limit = ks_max if ks_max is not None else 1.36 / math.sqrt(checks["n_obs"])
    ok = (
        abs(checks["coverage_50"] - 0.5) <= tol
        and abs(checks["coverage_90"] - 0.9) <= tol
        and p_min <= checks["p_value"] <= 1 - p_min
        and checks["pit_ks"] <= limit
    )
    p_values: dict[str, JsonValue] = {target: checks["p_value"]}
    metrics = {
        "coverage_50": checks["coverage_50"],
        "coverage_90": checks["coverage_90"],
        "pit_ks": checks["pit_ks"],
        f"ppc_p_{statistic}": checks["p_value"],
    }
    cand = MetricsCandidate(
        name=ctx.node_id,
        group=ctx.node_id,
        kind="model",
        model=mv.model,
        settings=dict(mv.settings) or None,
        metrics={k: clean_number(v) for k, v in metrics.items()},
    )
    if ok:
        plain = "The predictions of the model agree with the observed data."
    else:
        plain = (
            "The predictions of the model do not agree with the observed data: look at the "
            "coverage, the PIT distance and the p-value."
        )
    result: dict[str, JsonValue] = {
        "coverage_50": checks["coverage_50"],
        "coverage_90": checks["coverage_90"],
        "pit_ks": checks["pit_ks"],
        "pit_ks_limit": limit,
        "p_values": p_values,
        "statistic": statistic,
        "ok": ok,
        "n_obs": int(checks["n_obs"]),
        "n_draws": int(checks["n_draws"]),
        "verdict_plain": plain,
    }
    return NodeOutcome(
        result=result,
        outputs={"metrics": MetricsPayload(candidates=[cand]).model_dump(mode="json")},
    )


# ========================================================================== compare.stacking


def _softmax(z: np.ndarray, axis: int = -1) -> np.ndarray:
    z = z - np.max(z, axis=axis, keepdims=True)
    e = np.exp(z)
    out: np.ndarray = e / e.sum(axis=axis, keepdims=True)
    return out


def stacking_weights(lpd: np.ndarray) -> np.ndarray:
    """Stacking weights of ``K`` models from their ``(N, K)`` pointwise log predictive
    densities: ``argmax_w sum_i log sum_k w_k exp(lpd_ik)`` over the simplex (SLSQP)."""
    from scipy.optimize import minimize

    x = np.asarray(lpd, dtype=float)
    n, k = x.shape
    if k == 1:
        return np.ones(1)
    dens = np.exp(x - x.max(axis=1, keepdims=True))  # the row shift does not change argmax

    def loss(w: np.ndarray) -> tuple[float, np.ndarray]:
        mix = np.maximum(dens @ w, 1e-300)
        return float(-np.sum(np.log(mix)) / n), -(dens / mix[:, None]).sum(axis=0) / n

    res = minimize(
        loss,
        np.full(k, 1.0 / k),
        jac=True,
        method="SLSQP",
        bounds=[(0.0, 1.0)] * k,
        constraints=[
            {"type": "eq", "fun": lambda w: float(w.sum() - 1.0), "jac": lambda w: np.ones_like(w)}
        ],
        options={"maxiter": 500, "ftol": 1e-12},
    )
    w = np.clip(np.asarray(res.x, dtype=float), 0.0, None)
    return w / w.sum() if w.sum() > 0 else np.full(k, 1.0 / k)


_PBMA_BLOCK_VALUES = 262_144
"""Dirichlet weights one block of the pseudo-BMA+ bootstrap holds (2 MB of float64)."""


def pseudo_bma_weights(lpd: np.ndarray, *, n_bootstrap: int = 1000, seed: int = 0) -> np.ndarray:
    """Pseudo-BMA+ weights: Akaike-type weights ``softmax(elpd)`` averaged over a Bayesian
    bootstrap (Dirichlet(1) weights on the ``N`` points) of the ``(N, K)`` pointwise elpd."""
    x = np.asarray(lpd, dtype=float)
    n = x.shape[0]
    rng = np.random.default_rng(seed)
    # the (B, N) Dirichlet weights in blocks of about 2 MB: B = N = 10 000 would be 800 MB
    block = max(1, _PBMA_BLOCK_VALUES // max(n, 1))
    total = np.zeros(x.shape[1])
    for start in range(0, n_bootstrap, block):
        alpha = rng.dirichlet(np.ones(n), size=min(block, n_bootstrap - start))  # (b, N)
        total += _softmax(n * (alpha @ x), axis=1).sum(axis=0)
    out: np.ndarray = total / n_bootstrap
    return out


def _normal_pseudo_bma(
    elpd: np.ndarray, se: np.ndarray | None, *, n_bootstrap: int, seed: int
) -> np.ndarray:
    """Pseudo-BMA weights from elpd alone, averaged over normal draws of elpd ± se when
    the standard errors are known (the models are treated as independent)."""
    if se is None:
        return _softmax(elpd)
    rng = np.random.default_rng(seed)
    z = elpd[None, :] + se[None, :] * rng.standard_normal((n_bootstrap, elpd.size))
    out: np.ndarray = _softmax(z, axis=1).mean(axis=0)
    return out


def _stack_candidates(
    payloads: Sequence[Mapping[str, Any]], metric: str
) -> list[tuple[str, MetricsCandidate, ElpdPayload | None]]:
    out: list[tuple[str, MetricsCandidate, ElpdPayload | None]] = []
    seen: set[str] = set()
    for raw in payloads:
        try:
            mp = MetricsPayload.model_validate(raw)
        except ValidationError as exc:
            raise data_error(f"not a metrics payload: {str(exc)[:200]}") from None
        for c in mp.candidates:
            if c.key in seen:
                continue
            seen.add(c.key)
            detail = (c.elpd or {}).get(metric)
            if detail is None and clean_number(c.metrics.get(metric)) is not None:
                detail = ElpdPayload(elpd=float(c.metrics[metric]))  # type: ignore[arg-type]
            out.append((c.key, c, detail))
    return out


def _stacking_node(ctx: NodeContext) -> NodeOutcome:
    metric = str(ctx.params.get("metric") or "elpd_loo")
    method = str(ctx.params.get("method") or "stacking")
    n_boot = int(_param(ctx, "n_bootstrap", 1000))
    cands = _stack_candidates(ctx.json("candidates"), metric)
    if len(cands) < 2:
        raise data_error("compare.stacking needs at least two candidates")
    lacking = [k for k, _, d in cands if d is None]
    if lacking:
        raise data_error(
            f"these candidates have no {metric}: {', '.join(lacking)}. Add "
            f"'evaluate: {{metrics: [{metric}]}}' to their model.fit nodes."
        )
    keys = [k for k, _, _ in cands]
    details = [d for _, _, d in cands if d is not None]
    elpd = np.array([d.elpd for d in details])
    lengths = {len(d.pointwise) if d.pointwise is not None else -1 for d in details}
    notes: list[str] = []
    seed = int(ctx.seed) % (2**32)
    if len(lengths) == 1 and -1 not in lengths:
        lpd = np.column_stack([np.asarray(d.pointwise, dtype=float) for d in details])
        pbma = pseudo_bma_weights(lpd, n_bootstrap=n_boot, seed=seed)
        if method == "stacking":
            weights, used = stacking_weights(lpd), "stacking"
        else:
            weights, used = pbma, "pseudo_bma_plus"
    else:
        ses = [d.se for d in details]
        se = np.array(ses, dtype=float) if all(s is not None for s in ses) else None
        pbma = _normal_pseudo_bma(elpd, se, n_bootstrap=n_boot, seed=seed)
        weights, used = pbma, "pseudo_bma" if se is None else "pseudo_bma_se"
        why = (
            "the candidates have pointwise values of different lengths (different data)"
            if -1 not in lengths
            else "at least one candidate has no pointwise values (more than 10 000 data "
            "points, or an older fit)"
        )
        notes.append(
            f"Stacking was not possible: {why}. The weights are pseudo-BMA weights from "
            f"{metric}" + (" and its standard error." if se is not None else ".")
        )
    order = np.argsort(-weights, kind="stable")
    best = keys[int(order[0])]
    rows: list[dict[str, JsonValue]] = []
    for i in order:
        d = details[int(i)]
        rows.append(
            {
                "candidate": keys[int(i)],
                "model": cands[int(i)][1].model,
                metric: _num(d.elpd),
                "se": _num(d.se) if d.se is not None else None,
                "d_elpd": _num(d.elpd - float(elpd.max())),
                "weight": _num(float(weights[int(i)])),
                "pseudo_bma": _num(float(pbma[int(i)])),
            }
        )
    table = TablePayload(
        title=f"Model weights ({used})",
        kind="stacking",
        columns=["candidate", "model", metric, "se", "d_elpd", "weight", "pseudo_bma"],
        rows=rows,
        meta={"metric": metric, "method": used, "n_bootstrap": n_boot, "notes": list(notes)},
    )
    result: dict[str, JsonValue] = {
        "weights": {keys[i]: _num(float(weights[i])) for i in range(len(keys))},
        "pseudo_bma": {keys[i]: _num(float(pbma[i])) for i in range(len(keys))},
        "best": best,
        "method": used,
        "metric": metric,
        "note_plain": " ".join(notes) or None,
    }
    return NodeOutcome(
        result=result, outputs={"table": table.model_dump(mode="json")}, warnings_plain=notes
    )


POSTERIOR_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "model.summary": _summary_node,
    "evaluate.ppc": _ppc_node,
    "compare.stacking": _stacking_node,
}
