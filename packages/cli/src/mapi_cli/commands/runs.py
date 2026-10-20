"""``mapi runs ls|get|logs|watch|cancel|retry`` and the run log (v0.11): ``metrics``,
``params``, ``log-metric``, ``log-param``, ``tag``, ``rename``, ``note`` (remote)."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer
from mapi_client import RunEnd, RunEventFrame, RunLiveFrame, RunSnapshot, RunTrackingFrame
from mapi_spec.api import Run
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.tracking import ParamValue, parse_run_ref

from mapi_cli.commands.fit import cost_text, follow_run, report_run
from mapi_cli.output import Output, safe
from mapi_cli.state import EXIT_FAILURE, cli_errors, state

app = typer.Typer(help="Inspect and control runs on the API.", no_args_is_help=True)

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
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="At most N runs")] = 20,
) -> None:
    """List runs, newest first. Use filters to find runs by metric, param, tag or name."""
    st = state(ctx)
    extra = list(columns or [])
    with st.client() as api:
        runs = api.runs.list(
            kind=kind,
            state=run_state,
            spec_id=spec_id,
            parent_run_id=parent,
            name=name,
            tags=[_tag_filter(t) for t in tag or []],
            filters=list(filters or []),
            order_by=order_by,
            columns=extra,
            limit=min(limit, 200),
        ).take(limit)
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
