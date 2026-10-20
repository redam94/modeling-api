"""``mapi``: the modeling-api command line.

Local commands (``new``, ``check``, ``fit``, ``score``, ``catalog``, ``plugin``, ``spec``)
need no server; remote commands talk to the API configured with ``--url``/``MAPI_URL``,
``--token``/``MAPI_TOKEN``, ``mapi login`` or a profile in ``~/.config/mapi/config.toml``,
in the project chosen with ``mapi use``/``--project``/``MAPI_PROJECT``.
``--json`` prints machine-readable output on stdout.
"""

from __future__ import annotations

from typing import Annotated

import typer

from mapi_cli import __version__
from mapi_cli.commands import (
    admin,
    auth,
    catalog,
    check,
    choose,
    config,
    datasets,
    endpoints,
    experiments,
    explore,
    fit,
    governance,
    guided,
    new,
    plots,
    plugin,
    priors,
    runs,
    score,
    serve,
    signup,
    tenant,
    tokens,
    usage,
    webhooks,
    workflows,
)
from mapi_cli.commands import spec as spec_cmd
from mapi_cli.output import Output
from mapi_cli.state import AppState

HELP = (
    "The modeling-api command line.\n\n"
    "Local commands (new, check, fit, score, catalog, plugin, spec) need no server. Remote "
    "commands (runs, datasets, tokens, usage, admin, endpoints, webhooks, plots, guided, "
    "priors, tenant, signup, workflows, governance, validate, validations, approve, explore, "
    "optimize, choose, about, --remote) use the API from --url, "
    "MAPI_URL or a profile in ~/.config/mapi/config.toml, signed in with `mapi login` (or "
    "--token / MAPI_TOKEN), in the project chosen with `mapi use` (or --project / "
    "MAPI_PROJECT). --json prints machine-readable output on stdout."
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
    project: Annotated[
        str | None,
        typer.Option(
            "--project", help="<tenant>/<project> to act in (default: MAPI_PROJECT, mapi use)"
        ),
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
        url=url,
        token=token,
        profile=profile,
        json_mode=json_mode,
        output=Output(json_mode),
        project=project,
    )


app.command("new")(new.new)
app.command("check")(check.check)
app.command("fit")(fit.fit)
app.command("score")(score.score)
app.command("catalog")(catalog.catalog)
app.command("login")(auth.login)
app.command("logout")(auth.logout)
app.command("whoami")(auth.whoami)
app.command("use")(auth.use)
app.command("usage")(usage.usage)
app.command("signup")(signup.signup)
app.command("verify-email")(signup.verify_email)
app.command("serve", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(
    serve.serve
)
app.add_typer(runs.app, name="runs")
app.add_typer(experiments.app, name="experiments")
app.add_typer(datasets.app, name="datasets")
app.add_typer(plugin.app, name="plugin")
app.add_typer(spec_cmd.app, name="spec")
app.add_typer(config.app, name="config")
app.add_typer(tokens.app, name="tokens")
app.add_typer(admin.app, name="admin")
app.add_typer(endpoints.app, name="endpoints")
app.add_typer(webhooks.app, name="webhooks")
app.add_typer(plots.app, name="plots")
app.add_typer(guided.app, name="guided")
app.add_typer(priors.app, name="priors")
app.add_typer(tenant.app, name="tenant")
# v0.5: workflows, governance, explorers, optimisation, About cards
app.add_typer(workflows.app, name="workflows")
app.add_typer(governance.app, name="governance")
app.add_typer(governance.validate_app, name="validate")
app.add_typer(governance.validations, name="validations")
app.command("approve")(governance.approve)
app.add_typer(explore.app, name="explore")
app.command("optimize")(explore.optimize)
app.command("choose")(choose.choose)
app.command("about")(choose.about)


def run() -> None:  # pragma: no cover - console script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
