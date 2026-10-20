"""``mapi runs ls|get|logs|watch|cancel|retry`` and the run log (v0.11): ``metrics``,
``params``, ``log-metric``, ``log-param``, ``tag``, ``rename``, ``note`` (remote).

v0.14: ``media``, ``log-image``, ``compare`` and the saved views ``views ls|save|rm``;
``ls --experiment`` and ``ls --view``."""

from __future__ import annotations

import json
import os
import sys
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
    media_file_name,
)
from mapi_spec.api import Run, RunCompare, RunViewQuery
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.tracking import ParamValue, parse_run_ref

from mapi_cli.commands.experiments import resolve_experiment
from mapi_cli.commands.fit import cost_text, follow_run, report_run
from mapi_cli.output import Output, safe
from mapi_cli.state import EXIT_FAILURE, cli_errors, confirm, state

app = typer.Typer(help="Inspect and control runs on the API.", no_args_is_help=True)
views_app = typer.Typer(
    help="Save, list and delete run views (a saved search of the runs table).",
    no_args_is_help=True,
)
app.add_typer(views_app, name="views")

_STATE_STYLE = {
    "succeeded": "green",
    "failed": "red",
    "rejected": "red",
    "cancelled": "yellow",
    "partial": "yellow",
    "running": "cyan",
    "stalled": "magenta",
}


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    kind: Annotated[RunKind | None, typer.Option("--kind", help="Filter by kind")] = None,
    run_state: Annotated[RunState | None, typer.Option("--state", help="Filter by state")] = None,
    spec_id: Annotated[str | None, typer.Option("--spec", help="Filter by spec id")] = None,
    parent: Annotated[
        str | None, typer.Option("--parent", help="Show only the child runs of this run")
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Show only runs with this text in the name")
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Show only runs with this tag: KEY or KEY=VALUE. You can use the option "
            "more than one time.",
        ),
    ] = None,
    filters: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            help="Show only runs that agree with this filter, for example "
            "'metric.rmse < 0.5' or 'param.fit.draws >= 1000'. You can use the option "
            "more than one time.",
        ),
    ] = None,
    order_by: Annotated[
        str | None,
        typer.Option(
            "--order-by",
            help="Sort the runs by this value, for example '-metric.rmse' (a '-' sorts from "
            "high to low). The default is '-created_at'.",
        ),
    ] = None,
    columns: Annotated[
        list[str] | None,
        typer.Option(
            "--column",
            "-c",
            help="Add a column with this value, for example 'metric.rmse' or "
            "'param.fit.draws'. You can use the option more than one time.",
        ),
    ] = None,
    experiment: Annotated[
        str | None,
        typer.Option("--experiment", "-e", help="Show only the runs of this experiment"),
    ] = None,
    view: Annotated[
        str | None,
        typer.Option(
            "--view",
            help="Use the search of this saved view. Other options add to the search or "
            "replace a part of it.",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="At most N runs")] = 20,
) -> None:
    """List runs, newest first. Use filters to find runs by metric, param, tag or name."""
    st = state(ctx)
    with st.client() as api:
        query: dict[str, Any] = {}
        if view is not None:
            query = _view_query(api, view).model_dump(mode="json")
        args = _merge_query(
            query,
            kind=kind.value if kind else None,
            state=run_state.value if run_state else None,
            spec_id=spec_id,
            parent_run_id=parent,
            name=name,
            experiment_id=resolve_experiment(api, experiment).id if experiment else None,
            tags=[_tag_filter(t) for t in tag or []],
            filters=list(filters or []),
            order_by=order_by,
            columns=list(columns or []),
        )
        extra = list(args["columns"])
        runs = api.runs.list(**args, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(runs)
        return
    out.table(
        ["id", "name", "kind", "state", "attempt", "progress", "cost", "created", *extra],
        [
            (
                r.id,
                r.name or "",
                r.kind.value,
                safe(r.state.value, _STATE_STYLE.get(r.state.value, "")),
                r.attempt,
                f"{r.progress.fraction:.0%}"
                if r.progress and r.progress.fraction is not None
                else "",
                cost_text(r.cost),
                r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                *(_column(r.columns, col) for col in extra),
            )
            for r in runs
        ],
    )


def _merge_query(base: dict[str, Any], **given: Any) -> dict[str, Any]:
    """``runs.list`` arguments: a saved view's query, with the given options on top (a
    single value replaces the view's; filters, tags and columns are added)."""
    out: dict[str, Any] = {
        "kind": base.get("kind"),
        "state": base.get("state"),
        "spec_id": base.get("spec_id"),
        "parent_run_id": base.get("parent_run_id"),
        "name": base.get("name"),
        "experiment_id": base.get("experiment_id"),
        "order_by": base.get("order_by"),
    }
    for key in out:
        if given.get(key) is not None:
            out[key] = given[key]
    for key in ("tags", "filters", "columns"):
        merged = [*base.get(key, []), *given.get(key, [])]
        out[key] = list(dict.fromkeys(merged))
    return out


def _view_query(api: Client, name_or_id: str) -> RunViewQuery:
    found = api.run_views.find(name_or_id)
    if found is None:
        raise MapiError(
            f"no saved view has the id or the name {name_or_id!r}. "
            "Use 'mapi runs views ls' to see the views",
            code=ErrorCode.NOT_FOUND,
        )
    return found.query


def _tag_filter(text: str) -> str:
    """``KEY=VALUE`` on the command line is ``KEY:VALUE`` in the API (``KEY`` stays)."""
    key, sep, value = text.partition("=")
    return f"{key}:{value}" if sep else key


def _column(columns: dict[str, float | str | None] | None, ref: str) -> str:
    if not columns:
        return ""
    value = columns.get(parse_run_ref(ref).text())
    return "" if value is None else (f"{value:.4g}" if isinstance(value, float) else str(value))


def _kv_pairs(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"{what} {item!r} must be KEY=VALUE")
        out[key] = value
    return out


@app.command("get")
@cli_errors
def get(ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="Run id")]) -> None:
    """Show one run (state, failure, cost, problem)."""
    st = state(ctx)
    with st.client() as api:
        run = api.runs.get(run_id)
    report_run(st.output, run)


@app.command("logs")
@cli_errors
def logs(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    attempt: Annotated[int | None, typer.Option("--attempt", min=1)] = None,
    stream: Annotated[
        str | None, typer.Option("--stream", help="stdout, stderr, system or user")
    ] = None,
    level: Annotated[
        str | None,
        typer.Option(
            "--level",
            help="Show only lines at this level or higher: debug, info, warning or error",
        ),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", min=1, help="At most N")] = None,
) -> None:
    """Print captured log lines (untrusted text, control characters stripped)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        pager = api.runs.logs(run_id, attempt=attempt, stream=stream, level=level)
        lines = pager.take(limit) if limit is not None else list(pager)
    if out.json_mode:
        out.emit_json(lines)
        return
    for line in lines:
        tag = f"{line.stream} {line.level}" if line.level else line.stream
        prefix = safe(f"[a{line.attempt} {tag}] ", "dim")
        style = _LINE_STYLE.get(line.level or "", "red" if line.stream == "stderr" else "")
        text = prefix + safe(line.line, style)
        if line.fields:
            text += safe(" " + " ".join(f"{k}={v}" for k, v in line.fields.items()), "dim")
        out.out.print(text)


_LINE_STYLE = {"warning": "yellow", "error": "red"}


@app.command("watch")
@cli_errors
def watch(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    last_event_id: Annotated[
        int | None, typer.Option("--from", help="Resume after this event id")
    ] = None,
) -> None:
    """Follow a run's live events until it ends (--json: one JSON event per line)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        if out.json_mode:
            import json

            for item in api.runs.stream_events(run_id, last_event_id=last_event_id):
                if isinstance(item, RunSnapshot):
                    doc = {"event": "snapshot", "run": item.run.model_dump(mode="json")}
                elif isinstance(item, RunEventFrame | RunLiveFrame):
                    doc = {"event": item.name.value, "data": item.event.model_dump(mode="json")}
                elif isinstance(item, RunTrackingFrame):
                    doc = {"event": "tracking", "data": item.frame.model_dump(mode="json")}
                else:
                    assert isinstance(item, RunEnd)
                    doc = {"event": "end", "run": item.run.model_dump(mode="json")}
                typer.echo(json.dumps(doc))
            final = api.runs.get(run_id)
        else:
            final = follow_run(api, out, api.runs.get(run_id), title=f"run {run_id}")
            report_run(out, final)
    if final.state.value in ("failed", "rejected", "cancelled"):
        raise typer.Exit(EXIT_FAILURE)


@app.command("cancel")
@cli_errors
def cancel(ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="Run id")]) -> None:
    """Request cancellation (idempotent)."""
    st = state(ctx)
    with st.client() as api:
        run = api.runs.cancel(run_id)
    report_run(st.output, run)


@app.command("retry")
@cli_errors
def retry(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id of a failed run")],
    resource_class: Annotated[
        ResourceClass | None, typer.Option("--resource-class", help="S, M, L or XL")
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed", min=0)] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Start a new run from a failed one (e.g. one resource class up)."""
    st = state(ctx)
    with st.client() as api:
        run = api.runs.retry(run_id, resource_class=resource_class, seed=seed, reason=reason)
    report_run(st.output, run)


# ------------------------------------------------------------------------------- run log


@app.command("metrics")
@cli_errors
def metrics(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="Show the points of this metric, not the summary of all"),
    ] = None,
    points: Annotated[
        int,
        typer.Option(
            "--points", min=10, max=10_000, help="With --key: the maximum points for each series"
        ),
    ] = 1000,
) -> None:
    """Show the metrics of a run: one row for each series, or the points of one metric."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        if key is not None:
            hist = api.runs.metric_history(run_id, key, max_points=points)
        else:
            series = list(api.runs.metrics(run_id))
    if key is not None:
        if out.json_mode:
            out.emit_json(hist)
            return
        out.table(
            ["labels", "step", "value"],
            [
                (_labels(s.labels), "" if step is None else step, f"{value:.6g}")
                for s in hist.series
                for step, value in zip(s.steps, s.values, strict=True)
            ],
            title=f"{key} (downsampled)" if any(s.downsampled for s in hist.series) else key,
        )
        return
    if out.json_mode:
        out.emit_json(series)
        return
    if not series:
        out.info("The run did not log metrics.")
        return
    out.table(
        ["key", "labels", "count", "last", "step", "min", "max"],
        [
            (
                s.key,
                _labels(s.labels),
                s.count,
                f"{s.last:.6g}",
                "" if s.last_step is None else s.last_step,
                f"{s.min:.6g}",
                f"{s.max:.6g}",
            )
            for s in series
        ],
    )


def _labels(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


@app.command("params")
@cli_errors
def params(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="Show only keys that start with this text")
    ] = None,
) -> None:
    """Show the params of a run."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        items = list(api.runs.params(run_id, prefix=prefix))
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["key", "value", "source", "logged"],
        [(p.key, p.value, p.source, p.logged_at.strftime("%Y-%m-%d %H:%M:%S")) for p in items],
    )


@app.command("log-metric")
@cli_errors
def log_metric(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    key: Annotated[str, typer.Argument(help="Metric key, for example revenue_lift")],
    value: Annotated[float, typer.Argument(help="Value (a number)")],
    step: Annotated[int | None, typer.Option("--step", min=0, help="Step of the point")] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Label KEY=VALUE. You can use the option more than one time."),
    ] = None,
) -> None:
    """Add a metric point to a run. You can also do this after the run ends."""
    st = state(ctx)
    labels = _kv_pairs(label or [], "label")
    with st.client() as api:
        res = api.runs.log_metric(run_id, key, value, step=step, labels=labels or None)
    if st.output.json_mode:
        st.output.emit_json(res)
    else:
        st.output.success(f"logged {res.accepted} point(s), {res.duplicates} duplicate(s)")


def _param_value(text: str, as_text: bool) -> ParamValue:
    if as_text:
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, bool | int | float) else text


@app.command("log-param")
@cli_errors
def log_param(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    key: Annotated[str, typer.Argument(help="Param key, for example analyst")],
    value: Annotated[str, typer.Argument(help="Value: a number, true, false or text")],
    as_text: Annotated[
        bool, typer.Option("--text", help="Keep the value as text (do not read a number)")
    ] = False,
) -> None:
    """Add a param to a run. A param cannot change after you set it."""
    st = state(ctx)
    with st.client() as api:
        res = api.runs.log_params(run_id, {key: _param_value(value, as_text)})
    if st.output.json_mode:
        st.output.emit_json(res)
    elif res.accepted:
        st.output.success(f"logged param {key}")
    else:
        st.output.success(f"param {key} has this value already")


@app.command("tag")
@cli_errors
def tag(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    tags: Annotated[list[str] | None, typer.Argument(help="Tags to set: KEY=VALUE ...")] = None,
    delete: Annotated[
        list[str] | None,
        typer.Option(
            "--delete", help="Remove the tag KEY. You can use the option more than one time."
        ),
    ] = None,
) -> None:
    """Set or remove tags of a run."""
    st = state(ctx)
    changes: dict[str, str | None] = dict(_kv_pairs(tags or [], "tag"))
    changes.update({k: None for k in delete or []})
    if not changes:
        raise ValueError("give at least one KEY=VALUE or --delete KEY")
    with st.client() as api:
        run = api.runs.set_tags(run_id, changes)
    _report_log(st.output, run)


@app.command("rename")
@cli_errors
def rename(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    name: Annotated[str, typer.Argument(help="New name (an empty name removes the name)")],
) -> None:
    """Set the name of a run."""
    st = state(ctx)
    with st.client() as api:
        run = api.runs.update(run_id, name=name or None)
    _report_log(st.output, run)


@app.command("note")
@cli_errors
def note(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    text: Annotated[str, typer.Argument(help="Note text, or '-' to read the note from stdin")],
) -> None:
    """Set the note of a run. An empty note removes the note."""
    st = state(ctx)
    body = sys.stdin.read() if text == "-" else text
    with st.client() as api:
        run = api.runs.update(run_id, note=body or None)
    _report_log(st.output, run)


def _report_log(out: Output, run: Run) -> None:
    if out.json_mode:
        out.emit_json(run)
        return
    out.kv(
        [
            ("run", run.id),
            ("name", run.name),
            ("tags", ", ".join(f"{k}={v}" for k, v in sorted(run.tags.items()))),
            ("note", (run.note or "")[:200]),
        ]
    )


# --------------------------------------------------------------------------- media (v0.14)


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


@app.command("media")
@cli_errors
def media(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    key: Annotated[str | None, typer.Option("--key", help="Show only this media key")] = None,
    download: Annotated[
        Path | None,
        typer.Option(
            "--download",
            help="Write the files to this directory, with names KEY-LABELS-STEP.EXT",
            file_okay=False,
        ),
    ] = None,
) -> None:
    """Show the images and tables that a run logged. You can also download them."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        items = list(api.runs.media(run_id, key=key, links=download is not None))
        written: list[str] = []
        if download is not None:
            download.mkdir(parents=True, exist_ok=True)
            used: set[str] = set()
            for item in items:
                name = media_file_name(item)
                if name in used:  # the same key, labels and step in two attempts
                    stem, ext = os.path.splitext(name)
                    name = f"{stem}-a{item.attempt}{ext}"
                used.add(name)
                (download / name).write_bytes(api.runs.download_media(run_id, item))
                written.append(str(download / name))
    if out.json_mode:
        out.emit_json({"media": items, "files": written} if download is not None else items)
        return
    if not items:
        out.info("The run did not log images or tables.")
        return
    out.table(
        ["key", "labels", "step", "kind", "size", "caption"],
        [
            (
                m.key,
                _labels(m.labels),
                "" if m.step is None else m.step,
                m.kind if m.width is None else f"{m.kind} {m.width}x{m.height}",
                _size(m.size),
                (m.caption or "")[:80],
            )
            for m in items
        ],
    )
    if written:
        out.success(f"wrote {len(written)} file(s) to {download}")


@app.command("log-image")
@cli_errors
def log_image(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id")],
    key: Annotated[str, typer.Argument(help="Media key, for example samples")],
    file: Annotated[
        Path, typer.Argument(help="PNG or JPEG file (at most 4 MB)", exists=True, dir_okay=False)
    ],
    step: Annotated[int | None, typer.Option("--step", min=0, help="Step of the image")] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Label KEY=VALUE. You can use the option more than one time."),
    ] = None,
    caption: Annotated[
        str | None, typer.Option("--caption", help="Text to show with the image")
    ] = None,
) -> None:
    """Add an image to a run. You can also do this after the run ends."""
    st = state(ctx)
    labels = _kv_pairs(label or [], "label")
    with st.client() as api:
        item = api.runs.log_image(
            run_id, key, file, step=step, labels=labels or None, caption=caption
        )
    if st.output.json_mode:
        st.output.emit_json(item)
    else:
        st.output.success(f"logged image {item.key} ({_size(item.size)}) as {item.id}")


# --------------------------------------------------------------------- compare (v0.14)

_AGGS = ("last", "min", "max")


@app.command("compare")
@cli_errors
def compare(
    ctx: typer.Context,
    run_ids: Annotated[list[str], typer.Argument(help="2 to 10 run ids")],
    keys: Annotated[
        list[str] | None,
        typer.Option(
            "--key",
            "-k",
            help="Compare this metric. You can use the option more than one time. "
            "The default is the metrics that most of the runs have.",
        ),
    ] = None,
    agg: Annotated[
        str, typer.Option("--agg", help="The value to show for each metric: last, min or max")
    ] = "last",
    all_params: Annotated[
        bool, typer.Option("--all-params", help="Show all params, not only the params that differ")
    ] = False,
) -> None:
    """Compare runs: a table of metric values, then the params that are different."""
    if agg not in _AGGS:
        raise ValueError(f"--agg must be one of {', '.join(_AGGS)}")
    st = state(ctx)
    with st.client() as api:
        res = api.runs.compare(run_ids, keys=keys or (), max_points=10)
    out = st.output
    if out.json_mode:
        out.emit_json(res)
        return
    _compare_tables(out, res, agg, all_params, keys or [])


def _run_label(run: Run) -> str:
    return run.name or run.id


def _compare_tables(
    out: Output, res: RunCompare, agg: str, all_params: bool, keys: list[str]
) -> None:
    order = [r.id for r in res.runs]
    chosen = set(keys) or {m.key for m in res.metrics}
    cells: dict[str, dict[str, str]] = {}
    for s in res.summaries:
        if chosen and s.key not in chosen:
            continue
        name = s.key + (f"{{{_labels(s.labels)}}}" if s.labels else "")
        value = getattr(s, agg)
        cells.setdefault(name, {})[s.run_id] = "" if value is None else f"{value:.4g}"
    if cells:
        out.table(
            ["metric", *(_run_label(r) for r in res.runs)],
            [(name, *(row.get(rid, "") for rid in order)) for name, row in sorted(cells.items())],
            title=f"metrics ({agg})",
        )
    else:
        out.info("The runs have no metrics in common.")
    rows = [p for p in res.params if all_params or p.differs]
    if rows:
        out.table(
            ["param", *(_run_label(r) for r in res.runs)],
            [(p.key, *(p.values.get(rid) or "-" for rid in order)) for p in rows],
            title="params" if all_params else "params that differ",
        )
    elif res.params:
        out.info("The runs have the same params.")
    if any(res.truncated.values()):
        parts = ", ".join(k for k, v in res.truncated.items() if v)
        out.warn(f"the server shows only a part of these items: {parts}")


# ------------------------------------------------------------------ saved views (v0.14)


@views_app.command("ls")
@cli_errors
def views_ls(
    ctx: typer.Context,
    experiment: Annotated[
        str | None,
        typer.Option("--experiment", "-e", help="Show only the views of this experiment"),
    ] = None,
) -> None:
    """List the saved run views of the project."""
    st = state(ctx)
    with st.client() as api:
        exp_id = resolve_experiment(api, experiment).id if experiment else None
        items = list(api.run_views.list(experiment_id=exp_id))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    if not items:
        out.info("The project has no saved views. Use 'mapi runs views save NAME'.")
        return
    out.table(
        ["id", "name", "experiment", "filters", "order", "columns"],
        [
            (
                v.id,
                v.name,
                v.experiment_id or "",
                "; ".join(v.query.filters),
                v.query.order_by or "",
                ", ".join(v.query.columns),
            )
            for v in items
        ],
    )


@views_app.command("save")
@cli_errors
def views_save(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name of the view")],
    filters: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            help="A filter, for example 'metric.rmse < 0.5'. "
            "You can use the option more than one time.",
        ),
    ] = None,
    columns: Annotated[
        list[str] | None,
        typer.Option(
            "--column",
            "-c",
            help="A column, for example 'metric.rmse'. You can use the option more than one time.",
        ),
    ] = None,
    order_by: Annotated[
        str | None, typer.Option("--order-by", help="Sort order, for example '-metric.rmse'")
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag", help="A tag: KEY or KEY=VALUE. You can use the option more than one time."
        ),
    ] = None,
    kind: Annotated[RunKind | None, typer.Option("--kind", help="Kind of run")] = None,
    run_state: Annotated[RunState | None, typer.Option("--state", help="State of run")] = None,
    experiment: Annotated[
        str | None,
        typer.Option(
            "--experiment",
            "-e",
            help="Save the view in this experiment. The view then shows only its runs.",
        ),
    ] = None,
) -> None:
    """Save a search of the runs table as a view. A view with the same name is replaced."""
    st = state(ctx)
    with st.client() as api:
        exp_id = resolve_experiment(api, experiment).id if experiment else None
        query = RunViewQuery(
            kind=kind,
            state=run_state,
            experiment_id=exp_id,
            tags=[_tag_filter(t) for t in tag or []],
            filters=list(filters or []),
            order_by=order_by,
            columns=list(columns or []),
        )
        existing = api.run_views.find(name, experiment_id=exp_id)
        if existing is not None and existing.name.lower() == name.strip().lower():
            saved = api.run_views.update(existing.id, query=query)
        else:
            saved = api.run_views.create(name, query, experiment_id=exp_id)
    if st.output.json_mode:
        st.output.emit_json(saved)
    else:
        st.output.success(f"saved view {saved.name} ({saved.id})")


@views_app.command("rm")
@cli_errors
def views_rm(
    ctx: typer.Context,
    view: Annotated[str, typer.Argument(help="View id or name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Delete a saved run view. The runs do not change."""
    st = state(ctx)
    with st.client() as api:
        found = api.run_views.find(view)
        if found is None:
            raise MapiError(
                f"no saved view has the id or the name {view!r}", code=ErrorCode.NOT_FOUND
            )
        confirm(st, f"Delete the saved view {found.name!r}?", yes=yes)
        api.run_views.delete(found.id)
    if st.output.json_mode:
        st.output.emit_json({"deleted": found.id})
    else:
        st.output.success(f"deleted view {found.id}")
