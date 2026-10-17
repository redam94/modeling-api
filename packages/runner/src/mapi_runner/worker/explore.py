"""``explore`` runs and the explorer views written by ingest and fit runs (M3.5).

Every section is written to ``ui/explore/<section>.json`` (a :mod:`mapi_spec.views`
document) with its Vega-Lite plots at ``ui/explore/plots/<section>.json`` (platform recipes
from :mod:`mapi_plots.explorer`), and ``ui/explore/index.json`` lists what was produced
and why a section is missing (``skipped``: it does not apply; ``error``: it failed).

* **ingest** writes the cheap Data Explorer sections
  (:data:`~mapi_runner.worker.explore_data.CHEAP_SECTIONS`);
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
    EXPLORE_INDEX,
    EXPLORE_PREFIX,
    ExplorerIndex,
    ExplorerSectionRef,
    explore_view_name,
)
from pydantic import BaseModel

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.explore_data import (
    CHEAP_SECTIONS,
    DATA_SECTION_FUNCS,
    DataContext,
    SectionSkipped,
    data_context,
)
from mapi_runner.worker.explore_model import FIT_SECTIONS, MODEL_SECTION_FUNCS, ModelContext
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
) -> ExplorerIndex:
    """Compute ``sections`` with ``funcs`` and write each view, its plots and the index."""
    refs: list[ExplorerSectionRef] = []
    lo, hi = progress
    for i, section in enumerate(sections):
        ctx.cancel.check()
        ctx.progress(lo + (hi - lo) * i / max(1, len(sections)), f"explorer: {section}")
        name = explore_view_name(section)
        try:
            view = funcs[section](context)
            body = view.model_dump(mode="json", exclude_none=True)
            ctx.write_json(name, body)
            plots = explorer_plots(body)
            extra: dict[str, Any] = {}
            if plots:
                ctx.write_json(explore_plots_name(section), plots)
                extra["plots"] = explore_plots_name(section)
            refs.append(ExplorerSectionRef(section=section, view=name, **extra))
        except SectionSkipped as exc:
            refs.append(
                ExplorerSectionRef(
                    section=section,
                    view=name,
                    status="skipped",
                    message_plain=f"Not available: {exc}."[:500],
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
    index = ExplorerIndex.model_validate({"target": target, "ref": ref[:512], "sections": refs})
    ctx.write_json(EXPLORE_INDEX, index.model_dump(mode="json", exclude_none=True))
    return index


def write_data_explorer(
    ctx: WorkerContext,
    ds: xr.Dataset,
    schema: DataSchema | None,
    *,
    ref: str,
    sections: Sequence[str] = CHEAP_SECTIONS,
    req: DataExploreRequest | None = None,
) -> ExplorerIndex | None:
    """The Data Explorer views of a dataset (never raises: a failure is a run warning)."""
    try:
        context = data_context(ds, schema, req)
        return write_sections(ctx, DATA_SECTION_FUNCS, sections, context, target="dataset", ref=ref)
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


def run_explore(ctx: WorkerContext, job: ExploreJob) -> ExplorerIndex:
    """An ``explore`` run (see the module docstring)."""
    from mapi_runner.worker.data import (
        load_bundle,
        load_dataset,
        load_raw_schema,
        load_side_tables,
    )

    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the inputs")
    if job.target == "dataset":
        assert job.data is not None
        ds = load_dataset(ctx)
        schema = load_raw_schema(ctx, ds)
        ctx.state(RunnerPhase.FIT, f"computing {len(job.data.sections)} explorer sections")
        context = data_context(ds, schema, job.data)
        index = write_sections(
            ctx, DATA_SECTION_FUNCS, list(job.data.sections), context, target="dataset", ref=job.ref
        )
        ctx.progress(1.0, "explorer views ready", force=True)
        return index
    assert job.model is not None
    req: ModelExploreRequest = job.model
    bundle = load_bundle(ctx)
    data = load_dataset(ctx)
    tables = load_side_tables(ctx)
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
        bundle, ctx.registry, _BACKTEST_BUDGET, side_tables=tables, cancel=ctx.cancel
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
    )
    ctx.state(RunnerPhase.FIT, f"computing {len(req.sections)} explorer sections")
    index = write_sections(
        ctx, MODEL_SECTION_FUNCS, list(req.sections), mcontext, target="model", ref=job.ref
    )
    ctx.progress(1.0, "explorer views ready", force=True)
    return index
