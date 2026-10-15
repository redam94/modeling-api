"""``mapi``: the modeling-api command line.

Local commands (``new``, ``check``, ``fit``, ``score``, ``catalog``, ``plugin``, ``spec``)
need no server; remote commands talk to the API configured with ``--url``/``MAPI_URL``,
``--token``/``MAPI_TOKEN`` or a profile in ``~/.config/mapi/config.toml``.
``--json`` prints machine-readable output on stdout.
"""

from __future__ import annotations

from typing import Annotated

import typer

from mapi_cli import __version__
from mapi_cli.commands import catalog, check, config, datasets, fit, new, plugin, runs, score, serve
from mapi_cli.commands import spec as spec_cmd
from mapi_cli.output import Output
from mapi_cli.state import AppState

HELP = (
    "The modeling-api command line.\n\n"
    "Local commands (new, check, fit, score, catalog, plugin, spec) need no server. Remote "
    "commands (runs, datasets, --remote) use the API from --url, MAPI_URL or a profile in "
    "~/.config/mapi/config.toml, with --token or MAPI_TOKEN. --json prints machine-readable "
    "output on stdout."
)

app = typer.Typer(
    name="mapi",
    help=HELP,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"mapi {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    url: Annotated[
        str | None, typer.Option("--url", help="API base URL (default: MAPI_URL, profile, local)")
    ] = None,
    token: Annotated[
        str | None, typer.Option("--token", help="Bearer token (default: MAPI_TOKEN, profile)")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Config profile (default: MAPI_PROFILE)")
    ] = None,
    json_mode: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON on stdout")
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version"),
    ] = False,
) -> None:
    ctx.obj = AppState(
        url=url, token=token, profile=profile, json_mode=json_mode, output=Output(json_mode)
    )


app.command("new")(new.new)
app.command("check")(check.check)
app.command("fit")(fit.fit)
app.command("score")(score.score)
app.command("catalog")(catalog.catalog)
app.command("serve", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(
    serve.serve
)
app.add_typer(runs.app, name="runs")
app.add_typer(datasets.app, name="datasets")
app.add_typer(plugin.app, name="plugin")
app.add_typer(spec_cmd.app, name="spec")
app.add_typer(config.app, name="config")


def run() -> None:  # pragma: no cover - console script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
