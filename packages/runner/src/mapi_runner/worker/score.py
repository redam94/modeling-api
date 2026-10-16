"""``score``, ``evaluate`` and ``derive`` runs against a fitted bundle.

Outputs:

* score - ``score.zarr.zip`` (the result ``DataTree``), ``result.json`` (representation, units,
  output space, issues, metrics), ``ui/summary.arrow`` and ``plots.json``; forecasts add
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
import xarray as xr
from mapi_sdk._util import normalise_dataset
from mapi_sdk.bundle import Bundle, MetricsReport
from mapi_sdk.context import drop_context
from mapi_sdk.errors import PayloadLimitError
from mapi_sdk.harness import _to_original, score_bundle
from mapi_sdk.models import ScoreContext
from mapi_sdk.wire import to_arrow_table, write_arrow_ipc, write_datatree_zip
from mapi_spec.capabilities import REPRESENTATION_RULES
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import INPUT_DATASET, RunnerPhase
from mapi_spec.spec import ScoreRequest
from mapi_spec.views import COUNTERFACTUAL_VIEW, FORECAST_VIEW

from mapi_runner.jobs import INPUT_SCENARIO, DeriveJob, EvaluateJob, ScoreJob
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import (
    load_bundle,
    load_dataset,
    load_side_tables,
    supplied_side_tables,
)
from mapi_runner.worker.guided import counterfactual_view, forecast_view
from mapi_runner.worker.plots import render_for

__all__ = ["run_derive", "run_evaluate", "run_score"]

_DEFAULT_PLOTS: dict[str, list[str]] = {
    "predict": ["core.ppc_fan"],
    "forecast": ["core.ppc_fan"],
    "counterfactual": ["core.counterfactual"],
    "evaluate": ["core.fit_vs_actual", "core.residuals_over_time"],
}


def _optional_dataset(ctx: WorkerContext, name: str) -> xr.Dataset | None:
    return load_dataset(ctx, name) if ctx.has_input(name) else None


def _result_json(tree: xr.DataTree) -> dict[str, Any]:
    attrs = {str(k): v for k, v in tree.attrs.items()}
    out: dict[str, Any] = {"groups": sorted(n.path for n in tree.subtree if n.path != "/")}
    for key, value in attrs.items():
        if key in ("output_space", "retransform", "issues", "metrics") and isinstance(value, str):
            out[key] = json.loads(value)
        elif isinstance(value, np.generic):
            out[key] = value.item()
        else:
            out[key] = value
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
) -> None:
    raw = write_datatree_zip(tree)
    assert raw is not None
    ctx.write_output("score.zarr.zip", raw)
    ctx.write_json("result.json", _result_json(tree))
    if "summary" in tree:
        node = tree["summary"]
        assert isinstance(node, xr.DataTree)
        try:
            ctx.write_output("ui/summary.arrow", write_arrow_ipc(to_arrow_table(node.to_dataset())))
        except PayloadLimitError as exc:
            ctx.warning("VIEW_TOO_LARGE", f"the summary view was not written: {exc.detail}")
    ctx.state(RunnerPhase.PLOTS, "rendering plots")
    ctx.write_json("plots.json", render_for(ctx, extra_plots, [*(plot_trees or []), tree]))


def run_score(ctx: WorkerContext, job: ScoreJob) -> xr.DataTree:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the bundle and the payload")
    bundle = load_bundle(ctx)
    data = _optional_dataset(ctx, INPUT_DATASET)
    scenario = _optional_dataset(ctx, INPUT_SCENARIO)
    tables = load_side_tables(ctx)
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
    )
    plot_trees: list[xr.DataTree] = []
    if guided:
        plot_trees = _guided_views(ctx, bundle, tree, job.request, tables)
        tree = _without_full(tree)
    plots = job.plots if job.plots is not None else _DEFAULT_PLOTS.get(job.request.mode, [])
    _write_tree(ctx, tree, plots, plot_trees)
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
    tables = load_side_tables(ctx)
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
    data = load_dataset(ctx)
    tables = load_side_tables(ctx)
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
