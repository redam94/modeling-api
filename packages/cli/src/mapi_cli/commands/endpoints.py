"""``mapi endpoints ls|get|history|promote|rollback|schema|score|snippets`` (v0.4).

An endpoint is a stable alias for one model version (``endpoint://<project>/<alias>``).
Promotions and rollbacks change what every integration calling the alias gets, so they ask
for confirmation (``--yes`` skips it). ``score`` sends an ``EndpointScoreCall`` (a JSON file,
the endpoint's example with ``--example``, or flags) with ``Prefer: wait`` and downloads the
result DataTree to ``--out``.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_client import ScoreResult
from mapi_spec.api import Endpoint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.snippets import SnippetLanguage
from rich.text import Text

from mapi_cli.commands.score import tree_summary
from mapi_cli.output import Output, safe, strip_control
from mapi_cli.state import cli_errors, confirm, state

app = typer.Typer(
    help="Endpoints: stable aliases for promoted model versions (promote, score, snippets).",
    no_args_is_help=True,
)

_GATE_STYLE = {"allowed": "green", "needs_override": "yellow", "blocked": "red"}


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _show(out: Output, ep: Endpoint) -> None:
    gate = ep.gate.outcome
    out.kv(
        [
            ("alias", ep.alias),
            ("ref", ep.ref),
            ("model", ep.model_ref),
            ("revision", ep.revision),
            ("gate", safe(gate, _GATE_STYLE.get(gate, ""))),
            ("scorecard", ep.gate.scorecard_overall),
            ("overridden", "yes" if ep.overridden else None),
            ("fit mode", ep.evaluation_policy.fit_mode.value),
            ("schema changes", ep.evaluation_policy.schema_change),
            ("score modes", ", ".join(ep.schema_snapshot.score_modes)),
            ("promoted", f"{_when(ep.promoted_at)} by {ep.promoted_by or '-'}"),
            ("description", ep.description),
        ]
    )


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """List the project's endpoints."""
    st = state(ctx)
    with st.client() as api:
        items = api.endpoints.list(limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["alias", "model", "revision", "gate", "promoted"],
        [
            (
                e.alias,
                e.model_ref,
                e.revision,
                safe(e.gate.outcome, _GATE_STYLE.get(e.gate.outcome, "")),
                _when(e.promoted_at),
            )
            for e in items
        ],
    )


@app.command("get")
@cli_errors
def get(ctx: typer.Context, alias: Annotated[str, typer.Argument(help="Endpoint alias")]) -> None:
    """Show an endpoint, the version it serves and its promotion gate."""
    st = state(ctx)
    with st.client() as api:
        ep = api.endpoints.get(alias)
    if st.output.json_mode:
        st.output.emit_json(ep)
    else:
        _show(st.output, ep)


@app.command("history")
@cli_errors
def history(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 20,
) -> None:
    """Promotions and rollbacks, newest first."""
    st = state(ctx)
    with st.client() as api:
        items = api.endpoints.history(alias, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["revision", "action", "model", "gate", "by", "when", "reason"],
        [
            (
                r.revision,
                r.action,
                r.model_ref,
                safe(r.gate.outcome + (" (override)" if r.overridden else "")),
                r.actor_id or r.token_id or "-",
                _when(r.created_at),
                r.reason or "",
            )
            for r in items
        ],
    )


@app.command("promote")
@cli_errors
def promote(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias (created when new)")],
    model: Annotated[
        str | None, typer.Argument(help="model://project/name@vN (or --version-id)")
    ] = None,
    version_id: Annotated[
        str | None, typer.Option("--version-id", help="Model version id (mv_…) instead")
    ] = None,
    override: Annotated[
        bool,
        typer.Option(
            "--override", help="Override a 'needs_override' gate (signed-in project admins)"
        ),
    ] = False,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why (required with --override; audited)")
    ] = None,
    expected_revision: Annotated[
        int | None,
        typer.Option("--expected-revision", min=0, help="Fail if the alias moved (0: new)"),
    ] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Point an alias at a model version, through the scorecard promotion gate."""
    st = state(ctx)
    out = st.output
    if (model is None) == (version_id is None):
        raise ValueError("give the model ref (model://…@vN) or --version-id, not both")
    if override and not (reason and reason.strip()):
        raise ValueError("--override needs a --reason")
    with st.client() as api:
        target = model or version_id
        current = None
        if not yes:
            try:
                current = api.endpoints.get(alias).model_ref
            except MapiError as exc:  # a new alias
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
            where = f" (now {current})" if current else " (new alias)"
            confirm(st, f"Point {alias}{where} at {target}?", yes=False)
        ep = api.endpoints.promote(
            alias,
            model,
            model_version_id=version_id,
            override=override,
            reason=reason,
            expected_revision=expected_revision,
            description=description,
        )
    if out.json_mode:
        out.emit_json(ep)
        return
    _show(out, ep)
    out.success(f"{ep.ref} -> {ep.model_ref} (revision {ep.revision})")


@app.command("rollback")
@cli_errors
def rollback(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias")],
    reason: Annotated[str, typer.Option("--reason", help="Why (audited)")],
    to_revision: Annotated[
        int | None, typer.Option("--to-revision", min=1, help="Default: the previous revision")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Point the alias back at an earlier revision's version."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        if not yes:
            current = api.endpoints.get(alias)
            which = f"revision {to_revision}" if to_revision else "the previous revision"
            confirm(st, f"Roll {alias} back from {current.model_ref} to {which}?", yes=False)
        ep = api.endpoints.rollback(alias, reason, to_revision=to_revision)
    if out.json_mode:
        out.emit_json(ep)
        return
    out.success(f"{ep.ref} -> {ep.model_ref} (revision {ep.revision})")


@app.command("schema")
@cli_errors
def schema(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias")],
    example: Annotated[
        bool, typer.Option("--example", help="Print only the example payload (JSON)")
    ] = False,
) -> None:
    """What to send: the input schema frozen at promotion and an example payload."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        sch = api.endpoints.schema(alias)
    if example:
        typer.echo(json.dumps(sch.example, indent=2, ensure_ascii=False))
        return
    if out.json_mode:
        out.emit_json(sch)
        return
    snap = sch.snapshot
    out.kv(
        [
            ("endpoint", sch.ref),
            ("model", sch.model_ref),
            ("revision", sch.revision),
            ("inputs stage", snap.inputs_stage),
            ("score modes", ", ".join(snap.score_modes)),
            ("horizon dim", snap.horizon_dim),
            ("future inputs", ", ".join(snap.future_required)),
            (
                "actuals",
                (
                    "forecasts can carry actuals (mapi endpoints forecast)"
                    if snap.conditions_on_actuals
                    else "no: forecasts start at the end of the fitted history"
                )
                if snap.horizon_dim
                else None,
            ),
            ("inline payloads", f"up to {sch.max_inline_bytes // (1024 * 1024)} MB"),
        ]
    )
    out.table(
        ["dim", "kind", "policy", "fitted"],
        [
            (
                name,
                d.kind,
                d.policy.value,
                _coords(snap.fit_coords.get(name)),
            )
            for name, d in snap.input_schema.dims.items()
        ],
        title="Dims",
    )
    out.table(
        ["variable", "dims", "dtype", "role"],
        [
            (name, ", ".join(v.dims), v.dtype, v.role.value if v.role else "")
            for name, v in snap.input_schema.vars.items()
        ],
        title="Variables",
    )
    if sch.input_table is not None and sch.input_table.columns:
        out.table(
            ["column", "holds", "description"],
            [(c.name, c.role, c.description) for c in sch.input_table.columns],
            title="Input table (CSV columns for mapi endpoints forecast; one row per period)",
        )
    out.info("example payload (mapi endpoints schema --example):")
    typer.echo(json.dumps(sch.example, indent=2, ensure_ascii=False))


def _coords(cs: Any) -> str:
    if cs is None:
        return ""
    if cs.values:
        shown = ", ".join(str(v) for v in cs.values[:5])
        return shown + (" …" if cs.size > 5 else "")
    if cs.start is not None:
        return f"{cs.start} … {cs.end} ({cs.size})"
    return str(cs.size)


def _load_call(payload: Path | None) -> dict[str, Any]:
    if payload is None:
        return {}
    text = sys.stdin.read() if str(payload) == "-" else payload.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the payload is not JSON: {exc.msg} (line {exc.lineno})") from None
    if not isinstance(doc, dict):
        raise ValueError("the payload must be a JSON object (an EndpointScoreCall)")
    return doc


def _data_value(value: str) -> Any:
    """``--data``/``--scenario``: a dataset ref, an XrJSON file or a netCDF/zarr-zip file."""
    if value.startswith("dataset://"):
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file (or give a dataset:// reference)")
    name = path.name.lower()
    if name.endswith(".json"):
        return json.loads(path.read_text(encoding="utf-8"))
    from mapi_client.transfer import require_xarray

    require_xarray()
    if name.endswith((".zarr.zip", ".zip")):
        from mapi_sdk.wire import read_zarr_zip

        return read_zarr_zip(path.read_bytes())
    if name.endswith((".nc", ".nc4")):
        import xarray as xr

        with xr.open_dataset(path) as ds:
            return ds.load()
    raise ValueError(f"cannot read {path.name}: use .json (XrJSON), .zarr.zip or .nc")


@app.command("score")
@cli_errors
def score(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias")],
    payload: Annotated[
        Path | None,
        typer.Argument(help="EndpointScoreCall JSON file ('-' = stdin)", dir_okay=False),
    ] = None,
    example: Annotated[
        bool, typer.Option("--example", help="Send the endpoint's example payload")
    ] = False,
    mode: Annotated[str | None, typer.Option("--mode", help="predict, forecast, …")] = None,
    periods: Annotated[
        int | None, typer.Option("--periods", min=1, help="Forecast horizon (periods)")
    ] = None,
    horizon_dim: Annotated[str, typer.Option("--horizon-dim")] = "time",
    data: Annotated[
        str | None,
        typer.Option("--data", help="dataset:// ref, XrJSON .json, .zarr.zip or .nc file"),
    ] = None,
    scenario: Annotated[str | None, typer.Option("--scenario", help="Like --data")] = None,
    draws: Annotated[int | None, typer.Option("--draws", min=0, help="Draws to return")] = None,
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the result DataTree (.zarr.zip)")
    ] = None,
    wait: Annotated[int, typer.Option("--wait", min=0, max=60, help="Prefer: wait=N")] = 60,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Give up after N seconds")
    ] = None,
) -> None:
    """Score the endpoint's current version and wait for the result."""
    st = state(ctx)
    out = st.output
    body = _load_call(payload)
    with st.client() as api:
        if example:
            if payload is not None:
                raise ValueError("give a payload file or --example, not both")
            body = dict(api.endpoints.schema(alias).example)
        fields: dict[str, Any] = {"mode": mode}
        if periods is not None:
            fields["horizon"] = {"dim": horizon_dim, "periods": periods}
        if data is not None:
            fields["data"] = _data_value(data)
        if scenario is not None:
            fields["scenario"] = _data_value(scenario)
        if draws is not None:
            fields["output"] = {**dict(body.get("output") or {}), "draws": draws}
        out.info(f"scoring {alias}")
        res = api.endpoints.score(alias, body, wait=wait, timeout=timeout, download=False, **fields)
        tree = None
        link = res.tree_link
        if link is not None and out_file is not None:
            api.download(link, out_file)
        from mapi_client.transfer import datatree_from_bytes, xarray_available

        if link is not None and xarray_available() and not out.json_mode:
            raw = out_file.read_bytes() if out_file is not None else api.download(link)
            tree = datatree_from_bytes(raw)
    if out.json_mode:
        out.emit_json(
            {"run": res.run, "outputs": res.outputs, "out": str(out_file) if out_file else None}
        )
        return
    out.kv([("run", res.run.id), ("state", safe(res.run.state.value, "green"))])
    _show_result(out, res)
    if tree is not None:
        out.table(
            ["group", "variable", "dims"],
            [
                (r["group"], r["var"], ", ".join(f"{d}={n}" for d, n in r["dims"].items()))
                for r in tree_summary(tree)
            ],
        )
    if out_file is not None and link is not None:
        out.success(f"wrote {out_file}")


def _show_result(out: Output, res: ScoreResult, *, max_rows: int = 40) -> None:
    """What a forecast was conditioned on, then the inline summary tables."""
    cond = res.conditioning
    if cond is not None:
        style = "green" if cond.conditioned else ""
        out.out.print(safe(cond.message, style))
    for table in res.summary.tables if res.summary is not None else []:
        rows = [["" if c is None else c for c in r] for r in table.rows[:max_rows]]
        title = table.variable
        if len(table.rows) > max_rows:
            title += f" (first {max_rows} of {len(table.rows)} rows)"
        out.table(table.columns, rows, title=title)


def _cell(text: str) -> Any:
    """A CSV cell: empty is ``None``, numbers are numbers, the rest is text (dates)."""
    value = text.strip()
    if not value:
        return None
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            continue
    return value


def _read_table(path: Path) -> list[dict[str, Any]]:
    """A long table from a CSV file (a header row) or a JSON list of row objects."""
    text = sys.stdin.read() if str(path) == "-" else path.expanduser().read_text("utf-8-sig")
    if path.suffix.lower() == ".json":
        rows = json.loads(text)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError(f"{path.name} must hold a JSON list of row objects")
        return rows
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError(f"{path.name} has no header row")
    return [{k.strip(): _cell(v or "") for k, v in row.items() if k} for row in reader]


@app.command("forecast")
@cli_errors
def forecast(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias")],
    table: Annotated[
        Path | None,
        typer.Argument(
            help="CSV (header row) or JSON rows in the endpoint's input layout: actuals with "
            "the target filled, dates to forecast with it empty ('-' = stdin)",
            dir_okay=False,
        ),
    ] = None,
    actuals: Annotated[
        Path | None, typer.Option("--actuals", help="Rows with the target filled (CSV/JSON)")
    ] = None,
    future: Annotated[
        list[str] | None,
        typer.Option(
            "--future",
            help="A date to forecast (repeatable), or a CSV/JSON file of future rows with "
            "their driver values",
        ),
    ] = None,
    periods: Annotated[
        int | None,
        typer.Option("--periods", min=1, help="Periods to append after the last actual"),
    ] = None,
    future_covariates: Annotated[
        str,
        typer.Option(
            "--future-covariates",
            help="Empty future drivers: carry_last, seasonal_naive, zero or supplied",
        ),
    ] = "carry_last",
    wait: Annotated[int, typer.Option("--wait", min=0, max=60, help="Prefer: wait=N")] = 60,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Give up after N seconds")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the call body (JSON) and send nothing")
    ] = False,
) -> None:
    """Forecast with your latest actuals and the dates to forecast (no refit).

    The table has the columns of `mapi endpoints schema ALIAS` (Input table): the time
    column, the target and any drivers. Rows with the target filled are actuals: the model
    updates its state with them using its fitted parameters and forecasts from the last
    one. Rows after them with the target empty are forecast; without such rows, --periods
    (default 8) are appended. The answer says what the forecast was conditioned on.
    """
    from mapi_client import forecast_call

    st = state(ctx)
    out = st.output
    future_arg: Any = None
    if future:
        files = [f for f in future if Path(f).suffix.lower() in (".csv", ".json")]
        if files and len(future) > 1:
            raise ValueError("give one --future file, or dates")
        future_arg = _read_table(Path(files[0])) if files else list(future)
    with st.client() as api:
        schema = api.endpoints.schema(alias)
        call = forecast_call(
            schema,
            _read_table(table) if table is not None else None,
            actuals=_read_table(actuals) if actuals is not None else None,
            future=future_arg,
            periods=periods,
            future_covariates=future_covariates,
        )
        if dry_run:
            body = call.model_dump(mode="json", by_alias=True, exclude_defaults=True)
            typer.echo(json.dumps(body, indent=2, ensure_ascii=False))
            return
        out.info(f"forecasting {alias}")
        res = api.endpoints.score(alias, call, wait=wait, timeout=timeout, download=False)
    if out.json_mode:
        out.emit_json({"run": res.run, "summary": res.summary, "outputs": res.outputs})
        return
    out.kv([("run", res.run.id), ("state", safe(res.run.state.value, "green"))])
    _show_result(out, res)


@app.command("snippets")
@cli_errors
def snippets(
    ctx: typer.Context,
    alias: Annotated[str, typer.Argument(help="Endpoint alias")],
    language: Annotated[
        list[SnippetLanguage] | None,
        typer.Option("--language", "-l", help="python, typescript, curl, r, apps_script, notebook"),
    ] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="API URL to embed (default: the server's)")
    ] = None,
) -> None:
    """Ready-to-run code that scores the endpoint (reads the token from MAPI_TOKEN).

    With one --language only the code is printed, so it can be redirected to a file.
    """
    st = state(ctx)
    out = st.output
    with st.client() as api:
        found = api.endpoints.snippets(alias, language, base_url=base_url)
    if out.json_mode:
        out.emit_json(found)
        return
    if language and len(language) == 1 and len(found.items) == 1:
        typer.echo(strip_control(found.items[0].code), nl=False)
        return
    for item in found.items:
        out.out.print(Text(f"# {strip_control(item.title)}", style="bold"))
        if item.requires:
            out.out.print(safe(f"# requires: {item.requires}", "dim"))
        typer.echo(strip_control(item.code))
