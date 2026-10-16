"""``mapi signup`` and ``mapi verify-email`` (v0.4): self-service sign-up.

Both are public calls: no credential is sent. The server answers a sign-up the same way for
new and known addresses; the e-mail it sends carries a link with a ``mev_…`` token that
``mapi verify-email`` (or the link itself) redeems, creating a public-tier workspace whose
first project is ``<tenant>/default``. Then ``mapi login`` signs in.
"""

from __future__ import annotations

from typing import Annotated

import typer
from mapi_spec.trust import TERMS_VERSION

from mapi_cli.output import safe
from mapi_cli.state import cli_errors, state


@cli_errors
def signup(
    ctx: typer.Context,
    email: Annotated[str, typer.Option("--email", help="Your work e-mail address")],
    name: Annotated[str, typer.Option("--name", help="Your name")],
    organisation: Annotated[
        str | None, typer.Option("--organisation", help="Names the new workspace")
    ] = None,
    accept_terms: Annotated[
        bool,
        typer.Option("--accept-terms", help=f"Accept the terms of service ({TERMS_VERSION})"),
    ] = False,
) -> None:
    """Create a workspace: sends a verification e-mail (valid 24 hours)."""
    st = state(ctx)
    out = st.output
    if not accept_terms:
        accept_terms = typer.confirm(
            f"Do you accept the terms of service ({TERMS_VERSION})?", default=False, err=True
        )
        if not accept_terms:
            out.fail("TERMS_NOT_ACCEPTED", "aborted", "the terms must be accepted to sign up")
            raise typer.Exit(1)
    with st.client() as api:
        got = api.signup(email, name, organisation=organisation, accepted_terms=True)
    if out.json_mode:
        out.emit_json(got)
        return
    hours = got.expires_in_s // 3600
    out.success(
        f"Check {email} for a verification link (valid {hours} hours), then run "
        "`mapi verify-email <token>` or open the link."
    )


@cli_errors
def verify_email(
    ctx: typer.Context,
    token: Annotated[str, typer.Argument(help="The mev_… token from the e-mail link")],
) -> None:
    """Redeem a verification token: creates your workspace and its default project."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        got = api.verify_email(token.strip())
    if out.json_mode:
        out.emit_json(got)
        return
    out.kv(
        [
            ("e-mail", got.email),
            ("workspace", got.tenant_id),
            ("project", f"{got.tenant_id}/{got.project_id}"),
            ("tier", safe(got.trust_tier.value)),
        ]
    )
    out.success(
        f"Verified. Sign in with `mapi login`, then `mapi use {got.tenant_id}/{got.project_id}`."
    )
