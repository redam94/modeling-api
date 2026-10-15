"""``mapi runs ls|get|logs|watch|cancel|retry`` (remote)."""

from __future__ import annotations

from typing import Annotated

import typer
from mapi_client import RunEnd, RunEventFrame, RunLiveFrame, RunSnapshot
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState

from mapi_cli.commands.fit import cost_text, follow_run, report_run
from mapi_cli.output import safe
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
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="At most N runs")] = 20,
) -> None:
    """List runs, newest first."""
    st = state(ctx)
    with st.client() as api:
        runs = api.runs.list(
            kind=kind, state=run_state, spec_id=spec_id, limit=min(limit, 200)
        ).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(runs)
        return
    out.table(
        ["id", "kind", "state", "attempt", "progress", "cost", "created"],
        [
            (
                r.id,
                r.kind.value,
                safe(r.state.value, _STATE_STYLE.get(r.state.value, "")),
                r.attempt,
                f"{r.progress.fraction:.0%}"
                if r.progress and r.progress.fraction is not None
                else "",
                cost_text(r.cost),
                r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            for r in runs
        ],
    )


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
    stream: Annotated[str | None, typer.Option("--stream", help="stdout, stderr or system")] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", min=1, help="At most N")] = None,
) -> None:
    """Print captured log lines (untrusted text, control characters stripped)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        pager = api.runs.logs(run_id, attempt=attempt, stream=stream)
        lines = pager.take(limit) if limit is not None else list(pager)
    if out.json_mode:
        out.emit_json(lines)
        return
    for line in lines:
        prefix = safe(f"[a{line.attempt} {line.stream}] ", "dim")
        out.out.print(prefix + safe(line.line, "red" if line.stream == "stderr" else ""))


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
