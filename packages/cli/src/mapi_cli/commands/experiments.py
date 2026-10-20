"""``mapi experiments ls|create|show|rename|archive|unarchive|rm|add|remove`` (v0.14).

An experiment is a named group of runs in a project. ``EXP`` is an experiment id or its exact
name (case is ignored). :func:`resolve_experiment` is shared with ``mapi fit --experiment``
and ``mapi runs ls --experiment``.
"""

from __future__ import annotations

from typing import Annotated

import typer
from mapi_client import Client
from mapi_spec.api import Experiment, ExperimentRunsResult
from mapi_spec.errors import ErrorCode, MapiError

from mapi_cli.output import Output
from mapi_cli.state import cli_errors, confirm, state

app = typer.Typer(help="Make and change experiments (named groups of runs).", no_args_is_help=True)

_EXP_HELP = "Experiment id or name"


def resolve_experiment(api: Client, name_or_id: str) -> Experiment:
    """The experiment with this id or name, else a ``NOT_FOUND`` error."""
    found = api.experiments.find(name_or_id)
    if found is None:
        raise MapiError(
            f"no experiment has the id or the name {name_or_id!r}. "
            "Use 'mapi experiments ls' to see the experiments",
            code=ErrorCode.NOT_FOUND,
        )
    return found


def _when(exp: Experiment) -> str:
    return exp.last_run_at.strftime("%Y-%m-%d %H:%M") if exp.last_run_at else ""


def _report(out: Output, exp: Experiment) -> None:
    if out.json_mode:
        out.emit_json(exp)
        return
    out.kv(
        [
            ("experiment", exp.id),
            ("name", exp.name),
            ("description", exp.description),
            ("archived", "yes" if exp.archived else "no"),
            ("runs", exp.run_count),
            ("last run", _when(exp)),
            ("created", exp.created_at.strftime("%Y-%m-%d %H:%M:%S")),
        ]
    )


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Option("--name", help="Show only experiments with this name")
    ] = None,
    archived: Annotated[
        bool | None,
        typer.Option(
            "--archived/--active",
            help="Show only archived experiments, or only active experiments. "
            "The default shows all experiments.",
            show_default=False,
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="At most N")] = 50,
) -> None:
    """List the experiments of the project, newest first."""
    st = state(ctx)
    with st.client() as api:
        items = api.experiments.list(name=name, archived=archived, limit=min(limit, 200)).take(
            limit
        )
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    if not items:
        out.info("The project has no experiments. Use 'mapi experiments create NAME'.")
        return
    out.table(
        ["id", "name", "runs", "last run", "archived"],
        [(e.id, e.name, e.run_count, _when(e), "yes" if e.archived else "") for e in items],
    )


@app.command("create")
@cli_errors
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name of the experiment (unique in the project)")],
    description: Annotated[
        str | None, typer.Option("--description", help="Text that tells what the runs compare")
    ] = None,
) -> None:
    """Make an experiment."""
    st = state(ctx)
    with st.client() as api:
        exp = api.experiments.create(name, description=description)
    _report(st.output, exp)


@app.command("show")
@cli_errors
def show(ctx: typer.Context, exp: Annotated[str, typer.Argument(help=_EXP_HELP)]) -> None:
    """Show one experiment."""
    st = state(ctx)
    with st.client() as api:
        found = resolve_experiment(api, exp)
    _report(st.output, found)


@app.command("rename")
@cli_errors
def rename(
    ctx: typer.Context,
    exp: Annotated[str, typer.Argument(help=_EXP_HELP)],
    name: Annotated[str, typer.Argument(help="New name")],
) -> None:
    """Change the name of an experiment."""
    st = state(ctx)
    with st.client() as api:
        found = resolve_experiment(api, exp)
        updated = api.experiments.update(found.id, name=name)
    _report(st.output, updated)


def _archive(ctx: typer.Context, exp: str, archived: bool) -> None:
    st = state(ctx)
    with st.client() as api:
        found = resolve_experiment(api, exp)
        updated = api.experiments.update(found.id, archived=archived)
    _report(st.output, updated)


@app.command("archive")
@cli_errors
def archive(ctx: typer.Context, exp: Annotated[str, typer.Argument(help=_EXP_HELP)]) -> None:
    """Archive an experiment. An archived experiment does not accept new runs."""
    _archive(ctx, exp, True)


@app.command("unarchive")
@cli_errors
def unarchive(ctx: typer.Context, exp: Annotated[str, typer.Argument(help=_EXP_HELP)]) -> None:
    """Make an archived experiment active again."""
    _archive(ctx, exp, False)


@app.command("rm")
@cli_errors
def rm(
    ctx: typer.Context,
    exp: Annotated[str, typer.Argument(help=_EXP_HELP)],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Delete an experiment. Its runs stay, without an experiment. Its saved views are deleted."""
    st = state(ctx)
    with st.client() as api:
        found = resolve_experiment(api, exp)
        confirm(st, f"Delete experiment {found.name!r}? Its {found.run_count} runs stay.", yes=yes)
        api.experiments.delete(found.id)
    if st.output.json_mode:
        st.output.emit_json({"deleted": found.id})
    else:
        st.output.success(f"deleted experiment {found.id}")


def _report_moves(out: Output, res: ExperimentRunsResult, exp: Experiment) -> None:
    if out.json_mode:
        out.emit_json(res)
        return
    out.success(f"experiment {exp.name}: {res.added} run(s) added, {res.removed} run(s) removed")
    if res.missing:
        out.warn("these ids are not runs of the project: " + ", ".join(res.missing))


@app.command("add")
@cli_errors
def add(
    ctx: typer.Context,
    exp: Annotated[str, typer.Argument(help=_EXP_HELP)],
    runs: Annotated[list[str], typer.Argument(help="Run ids to add")],
) -> None:
    """Add runs to an experiment. A run moves out of its previous experiment."""
    st = state(ctx)
    with st.client() as api:
        found = resolve_experiment(api, exp)
        res = api.experiments.add_runs(found.id, runs)
    _report_moves(st.output, res, found)


@app.command("remove")
@cli_errors
def remove(
    ctx: typer.Context,
    exp: Annotated[str, typer.Argument(help=_EXP_HELP)],
    runs: Annotated[list[str], typer.Argument(help="Run ids to remove")],
) -> None:
    """Remove runs from an experiment. The runs stay, without an experiment."""
    st = state(ctx)
    with st.client() as api:
        found = resolve_experiment(api, exp)
        res = api.experiments.remove_runs(found.id, runs)
    _report_moves(st.output, res, found)
