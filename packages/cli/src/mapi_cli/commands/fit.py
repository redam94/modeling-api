"""``mapi fit spec.yaml --data d.zarr.zip [--remote URL]``.

Locally the spec is fitted in this process with the in-process executor (isolation NONE,
for your own code and data) and the bundle is written to ``--out``. With ``--remote`` the
data (and side tables) are uploaded as new versions of the datasets the spec references,
the spec is created or revised by ``metadata.name``, and a ``fit`` run is submitted and
followed over SSE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_client import (
    Client,
    RunEnd,
    RunEventFrame,
    RunLiveFrame,
    RunSnapshot,
    RunTrackingFrame,
)
from mapi_spec.api import Run
from mapi_spec.cost import RunCost
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.loader import load_model_spec
from mapi_spec.refs import DatasetRef
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import ModelSpec
from rich.text import Text

from mapi_cli.output import Output, money, safe
from mapi_cli.progress import EventRenderer
from mapi_cli.state import EXIT_FAILURE, AppState, cli_errors, state

__all__ = ["cost_rows", "cost_text", "fit", "follow_run", "report_run"]


def follow_run(api: Client, out: Output, run: Run, *, title: str) -> Run:
    """Follow a run's event stream until it ends; returns the final run.

    Params and tags never travel on the stream: a ``tracking`` frame only says that they
    changed. The params and tags that changed are then read and printed (best effort).
    """
    final = run
    with EventRenderer(out, title) as renderer:
        seen = _TrackedValues(api, run)
        for item in api.runs.stream_events(run.id):
            if isinstance(item, RunSnapshot):
                final = item.run
            elif isinstance(item, RunEventFrame):
                renderer.handle(item.event)
            elif isinstance(item, RunLiveFrame):
                renderer.handle(item)
            elif isinstance(item, RunTrackingFrame):
                renderer.tracking(item.frame)
                if item.frame.params and renderer.enabled:
                    renderer.params(seen.new_params())
                if item.frame.tags and renderer.enabled:
                    renderer.tags(seen.new_tags())
            elif isinstance(item, RunEnd):
                final = item.run
    return final


class _TrackedValues:
    """The params and tags a watch already showed. The tags of the start are known; the
    first params change shows every param (the watch of a new run sees them all anyway)."""

    def __init__(self, api: Client, run: Run) -> None:
        self.api = api
        self.run_id = run.id
        self.tags: dict[str, str] = dict(run.tags)
        self.params: dict[str, str] = {}

    def new_params(self) -> dict[str, str]:
        try:
            now = {p.key: p.value for p in self.api.runs.params(self.run_id)}
        except MapiError:  # printing them is best effort: the watch goes on
            return {}
        new = {k: v for k, v in now.items() if self.params.get(k) != v}
        self.params = now
        return new

    def new_tags(self) -> dict[str, str]:
        try:
            now = dict(self.api.runs.get(self.run_id).tags)
        except MapiError:
            return {}
        new = {k: v for k, v in now.items() if self.tags.get(k) != v}
        self.tags = now
        return new


def report_run(out: Output, run: Run) -> None:
    if out.json_mode:
        out.emit_json(run)
        return
    out.kv(
        [
            ("run", run.id),
            ("name", run.name),
            ("state", safe(run.state.value, "green" if run.state.value == "succeeded" else "red")),
            ("kind", run.kind.value),
            ("attempt", run.attempt),
            ("model version", run.output_model_version_id),
            ("failure", run.failure_class.value if run.failure_class else None),
            ("tags", ", ".join(f"{k}={v}" for k, v in sorted(run.tags.items()))),
            *cost_rows(run.cost),
        ]
    )
    if run.problem is not None:
        out.error(_problem_error(run))


def cost_text(cost: RunCost) -> str:
    """``0.031200 USD`` (``~`` prefix while estimated); ``-`` when not metered (v0.2)."""
    if cost.amount_micros is not None:
        text = money(cost.amount_micros, cost.currency)
    elif cost.usd is not None:
        text = f"{cost.usd:.6f} USD"
    else:
        return "-"
    return f"~{text}" if cost.estimated else text


def cost_rows(cost: RunCost) -> list[tuple[str, object]]:
    """Key/value rows describing a run's cost (empty when the server does not meter it)."""
    if cost.amount_micros is None and cost.usd is None:
        return []
    usage = None
    if cost.vcpu_seconds is not None:
        usage = f"{cost.vcpu_seconds:,.0f} vCPU-s"
        if cost.gib_seconds is not None:
            usage += f", {cost.gib_seconds:,.0f} GiB-s"
    label = "cost (estimated)" if cost.estimated else "cost"
    return [
        (label, Text(cost_text(cost).lstrip("~"), style="bold")),
        ("compute", usage),
        ("estimate", money(cost.estimate_micros, cost.currency) if cost.estimate_micros else None),
        ("attempts billed", cost.attempts_billed or None),
        ("price table", cost.price_version),
    ]


def _problem_error(run: Run) -> MapiError:
    from mapi_spec.errors import error_from_problem

    assert run.problem is not None
    return error_from_problem(run.problem)


def _fit_remote(
    st: AppState,
    url: str,
    spec_file: Path,
    text: str,
    spec: ModelSpec,
    data: Path | None,
    tables: dict[str, Path],
    resource_class: ResourceClass | None,
    wait: bool,
) -> None:
    out = st.output
    unknown = sorted(set(tables) - set(spec.data.side_tables))
    if unknown:
        raise MapiError(
            f"side table {unknown[0]!r} is not declared under data.side_tables",
            code=ErrorCode.SCHEMA_INVALID,
        )
    with st.client(url) as api:
        uploads: list[tuple[Path, DatasetRef]] = []
        if data is not None:
            uploads.append((data, spec.data.dataset))
        uploads += [(path, spec.data.side_tables[n].ref) for n, path in tables.items()]
        for path, ref in uploads:
            ds_name = ref.name
            if not ref.is_latest:
                out.warn(
                    f"{ref} pins a version: the spec will not read the version uploaded now "
                    "(use @latest to fit on new uploads)"
                )
            out.info(f"uploading {path} as a new version of dataset {ds_name!r}")
            version = api.datasets.upload(path, dataset=ds_name)
            out.info(f"  → {version.ref} ({version.status.value})")
        rev = api.specs.upsert(text, message=f"mapi fit {spec_file.name}")
        if not rev.valid:
            out.issues(rev.issues, title=f"{rev.ref} is invalid")
            raise typer.Exit(EXIT_FAILURE)
        out.info(f"spec {rev.ref}")
        run = api.runs.fit(rev.spec_id, rev.revision, resource_class=resource_class)
        out.info(f"submitted run {run.id}")
        if wait:
            run = follow_run(api, out, run, title=f"fit {spec.metadata.name}")
    report_run(out, run)
    if run.state.terminal and run.state.value not in ("succeeded", "partial"):
        raise typer.Exit(EXIT_FAILURE)


def _fit_local(
    out: Output,
    spec_file: Path,
    text: str,
    spec: ModelSpec,
    data: Path,
    tables: dict[str, Path],
    schema: Path | None,
    bundle_dir: Path,
    timeout: float | None,
) -> None:
    from mapi_cli.local import file_ref, load_registry, raw_schema, read_dataset, run_inproc

    missing = sorted(set(spec.data.side_tables) - set(tables))
    if missing:
        raise MapiError(
            f"side table {missing[0]!r} is needed: pass --side-table {missing[0]}=PATH",
            code=ErrorCode.SIDE_TABLE_COVERAGE,
        )
    for p in [data, *tables.values()]:
        if not p.is_file():
            raise FileNotFoundError(f"{p} is not a file")
    registry, load_errors = load_registry()
    for err in load_errors:
        out.warn(f"plugin failed to load: {err}")
    payload: dict[str, Any] = {
        "spec": text,
        "data": file_ref(data),
        "side_tables": {name: file_ref(p) for name, p in tables.items()},
    }
    if schema is not None:
        payload["raw_schema"] = raw_schema(read_dataset(data), schema).model_dump(mode="json")
    with EventRenderer(out, f"fit {spec.metadata.name}") as renderer:
        result = run_inproc(
            RunKind.FIT, payload, registry=registry, on_event=renderer.handle, timeout=timeout
        )
    if not result.ok:
        raise result.error()
    bundle = result.result
    manifest = bundle.write(bundle_dir)
    metrics = dict(bundle.metrics.values)
    summary = {
        "run_id": result.run_id,
        "state": result.status.state.value,
        "bundle": str(bundle_dir),
        "bundle_id": manifest.bundle_id,
        "spec_hash": manifest.spec_hash,
        "metrics": metrics,
    }
    if out.json_mode:
        out.emit_json(summary)
        return
    out.success(f"fitted {spec_file} → {bundle_dir}")
    if metrics:
        out.table(
            ["metric", "value"],
            [(k, f"{v:.4g}" if v is not None else "-") for k, v in sorted(metrics.items())],
        )


@cli_errors
def fit(
    ctx: typer.Context,
    spec_file: Annotated[Path, typer.Argument(help="ModelSpec YAML", exists=True, dir_okay=False)],
    data: Annotated[
        Path | None, typer.Option("--data", help="Fit data (.zarr.zip); required locally")
    ] = None,
    side_table: Annotated[
        list[str] | None,
        typer.Option("--side-table", help="NAME=PATH for each data.side_tables entry"),
    ] = None,
    schema: Annotated[
        Path | None, typer.Option("--schema", help="Raw DataSchema (JSON/YAML) of --data")
    ] = None,
    out_dir: Annotated[
        Path | None, typer.Option("--out", help="Bundle directory (default: bundles/<name>)")
    ] = None,
    remote: Annotated[
        str | None,
        typer.Option("--remote", help="Fit on the API at this URL ('@' = configured URL)"),
    ] = None,
    resource_class: Annotated[
        ResourceClass | None, typer.Option("--resource-class", help="(remote) S, M, L or XL")
    ] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="(remote) follow the run")] = True,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="(local) give up after N seconds")
    ] = None,
) -> None:
    """Fit a spec locally (in-process) or on the API (--remote)."""
    from mapi_cli.local import parse_assignments

    st = state(ctx)
    text = spec_file.read_text(encoding="utf-8")
    spec = load_model_spec(text)
    tables = parse_assignments(side_table or [])
    if remote is not None:
        _fit_remote(st, remote, spec_file, text, spec, data, tables, resource_class, wait)
        return
    if data is None:
        raise MapiError("local fits need --data (a .zarr.zip)", code=ErrorCode.SCHEMA_INVALID)
    bundle_dir = out_dir or Path("bundles") / spec.metadata.name
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise FileExistsError(f"{bundle_dir} is not empty (choose another --out)")
    _fit_local(st.output, spec_file, text, spec, data, tables, schema, bundle_dir, timeout)
