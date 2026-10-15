"""``mapi admin ...``: platform operator commands (v0.3, ``/v1/admin``).

Every change asks for confirmation (``--yes`` to skip, e.g. in a runbook script) after
showing what will change; the server audits it. Kill-switch changes need ``--reason``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

import typer
from mapi_client import Client
from mapi_spec.admin import KillSwitches, KillSwitchUpdate
from mapi_spec.api import AuditEvent, ExecutorHealth, Tenant, TenantAdminUpdate, TenantCreate
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.cost import MICROS_PER_UNIT
from mapi_spec.quotas import QuotaLimits, QuotaPolicy, QuotaScope
from mapi_spec.resources import ResourceClass
from mapi_spec.tenancy import TenantStatus, TrustTier
from rich.text import Text

from mapi_cli.output import Output, money, safe, strip_control
from mapi_cli.state import cli_errors, confirm, state

app = typer.Typer(
    help="Platform operator commands: tenants, quotas, kill switches, executors, audit.",
    no_args_is_help=True,
)
tenants_app = typer.Typer(help="All tenants (create, change tier/status/residency).")
quotas_app = typer.Typer(help="Quota policies per token, tenant or the platform.")
switches_app = typer.Typer(help="Platform-wide emergency controls (audited).")
executors_app = typer.Typer(help="Executor health, capacity and conformance.")
app.add_typer(tenants_app, name="tenants", no_args_is_help=True)
app.add_typer(quotas_app, name="quotas", no_args_is_help=True)
app.add_typer(switches_app, name="kill-switches", no_args_is_help=True)
app.add_typer(executors_app, name="executors", no_args_is_help=True)

Yes = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")]
Limit = Annotated[int, typer.Option("--limit", "-n", min=1, help="At most N rows")]

_STATUS_STYLE = {"active": "green", "suspended": "yellow", "deleting": "red"}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B|B)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
}


def parse_size(text: str) -> int:
    """``"100GiB"`` → bytes (``B``, ``KB``…``TB`` decimal, ``KiB``…``TiB`` binary)."""
    m = _SIZE_RE.fullmatch(text)
    if m is None:
        raise ValueError(f"not a size: {text!r} (e.g. 500MiB, 100GiB)")
    unit = (m[2] or "B").lower()
    return int(Decimal(m[1]) * _SIZE_UNITS[unit])


def parse_amount(text: str) -> int:
    """``"500"`` (currency units) → micros, exactly."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not an amount: {text!r}") from None
    if value < 0 or value != value.quantize(Decimal("0.000001")):
        raise ValueError("amounts are >= 0 with at most 6 decimals")
    return int(value * MICROS_PER_UNIT)


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


# =================================================================================== tenants


def _tenant_kv(out: Output, t: Tenant) -> None:
    out.kv(
        [
            ("id", t.id),
            ("name", t.name),
            ("trust tier", t.trust_tier.value),
            ("status", safe(t.status.value, _STATUS_STYLE.get(t.status.value, ""))),
            ("reason", t.status_reason),
            ("data residency", t.data_residency),
            ("created", _when(t.created_at)),
            ("updated", _when(t.updated_at)),
        ]
    )


@tenants_app.command("ls")
@cli_errors
def tenants_ls(
    ctx: typer.Context,
    status: Annotated[TenantStatus | None, typer.Option("--status")] = None,
    tier: Annotated[TrustTier | None, typer.Option("--tier")] = None,
    limit: Limit = 100,
) -> None:
    """List all tenants."""
    st = state(ctx)
    with st.client() as api:
        items = api.admin.tenants(status=status, trust_tier=tier, limit=min(limit, 200)).take(limit)
    if st.output.json_mode:
        st.output.emit_json(items)
        return
    st.output.table(
        ["id", "name", "tier", "status", "residency", "created"],
        [
            (
                t.id,
                t.name,
                t.trust_tier.value,
                safe(t.status.value, _STATUS_STYLE.get(t.status.value, "")),
                t.data_residency,
                _when(t.created_at),
            )
            for t in items
        ],
    )


@tenants_app.command("get")
@cli_errors
def tenants_get(ctx: typer.Context, tenant_id: Annotated[str, typer.Argument()]) -> None:
    """Show one tenant."""
    st = state(ctx)
    with st.client() as api:
        tenant = api.admin.tenant(tenant_id)
    if st.output.json_mode:
        st.output.emit_json(tenant)
    else:
        _tenant_kv(st.output, tenant)


@tenants_app.command("create")
@cli_errors
def tenants_create(
    ctx: typer.Context,
    tenant_id: Annotated[str, typer.Argument(help="A lowercase slug, e.g. acme")],
    name: Annotated[str, typer.Option("--name", help="Display name")],
    tier: Annotated[TrustTier, typer.Option("--tier")] = TrustTier.VERIFIED,
    residency: Annotated[str, typer.Option("--residency", help="eu, us, asia or a region")] = "us",
    admin_email: Annotated[
        str | None, typer.Option("--admin-email", help="Invite this address as tenant admin")
    ] = None,
    yes: Yes = False,
) -> None:
    """Create a tenant."""
    st = state(ctx)
    body = TenantCreate(
        id=tenant_id,
        name=name,
        trust_tier=tier,
        data_residency=residency,
        initial_admin_email=admin_email,
    )
    confirm(
        st,
        f"Create tenant {tenant_id!r} ({tier.value}, residency {residency})"
        + (f", inviting {admin_email} as admin" if admin_email else "")
        + "?",
        yes=yes,
    )
    with st.client() as api:
        tenant = api.admin.create_tenant(body)
    if st.output.json_mode:
        st.output.emit_json(tenant)
    else:
        _tenant_kv(st.output, tenant)


@tenants_app.command("update")
@cli_errors
def tenants_update(
    ctx: typer.Context,
    tenant_id: Annotated[str, typer.Argument()],
    name: Annotated[str | None, typer.Option("--name")] = None,
    tier: Annotated[TrustTier | None, typer.Option("--tier")] = None,
    status: Annotated[
        TenantStatus | None,
        typer.Option("--status", help="suspended: read-only; deleting: no access"),
    ] = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Required to suspend or delete")
    ] = None,
    residency: Annotated[
        str | None, typer.Option("--residency", help="Applies to new runs only")
    ] = None,
    yes: Yes = False,
) -> None:
    """Change a tenant's name, trust tier, status or data residency."""
    st = state(ctx)
    fields: dict[str, Any] = {
        k: v
        for k, v in (
            ("name", name),
            ("trust_tier", tier),
            ("status", status),
            ("status_reason", reason),
            ("data_residency", residency),
        )
        if v is not None
    }
    body = TenantAdminUpdate.model_validate(fields)
    with st.client() as api:
        current = api.admin.tenant(tenant_id)
        changes = [
            (key, getattr(current, key), value)
            for key, value in body.model_dump(exclude_unset=True).items()
        ]
        _show_changes(st.output, f"tenant {tenant_id}", changes)
        warning = ""
        if status is TenantStatus.SUSPENDED:
            warning = " Its members will only be able to read."
        elif status is TenantStatus.DELETING:
            warning = " Its members lose all access."
        confirm(st, f"Apply these changes to tenant {tenant_id!r}?{warning}", yes=yes)
        tenant = api.admin.update_tenant(tenant_id, body)
    if st.output.json_mode:
        st.output.emit_json(tenant)
    else:
        _tenant_kv(st.output, tenant)


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, StrEnum):
        return value.value
    return strip_control(str(value))


def _show_changes(out: Output, what: str, changes: list[tuple[str, object, object]]) -> None:
    """Print a before → after table on stderr (the confirmation context)."""
    if out.json_mode or not changes:
        return
    err = Output(json_mode=False, out=out.err, err=out.err)
    err.table(
        ["setting", "now", "after"],
        [(k, _fmt(before), Text(_fmt(after), style="bold")) for k, before, after in changes],
        title=f"Changes to {what}",
    )


# ==================================================================================== quotas

_LIMIT_LABELS = {
    "vcpu_seconds_per_day": "vCPU-s/day",
    "vcpu_seconds_per_month": "vCPU-s/month",
    "concurrent_runs": "concurrent runs",
    "max_fanout_tasks": "fan-out tasks",
    "storage_bytes": "storage",
    "egress_bytes_per_month": "egress/month",
    "requests_per_minute": "requests/min",
    "max_resource_class": "max class",
}


def _limits_text(limits: QuotaLimits) -> str:
    parts = [
        f"{_LIMIT_LABELS[k]}={_fmt(v)}"
        for k, v in limits.model_dump(mode="json", exclude_none=True).items()
    ]
    return ", ".join(parts) or "(no limits at this level)"


@quotas_app.command("ls")
@cli_errors
def quotas_ls(
    ctx: typer.Context,
    scope: Annotated[QuotaScope | None, typer.Option("--scope")] = None,
    subject: Annotated[str | None, typer.Option("--subject", help="Token id or tenant id")] = None,
    limit: Limit = 100,
) -> None:
    """List quota policies."""
    st = state(ctx)
    with st.client() as api:
        items = api.admin.quotas(scope=scope, subject_id=subject, limit=min(limit, 200)).take(limit)
    if st.output.json_mode:
        st.output.emit_json(items)
        return
    st.output.table(
        ["scope", "subject", "limits", "note", "updated"],
        [
            (q.scope.value, q.subject_id, _limits_text(q.limits), q.note, _when(q.updated_at))
            for q in items
        ],
    )


@quotas_app.command("set")
@cli_errors
def quotas_set(
    ctx: typer.Context,
    scope: Annotated[QuotaScope, typer.Argument(help="token, tenant or global")],
    subject: Annotated[str, typer.Argument(help="Token id, tenant id, or * for global")],
    vcpu_day: Annotated[float | None, typer.Option("--vcpu-seconds-per-day", min=0)] = None,
    vcpu_month: Annotated[float | None, typer.Option("--vcpu-seconds-per-month", min=0)] = None,
    concurrent: Annotated[int | None, typer.Option("--concurrent-runs", min=0)] = None,
    fanout: Annotated[int | None, typer.Option("--max-fanout-tasks", min=1)] = None,
    storage: Annotated[str | None, typer.Option("--storage", help="e.g. 100GiB")] = None,
    egress: Annotated[str | None, typer.Option("--egress-per-month", help="e.g. 5GiB")] = None,
    rpm: Annotated[int | None, typer.Option("--requests-per-minute", min=1)] = None,
    max_class: Annotated[ResourceClass | None, typer.Option("--max-resource-class")] = None,
    note: Annotated[str | None, typer.Option("--note", help="Why (shown in listings)")] = None,
    yes: Yes = False,
) -> None:
    """Create or REPLACE the policy of (scope, subject): limits left out are removed."""
    st = state(ctx)
    limits = QuotaLimits(
        vcpu_seconds_per_day=vcpu_day,
        vcpu_seconds_per_month=vcpu_month,
        concurrent_runs=concurrent,
        max_fanout_tasks=fanout,
        storage_bytes=parse_size(storage) if storage else None,
        egress_bytes_per_month=parse_size(egress) if egress else None,
        requests_per_minute=rpm,
        max_resource_class=max_class,
    )
    with st.client() as api:
        current = _current_policy(api, scope, subject)
        if not st.output.json_mode:
            before = _limits_text(current.limits) if current else "(tier default)"
            st.output.info(Text(f"{scope.value}:{subject} now:   ") + safe(before))
            st.output.info(Text(f"{scope.value}:{subject} after: ") + safe(_limits_text(limits)))
        confirm(st, f"Replace the {scope.value} quota policy of {subject!r}?", yes=yes)
        policy = api.admin.set_quota(scope, subject, limits, note=note)
    if st.output.json_mode:
        st.output.emit_json(policy)
    else:
        st.output.success(
            f"{policy.scope.value}:{policy.subject_id}: {_limits_text(policy.limits)}"
        )


def _current_policy(api: Client, scope: QuotaScope, subject: str) -> QuotaPolicy | None:
    for policy in api.admin.quotas(scope=scope, subject_id=subject):
        if policy.scope is scope and policy.subject_id == subject:
            return policy
    return None


@quotas_app.command("rm")
@cli_errors
def quotas_rm(
    ctx: typer.Context,
    scope: Annotated[QuotaScope, typer.Argument()],
    subject: Annotated[str, typer.Argument()],
    yes: Yes = False,
) -> None:
    """Remove a policy (the tier default applies again)."""
    st = state(ctx)
    confirm(
        st,
        f"Remove the {scope.value} quota policy of {subject!r} (tier defaults apply again)?",
        yes=yes,
    )
    with st.client() as api:
        api.admin.delete_quota(scope, subject)
    if st.output.json_mode:
        st.output.emit_json({"removed": {"scope": scope.value, "subject_id": subject}})
    else:
        st.output.success(f"Removed the {scope.value} policy of {subject}")


# ============================================================================= kill switches


_SWITCH_FIELDS = ("untrusted_execution_enabled", "global_spend_cap_micros", "spend_cap_period")


class OnOff(StrEnum):
    ON = "on"
    OFF = "off"


def _switches_kv(out: Output, ks: KillSwitches) -> None:
    out.kv(
        [
            (
                "uploaded code",
                Text("running", style="green")
                if ks.untrusted_execution_enabled
                else Text("PAUSED", style="bold red"),
            ),
            (
                "spend cap",
                f"{money(ks.global_spend_cap_micros, ks.spend_cap_currency)} per "
                f"{ks.spend_cap_period}"
                if ks.global_spend_cap_micros is not None
                else "none",
            ),
            ("reason", ks.reason),
            ("updated", f"{_when(ks.updated_at)} by {ks.updated_by or '-'}"),
        ]
    )


@switches_app.command("get")
@cli_errors
def switches_get(ctx: typer.Context) -> None:
    """Show the kill switches."""
    st = state(ctx)
    with st.client() as api:
        ks = api.admin.kill_switches()
    if st.output.json_mode:
        st.output.emit_json(ks)
    else:
        _switches_kv(st.output, ks)


@switches_app.command("set")
@cli_errors
def switches_set(
    ctx: typer.Context,
    reason: Annotated[str, typer.Option("--reason", help="Why (audited); required")],
    untrusted_code: Annotated[
        OnOff | None,
        typer.Option("--untrusted-code", help="off: pause every run of uploaded plugin code"),
    ] = None,
    spend_cap: Annotated[
        str | None,
        typer.Option("--spend-cap", help="Stop new compute past this platform spend (e.g. 500)"),
    ] = None,
    clear_spend_cap: Annotated[
        bool, typer.Option("--clear-spend-cap", help="Remove the spend cap")
    ] = False,
    period: Annotated[str | None, typer.Option("--spend-cap-period", help="day or month")] = None,
    yes: Yes = False,
) -> None:
    """Flip kill switches (confirmation shows the change; the server audits it)."""
    st = state(ctx)
    if period is not None and period not in ("day", "month"):
        raise ValueError("--spend-cap-period must be day or month")
    update = KillSwitchUpdate.model_validate(
        {
            k: v
            for k, v in (
                (
                    "untrusted_execution_enabled",
                    None if untrusted_code is None else untrusted_code is OnOff.ON,
                ),
                ("global_spend_cap_micros", parse_amount(spend_cap) if spend_cap else None),
                ("clear_spend_cap", clear_spend_cap or None),
                ("spend_cap_period", period),
                ("reason", reason),
            )
            if v is not None
        }
    )
    with st.client() as api:
        current = api.admin.kill_switches()
        after = update.apply(current, now=datetime.now(UTC), by=None)
        changes = [
            (key, getattr(current, key), getattr(after, key))
            for key in (
                "untrusted_execution_enabled",
                "global_spend_cap_micros",
                "spend_cap_period",
            )
            if getattr(current, key) != getattr(after, key)
        ]
        if not changes:
            raise ValueError("nothing to change (give --untrusted-code, --spend-cap, ...)")
        _show_changes(st.output, "the kill switches", changes)
        pausing = untrusted_code is OnOff.OFF and current.untrusted_execution_enabled
        confirm(
            st,
            "Pause ALL runs of uploaded code platform-wide?"
            if pausing
            else "Apply these kill-switch changes?",
            yes=yes,
        )
        ks = api.admin.set_kill_switches(update)
    if st.output.json_mode:
        st.output.emit_json(ks)
    else:
        _switches_kv(st.output, ks)


# ================================================================================ executors


@executors_app.command("ls")
@cli_errors
def executors_ls(ctx: typer.Context) -> None:
    """Executor health, load and conformance."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.admin.executors())
    if st.output.json_mode:
        st.output.emit_json(items)
        return
    st.output.table(
        ["name", "enabled", "status", "conformance", "running", "queued", "submits/min", "429s/h"],
        [
            (
                e.name,
                "yes" if e.enabled else "no",
                safe(e.status.value, {"ok": "green", "degraded": "yellow"}.get(e.status, "red")),
                "passed" if e.conformance_passed else "-",
                e.running_executions,
                e.queued_runs,
                f"{e.submits_last_minute}/{e.submit_rate_limit_per_minute or '∞'}",
                e.throttled_last_hour,
            )
            for e in items
        ],
    )


@executors_app.command("get")
@cli_errors
def executors_get(ctx: typer.Context, name: Annotated[str, typer.Argument()]) -> None:
    """One executor."""
    st = state(ctx)
    with st.client() as api:
        e: ExecutorHealth = api.admin.executor(name)
    if st.output.json_mode:
        st.output.emit_json(e)
        return
    st.output.kv(
        [
            ("name", e.name),
            ("enabled", "yes" if e.enabled else "no"),
            ("status", e.status.value),
            ("message", e.message),
            ("isolation", e.caps.isolation.name),
            ("egress / dns enforced", f"{e.caps.egress_enforced} / {e.caps.dns_enforced}"),
            ("caps allow shared tiers", "yes" if e.allows_public_tenants else "no"),
            ("verified / public tenants", "enabled" if e.shared_tiers_enabled else "held"),
            ("regions", ", ".join(e.regions)),
            ("conformance", "passed" if e.conformance_passed else "not passed"),
            ("security suite", "passed" if e.security_passed else "not passed"),
            ("running / queued", f"{e.running_executions} / {e.queued_runs}"),
            ("failures last hour", e.failures_last_hour),
            ("last success", _when(e.last_success_at)),
            ("checked", _when(e.checked_at)),
        ]
    )


# ==================================================================================== audit


@app.command("audit")
@cli_errors
def audit(
    ctx: typer.Context,
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    project: Annotated[str | None, typer.Option("--project-id")] = None,
    actor: Annotated[str | None, typer.Option("--actor")] = None,
    action: Annotated[AuditAction | None, typer.Option("--action")] = None,
    outcome: Annotated[AuditOutcome | None, typer.Option("--outcome")] = None,
    since: Annotated[
        datetime | None, typer.Option("--since", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"])
    ] = None,
    until: Annotated[
        datetime | None, typer.Option("--until", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"])
    ] = None,
    limit: Limit = 50,
) -> None:
    """The platform audit log, newest first (times in UTC)."""
    st = state(ctx)
    with st.client() as api:
        items = api.admin.audit(
            tenant_id=tenant,
            project_id=project,
            actor_id=actor,
            action=action,
            outcome=outcome,
            since=since.replace(tzinfo=UTC) if since else None,
            until=until.replace(tzinfo=UTC) if until else None,
            limit=min(limit, 200),
        ).take(limit)
    render_audit(st.output, items)


def render_audit(out: Output, items: list[AuditEvent]) -> None:
    if out.json_mode:
        out.emit_json(items)
        return
    style = {"success": "green", "denied": "yellow", "error": "red"}
    out.table(
        ["time", "where", "actor", "action", "resource", "outcome"],
        [
            (
                _when(e.ts),
                "/".join(x for x in (e.tenant_id, e.project_id) if x) or "platform",
                (e.actor_id or "-") + (" (operator)" if e.operator else ""),
                e.action.value,
                f"{e.resource_kind.value}:{e.resource_id or '-'}",
                safe(
                    e.outcome.value + (f" {e.code.value}" if e.code else ""),
                    style.get(e.outcome.value, ""),
                ),
            )
            for e in items
        ],
    )
