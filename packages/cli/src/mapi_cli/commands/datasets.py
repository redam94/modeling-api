"""``mapi datasets ls|upload`` (remote)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from mapi_spec.api import IngestMapping
from mapi_spec.loader import parse_text

from mapi_cli.state import EXIT_FAILURE, cli_errors, state

app = typer.Typer(help="Datasets on the API.", no_args_is_help=True)


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """List datasets."""
    st = state(ctx)
    with st.client() as api:
        items = api.datasets.list(limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "name", "ref", "latest", "updated"],
        [
            (d.id, d.name, d.ref, d.latest_version or "-", d.updated_at.strftime("%Y-%m-%d %H:%M"))
            for d in items
        ],
    )


@app.command("upload")
@cli_errors
def upload(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help=".zarr.zip, .nc, .csv or .parquet", exists=True, dir_okay=False)
    ],
    name: Annotated[str, typer.Option("--name", help="Dataset name (created when missing)")],
    mapping: Annotated[
        Path | None,
        typer.Option("--mapping", help="IngestMapping YAML/JSON (required for csv/parquet)"),
    ] = None,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the ingest run")] = True,
    timeout: Annotated[float | None, typer.Option("--timeout")] = None,
) -> None:
    """Upload a file as a new dataset version (signed PUT + sandboxed ingest run)."""
    st = state(ctx)
    out = st.output
    ingest = (
        IngestMapping.model_validate(parse_text(mapping.read_text(encoding="utf-8")))
        if mapping is not None
        else None
    )
    with st.client() as api:
        out.info(f"uploading {file} ({file.stat().st_size} bytes) to dataset {name!r}")
        version = api.datasets.upload(
            file, dataset=name, mapping=ingest, message=message, wait=wait, timeout=timeout
        )
    if out.json_mode:
        out.emit_json(version)
    else:
        out.kv(
            [
                ("ref", version.ref),
                ("status", version.status.value),
                ("sha256", version.sha256),
                ("size", version.size_bytes),
                ("ingest run", version.ingest_run_id),
            ]
        )
        out.issues(version.issues)
    if version.status.value == "failed":
        raise typer.Exit(EXIT_FAILURE)
