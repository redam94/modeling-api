"""``score``, ``evaluate`` and ``derive`` runs against a fitted bundle.

Outputs:

* score - ``score.zarr.zip`` (the result ``DataTree``), ``result.json`` (representation, units,
  output space, issues, metrics), ``summary.json`` (the summary as plain tables, which the API
  inlines in its answer; a table lists the rendered plots that draw it; forecasts add
  ``conditioning``, what the forecast was conditioned on - :mod:`mapi_spec.actuals` - also
  in ``result.json``), ``ui/summary.arrow`` and ``plots.json`` (by the shape of each output,
  :mod:`mapi_runner.worker.score_plots`); models with grid outputs add ``ui/fields.json`` (a
  FieldsView of the maps, as in the Model Explorer; not for counterfactuals); forecasts add
  ``ui/guided/forecast.json`` and counterfactuals ``ui/guided/counterfactual.json`` (M3,
  :mod:`mapi_runner.worker.guided`), and a forecast's fan chart shows the fitted history
  with the split line;
* evaluate - the same plus ``metrics.json`` (a ``MetricsReport``);
* derive - ``derived.zarr.zip`` (``/derived/<name>``), ``ui/grids/<name>.arrow`` and
  ``result.json``.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
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
from mapi_spec.views import COUNTERFACTUAL_VIEW, FORECAST_VIEW, SCORE_FIELDS_VIEW

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
from mapi_runner.worker.explore_data import SectionSkipped
from mapi_runner.worker.fieldview import fields_view
from mapi_runner.worker.guided import _kpi_var, counterfactual_view, forecast_view
from mapi_runner.worker.plots import render_for
from mapi_runner.worker.score_plots import (
    ScorePlots,
    area_plot_tree,
    default_score_plots,
    field_plot_tree,
    has_area_outputs,
    has_grid_outputs,
    time_dims,
)

__all__ = ["run_derive", "run_evaluate", "run_score"]

_MAX_TABLE_PLOTS = 16
"""``ScoreSummaryTable.plots`` holds at most this many recipe ids."""


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


def summary_tables(
    ds: xr.Dataset,
    *,
    max_cells: int = SCORE_SUMMARY_MAX_CELLS,
    time_dims: Collection[str] = (),
    plots: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The ``summary`` group (variables with a leading ``stat`` dim) as plain tables: the
    coordinate columns (``dims``), then one column per statistic; at most ``max_cells``
    cells, smallest tables first (a large grid is what a truncation drops). A table over one
    of ``time_dims`` names it ``time_dim``; a circular output summarised on the circle
    carries its ``period``; a variable in ``plots`` lists the recipe ids that draw it."""
    tables: list[dict[str, Any]] = []
    cells = 0
    truncated = False
    names = [n for n in ds.data_vars if "stat" in ds[n].dims]
    for name in sorted(names, key=lambda n: (ds[n].size, str(n))):
        da = ds[name]
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
        table: dict[str, Any] = {
            "variable": str(name),
            "columns": [*dims, *stats],
            "rows": rows,
            "dims": dims,
        }
        t = next((d for d in time_dims if d in dims), None)
        if t is not None:
            table["time_dim"] = t
        period = da.attrs.get(CIRCULAR_ATTR)
        if period is not None:
            table["period"] = float(period)
        drawn = list((plots or {}).get(str(name), ()))[:_MAX_TABLE_PLOTS]
        if drawn:
            table["plots"] = drawn
        tables.append(table)
        if truncated:
            break
    return {"tables": tables, "truncated": truncated}


def summary_document(
    tree: xr.DataTree,
    actuals: list[dict[str, Any]] | None = None,
    bundle: Bundle | None = None,
    plots: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any] | None:
    """``summary.json`` (a :class:`~mapi_spec.api.ScoreSummary`) of a score result: the
    summary tables (with their time dim, given the ``bundle``, and the ids of the rendered
    ``plots`` that draw each variable) and, for forecasts, what the forecast was conditioned
    on (:mod:`mapi_spec.actuals`) and the actuals themselves (:func:`actuals_tables`).
    ``None`` when the result has no summary group."""
    if "summary" not in tree:
        return None
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    ds = node.to_dataset()
    out = summary_tables(
        ds, time_dims=time_dims(bundle, ds) if bundle is not None else (), plots=plots
    )
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
    # the KPI on the target's own dims (a gridded model's map, not its total)
    var = _kpi_var(bundle, node.to_dataset(), skip_grids=False)
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


def _without_full(tree: xr.DataTree, *, observed: bool = False) -> xr.DataTree:
    """``tree`` without the full-representation groups scored only for the guided views,
    the maps and the evaluate plots, and without ``/observed`` when ``observed`` (the stored
    result of a score-page evaluate never carries the observed target)."""
    drop = (*_FULL_GROUPS, "/observed") if observed else _FULL_GROUPS
    return xr.DataTree.from_dict(
        {
            node.path: node.to_dataset(inherit=False)
            for node in tree.subtree
            if node.path not in drop
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


def _write_fields(ctx: WorkerContext, bundle: Bundle, tree: xr.DataTree) -> None:
    """``ui/fields.json``: the grid outputs of a result that still holds ``/predictions``."""
    if "predictions" not in tree:
        return
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    pred = node.to_dataset(inherit=False)
    found: dict[str, str] = {}  # output -> its name in the result (as the summary tables)

    def source(name: str) -> xr.DataArray | None:
        for candidate in (name, bundle.binding.to_dataset(name)):
            if candidate in pred.data_vars:
                found[name] = candidate
                return pred[candidate]
        return None

    try:
        view = fields_view(
            bundle,
            source,
            caption="Shows each output of the score on its grid. Select the mean, the "
            "uncertainty or a draw. Use the slider to go through the frames.",
        )
        fields = [
            f.model_copy(update={"output": found.get(f.output, f.output)[:128]})
            for f in view.fields
        ]
        view = view.model_copy(update={"fields": fields})
        ctx.write_json(SCORE_FIELDS_VIEW, view.model_dump(mode="json", exclude_none=True))
    except SectionSkipped:
        return
    except (ValueError, KeyError, MapiError) as exc:  # a view never fails the score
        ctx.warning(
            "VIEW_FAILED",
            "the field view of the score could not be written",
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        )


def _drawn(plots: ScorePlots, rendered: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Each result variable -> the ids of the rendered plots whose options name it."""
    ids = {str(p.get("id")) for p in rendered}
    out = {var: [r for r in rids if r in ids] for var, rids in plots.by_var().items()}
    return {var: rids for var, rids in out.items() if rids}


def _plot_input(bundle: Bundle, tree: xr.DataTree) -> xr.DataTree:
    """``tree`` with the bundle's ``/binding`` (as the fit-time plot input has), so a recipe
    finds the dataset names of canonical dims. A score with dataset names calls the ``outcome``
    dim of a multi-outcome MMM e.g. ``product``, and the outcomes must not be added up."""
    if "binding" in tree.children:
        return tree
    out = tree.copy(deep=False)  # the stored result keeps its own nodes
    out["binding"] = xr.DataTree(
        xr.Dataset(attrs={"json": bundle.resolved.binding.model_dump_json()})
    )
    return out


def _write_tree(
    ctx: WorkerContext,
    bundle: Bundle,
    tree: xr.DataTree,
    plots: ScorePlots,
    plot_trees: list[xr.DataTree] | None = None,
    actuals: list[dict[str, Any]] | None = None,
) -> None:
    raw = write_datatree_zip(tree)
    assert raw is not None
    ctx.write_output("score.zarr.zip", raw)
    ctx.write_json("result.json", _result_json(tree))
    # the plots first: each summary table lists the rendered plots that draw its variable
    ctx.state(RunnerPhase.PLOTS, "rendering plots")
    rendered = render_for(
        ctx, plots.refs, [*(plot_trees or []), _plot_input(bundle, tree)], options=plots.options
    )
    ctx.write_json("plots.json", rendered)
    summary = summary_document(tree, actuals, bundle, plots=_drawn(plots, rendered))
    if summary is not None:
        node = tree["summary"]
        assert isinstance(node, xr.DataTree)
        ctx.write_json(SCORE_SUMMARY_OUTPUT, summary)
        try:
            ctx.write_output("ui/summary.arrow", write_arrow_ipc(to_arrow_table(node.to_dataset())))
        except PayloadLimitError as exc:
            ctx.warning("VIEW_TOO_LARGE", f"the summary view was not written: {exc.detail}")


def run_score(ctx: WorkerContext, job: ScoreJob) -> xr.DataTree:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the bundle and the payload")
    bundle = load_bundle(ctx)
    data = _payload(ctx, INPUT_DATASET, INPUT_INLINE_DATA, bundle)
    scenario = _payload(ctx, INPUT_SCENARIO, INPUT_INLINE_SCENARIO, bundle)
    history = _optional_dataset(ctx, INPUT_HISTORY)
    tables = load_side_tables(ctx, bundle.spec)
    ctx.state(RunnerPhase.SCORE, f"scoring ({job.request.mode})")
    mode = job.request.mode
    guided = mode in ("forecast", "counterfactual")
    # grid and area outputs keep their draws until the maps are drawn (the summary has no
    # draws); an evaluate keeps its predictions and /observed for the evaluate plots
    full = guided or has_grid_outputs(bundle) or has_area_outputs(bundle) or mode == "evaluate"
    tree = score_bundle(
        bundle,
        job.request,
        data,
        scenario=scenario,
        side_tables=tables,
        registry=ctx.registry,
        max_draws=job.max_draws,
        hooks=ctx.fit_hooks(),
        return_full=full,
        history=history,
    )
    plot_trees: list[xr.DataTree] = []
    if guided:
        plot_trees = _guided_views(ctx, bundle, tree, job.request, tables)
    if mode == "evaluate":
        # the untrimmed result: fit vs actual and the binary plots need /observed
        plot_trees.insert(0, tree)
    if mode != "counterfactual":
        field_tree = field_plot_tree(bundle, tree)
        area_tree = area_plot_tree(bundle, tree)
        plot_trees += [t for t in (field_tree, area_tree) if t is not None]
        _write_fields(ctx, bundle, tree)
    plots = (
        ScorePlots(list(job.plots))
        if job.plots is not None
        else default_score_plots(bundle, mode, tree)
    )
    if full:
        # the stored evaluate result never carries the /observed that return_full added
        tree = _without_full(tree, observed=mode == "evaluate")
    try:
        flat = (
            None if data is None else flatten(data, bundle.spec.data, load_tree_schema(ctx, data))
        )
        actuals = actuals_tables(bundle, job.request, flat, tree)
    except (ValueError, KeyError) as exc:  # the comparison is a convenience; the score stands
        ctx.warning("VIEW_FAILED", f"the actuals were not added to the summary: {exc}"[:500])
        actuals = []
    _write_tree(ctx, bundle, tree, plots, plot_trees, actuals)
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
    _write_fields(ctx, bundle, tree)
    area_tree = area_plot_tree(bundle, tree)
    _write_tree(
        ctx,
        bundle,
        tree,
        default_score_plots(bundle, "evaluate", tree),
        [area_tree] if area_tree is not None else None,
    )
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
