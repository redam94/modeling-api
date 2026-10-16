"""``fit``: the SDK harness end to end, then the bundle, its sidecars and the ``ui/`` views.

Outputs (relative to the attempt prefix)::

    bundle.zarr.zip  bundle.manifest.json  diagnostics.json  plots.json  metrics.json
    coords.json  scorecard.json (M3: the sanity scorecard, mapi_spec.scorecard.Scorecard)
    evaluation/{bundle.zarr.zip, manifest.json, diagnostics.json, plots.json, metrics.json}
    ui/{thin.zarr.zip, summaries.json, tree_index.json, preprocess.json, grids/*.arrow,
        grids/response_curves.json (M3: the what-if grid view), plotdata/*.arrow}

The SDK names the bundle's own manifest ``manifest.json``; at the top level that name is the
runner's output manifest, so it is uploaded as ``bundle.manifest.json``.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.schema import summarize_coords
from mapi_spec.capabilities import REPRESENTATION_RULES
from mapi_spec.run import RunnerPhase
from mapi_spec.scorecard import SCORECARD_OUTPUT
from mapi_spec.spec import AsOfSplit, ModelSpec, ScoreRequest, TimeHoldoutSplit
from mapi_spec.views import RESPONSE_GRID_VIEW

from mapi_runner.jobs import BUNDLE_MANIFEST_OUTPUT
from mapi_runner.worker.checkpoints import RunCheckpointSink
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import (
    load_dataset,
    load_raw_schema,
    load_side_tables,
    supplied_side_tables,
)
from mapi_runner.worker.guided import check_lift_tests, response_grid_view
from mapi_runner.worker.plots import render_for
from mapi_runner.worker.scorecard import fit_scorecard
from mapi_runner.worker.views import externalize_plot_data, write_ui_views

__all__ = ["FIT_COORDS", "run_fit", "write_fit_coords", "write_guided_views"]

FIT_COORDS = "coords.json"


def run_fit(ctx: WorkerContext, spec: ModelSpec) -> Bundle:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the dataset")
    data = load_dataset(ctx)
    schema = load_raw_schema(ctx, data)
    tables = load_side_tables(ctx)
    check_lift_tests(spec, data)
    sink: RunCheckpointSink | None = None
    if ctx.start.checkpointing:
        sink = RunCheckpointSink(ctx)
        if ctx.start.resume:
            ctx.download_resume()
    ctx.state(RunnerPhase.FIT, "fitting the pipeline and the model")
    bundle = fit_spec(
        spec,
        data,
        tables,
        ctx.registry,
        raw_schema=schema,
        hooks=ctx.fit_hooks(checkpoint_sink=sink, window=(0.0, 0.9)),
    )
    ctx.cancel.check()
    ctx.state(RunnerPhase.PLOTS, "rendering plots and views")
    refs = [str(p) for p in spec.outputs.plots]
    plots = render_for(
        ctx,
        refs,
        [bundle.to_tree(), lambda: _evaluation_tree(ctx, spec, data, bundle, tables)],
    )
    bundle.plots = externalize_plot_data(ctx, plots)
    write_bundle(ctx, bundle)
    write_fit_coords(ctx, bundle)
    write_ui_views(ctx, bundle)
    write_guided_views(ctx, bundle, data)
    for diagnostic in bundle.diagnostics[:200]:
        ctx.diagnostic(diagnostic)
    for name, value in sorted(bundle.metrics.values.items()):
        if value is not None and np.isfinite(value):
            ctx.metric(name, float(value))
    ctx.progress(1.0, "fit complete", force=True)
    return bundle


def write_guided_views(ctx: WorkerContext, bundle: Bundle, data: xr.Dataset) -> None:
    """``scorecard.json`` and, for models with response curves, the what-if grid view."""
    card = fit_scorecard(bundle, data)
    ctx.write_json(SCORECARD_OUTPUT, card.model_dump(mode="json", exclude_none=True))
    try:
        grid = response_grid_view(bundle)
    except ValueError as exc:  # the view contract refused it (never fail a fit on a view)
        ctx.warning(
            "VIEW_INVALID",
            "the what-if grid was not written: it does not meet the view contract",
            str(exc)[:8000],
        )
        grid = None
    if grid is not None:
        ctx.write_json(RESPONSE_GRID_VIEW, grid.model_dump(mode="json", exclude_none=True))


def write_bundle(ctx: WorkerContext, bundle: Bundle) -> None:
    """``Bundle.write`` into the outputs dir, then rename the SDK manifest."""
    manifest = bundle.write(ctx.outputs_dir)
    os.replace(ctx.outputs_dir / "manifest.json", ctx.outputs_dir / BUNDLE_MANIFEST_OUTPUT)
    del manifest


def write_fit_coords(ctx: WorkerContext, bundle: Bundle) -> None:
    """``coords.json``: ``{stage: {dim: CoordSummary}}`` of the fitted data, which the
    control plane stores as ``ScoreMetadata.fit_coords`` for CoordPlan checks at submit."""
    summaries = {
        stage: {
            dim: summary.model_dump(mode="json", exclude_none=True)
            for dim, summary in summarize_coords(coords, bundle.resolved.stages.get(stage)).items()
        }
        for stage, coords in bundle.fit_coords.items()
    }
    ctx.write_json(FIT_COORDS, summaries)


def _holdout(spec: ModelSpec, data: xr.Dataset, evaluation: Bundle) -> xr.Dataset | None:
    split = spec.data.split
    if not isinstance(split, TimeHoldoutSplit | AsOfSplit):
        return None
    stage = evaluation.resolved.split_stage or "raw"
    coords = evaluation.fit_coords.get(stage)
    if coords is None or split.dim not in coords.coords:
        return None
    end = coords[split.dim].values.max()
    dim = evaluation.binding.to_dataset(split.dim)
    if dim not in data.dims:
        return None
    mask = np.asarray(data[dim].values) > end
    return data.isel({dim: np.flatnonzero(mask)}) if mask.any() else None


def _evaluation_tree(
    ctx: WorkerContext,
    spec: ModelSpec,
    data: xr.Dataset,
    bundle: Bundle,
    tables: dict[str, xr.Dataset],
) -> xr.DataTree | None:
    """Full predictions + observed values on the holdout (in sample without a split), for
    plots that need predictions (fan charts, fit vs actual, residuals)."""
    target = bundle.evaluation or bundle
    holdout = _holdout(spec, data, target) if bundle.evaluation is not None else None
    payload = holdout if holdout is not None else data
    caps = target.model.manifest.capabilities
    request: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": f"model://{spec.metadata.project}/{spec.metadata.name}@latest",
        "mode": "evaluate" if "evaluate" in caps.score_modes else "predict",
        "data": "dataset://runner/holdout@latest",
        "output": {
            "names": "canonical",
            "kind": "predictive"
            if REPRESENTATION_RULES[caps.representation].predictive
            else "expected",
        },
        "seed": spec.model.fit.seed or 0,
    }
    supplied = supplied_side_tables(spec, tables)
    if supplied:  # a score request must name the side tables it supplies
        request["side_tables"] = supplied
    metrics = [m for m in spec.evaluate.metrics if not m.startswith("elpd_")] or ["rmse"]
    try:
        return score_bundle(
            target,
            ScoreRequest.model_validate(request),
            payload,
            side_tables=tables,
            registry=ctx.registry,
            metrics=metrics,
            return_full=True,
        )
    except Exception as exc:  # plots only: never fail the fit
        ctx.warning(
            "EVALUATION_VIEW_FAILED",
            "the holdout predictions for plots could not be computed",
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        )
        return None
