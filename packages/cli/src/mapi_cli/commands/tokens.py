"""``mapi tokens ls|create|get|revoke``: API tokens of the selected project (v0.3).

A token's secret exists once, in the answer to ``create``: it is written to stdout alone (so
``mapi tokens create ci -s runs:create > ci.token`` captures exactly the token) unless
``--save`` stores it in the profile instead. Listings show ``mapi_<prefix>_…`` only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from mapi_client.config import update_profile
from mapi_spec.api import ApiToken
from mapi_spec.tokens import DEFAULT_TOKEN_TTL_DAYS, MAX_TOKEN_TTL_DAYS, BudgetPeriod, TokenStatus
from rich.text import Text

from mapi_cli.output import Output, safe, strip_control
from mapi_cli.state import cli_errors, confirm, state

app = typer.Typer(
    help="API tokens of the selected project (secrets are shown once, at creation).",
    no_args_is_help=True,
)

_STATUS_STYLE = {"active": "green", "expired": "yellow", "revoked": "red"}


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _limits(t: ApiToken) -> str:
    parts = []
    if t.rate_limit_per_minute:
        parts.append(f"{t.rate_limit_per_minute}/min")
    if t.vcpu_seconds_budget:
        parts.append(f"{t.vcpu_seconds_budget:g} vCPU-s/{t.budget_period.value}")
    return ", ".join(parts) or "-"


def _show(out: Output, t: ApiToken) -> None:
    out.kv(
        [
            ("id", t.id),
            ("name", t.name),
            ("token", t.display),
            ("project", f"{t.tenant_id}/{t.project_id}"),
            ("owner", t.owner_id),
            ("scopes", ", ".join(t.scopes)),
            ("status", safe(t.status.value, _STATUS_STYLE.get(t.status.value, ""))),
            ("limits", _limits(t)),
            ("created", _when(t.created_at)),
            ("expires", _when(t.expires_at)),
            ("last used", _when(t.last_used_at)),
            ("revoked", _when(t.revoked_at) if t.revoked_at else None),
        ]
    )


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    all_owners: Annotated[
        bool, typer.Option("--all", help="Everyone's tokens in the project (project admins)")
    ] = False,
    status: Annotated[
        TokenStatus | None, typer.Option("--status", help="active, expired or revoked")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """List tokens (yours, or everyone's with --all)."""
    st = state(ctx)
    with st.client() as api:
        items = api.tokens.list(all_owners=all_owners, status=status, limit=min(limit, 200)).take(
            limit
        )
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "name", "token", "scopes", "status", "expires", "last used"],
        [
            (
                t.id,
                t.name,
                t.display,
                ", ".join(t.scopes),
                safe(t.status.value, _STATUS_STYLE.get(t.status.value, "")),
                _when(t.expires_at),
                _when(t.last_used_at),
            )
            for t in items
        ],
    )


@app.command("get")
@cli_errors
def get(ctx: typer.Context, token_id: Annotated[str, typer.Argument(help="tok_… id")]) -> None:
    """Show one token (never its secret)."""
    st = state(ctx)
    with st.client() as api:
        token = api.tokens.get(token_id)
    if st.output.json_mode:
        st.output.emit_json(token)
    else:
        _show(st.output, token)


@app.command("create")
@cli_errors
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="A name to recognise the token by")],
    scopes: Annotated[
        list[str],
        typer.Option(
            "--scope",
            "-s",
            help="models:read, runs:create, score:<model id> (score:* for every model), "
            "datasets:write, plugins:publish, admin (repeatable)",
        ),
    ],
    expires_in_days: Annotated[
        int,
        typer.Option("--expires-in-days", min=1, max=MAX_TOKEN_TTL_DAYS, help="Lifetime"),
    ] = DEFAULT_TOKEN_TTL_DAYS,
    rate_limit: Annotated[
        int | None, typer.Option("--rate-limit", min=1, help="Requests per minute")
    ] = None,
    budget: Annotated[
        float | None, typer.Option("--budget", min=0.001, help="vCPU-seconds per period")
    ] = None,
    budget_period: Annotated[
        BudgetPeriod, typer.Option("--budget-period", help="day or month")
    ] = BudgetPeriod.MONTH,
    save: Annotated[
        bool,
        typer.Option("--save", help="Store the token in the profile instead of printing it"),
    ] = False,
) -> None:
    """Mint a token in the selected project. Its secret is shown once."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        created = api.tokens.create(
            name,
            scopes,
            expires_in_days=expires_in_days,
            rate_limit_per_minute=rate_limit,
            vcpu_seconds_budget=budget,
            budget_period=budget_period,
        )
    token = created.token
    if save:
        profile = st.target_profile()
        update_profile(st.config_file, profile, set_values={"token": created.secret})
        if out.json_mode:
            out.emit_json({"token": token, "saved_to_profile": profile})
        else:
            _show(out, token)
            out.success(f"Saved {token.display} to profile {profile or '<top-level>'}")
        return
    if out.json_mode:
        out.emit_json(created.model_dump(mode="json"))  # explicitly requested: secret included
        return
    out.info(
        Text(f"Created {token.id} ({token.display}) in {token.tenant_id}/{token.project_id}, ")
        + Text(f"expires {_when(token.expires_at)}. ")
        + Text("Store the token now: it is not shown again.", style="bold yellow")
    )
    typer.echo(created.secret)


@app.command("revoke")
@cli_errors
def revoke(
    ctx: typer.Context,
    token_id: Annotated[str, typer.Argument(help="tok_… id")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Revoke a token: programs using it stop working at once."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        if not yes:
            current = api.tokens.get(token_id)
            confirm(
                st,
                f"Revoke {strip_control(current.name)!r} ({current.display}, "
                f"{current.tenant_id}/{current.project_id})?",
                yes=False,
            )
        api.tokens.revoke(token_id)
    if out.json_mode:
        out.emit_json({"revoked": token_id})
    else:
        out.success(f"Revoked {token_id}")
