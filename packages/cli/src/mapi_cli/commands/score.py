"""``mapi score request.yaml``: score a fitted model with replayed preprocessing.

Locally (``--bundle DIR`` from ``mapi fit``) the request runs in this process; payloads come
from ``--data``/``--scenario``/``--side-table`` files. Remotely (``--remote`` with
``--model``/``--version``) the API scores the model version (``Prefer: wait``) and the result
DataTree is downloaded (CRC32C-verified) to ``--out``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.loader import load_score_request

from mapi_cli.output import Output, safe
from mapi_cli.progress import EventRenderer
from mapi_cli.state import AppState, cli_errors, state

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["score", "tree_summary"]


def tree_summary(tree: xr.DataTree) -> list[dict[str, Any]]:
    """Groups and variables of a result tree (for display and --json)."""
    rows: list[dict[str, Any]] = []
    for node in tree.subtree:
        ds = node.to_dataset(inherit=False)
        for name, var in ds.data_vars.items():
            rows.append(
                {
                    "group": node.path,
                    "var": str(name),
                    "dims": {str(d): int(n) for d, n in var.sizes.items()},
                    "units": var.attrs.get("units"),
                }
            )
    return rows


def _print_tree(out: Output, tree: xr.DataTree) -> None:
    attrs = tree.attrs
    out.kv(
        [
            ("representation", attrs.get("representation")),
            ("output", attrs.get("output_kind") or attrs.get("kind")),
            ("output space", attrs.get("output_space")),
        ]
    )
    out.table(
        ["group", "variable", "dims", "units"],
        [
            (
                r["group"],
                r["var"],
                ", ".join(f"{d}={n}" for d, n in r["dims"].items()),
                r["units"] or "",
            )
            for r in tree_summary(tree)
        ],
    )


def _score_local(
    out: Output,
    request_text: str,
    bundle: Path,
    data: Path | None,
    scenario: Path | None,
    tables: dict[str, Path],
    dest: Path | None,
    timeout: float | None,
) -> None:
    from mapi_cli.local import file_ref, load_registry, run_inproc

    if not (bundle / "manifest.json").is_file():
        raise FileNotFoundError(f"{bundle} is not a bundle directory (no manifest.json)")
    req = load_score_request(request_text)
    registry, load_errors = load_registry()
    for err in load_errors:
        out.warn(f"plugin failed to load: {err}")
    payload: dict[str, Any] = {"request": request_text, "bundle": file_ref(bundle)}
    if data is not None:
        payload["data"] = file_ref(data)
    if scenario is not None:
        payload["scenario"] = file_ref(scenario)
    if tables:
        payload["side_tables"] = {n: file_ref(p) for n, p in tables.items()}
    kind = RunKind.EVALUATE if req.mode == "evaluate" else RunKind.SCORE
    with EventRenderer(out, f"score ({req.mode})") as renderer:
        result = run_inproc(
            kind, payload, registry=registry, on_event=renderer.handle, timeout=timeout
        )
    if not result.ok:
        raise result.error()
    tree = result.result
    if dest is not None:
        from mapi_sdk.wire import write_datatree_zip

        write_datatree_zip(tree, dest)
    if out.json_mode:
        out.emit_json(
            {
                "run_id": result.run_id,
                "state": result.status.state.value,
                "out": None if dest is None else str(dest),
                "attrs": {k: v for k, v in tree.attrs.items() if isinstance(v, str | int | float)},
                "variables": tree_summary(tree),
            }
        )
        return
    _print_tree(out, tree)
    if dest is not None:
        out.success(f"wrote {dest}")


def _score_remote(
    st: AppState,
    url: str,
    request_text: str,
    model: str,
    version: int,
    dest: Path | None,
    wait: int,
    timeout: float | None,
) -> None:
    out = st.output
    from mapi_client.transfer import datatree_from_bytes, xarray_available

    with st.client(url) as api:
        out.info(f"scoring {model} v{version}")
        res = api.models.score(
            model, version, request_text, wait=wait, timeout=timeout, download=False
        )
        link = res.tree_link
        tree = None
        if link is not None and dest is not None:
            api.download(link, dest)
            if xarray_available():
                tree = datatree_from_bytes(dest.read_bytes())
        elif link is not None and xarray_available() and not out.json_mode:
            tree = datatree_from_bytes(api.download(link))
        if dest is not None and link is None:
            out.warn(f"the run produced no *.zarr.zip output; {dest} was not written")
            dest = None
    if out.json_mode:
        out.emit_json(
            {"run": res.run, "outputs": res.outputs, "out": None if dest is None else str(dest)}
        )
        return
    out.kv([("run", res.run.id), ("state", safe(res.run.state.value, "green"))])
    out.table(["output", "size", "crc32c"], [(o.name, o.size, o.crc32c) for o in res.outputs])
    if tree is not None:
        _print_tree(out, tree)
    if dest is not None:
        out.success(f"wrote {dest}")


@cli_errors
def score(
    ctx: typer.Context,
    request: Annotated[Path, typer.Argument(help="ScoreRequest YAML", exists=True, dir_okay=False)],
    bundle: Annotated[
        Path | None, typer.Option("--bundle", help="(local) bundle directory from 'mapi fit'")
    ] = None,
    data: Annotated[Path | None, typer.Option("--data", help="(local) data payload")] = None,
    scenario: Annotated[
        Path | None, typer.Option("--scenario", help="(local) scenario payload")
    ] = None,
    side_table: Annotated[
        list[str] | None, typer.Option("--side-table", help="(local) NAME=PATH")
    ] = None,
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the result DataTree (.zarr.zip)")
    ] = None,
    remote: Annotated[
        str | None, typer.Option("--remote", help="Score on the API ('@' = configured URL)")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="(remote) model id")] = None,
    version: Annotated[int | None, typer.Option("--version", help="(remote) version")] = None,
    wait: Annotated[
        int, typer.Option("--wait", min=0, max=60, help="(remote) Prefer: wait=N seconds")
    ] = 60,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Give up after N seconds")
    ] = None,
) -> None:
    """Score a fitted bundle locally, or a model version on the API (--remote)."""
    from mapi_cli.local import parse_assignments

    st = state(ctx)
    text = request.read_text(encoding="utf-8")
    if remote is not None:
        if model is None or version is None:
            raise MapiError(
                "--remote scoring needs --model and --version", code=ErrorCode.SCHEMA_INVALID
            )
        if bundle or data or scenario or side_table:
            raise MapiError(
                "--bundle/--data/--scenario/--side-table are local options; remote payloads "
                "are dataset refs inside the request",
                code=ErrorCode.SCHEMA_INVALID,
            )
        _score_remote(st, remote, text, model, version, out_file, wait, timeout)
        return
    if bundle is None:
        raise MapiError(
            "local scoring needs --bundle (or use --remote)", code=ErrorCode.SCHEMA_INVALID
        )
    tables = parse_assignments(side_table or [])
    _score_local(st.output, text, bundle, data, scenario, tables, out_file, timeout)
