"""``validate`` (explicit sandboxed dry-run) and ``describe`` (catalog export).

``validate`` resolves dynamic schema effects that static validation cannot: it validates the
spec against the dataset's schema, checks the data array by array, then fits the pipeline on
the data (no model fit) and records the schema at every stage. The run succeeds whenever it
produces a report; ``validation.json`` says whether the spec is valid.

``describe`` exports what the control plane needs for static validation of specs against
this image: model manifests (schema templates, settings JSON Schemas, capabilities), step
catalog entries (settings schemas and ``SchemaEffect`` JSON) and plot recipes.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.errors import SchemaValidationError
from mapi_sdk.harness import _resolve
from mapi_sdk.pipeline import RAW, Pipeline
from mapi_sdk.schema import check_dataset, infer_schema
from mapi_spec.api import StageSchema, ValidateResponse
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.jobs import PLOT_EXAMPLES_OUTPUT
from mapi_spec.run import RunnerPhase, catalog_digest
from mapi_spec.spec import ModelSpec

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import load_dataset, load_raw_schema, load_side_tables

__all__ = ["run_describe", "run_validate"]

_DISTS = ("mapi-sdk", "mapi-steps", "mapi-plots", "mapi-pymc", "mapi-sklearn", "mapi-runner")


def run_describe(ctx: WorkerContext) -> dict[str, Any]:
    ctx.state(RunnerPhase.VALIDATE, "exporting the catalog")
    catalog = ctx.registry.catalog()
    digest = catalog_digest(catalog)
    ctx.write_output("catalog.json", catalog.model_dump_json(indent=2).encode())
    ctx.state(RunnerPhase.VALIDATE, "rendering the plot examples")
    examples = ctx.registry.plot_examples(
        on_error=lambda ref, exc: ctx.warning(
            "PLOT_EXAMPLE_FAILED",
            f"the example of plot {ref} could not be rendered; the catalog shows it without one",
            untrusted_text=f"{type(exc).__name__}: {exc}"[:2000],
        )
    )
    ctx.write_output(PLOT_EXAMPLES_OUTPUT, examples.model_dump_json().encode())
    versions: dict[str, str] = {}
    for dist in _DISTS:
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    info = {
        "catalog_digest": digest,
        "models": len(catalog.models),
        "steps": len(catalog.steps),
        "plots": len(catalog.plots),
        "plot_examples": len(examples.plots),
        "distributions": versions,
    }
    ctx.write_json("describe.json", info)
    ctx.progress(1.0, "catalog exported", force=True)
    return info


def _issues_of(exc: MapiError) -> list[Issue]:
    return list(exc.issues) or [exc.to_issue()]


def run_validate(ctx: WorkerContext, spec: ModelSpec) -> ValidateResponse:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the dataset")
    data = load_dataset(ctx)
    given = load_raw_schema(ctx, data)
    tables = load_side_tables(ctx)
    ctx.state(RunnerPhase.VALIDATE, "validating the spec against the data")
    registry = ctx.registry
    catalog = registry.catalog()
    issues: list[Issue] = []
    stages: list[StageSchema] = []
    resolved = None
    try:
        resolved, schema, found = _resolve(spec, registry, data, given)
        issues.extend(found)
    except MapiError as exc:
        issues.extend(_issues_of(exc))
        schema = given or infer_schema(data)
    if resolved is not None:
        try:
            for si in check_dataset(schema, data):
                issues.append(si.to_issue())
        except SchemaValidationError as exc:
            issues.extend(_issues_of(exc))
    if resolved is not None and not any(i.severity is Severity.ERROR for i in issues):
        ctx.state(RunnerPhase.PREPROCESS, "dry-running the pipeline")
        try:
            pipeline = Pipeline.from_spec(spec, registry, resolved)
            raw = BindingMap(resolved.binding).apply(data)
            trace = pipeline.fit(
                raw,
                side_tables=tables,
                seed=spec.model.fit.seed or 0,
                cancel=ctx.cancel,
                log=ctx.log,
                keep_stages=True,
            )
            for name in pipeline.stage_names:
                ds: xr.Dataset | None = trace.stages.get(name)
                static = resolved.stages.get(name)
                stage_schema = static
                if stage_schema is None and ds is not None:
                    stage_schema = infer_schema(ds)
                stages.append(
                    StageSchema(
                        name=name, data_schema=stage_schema, split=name == resolved.split_stage
                    )
                )
            model_schema = resolved.model.input_schema
            if model_schema is not None:
                try:
                    check_dataset(model_schema, trace.data, check_values=False, check_coords=False)
                except SchemaValidationError as exc:
                    issues.append(
                        Issue(
                            code=ErrorCode.SCHEMA_INVALID,
                            pointer="/model",
                            message="the output of the pipeline does not match the input schema "
                            "of the model: " + exc.detail[:1500],
                        )
                    )
        except MapiError as exc:
            issues.extend(_issues_of(exc))
    if not stages and resolved is not None:
        stages = [
            StageSchema(name=n, data_schema=s, split=n == resolved.split_stage)
            for n, s in resolved.stages.items()
        ]
    if not stages:
        stages = [StageSchema(name=RAW, data_schema=schema, split=False)]
    valid = not any(i.severity is Severity.ERROR for i in issues)
    report = ValidateResponse(
        valid=valid,
        kind="ModelSpec",
        hash=resolved.spec_hash if resolved is not None else None,
        issues=issues,
        stages=stages,
        resolved=resolved,
        catalog_digest=catalog_digest(catalog),
    )
    ctx.write_output("validation.json", report.model_dump_json(indent=2).encode())
    for issue in issues[:50]:
        if issue.severity is Severity.ERROR:
            ctx.warning(
                "SPEC_INVALID", f"{issue.code.value} at {issue.pointer or '/'}: {issue.message}"
            )
    ctx.progress(1.0, "validation finished", force=True)
    return report
