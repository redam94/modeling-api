"""``score``, ``evaluate`` and ``derive`` runs against a fitted bundle.

Outputs:

* score - ``score.zarr.zip`` (the result ``DataTree``), ``result.json`` (representation, units,
  output space, issues, metrics), ``summary.json`` (the summary as plain tables, which the API
  inlines in its answer; forecasts add ``conditioning``, what the forecast was conditioned on -
  :mod:`mapi_spec.actuals` - also in ``result.json``), ``ui/summary.arrow`` and
  ``plots.json``; forecasts add
  ``ui/guided/forecast.json`` and counterfactuals ``ui/guided/counterfactual.json`` (M3,
  :mod:`mapi_runner.worker.guided`), and a forecast's fan chart shows the fitted history
  with the split line;
* evaluate - the same plus ``metrics.json`` (a ``MetricsReport``);
* derive - ``derived.zarr.zip`` (``/derived/<name>``), ``ui/grids/<name>.arrow`` and
  ``result.json``.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk._util import normalise_dataset
from mapi_sdk.bundle import Bundle, MetricsReport
from mapi_sdk.context import drop_context
from mapi_sdk.errors import PayloadLimitError
from mapi_sdk.harness import _to_original, score_bundle
from mapi_sdk.models import ScoreContext
from mapi_sdk.summarise import CIRCULAR_ATTR
from mapi_sdk.wire import to_arrow_table, write_arrow_ipc, write_datatree_zip
from mapi_spec._types import RESERVED_STAGE
from mapi_spec.actuals import CONDITIONING_ATTR
from mapi_spec.api import SCORE_SUMMARY_MAX_CELLS, SCORE_SUMMARY_OUTPUT
from mapi_spec.capabilities import REPRESENTATION_RULES
from mapi_spec.endpoints import INPUT_INLINE_DATA, INPUT_INLINE_SCENARIO
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import INPUT_DATASET, RunnerPhase
from mapi_spec.spec import ScoreRequest
from mapi_spec.views import COUNTERFACTUAL_VIEW, FORECAST_VIEW

from mapi_runner.jobs import INPUT_HISTORY, INPUT_SCENARIO, DeriveJob, EvaluateJob, ScoreJob
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import (
    flatten,
    load_bundle,
    load_dataset,
    load_inline,
    load_side_tables,
    load_tree_schema,
    supplied_side_tables,
)
from mapi_runner.worker.guided import _kpi_var, counterfactual_view, forecast_view
from mapi_runner.worker.plots import render_for

__all__ = ["run_derive", "run_evaluate", "run_score"]

_DEFAULT_PLOTS: dict[str, list[str]] = {
    "predict": ["core.ppc_fan"],
    "forecast": ["core.ppc_fan"],
    "counterfactual": ["core.counterfactual"],
    "evaluate": ["core.fit_vs_actual", "core.residuals_over_time"],
}


def _optional_dataset(ctx: WorkerContext, name: str) -> xr.Dataset | xr.DataTree | None:
    """A dataset input when given (M4: a tree dataset stays a tree; the harness selects
    the bundle's ``data.nodes`` from it)."""
    return load_dataset(ctx, name) if ctx.has_input(name) else None


def _payload(
    ctx: WorkerContext, name: str, inline: str, bundle: Bundle
) -> xr.Dataset | xr.DataTree | None:
    """A score input: the stored dataset ``name``, else an endpoint call's inline XrJSON
    (named like the endpoint's canonical input contract; renamed to the dataset's names)."""
    data = _optional_dataset(ctx, name)
    if data is None and ctx.has_input(inline):
        data = load_inline(ctx, inline, bundle.raw_schema, bundle.binding)
    return data


def _result_json(tree: xr.DataTree) -> dict[str, Any]:
    attrs = {str(k): v for k, v in tree.attrs.items()}
    out: dict[str, Any] = {"groups": sorted(n.path for n in tree.subtree if n.path != "/")}
    for key, value in attrs.items():
        if key in ("output_space", "retransform", "issues", "metrics", CONDITIONING_ATTR) and (
            isinstance(value, str)
        ):
            out[key] = json.loads(value)
        elif isinstance(value, np.generic):
            out[key] = value.item()
        else:
            out[key] = value
    return out


def _cell(value: Any) -> Any:
    """A JSON cell: ISO text for dates (just the day at midnight), ``None`` for NaN."""
    if isinstance(value, np.datetime64 | pd.Timestamp):
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        return ts.date().isoformat() if ts == ts.normalize() else ts.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def summary_tables(ds: xr.Dataset, *, max_cells: int = SCORE_SUMMARY_MAX_CELLS) -> dict[str, Any]:
    """The ``summary`` group (variables with a leading ``stat`` dim) as plain tables: the
    coordinate columns, then one column per statistic; at most ``max_cells`` cells. A
    circular output summarised on the circle carries its ``period``."""
    tables: list[dict[str, Any]] = []
    cells = 0
    truncated = False
    for name in sorted(ds.data_vars, key=str):
        da = ds[name]
        if "stat" not in da.dims:
            continue
        da = da.transpose("stat", ...)
        dims = [str(d) for d in da.dims if d != "stat"]
        stats = [str(s) for s in da["stat"].values]
        labels = [ds[d].values if d in ds.coords else np.arange(da.sizes[d]) for d in dims]
        values = da.values
        rows: list[list[Any]] = []
        for idx in np.ndindex(*[da.sizes[d] for d in dims]):
            if cells + len(dims) + len(stats) > max_cells:
                truncated = True
                break
            rows.append(
                [_cell(labels[k][i]) for k, i in enumerate(idx)]
                + [_cell(v) for v in values[(slice(None), *idx)]]
            )
            cells += len(dims) + len(stats)
        table: dict[str, Any] = {"variable": str(name), "columns": [*dims, *stats], "rows": rows}
        period = da.attrs.get(CIRCULAR_ATTR)
        if period is not None:
            table["period"] = float(period)
        tables.append(table)
        if truncated:
            break
    return {"tables": tables, "truncated": truncated}


def summary_document(
    tree: xr.DataTree, actuals: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """``summary.json`` (a :class:`~mapi_spec.api.ScoreSummary`) of a score result: the
    summary tables and, for forecasts, what the forecast was conditioned on
    (:mod:`mapi_spec.actuals`) and the actuals themselves (:func:`actuals_tables`). ``None``
    when the result has no summary group."""
    if "summary" not in tree:
        return None
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    out = summary_tables(node.to_dataset())
    conditioning = tree.attrs.get(CONDITIONING_ATTR)
    if isinstance(conditioning, str):
        out["conditioning"] = json.loads(conditioning)
    if actuals:
        out["actuals"] = actuals
    return out


def actuals_tables(
    bundle: Bundle,
    req: ScoreRequest,
    data: xr.Dataset | None,
    tree: xr.DataTree,
    *,
    max_cells: int = SCORE_SUMMARY_MAX_CELLS // 4,
) -> list[dict[str, Any]]:
    """The actuals a forecast was sent, to compare with the predictions on their periods:
    one table per target, named after the summary's prediction variable (its KPI), with the
    target's coordinate columns then ``actual``. Only for raw-stage payloads (dataset names,
    original units) and output in original units; the names follow ``output.names``."""
    if (
        req.mode != "forecast"
        or data is None
        or req.inputs_stage != RESERVED_STAGE
        or req.output.units != "original"
        or "summary" not in tree
    ):
        return []
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    var = _kpi_var(bundle, node.to_dataset())
    raw = bundle.resolved.stages.get(RESERVED_STAGE)
    if var is None or raw is None:
        return []
    out: list[dict[str, Any]] = []
    for target in raw.targets:
        name = bundle.binding.to_dataset(target)
        if name not in data:
            continue
        ds = xr.Dataset({name: data[name]})
        if req.output.names == "canonical":
            ds = bundle.binding.apply(ds, missing="ignore")
            name = target
        da = ds[name]
        dims = [str(d) for d in da.dims]
        labels = [ds[d].values if d in ds.coords else np.arange(da.sizes[d]) for d in dims]
        values = np.asarray(da.values, dtype=np.float64)
        rows: list[list[Any]] = []
        for idx in zip(*np.nonzero(np.isfinite(values)), strict=True):
            if (len(rows) + 1) * (len(dims) + 1) > max_cells:
                break
            rows.append([_cell(labels[k][i]) for k, i in enumerate(idx)] + [_cell(values[idx])])
        if rows:
            out.append({"variable": var, "columns": [*dims, "actual"], "rows": rows})
    return out


_FULL_GROUPS = ("/predictions", "/baseline/predictions", "/delta/predictions")


def _without_full(tree: xr.DataTree) -> xr.DataTree:
    """``tree`` without the full-representation groups scored only for the guided views."""
    return xr.DataTree.from_dict(
        {
            node.path: node.to_dataset(inherit=False)
            for node in tree.subtree
            if node.path not in _FULL_GROUPS
        }
    )


def _guided_views(
    ctx: WorkerContext,
    bundle: Bundle,
    tree: xr.DataTree,
    request: ScoreRequest,
    tables: dict[str, xr.Dataset],
) -> list[xr.DataTree]:
    """Write the guided view of a forecast/counterfactual; returns extra plot inputs."""
    try:
        if request.mode == "forecast":
            got = forecast_view(bundle, tree, request, registry=ctx.registry, tables=tables)
            if got is None:
                return []
            view, plot_tree = got
            ctx.write_json(FORECAST_VIEW, view.model_dump(mode="json", exclude_none=True))
            return [plot_tree]
        cf = counterfactual_view(bundle, tree, request)
        if cf is not None:
            ctx.write_json(COUNTERFACTUAL_VIEW, cf.model_dump(mode="json", exclude_none=True))
    except (ValueError, KeyError, MapiError) as exc:  # a view never fails the score
        ctx.warning(
            "VIEW_FAILED",
            f"the guided {request.mode} view could not be written",
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        )
    return []


def _write_tree(
    ctx: WorkerContext,
    tree: xr.DataTree,
    extra_plots: list[str],
    plot_trees: list[xr.DataTree] | None = None,
    actuals: list[dict[str, Any]] | None = None,
) -> None:
    raw = write_datatree_zip(tree)
    assert raw is not None
    ctx.write_output("score.zarr.zip", raw)
    ctx.write_json("result.json", _result_json(tree))
    summary = summary_document(tree, actuals)
    if summary is not None:
        node = tree["summary"]
        assert isinstance(node, xr.DataTree)
        ctx.write_json(SCORE_SUMMARY_OUTPUT, summary)
        try:
            ctx.write_output("ui/summary.arrow", write_arrow_ipc(to_arrow_table(node.to_dataset())))
        except PayloadLimitError as exc:
            ctx.warning("VIEW_TOO_LARGE", f"the summary view was not written: {exc.detail}")
    ctx.state(RunnerPhase.PLOTS, "rendering plots")
    ctx.write_json("plots.json", render_for(ctx, extra_plots, [*(plot_trees or []), tree]))


def run_score(ctx: WorkerContext, job: ScoreJob) -> xr.DataTree:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the bundle and the payload")
    bundle = load_bundle(ctx)
    data = _payload(ctx, INPUT_DATASET, INPUT_INLINE_DATA, bundle)
    scenario = _payload(ctx, INPUT_SCENARIO, INPUT_INLINE_SCENARIO, bundle)
    history = _optional_dataset(ctx, INPUT_HISTORY)
    tables = load_side_tables(ctx, bundle.spec)
    ctx.state(RunnerPhase.SCORE, f"scoring ({job.request.mode})")
    guided = job.request.mode in ("forecast", "counterfactual")
    tree = score_bundle(
        bundle,
        job.request,
        data,
        scenario=scenario,
        side_tables=tables,
        registry=ctx.registry,
        max_draws=job.max_draws,
        hooks=ctx.fit_hooks(),
        return_full=guided,
        history=history,
    )
    plot_trees: list[xr.DataTree] = []
    if guided:
        plot_trees = _guided_views(ctx, bundle, tree, job.request, tables)
        tree = _without_full(tree)
    plots = job.plots if job.plots is not None else _DEFAULT_PLOTS.get(job.request.mode, [])
    try:
        flat = (
            None if data is None else flatten(data, bundle.spec.data, load_tree_schema(ctx, data))
        )
        actuals = actuals_tables(bundle, job.request, flat, tree)
    except (ValueError, KeyError) as exc:  # the comparison is a convenience; the score stands
        ctx.warning("VIEW_FAILED", f"the actuals were not added to the summary: {exc}"[:500])
        actuals = []
    _write_tree(ctx, tree, plots, plot_trees, actuals)
    return tree


def _default_request(bundle: Bundle, tables: dict[str, xr.Dataset]) -> ScoreRequest:
    """The fit-time evaluation request: same output kind and seed, canonical names."""
    caps = bundle.model.manifest.capabilities
    spec = bundle.spec
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"model://{spec.metadata.project}/{spec.metadata.name}@latest",
            "mode": "evaluate" if "evaluate" in caps.score_modes else "predict",
            "data": "dataset://runner/evaluation@latest",
            "output": {
                "names": "canonical",
                "kind": "predictive"
                if REPRESENTATION_RULES[caps.representation].predictive
                else "expected",
            },
            "seed": spec.model.fit.seed or 0,
            "side_tables": supplied_side_tables(spec, tables),
        }
    )


def run_evaluate(ctx: WorkerContext, job: EvaluateJob) -> MetricsReport:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the bundle and the evaluation data")
    bundle = load_bundle(ctx)
    data = load_dataset(ctx)
    tables = load_side_tables(ctx, bundle.spec)
    request = job.request or _default_request(bundle, tables)
    names = job.metrics
    if names is None:
        names = [m for m in bundle.spec.evaluate.metrics if not m.startswith("elpd_")] or ["rmse"]
    ctx.state(RunnerPhase.EVALUATE, "evaluating through the score path")
    tree = score_bundle(
        bundle,
        request,
        data,
        side_tables=tables,
        registry=ctx.registry,
        metrics=names,
        return_full=True,
        hooks=ctx.fit_hooks(),
    )
    report = MetricsReport.model_validate_json(str(tree.attrs["metrics"]))
    ctx.write_output("metrics.json", report.model_dump_json(indent=2).encode())
    for name, value in sorted(report.values.items()):
        if value is not None and np.isfinite(value):
            ctx.metric(name, float(value))
    _write_tree(ctx, tree, _DEFAULT_PLOTS["evaluate"])
    return report


def run_derive(ctx: WorkerContext, job: DeriveJob) -> dict[str, xr.Dataset]:
    """Derived quantities declared by the model, computed over ``dataset`` with the bundle's
    stored step states (never refitted) and mapped to the output space."""
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the bundle and the data")
    bundle = load_bundle(ctx)
    loaded = load_dataset(ctx)
    data = flatten(loaded, bundle.spec.data, load_tree_schema(ctx, loaded))
    tables = load_side_tables(ctx, bundle.spec)
    registry = ctx.registry
    manifest = bundle.model.manifest
    plugin = registry.model(f"{manifest.id}@{manifest.version}")()
    derive = getattr(plugin, "derive", None)
    if not callable(derive):
        raise MapiError(
            f"model {manifest.id!r} computes no derived quantities",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    declared = {d.name for d in manifest.derived}
    for j, name in enumerate(job.names):
        if name not in declared:
            raise MapiError(
                f"model {manifest.id!r} declares no derived quantity {name!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/spec/names/{j}",
            )
    ctx.state(RunnerPhase.DERIVE, "replaying the pipeline")
    pipeline = bundle.pipeline(registry)
    seed = bundle.model.fit_config.seed or 0
    raw = bundle.binding.apply(normalise_dataset(data), missing="ignore")
    trace = pipeline.transform(
        raw,
        bundle.states,
        side_tables={**tables, **bundle.side_tables},
        seed=seed,
        cancel=ctx.cancel,
        log=ctx.log,
        fit_coords=bundle.fit_coords,
    )
    model_ds = drop_context(trace.data, trace.marks)
    draws = job.draws or int(bundle.spec.outputs.ui_draws)
    sctx = ScoreContext(
        mode="predict",
        kind="expected",
        seed=seed,
        schema=bundle.model.schema,
        policies=bundle.resolved.model.effective_policies,
        draw_index=None if bundle.thin is None else bundle.thin[:draws],
        log=ctx.log,
        cancel=ctx.cancel,
        to_original=_to_original(pipeline, bundle.states, {**tables, **bundle.side_tables}),
        tree=bundle.binding.apply_tree(loaded)
        if isinstance(loaded, xr.DataTree) and manifest.capabilities.input == "tree"
        else None,
    )
    out: dict[str, xr.Dataset] = {}
    for i, name in enumerate(job.names):
        ctx.cancel.check()
        ctx.progress(i / len(job.names), f"deriving {name}")
        try:
            result = derive(bundle.model, name, model_ds, sctx)
        except MapiError:
            raise
        except NotImplementedError:
            raise MapiError(
                f"model {manifest.id!r} declares {name!r} but cannot compute it",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            ) from None
        if not isinstance(result, xr.Dataset):
            raise MapiError(
                f"derive({name!r}) must return an xarray Dataset", code=ErrorCode.USER_CODE_ERROR
            )
        out[name] = result
    nodes: dict[str, xr.Dataset] = {"/": xr.Dataset(attrs={"bundle_id": bundle.bundle_id})}
    nodes.update({f"/derived/{k}": v for k, v in out.items()})
    raw_zip = write_datatree_zip(xr.DataTree.from_dict(nodes))
    assert raw_zip is not None
    ctx.write_output("derived.zarr.zip", raw_zip)
    for name, ds in out.items():
        try:
            ctx.write_output(f"ui/grids/{name}.arrow", write_arrow_ipc(to_arrow_table(ds)))
        except PayloadLimitError as exc:
            ctx.warning("VIEW_TOO_LARGE", f"grid {name!r} was not written: {exc.detail}")
    ctx.write_json(
        "result.json",
        {"bundle_id": bundle.bundle_id, "derived": sorted(out), "draws": draws},
    )
    ctx.progress(1.0, "derived quantities ready", force=True)
    return out
