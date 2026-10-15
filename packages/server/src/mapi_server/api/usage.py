"""Usage and cost: ``GET /v1/usage`` (one project) and ``GET /v1/tenants/{id}/usage``.

Totals come from the append-only cost ledger for one UTC day or month; ``quotas`` shows the
limits in force now at each level that applies to the caller (token, tenant, global) with
current use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from mapi_spec.api import UsageReport
from mapi_spec.cost import summarize_ledger
from mapi_spec.quotas import QuotaLimitStatus, QuotaScope, quota_status
from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.api.common import Auth, Ctx, Session
from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.cost import ledger_dto
from mapi_server.services.quotas import quota_usage, tenant_policy, token_policy
from mapi_server.services.runs import now

__all__ = ["period_bounds", "router"]

router = APIRouter(tags=["usage"])

Period = Literal["day", "month"]
GroupBy = Literal["resource_class", "project", "day", "token", "run"]


def period_bounds(period: Period, start: datetime | None) -> tuple[datetime, datetime]:
    """The UTC day or month containing ``start`` (default: now)."""
    at = (start or now()).astimezone(UTC)
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day, day + timedelta(days=1)
    first = day.replace(day=1)
    nxt = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    return first, nxt


async def _report(
    ctx: ServerContext,
    auth: RequestAuth,
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None,
    period: Period,
    start: datetime | None,
    group_by: GroupBy | None,
) -> UsageReport:
    lo, hi = period_bounds(period, start)
    stmt = select(m.CostLedgerEntry).where(
        m.CostLedgerEntry.tenant_id == tenant_id,
        m.CostLedgerEntry.recorded_at >= lo,
        m.CostLedgerEntry.recorded_at < hi,
    )
    if project_id is not None:
        stmt = stmt.where(m.CostLedgerEntry.project_id == project_id)
    entries = [ledger_dto(r) for r in (await session.execute(stmt)).scalars()]
    totals = summarize_ledger(entries)["total"]
    groups = summarize_ledger(entries, group_by) if group_by else {}
    ts = now()
    quotas: list[QuotaLimitStatus] = []
    async with ctx.db.session() as system:
        tenant = await system.get(m.Tenant, tenant_id)
        assert tenant is not None
        if auth.token is not None:
            policy = await token_policy(system, auth.token)
            usage = await quota_usage(session, QuotaScope.TOKEN, auth.token.id, now=ts)
            quotas.extend(quota_status(policy, usage, now=ts))
        policy = await tenant_policy(system, tenant_id, tenant.trust_tier)
    usage = await quota_usage(session, QuotaScope.TENANT, tenant_id, now=ts)
    quotas.extend(quota_status(policy, usage, now=ts))
    return UsageReport(
        tenant_id=tenant_id,
        project_id=project_id,
        period=period,
        start=lo,
        end=hi,
        totals=totals,
        group_by=group_by,
        groups=groups,
        price_versions=sorted({e.price_version for e in entries}),
        quotas=quotas,
    )


@router.get("/v1/usage", response_model=UsageReport, operation_id="get_usage")
async def get_usage(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    period: Period = "month",
    start: Annotated[AwareDatetime | None, Query()] = None,
    group_by: GroupBy | None = None,
) -> UsageReport:
    """Cost of the selected project for one UTC period, and the quotas in force now."""
    sc = ctx.scope
    return await _report(
        ctx, auth, session, tenant_id=sc.tenant_id, project_id=sc.project_id, period=period,
        start=start, group_by=group_by,
    )  # fmt: skip


@router.get(
    "/v1/tenants/{tenant_id}/usage",
    response_model=UsageReport,
    tags=["tenants"],
    operation_id="get_tenant_usage",
)
async def get_tenant_usage(
    ctx: Ctx,
    auth: Auth,
    session: Session,
    tenant_id: Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")],
    period: Period = "month",
    start: Annotated[AwareDatetime | None, Query()] = None,
    group_by: GroupBy | None = None,
) -> UsageReport:
    """Cost of the whole tenant (tenant admins)."""
    return await _report(
        ctx, auth, session, tenant_id=tenant_id, project_id=None, period=period, start=start,
        group_by=group_by,
    )  # fmt: skip
