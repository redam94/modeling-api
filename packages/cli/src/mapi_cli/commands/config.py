"""``mapi config show|path|set|unset``: the connection settings and the config file.

Secrets are never shown: an API token appears as ``mapi_<prefix>_…``, a sign-in as the
signed-in identity and its expiry. ``set`` refuses secret keys (use ``mapi login
--with-token`` for tokens, ``MAPI_OIDC_CLIENT_SECRET`` for an OIDC client secret).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import typer
from mapi_client import ClientConfig
from mapi_client.auth import describe_token
from mapi_client.config import (
    PROFILE_KEYS,
    SECRET_KEYS,
    check_issuer,
    check_project,
    config_path,
    update_profile,
)
from mapi_client.credentials import CredentialStore

from mapi_cli.state import cli_errors, state

app = typer.Typer(help="Client configuration (~/.config/mapi/config.toml).", no_args_is_help=True)

SETTABLE: Final = tuple(sorted(PROFILE_KEYS - SECRET_KEYS))


def _parse_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("true", "yes", "on", "1"):
        return True
    if value in ("false", "no", "off", "0"):
        return False
    raise ValueError(f"not a boolean: {text!r}")


def _parse(key: str, text: str) -> Any:
    if key == "url":
        return ClientConfig(base_url=text).base_url
    if key == "project":
        return check_project(text)
    if key == "oidc_issuer":
        return check_issuer(text)
    if key == "timeout":
        value = float(text)
        if value <= 0:
            raise ValueError("timeout must be positive")
        return value
    if key == "max_retries":
        retries = int(text)
        if retries < 0:
            raise ValueError("max_retries must be >= 0")
        return retries
    if key == "verify":
        return _parse_bool(text)
    if not text.strip():
        raise ValueError(f"{key} must not be empty")
    return text.strip()


@app.command("show")
@cli_errors
def show(ctx: typer.Context) -> None:
    """Resolved settings and where each came from (secrets are never shown)."""
    st = state(ctx)
    cfg = st.config()
    session = None
    if cfg.token is None and cfg.credentials_file is not None:
        stored = CredentialStore(cfg.credentials_file).load(cfg.profile_key)
        session = stored if stored is not None and stored.api_url == cfg.base_url else None
    if cfg.token:
        auth = f"token {describe_token(cfg.token)}"
    elif session is not None:
        state_ = "expired, renews on use" if session.expired() else "valid"
        auth = (
            f"sign-in {session.email or session.subject} ({state_}; "
            f"until {session.expires_at:%Y-%m-%d %H:%M} UTC)"
        )
    else:
        auth = None
    doc: dict[str, object] = {
        "url": cfg.base_url,
        "profile": cfg.profile,
        "project": cfg.project,
        "auth": auth,
        "token": "set" if cfg.token else None,
        "oidc_issuer": cfg.oidc_issuer,
        "oidc_client_id": cfg.oidc_client_id,
        "timeout": cfg.timeout,
        "max_retries": cfg.max_retries,
        "config_file": str(config_path()),
        "credentials_file": str(cfg.credentials_file) if cfg.credentials_file else None,
    }
    if st.output.json_mode:
        st.output.emit_json({**doc, "sources": dict(cfg.sources)})
        return
    sources = ", ".join(f"{k}={v}" for k, v in cfg.sources.items() if v != "default")
    st.output.kv([*((k, v) for k, v in doc.items() if k != "token"), ("sources", sources)])


@app.command("path")
def path() -> None:
    """Print the config file location."""
    typer.echo(str(config_path()))


@app.command("set")
@cli_errors
def set_value(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=f"One of: {', '.join(SETTABLE)}")],
    value: Annotated[str, typer.Argument()],
) -> None:
    """Set a key of the profile (--profile, MAPI_PROFILE, else the default profile)."""
    st = state(ctx)
    if key in SECRET_KEYS:
        raise ValueError(
            f"{key} is a secret and is not set on the command line: use `mapi login "
            "--with-token` for tokens, MAPI_OIDC_CLIENT_SECRET for the OIDC client secret"
        )
    if key not in SETTABLE:
        raise ValueError(f"unknown key {key!r} (one of: {', '.join(SETTABLE)})")
    parsed = _parse(key, value)
    profile = st.target_profile()
    update_profile(st.config_file, profile, set_values={key: parsed})
    if st.output.json_mode:
        st.output.emit_json({"profile": profile, key: parsed})
    else:
        st.output.success(f"{key} = {parsed} (profile {profile or '<top-level>'})")


@app.command("unset")
@cli_errors
def unset_value(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="A key to remove from the profile")],
) -> None:
    """Remove a key from the profile (``token`` too: prefer ``mapi logout``)."""
    st = state(ctx)
    if key not in PROFILE_KEYS:
        raise ValueError(f"unknown key {key!r}")
    profile = st.target_profile()
    update_profile(st.config_file, profile, unset=[key])
    if st.output.json_mode:
        st.output.emit_json({"profile": profile, "unset": key})
    else:
        st.output.success(f"removed {key} (profile {profile or '<top-level>'})")
