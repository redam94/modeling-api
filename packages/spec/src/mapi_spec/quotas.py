"""Quotas, rate limits, priority classes and fair-share leasing (plan §8.3, §8.8, §9).

Three levels of :class:`QuotaPolicy` apply to every request and run, most specific first:
**token**, **tenant**, **global**. Each sets optional :class:`QuotaLimits` (``None`` =
unlimited at that level). :func:`evaluate_quota` is the single pure decision function: the
first limit a demand would exceed yields a :class:`QuotaDecision` with problem code
``RATE_LIMITED`` (request rate; retryable) or ``QUOTA_EXCEEDED`` (everything else).

Quotas are expressed in **vCPU-seconds** because abuse risk is compute, not run count.
:class:`PriorityClass` weights drive :func:`plan_leases`, the dispatcher's weighted
fair-share choice of which queued runs to lease next, so one tenant's 237-task SBC cannot
starve everyone else's interactive scoring.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from mapi_spec._dto import (
    EXAMPLE_TS,
    TENANT_ID_RE,
    Id,
    RequestModel,
    ResponseModel,
    dto_examples,
)
from mapi_spec._types import StrictModel
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.tenancy import TrustTier
from mapi_spec.tokens import BudgetPeriod, TokenRecord

__all__ = [
    "DEFAULT_GLOBAL_LIMITS",
    "DEFAULT_TIER_LIMITS",
    "GLOBAL_SUBJECT",
    "JOBS_RUN_PER_MINUTE",
    "MAX_SCORE_DRAWS",
    "PRIORITY_WEIGHTS",
    "LeaseCandidate",
    "LimitName",
    "PriorityClass",
    "QuotaDecision",
    "QuotaDemand",
    "QuotaLimitStatus",
    "QuotaLimits",
    "QuotaPolicy",
    "QuotaPolicyUpsert",
    "QuotaScope",
    "QuotaUsage",
    "backoff_seconds",
    "default_priority",
    "evaluate_quota",
    "max_score_draws",
    "plan_leases",
    "policy_for_token",
    "quota_status",
    "window_reset",
]

GLOBAL_SUBJECT: Final = "*"
"""``subject_id`` of the single global policy."""

JOBS_RUN_PER_MINUTE: Final = 180
"""Cloud Run ``jobs.run`` calls per minute per region the dispatcher's token bucket stays
below ([verify M0] in the plan; configurable in the server)."""


class QuotaScope(StrEnum):
    """Policy level, most specific first."""

    TOKEN = "token"
    TENANT = "tenant"
    GLOBAL = "global"

    @property
    def rank(self) -> int:
        return list(QuotaScope).index(self)


class PriorityClass(StrEnum):
    """Scheduling class of a run. Interactive work (scores, renders) outranks batch fits."""

    INTERACTIVE = "interactive"
    BATCH = "batch"


PRIORITY_WEIGHTS: Final[dict[PriorityClass, float]] = {
    PriorityClass.INTERACTIVE: 4.0,
    PriorityClass.BATCH: 1.0,
}
"""Fair-share weights: at equal load an interactive run is leased before a batch run, and a
tenant's running batch work counts four times as heavily against its next interactive run."""

_INTERACTIVE_KINDS = frozenset(
    {
        RunKind.SCORE,
        RunKind.RENDER,
        RunKind.DERIVE,
        RunKind.VALIDATE,
        RunKind.DESCRIBE,
        RunKind.TRANSLATE_PRIORS,  # M3: a guided user waits for it while drafting
    }
)


def default_priority(kind: RunKind | str) -> PriorityClass:
    """Interactive for short, user-facing kinds (score, render, derive, validate, describe,
    translate_priors); batch for fits, ingest, evaluate, optimise and fan-out parents."""
    return PriorityClass.INTERACTIVE if RunKind(kind) in _INTERACTIVE_KINDS else PriorityClass.BATCH


class LimitName(StrEnum):
    """Every limit a :class:`QuotaDecision` can name."""

    REQUESTS_PER_MINUTE = "requests_per_minute"
    RESOURCE_CLASS = "max_resource_class"
    FANOUT_TASKS = "max_fanout_tasks"
    CONCURRENT_RUNS = "concurrent_runs"
    VCPU_SECONDS_PER_DAY = "vcpu_seconds_per_day"
    VCPU_SECONDS_PER_MONTH = "vcpu_seconds_per_month"
    STORAGE_BYTES = "storage_bytes"
    EGRESS_BYTES_PER_MONTH = "egress_bytes_per_month"
    UNTRUSTED_EXECUTION = "untrusted_execution"
    GLOBAL_SPEND = "global_spend"


class QuotaLimits(StrictModel):
    """Limits at one level. ``None`` = no limit at this level (other levels still apply)."""

    vcpu_seconds_per_day: float | None = Field(None, ge=0)
    vcpu_seconds_per_month: float | None = Field(None, ge=0)
    concurrent_runs: int | None = Field(None, ge=0)
    max_fanout_tasks: int | None = Field(None, ge=1, le=10_000)
    storage_bytes: int | None = Field(None, ge=0)
    egress_bytes_per_month: int | None = Field(None, ge=0)
    requests_per_minute: int | None = Field(None, ge=1)
    max_resource_class: ResourceClass | None = None

    def tightened(self, other: QuotaLimits) -> QuotaLimits:
        """Field-wise the more restrictive of ``self`` and ``other``."""
        merged: dict[str, object] = {}
        for name in type(self).model_fields:
            a, b = getattr(self, name), getattr(other, name)
            merged[name] = b if a is None else a if b is None else min(a, b)
        return QuotaLimits.model_validate(merged)


GIB: Final = 1024**3

DEFAULT_TIER_LIMITS: Final[dict[TrustTier, QuotaLimits]] = {
    TrustTier.PUBLIC: QuotaLimits(
        vcpu_seconds_per_day=3_600,
        vcpu_seconds_per_month=36_000,
        concurrent_runs=2,
        max_fanout_tasks=16,
        storage_bytes=2 * GIB,
        egress_bytes_per_month=5 * GIB,
        requests_per_minute=60,
        max_resource_class=ResourceClass.S,
    ),
    TrustTier.VERIFIED: QuotaLimits(
        vcpu_seconds_per_day=86_400,
        vcpu_seconds_per_month=1_000_000,
        concurrent_runs=20,
        max_fanout_tasks=256,
        storage_bytes=100 * GIB,
        egress_bytes_per_month=100 * GIB,
        requests_per_minute=600,
        max_resource_class=ResourceClass.L,
    ),
    TrustTier.INTERNAL: QuotaLimits(
        concurrent_runs=100, max_fanout_tasks=1_000, requests_per_minute=6_000
    ),
    TrustTier.SELF_HOSTED_TRUSTED: QuotaLimits(),
}
"""Tenant limits when no tenant policy overrides them (free tier: class S only, plan Q6)."""

MAX_SCORE_DRAWS: Final[dict[TrustTier, int]] = {
    TrustTier.PUBLIC: 300,
    TrustTier.VERIFIED: 500,
    TrustTier.INTERNAL: 500,
    TrustTier.SELF_HOSTED_TRUSTED: 500,
}
"""Draws a score request may return (``output.draws``), per tier (plan Q2: 300, 500 on
paid tiers). The server refuses more with ``QUOTA_EXCEEDED`` and stamps the cap on the
score job (``ScoreJob.max_draws``), which the runner enforces again."""


def max_score_draws(tier: TrustTier | str | None) -> int:
    """The draw cap of a tenant's tier (the free tier's when it is unknown)."""
    if tier is None:
        return MAX_SCORE_DRAWS[TrustTier.PUBLIC]
    try:
        return MAX_SCORE_DRAWS[TrustTier(tier)]
    except ValueError:
        return MAX_SCORE_DRAWS[TrustTier.PUBLIC]


DEFAULT_GLOBAL_LIMITS: Final = QuotaLimits(concurrent_runs=700)
"""70% of Cloud Run's 1,000 running executions per project and region (plan §8.8)."""


def _check_subject(scope: QuotaScope, subject_id: str) -> None:
    if scope is QuotaScope.GLOBAL and subject_id != GLOBAL_SUBJECT:
        raise ValueError(f"the global policy's subject_id is {GLOBAL_SUBJECT!r}")
    if scope is QuotaScope.TENANT and not re.fullmatch(TENANT_ID_RE, subject_id):
        raise ValueError("a tenant policy's subject_id is a tenant id")
    if scope is QuotaScope.TOKEN and not re.fullmatch(r"tok_[0-9a-z]{26}", subject_id):
        raise ValueError("a token policy's subject_id is a token id (tok_…)")


class QuotaPolicyUpsert(RequestModel):
    """``PUT /v1/admin/quotas``: create or replace the policy for ``(scope, subject_id)``."""

    scope: QuotaScope
    subject_id: str = Field(..., max_length=64)
    limits: QuotaLimits
    note: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "scope": "tenant",
            "subject_id": "acme",
            "limits": {"vcpu_seconds_per_day": 172800, "concurrent_runs": 40},
            "note": "beta customer, doubled daily compute",
        }
    )

    @model_validator(mode="after")
    def _subject(self) -> QuotaPolicyUpsert:
        _check_subject(self.scope, self.subject_id)
        return self


class QuotaPolicy(ResponseModel):
    """The limits in force for one subject at one level."""

    scope: QuotaScope
    subject_id: str = Field(..., max_length=64)
    limits: QuotaLimits
    note: str | None = None
    updated_at: AwareDatetime | None = None
    updated_by: Id | None = None

    model_config = dto_examples(
        {
            "scope": "tenant",
            "subject_id": "acme",
            "limits": {
                "vcpu_seconds_per_day": 172800,
                "vcpu_seconds_per_month": 1000000,
                "concurrent_runs": 40,
                "max_fanout_tasks": 256,
                "max_resource_class": "L",
            },
            "note": "beta customer, doubled daily compute",
            "updated_at": EXAMPLE_TS,
            "updated_by": "usr_01jaf3a0b0c0d0e0f0g0h0j0k0",
        },
        {"scope": "global", "subject_id": "*", "limits": {"concurrent_runs": 700}},
    )

    @model_validator(mode="after")
    def _subject(self) -> QuotaPolicy:
        _check_subject(self.scope, self.subject_id)
        return self


def policy_for_token(record: TokenRecord) -> QuotaPolicy:
    """The token-level policy implied by a token's own rate limit and vCPU budget."""
    budget = record.vcpu_seconds_budget
    day = budget if record.budget_period is BudgetPeriod.DAY else None
    month = budget if record.budget_period is BudgetPeriod.MONTH else None
    return QuotaPolicy(
        scope=QuotaScope.TOKEN,
        subject_id=record.id,
        limits=QuotaLimits(
            requests_per_minute=record.rate_limit_per_minute,
            vcpu_seconds_per_day=day,
            vcpu_seconds_per_month=month,
        ),
    )


class QuotaUsage(StrictModel):
    """Counters for one subject in the current windows (UTC day, UTC month, minute)."""

    vcpu_seconds_today: float = Field(0, ge=0)
    vcpu_seconds_month: float = Field(0, ge=0)
    concurrent_runs: int = Field(0, ge=0)
    storage_bytes: int = Field(0, ge=0)
    egress_bytes_month: int = Field(0, ge=0)
    requests_this_minute: int = Field(0, ge=0)


class QuotaDemand(StrictModel):
    """What a request or submission is about to consume (estimates for compute)."""

    requests: int = Field(0, ge=0, description="API calls (1 per request when rate-limiting)")
    new_runs: int = Field(0, ge=0)
    vcpu_seconds: float = Field(0, ge=0, description="Estimated vCPU-seconds of the new runs")
    fanout_tasks: int = Field(0, ge=0, description="Task count of one fan-out submission")
    storage_bytes: int = Field(0, ge=0)
    egress_bytes: int = Field(0, ge=0)
    resource_class: ResourceClass | None = None


DenyCode = Literal[ErrorCode.QUOTA_EXCEEDED, ErrorCode.RATE_LIMITED]


class QuotaDecision(ResponseModel):
    """The outcome of :func:`evaluate_quota`. ``allowed=False`` carries the first limit hit."""

    allowed: bool
    code: DenyCode | None = None
    scope: QuotaScope | None = None
    subject_id: str | None = None
    limit_name: LimitName | None = None
    limit: float | str | None = None
    used: float | None = None
    requested: float | str | None = None
    retry_after_seconds: int | None = Field(None, ge=0)
    message: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {"allowed": True},
        {
            "allowed": False,
            "code": "QUOTA_EXCEEDED",
            "scope": "tenant",
            "subject_id": "acme",
            "limit_name": "vcpu_seconds_per_day",
            "limit": 86400,
            "used": 85000,
            "requested": 2400,
            "retry_after_seconds": 3600,
            "message": "tenant acme would exceed vcpu_seconds_per_day (85000 + 2400 > 86400)",
        },
        {
            "allowed": False,
            "code": "RATE_LIMITED",
            "scope": "token",
            "subject_id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
            "limit_name": "requests_per_minute",
            "limit": 120,
            "used": 120,
            "requested": 1,
            "retry_after_seconds": 17,
        },
    )

    @model_validator(mode="after")
    def _shape(self) -> QuotaDecision:
        if self.allowed == (self.code is not None):
            raise ValueError("a denial needs a code and an allowance must not have one")
        return self

    @classmethod
    def allow(cls) -> QuotaDecision:
        return cls(allowed=True)

    def to_problem(self, *, instance: str | None = None) -> Problem:
        """The RFC 7807 body (send ``Retry-After`` from :attr:`retry_after_seconds`)."""
        if self.code is None:
            raise ValueError("an allowed decision has no problem")
        return Problem.from_code(self.code, self.message, instance=instance)

    def headers(self) -> dict[str, str]:
        """HTTP headers for a 429 (``Retry-After``)."""
        if self.retry_after_seconds is None:
            return {}
        return {"Retry-After": str(self.retry_after_seconds)}


def window_reset(window: Literal["minute", "day", "month"], now: datetime) -> datetime:
    """Start of the next UTC window after ``now``."""
    now = now.astimezone(UTC)
    if window == "minute":
        return now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )


def _seconds_until(window: Literal["minute", "day", "month"], now: datetime) -> int:
    return max(1, math.ceil((window_reset(window, now) - now.astimezone(UTC)).total_seconds()))


def _fmt(x: float) -> str:
    return f"{x:g}"


def _deny(
    code: DenyCode,
    policy: QuotaPolicy,
    name: LimitName,
    limit: float | str,
    used: float | None,
    requested: float | str,
    retry_after: int | None,
) -> QuotaDecision:
    who = (
        "global"
        if policy.scope is QuotaScope.GLOBAL
        else f"{policy.scope.value} {policy.subject_id}"
    )
    if isinstance(limit, str) or isinstance(requested, str):
        msg = f"{who} does not allow {name.value} {requested} (maximum {limit})"
    elif used is None:
        msg = f"{who} limits {name.value} to {_fmt(limit)} (requested {_fmt(requested)})"
    else:
        msg = f"{who} would exceed {name.value} ({_fmt(used)} + {_fmt(requested)} > {_fmt(limit)})"
    return QuotaDecision(
        allowed=False,
        code=code,
        scope=policy.scope,
        subject_id=policy.subject_id,
        limit_name=name,
        limit=limit,
        used=used,
        requested=requested,
        retry_after_seconds=retry_after,
        message=msg[:500],
    )


def evaluate_quota(
    levels: Iterable[tuple[QuotaPolicy, QuotaUsage]],
    demand: QuotaDemand,
    *,
    now: datetime,
) -> QuotaDecision:
    """Check ``demand`` against every level; return the first violation or an allowance.

    Order: request rate at every level first (``RATE_LIMITED``), then per level from token to
    global (``QUOTA_EXCEEDED``): resource class, fan-out width, concurrent runs, vCPU-seconds
    per day and month, storage, egress. A cumulative limit fails when
    ``used + requested > limit`` and only if the demand asks for some of it, so a subject
    already over its storage quota can still read and delete. ``retry_after_seconds`` is set
    for windowed limits (minute, UTC day, UTC month).
    """
    ordered = sorted(levels, key=lambda pu: pu[0].scope.rank)
    if demand.requests:
        for policy, usage in ordered:
            rpm = policy.limits.requests_per_minute
            if rpm is not None and usage.requests_this_minute + demand.requests > rpm:
                return _deny(
                    ErrorCode.RATE_LIMITED, policy, LimitName.REQUESTS_PER_MINUTE, rpm,
                    usage.requests_this_minute, demand.requests, _seconds_until("minute", now),
                )  # fmt: skip
    q: DenyCode = ErrorCode.QUOTA_EXCEEDED
    for policy, usage in ordered:
        lim = policy.limits
        if (
            demand.resource_class is not None
            and lim.max_resource_class is not None
            and demand.resource_class > lim.max_resource_class
        ):
            return _deny(
                q, policy, LimitName.RESOURCE_CLASS, lim.max_resource_class.value, None,
                demand.resource_class.value, None,
            )  # fmt: skip
        if lim.max_fanout_tasks is not None and demand.fanout_tasks > lim.max_fanout_tasks:
            return _deny(
                q, policy, LimitName.FANOUT_TASKS, lim.max_fanout_tasks, None,
                demand.fanout_tasks, None,
            )  # fmt: skip
        checks: tuple[tuple[LimitName, float | None, float, float, int | None], ...] = (
            (LimitName.CONCURRENT_RUNS, lim.concurrent_runs, usage.concurrent_runs,
             demand.new_runs, None),
            (LimitName.VCPU_SECONDS_PER_DAY, lim.vcpu_seconds_per_day, usage.vcpu_seconds_today,
             demand.vcpu_seconds, _seconds_until("day", now)),
            (LimitName.VCPU_SECONDS_PER_MONTH, lim.vcpu_seconds_per_month,
             usage.vcpu_seconds_month, demand.vcpu_seconds, _seconds_until("month", now)),
            (LimitName.STORAGE_BYTES, lim.storage_bytes, usage.storage_bytes,
             demand.storage_bytes, None),
            (LimitName.EGRESS_BYTES_PER_MONTH, lim.egress_bytes_per_month,
             usage.egress_bytes_month, demand.egress_bytes, _seconds_until("month", now)),
        )  # fmt: skip
        for name, limit, used, requested, retry in checks:
            if limit is not None and requested > 0 and used + requested > limit:
                return _deny(q, policy, name, limit, used, requested, retry)
    return QuotaDecision.allow()


class QuotaLimitStatus(ResponseModel):
    """One limit of one level as shown in ``/usage`` (``remaining`` is ``None`` if unlimited)."""

    scope: QuotaScope
    subject_id: str
    limit_name: LimitName
    limit: float | None = None
    used: float
    remaining: float | None = None
    fraction_used: float | None = Field(None, ge=0)
    resets_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "scope": "tenant",
            "subject_id": "acme",
            "limit_name": "vcpu_seconds_per_day",
            "limit": 86400,
            "used": 43200,
            "remaining": 43200,
            "fraction_used": 0.5,
            "resets_at": "2026-10-16T00:00:00Z",
        }
    )


def quota_status(
    policy: QuotaPolicy, usage: QuotaUsage, *, now: datetime
) -> list[QuotaLimitStatus]:
    """The cumulative limits of one level with their current use (for ``/usage``)."""
    lim = policy.limits
    rows: tuple[tuple[LimitName, float | None, float, datetime | None], ...] = (
        (LimitName.VCPU_SECONDS_PER_DAY, lim.vcpu_seconds_per_day, usage.vcpu_seconds_today,
         window_reset("day", now)),
        (LimitName.VCPU_SECONDS_PER_MONTH, lim.vcpu_seconds_per_month, usage.vcpu_seconds_month,
         window_reset("month", now)),
        (LimitName.CONCURRENT_RUNS, lim.concurrent_runs, usage.concurrent_runs, None),
        (LimitName.STORAGE_BYTES, lim.storage_bytes, usage.storage_bytes, None),
        (LimitName.EGRESS_BYTES_PER_MONTH, lim.egress_bytes_per_month, usage.egress_bytes_month,
         window_reset("month", now)),
    )  # fmt: skip
    out = []
    for name, limit, used, resets in rows:
        out.append(
            QuotaLimitStatus(
                scope=policy.scope,
                subject_id=policy.subject_id,
                limit_name=name,
                limit=limit,
                used=used,
                remaining=None if limit is None else max(0.0, limit - used),
                fraction_used=None if not limit else used / limit,
                resets_at=resets,
            )
        )
    return out


# ================================================================================== fair share


@dataclass(frozen=True, slots=True)
class LeaseCandidate:
    """A queued run the dispatcher could lease (``tasks`` > 1 for ``taskCount`` fan-out)."""

    run_id: str
    tenant_id: str
    priority: PriorityClass
    queued_at: datetime
    tasks: int = 1


def plan_leases(
    candidates: Sequence[LeaseCandidate],
    *,
    capacity: int,
    running_tasks: Mapping[str, int] | None = None,
    concurrency_left: Mapping[str, int | None] | None = None,
    tenant_weights: Mapping[str, float] | None = None,
    weights: Mapping[PriorityClass, float] = PRIORITY_WEIGHTS,
) -> list[LeaseCandidate]:
    """Choose which queued runs to lease now (weighted fair share; pure and deterministic).

    Repeatedly picks the candidate with the smallest virtual finish
    ``(load[tenant] + tasks) / (tenant_weight × priority_weight)``, ties broken by
    ``queued_at`` then ``run_id``. ``load`` starts at the tenant's ``running_tasks`` and grows
    with every pick. A candidate is skipped when its ``tasks`` exceed the remaining global
    ``capacity`` or its tenant has no ``concurrency_left`` (runs). Returns the picks in order.
    """
    load = dict(running_tasks or {})
    left = dict(concurrency_left or {})
    tw = tenant_weights or {}
    remaining = max(0, capacity)
    pool = sorted(candidates, key=lambda c: (c.queued_at, c.run_id))
    picked: list[LeaseCandidate] = []
    while pool and remaining > 0:
        best: LeaseCandidate | None = None
        best_key: tuple[float, datetime, str] | None = None
        for c in pool:
            if c.tasks > remaining:
                continue
            cap = left.get(c.tenant_id)
            if cap is not None and cap <= 0:
                continue
            weight = tw.get(c.tenant_id, 1.0) * weights[c.priority]
            key = ((load.get(c.tenant_id, 0) + c.tasks) / weight, c.queued_at, c.run_id)
            if best_key is None or key < best_key:
                best, best_key = c, key
        if best is None:
            break
        picked.append(best)
        pool.remove(best)
        remaining -= best.tasks
        load[best.tenant_id] = load.get(best.tenant_id, 0) + best.tasks
        cap_left = left.get(best.tenant_id)
        if cap_left is not None:
            left[best.tenant_id] = cap_left - 1
    return picked


def backoff_seconds(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 64.0,
    retry_after: float | None = None,
) -> float:
    """Delay before retry ``attempt`` (1-based) after a 429: ``min(cap, base·2^(attempt-1))``,
    never shorter than a server-sent ``Retry-After``. Callers add jitter."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    delay = min(cap, base * float(2 ** (attempt - 1)))
    return max(delay, retry_after or 0.0)
