"""``mapi login|logout|whoami|use``: credentials and project selection (v0.3).

* ``mapi login`` signs in with OpenID Connect in the browser (loopback redirect + PKCE) or,
  with ``--device``, with a code entered on another device. The sign-in is stored, ``0600``,
  in ``credentials.json`` next to the config file and renewed automatically.
* ``mapi login --with-token`` stores an API token (read from stdin, checksum-checked and
  verified with ``GET /v1/me``) in the profile's ``token`` key, ``0600``.
* ``mapi use <tenant>/<project>`` selects the project (``X-Mapi-Project``) for the profile.

No command prints a token or secret: tokens are shown as ``mapi_<prefix>_…``.
"""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import Annotated, Any

import typer
from mapi_client import Client, ClientConfig, ConfigError
from mapi_client.auth import describe_token
from mapi_client.config import (
    ENV_PROJECT,
    ENV_TOKEN,
    check_project,
    read_config_file,
    update_profile,
)
from mapi_client.credentials import CredentialStore, OidcSession
from mapi_client.oidc import DeviceCode
from mapi_client.oidc import login as oidc_login
from mapi_client.oidc import logout as oidc_logout
from mapi_spec.api import Me, MeProject
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import TokenPrincipal, UserPrincipal, parse_project_selector
from mapi_spec.tokens import parse_token
from rich.text import Text

from mapi_cli.output import Output, safe
from mapi_cli.state import AppState, cli_errors, state

__all__ = ["binding_conflict", "login", "logout", "use", "whoami"]


# ------------------------------------------------------------------------------- helpers


def _persist(st: AppState, values: dict[str, Any], *, unset: tuple[str, ...] = ()) -> str | None:
    """Write settings into the target profile (created when missing). Returns its name."""
    profile = st.target_profile()
    if values or unset:
        update_profile(st.config_file, profile, set_values=values, unset=unset)
    return profile


def _try_me(api: Client) -> Me | None:
    """``GET /v1/me``, or ``None`` when the server predates it (v0.2: 404)."""
    try:
        return api.me()
    except MapiError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            return None
        raise


def _who(me: Me) -> str:
    p = me.principal
    if isinstance(p, UserPrincipal):
        return p.email or p.name or p.id
    if isinstance(p, TokenPrincipal):
        return f"token {p.id} (owner {p.owner_id})"
    return p.id


def binding_conflict(me: Me, project: str | None) -> str | None:
    """A warning when the API token is bound to another project than the selected one
    (every project-scoped request would be refused with 403)."""
    if me.token is None or not project:
        return None
    sel = parse_project_selector(project)
    bound = f"{me.token.tenant_id}/{me.token.project_id}"
    if sel.project_id == me.token.project_id and sel.tenant_id in (None, me.token.tenant_id):
        return None
    return (
        f"the API token is bound to {bound} but the selected project is {project}: "
        f"run `mapi use {bound}` or mint a token in {project}"
    )


def _read_token_input() -> str:
    if sys.stdin.isatty():
        value: str = typer.prompt("API token", hide_input=True, err=True)
        return value.strip()
    line = sys.stdin.readline()
    if not line.strip():
        raise ConfigError("no token on stdin (pipe it in: mapi login --with-token < token.txt)")
    return line.strip()


# --------------------------------------------------------------------------------- login


@cli_errors
def login(
    ctx: typer.Context,
    with_token: Annotated[
        bool,
        typer.Option(
            "--with-token", help="Store an API token read from stdin (no browser sign-in)"
        ),
    ] = False,
    device: Annotated[
        bool,
        typer.Option("--device", help="Sign in with a code on another device (no local browser)"),
    ] = False,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print the sign-in URL instead of opening a browser"),
    ] = False,
    issuer: Annotated[
        str | None,
        typer.Option("--issuer", help="OIDC issuer URL (saved to the profile)"),
    ] = None,
    client_id: Annotated[
        str | None,
        typer.Option("--client-id", help="OIDC client id (saved to the profile)"),
    ] = None,
    port: Annotated[
        int, typer.Option("--port", min=0, max=65535, help="Loopback port (0: any free port)")
    ] = 0,
    timeout: Annotated[
        float, typer.Option("--timeout", min=1, help="Seconds to wait for the browser")
    ] = 300.0,
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Check the token with GET /v1/me")
    ] = True,
) -> None:
    """Sign in to the API (browser OIDC, device code, or an API token)."""
    st = state(ctx)
    out = st.output
    settings: dict[str, Any] = {}
    if st.url:
        settings["url"] = st.url
    if issuer:
        settings["oidc_issuer"] = issuer
    if client_id:
        settings["oidc_client_id"] = client_id
    if with_token:
        _login_token(st, settings, verify=verify)
        return
    profile = _persist(st, settings)
    cfg = st.config()
    if cfg.token is not None:
        where = "MAPI_TOKEN" if cfg.sources.get("token") == f"env:{ENV_TOKEN}" else "the profile"
        out.warn(
            f"an API token from {where} is configured and takes precedence over a browser "
            "sign-in; run `mapi logout` (or unset MAPI_TOKEN) to use the sign-in"
        )

    def show_url(url: str) -> None:
        if not no_browser:
            out.info("Opening the browser to sign in. If it does not open, visit:")
        out.info(safe(url, "underline"))

    def show_code(code: DeviceCode) -> None:
        out.info(
            Text("To sign in, open ")
            + safe(code.verification_uri, "underline")
            + Text(" and enter the code ")
            + safe(code.user_code, "bold")
        )
        if code.verification_uri_complete:
            out.info(Text("or visit ") + safe(code.verification_uri_complete, "underline"))
        out.info(f"(the code expires in {code.expires_in // 60} min; waiting...)")

    def open_browser(url: str) -> object:
        return None if no_browser else webbrowser.open(url)

    session = oidc_login(
        cfg,
        method="device" if device else "browser",
        open_browser=open_browser,
        show_url=show_url,
        show_code=show_code,
        timeout=timeout,
        port=port,
    )
    me = None
    if verify and cfg.token is None:
        with Client(config=cfg) as api:
            me = _try_me(api)
    _report_login(out, cfg, profile, session=session, me=me)


def _login_token(st: AppState, settings: dict[str, Any], *, verify: bool) -> None:
    out = st.output
    token = str(parse_token(_read_token_input()))  # rejects typos without echoing them
    _persist(st, settings)
    loaded = st.config()
    cfg = loaded.with_overrides(token=token)
    me = None
    if verify:
        with Client(config=cfg) as api:
            me = _try_me(api)
        if me is None:
            out.warn("the server has no /v1/me (v0.2?): the token was stored unverified")
    profile = _persist(st, {"token": token})
    _report_login(out, cfg, profile, me=me)
    if loaded.sources.get("token") == f"env:{ENV_TOKEN}":
        out.warn("MAPI_TOKEN is set in your environment and overrides the stored token")


def _report_login(
    out: Output,
    cfg: ClientConfig,
    profile: str | None,
    *,
    session: OidcSession | None = None,
    me: Me | None = None,
) -> None:
    doc: dict[str, Any] = {
        "url": cfg.base_url,
        "profile": profile,
        "method": "oidc" if session is not None else "token",
        "who": _who(me) if me is not None else (session.email if session else None),
        "project": cfg.project,
        "expires_at": session.expires_at.isoformat() if session else None,
        "token": describe_token(cfg.token) if session is None and cfg.token else None,
    }
    conflict = binding_conflict(me, cfg.project) if me is not None else None
    if conflict:
        out.warn(conflict)
    if out.json_mode:
        out.emit_json(doc)
        return
    who = doc["who"] or "(identity not reported)"
    out.success(f"Logged in to {cfg.base_url} as {who}")
    out.kv(
        [
            ("profile", profile or "<top-level>"),
            ("method", doc["method"]),
            ("token", doc["token"]),
            ("project", cfg.project or "(not selected: run `mapi use <tenant>/<project>`)"),
        ]
    )


# -------------------------------------------------------------------------------- logout


@cli_errors
def logout(
    ctx: typer.Context,
    revoke: Annotated[
        bool,
        typer.Option(
            "--revoke",
            help="Also revoke the stored API token on the server (it stops working everywhere)",
        ),
    ] = False,
) -> None:
    """Forget the stored sign-in and API token of the profile."""
    st = state(ctx)
    out = st.output
    profile = st.target_profile()
    data = read_config_file(st.config_file)
    table = data if profile is None else dict(data.get("profiles", {})).get(profile, {})
    file_token = table.get("token") if isinstance(table, dict) else None
    revoked = None
    if revoke and file_token:
        cfg = st.config().with_overrides(token=str(file_token))
        with Client(config=cfg) as api:
            me = api.me()
            if me.token is None:
                raise MapiError(
                    "the server did not identify the stored token", code=ErrorCode.NOT_FOUND
                )
            api.tokens.revoke(me.token.id)
            revoked = me.token.id
    removed_session = oidc_logout(st.config().with_overrides(token=None))
    if file_token:
        _persist(st, {}, unset=("token",))
    doc = {
        "profile": profile,
        "signed_out": removed_session is not None,
        "token_removed": bool(file_token),
        "token_revoked": revoked,
    }
    if out.json_mode:
        out.emit_json(doc)
    elif removed_session is None and not file_token:
        out.info(f"no stored credentials for profile {profile or '<top-level>'}")
    else:
        out.success(f"Logged out of profile {profile or '<top-level>'}")
        if revoked:
            out.info(f"revoked token {revoked}")
    if os.environ.get(ENV_TOKEN, "").strip():
        out.warn("MAPI_TOKEN is still set in your environment")


# -------------------------------------------------------------------------------- whoami


@cli_errors
def whoami(ctx: typer.Context) -> None:
    """Who you are, how you are signed in, and the projects you can use."""
    st = state(ctx)
    out = st.output
    cfg = st.config()
    with Client(config=cfg) as api:
        me = api.me()
    session = None
    if cfg.token is None and cfg.credentials_file is not None:
        session = CredentialStore(cfg.credentials_file).load(cfg.profile_key)
    conflict = binding_conflict(me, cfg.project)
    if conflict:
        out.warn(conflict)
    if out.json_mode:
        out.emit_json(
            {
                "url": cfg.base_url,
                "profile": cfg.profile,
                "project": cfg.project,
                "me": me,
                "credentials": "token" if cfg.token else ("oidc" if session else None),
            }
        )
        return
    p = me.principal
    pairs: list[tuple[str, object]] = [("api", cfg.base_url), ("profile", cfg.profile)]
    if isinstance(p, UserPrincipal):
        pairs += [("user", p.email or p.id), ("name", p.name), ("id", p.id)]
    elif isinstance(p, TokenPrincipal):
        pairs += [
            ("token", f"{p.id} (mapi_{p.prefix}_…)"),
            ("owner", p.owner_id),
            ("bound to", f"{p.tenant_id}/{p.project_id}"),
            ("scopes", ", ".join(p.scopes)),
        ]
    else:
        pairs += [("service", p.id)]
    pairs.append(("auth", me.auth))
    if session is not None:
        pairs.append(("session expires", session.expires_at.strftime("%Y-%m-%d %H:%M UTC")))
    if me.operator:
        pairs.append(("operator", Text("yes", style="bold magenta")))
    pairs.append(("project", cfg.project or "(none selected)"))
    out.kv(pairs)
    rows = []
    for t in me.tenants:
        if not t.projects:
            rows.append((t.tenant.id, "*", t.role.value if t.role else "-", t.tenant.status.value))
        for proj in t.projects:
            mark = "→ " if _selected(cfg.project, proj) else ""
            rows.append(
                (t.tenant.id, f"{mark}{proj.project_id}", proj.role.value, t.tenant.status.value)
            )
    if rows:
        out.table(["tenant", "project", "role", "status"], rows)


def _selected(selection: str | None, proj: MeProject) -> bool:
    if not selection:
        return False
    sel = parse_project_selector(selection)
    return sel.project_id == proj.project_id and sel.tenant_id in (None, proj.tenant_id)


# ----------------------------------------------------------------------------------- use


@cli_errors
def use(
    ctx: typer.Context,
    project: Annotated[
        str | None,
        typer.Argument(help="<tenant>/<project> (or a project id when unambiguous)"),
    ] = None,
    unset: Annotated[
        bool, typer.Option("--unset", help="Clear the project selection of the profile")
    ] = False,
    verify: Annotated[
        bool,
        typer.Option("--verify/--no-verify", help="Check membership with GET /v1/me"),
    ] = True,
) -> None:
    """Select the project commands act in (sent as X-Mapi-Project), or show it."""
    st = state(ctx)
    out = st.output
    if unset:
        profile = _persist(st, {}, unset=("project",))
        _report_use(out, None, profile)
        return
    if project is None:
        cfg = st.config()
        source = cfg.sources.get("project", "default")
        if out.json_mode:
            out.emit_json({"project": cfg.project, "source": source})
        else:
            out.kv([("project", cfg.project or "(none selected)"), ("from", source)])
        return
    selection = check_project(project)
    if verify:
        selection = _verify_selection(st, selection)
    profile = _persist(st, {"project": selection})
    _report_use(out, selection, profile)
    if os.environ.get(ENV_PROJECT, "").strip():
        out.warn("MAPI_PROJECT is set in your environment and overrides this selection")


def _verify_selection(st: AppState, selection: str) -> str:
    sel = parse_project_selector(selection)
    cfg = st.config().with_overrides(project=None)
    with Client(config=cfg) as api:
        me = api.me()
    projects = [p for t in me.tenants for p in t.projects]
    matches = [
        p
        for p in projects
        if p.project_id == sel.project_id and sel.tenant_id in (None, p.tenant_id)
    ]
    if isinstance(me.principal, TokenPrincipal):
        bound = f"{me.principal.tenant_id}/{me.principal.project_id}"
        if not matches or f"{matches[0].tenant_id}/{matches[0].project_id}" != bound:
            raise MapiError(
                f"the configured API token is bound to {bound}; tokens act in one project "
                "only (mint one in the other project, or sign in with `mapi login`)",
                code=ErrorCode.FORBIDDEN,
            )
    if not matches:
        known = ", ".join(f"{p.tenant_id}/{p.project_id}" for p in projects) or "none"
        raise MapiError(
            f"you are not a member of a project {selection!r} (yours: {known})",
            code=ErrorCode.NOT_FOUND,
        )
    if len(matches) > 1:
        options = ", ".join(f"{p.tenant_id}/{p.project_id}" for p in matches)
        raise MapiError(
            f"{selection!r} is ambiguous; name the tenant: {options}",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return f"{matches[0].tenant_id}/{matches[0].project_id}"


def _report_use(out: Output, selection: str | None, profile: str | None) -> None:
    if out.json_mode:
        out.emit_json({"project": selection, "profile": profile})
    elif selection is None:
        out.success(f"Cleared the project of profile {profile or '<top-level>'}")
    else:
        out.success(f"Using project {selection} (profile {profile or '<top-level>'})")
