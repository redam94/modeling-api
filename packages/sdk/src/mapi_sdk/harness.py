"""The fit/score harness: the platform side of every run (plan §4.2).

:func:`fit_spec` implements ``fit.mode``:

* bind the data, validate it, fit the stateless prefix on all data up to the split stage;
* split; fit the remaining step states and the model on train (the *evaluation* fit);
* evaluate the holdout **through the score path** (context from train) - never a separate
  code path - and compute the spec's metrics;
* ``evaluate_then_refit``: refit states and model on train ∪ holdout so the context tails and
  terminal state come from the full data; the production bundle links the evaluation
  bundle and its metrics. ``train_only`` keeps the train fit as the result.
* derived quantities the spec requests (``outputs.derived``) whose manifest declaration
  says ``cache: fit`` are computed on the production fit by the plugin's ``derive`` hook,
  over the first ``outputs.ui_draws`` draws of the thinning index, with
  ``ScoreContext.to_original`` mapping them to the output space.

:func:`score_bundle` implements scoring: CoordPlan checks, binding, interventions, entry at
``inputs_stage``, context replay, canonical coords, ``model.score``, context removal,
selection, inverse transforms to the output space, aggregation, summaries, thinned draws and
reverse binding. It returns an ``xr.DataTree``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import REPRESENTATION_RULES, OutputSelector, Representation
from mapi_spec.errors import CoordPlanError, ErrorCode, Issue, MapiError, Severity
from mapi_spec.loader import load_model_spec, load_score_request
from mapi_spec.schema import DataSchema, DimPolicy, Role
from mapi_spec.spec import (
    FitMode,
    LastPerGroupSplit,
    ModelSpec,
    ScorePolicy,
    ScoreRequest,
    Split,
)
from mapi_spec.validate import ResolvedSpec, apply_defaults, validate_spec

from mapi_sdk._util import json_safe, normalise_dataset
from mapi_sdk.binding import BindingMap
from mapi_sdk.bundle import Bundle, MetricsReport, assert_no_pickle, new_manifest
from mapi_sdk.context import drop_context
from mapi_sdk.coords import (
    CanonicalPlan,
    CoordPlan,
    canonicalise_coords,
    horizon_frame,
    next_label,
)
from mapi_sdk.errors import SchemaValidationError, StepError
from mapi_sdk.metrics import compute_metrics, elpd
from mapi_sdk.models import PREDICTIONS, FitContext, FitOutput, Fitted, ModelPlugin, ScoreContext
from mapi_sdk.pipeline import RAW, FitTrace, Pipeline, ReplayTrace
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import check_dataset, infer_schema
from mapi_sdk.split import SplitResult, split_dataset
from mapi_sdk.steps import CancelToken, StepState
from mapi_sdk.summarise import (
    aggregate,
    default_summary_names,
    select_draws,
    summarise,
    thin_index,
)
from mapi_sdk.wire import write_zarr_zip

__all__ = [
    "DEFAULT_EVAL_METRICS",
    "FitHooks",
    "data_hash",
    "fit_spec",
    "score_bundle",
]

SCHEMA_ATTR = "mapi_schema"
DEFAULT_EVAL_METRICS = ("rmse", "mae", "mape")
MAX_SMEAR_RESIDUALS = 200


@dataclass(slots=True)
class FitHooks:
    """Optional callbacks for progress, logging and cooperative cancellation.

    ``checkpoint_sink`` is handed to the model's :class:`~mapi_sdk.models.FitContext` so
    engines with segmented sampling can checkpoint (and resume) long fits.
    """

    log: Callable[[str], None] | None = None
    on_progress: Callable[[float, str], None] | None = None
    cancel: CancelToken | None = None
    checkpoint_sink: Any | None = None

    def progress(self, fraction: float, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(fraction, message)

    def window(self, lo: float, hi: float) -> Callable[[float, str], None] | None:
        """A progress callback mapping a sub-task's ``[0, 1]`` onto ``[lo, hi]``."""
        if self.on_progress is None:
            return None
        report = self.on_progress

        def scaled(fraction: float, message: str) -> None:
            report(lo + (hi - lo) * min(max(fraction, 0.0), 1.0), message)

        return scaled

    def check(self) -> None:
        if self.cancel is not None:
            self.cancel.check()


def data_hash(ds: xr.Dataset) -> str:
    """``sha256:`` of the canonical zarr-zip bytes of ``ds`` (deterministic)."""
    import hashlib

    raw = write_zarr_zip(ds)
    assert raw is not None
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ============================================================================================ fit


def _raw_schema(spec: ModelSpec, data: xr.Dataset, given: DataSchema | None) -> DataSchema:
    if given is not None:
        return given
    stored = data.attrs.get(SCHEMA_ATTR)
    if isinstance(stored, str):
        return DataSchema.model_validate_json(stored)
    if isinstance(stored, Mapping):
        return DataSchema.model_validate(dict(stored))
    return infer_schema(data)


def _with_targets(
    schema: DataSchema, resolved_targets: Sequence[str], binding: BindingMap
) -> DataSchema:
    """Mark the dataset variables bound to the model's target variables as targets."""
    names = {binding.to_dataset(t) for t in resolved_targets}
    if not names - set(schema.targets):
        return schema
    data = schema.model_dump()
    for n in names:
        if n in data["vars"]:
            data["vars"][n]["role"] = Role.TARGET
    return DataSchema.model_validate(data)


def _resolve(
    spec: ModelSpec, registry: Registry, data: xr.Dataset, raw_schema: DataSchema | None
) -> tuple[ResolvedSpec, DataSchema, list[Issue]]:
    catalog = registry.catalog()
    schema = _raw_schema(spec, data, raw_schema)
    result = validate_spec(spec, catalog, schema)
    # the model's template decides the target roles whatever schema was given (validation
    # already judged target dependence by them); the stored raw schema says so too
    if result.resolved is not None:
        model_schema = result.resolved.model.input_schema
        if model_schema is not None and model_schema.targets:
            fixed = _with_targets(schema, model_schema.targets, BindingMap(spec.data.bind))
            if fixed != schema:
                schema = fixed
                result = validate_spec(spec, catalog, schema)
    resolved = result.raise_for_errors()
    return resolved, schema, result.issues


def _model_settings(bundle_manifest_schema: Mapping[str, Any], spec: ModelSpec) -> dict[str, Any]:
    return apply_defaults(bundle_manifest_schema, spec.model.plugin_settings)


@dataclass(slots=True)
class _FitEnv:
    spec: ModelSpec
    resolved: ResolvedSpec
    raw_schema: DataSchema
    registry: Registry
    pipeline: Pipeline
    plugin: ModelPlugin
    settings: dict[str, Any]
    side_tables: dict[str, xr.Dataset]
    seed: int
    hooks: FitHooks
    data_hash: str | None
    validation_issues: list[Issue] = field(default_factory=list)


def fit_spec(
    spec: ModelSpec | str,
    data: xr.Dataset,
    side_tables: Mapping[str, xr.Dataset] | None = None,
    registry: Registry | None = None,
    *,
    raw_schema: DataSchema | None = None,
    hooks: FitHooks | None = None,
    keep_evaluation_bundle: bool = True,
    hash_data: bool = True,
) -> Bundle:
    """Fit ``spec`` on ``data`` and return the production :class:`~mapi_sdk.bundle.Bundle`.

    ``raw_schema`` describes ``data`` under its dataset names; when omitted it is read from
    ``data.attrs["mapi_schema"]`` or inferred (variables bound to the model's targets are
    marked as targets). ``side_tables`` maps ``data.side_tables`` names to datasets.
    """
    spec = load_model_spec(spec) if isinstance(spec, str) else spec
    registry = registry or default_registry()
    hooks = hooks or FitHooks()
    data = normalise_dataset(data)
    resolved, schema, issues = _resolve(spec, registry, data, raw_schema)
    check_dataset(schema, data)
    hooks.progress(0.05, "validated the spec and the data")

    tables = {k: normalise_dataset(v) for k, v in (side_tables or {}).items()}
    needed = sorted({t for s in resolved.steps for t in s.side_tables})
    missing = [t for t in needed if t not in tables]
    if missing:
        raise MapiError(
            f"side table {missing[0]!r} is used by the pipeline but was not provided",
            code=ErrorCode.SIDE_TABLE_COVERAGE,
            pointer=f"/data/side_tables/{missing[0]}",
        )
    plugin_cls = registry.model(resolved.model.resolved)
    plugin = plugin_cls()
    manifest = plugin_cls.manifest()
    env = _FitEnv(
        spec=spec,
        resolved=resolved,
        raw_schema=schema,
        registry=registry,
        pipeline=Pipeline.from_spec(spec, registry, resolved),
        plugin=plugin,
        settings=_model_settings(manifest.settings_schema, spec),
        side_tables=tables,
        seed=spec.model.fit.seed or 0,
        hooks=hooks,
        data_hash=data_hash(data) if hash_data else None,
        validation_issues=issues,
    )
    raw = BindingMap(resolved.binding).apply(data)

    split = spec.data.split
    split_stage = resolved.split_stage or RAW
    if split is None:
        bundle = _fit_from(env, None, raw, start=RAW, role="production", progress=(0.05, 0.95))
        bundle.metrics = MetricsReport(
            skipped={
                m: "no data.split is declared"
                for m in spec.evaluate.metrics
                if not m.startswith("elpd_")
            }
        )
        _add_elpd(env, bundle)
        hooks.progress(1.0, "fit complete")
        return bundle

    prefix = env.pipeline.fit(
        raw,
        start=RAW,
        end=split_stage,
        side_tables=tables,
        seed=env.seed,
        cancel=hooks.cancel,
        log=hooks.log,
        keep_stages=False,
    )
    hooks.check()
    sr = split_dataset(prefix.data, split, schema=resolved.stages.get(split_stage), seed=env.seed)
    _check_recursion(resolved, split, sr)
    hooks.progress(0.15, "split the data")
    evaluation = _fit_from(
        env, prefix, sr.train, start=split_stage, role="evaluation", progress=(0.15, 0.45)
    )
    hooks.progress(0.45, "fitted on the training data")
    metrics = _evaluate(env, evaluation, sr, split_stage)
    hooks.progress(0.55, "evaluated the holdout through the score path")

    if spec.model.fit.mode is FitMode.EVALUATE_THEN_REFIT:
        production = _fit_from(
            env, prefix, prefix.data, start=split_stage, role="production", progress=(0.55, 0.95)
        )
        metrics.evaluation_bundle = evaluation.bundle_id
        production.manifest.lineage = {
            "evaluation_bundle": evaluation.bundle_id,
            "fit_mode": spec.model.fit.mode.value,
        }
        if keep_evaluation_bundle:
            production.evaluation = evaluation
    else:
        production = evaluation
        production.manifest.role = "production"
        production.manifest.lineage = {"fit_mode": spec.model.fit.mode.value}
    production.metrics = metrics
    _add_elpd(env, production)
    hooks.progress(1.0, "fit complete")
    return production


def _check_recursion(rs: ResolvedSpec, split: Split, sr: SplitResult) -> None:
    """Reject target lags shorter than the holdout once its length is known.

    Static validation can only judge ``time_holdout: last``; ``fraction``/``after`` and
    ``as_of`` (plus its ``gap``) holdouts are measured here, before the evaluation scores
    the holdout with its own observed targets feeding the lags.
    """
    fdim = split.forecast_dim
    if fdim is None or sr.is_masked or "recursion" in rs.model.capabilities.owns:
        return
    if isinstance(split, LastPerGroupSplit):
        periods = split.last
    elif fdim in sr.holdout.dims:
        periods = int(sr.holdout.sizes[fdim])  # an as_of gap is part of the holdout
    else:
        return
    for idx, s in enumerate(rs.steps):
        ctx = s.context
        if not s.depends_on_target or ctx is None or ctx.along != fdim:
            continue
        min_lag = ctx.min_lag if ctx.min_lag is not None else 1
        if periods > min_lag:
            raise MapiError(
                f"step {s.id!r} lags the target by {min_lag} along {fdim!r} but the holdout "
                f"spans {periods} periods; recursive forecasting is not supported in v1",
                code=ErrorCode.RECURSIVE_UNSUPPORTED,
                pointer=f"/pipeline/{idx}",
            )


def _add_elpd(env: _FitEnv, bundle: Bundle) -> None:
    caps = bundle.model.manifest.capabilities
    values, skipped, diagnostics = elpd(
        bundle.model.groups,
        env.spec.evaluate.metrics,
        loo_valid=caps.loo_valid,
        reason=caps.loo_invalid_reason,
    )
    bundle.metrics.values.update(values)
    bundle.metrics.skipped.update(skipped)
    bundle.diagnostics.extend(diagnostics)


def _fit_from(
    env: _FitEnv,
    prefix: FitTrace | None,
    ds: xr.Dataset,
    *,
    start: str,
    role: Literal["production", "evaluation"],
    progress: tuple[float, float] = (0.0, 1.0),
) -> Bundle:
    hooks = env.hooks
    rest = env.pipeline.fit(
        ds,
        start=start,
        side_tables=env.side_tables,
        seed=env.seed,
        cancel=hooks.cancel,
        log=hooks.log,
    )
    states = {**(prefix.states if prefix else {}), **rest.states}
    coords = {**(prefix.coords if prefix else {}), **rest.coords}
    model_ds = rest.data
    rs = env.resolved
    model_schema = rs.model.input_schema
    if model_schema is not None:
        try:
            check_dataset(model_schema, model_ds, check_values=False, check_coords=False)
        except SchemaValidationError as exc:
            raise StepError(
                "the pipeline output does not match the model's input schema: " + exc.detail
            ) from None
    hooks.check()
    manifest = type(env.plugin).manifest()
    s = env.plugin.parse_settings(env.settings)
    model_coords = coords.get(rs.model_stage, xr.Dataset())
    fctx = FitContext(
        seed=env.seed,
        schema=model_schema,
        policies=rs.model.effective_policies,
        coords={str(d): np.asarray(model_coords[d].values) for d in model_coords.coords},
        log=hooks.log,
        on_progress=hooks.window(*progress),
        cancel=hooks.cancel,
        checkpoint_sink=hooks.checkpoint_sink,
        stage_schema=rs.stages.get(rs.model_stage),
    )
    try:
        out = env.plugin.fit(model_ds, s, env.spec.model.fit, fctx)
    except MapiError:
        raise
    except Exception as exc:
        raise MapiError(
            f"model {manifest.id!r} failed during fit",
            code=ErrorCode.USER_CODE_ERROR,
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        ) from exc
    if not isinstance(out, FitOutput):
        raise MapiError(
            f"model {manifest.id!r} fit() must return a FitOutput", code=ErrorCode.USER_CODE_ERROR
        )
    postprocess = getattr(env.plugin, "postprocess", None)
    if callable(postprocess):
        out = postprocess(out, model_ds, s)
    assert_no_pickle(out.native)
    fitted = Fitted(
        manifest=manifest,
        settings=env.settings,
        fit_config=env.spec.model.fit,
        groups=dict(out.groups),
        learned=dict(json_safe(out.learned)),
        native=dict(out.native),
        terminal_state=out.terminal_state,
        derived=dict(out.derived),
        schema=model_schema,
        coords=dict(fctx.coords),
    )
    diagnostics = list(out.diagnostics)
    diag_hook = getattr(env.plugin, "diagnostics", None)
    if callable(diag_hook):
        diagnostics += diag_hook(fitted, model_ds)
    thin = None
    post = fitted.groups.get("posterior")
    if post is not None and {"chain", "draw"} <= set(post.dims):
        thin = thin_index(post.sizes["chain"], post.sizes["draw"], seed=env.seed)
    if role == "production" or env.spec.model.fit.mode is not FitMode.EVALUATE_THEN_REFIT:
        fitted.derived.update(_fit_derived(env, fitted, model_ds, states, thin))
    pinned = {
        t: env.side_tables[t]
        for t in sorted({t for st in rs.steps for t in st.side_tables})
        if rs.side_tables.get(t) is ScorePolicy.PINNED and t in env.side_tables
    }
    bundle = Bundle(
        spec=env.spec,
        resolved=rs,
        raw_schema=env.raw_schema,
        states=states,
        fit_coords=coords,
        model=fitted,
        manifest=new_manifest(
            spec=env.spec,
            resolved=rs,
            model=fitted,
            states=states,
            role=role,
            data_hash=env.data_hash,
            fit_info={
                "mode": env.spec.model.fit.mode.value,
                "seed": env.seed,
                "engine": env.spec.model.fit.engine,
                "train_stage": start,
            },
        ),
        thin=thin,
        side_tables=pinned,
        diagnostics=diagnostics,
    )
    if (
        manifest.capabilities.representation is Representation.POINT
        and env.settings.get("retransform") == "smearing"
    ):
        bundle.residuals = _residuals(env, bundle, model_ds)
    return bundle


def _to_original(
    pipeline: Pipeline, states: Mapping[str, StepState], tables: Mapping[str, xr.Dataset]
) -> Callable[[xr.DataArray, str], xr.DataArray]:
    """``ScoreContext.to_original`` for derived quantities: invert ``da`` (in the units of
    input variable ``src``) back to the last reshape stage.

    Inversion starts at the latest stage where ``src`` has no dims beyond those of ``da``:
    a spend level ``(geo, channel, time)`` derived from a lag tensor ``spend(..., lag)``
    starts before the lag step (lag 0 is the period's own spend).
    """
    stages = pipeline.stage_names
    floor = pipeline.index(pipeline.last_reshape_stage)

    def convert(da: xr.DataArray, src: str) -> xr.DataArray:
        dims = {str(d) for d in da.dims}
        start: str | None = None
        for stage in reversed(stages[floor:]):
            schema = pipeline.schema_at(stage)
            if schema is not None and src in schema.vars and set(schema.vars[src].dims) <= dims:
                start = stage
                break
        if start is None:
            out = da.copy()
            out.attrs = {**da.attrs, "output_space": pipeline.final_stage}
            return out
        key = "__derived__"
        inv = pipeline.inverse_outputs(
            xr.Dataset({key: da}),
            states,
            {key: src},
            from_stage=start,
            representation=Representation.DRAWS,
            side_tables=tables,
        )
        return inv.data[key].rename(da.name)

    return convert


def _fit_derived(
    env: _FitEnv,
    fitted: Fitted,
    model_ds: xr.Dataset,
    states: Mapping[str, StepState],
    thin: np.ndarray | None,
) -> dict[str, xr.Dataset]:
    """Derived quantities the spec requests (``outputs.derived``) that the model caches at
    fit, computed on the model-stage fit data over the first ``outputs.ui_draws`` draws of
    the bundle thinning index (plan §4.2)."""
    decls = {d.name: d for d in fitted.manifest.derived}
    wanted = [
        n
        for n in env.spec.outputs.derived
        if n in decls and decls[n].cache == "fit" and n not in fitted.derived
    ]
    derive = getattr(env.plugin, "derive", None)
    if not wanted or not callable(derive):
        return {}
    rs = env.resolved
    ctx = ScoreContext(
        mode="predict",
        kind="expected",
        seed=env.seed,
        schema=fitted.schema,
        policies=rs.model.effective_policies,
        draw_index=None if thin is None else thin[: env.spec.outputs.ui_draws],
        log=env.hooks.log,
        cancel=env.hooks.cancel,
        to_original=_to_original(env.pipeline, states, env.side_tables),
    )
    model_id = fitted.manifest.id
    out: dict[str, xr.Dataset] = {}
    for j, name in enumerate(wanted):
        env.hooks.check()
        try:
            result = derive(fitted, name, model_ds, ctx)
        except MapiError:
            raise
        except NotImplementedError:
            raise MapiError(
                f"model {model_id!r} declares derived quantity {name!r} but cannot compute it",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer=f"/outputs/derived/{j}",
            ) from None
        except Exception as exc:
            raise MapiError(
                f"model {model_id!r} failed while deriving {name!r}",
                code=ErrorCode.USER_CODE_ERROR,
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc
        if not isinstance(result, xr.Dataset):
            raise MapiError(
                f"model {model_id!r} derive({name!r}) must return an xarray Dataset",
                code=ErrorCode.USER_CODE_ERROR,
            )
        out[name] = result
    return out


def _var_sources(fitted: Fitted, schema: DataSchema | None) -> dict[str, str]:
    """Output variable -> input variable whose units/transforms it inherits.

    Outputs without ``units_of`` inherit the single target's transforms unless they declare
    their own ``units`` (a probability or a count of states is never on the target scale).
    """
    out: dict[str, str] = {}
    targets = schema.targets if schema is not None else []
    for name, decl in fitted.manifest.outputs.items():
        if decl.units_of is not None:
            out[name] = decl.units_of
        elif decl.units is None and len(targets) == 1:
            out[name] = targets[0]
    return out


def _residuals(env: _FitEnv, bundle: Bundle, model_ds: xr.Dataset) -> dict[str, xr.DataArray]:
    """Model-space in-sample residuals for Duan's smearing retransformation.

    Only *target-shaped* outputs (same dims as their source variable) give residuals, and
    the first such output per source wins: a decomposition such as
    ``contributions(component, ...)`` also inherits the target's units but its cells are
    not predictions of the target.
    """
    rs = env.resolved
    req = _internal_request(env.spec, "predict", rs.model_stage, OutputSelector(units="model"))
    canon, plan = canonicalise_coords(
        model_ds,
        bundle.fit_coords[rs.model_stage],
        rs.model.input_schema,
        pad_extend=bool(getattr(env.plugin, "pad_extend", True)),
    )
    ctx = ScoreContext(mode="predict", kind="expected", seed=env.seed, schema=rs.model.input_schema)
    tree = env.plugin.score(bundle.model, canon, req, ctx)
    pred = plan.restore(_predictions(tree))
    out: dict[str, xr.DataArray] = {}
    for var, src in _var_sources(bundle.model, rs.model.input_schema).items():
        if var not in pred or src not in model_ds or src in out:
            continue
        if set(pred[var].dims) != set(model_ds[src].dims):
            continue
        r = (model_ds[src] - pred[var]).values.reshape(-1)
        r = r[np.isfinite(r)]
        if r.size > MAX_SMEAR_RESIDUALS:
            idx = np.linspace(0, r.size - 1, MAX_SMEAR_RESIDUALS).round().astype(int)
            r = np.sort(r)[idx]
        out[src] = xr.DataArray(r, dims=[f"residual__{src}"])
    return out


def _internal_request(
    spec: ModelSpec,
    mode: str,
    inputs_stage: str,
    output: OutputSelector,
    seed: int | None = None,
) -> ScoreRequest:
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"model://{spec.metadata.project}/{spec.metadata.name}@latest",
            "mode": mode,
            "inputs_stage": inputs_stage,
            "data": "dataset://mapi/holdout@latest",
            "output": output.model_dump(mode="json", exclude_defaults=True),
            "seed": seed,
        }
    )


def _evaluate(env: _FitEnv, evaluation: Bundle, sr: SplitResult, split_stage: str) -> MetricsReport:
    caps = evaluation.model.manifest.capabilities
    rules = REPRESENTATION_RULES[caps.representation]
    mode = "evaluate" if "evaluate" in caps.score_modes else "predict"
    output = OutputSelector(
        kind="predictive" if rules.predictive else "expected", names="canonical", summary=None
    )
    req = _internal_request(env.spec, mode, split_stage, output, seed=env.seed)
    names = list(env.spec.evaluate.metrics) or list(DEFAULT_EVAL_METRICS)
    payload, observed = sr.holdout, None
    mask = sr.holdout_mask
    split = env.spec.data.split
    if sr.lead and split is not None and split.forecast_dim in sr.holdout.dims:
        fdim = str(split.forecast_dim)
        mask = xr.DataArray(
            np.arange(sr.holdout.sizes[fdim]) >= sr.lead,
            dims=[fdim],
            coords={fdim: sr.holdout[fdim].values} if fdim in sr.holdout.coords else None,
        )
    if sr.is_masked and any(s.depends_on_target for s in env.resolved.steps):
        # cell-level split: held-out targets must not feed target-dependent features
        # (lags of y) - score the target-masked data, compare against the full data
        payload, observed = sr.train, sr.holdout
    tree = score_bundle(
        evaluation,
        req,
        payload,
        side_tables=env.side_tables,
        registry=env.registry,
        metrics=[m for m in names if not m.startswith("elpd_")],
        metric_mask=mask,
        observed=observed,
    )
    report = MetricsReport.model_validate_json(tree.attrs["metrics"])
    report.split = json_safe(
        env.spec.data.split.model_dump(mode="json") if env.spec.data.split else None
    )
    return report


# ========================================================================================== score


def _predictions(tree: xr.DataTree) -> xr.Dataset:
    if PREDICTIONS not in tree:
        raise MapiError(
            f"the model's score() must return a DataTree with a {PREDICTIONS!r} group",
            code=ErrorCode.USER_CODE_ERROR,
        )
    node = tree[PREDICTIONS]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset(inherit=False)


def _apply_interventions(ds: xr.Dataset, req: ScoreRequest, binding: BindingMap) -> xr.Dataset:
    out = ds
    for k, iv in enumerate(req.interventions):
        var = binding.to_canonical(iv.var)
        if var not in out:
            raise MapiError(
                f"intervention variable {var!r} is not in the score data",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/interventions/{k}/var",
            )
        da = out[var]
        mask = xr.ones_like(da, dtype=bool)
        for raw_dim, sel in iv.sel.items():
            dim = binding.to_canonical(raw_dim)
            if dim not in da.dims:
                raise MapiError(
                    f"variable {var!r} has no dim {dim!r}",
                    code=ErrorCode.COORD_NOT_ALLOWED,
                    pointer=f"/interventions/{k}/sel/{raw_dim}",
                )
            labels = np.asarray(sel if isinstance(sel, list) else [sel])
            if out[dim].dtype.kind == "M":
                labels = labels.astype("datetime64[ns]")
            mask = mask & out[dim].isin(labels)
        if iv.multiply is not None:
            new = da * iv.multiply
        elif iv.add is not None:
            new = da + iv.add
        else:
            assert iv.set_ is not None
            new = xr.full_like(da, iv.set_, dtype=float)
        out = out.assign({var: new.where(mask, da)})
    return out


def _horizon_payload(bundle: Bundle, req: ScoreRequest) -> xr.Dataset:
    assert req.horizon is not None
    rs = bundle.resolved
    hdim = req.horizon.dim
    model_coords = bundle.fit_coords.get(rs.model_stage)
    if model_coords is None or hdim not in model_coords.coords:
        raise MapiError(
            f"cannot build a horizon along {hdim!r}: no fitted coordinates",
            code=ErrorCode.COORD_NOT_ALLOWED,
            pointer="/horizon/dim",
        )
    model_schema = rs.model.input_schema or rs.stages.get(rs.model_stage)
    mfreq = model_schema.dims[hdim].freq if model_schema and hdim in model_schema.dims else None
    fit_end = np.asarray(model_coords[hdim].values).max()
    step = 1
    vals = np.asarray(model_coords[hdim].values)
    if vals.dtype.kind in "iu" and vals.size > 1:
        step = int(np.diff(np.sort(vals)).min())
    end = next_label(fit_end, freq=mfreq, step=step, k=req.horizon.periods)
    stage = req.inputs_stage
    stage_coords = bundle.fit_coords.get(stage)
    if stage_coords is None or hdim not in stage_coords.coords:
        raise MapiError(
            f"stage {stage!r} has no {hdim!r} coordinate to extend",
            code=ErrorCode.COORD_NOT_ALLOWED,
            pointer="/inputs_stage",
        )
    sschema = rs.stages.get(stage)
    sfreq = sschema.dims[hdim].freq if sschema and hdim in sschema.dims else None
    return horizon_frame(stage_coords, hdim, until=end, freq=sfreq)


def _order_payload(ds: xr.Dataset, schema: DataSchema | None) -> xr.Dataset:
    """Sort a score payload along its ordered dims before any step runs.

    Look-back steps (lags, adstock, forward fill) shift by *position*, so a payload whose
    time labels arrive out of order would silently get wrong features. Dims that are
    ``time``, ``ordered`` or ``extend`` at the entry stage are sorted; duplicate labels
    there are rejected (``COORD_NOT_ALLOWED``).
    """
    if schema is None:
        return ds
    for d in list(ds.dims):
        dim = str(d)
        spec = schema.dims.get(dim)
        if spec is None or dim not in ds.coords:
            continue
        if not (spec.kind == "time" or spec.ordered or spec.policy is DimPolicy.EXTEND):
            continue
        values = np.asarray(ds[dim].values)
        if values.dtype.kind not in "iufM" or values.size < 2:
            continue
        index = pd.Index(values)
        if index.has_duplicates:
            raise CoordPlanError(
                f"score coords of {dim!r} repeat labels; an ordered dim needs unique labels",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
        if not index.is_monotonic_increasing:
            ds = ds.sortby(dim)
    return ds


@dataclass(slots=True)
class _Scored:
    pred: xr.Dataset
    observed: xr.Dataset
    extra: dict[str, xr.Dataset]
    issues: list[Issue]


def score_bundle(
    bundle: Bundle,
    request: ScoreRequest | str,
    data: xr.Dataset | None = None,
    *,
    scenario: xr.Dataset | None = None,
    side_tables: Mapping[str, xr.Dataset] | None = None,
    registry: Registry | None = None,
    max_draws: int | None = None,
    return_full: bool = False,
    metrics: Sequence[str] | None = None,
    metric_mask: xr.DataArray | None = None,
    hooks: FitHooks | None = None,
    observed: xr.Dataset | None = None,
) -> xr.DataTree:
    """Score a fitted bundle; returns a ``DataTree``.

    ``data`` is the request's ``data`` payload and ``scenario`` its ``scenario`` payload.
    Outside counterfactuals a scenario simply *is* the payload. A counterfactual scores
    the scenario (or the data, when no scenario is given) with the interventions applied
    against the **baseline** ``data``; a scenario without baseline data is rejected.

    Groups: ``summary`` (stats along a ``stat`` dim), ``draws`` (when requested; joint,
    from the bundle thinning index), ``predictions`` (full representation, when
    ``return_full``), and for counterfactuals ``baseline/...`` and ``delta/...``. Root
    attrs carry the representation, output kind, units/output space, retransformation
    labels, non-error issues and - in ``evaluate`` mode - the metrics. ``observed``
    (evaluation of cell-level splits) gives the observed targets for metrics when ``data``
    has them masked.
    """
    req = load_score_request(request) if isinstance(request, str) else request
    registry = registry or default_registry()
    hooks = hooks or FitHooks()
    rs = bundle.resolved
    caps = bundle.model.manifest.capabilities
    rep = caps.representation
    binding = bundle.binding
    plugin_cls = registry.model(f"{bundle.model.manifest.id}@{bundle.model.manifest.version}")
    plugin = plugin_cls()
    pipeline = bundle.pipeline(registry)
    pipeline.index(req.inputs_stage)

    counterfactual = req.mode == "counterfactual"
    if counterfactual and req.scenario is not None and scenario is None:
        raise MapiError(
            "the counterfactual names a scenario dataset but none was supplied",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/scenario",
        )
    if counterfactual and scenario is not None and data is None:
        raise MapiError(
            "a counterfactual with a scenario needs the baseline in 'data'",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/data",
        )
    if not counterfactual and scenario is not None:
        data, scenario = scenario, None  # the scenario is the payload
    if data is None:
        if req.horizon is None:
            raise MapiError(
                "the score request has no data; give data/scenario or a forecast horizon",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        data = _horizon_payload(bundle, req)
    entry_schema = rs.stages.get(req.inputs_stage)
    data = _order_payload(binding.apply(normalise_dataset(data), missing="ignore"), entry_schema)

    plan = CoordPlan(req, rs, bundle.fit_coords, max_draws=max_draws)
    issues = plan.check(data)
    if scenario is not None:
        scenario = binding.apply(normalise_dataset(scenario), missing="ignore")
        scenario = _order_payload(scenario, entry_schema)
        issues += [i for i in plan.check(scenario) if i not in issues]
    # pinned tables always replay the version stored at fit; callers only supply the others
    tables = {
        **{
            k: normalise_dataset(v)
            for k, v in (side_tables or {}).items()
            if rs.side_tables.get(k) is not ScorePolicy.PINNED
        },
        **bundle.side_tables,
    }
    needed = sorted({t for s in rs.steps_after(req.inputs_stage) for t in s.side_tables})
    for t in needed:
        if t not in tables:
            raise MapiError(
                f"side table {t!r} is needed from stage {req.inputs_stage!r} but was not provided",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
                pointer=f"/side_tables/{t}",
            )
    seed = req.seed if req.seed is not None else (bundle.model.fit_config.seed or 0)
    targets = rs.model.input_schema.targets if rs.model.input_schema is not None else []

    def prepare(ds: xr.Dataset) -> tuple[ReplayTrace, xr.Dataset, CanonicalPlan]:
        hooks.check()
        trace = pipeline.transform(
            ds,
            bundle.states,
            start=req.inputs_stage,
            side_tables=tables,
            seed=seed,
            cancel=hooks.cancel,
            log=hooks.log,
        )
        canon, plan = canonicalise_coords(
            trace.data,
            bundle.fit_coords[rs.model_stage],
            rs.model.input_schema,
            pad_extend=bool(getattr(plugin, "pad_extend", True)),
            marks=trace.marks,
        )
        return trace, canon, plan

    def observe(trace: ReplayTrace, plan: CanonicalPlan) -> xr.Dataset:
        model_ds = trace.data
        return plan.restore(
            drop_context(model_ds[[t for t in targets if t in model_ds]], trace.marks)
        )

    def run(ds: xr.Dataset) -> _Scored:
        trace, canon, plan = prepare(ds)
        ctx = ScoreContext(
            mode=req.mode,
            kind=req.output.kind,
            seed=seed,
            schema=rs.model.input_schema,
            policies=rs.model.effective_policies,
            horizon=req.horizon,
            draw_index=bundle.thin,
            log=hooks.log,
            cancel=hooks.cancel,
        )
        try:
            tree = plugin.score(bundle.model, canon, req, ctx)
        except MapiError:
            raise
        except Exception as exc:
            raise MapiError(
                f"model {bundle.model.manifest.id!r} failed during score",
                code=ErrorCode.USER_CODE_ERROR,
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc
        pred = plan.restore(drop_context(_predictions(tree), trace.marks))
        observed = observe(trace, plan)
        extra = {
            name: child.to_dataset(inherit=False)
            for name, child in tree.children.items()
            if name != PREDICTIONS
        }
        return _Scored(pred, observed, extra, trace.issues)

    scored = data if scenario is None else scenario
    if req.interventions:
        scored = _apply_interventions(scored, req, binding)
    main = run(scored)
    issues += main.issues
    if observed is not None:
        # cell-level evaluation: features come from the target-masked payload, the
        # observed targets for metrics from the full one
        obs_in = binding.apply(normalise_dataset(observed), missing="ignore")
        obs_trace, _, obs_plan = prepare(_order_payload(obs_in, entry_schema))
        main.observed = observe(obs_trace, obs_plan)
    baseline = run(data) if counterfactual else None

    def finish(pred: xr.Dataset) -> tuple[xr.Dataset, dict[str, str], dict[str, str]]:
        if req.select:
            sel = {binding.to_canonical(k): v for k, v in req.select.items()}
            sel = {k: v for k, v in sel.items() if k in pred.dims}
            pred = pred.sel({k: _labels(pred, k, v) for k, v in sel.items()})
        if req.output.vars:
            keep = [v for v in req.output.vars if v in pred]
            pred = pred[keep]
        space: dict[str, str] = {}
        retrans: dict[str, str] = {}
        if req.output.units == "original" and "inverse_units" not in caps.owns:
            inv = pipeline.inverse_outputs(
                pred,
                bundle.states,
                _var_sources(bundle.model, rs.model.input_schema),
                representation=rep,
                retransform="smearing"
                if bundle.model.settings.get("retransform") == "smearing"
                else "median",
                residuals=bundle.residuals,
                side_tables=tables,
            )
            pred, space, retrans = inv.data, inv.space, inv.retransform
        elif "inverse_units" in caps.owns:
            space = {str(v): "model-owned" for v in pred.data_vars}
        else:
            space = {str(v): rs.model_stage for v in pred.data_vars}
        return pred, space, retrans

    pred, space, retrans = finish(main.pred)
    pred, flagged = aggregate(pred, rep, _canonical_agg(req.output, binding))
    groups: dict[str, xr.Dataset] = {}
    names = (
        list(req.output.summary)
        if req.output.summary is not None
        else default_summary_names(pred, rep)
    )
    groups["summary"] = summarise(pred, rep, names)
    if req.output.draws > 0:
        if bundle.thin is None:
            raise MapiError(
                "draws were requested but this model stores no draws",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/output/draws",
            )
        groups["draws"] = select_draws(pred, bundle.thin, req.output.draws)
    if return_full:
        groups["predictions"] = pred
    for name, extra in main.extra.items():
        groups[name] = extra
    if baseline is not None:
        base, _, _ = finish(baseline.pred)
        base, _ = aggregate(base, rep, _canonical_agg(req.output, binding))
        groups["baseline/summary"] = summarise(base, rep, names)
        if rep in (Representation.DRAWS, Representation.POINT):
            delta = pred - base
            for v in delta.data_vars:
                delta[v].attrs = {**pred[v].attrs, "delta": "scenario - baseline"}
            groups["delta/summary"] = summarise(delta, rep, names)
            if req.output.draws > 0 and bundle.thin is not None:
                groups["delta/draws"] = select_draws(delta, bundle.thin, req.output.draws)
            if return_full:
                groups["delta/predictions"] = delta
        else:
            issues.append(
                Issue(
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    severity=Severity.WARNING,
                    message=f"differences of {rep.value!r} outputs are not valid; only the "
                    "scenario and baseline summaries are returned",
                )
            )
        if return_full:
            groups["baseline/predictions"] = base

    attrs: dict[str, Any] = {
        "bundle_id": bundle.bundle_id,
        "representation": rep.value,
        "mode": req.mode,
        "kind": req.output.kind,
        "units": req.output.units,
        "names": req.output.names,
        "output_space": json.dumps(space, sort_keys=True),
        "retransform": json.dumps(retrans, sort_keys=True),
        "aggregation_flagged": bool(flagged),
        "issues": json.dumps(
            [i.model_dump(mode="json") for i in issues if i.severity is not Severity.ERROR]
        ),
    }
    if req.mode == "evaluate" or metrics is not None:
        report = _score_metrics(bundle, pipeline, main, rep, metrics, metric_mask, tables)
        attrs["metrics"] = report.model_dump_json()
        if return_full:
            groups["observed"] = _observed_output(bundle, pipeline, main, tables)
    if req.output.names == "dataset":
        groups = {k: binding.reverse(v) for k, v in groups.items()}
    nodes: dict[str, xr.Dataset] = {"/": xr.Dataset(attrs=attrs)}
    nodes.update({f"/{k}": v for k, v in groups.items()})
    return xr.DataTree.from_dict(nodes)


def _labels(ds: xr.Dataset, dim: str, values: Sequence[Any]) -> np.ndarray:
    arr = np.asarray(values)
    if ds[dim].dtype.kind == "M":
        arr = arr.astype("datetime64[ns]")
    return arr


def _canonical_agg(output: OutputSelector, binding: BindingMap) -> Any:
    if output.aggregate is None:
        return None
    return output.aggregate.model_copy(
        update={"over": [binding.to_canonical(d) for d in output.aggregate.over]}
    )


def _observed_output(
    bundle: Bundle, pipeline: Pipeline, scored: _Scored, tables: Mapping[str, xr.Dataset]
) -> xr.Dataset:
    """Observed targets mapped to the output space exactly like the predictions."""
    obs = scored.observed
    if not obs.data_vars:
        return obs
    caps = bundle.model.manifest.capabilities
    if "inverse_units" in caps.owns:
        return obs
    inv = pipeline.inverse_outputs(
        obs,
        bundle.states,
        {str(t): str(t) for t in obs.data_vars},
        representation=Representation.DRAWS,
        side_tables=tables,
    )
    return inv.data


def _score_metrics(
    bundle: Bundle,
    pipeline: Pipeline,
    scored: _Scored,
    rep: Representation,
    metrics: Sequence[str] | None,
    mask: xr.DataArray | None,
    tables: Mapping[str, xr.Dataset],
) -> MetricsReport:
    names = list(metrics) if metrics is not None else list(DEFAULT_EVAL_METRICS)
    observed = _observed_output(bundle, pipeline, scored, tables)
    report = MetricsReport()
    sources = _var_sources(bundle.model, bundle.resolved.model.input_schema)
    if "inverse_units" in bundle.model.manifest.capabilities.owns:
        # the model already reports original units; observed stay at the model stage too
        pred_data = scored.pred
        pred_spaces = {str(v): "model-owned" for v in scored.pred.data_vars}
    else:
        inv = pipeline.inverse_outputs(
            scored.pred,
            bundle.states,
            sources,
            representation=rep,
            retransform="smearing"
            if bundle.model.settings.get("retransform") == "smearing"
            else "median",
            residuals=bundle.residuals,
            side_tables=tables,
        )
        pred_data, pred_spaces = inv.data, inv.space
    for var, src in sources.items():
        if var not in pred_data or src not in observed:
            continue
        values, skipped = compute_metrics(pred_data[var], observed[src], rep, names, mask=mask)
        report.values.update(values)
        report.skipped.update(skipped)
        counted = observed[src].notnull()
        if mask is not None:
            counted = counted & mask.broadcast_like(counted).astype(bool)
        report.n_holdout = int(counted.sum())
        report.output_space = pred_spaces.get(var)
        break
    else:
        for n in names:
            report.skipped.setdefault(n, "no observed target values to compare against")
    return report
