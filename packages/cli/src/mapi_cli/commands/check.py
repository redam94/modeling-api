"""``mapi check spec.yaml [--data file]``: static validation without running anything.

With ``--data`` (a ``.zarr.zip``) or ``--schema`` (a DataSchema JSON/YAML) the full static
validation of the control plane runs locally (binding, schema propagation through every
step, split placement and leakage, model schema and capabilities) against the catalog of
the locally installed plugins. Without data only the data-independent checks run
(references, step/model/fit settings, plots, derived quantities).
``--remote URL`` sends the document to ``POST /v1/specs:validate`` instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, has_errors, json_pointer
from mapi_spec.loader import load_model_spec
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_settings, validate_spec

from mapi_cli.output import Output
from mapi_cli.state import EXIT_FAILURE, cli_errors, state

__all__ = ["check", "static_check"]


def static_check(spec: ModelSpec, catalog: Catalog) -> list[Issue]:
    """The data-independent part of ``validate_spec``."""
    issues: list[Issue] = []
    for i, call in enumerate(spec.pipeline):
        ptr = json_pointer("pipeline", i)
        entry = catalog.resolve_step(call.use)
        if entry is None:
            known = catalog.versions("step", call.use.id)
            issues.append(
                Issue(
                    code=ErrorCode.REF_UNRESOLVED,
                    pointer=f"{ptr}/use",
                    message=(
                        f"no version of {call.use.id!r} satisfies {str(call.use.range)!r} "
                        f"(available: {', '.join(known)})"
                        if known
                        else f"unknown step {call.use.id!r}"
                    ),
                )
            )
            continue
        issues += validate_settings(entry.settings_schema, call.with_, f"{ptr}/with")
    entry_m = catalog.resolve_model(spec.model.use)
    if entry_m is None:
        known = catalog.versions("model", spec.model.use.id)
        issues.append(
            Issue(
                code=ErrorCode.REF_UNRESOLVED,
                pointer="/model/use",
                message=(
                    f"no version of {spec.model.use.id!r} satisfies "
                    f"{str(spec.model.use.range)!r} (available: {', '.join(known)})"
                    if known
                    else f"unknown model {spec.model.use.id!r}"
                ),
            )
        )
    else:
        man = entry_m.manifest
        issues += validate_settings(
            man.settings_schema, spec.model.plugin_settings, "/model/settings"
        )
        fit_json = spec.model.fit.model_dump(mode="json", exclude_defaults=True)
        issues += validate_settings(man.fit_schema, fit_json, "/model/fit")
        derived = {d.name for d in man.derived}
        for j, name in enumerate(spec.outputs.derived):
            if name not in derived:
                issues.append(
                    Issue(
                        code=ErrorCode.REF_UNRESOLVED,
                        pointer=json_pointer("outputs", "derived", j),
                        message=f"model {man.id!r} does not declare derived quantity {name!r}",
                    )
                )
    for j, plot in enumerate(spec.outputs.plots):
        if catalog.resolve_plot(plot) is None:
            issues.append(
                Issue(
                    code=ErrorCode.REF_UNRESOLVED,
                    pointer=json_pointer("outputs", "plots", j),
                    message=f"unknown plot {plot.id!r}",
                )
            )
    issues.append(
        Issue(
            code=ErrorCode.SCHEMA_DYNAMIC,
            severity=Severity.INFO,
            message="data-dependent checks were skipped (binding, schema propagation, split, "
            "model schema); pass --data or --schema to run them",
        )
    )
    return issues


def _report(
    out: Output,
    path: Path,
    mode: str,
    issues: list[Issue],
    stages: list[dict[str, Any]],
    show_stages: bool,
) -> bool:
    valid = not has_errors(issues)
    if out.json_mode:
        out.emit_json(
            {"spec": str(path), "valid": valid, "mode": mode, "issues": issues, "stages": stages}
        )
        return valid
    out.issues(issues)
    if show_stages and stages:
        rows = []
        for st in stages:
            schema = st.get("data_schema")
            if schema is None:
                rows.append((st["name"], "(dynamic)", ""))
                continue
            dims = ", ".join(f"{d}" for d in schema.get("dims", {}))
            vars_ = ", ".join(schema.get("vars", {}))
            rows.append((st["name"] + (" ← split" if st.get("split") else ""), dims, vars_))
        out.table(["stage", "dims", "vars"], rows, title="Schema by stage")
    n_err = sum(i.severity is Severity.ERROR for i in issues)
    n_warn = sum(i.severity is Severity.WARNING for i in issues)
    if valid:
        out.success(f"{path}: valid ({mode} checks, {n_warn} warning(s))")
    else:
        out.err.print(f"{path}: invalid ({n_err} error(s), {n_warn} warning(s))", markup=False)
    return valid


@cli_errors
def check(
    ctx: typer.Context,
    spec_file: Annotated[
        Path, typer.Argument(help="ModelSpec YAML/JSON", exists=True, dir_okay=False)
    ],
    data: Annotated[
        Path | None, typer.Option("--data", help="Dataset (.zarr.zip) giving the raw schema")
    ] = None,
    schema: Annotated[
        Path | None, typer.Option("--schema", help="Raw DataSchema (JSON/YAML) instead of data")
    ] = None,
    stages: Annotated[
        bool, typer.Option("--stages", help="Show the schema at every stage")
    ] = False,
    remote: Annotated[
        str | None,
        typer.Option("--remote", help="Validate on the API at this URL ('@' = configured URL)"),
    ] = None,
    dataset: Annotated[
        str | None, typer.Option("--dataset", help="(remote) dataset ref overriding data.dataset")
    ] = None,
) -> None:
    """Statically validate a spec (exit status 1 when it has errors)."""
    st = state(ctx)
    out = st.output
    text = spec_file.read_text(encoding="utf-8")
    if remote is not None:
        if data is not None or schema is not None:
            raise MapiError(
                "--data/--schema are local options; with --remote pass --dataset <ref>",
                code=ErrorCode.SCHEMA_INVALID,
            )
        with st.client(remote) as api:
            res = api.specs.validate(text, dataset=dataset)
        stage_rows = [s.model_dump(mode="json") for s in res.stages]
        if not _report(out, spec_file, "remote", list(res.issues), stage_rows, stages):
            raise typer.Exit(EXIT_FAILURE)
        return
    if dataset is not None:
        raise MapiError("--dataset only applies with --remote", code=ErrorCode.SCHEMA_INVALID)
    try:
        spec = load_model_spec(text)
    except MapiError as exc:
        _report(out, spec_file, "parse", exc.issues or [exc.to_issue()], [], False)
        raise typer.Exit(EXIT_FAILURE) from None

    from mapi_cli.local import load_registry, raw_schema, read_dataset, read_schema_file

    registry, load_errors = load_registry()
    for err in load_errors:
        out.warn(f"plugin failed to load: {err}")
    catalog = registry.catalog()
    if data is not None or schema is not None:
        raw = (
            raw_schema(read_dataset(data), schema) if data is not None else read_schema_file(schema)  # type: ignore[arg-type]
        )
        result = validate_spec(spec, catalog, raw)
        split = result.resolved.split_stage if result.resolved is not None else None
        stage_rows = [
            {
                "name": s.name,
                "data_schema": None if s.schema is None else s.schema.model_dump(mode="json"),
                "split": s.name == split,
            }
            for s in result.stages
        ]
        ok = _report(out, spec_file, "full", result.issues, stage_rows, stages)
    else:
        ok = _report(out, spec_file, "static", static_check(spec, catalog), [], False)
    if not ok:
        raise typer.Exit(EXIT_FAILURE)
