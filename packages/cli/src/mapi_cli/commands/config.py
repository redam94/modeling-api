"""``mapi config show|path``: the resolved connection settings."""

from __future__ import annotations

import typer
from mapi_client.config import config_path

from mapi_cli.state import cli_errors, state

app = typer.Typer(help="Client configuration (~/.config/mapi/config.toml).", no_args_is_help=True)


@app.command("show")
@cli_errors
def show(ctx: typer.Context) -> None:
    """Resolved URL, profile and where each value came from (the token is never shown)."""
    st = state(ctx)
    cfg = st.config()
    doc: dict[str, object] = {
        "url": cfg.base_url,
        "profile": cfg.profile,
        "token": "set" if cfg.token else None,
        "timeout": cfg.timeout,
        "max_retries": cfg.max_retries,
        "config_file": str(config_path()),
    }
    if st.output.json_mode:
        st.output.emit_json({**doc, "sources": dict(cfg.sources)})
        return
    sources = ", ".join(f"{k}={v}" for k, v in cfg.sources.items())
    st.output.kv([*doc.items(), ("sources", sources)])


@app.command("path")
def path() -> None:
    """Print the config file location."""
    typer.echo(str(config_path()))
