"""``mapi webhooks ls|get|create|rotate|deliveries|replay|rm`` (v0.4).

A webhook's signing secret exists once, in the answer to ``create`` (and ``rotate``): it is
written to stdout alone (``mapi webhooks create URL -e run.succeeded > hook.secret``
captures exactly the secret) and never shown again; listings show ``whsec_…<last 4>``.
Rotating retires the current secret after 24 hours and deleting stops deliveries at once,
so both ask for confirmation (``--yes`` skips it).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from mapi_spec.api import Webhook, WebhookCreated
from mapi_spec.webhooks import DeliveryStatus, WebhookEventType
from rich.text import Text

from mapi_cli.output import Output, safe, strip_control
from mapi_cli.state import cli_errors, confirm, state

app = typer.Typer(
    help="Webhooks of the selected project (secrets are shown once, at creation).",
    no_args_is_help=True,
)

_STATUS_STYLE = {
    "succeeded": "green",
    "pending": "cyan",
    "retrying": "yellow",
    "failed": "red",
    "blocked": "red",
}


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _show(out: Output, w: Webhook) -> None:
    out.kv(
        [
            ("id", w.id),
            ("url", w.url),
            ("events", ", ".join(e.value for e in w.events)),
            ("active", safe("yes" if w.active else "no", "green" if w.active else "red")),
            ("secret", w.secret_hint),
            ("failures", w.failure_count or None),
            ("disabled", w.disabled_reason),
            ("last delivery", _when(w.last_delivery_at)),
            ("last success", _when(w.last_success_at)),
            ("description", w.description),
        ]
    )


def _print_secret(out: Output, created: WebhookCreated, verb: str) -> None:
    """The one time a secret is shown: alone on stdout (or in the --json document)."""
    if out.json_mode:
        out.emit_json(
            {"webhook": created.webhook.model_dump(mode="json"), "secret": created.secret}
        )
        return
    out.info(
        Text(f"{verb} {created.webhook.id} for {strip_control(created.webhook.url)}. ")
        + Text("Store the signing secret now: it is not shown again.", style="bold yellow")
    )
    typer.echo(created.secret)


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context, limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50
) -> None:
    """List webhooks (never their secrets)."""
    st = state(ctx)
    with st.client() as api:
        items = api.webhooks.list(limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "url", "events", "active", "failures", "last success"],
        [
            (
                w.id,
                w.url,
                ", ".join(e.value for e in w.events),
                safe("yes" if w.active else "no", "green" if w.active else "red"),
                w.failure_count,
                _when(w.last_success_at),
            )
            for w in items
        ],
    )


@app.command("get")
@cli_errors
def get(ctx: typer.Context, webhook_id: Annotated[str, typer.Argument(help="whk_… id")]) -> None:
    """Show one webhook (never its secret)."""
    st = state(ctx)
    with st.client() as api:
        w = api.webhooks.get(webhook_id)
    if st.output.json_mode:
        st.output.emit_json(w)
    else:
        _show(st.output, w)


@app.command("create")
@cli_errors
def create(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="https:// URL that receives the deliveries")],
    events: Annotated[
        list[WebhookEventType],
        typer.Option("--event", "-e", help="Event to subscribe to (repeatable)"),
    ],
    description: Annotated[str | None, typer.Option("--description")] = None,
    inactive: Annotated[
        bool, typer.Option("--inactive", help="Create it paused (no deliveries yet)")
    ] = False,
) -> None:
    """Add a webhook. Its signing secret is printed once."""
    st = state(ctx)
    with st.client() as api:
        created = api.webhooks.create(url, events, description=description, active=not inactive)
    _print_secret(st.output, created, "Created")


@app.command("rotate")
@cli_errors
def rotate(
    ctx: typer.Context,
    webhook_id: Annotated[str, typer.Argument(help="whk_… id")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """New signing secret (printed once); the current one keeps signing for 24 hours."""
    st = state(ctx)
    with st.client() as api:
        if not yes:
            current = api.webhooks.get(webhook_id)
            confirm(
                st,
                f"Rotate the secret of {current.id} ({strip_control(current.url)})? The "
                f"current secret ({current.secret_hint}) stops working in 24 hours.",
                yes=False,
            )
        created = api.webhooks.rotate_secret(webhook_id)
    _print_secret(st.output, created, "Rotated the secret of")


@app.command("deliveries")
@cli_errors
def deliveries(
    ctx: typer.Context,
    webhook_id: Annotated[str, typer.Argument(help="whk_… id")],
    status: Annotated[
        DeliveryStatus | None, typer.Option("--status", help="Only deliveries in this state")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 20,
) -> None:
    """The delivery log, newest first."""
    st = state(ctx)
    with st.client() as api:
        items = api.webhooks.deliveries(webhook_id, status=status, limit=min(limit, 200)).take(
            limit
        )
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "event", "status", "try", "code", "ms", "when", "error / response"],
        [
            (
                d.id,
                d.event_type.value,
                safe(d.status.value, _STATUS_STYLE.get(d.status.value, "")),
                d.attempt,
                d.response_code or "",
                d.duration_ms if d.duration_ms is not None else "",
                _when(d.created_at),
                # the response excerpt is the receiver's text: shown as plain text only
                safe(d.error or d.response_excerpt or ""),
            )
            for d in items
        ],
    )


@app.command("replay")
@cli_errors
def replay(
    ctx: typer.Context,
    webhook_id: Annotated[str, typer.Argument(help="whk_… id")],
    delivery_id: Annotated[str, typer.Argument(help="whd_… id")],
) -> None:
    """Send a delivery's event again (a new delivery)."""
    st = state(ctx)
    with st.client() as api:
        d = api.webhooks.replay(webhook_id, delivery_id)
    if st.output.json_mode:
        st.output.emit_json(d)
    else:
        st.output.success(f"Queued {d.id} (replay of {delivery_id})")


@app.command("rm")
@cli_errors
def rm(
    ctx: typer.Context,
    webhook_id: Annotated[str, typer.Argument(help="whk_… id")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Delete a webhook and its delivery log."""
    st = state(ctx)
    with st.client() as api:
        if not yes:
            current = api.webhooks.get(webhook_id)
            confirm(
                st,
                f"Delete {current.id} ({strip_control(current.url)}) and its delivery log?",
                yes=False,
            )
        api.webhooks.delete(webhook_id)
    if st.output.json_mode:
        st.output.emit_json({"deleted": webhook_id})
    else:
        st.output.success(f"Deleted {webhook_id}")
