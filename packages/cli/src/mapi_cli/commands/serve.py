"""``mapi serve``: run the local API (delegates to ``mapi-server dev`` when installed)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from typing import Annotated, Final

import typer

from mapi_cli.state import cli_errors, state

SERVER_MODULE: Final = "mapi_server"


def server_command(extra: list[str]) -> list[str]:
    """``python -m mapi_server dev ...`` with the running interpreter."""
    return [sys.executable, "-m", SERVER_MODULE, "dev", *extra]


@cli_errors
def serve(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", help="Bind address")] = None,
    port: Annotated[int | None, typer.Option("--port", min=1, max=65535)] = None,
) -> None:
    """Start the local development stack (API, embedded Postgres, GCS shim).

    Extra arguments are passed through to "mapi-server dev".
    """
    out = state(ctx).output
    if importlib.util.find_spec(SERVER_MODULE) is None:
        out.err.print(
            "mapi serve needs the server: pip install mapi-server (AGPL-3.0 / commercial)",
            markup=False,
        )
        raise typer.Exit(1)
    extra: list[str] = []
    if host is not None:
        extra += ["--host", host]
    if port is not None:
        extra += ["--port", str(port)]
    extra += list(ctx.args)
    cmd = server_command(extra)
    out.info(f"starting: {' '.join(cmd[1:])}")
    try:
        code = subprocess.call(cmd, env=os.environ.copy())
    except KeyboardInterrupt:
        code = 130
    raise typer.Exit(code)
