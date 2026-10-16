"""``translate_priors``: business priors to model-space priors, in the sandbox (M3).

The run fits the pipeline's preprocessing on the whole dataset (the scales a production
refit would learn), hands the model-stage data to the plugin's ``translate_priors`` hook
and writes::

    priors.json   PriorTranslationResult: model-space priors (settings.priors form), the
                  prior predictive in business units (ROI, share, baseline share, KPI fan),
                  coverage of every stated range and plain-language issues
    plots.json    mmm.prior_beliefs (stated range vs prior, per channel) and
                  core.prior_predictive (the KPI the priors can generate vs the data)

Nothing is fitted and no bundle is written; ``mapi_spec.calibration.apply_prior_translation``
merges the priors into the spec.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import xarray as xr
from mapi_sdk.harness import prepare_model_stage
from mapi_spec.calibration import (
    PRIOR_TRANSLATION_OUTPUT,
    PriorTranslationJob,
    PriorTranslationResult,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import parse_document
from mapi_spec.run import RunnerPhase
from mapi_spec.spec import ModelSpec

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import load_dataset, load_raw_schema, load_side_tables
from mapi_runner.worker.plots import render_for

__all__ = ["PRIOR_PLOTS", "preview_tree", "run_translate_priors"]

PRIOR_PLOTS = ("mmm.prior_beliefs", "core.prior_predictive")
"""Plots of a translation run (rendered from :func:`preview_tree`)."""


def preview_tree(preview: xr.Dataset) -> xr.DataTree:
    """The plot input of a translation: ``/prior_beliefs`` (per-channel draws and stated
    ranges), ``/prior_predictive`` (``kpi(chain, draw, time)``) and ``/observed_data``."""
    nodes: dict[str, xr.Dataset] = {"/": xr.Dataset(attrs={"source": "translate_priors"})}
    beliefs = [
        v
        for v in (
            "roi",
            "contribution_share",
            "baseline_share",
            "stated_quantity",
            "stated_low",
            "stated_high",
            "stated_credibility",
        )
        if v in preview
    ]
    if beliefs:
        nodes["/prior_beliefs"] = preview[beliefs]
    if "kpi" in preview:
        kpi = preview["kpi"]
        n = int(kpi.sizes["sample"])
        draws = (
            kpi.rename({"sample": "draw"}).expand_dims(chain=[0]).assign_coords(draw=np.arange(n))
        )
        nodes["/prior_predictive"] = xr.Dataset({"kpi": draws})
        if "kpi_observed" in preview:
            nodes["/observed_data"] = xr.Dataset({"kpi": preview["kpi_observed"]})
    return xr.DataTree.from_dict(nodes)


def _plugin_translate(
    plugin: Any, ds: xr.Dataset, settings: Any, job: PriorTranslationJob, fctx: Any
) -> Any:
    caps = type(plugin).manifest().capabilities
    hook = getattr(plugin, "translate_priors", None)
    if "business_priors" not in caps.calibration or not callable(hook):
        raise MapiError(
            f"model {plugin.id!r} does not translate business priors",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer="/business_priors",
        )
    try:
        return hook(ds, settings, job, fctx)
    except (MapiError, NotImplementedError):
        raise
    except Exception as exc:
        raise MapiError(
            f"model {plugin.id!r} failed while translating business priors",
            code=ErrorCode.USER_CODE_ERROR,
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        ) from exc


def run_translate_priors(ctx: WorkerContext, job: PriorTranslationJob) -> PriorTranslationResult:
    spec = parse_document(dict(job.spec))
    if not isinstance(spec, ModelSpec):  # pragma: no cover - parse_job checked it
        raise MapiError("translate_priors needs a ModelSpec", code=ErrorCode.SCHEMA_INVALID)
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the dataset")
    data = load_dataset(ctx)
    schema = load_raw_schema(ctx, data)
    tables = load_side_tables(ctx)
    ctx.state(RunnerPhase.PREPROCESS, "fitting the preprocessing on all the data")
    stage = prepare_model_stage(
        spec,
        data,
        tables,
        ctx.registry,
        raw_schema=schema,
        hooks=ctx.fit_hooks(window=(0.0, 0.3)),
    )
    ctx.cancel.check()
    ctx.state(RunnerPhase.SAMPLE, "translating business priors")
    fctx = stage.fit_context
    fctx.on_progress = lambda f, m: ctx.progress(0.3 + 0.6 * f, m)
    settings = stage.plugin.parse_settings(stage.settings)
    out = _plugin_translate(stage.plugin, stage.data, settings, job, fctx)
    result = PriorTranslationResult.model_validate(out.result.model_dump(mode="json"))
    ctx.write_json(PRIOR_TRANSLATION_OUTPUT, result.model_dump(mode="json", exclude_none=True))
    ctx.state(RunnerPhase.PLOTS, "rendering the prior preview")
    ctx.write_json("plots.json", render_for(ctx, PRIOR_PLOTS, [preview_tree(out.preview)]))
    for issue in result.issues[:50]:
        if issue.severity != "info":
            ctx.warning(str(issue.code), issue.message)
    if result.coverage:
        ctx.metric("prior_coverage_min", float(min(result.coverage.values())))
    ctx.progress(1.0, "priors translated", force=True)
    return result
