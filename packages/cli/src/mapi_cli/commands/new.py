"""``mapi new <template>``: scaffold a spec from a conformance-fixture template."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Annotated

import typer

from mapi_cli.output import Output
from mapi_cli.state import cli_errors, state
from mapi_cli.templates import TEMPLATES, Template, render

__all__ = ["new", "write_sample_data"]


def _list(out: Output) -> None:
    if out.json_mode:
        out.emit_json(
            [
                {
                    "name": t.name,
                    "fixture": t.fixture,
                    "summary": t.summary,
                    "side_tables": list(t.side_tables),
                }
                for t in TEMPLATES.values()
            ]
        )
        return
    out.table(
        ["template", "summary", "side tables"],
        [(t.name, t.summary, ", ".join(t.side_tables) or "-") for t in TEMPLATES.values()],
    )


def write_sample_data(template: Template, directory: Path, *, force: bool) -> dict[str, Path]:
    """Write the fixture's tiny synthetic data (and side tables) as ``*.zarr.zip`` files.

    Needs ``mapi-conformance`` (and the plugins it exercises). The raw DataSchema is stored
    in the ``mapi_schema`` attribute, so ``mapi check/fit --data`` pick it up.
    """
    if importlib.util.find_spec("mapi_conformance") is None:
        raise ImportError("--with-data needs the conformance corpus: pip install mapi-conformance")
    from mapi_conformance.runner import load_fixture
    from mapi_sdk.wire import write_zarr_zip

    from mapi_cli.local import SCHEMA_ATTR

    fixture = load_fixture(template.fixture)
    data, tables = fixture.generate(seed=0, size="tiny")
    data = data.copy()
    data.attrs[SCHEMA_ATTR] = fixture.raw_schema(data).model_dump_json(exclude_defaults=True)
    files = {"data": directory / "data.zarr.zip"}
    files |= {name: directory / f"{name}.zarr.zip" for name in tables}
    clash = [p for p in files.values() if p.exists()]
    if clash and not force:
        raise FileExistsError(f"{clash[0]} exists (use --force to overwrite)")
    write_zarr_zip(data, files["data"])
    for name, table in tables.items():
        write_zarr_zip(table, files[name])
    return files


@cli_errors
def new(
    ctx: typer.Context,
    template: Annotated[
        str | None, typer.Argument(help=f"One of: {', '.join(TEMPLATES)}", show_default=False)
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Spec file to write (default: spec.yaml)")
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="metadata.name (default: the template name)")
    ] = None,
    project: Annotated[str, typer.Option("--project", help="metadata.project")] = "default",
    with_data: Annotated[
        bool,
        typer.Option("--with-data", help="Also write the fixture's tiny sample data (zarr zips)"),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files")] = False,
    list_: Annotated[bool, typer.Option("--list", help="List the templates")] = False,
) -> None:
    """Scaffold a ModelSpec YAML from a template (geo-mmm, forecast, tabular, glm)."""
    out = state(ctx).output
    if list_ or template is None:
        _list(out)
        return
    tpl = TEMPLATES.get(template)
    if tpl is None:
        out.err.print(f"unknown template {template!r}; choose one of: {', '.join(TEMPLATES)}")
        raise typer.Exit(2)
    dest = output or Path("spec.yaml")
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} exists (use --force to overwrite)")
    text = render(tpl, name=name or tpl.name, project=project, file=dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data_files: dict[str, Path] = {}
    if with_data:
        data_files = write_sample_data(tpl, dest.parent, force=force)
    dest.write_text(text, encoding="utf-8")
    if out.json_mode:
        out.emit_json(
            {
                "spec": str(dest),
                "template": tpl.name,
                "data": {k: str(v) for k, v in data_files.items()},
            }
        )
        return
    out.success(f"wrote {dest} from template {tpl.name} (fixture {tpl.fixture})")
    for label, path in data_files.items():
        out.out.print(f"  {label}: {path}")
    cmd = f"mapi fit {dest}"
    if data_files:
        cmd += f" --data {data_files['data']}"
        cmd += "".join(f" --side-table {n}={p}" for n, p in data_files.items() if n != "data")
    else:
        cmd += " --data <data.zarr.zip>"
        cmd += "".join(f" --side-table {n}=<{n}.zarr.zip>" for n in tpl.side_tables)
    out.out.print(
        f"next: mapi check {dest}" + (f" --data {data_files['data']}" if data_files else "")
    )
    out.out.print(f"      {cmd}")
