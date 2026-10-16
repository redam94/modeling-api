"""Model nodes in the runtime: ``model.fit``, ``model.backtest``, ``model.forecast``,
``search.grid`` and the residuals other nodes test.

Fits go through the SDK harness exactly like a ``fit`` run (:func:`mapi_sdk.harness.fit_spec`
on a generated :class:`~mapi_spec.spec.ModelSpec`), forecasts through the score path
(:func:`mapi_sdk.harness.score_bundle`), so preprocessing is replayed and every model in the
catalog works the same way.

**Derived data.** When a model node's data comes from transform nodes whose steps have a
steps-core equivalent (``transform.steps``, ``transform.select``), the spec's pipeline
starts with those steps and the model is fitted on the *original* dataset: the transform is
part of the bundle and is replayed at score time. A dataset changed by a transform without
one (differences, Box-Cox) is fitted as it is, with a warning.

**Backtests** refit the model on every fold of a split plan. Window folds (expanding,
rolling) train on ``[train_start, train_stop)`` and forecast through the gap to the end of
the test window with the actual future drivers; k-fold folds mask the test block (and its
buffers) in the target and predict it in-sample; geo folds train without the held-out
groups and predict them. Errors are scored per fold, per horizon (the ``h``-th period of
the test window) and overall: MAE, RMSE, MAPE, sMAPE, MASE (scaled by the in-sample
seasonal-naive MAE of each fold's training data) and the 90% range coverage when the model
gives ranges.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.metrics import point_estimate
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.summarise import interp_quantile
from mapi_spec.capabilities import Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema, Role
from mapi_spec.spec import ModelSpec, ScoreRequest
from mapi_workflows.binding import auto_bind, model_input_schema
from mapi_workflows.payloads import MetricsCandidate, MetricsPayload, TablePayload, clean_number
from mapi_workflows.runtime.data import (
    data_error,
    label_of,
    schema_of,
    season_length,
    target_var,
    time_dim,
    with_schema,
)
from mapi_workflows.runtime.values import DatasetValue, ModelValue, NodeContext, NodeOutcome
from pydantic import JsonValue

__all__ = [
    "METRIC_NAMES",
    "FoldForecast",
    "Residuals",
    "backtest_model",
    "build_spec",
    "fold_metrics",
    "forecast_errors",
    "model_residuals",
    "split_payload",
]

METRIC_NAMES: Final = ("mae", "rmse", "mape", "smape", "mase", "coverage_90")
_LOCAL_PROJECT: Final = "workflows"


def _registry(ctx: NodeContext) -> Registry:
    return ctx.registry if ctx.registry is not None else default_registry()


def _hooks(ctx: NodeContext, lo: float = 0.0, hi: float = 1.0) -> FitHooks:
    def progress(frac: float, msg: str) -> None:
        if ctx.progress is not None:
            ctx.progress(lo + (hi - lo) * frac, msg)

    return FitHooks(on_progress=progress, cancel=ctx.cancel, log=ctx.log)


# ================================================================================== specs


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_" else "-" for c in text.lower()).strip("-_")
    return (out or "model")[:60]


def _root(dv: DatasetValue) -> tuple[DatasetValue, list[dict[str, JsonValue]], list[str]]:
    """The dataset a model should be fitted on and the steps to put first in its pipeline."""
    if dv.root is not None and dv.replayable and dv.pipeline:
        return dv.root, list(dv.pipeline), []
    if dv.root is not None and not dv.replayable:
        return (
            dv,
            [],
            [
                "the model is fitted on data changed by a transform that has no preprocessing-step "
                "equivalent (" + "; ".join(dv.notes[-3:]) + "): its forecasts are in that space"
            ],
        )
    return dv, [], []


def build_spec(
    params: Mapping[str, JsonValue],
    data: DatasetValue,
    *,
    name: str,
    project: str = _LOCAL_PROJECT,
    split: Mapping[str, JsonValue] | None = None,
    settings: Mapping[str, JsonValue] | None = None,
    mode: str | None = None,
    registry: Registry | None = None,
) -> tuple[ModelSpec, DatasetValue, list[str]]:
    """The ``ModelSpec`` a model node fits, the dataset to fit it on and warnings."""
    model = params.get("model")
    if not isinstance(model, str) or not model:
        raise MapiError("the node needs a catalog model ('model')", code=ErrorCode.WORKFLOW_INVALID)
    base, carried, warns = _root(data)
    raw_pipeline = params.get("pipeline") or []
    pipeline = [*carried, *(raw_pipeline if isinstance(raw_pipeline, list) else [])]
    raw_fit = params.get("fit")
    fit: dict[str, Any] = dict(raw_fit) if isinstance(raw_fit, dict) else {}
    if mode is not None:
        fit["mode"] = mode
    ref = (
        base.ref
        if base.ref and base.ref.startswith("dataset://")
        else (f"dataset://{project}/{_slug(name)}-data@latest")
    )
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {
            "name": _slug(name),
            "project": project,
            "labels": {"mapi.dev/workflow-node": _slug(name)},
        },
        "data": {"dataset": ref, "bind": params.get("bind") or {}},
        "pipeline": pipeline,
        "model": {
            "use": model if "@" in model else f"{model}@*",
            "settings": _settings(settings, params),
            "fit": fit,
        },
    }
    if registry is not None:
        doc["data"]["bind"] = _auto_bind(
            registry, doc["model"]["use"], doc["model"]["settings"], base, doc["data"]["bind"]
        )
    if split:
        doc["data"]["split"] = dict(split)
    ev = params.get("evaluate")
    if isinstance(ev, dict):
        doc["evaluate"] = ev
    try:
        spec = ModelSpec.model_validate(doc)
    except ValueError as exc:
        raise MapiError(
            "the model node's settings do not make a valid spec",
            code=ErrorCode.WORKFLOW_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None
    return spec, base, warns


def _auto_bind(
    registry: Registry,
    use: str,
    settings: Mapping[str, Any],
    base: DatasetValue,
    given: Mapping[str, Any],
) -> dict[str, Any]:
    """The spec's binding completed from the data's roles (:mod:`mapi_workflows.binding`)."""
    try:
        cls = registry.model(use)
    except MapiError:
        return dict(given)
    schema = model_input_schema(cls.manifest().schema_template, settings)
    if schema is None:
        return dict(given)
    return dict(auto_bind(schema, schema_of(base), given))


def _settings(
    settings: Mapping[str, JsonValue] | None, params: Mapping[str, JsonValue]
) -> dict[str, Any]:
    if settings is not None:
        return dict(settings)
    raw = params.get("settings")
    return dict(raw) if isinstance(raw, dict) else {}


def _metrics_of(bundle: Bundle) -> dict[str, float | None]:
    return {k: clean_number(v) for k, v in (bundle.metrics.values or {}).items()}


def _scorecard(bundle: Bundle) -> str:
    sev = {str(getattr(d, "severity", "info")) for d in bundle.diagnostics}
    if "error" in sev:
        return "fail"
    if "warning" in sev:
        return "warn"
    return "pass"


def _target_names(bundle: Bundle) -> tuple[str, str, str]:
    """``(output var, canonical target, dataset target)`` of a fitted bundle."""
    manifest = bundle.model.manifest
    schema = bundle.model_schema
    targets = list(schema.targets) if schema is not None else []
    for name, decl in manifest.outputs.items():
        src = getattr(decl, "units_of", None)
        if src in targets:
            return name, src, bundle.binding.to_dataset(src)
    if targets and manifest.outputs:
        out = next(iter(manifest.outputs))
        return out, targets[0], bundle.binding.to_dataset(targets[0])
    raise MapiError(
        "the model declares no output for its target", code=ErrorCode.CAPABILITY_UNSUPPORTED
    )


def _request(spec: ModelSpec, mode: str, **extra: Any) -> ScoreRequest:
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"model://{spec.metadata.project}/{spec.metadata.name}@latest",
            "mode": mode,
            "data": "dataset://workflows/payload@latest",
            **extra,
        }
    )


# ============================================================================= model.fit


def split_payload(ctx: NodeContext, port: str = "split") -> dict[str, Any] | None:
    vals = ctx.json(port)
    return vals[0] if vals else None


def fit_node(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    split = None
    plan = split_payload(ctx)
    if plan is not None:
        inner = plan.get("plan") if isinstance(plan.get("plan"), dict) else None
        if not inner or inner.get("kind") != "holdout":
            raise data_error("model.fit takes a holdout plan (split.holdout) as its split")
        split = inner.get("split")
    spec, base, warns = build_spec(
        ctx.params, dv, name=ctx.node_id, split=split, registry=_registry(ctx)
    )
    ctx.info(f"fitting {spec.model.use} on {base.ref or 'the input data'}")
    bundle = fit_spec(spec, base.data, registry=_registry(ctx), hooks=_hooks(ctx))
    metrics = _metrics_of(bundle)
    ref = f"local://{ctx.node_id}/{bundle.bundle_id}"
    mv = ModelValue(
        bundle=bundle,
        data=base.data,
        ref=ref,
        model=str(spec.model.use),
        settings=dict(spec.model.settings),
    )
    cand = MetricsCandidate(
        name=ctx.node_id,
        group=ctx.node_id,
        kind="model",
        model=str(ctx.params.get("model")),
        settings=dict(spec.model.settings),
        metrics=metrics,
    )
    result: dict[str, JsonValue] = {
        "model_version": ref,
        "metrics": dict(metrics),
        "scorecard": _scorecard(bundle),
        "model": str(spec.model.use),
    }
    return NodeOutcome(
        result=result,
        outputs={"model": mv, "metrics": MetricsPayload(candidates=[cand]).model_dump(mode="json")},
        warnings_plain=warns,
    )


# ============================================================================ forecasting


@dataclass(slots=True)
class FoldForecast:
    """Point forecasts, 90% ranges and actuals over one fold's test window."""

    actual: np.ndarray  # (h, cells)
    point: np.ndarray
    lower: np.ndarray | None
    upper: np.ndarray | None
    scale: float | None  # MASE scale of the fold's training data


def _interval(da: xr.DataArray, rep: Representation) -> tuple[xr.DataArray, xr.DataArray] | None:
    try:
        if rep is Representation.DRAWS:
            q = da.quantile([0.05, 0.95], dim=["chain", "draw"])
            return q.isel(quantile=0, drop=True), q.isel(quantile=1, drop=True)
        if rep is Representation.QUANTILES:
            return interp_quantile(da, 0.05), interp_quantile(da, 0.95)
        if rep is Representation.INTERVAL:
            return da.sel(bound="lower", drop=True), da.sel(bound="upper", drop=True)
        if rep is Representation.MOMENTS:
            m, s = da.sel(moment="mean", drop=True), da.sel(moment="sd", drop=True)
            return m - 1.6449 * s, m + 1.6449 * s
    except (KeyError, ValueError, MapiError):
        return None
    return None


def cells_of(da: xr.DataArray, tdim: str) -> np.ndarray:
    others = [d for d in da.dims if d != tdim]
    return np.asarray(da.transpose(tdim, *others).values, dtype=float).reshape(da.sizes[tdim], -1)


def mase_scale(train: np.ndarray, m: int) -> float | None:
    """In-sample seasonal-naive MAE of a training block (``m`` falls back to 1)."""
    for lag in (m, 1):
        if lag < train.shape[0]:
            d = np.abs(train[lag:] - train[:-lag])
            d = d[np.isfinite(d)]
            if d.size and float(d.mean()) > 0:
                return float(d.mean())
    return None


def _score_window(
    ctx: NodeContext,
    spec: ModelSpec,
    data: xr.Dataset,
    tdim: str,
    target: str,
    fold: Mapping[str, Any],
    season: int,
) -> FoldForecast:
    reg = _registry(ctx)
    t0, t1 = int(fold["train_start"]), int(fold["train_stop"])
    s0, s1 = int(fold["test_start"]), int(fold["test_stop"])
    train = data.isel({tdim: slice(t0, t1)})
    bundle = fit_spec(
        spec, train, registry=reg, hooks=_hooks(ctx), keep_evaluation_bundle=False, hash_data=False
    )
    out_var, _canon_target, _ = _target_names(bundle)
    periods = s1 - t1
    canon_t = bundle.binding.to_canonical(tdim)
    future = data.isel({tdim: slice(t1, s1)})
    targets = [
        target,
        *[v for v, s in schema_of(data).vars.items() if s.role is Role.TARGET and v in future],
    ]
    future = future.drop_vars([v for v in set(targets) if v in future])
    req = _request(spec, "forecast", horizon={"dim": canon_t, "periods": periods})
    tree = score_bundle(
        bundle,
        req,
        data=future if future.data_vars else None,
        registry=reg,
        return_full=True,
        history=train,
    )
    pred = tree["predictions"].to_dataset()
    da = pred[out_var]
    rep = Representation(tree.attrs.get("representation", "point"))
    point = point_estimate(da, rep)
    window = slice(s0 - t1, s1 - t1)
    pt = cells_of(point.isel({tdim: window}), tdim)
    iv = _interval(da, rep)
    lo = cells_of(iv[0].isel({tdim: window}), tdim) if iv else None
    hi = cells_of(iv[1].isel({tdim: window}), tdim) if iv else None
    actual = cells_of(data[target].isel({tdim: slice(s0, s1)}), tdim)
    return FoldForecast(actual, pt, lo, hi, mase_scale(cells_of(train[target], tdim), season))


def _allow_missing(ds: xr.Dataset, var: str) -> xr.Dataset:
    """``ds`` whose schema allows missing values in ``var`` (a masked target)."""
    schema = schema_of(ds)
    if var not in schema.vars or schema.vars[var].missing != "forbid":
        return ds
    doc = schema.model_dump()
    doc["vars"][var]["missing"] = "allow"
    return with_schema(ds, DataSchema.model_validate(doc))


def _score_masked(
    ctx: NodeContext,
    spec: ModelSpec,
    data: xr.Dataset,
    tdim: str,
    target: str,
    fold: Mapping[str, Any],
    season: int,
) -> FoldForecast:
    """k-fold: mask the test block and buffers in the target, fit, predict the block."""
    reg = _registry(ctx)
    n = data.sizes[tdim]
    mask = np.zeros(n, dtype=bool)
    for a, b in fold.get("excluded") or []:
        mask[int(a) : int(b)] = True
    s0, s1 = int(fold["test_start"]), int(fold["test_stop"])
    masked = data.copy()
    tgt = masked[target]
    m = (
        xr.DataArray(mask, dims=[tdim], coords={tdim: data[tdim]})
        if tdim in data.coords
        else (xr.DataArray(mask, dims=[tdim]))
    )
    masked[target] = tgt.where(~m)
    masked = _allow_missing(masked, target)
    bundle = fit_spec(
        spec, masked, registry=reg, hooks=_hooks(ctx), keep_evaluation_bundle=False, hash_data=False
    )
    out_var, _, _ = _target_names(bundle)
    tree = score_bundle(
        bundle, _request(spec, "predict"), data=masked, registry=reg, return_full=True
    )
    pred = tree["predictions"].to_dataset()
    da = pred[out_var]
    rep = Representation(tree.attrs.get("representation", "point"))
    point = point_estimate(da, rep)
    pt = cells_of(point.isel({tdim: slice(s0, s1)}), tdim)
    iv = _interval(da, rep)
    lo = cells_of(iv[0].isel({tdim: slice(s0, s1)}), tdim) if iv else None
    hi = cells_of(iv[1].isel({tdim: slice(s0, s1)}), tdim) if iv else None
    actual = cells_of(data[target].isel({tdim: slice(s0, s1)}), tdim)
    train = cells_of(masked[target], tdim)
    return FoldForecast(actual, pt, lo, hi, mase_scale(train, season))


def _score_groups(
    ctx: NodeContext,
    spec: ModelSpec,
    data: xr.Dataset,
    tdim: str,
    target: str,
    fold: Mapping[str, Any],
    gdim: str,
    season: int,
) -> FoldForecast:
    """geo holdout: train without the held-out groups (or without their test window), then
    predict them."""
    reg = _registry(ctx)
    test_groups = list(fold.get("test_groups") or [])
    s0, s1 = fold.get("test_start"), fold.get("test_stop")
    if s0 is None:
        train = data.sel({gdim: [g for g in data[gdim].values if label_of(g) not in test_groups]})
        test = data.sel({gdim: [g for g in data[gdim].values if label_of(g) in test_groups]})
        window = slice(None)
    else:
        train = data.copy()
        held = xr.DataArray(
            [label_of(g) in test_groups for g in data[gdim].values],
            dims=[gdim],
            coords={gdim: data[gdim]},
        )
        tmask = xr.DataArray(
            np.arange(data.sizes[tdim]) >= int(s0), dims=[tdim], coords={tdim: data[tdim]}
        )
        train[target] = data[target].where(~(held & tmask))
        if fold.get("train_start") is None:  # no pre-period: drop held groups entirely
            train[target] = train[target].where(~held)
        test = data.sel({gdim: [g for g in data[gdim].values if label_of(g) in test_groups]})
        window = slice(int(s0), int(s1) if s1 is not None else None)
        train = _allow_missing(train, target)
    bundle = fit_spec(
        spec, train, registry=reg, hooks=_hooks(ctx), keep_evaluation_bundle=False, hash_data=False
    )
    out_var, _, _ = _target_names(bundle)
    tree = score_bundle(
        bundle, _request(spec, "predict"), data=test, registry=reg, return_full=True
    )
    pred = tree["predictions"].to_dataset()
    da = pred[out_var]
    rep = Representation(tree.attrs.get("representation", "point"))
    point = point_estimate(da, rep).isel({tdim: window})
    iv = _interval(da, rep)
    actual = test[target].isel({tdim: window})
    return FoldForecast(
        cells_of(actual, tdim),
        cells_of(point, tdim),
        cells_of(iv[0].isel({tdim: window}), tdim) if iv else None,
        cells_of(iv[1].isel({tdim: window}), tdim) if iv else None,
        mase_scale(cells_of(train[target], tdim), season),
    )


def forecast_errors(fc: FoldForecast) -> np.ndarray:
    """Per test period, the mean forecast error over cells (actual - forecast)."""
    e = fc.actual - fc.point
    with np.errstate(invalid="ignore"):
        out = np.nanmean(np.where(np.isfinite(e), e, np.nan), axis=1)
    return np.asarray(out, dtype=float)


def fold_metrics(
    fcs: Sequence[FoldForecast], rows: Sequence[int] | None = None
) -> dict[str, float | None]:
    """Metrics over the given test-window rows (``None``: all) of every fold."""
    a, p, lo, hi, sc = [], [], [], [], []
    for fc in fcs:
        idx = (
            list(range(fc.actual.shape[0]))
            if rows is None
            else [r for r in rows if r < fc.actual.shape[0]]
        )
        if not idx:
            continue
        a.append(fc.actual[idx].reshape(-1))
        p.append(fc.point[idx].reshape(-1))
        if fc.lower is not None and fc.upper is not None:
            lo.append(fc.lower[idx].reshape(-1))
            hi.append(fc.upper[idx].reshape(-1))
        if fc.scale is not None:
            sc.append(np.full(len(idx) * fc.actual.shape[1], fc.scale))
    if not a:
        return {m: None for m in METRIC_NAMES}
    act, pred = np.concatenate(a), np.concatenate(p)
    ok = np.isfinite(act) & np.isfinite(pred)
    if not ok.any():
        return {m: None for m in METRIC_NAMES}
    err = act[ok] - pred[ok]
    out: dict[str, float | None] = {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
    }
    nz = np.abs(act[ok]) > 1e-12
    out["mape"] = float(np.mean(np.abs(err[nz] / act[ok][nz]))) if nz.any() else None
    den = np.abs(act[ok]) + np.abs(pred[ok])
    good = den > 1e-12
    out["smape"] = float(np.mean(2 * np.abs(err[good]) / den[good])) if good.any() else None
    if sc and len(np.concatenate(sc)) == len(act):
        scale = np.concatenate(sc)[ok]
        out["mase"] = float(np.mean(np.abs(err) / scale))
    else:
        out["mase"] = None
    if lo and len(np.concatenate(lo)) == len(act):
        lw, up = np.concatenate(lo)[ok], np.concatenate(hi)[ok]
        out["coverage_90"] = float(np.mean((act[ok] >= lw) & (act[ok] <= up)))
    else:
        out["coverage_90"] = None
    return out


def plan_fold_list(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    folds = plan.get("folds")
    if not isinstance(folds, list) or not folds:
        raise data_error("the split plan has no folds")
    return [dict(f) for f in folds if isinstance(f, dict)]


def backtest_model(
    ctx: NodeContext,
    data: DatasetValue,
    plan: Mapping[str, Any],
    *,
    settings: Mapping[str, JsonValue] | None = None,
    name: str | None = None,
) -> tuple[list[FoldForecast], dict[str, Any], list[str]]:
    """Refit and score a model on every fold: the fold forecasts, info and warnings."""
    spec, base, warns = build_spec(
        ctx.params,
        data,
        name=name or ctx.node_id,
        settings=settings,
        mode="train_only",
        registry=_registry(ctx),
    )
    ds = base.data
    schema = schema_of(base)
    tdim = time_dim(ds, schema, plan_dim(plan))
    target = target_var(ds, schema, _bound_target(ctx.params, spec))
    try:
        season = season_length(ds, schema, tdim, None)
    except MapiError:
        season = 1
    kind = str((plan.get("plan") or {}).get("kind") or "")
    folds = plan_fold_list(plan)
    out: list[FoldForecast] = []
    for i, f in enumerate(folds):
        ctx.check()
        if ctx.progress is not None:
            ctx.progress(i / len(folds), f"fold {i + 1} of {len(folds)}")
        if f.get("test_groups") is not None:
            gdim = str((plan.get("plan") or {}).get("dim") or "geo")
            out.append(_score_groups(ctx, spec, ds, tdim, target, f, gdim, season))
        elif kind in ("blocked_kfold", "purged_kfold"):
            out.append(_score_masked(ctx, spec, ds, tdim, target, f, season))
        else:
            out.append(_score_window(ctx, spec, ds, tdim, target, f, season))
    info = {
        "model": str(spec.model.use),
        "settings": dict(spec.model.settings),
        "target": target,
        "dim": tdim,
    }
    return out, info, warns


def plan_dim(plan: Mapping[str, Any]) -> str | None:
    dim = plan.get("dim")
    if isinstance(dim, str):
        return dim
    inner = plan.get("plan")
    if isinstance(inner, dict):
        if inner.get("kind") == "geo_holdout":
            t = inner.get("time_dim")
            return t if isinstance(t, str) else None
        d = inner.get("dim")
        return d if isinstance(d, str) else None
    return None


def _bound_target(params: Mapping[str, JsonValue], spec: ModelSpec) -> str | None:
    var = params.get("var")
    if isinstance(var, str):
        return var
    bound = spec.data.bind.vars
    return next(iter(bound.values())) if len(bound) == 1 else None


def _horizon_rows(plan: Mapping[str, Any]) -> dict[str, list[int]]:
    folds = plan.get("folds") or []
    hs: list[int] = []
    if folds and isinstance(folds[0], dict):
        hs = [int(h) for h in folds[0].get("horizons") or []]
    return {str(h): [h - 1] for h in hs}


def candidate(
    name: str,
    group: str,
    fcs: Sequence[FoldForecast],
    plan: Mapping[str, Any],
    *,
    kind: str = "model",
    model: str | None = None,
    settings: Mapping[str, JsonValue] | None = None,
) -> MetricsCandidate:
    by_h = {h: fold_metrics(fcs, rows) for h, rows in _horizon_rows(plan).items()}
    return MetricsCandidate(
        name=name,
        group=group,
        kind=kind,
        model=model,
        settings=dict(settings) if settings is not None else None,
        metrics=fold_metrics(fcs),
        by_horizon=by_h,
        by_fold=[fold_metrics([fc]) for fc in fcs],
        n_folds=len(fcs),
        errors=[[clean_number(x) for x in forecast_errors(fc)] for fc in fcs],
    )


def backtest_node(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    plan = split_payload(ctx)
    if plan is None:
        raise data_error("model.backtest needs a split plan")
    fcs, info, warns = backtest_model(ctx, dv, plan)
    wanted = ctx.params.get("metrics")
    cand = candidate(
        ctx.node_id,
        ctx.node_id,
        fcs,
        plan,
        model=str(ctx.params.get("model")),
        settings=info["settings"],
    )
    if isinstance(wanted, list) and wanted:
        keep = {str(m) for m in wanted} | {"coverage_90"}
        cand = cand.model_copy(
            update={
                "metrics": {k: v for k, v in cand.metrics.items() if k in keep},
                "by_horizon": {
                    h: {k: v for k, v in ms.items() if k in keep}
                    for h, ms in cand.by_horizon.items()
                },
            }
        )
    horizons = [int(h) for h in cand.by_horizon]
    payload = MetricsPayload(candidates=[cand], horizons=horizons)
    rows: list[dict[str, JsonValue]] = [
        {"fold": i, **{k: v for k, v in m.items()}} for i, m in enumerate(cand.by_fold)
    ]
    table = TablePayload(
        title="Backtest by fold",
        kind="backtest",
        columns=["fold", *METRIC_NAMES],
        rows=rows,
        meta={"by_horizon": cand.by_horizon},
    )
    result: dict[str, JsonValue] = {
        "metrics": dict(cand.metrics),
        "by_horizon": {h: dict(m) for h, m in cand.by_horizon.items()},
        "n_folds": cand.n_folds,
        "model": info["model"],
    }
    return NodeOutcome(
        result=result,
        outputs={
            "metrics": payload.model_dump(mode="json"),
            "table": table.model_dump(mode="json"),
        },
        warnings_plain=warns,
    )


# ================================================================================ forecast


def forecast_node(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    future = ctx.dataset("data", required=False)
    bundle = mv.bundle
    horizon = int(clean_number(ctx.params.get("horizon")) or 12)
    policy = str(ctx.params.get("future_covariates") or "error")
    summary = ctx.params.get("summary") or ["mean", "hdi_90"]
    spec = bundle.spec
    tdim_canon = next(
        (
            d
            for d, s in (bundle.model_schema.dims.items() if bundle.model_schema else [])
            if s.kind == "time"
        ),
        "time",
    )
    extra: dict[str, Any] = {"horizon": {"dim": tdim_canon, "periods": horizon}}
    if policy != "error":
        extra["future_covariates"] = policy
    reg = _registry(ctx)
    try:
        req = _request(spec, "forecast", **extra)
        tree = score_bundle(
            bundle,
            req,
            data=future.data if future is not None else None,
            registry=reg,
            history=mv.data,
            return_full=True,
        )
    except MapiError:
        raise
    pred = tree["predictions"].to_dataset()
    rep = Representation(tree.attrs.get("representation", "point"))
    out_var, _, _ = _target_names(bundle)
    point = point_estimate(pred[out_var], rep)
    total = float(np.nansum(point.values))
    keep = [str(x) for x in summary] if isinstance(summary, list) else None
    summ = tree["summary"].to_dataset() if "summary" in tree.children else pred
    if keep and "stat" in summ.dims:
        have = [k for k in keep if k in [str(v) for v in summ["stat"].values]]
        if have:
            summ = summ.sel(stat=have)
    fds = summ.copy()
    return NodeOutcome(
        result={"total": total, "horizon": horizon, "model": mv.ref},
        outputs={"forecast": DatasetValue(data=fds)},
    )


# ================================================================================ residuals


@dataclass(slots=True)
class Residuals:
    """In-sample residuals of a fitted model over its fit data (one series)."""

    resid: np.ndarray
    labels: np.ndarray
    target: str
    exog: np.ndarray | None
    exog_names: list[str]


def model_residuals(mv: ModelValue, registry: Registry | None = None) -> Residuals:
    """``actual - fitted`` over the fit data, through the score path (``predict``)."""
    if mv.data is None:
        raise data_error("the model's fit data is not available for residual checks")
    bundle = mv.bundle
    reg = registry or default_registry()
    tree = score_bundle(
        bundle, _request(bundle.spec, "predict"), data=mv.data, registry=reg, return_full=True
    )
    pred = tree["predictions"].to_dataset()
    out_var, _, target = _target_names(bundle)
    rep = Representation(tree.attrs.get("representation", "point"))
    point = point_estimate(pred[out_var], rep)
    schema = schema_of(mv.data)
    tdim = time_dim(mv.data, schema)
    actual = mv.data[target]
    others = [d for d in actual.dims if d != tdim]
    a = np.asarray(actual.sum(others, min_count=1).values if others else actual.values, float)
    p = np.asarray(
        point.sum([d for d in point.dims if d != tdim], min_count=1).values
        if len(point.dims) > 1
        else point.values,
        float,
    )
    n = min(len(a), len(p))
    a, p = a[-n:], p[-n:]
    labels = np.asarray(mv.data[tdim].values)[-n:] if tdim in mv.data.coords else np.arange(n)
    resid = a - p
    ok = np.isfinite(resid)
    # one-step-ahead predictions of state-space models start cold: drop a short burn-in
    burn = min(int(np.argmax(ok)) if ok.any() else 0, n)
    exog_names = [
        str(v)
        for v, s in schema.vars.items()
        if v != target
        and v in mv.data
        and s.role is not Role.TARGET
        and np.issubdtype(mv.data[v].dtype, np.number)
        and tdim in mv.data[v].dims
        and mv.data[v].ndim == 1
    ]
    exog = (
        np.column_stack([np.asarray(mv.data[v].values, float)[-n:] for v in exog_names])
        if exog_names
        else None
    )
    keep = ok.copy()
    keep[:burn] = False
    if exog is not None:
        keep &= np.all(np.isfinite(exog), axis=1)
    return Residuals(
        resid=resid[keep],
        labels=labels[keep],
        target=target,
        exog=exog[keep] if exog is not None else None,
        exog_names=exog_names,
    )


# =================================================================================== grid


def _set_pointer(doc: dict[str, Any], pointer: str, value: Any) -> None:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    if not pointer.startswith("/") or not parts or parts == [""]:
        raise MapiError(
            f"{pointer!r} is not a JSON pointer into the settings", code=ErrorCode.WORKFLOW_INVALID
        )
    cur: Any = doc
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur.setdefault(p, {})
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def grid_points(
    base: Mapping[str, Any], grid: Mapping[str, Sequence[Any]], cap: int
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(settings, point)`` for every combination of the grid (pointers into settings)."""
    pts: list[dict[str, Any]] = [{}]
    for ptr, values in grid.items():
        pts = [{**p, ptr: v} for p in pts for v in values]
        if len(pts) > cap:
            raise MapiError(
                f"the grid has more than {cap} points (max_points)", code=ErrorCode.WORKFLOW_INVALID
            )
    out = []
    for p in pts:
        s = copy.deepcopy(dict(base))
        for ptr, v in p.items():
            _set_pointer(s, ptr, v)
        out.append((s, p))
    return out


def grid_node(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    plan = split_payload(ctx)
    if plan is None:
        raise data_error("search.grid needs a split plan")
    grid = ctx.params.get("grid")
    if not isinstance(grid, dict) or not grid:
        raise MapiError("search.grid needs a 'grid'", code=ErrorCode.WORKFLOW_INVALID)
    metric = str(ctx.params.get("metric") or "rmse")
    direction = str(ctx.params.get("direction") or "min")
    cap = int(clean_number(ctx.params.get("max_points")) or 50)
    base = ctx.params.get("settings") or {}
    points = grid_points(
        base if isinstance(base, dict) else {},
        {k: v for k, v in grid.items() if isinstance(v, list)},
        cap,
    )
    rows: list[dict[str, JsonValue]] = []
    best: tuple[float, dict[str, Any]] | None = None
    failed = 0
    for i, (settings, point) in enumerate(points):
        ctx.check()
        try:
            fcs, _, _ = backtest_model(ctx, dv, plan, settings=settings, name=f"{ctx.node_id}-{i}")
        except MapiError as exc:
            failed += 1
            rows.append(
                {"point": i, **{k: _js(v) for k, v in point.items()}, "error": exc.detail[:200]}
            )
            continue
        ms = fold_metrics(fcs)
        score = ms.get(metric)
        rows.append({"point": i, **{k: _js(v) for k, v in point.items()}, **ms})
        if score is None or not math.isfinite(score):
            continue
        key = score if direction == "min" else -score
        if best is None or key < best[0]:
            best = (key, settings)
    if best is None:
        raise data_error(f"no grid point could be scored on {metric!r}")
    best_settings = best[1]
    best_score = best[0] if direction == "min" else -best[0]
    table = TablePayload(
        title="Grid search",
        kind="grid",
        columns=["point", *grid.keys(), *METRIC_NAMES],
        rows=rows,
        meta={"metric": metric, "direction": direction},
    )
    result: dict[str, JsonValue] = {
        "best_settings": best_settings,
        "best_score": best_score,
        "n_points": len(points),
        "n_failed": failed,
    }
    if ctx.services.get("sandbox"):
        # model versions come out of fit runs: the control plane refits the winner
        return NodeOutcome(result=result, outputs={"table": table.model_dump(mode="json")})
    spec, base_dv, _ = build_spec(
        ctx.params, dv, name=ctx.node_id, settings=best_settings, registry=_registry(ctx)
    )
    bundle = fit_spec(spec, base_dv.data, registry=_registry(ctx), hooks=_hooks(ctx))
    ref = f"local://{ctx.node_id}/{bundle.bundle_id}"
    mv = ModelValue(
        bundle=bundle,
        data=base_dv.data,
        ref=ref,
        model=str(spec.model.use),
        settings=dict(best_settings),
    )
    return NodeOutcome(result=result, outputs={"table": table.model_dump(mode="json"), "best": mv})


def _js(v: Any) -> JsonValue:
    return v if isinstance(v, str | int | float | bool | list | dict) or v is None else str(v)


MODEL_NODES: Final = {
    "model.fit": fit_node,
    "model.backtest": backtest_node,
    "model.forecast": forecast_node,
    "search.grid": grid_node,
}
