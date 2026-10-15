"""Quota policies, usage counters, request rate limits, run admission and kill switches.

Policies come from ``quota_policies`` (operators) or the defaults (the tenant's trust tier,
:data:`~mapi_spec.quotas.DEFAULT_TIER_LIMITS`; :data:`~mapi_spec.quotas.DEFAULT_GLOBAL_LIMITS`);
a token's policy is its own rate limit and vCPU budget tightened by any operator policy for
it. The pure decision is always :func:`mapi_spec.quotas.evaluate_quota`.

vCPU-second usage = metered ledger entries in the window + the estimates of runs that are
active and not yet metered, so a burst of submissions cannot overrun a budget before the
first run finishes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from mapi_spec.admin import DEFAULT_KILL_SWITCHES, KillSwitches, kill_switch_decision
from mapi_spec.cost import CostEntryKind
from mapi_spec.quotas import (
    DEFAULT_GLOBAL_LIMITS,
    DEFAULT_TIER_LIMITS,
    GLOBAL_SUBJECT,
    QuotaDecision,
    QuotaDemand,
    QuotaLimits,
    QuotaPolicy,
    QuotaScope,
    QuotaUsage,
    evaluate_quota,
    policy_for_token,
)
from mapi_spec.run import RunState
from mapi_spec.tenancy import TrustTier
from mapi_spec.tokens import TokenRecord
from sqlalchemy import BigInteger, ColumnElement, Float, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m

__all__ = [
    "ACTIVE_STATES",
    "DISPATCHED_STATES",
    "KILL_SWITCHES_KEY",
    "admit_run",
    "admit_storage",
    "count_requests",
    "global_policy",
    "global_spend",
    "kill_switch_check",
    "kill_switches",
    "policy_dto",
    "quota_usage",
    "rate_limit",
    "tenant_policy",
    "token_policy",
    "window_starts",
]

KILL_SWITCHES_KEY: Final = "kill_switches"
ACTIVE_STATES: Final = tuple(
    s.value for s in RunState if not s.terminal and s not in (RunState.CREATED,)
)
"""Runs between ``queued`` and a terminal state (the ``concurrent_runs`` count)."""
DISPATCHED_STATES: Final = tuple(
    s.value for s in RunState if not s.terminal and s not in (RunState.CREATED, RunState.QUEUED)
)
"""Runs holding (or about to hold) an execution: what the dispatcher's caps count."""


def window_starts(now: datetime) -> tuple[datetime, datetime]:
    """Start of the current UTC day and UTC month."""
    now = now.astimezone(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


def policy_dto(row: m.QuotaPolicy) -> QuotaPolicy:
    return QuotaPolicy(
        scope=QuotaScope(row.scope),
        subject_id=row.subject_id,
        limits=QuotaLimits.model_validate(row.limits),
        note=row.note,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def _stored(s: AsyncSession, scope: QuotaScope, subject: str) -> QuotaPolicy | None:
    row = await s.get(m.QuotaPolicy, (scope.value, subject))
    return policy_dto(row) if row is not None else None


async def tenant_policy(s: AsyncSession, tenant_id: str, tier: str) -> QuotaPolicy:
    stored = await _stored(s, QuotaScope.TENANT, tenant_id)
    if stored is not None:
        return stored
    limits = DEFAULT_TIER_LIMITS.get(TrustTier(tier), QuotaLimits())
    return QuotaPolicy(scope=QuotaScope.TENANT, subject_id=tenant_id, limits=limits)


async def global_policy(s: AsyncSession) -> QuotaPolicy:
    stored = await _stored(s, QuotaScope.GLOBAL, GLOBAL_SUBJECT)
    if stored is not None:
        return stored
    return QuotaPolicy(
        scope=QuotaScope.GLOBAL, subject_id=GLOBAL_SUBJECT, limits=DEFAULT_GLOBAL_LIMITS
    )


async def token_policy(s: AsyncSession, record: TokenRecord) -> QuotaPolicy:
    own = policy_for_token(record)
    stored = await _stored(s, QuotaScope.TOKEN, record.id)
    if stored is None:
        return own
    return QuotaPolicy(
        scope=QuotaScope.TOKEN,
        subject_id=record.id,
        limits=own.limits.tightened(stored.limits),
        note=stored.note,
        updated_at=stored.updated_at,
        updated_by=stored.updated_by,
    )


async def kill_switches(s: AsyncSession) -> KillSwitches:
    row = await s.get(m.PlatformSetting, KILL_SWITCHES_KEY)
    if row is None:
        return DEFAULT_KILL_SWITCHES
    return KillSwitches.model_validate(row.value)


# ========================================================================================= usage


def _estimate_vcpu() -> ColumnElement[float]:
    return func.coalesce(cast(m.Run.estimate["vcpu_seconds"].astext, Float), 0.0)


async def quota_usage(
    s: AsyncSession,
    scope: QuotaScope,
    subject_id: str,
    *,
    now: datetime,
    requests_this_minute: int = 0,
) -> QuotaUsage:
    """Current counters of one subject (``s`` must be able to see its rows: a tenant session
    for a token or tenant, the platform role for the global level)."""
    day, month = window_starts(now)
    ledger = select(
        func.coalesce(
            func.sum(m.CostLedgerEntry.vcpu_seconds).filter(m.CostLedgerEntry.recorded_at >= day),
            0.0,
        ),
        func.coalesce(func.sum(m.CostLedgerEntry.vcpu_seconds), 0.0),
    ).where(m.CostLedgerEntry.recorded_at >= month)
    active = select(func.count(), func.coalesce(func.sum(_estimate_vcpu()), 0.0)).where(
        m.Run.state.in_(ACTIVE_STATES)
    )
    if scope is QuotaScope.TOKEN:
        ledger = ledger.where(m.CostLedgerEntry.token_id == subject_id)
        active = active.where(m.Run.token_id == subject_id)
    elif scope is QuotaScope.TENANT:
        ledger = ledger.where(m.CostLedgerEntry.tenant_id == subject_id)
        active = active.where(m.Run.tenant_id == subject_id)
    today, this_month = (await s.execute(ledger)).one()
    running, pending_vcpu = (await s.execute(active)).one()
    storage = 0
    if scope is QuotaScope.TENANT:
        storage = int(
            (
                await s.execute(
                    select(
                        func.coalesce(func.sum(m.DatasetVersion.size_bytes), 0).cast(BigInteger)
                    ).where(m.DatasetVersion.tenant_id == subject_id)
                )
            ).scalar_one()
        )
        storage += int(
            (
                await s.execute(
                    select(func.coalesce(func.sum(m.Upload.size_bytes), 0)).where(
                        m.Upload.tenant_id == subject_id,
                        m.Upload.status.in_(("pending", "uploaded")),
                    )
                )
            ).scalar_one()
        )
    return QuotaUsage(
        vcpu_seconds_today=float(today) + float(pending_vcpu),
        vcpu_seconds_month=float(this_month) + float(pending_vcpu),
        concurrent_runs=int(running),
        storage_bytes=storage,
        requests_this_minute=requests_this_minute,
    )


async def global_spend(s: AsyncSession, switches: KillSwitches, *, now: datetime) -> int:
    """Platform spend (micros) in the kill switches' cap window and currency."""
    day, month = window_starts(now)
    start = day if switches.spend_cap_period == "day" else month
    value = (
        await s.execute(
            select(func.coalesce(func.sum(m.CostLedgerEntry.amount_micros), 0)).where(
                m.CostLedgerEntry.recorded_at >= start,
                m.CostLedgerEntry.currency == switches.spend_cap_currency,
                m.CostLedgerEntry.kind.in_(
                    (CostEntryKind.ACTUAL.value, CostEntryKind.ADJUSTMENT.value)
                ),
            )
        )
    ).scalar_one()
    return int(value)


async def kill_switch_check(
    s: AsyncSession, *, untrusted_code: bool, tier: str, now: datetime
) -> QuotaDecision:
    """:func:`mapi_spec.admin.kill_switch_decision` with the stored switches (platform role)."""
    switches = await kill_switches(s)
    spent = 0
    if switches.global_spend_cap_micros is not None:
        spent = await global_spend(s, switches, now=now)
    return kill_switch_decision(
        switches, untrusted_code=untrusted_code, tier=TrustTier(tier), spent_micros=spent, now=now
    )


# ==================================================================================== admission


async def admit_run(
    session: AsyncSession,
    system: AsyncSession,
    *,
    tenant_id: str,
    token: TokenRecord | None,
    demand: QuotaDemand,
    untrusted_code: bool,
    now: datetime,
) -> QuotaDecision:
    """May a new run be created? Kill switches first, then token, tenant and global quotas.

    ``session`` is the request's tenant session: the tenant row is locked (``FOR UPDATE``) so
    concurrent submissions of one tenant are admitted one at a time against fresh counters.
    ``system`` (platform role) reads the policies, kill switches and global counters.
    """
    tenant = (
        await session.execute(select(m.Tenant).where(m.Tenant.id == tenant_id).with_for_update())
    ).scalar_one()
    decision = await kill_switch_check(
        system, untrusted_code=untrusted_code, tier=tenant.trust_tier, now=now
    )
    if not decision.allowed:
        return decision
    levels: list[tuple[QuotaPolicy, QuotaUsage]] = []
    if token is not None:
        levels.append(
            (
                await token_policy(system, token),
                await quota_usage(session, QuotaScope.TOKEN, token.id, now=now),
            )
        )
    levels.append(
        (
            await tenant_policy(system, tenant_id, tenant.trust_tier),
            await quota_usage(session, QuotaScope.TENANT, tenant_id, now=now),
        )
    )
    levels.append(
        (
            await global_policy(system),
            await quota_usage(system, QuotaScope.GLOBAL, GLOBAL_SUBJECT, now=now),
        )
    )
    return evaluate_quota(levels, demand, now=now)


async def admit_storage(
    session: AsyncSession, system: AsyncSession, *, tenant_id: str, nbytes: int, now: datetime
) -> QuotaDecision:
    """May the tenant store ``nbytes`` more (an upload)? Reads and deletes are never
    blocked: only demands for more storage are checked."""
    tenant = await session.get(m.Tenant, tenant_id)
    if tenant is None:
        return QuotaDecision.allow()
    level = (
        await tenant_policy(system, tenant_id, tenant.trust_tier),
        await quota_usage(session, QuotaScope.TENANT, tenant_id, now=now),
    )
    return evaluate_quota([level], QuotaDemand(storage_bytes=nbytes), now=now)


# =================================================================================== rate limit


async def count_requests(s: AsyncSession, subject: str, *, now: datetime) -> int:
    """Count one request in ``subject``'s current minute; returns the count *before* it."""
    window = now.astimezone(UTC).replace(second=0, microsecond=0)
    await s.execute(
        delete(m.RateCounter).where(
            m.RateCounter.subject == subject,
            m.RateCounter.window_start < window - timedelta(minutes=1),
        )
    )
    count = (
        await s.execute(
            insert(m.RateCounter)
            .values(subject=subject, window_start=window, count=1)
            .on_conflict_do_update(
                index_elements=["subject", "window_start"],
                set_={"count": m.RateCounter.count + 1},
            )
            .returning(m.RateCounter.count)
        )
    ).scalar_one()
    return int(count) - 1


async def rate_limit(
    s: AsyncSession, levels: Sequence[tuple[QuotaPolicy, str]], *, now: datetime
) -> QuotaDecision:
    """``RATE_LIMITED`` if this request exceeds ``requests_per_minute`` at any level.

    ``levels`` pairs each policy with its counter subject; levels without a request limit
    are not counted (no write per request for them).
    """
    checked: list[tuple[QuotaPolicy, QuotaUsage]] = []
    for policy, subject in levels:
        if policy.limits.requests_per_minute is None:
            continue
        before = await count_requests(s, subject, now=now)
        checked.append((policy, QuotaUsage(requests_this_minute=before)))
    if not checked:
        return QuotaDecision.allow()
    return evaluate_quota(checked, QuotaDemand(requests=1), now=now)
