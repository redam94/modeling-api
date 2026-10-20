"""``fit``: the SDK harness end to end, then the bundle, its sidecars and the ``ui/`` views.

Outputs (relative to the attempt prefix)::

    bundle.zarr.zip  bundle.manifest.json  diagnostics.json  plots.json  metrics.json
    coords.json  scorecard.json (M3: the sanity scorecard, mapi_spec.scorecard.Scorecard)
    evaluation/{bundle.zarr.zip, manifest.json, diagnostics.json, plots.json, metrics.json}
    ui/{thin.zarr.zip, summaries.json, tree_index.json, preprocess.json, grids/*.arrow,
        grids/response_curves.json (M3: the what-if grid view), plotdata/*.arrow}
    ui/explore/{index.json, <section>.json, plots/<section>.json} (M3.5: the Model
        Explorer's fit-time sections, mapi_runner.worker.explore_model.FIT_SECTIONS)

v0.15: on an image collection (:func:`~mapi_runner.worker.data.is_collection`) the harness
reads the images lazily from the packs (a block cache in ``{scratch}/work/cache``); the
views, the scorecard and the Model Explorer read a decoded sample (:func:`view_sample`).

The SDK names the bundle's own manifest ``manifest.json``; at the top level that name is the
runner's output manifest, so it is uploaded as ``bundle.manifest.json``.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.collection import Collection
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.schema import summarize_coords
from mapi_sdk.tracking import METRICS_HISTORY
from mapi_spec.capabilities import REPRESENTATION_RULES
from mapi_spec.errors import MapiError
from mapi_spec.run import RunnerPhase
from mapi_spec.scorecard import SCORECARD_OUTPUT
from mapi_spec.spec import AsOfSplit, ModelSpec, ScoreRequest, TimeHoldoutSplit
from mapi_spec.views import RESPONSE_GRID_VIEW

from mapi_runner.jobs import BUNDLE_MANIFEST_OUTPUT
from mapi_runner.worker.checkpoints import RunCheckpointSink
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import (
    collection_cache,
    flatten,
    is_collection,
    load_collection,
    load_dataset,
    load_raw_schema,
    load_side_tables,
    load_tree_schema,
    sample_size,
    supplied_side_tables,
)
from mapi_runner.worker.explore import write_model_explorer
from mapi_runner.worker.guided import check_lift_tests, response_grid_view
from mapi_runner.worker.plots import render_for
from mapi_runner.worker.scorecard import fit_scorecard, model_structural_missing
from mapi_runner.worker.views import externalize_plot_data, write_ui_views

__all__ = [
    "FIT_COORDS",
    "fit_plot_refs",
    "run_fit",
    "view_sample",
    "write_fit_coords",
    "write_guided_views",
]

FIT_COORDS = "coords.json"
FIT_HISTORY = "fit_history"
"""The model group of a fit's loss per step (``mapi_pymc.engines.FIT_HISTORY``: ADVI)."""
LOSS_CURVE = "core.loss_curve"
TRAINING = "training"
"""v0.15: the model group of a network's per-epoch curves (``mapi_jax``; ``confusion`` var
when it classifies with validation data)."""
MISCLASSIFIED = "misclassified"
"""v0.15: the model group of wrongly classified validation images (thumbnails)."""
TRAINING_CURVES = "ml.training_curves"
CONFUSION_EPOCHS = "ml.confusion_epochs"
MISCLASSIFIED_PLOT = "ml.misclassified"
VIEW_SAMPLES = 500
"""Images a collection fit decodes for its views, scorecard and Model Explorer."""
VIEW_SIZE = (64, 64)
"""Their size when the collection's images do not share one."""
EVALUATION_SAMPLES = 2000
"""Held-out images a collection fit scores for its plots."""


def run_fit(ctx: WorkerContext, spec: ModelSpec) -> Bundle:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the dataset")
    collection: Collection | None = None
    given: xr.Dataset | xr.DataTree | Collection
    if is_collection(ctx):
        # v0.15: the harness reads the images lazily; the views read a small decoded sample
        collection = collection_cache(ctx, load_collection(ctx))
        given, tree_schema, tree, schema = collection, None, None, None
        try:
            data = view_sample(collection)
        except MapiError as exc:  # the fit does not need the sample: only the views do
            ctx.warning(
                "VIEW_SAMPLE_FAILED",
                "the views of the fit have no decoded sample of the images",
                exc.detail[:8000],
            )
            data = collection.manifest_dataset()
    else:
        given = load_dataset(ctx)
        # M4: a tree dataset is flattened per data.nodes (the harness does it again for the
        # fit); the views below read the flat data, a tree-input model also gets the tree
        tree_schema = load_tree_schema(ctx, given)
        data = flatten(given, spec.data, tree_schema)
        tree = given if isinstance(given, xr.DataTree) else None
        schema = load_raw_schema(ctx, data) if tree is None else None
    tables = load_side_tables(ctx, spec)
    check_lift_tests(spec, data)
    sink: RunCheckpointSink | None = None
    if ctx.start.checkpointing:
        sink = RunCheckpointSink(ctx)
        if ctx.start.resume:
            ctx.download_resume()
    ctx.state(RunnerPhase.FIT, "fitting the pipeline and the model")
    bundle = fit_spec(
        spec,
        given,
        tables,
        ctx.registry,
        raw_schema=schema,
        hooks=ctx.fit_hooks(checkpoint_sink=sink, window=(0.0, 0.9)),
        tree_schema=tree_schema,
    )
    ctx.cancel.check()
    ctx.state(RunnerPhase.PLOTS, "rendering plots and views")
    refs = fit_plot_refs(spec, bundle)
    plots = render_for(
        ctx,
        refs,
        [
            bundle.to_tree(),
            lambda: _evaluation_tree(ctx, spec, data, bundle, tables, tree, collection),
        ],
    )
    bundle.plots = externalize_plot_data(ctx, plots)
    write_bundle(ctx, bundle)
    write_fit_coords(ctx, bundle)
    write_ui_views(ctx, bundle)
    write_guided_views(ctx, bundle, data)
    write_model_explorer(
        ctx,
        bundle,
        data,
        ref=f"model://{spec.metadata.project}/{spec.metadata.name}@latest",
        side_tables=tables,
    )
    for diagnostic in bundle.diagnostics[:200]:
        ctx.diagnostic(diagnostic)
    for name, value in sorted(bundle.metrics.values.items()):
        if value is not None and np.isfinite(value):
            ctx.metric(name, float(value))
    ctx.progress(1.0, "fit complete", force=True)
    return bundle


def fit_plot_refs(spec: ModelSpec, bundle: Bundle) -> list[str]:
    """The spec's ``outputs.plots``, plus ``core.loss_curve`` when the fit kept its loss
    history (an ADVI fit: the loss of an approximation is always worth a look).

    v0.15 (networks): ``ml.training_curves`` when the bundle has a ``training`` group or its
    ``metrics_history`` has a ``split`` label dim, ``ml.confusion_epochs`` when
    ``training.confusion`` exists, ``ml.misclassified`` with a ``misclassified`` group that
    has at least one wrong image.
    """
    refs = [str(p) for p in spec.outputs.plots]
    groups = bundle.model.groups
    wanted: list[str] = []
    if FIT_HISTORY in groups:
        wanted.append(LOSS_CURVE)
    training = groups.get(TRAINING)
    history = groups.get(METRICS_HISTORY)
    if training is not None or (history is not None and "split" in history.dims):
        wanted.append(TRAINING_CURVES)
    if training is not None and "confusion" in training.data_vars:
        wanted.append(CONFUSION_EPOCHS)
    wrong = groups.get(MISCLASSIFIED)
    if wrong is not None and all(n > 0 for n in wrong.sizes.values()):
        # no wrong validation images (all classified right): nothing to show, no warning
        wanted.append(MISCLASSIFIED_PLOT)
    have = {r.split("@")[0] for r in refs}
    refs.extend(ref for ref in wanted if ref not in have)
    return refs


def write_guided_views(ctx: WorkerContext, bundle: Bundle, data: xr.Dataset) -> None:
    """``scorecard.json`` and, for models with response curves, the what-if grid view."""
    structural = model_structural_missing(getattr(ctx, "registry", None), bundle)
    card = fit_scorecard(bundle, data, structural_missing=structural)
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


def view_sample(collection: Collection) -> xr.Dataset:
    """v0.15: what the views, the scorecard and the Model Explorer of a collection fit read:
    a seeded sample of at most :data:`VIEW_SAMPLES` images decoded to ``pixels(image, y, x,
    channel)`` (the collection's size, else 64 x 64; smaller when the sample would pass the
    image cell cap)."""
    size = sample_size(collection.info, VIEW_SAMPLES, collection.info.size or VIEW_SIZE)
    return collection.loader().to_xarray(VIEW_SAMPLES, size, seed=0)


def _collection_holdout(
    bundle: Bundle, evaluation: Bundle | None, collection: Collection
) -> xr.Dataset:
    """The manifest rows the evaluation fit did not train on (all rows without a split), at
    most :data:`EVALUATION_SAMPLES` (seeded): the payload of a collection model's plots."""
    manifest = collection.manifest_dataset()
    dim = collection.info.dim
    if evaluation is not None:
        coords = evaluation.fit_coords.get(evaluation.resolved.model_stage)
        canonical = evaluation.binding.to_canonical(dim)
        if coords is not None and canonical in coords.coords:
            trained = set(np.asarray(coords[canonical].values).astype(str).tolist())
            keep = ~np.isin(manifest[dim].values.astype(str), list(trained))
            if keep.any():
                manifest = manifest.isel({dim: np.flatnonzero(keep)})
    n = manifest.sizes[dim]
    if n > EVALUATION_SAMPLES:
        rng = np.random.default_rng(bundle.spec.model.fit.seed or 0)
        pick = np.sort(rng.choice(n, EVALUATION_SAMPLES, replace=False))
        manifest = manifest.isel({dim: pick})
    return manifest


def _evaluation_tree(
    ctx: WorkerContext,
    spec: ModelSpec,
    data: xr.Dataset,
    bundle: Bundle,
    tables: dict[str, xr.Dataset],
    data_tree: xr.DataTree | None = None,
    collection: Collection | None = None,
) -> xr.DataTree | None:
    """Full predictions + observed values on the holdout (in sample without a split), for
    plots that need predictions (fan charts, fit vs actual, residuals). ``data`` is the flat
    fit data; ``data_tree`` the tree dataset it came from (M4: handed to tree-input models).
    v0.15: a collection fit scores held-out manifest rows through the collection (a model that
    reads datasets scores its seeded sample of the collection)."""
    target = bundle.evaluation or bundle
    holdout = _holdout(spec, data, target) if bundle.evaluation is not None else None
    payload: xr.Dataset | Collection = holdout if holdout is not None else data
    batched = False
    if collection is not None:
        batched = target.model.manifest.capabilities.input == "collection"
        payload = (
            _collection_holdout(bundle, bundle.evaluation, collection) if batched else collection
        )
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
        tree = score_bundle(
            target,
            ScoreRequest.model_validate(request),
            payload,
            side_tables=tables,
            registry=ctx.registry,
            metrics=metrics,
            return_full=True,
            tree=data_tree,
            collection=collection if batched else None,
        )
    except Exception as exc:  # plots only: never fail the fit
        ctx.warning(
            "EVALUATION_VIEW_FAILED",
            "the holdout predictions for plots could not be computed",
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        )
        return None
    if isinstance(tree, xr.DataTree) and "binding" not in tree.children:
        # canonical names in, so plots can title panels with the dataset's own names
        binding = target.resolved.binding.model_dump_json()
        tree["binding"] = xr.DataTree(xr.Dataset(attrs={"json": binding}))
    return tree
