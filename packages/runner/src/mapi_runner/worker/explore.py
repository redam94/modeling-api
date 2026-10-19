"""``explore`` runs and the explorer views written by ingest and fit runs (M3.5).

Every section is written to ``ui/explore/<section>.json`` (a :mod:`mapi_spec.views`
document) with its Vega-Lite plots at ``ui/explore/plots/<section>.json`` (platform recipes
from :mod:`mapi_plots.explorer`), and ``ui/explore/index.json`` lists what was produced
and why a section is missing (``skipped``: it does not apply; ``error``: it failed).

* **ingest** writes the cheap Data Explorer sections, ``structure`` and the picture
  sections that apply (:func:`~mapi_runner.worker.explore_data.ingest_sections`), and lists
  every section that does not apply as ``skipped`` with ``reason: not_applicable``;
* **fit** writes the Model Explorer's
  :data:`~mapi_runner.worker.explore_model.FIT_SECTIONS`;
* an **explore** run computes whatever an :class:`~mapi_spec.explore.ExploreJob` asks for
  (STL, stationarity, cross-correlations and VIF on datasets; forecasts, backtests and
  comparisons on models).

A failing section never fails an ingest or a fit: it is recorded as ``error`` in the index
and as a run warning.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import xarray as xr
from mapi_plots.explorer import explorer_plots
from mapi_sdk.bundle import Bundle
from mapi_spec.explore import DataExploreRequest, ExploreJob, ModelExploreRequest
from mapi_spec.governance import RefitBudget
from mapi_spec.jobs import (
    INPUT_COMPARE_PREFIX,
    INPUT_EVALUATION,
    INPUT_EVALUATION_MANIFEST,
)
from mapi_spec.run import RunnerPhase
from mapi_spec.schema import DataSchema
from mapi_spec.views import (
    DATA_SECTIONS,
    EXPLORE_INDEX,
    EXPLORE_PREFIX,
    SCIENCE_SECTIONS,
    ExplorerIndex,
    ExplorerSectionRef,
    explore_view_name,
)
from pydantic import BaseModel

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.explore_data import (
    DATA_SECTION_FUNCS,
    DataContext,
    SectionSkipped,
    data_context,
    ingest_sections,
)
from mapi_runner.worker.explore_model import FIT_SECTIONS, MODEL_SECTION_FUNCS, ModelContext
from mapi_runner.worker.explore_science import INGEST_IMAGE_CELLS, image_cells, science_plots
from mapi_runner.worker.refit import Refitter

__all__ = [
    "EXPLORE_PLOTS_PREFIX",
    "explore_plots_name",
    "run_explore",
    "write_data_explorer",
    "write_model_explorer",
    "write_sections",
]

EXPLORE_PLOTS_PREFIX: Final = EXPLORE_PREFIX + "plots/"
_BACKTEST_BUDGET: Final = RefitBudget(max_refits=12, draws=200, tune=200, chains=2)


def explore_plots_name(section: str) -> str:
    """``ui/explore/plots/<section>.json``: the section's PlotSpecs."""
    return f"{EXPLORE_PLOTS_PREFIX}{section}.json"


def write_sections(
    ctx: WorkerContext,
    funcs: Mapping[str, Callable[[Any], BaseModel]],
    sections: Sequence[str],
    context: DataContext | ModelContext,
    *,
    target: str,
    ref: str,
    progress: tuple[float, float] = (0.0, 1.0),
    prefix: str = EXPLORE_PREFIX,
    node: str | None = None,
) -> ExplorerIndex:
    """Compute ``sections`` with ``funcs`` and write each view, its plots and the index.

    M4: ``prefix`` places them elsewhere (``ui/explore/nodes/<slug>/`` for a node of a tree
    dataset) and ``node`` names the node the views describe."""
    refs: list[ExplorerSectionRef] = []
    lo, hi = progress
    data = context if isinstance(context, DataContext) else None
    not_applicable = data.shapes.not_applicable if data is not None else {}
    for i, section in enumerate(sections):
        ctx.cancel.check()
        ctx.progress(lo + (hi - lo) * i / max(1, len(sections)), f"explorer: {section}")
        name = explore_view_name(section)
        plots_name = explore_plots_name(section)
        if prefix != EXPLORE_PREFIX:
            name = prefix + name.removeprefix(EXPLORE_PREFIX)
            plots_name = prefix + plots_name.removeprefix(EXPLORE_PREFIX)
        if section in not_applicable:
            refs.append(
                ExplorerSectionRef(
                    section=section,
                    view=name,
                    status="skipped",
                    message_plain=f"Not available: {not_applicable[section]}."[:500],
                    reason="not_applicable",
                )
            )
            continue
        try:
            view = funcs[section](context)
            body = view.model_dump(mode="json", exclude_none=True)
            if node is not None:
                body["node"] = node
            ctx.write_json(name, body)
            plots = explorer_plots(body)
            if data is not None and section in SCIENCE_SECTIONS:
                plots += science_plots(section, data.ds, data.shapes)
            extra: dict[str, Any] = {}
            if plots:
                ctx.write_json(plots_name, plots)
                extra["plots"] = plots_name
            refs.append(ExplorerSectionRef(section=section, view=name, **extra))
        except SectionSkipped as exc:
            refs.append(
                ExplorerSectionRef(
                    section=section,
                    view=name,
                    status="skipped",
                    message_plain=f"Not available: {exc}."[:500],
                    reason=exc.reason,
                )
            )
        except Exception as exc:
            if getattr(ctx.cancel, "cancelled", False):
                raise
            ctx.warning(
                "EXPLORE_SECTION_FAILED",
                f"the server cannot calculate the {section} view",
                f"{type(exc).__name__}: {exc}"[:8000],
            )
            refs.append(
                ExplorerSectionRef(
                    section=section,
                    view=name,
                    status="error",
                    message_plain="The server cannot calculate this view for this data or model.",
                )
            )
    doc: dict[str, Any] = {"target": target, "ref": ref[:512], "sections": refs}
    if node is not None:
        doc["node"] = node
    if data is not None:
        doc["shapes"] = data.shapes.kinds[:64]
        doc["order"] = list(data.shapes.recommended)[:32]
    index = ExplorerIndex.model_validate(doc)
    index_name = EXPLORE_INDEX if prefix == EXPLORE_PREFIX else f"{prefix}index.json"
    ctx.write_json(index_name, index.model_dump(mode="json", exclude_none=True))
    return index


def write_data_explorer(
    ctx: WorkerContext,
    ds: xr.Dataset,
    schema: DataSchema | None,
    *,
    ref: str,
    sections: Sequence[str] | None = None,
    req: DataExploreRequest | None = None,
    prefix: str = EXPLORE_PREFIX,
    node: str | None = None,
) -> ExplorerIndex | None:
    """The Data Explorer views of a dataset (never raises: a failure is a run warning).

    ``sections`` defaults to the ingest set: :func:`ingest_sections` of the detected
    structure, plus every section that does not apply (listed as skipped, in
    :data:`~mapi_spec.views.DATA_SECTIONS` order). M4: ``prefix``/``node`` write the views of
    one node of a tree dataset."""
    try:
        context = data_context(ds, schema, req)
        if sections is None:
            wanted = set(ingest_sections(context.shapes)) | set(context.shapes.not_applicable)
            cells = image_cells(context.shapes, ds)
            if cells > INGEST_IMAGE_CELLS:
                # a large movie: the ingest leaves "images" out, an explore run computes it
                wanted.discard("images")
                ctx.warning(
                    "EXPLORE_DEFERRED",
                    "the images view is computed on demand",
                    f"the images have {cells:,} cells (the ingest computes "
                    f"{INGEST_IMAGE_CELLS:,} or fewer). Run the deeper checks to compute it.",
                )
            sections = [s for s in DATA_SECTIONS if s in wanted]
        return write_sections(
            ctx,
            DATA_SECTION_FUNCS,
            sections,
            context,
            target="dataset",
            ref=ref,
            prefix=prefix,
            node=node,
        )
    except Exception as exc:
        ctx.warning(
            "EXPLORE_FAILED",
            "the Data Explorer views were not written",
            f"{type(exc).__name__}: {exc}"[:8000],
        )
        return None


def write_model_explorer(
    ctx: WorkerContext,
    bundle: Bundle,
    data: xr.Dataset,
    *,
    ref: str,
    side_tables: Mapping[str, xr.Dataset] | None = None,
    sections: Sequence[str] = FIT_SECTIONS,
) -> ExplorerIndex | None:
    """The fit-time Model Explorer views (never raises: a failure is a run warning)."""
    try:
        context = ModelContext(
            bundle, data, ctx.registry, ref=ref, side_tables=dict(side_tables or {})
        )
        return write_sections(ctx, MODEL_SECTION_FUNCS, sections, context, target="model", ref=ref)
    except Exception as exc:
        ctx.warning(
            "EXPLORE_FAILED",
            "the Model Explorer views were not written",
            f"{type(exc).__name__}: {exc}"[:8000],
        )
        return None


def _explore_node(
    ctx: WorkerContext, data: xr.Dataset | xr.DataTree, node: str | None
) -> tuple[xr.Dataset, DataSchema | None, str | None]:
    """The dataset a Data Explorer request explores: a plain dataset as is, or (M4) one node
    of a tree (``node``, default: the root, else the first node with variables)."""
    from mapi_sdk.tree import node_dataset
    from mapi_spec.errors import ErrorCode, MapiError
    from mapi_spec.tree import ROOT

    from mapi_runner.worker.data import load_tree_schema

    if isinstance(data, xr.Dataset):
        if node not in (None, ROOT):
            raise MapiError(
                f"the dataset is not a tree: it has no node {node!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/node",
            )
        return data, None, None
    schema = load_tree_schema(ctx, data)
    assert schema is not None
    path = node or schema.default_node()
    if path not in schema.nodes:
        raise MapiError(
            f"the dataset has no node {path!r} (nodes: {', '.join(list(schema.nodes)[:20])})",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/data/node",
        )
    return node_dataset(data, path), schema.nodes[path], path


def run_explore(ctx: WorkerContext, job: ExploreJob) -> ExplorerIndex:
    """An ``explore`` run (see the module docstring)."""
    from mapi_runner.worker.data import (
        load_bundle,
        load_data_and_tree,
        load_dataset,
        load_raw_schema,
        load_side_tables,
    )

    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the inputs")
    if job.target == "dataset":
        assert job.data is not None
        ds, schema, node = _explore_node(ctx, load_dataset(ctx), job.data.node)
        if node is None:
            schema = load_raw_schema(ctx, ds)
        ctx.state(RunnerPhase.FIT, f"computing {len(job.data.sections)} explorer sections")
        context = data_context(ds, schema, job.data)
        index = write_sections(
            ctx,
            DATA_SECTION_FUNCS,
            list(job.data.sections),
            context,
            target="dataset",
            ref=job.ref,
            node=node,
        )
        ctx.progress(1.0, "explorer views ready", force=True)
        return index
    assert job.model is not None
    req: ModelExploreRequest = job.model
    bundle = load_bundle(ctx)
    # M4: a tree's nodes per data.nodes, and the tree itself for a tree-input model
    data, tree = load_data_and_tree(ctx, bundle.spec.data)
    tables = load_side_tables(ctx, bundle.spec)
    evaluation = (
        load_bundle(ctx, INPUT_EVALUATION, INPUT_EVALUATION_MANIFEST)
        if ctx.has_input(INPUT_EVALUATION)
        else None
    )
    compare: list[tuple[str, Bundle]] = []
    for k, ref in enumerate(req.compare_with):
        name = f"{INPUT_COMPARE_PREFIX}{k}"
        if ctx.has_input(name):
            compare.append((ref, load_bundle(ctx, name, f"{name}.manifest")))
    refitter = Refitter(
        bundle, ctx.registry, _BACKTEST_BUDGET, side_tables=tables, cancel=ctx.cancel, tree=tree
    )
    mcontext = ModelContext(
        bundle,
        data,
        ctx.registry,
        req,
        ref=job.ref,
        side_tables=tables,
        compare=compare,
        evaluation=evaluation,
        refitter=refitter,
        tree=tree,
    )
    ctx.state(RunnerPhase.FIT, f"computing {len(req.sections)} explorer sections")
    index = write_sections(
        ctx, MODEL_SECTION_FUNCS, list(req.sections), mcontext, target="model", ref=job.ref
    )
    ctx.progress(1.0, "explorer views ready", force=True)
    return index
