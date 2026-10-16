"""``mapi tenant status|export|delete`` (v0.4): verification, GDPR export and deletion of
the whole tenant (workspace).

The tenant is ``--tenant``, else the tenant of the selected project (``mapi use``), else your
only tenant. Deleting is irreversible after the grace period, so it asks you to type the
tenant id; scripts pass ``--confirm <tenant id>`` instead.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from mapi_client import Client
from mapi_spec.api import TenantDeletion
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.trust import EXPORT_PARTS, RETAINED_RECORDS, ExportPart
from rich.text import Text

from mapi_cli.output import Output, human_duration, safe
from mapi_cli.state import EXIT_FAILURE, AppState, cli_errors, state

app = typer.Typer(
    help="Your workspace (tenant): verification status, GDPR export and deletion.",
    no_args_is_help=True,
)

TenantOpt = Annotated[
    str | None,
    typer.Option("--tenant", "-t", help="Tenant id (default: the selected project's tenant)"),
]
_PARTS: dict[str, ExportPart] = {p: p for p in EXPORT_PARTS}
_DELETION_STYLE = {
    "scheduled": "yellow",
    "cancelled": "green",
    "deleting": "red",
    "purged": "red",
    "failed": "red",
}


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "-"


def _tenant(api: Client, explicit: str | None) -> str:
    if explicit:
        return explicit
    if api.project:
        return api.project.split("/", 1)[0]
    tenants = api.me().tenants
    if len(tenants) == 1:
        return tenants[0].tenant.id
    names = ", ".join(t.tenant.id for t in tenants) or "none"
    raise ValueError(f"pick a tenant with --tenant (yours: {names})")


def _deletion(api: Client, tenant_id: str) -> TenantDeletion | None:
    try:
        return api.tenants.deletion(tenant_id)
    except MapiError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            return None
        raise


def _show_deletion(out: Output, d: TenantDeletion) -> None:
    out.kv(
        [
            ("deletion", d.id),
            ("state", safe(d.state.value, _DELETION_STYLE.get(d.state.value, ""))),
            ("requested", f"{_when(d.requested_at)} by {d.requested_by or '-'}"),
            ("purge after", _when(d.purge_after) if d.state.value == "scheduled" else None),
            ("completed", _when(d.completed_at) if d.completed_at else None),
            ("cancelled", _when(d.cancelled_at) if d.cancelled_at else None),
        ]
    )
    if d.stores:
        out.table(
            ["store", "state", "done", "detail"],
            [
                (
                    s.store,
                    s.state,
                    f"{s.items_done}/{s.items_total}" if s.items_total is not None else "",
                    s.detail or "",
                )
                for s in d.stores
            ],
        )


@app.command("status")
@cli_errors
def status(ctx: typer.Context, tenant: TenantOpt = None) -> None:
    """Verification (e-mail, identity, code upload) and any deletion of the tenant."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        tenant_id = _tenant(api, tenant)
        ver = api.tenants.verification(tenant_id)
        deletion = _deletion(api, tenant_id)
    if out.json_mode:
        out.emit_json({"verification": ver, "deletion": deletion})
        return
    out.kv(
        [
            ("tenant", tenant_id),
            ("tier", ver.trust_tier.value),
            ("e-mail verified", "yes" if ver.email_verified else "no"),
            ("identity verified", "yes" if ver.identity_verified else "no"),
            ("uploaded code", "allowed" if ver.uploaded_code_allowed else "not allowed"),
            ("why", ver.reason_plain),
        ]
    )
    if deletion is not None:
        _show_deletion(out, deletion)


@app.command("export")
@cli_errors
def export(
    ctx: typer.Context,
    tenant: TenantOpt = None,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="metadata, datasets, models, audit, usage (repeatable)"),
    ] = None,
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="Archive path (default: <tenant>-export.zip)")
    ] = None,
    no_wait: Annotated[
        bool, typer.Option("--no-wait", help="Start the export and print its id only")
    ] = False,
    export_id: Annotated[
        str | None, typer.Option("--id", help="Download an export started earlier (exp_…)")
    ] = None,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Give up after N s")] = 3600,
) -> None:
    """GDPR export: everything the tenant stored, as one zip archive."""
    st = state(ctx)
    out = st.output
    parts: list[ExportPart] | None = None
    if include:
        bad = sorted(set(include) - set(_PARTS))
        if bad:
            raise ValueError(f"unknown --include {bad}; use {list(_PARTS)}")
        parts = [_PARTS[p] for p in include]
    with st.client() as api:
        tenant_id = _tenant(api, tenant)
        if export_id is None:
            started = api.tenants.export(tenant_id, include=parts)
            export_id = started.id
            if no_wait:
                if out.json_mode:
                    out.emit_json(started)
                else:
                    out.success(f"Started {started.id}; download it with --id {started.id}")
                return
            out.info(f"exporting {tenant_id} ({started.id}); this can take a while")
        dest = out_file or Path(f"{tenant_id}-export.zip")
        path = api.tenants.download_export(tenant_id, export_id, dest, timeout=timeout)
        final = api.tenants.export_status(tenant_id, export_id)
    if out.json_mode:
        out.emit_json({"export": final, "out": str(path)})
        return
    out.success(f"wrote {path} ({final.size_bytes or path.stat().st_size} bytes)")


def _confirm_deletion(st: AppState, tenant_id: str, confirm_id: str | None) -> None:
    out = st.output
    if confirm_id is not None:
        if confirm_id != tenant_id:
            out.fail("ABORTED", "aborted", f"--confirm {confirm_id!r} is not {tenant_id!r}")
            raise typer.Exit(EXIT_FAILURE)
        return
    out.info(
        Text(f"Deleting tenant {tenant_id} suspends it now and, after the grace period, ")
        + Text("purges every project, dataset, model, endpoint and webhook. ", style="bold")
        + Text(
            "Kept for legal reasons: " + "; ".join(f"{k}: {v}" for k, v in RETAINED_RECORDS.items())
        )
    )
    typed = typer.prompt(
        f"Type the tenant id ({tenant_id}) to confirm", default="", show_default=False, err=True
    )
    if typed.strip() != tenant_id:
        out.fail("ABORTED", "aborted", "nothing was changed")
        raise typer.Exit(EXIT_FAILURE)


@app.command("delete")
@cli_errors
def delete(
    ctx: typer.Context,
    tenant: TenantOpt = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why (kept with the record)")
    ] = None,
    confirm_id: Annotated[
        str | None,
        typer.Option("--confirm", help="The tenant id, to delete without the prompt (scripts)"),
    ] = None,
    cancel: Annotated[
        bool, typer.Option("--cancel", help="Cancel a scheduled deletion instead")
    ] = False,
) -> None:
    """Schedule deletion of the whole tenant (cancellable during the grace period)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        tenant_id = _tenant(api, tenant)
        if cancel:
            got = api.tenants.cancel_deletion(tenant_id)
        else:
            _confirm_deletion(st, tenant_id, confirm_id)
            got = api.tenants.request_deletion(tenant_id, confirm=tenant_id, reason=reason)
    if out.json_mode:
        out.emit_json(got)
        return
    _show_deletion(out, got)
    if cancel:
        out.success(f"Cancelled the deletion of {tenant_id}")
        return
    grace = (got.purge_after - got.requested_at).total_seconds()
    out.success(
        f"Deletion of {tenant_id} scheduled: purged after {_when(got.purge_after)} "
        f"(in {human_duration(grace)}). Cancel with `mapi tenant delete --cancel`."
    )
