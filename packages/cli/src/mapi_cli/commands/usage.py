"""``mapi usage``: metered cost and quota headroom of the project or tenant (v0.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

import typer
from mapi_spec.api import UsageGroupBy, UsagePeriod, UsageReport
from mapi_spec.quotas import LimitName, QuotaLimitStatus
from rich.text import Text

from mapi_cli.output import Output, human_duration, money, safe
from mapi_cli.state import cli_errors, state

__all__ = ["render_quotas", "render_usage", "usage"]

_BYTES = {LimitName.STORAGE_BYTES, LimitName.EGRESS_BYTES_PER_MONTH}
_SECONDS = {LimitName.VCPU_SECONDS_PER_DAY, LimitName.VCPU_SECONDS_PER_MONTH}


def _num(value: float | None, name: LimitName) -> str:
    if value is None:
        return "unlimited"
    if name in _BYTES:
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
    if name in _SECONDS:
        return f"{value:,.0f} vCPU-s"
    return f"{value:g}"


def _bar(fraction: float | None) -> Text:
    if fraction is None:
        return Text("-")
    style = "red" if fraction >= 0.9 else "yellow" if fraction >= 0.7 else "green"
    return Text(f"{fraction:.0%}", style=style)


def render_quotas(out: Output, rows: list[QuotaLimitStatus], *, now: datetime) -> None:
    if not rows:
        return
    out.table(
        ["level", "limit", "used", "limit value", "used %", "resets in"],
        [
            (
                f"{q.scope.value}:{q.subject_id}",
                q.limit_name.value,
                _num(q.used, q.limit_name),
                _num(q.limit, q.limit_name),
                _bar(q.fraction_used),
                human_duration((q.resets_at - now).total_seconds()) if q.resets_at else "-",
            )
            for q in rows
        ],
        title="Quotas",
    )


def render_usage(out: Output, report: UsageReport, *, now: datetime | None = None) -> None:
    t = report.totals
    scope = report.tenant_id + (f"/{report.project_id}" if report.project_id else " (tenant)")
    out.kv(
        [
            ("usage of", scope),
            (
                "period",
                f"{report.period} {report.start:%Y-%m-%d} – {report.end:%Y-%m-%d} (UTC)",
            ),
            ("cost", Text(money(t.amount_micros, t.currency), style="bold")),
            ("vCPU-seconds", f"{t.vcpu_seconds:,.0f}"),
            ("GiB-seconds", f"{t.gib_seconds:,.0f}"),
            ("runs", f"{t.runs} ({t.attempts} attempts)"),
            ("price table", ", ".join(report.price_versions)),
        ]
    )
    if report.groups:
        out.table(
            [report.group_by or "group", "cost", "vCPU-s", "GiB-s", "runs"],
            [
                (
                    safe(key),
                    money(g.amount_micros, g.currency),
                    f"{g.vcpu_seconds:,.0f}",
                    f"{g.gib_seconds:,.0f}",
                    g.runs,
                )
                for key, g in sorted(report.groups.items())
            ],
        )
    render_quotas(out, report.quotas, now=now or datetime.now(report.start.tzinfo))


class Period(StrEnum):
    DAY = "day"
    MONTH = "month"


class GroupBy(StrEnum):
    RESOURCE_CLASS = "resource_class"
    PROJECT = "project"
    DAY = "day"
    TOKEN = "token"
    RUN = "run"


_PERIODS: dict[Period, UsagePeriod] = {Period.DAY: "day", Period.MONTH: "month"}
_GROUPS: dict[GroupBy, UsageGroupBy] = {
    GroupBy.RESOURCE_CLASS: "resource_class",
    GroupBy.PROJECT: "project",
    GroupBy.DAY: "day",
    GroupBy.TOKEN: "token",
    GroupBy.RUN: "run",
}


@cli_errors
def usage(
    ctx: typer.Context,
    period: Annotated[Period, typer.Option("--period", help="UTC day or month")] = Period.MONTH,
    start: Annotated[
        datetime | None,
        typer.Option(
            "--start",
            help="A UTC date inside the period (e.g. 2026-09-01; default: now)",
            formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        ),
    ] = None,
    group_by: Annotated[
        GroupBy | None, typer.Option("--group-by", help="Break the totals down")
    ] = None,
    tenant: Annotated[
        str | None,
        typer.Option("--tenant", help="The whole tenant instead of the project (tenant admins)"),
    ] = None,
) -> None:
    """Cost and quota use of the selected project (or --tenant) for a UTC day or month."""
    st = state(ctx)
    when = start.replace(tzinfo=UTC) if start is not None and start.tzinfo is None else start
    per = _PERIODS[period]
    grp = _GROUPS[group_by] if group_by is not None else None
    with st.client() as api:
        if tenant:
            report = api.usage.tenant(tenant, period=per, start=when, group_by=grp)
        else:
            report = api.usage.get(period=per, start=when, group_by=grp)
    if st.output.json_mode:
        st.output.emit_json(report)
        return
    render_usage(st.output, report)
