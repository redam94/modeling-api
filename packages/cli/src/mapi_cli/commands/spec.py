"""``mapi spec upgrade``: migrate a document to the current ``apiVersion``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import dump_yaml, parse_document, parse_text, upgrade
from mapi_spec.spec import API_VERSION

from mapi_cli.state import EXIT_FAILURE, cli_errors, state

app = typer.Typer(help="Spec document tools.", no_args_is_help=True)


@app.command("upgrade")
@cli_errors
def upgrade_cmd(
    ctx: typer.Context,
    spec_file: Annotated[Path, typer.Argument(help="Spec YAML/JSON", exists=True, dir_okay=False)],
    to: Annotated[str, typer.Option("--to", help="Target apiVersion")] = API_VERSION,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write here (default: stdout)")
    ] = None,
    in_place: Annotated[bool, typer.Option("--in-place", help="Rewrite the file")] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Only report whether an upgrade is needed (exit 1)")
    ] = False,
) -> None:
    """Upgrade a document through the registered converters (N-1 → N).

    The result is re-validated as a typed document. Comments are not preserved when the
    document changes (it is re-serialised as canonical YAML).
    """
    out = state(ctx).output
    if in_place and output is not None:
        raise MapiError("use either --in-place or --output", code=ErrorCode.SCHEMA_INVALID)
    text = spec_file.read_text(encoding="utf-8")
    raw = parse_text(text)
    if not isinstance(raw, dict):
        raise MapiError("a document must be a mapping", code=ErrorCode.SCHEMA_INVALID)
    source = raw.get("apiVersion")
    upgraded = upgrade(raw, target=to, accepted=(str(source), to))
    changed = upgraded != raw
    parse_document(upgraded, accepted=(to,))  # the result must be a valid document
    if check:
        if out.json_mode:
            out.emit_json({"spec": str(spec_file), "from": source, "to": to, "changed": changed})
        elif changed:
            out.err.print(f"{spec_file}: {source} → {to} upgrade available", markup=False)
        else:
            out.success(f"{spec_file}: already {to}")
        if changed:
            raise typer.Exit(EXIT_FAILURE)
        return
    new_text = dump_yaml(upgraded) if changed else text
    dest = spec_file if in_place else output
    if dest is None:
        if out.json_mode:
            out.emit_json({"from": source, "to": to, "changed": changed, "document": upgraded})
        else:
            typer.echo(new_text, nl=not new_text.endswith("\n"))
        return
    if changed or dest != spec_file:
        dest.write_text(new_text, encoding="utf-8")
    if out.json_mode:
        out.emit_json({"spec": str(dest), "from": source, "to": to, "changed": changed})
    else:
        out.success(f"{dest}: {source} → {to}" + ("" if changed else " (no changes)"))
