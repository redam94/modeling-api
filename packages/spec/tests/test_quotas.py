"""Quota decisions, windows, priority classes and fair-share leasing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_spec.errors import ErrorCode
from mapi_spec.executor import RunKind
from mapi_spec.quotas import (
    DEFAULT_GLOBAL_LIMITS,
    DEFAULT_TIER_LIMITS,
    PRIORITY_WEIGHTS,
    LeaseCandidate,
    LimitName,
    PriorityClass,
    QuotaDecision,
    QuotaDemand,
    QuotaLimits,
    QuotaPolicy,
    QuotaPolicyUpsert,
    QuotaScope,
    QuotaUsage,
    backoff_seconds,
    default_priority,
    evaluate_quota,
    plan_leases,
    policy_for_token,
    quota_status,
    window_reset,
)
from mapi_spec.resources import ResourceClass
from mapi_spec.tenancy import TrustTier
from mapi_spec.tokens import BudgetPeriod, TokenRecord
from pydantic import ValidationError

NOW = datetime(2026, 10, 15, 23, 0, 30, tzinfo=UTC)
TOK = "tok_01jaf3r1s2t3v4w5x6y7z8a9b0"


def _policy(scope: QuotaScope, subject: str, **limits: Any) -> QuotaPolicy:
    return QuotaPolicy(scope=scope, subject_id=subject, limits=QuotaLimits(**limits))


def _levels(
    token: dict[str, Any] | None = None,
    tenant: dict[str, Any] | None = None,
    glob: dict[str, Any] | None = None,
    usage: dict[str, QuotaUsage] | None = None,
) -> list[tuple[QuotaPolicy, QuotaUsage]]:
    usage = usage or {}
    out = []
    if glob is not None:
        out.append((_policy(QuotaScope.GLOBAL, "*", **glob), usage.get("global", QuotaUsage())))
    if tenant is not None:
        out.append(
            (_policy(QuotaScope.TENANT, "acme", **tenant), usage.get("tenant", QuotaUsage()))
        )
    if token is not None:
        out.append((_policy(QuotaScope.TOKEN, TOK, **token), usage.get("token", QuotaUsage())))
    return out


def test_no_limits_allow_everything() -> None:
    d = evaluate_quota(_levels({}, {}, {}), QuotaDemand(requests=1, new_runs=5), now=NOW)
    assert d == QuotaDecision.allow() and d.allowed and d.code is None
    assert evaluate_quota([], QuotaDemand(vcpu_seconds=1e9), now=NOW).allowed


def test_rate_limit_is_checked_first_and_is_retryable() -> None:
    levels = _levels(
        token={"requests_per_minute": 10, "concurrent_runs": 0},
        usage={"token": QuotaUsage(requests_this_minute=10)},
    )
    d = evaluate_quota(levels, QuotaDemand(requests=1, new_runs=1), now=NOW)
    assert not d.allowed and d.code is ErrorCode.RATE_LIMITED
    assert d.limit_name is LimitName.REQUESTS_PER_MINUTE and d.scope is QuotaScope.TOKEN
    assert d.retry_after_seconds == 30
    assert d.headers() == {"Retry-After": "30"}
    p = d.to_problem(instance="/v1/runs")
    assert p.status == 429 and p.code is ErrorCode.RATE_LIMITED


def test_most_specific_level_reports_first() -> None:
    levels = _levels(
        token={"concurrent_runs": 1},
        tenant={"concurrent_runs": 1},
        usage={"token": QuotaUsage(concurrent_runs=1), "tenant": QuotaUsage(concurrent_runs=1)},
    )
    d = evaluate_quota(levels, QuotaDemand(new_runs=1), now=NOW)
    assert d.scope is QuotaScope.TOKEN and d.code is ErrorCode.QUOTA_EXCEEDED


@pytest.mark.parametrize(
    ("limits", "usage", "demand", "name", "retry"),
    [
        ({"concurrent_runs": 2}, {"concurrent_runs": 2}, {"new_runs": 1},
         LimitName.CONCURRENT_RUNS, None),
        ({"vcpu_seconds_per_day": 100}, {"vcpu_seconds_today": 90}, {"vcpu_seconds": 20},
         LimitName.VCPU_SECONDS_PER_DAY, 3570),
        ({"vcpu_seconds_per_month": 100}, {"vcpu_seconds_month": 90}, {"vcpu_seconds": 11},
         LimitName.VCPU_SECONDS_PER_MONTH, 16 * 86400 + 3570),
        ({"storage_bytes": 10}, {"storage_bytes": 10}, {"storage_bytes": 1},
         LimitName.STORAGE_BYTES, None),
        ({"egress_bytes_per_month": 10}, {"egress_bytes_month": 5}, {"egress_bytes": 6},
         LimitName.EGRESS_BYTES_PER_MONTH, 16 * 86400 + 3570),
        ({"max_fanout_tasks": 16}, {}, {"fanout_tasks": 17}, LimitName.FANOUT_TASKS, None),
        ({"max_resource_class": "S"}, {}, {"resource_class": "M"}, LimitName.RESOURCE_CLASS,
         None),
    ],
)  # fmt: skip
def test_each_limit(
    limits: dict[str, Any],
    usage: dict[str, Any],
    demand: dict[str, Any],
    name: LimitName,
    retry: int | None,
) -> None:
    levels = _levels(tenant=limits, usage={"tenant": QuotaUsage(**usage)})
    d = evaluate_quota(levels, QuotaDemand(**demand), now=NOW)
    assert not d.allowed and d.code is ErrorCode.QUOTA_EXCEEDED
    assert d.limit_name is name and d.subject_id == "acme"
    assert d.retry_after_seconds == retry
    assert d.message and "acme" in d.message
    assert d.to_problem().status == 429


def test_boundaries_are_inclusive_and_zero_demand_passes_even_when_over() -> None:
    levels = _levels(
        tenant={"vcpu_seconds_per_day": 100}, usage={"tenant": QuotaUsage(vcpu_seconds_today=90)}
    )
    assert evaluate_quota(levels, QuotaDemand(vcpu_seconds=10), now=NOW).allowed
    over = _levels(tenant={"storage_bytes": 10}, usage={"tenant": QuotaUsage(storage_bytes=50)})
    assert evaluate_quota(over, QuotaDemand(requests=1), now=NOW).allowed, "reads still work"
    assert evaluate_quota(
        _levels(tenant={"max_fanout_tasks": 16}), QuotaDemand(fanout_tasks=16), now=NOW
    ).allowed
    assert evaluate_quota(
        _levels(tenant={"max_resource_class": "M"}), QuotaDemand(resource_class="S"), now=NOW
    ).allowed
    zero = _levels(tenant={"concurrent_runs": 0})
    assert not evaluate_quota(zero, QuotaDemand(new_runs=1), now=NOW).allowed


def test_decision_shape_is_validated() -> None:
    with pytest.raises(ValidationError):
        QuotaDecision(allowed=False)
    with pytest.raises(ValidationError):
        QuotaDecision(allowed=True, code=ErrorCode.QUOTA_EXCEEDED)
    with pytest.raises(ValueError):
        QuotaDecision.allow().to_problem()
    assert QuotaDecision.allow().headers() == {}


@pytest.mark.parametrize(
    ("scope", "subject", "ok"),
    [
        ("global", "*", True),
        ("global", "acme", False),
        ("tenant", "acme", True),
        ("tenant", "*", False),
        ("token", TOK, True),
        ("token", "acme", False),
    ],
)
def test_policy_subjects(scope: str, subject: str, ok: bool) -> None:
    for model in (QuotaPolicy, QuotaPolicyUpsert):
        if ok:
            model.model_validate({"scope": scope, "subject_id": subject, "limits": {}})
        else:
            with pytest.raises(ValidationError):
                model.model_validate({"scope": scope, "subject_id": subject, "limits": {}})


def test_tightened_takes_the_stricter_value_per_field() -> None:
    a = QuotaLimits(concurrent_runs=5, vcpu_seconds_per_day=100, max_resource_class=ResourceClass.L)
    b = QuotaLimits(concurrent_runs=3, storage_bytes=7, max_resource_class=ResourceClass.S)
    t = a.tightened(b)
    assert t.concurrent_runs == 3 and t.vcpu_seconds_per_day == 100 and t.storage_bytes == 7
    assert t.max_resource_class is ResourceClass.S and t.requests_per_minute is None


def test_defaults_free_tier_is_class_s_and_bounded() -> None:
    pub = DEFAULT_TIER_LIMITS[TrustTier.PUBLIC]
    assert pub.max_resource_class is ResourceClass.S
    assert all(getattr(pub, f) is not None for f in QuotaLimits.model_fields)
    assert DEFAULT_TIER_LIMITS[TrustTier.SELF_HOSTED_TRUSTED] == QuotaLimits()
    assert set(DEFAULT_TIER_LIMITS) == set(TrustTier)
    assert DEFAULT_GLOBAL_LIMITS.concurrent_runs == 700


def test_token_policy_from_record() -> None:
    rec = TokenRecord(
        id=TOK,
        tenant_id="acme",
        project_id="growth",
        owner_id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
        name="ci",
        prefix="7k2m9q4x",
        secret_hash="hmac-sha256:" + "0" * 64,
        scopes=["models:read"],
        rate_limit_per_minute=60,
        vcpu_seconds_budget=500,
        budget_period=BudgetPeriod.DAY,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    p = policy_for_token(rec)
    assert p.scope is QuotaScope.TOKEN and p.subject_id == TOK
    assert p.limits.requests_per_minute == 60 and p.limits.vcpu_seconds_per_day == 500
    assert p.limits.vcpu_seconds_per_month is None
    monthly = policy_for_token(rec.model_copy(update={"budget_period": BudgetPeriod.MONTH}))
    assert monthly.limits.vcpu_seconds_per_month == 500


def test_windows() -> None:
    assert window_reset("minute", NOW) == datetime(2026, 10, 15, 23, 1, tzinfo=UTC)
    assert window_reset("day", NOW) == datetime(2026, 10, 16, tzinfo=UTC)
    assert window_reset("month", NOW) == datetime(2026, 11, 1, tzinfo=UTC)
    dec = datetime(2026, 12, 31, 12, tzinfo=UTC)
    assert window_reset("month", dec) == datetime(2027, 1, 1, tzinfo=UTC)


def test_quota_status_rows() -> None:
    pol = _policy(QuotaScope.TENANT, "acme", vcpu_seconds_per_day=100)
    rows = quota_status(pol, QuotaUsage(vcpu_seconds_today=25, storage_bytes=3), now=NOW)
    by = {r.limit_name: r for r in rows}
    day = by[LimitName.VCPU_SECONDS_PER_DAY]
    assert day.remaining == 75 and day.fraction_used == 0.25
    assert day.resets_at == datetime(2026, 10, 16, tzinfo=UTC)
    storage = by[LimitName.STORAGE_BYTES]
    assert storage.limit is None and storage.remaining is None and storage.used == 3


# --------------------------------------------------------------------------------- priorities


@pytest.mark.parametrize(
    ("kind", "cls"),
    [
        (RunKind.SCORE, PriorityClass.INTERACTIVE),
        (RunKind.RENDER, PriorityClass.INTERACTIVE),
        (RunKind.VALIDATE, PriorityClass.INTERACTIVE),
        (RunKind.FIT, PriorityClass.BATCH),
        (RunKind.INGEST, PriorityClass.BATCH),
        (RunKind.SBC, PriorityClass.BATCH),
    ],
)
def test_default_priority(kind: RunKind, cls: PriorityClass) -> None:
    assert default_priority(kind) is cls and default_priority(kind.value) is cls


def test_interactive_outweighs_batch() -> None:
    assert PRIORITY_WEIGHTS[PriorityClass.INTERACTIVE] > PRIORITY_WEIGHTS[PriorityClass.BATCH]


T0 = datetime(2026, 10, 15, tzinfo=UTC)


def _c(run: str, tenant: str, prio: str = "batch", t: int = 0, tasks: int = 1) -> LeaseCandidate:
    return LeaseCandidate(run, tenant, PriorityClass(prio), T0 + timedelta(seconds=t), tasks)


def test_one_tenant_sweep_cannot_starve_others() -> None:
    sweep = [_c(f"a{i}", "a", t=i) for i in range(50)]
    late = [_c("b0", "b", t=100), _c("c0", "c", "interactive", t=200)]
    picks = [c.run_id for c in plan_leases(sweep + late, capacity=4)]
    assert "b0" in picks and "c0" in picks
    assert picks[0] == "c0", "interactive work of an idle tenant goes first"


def test_running_load_and_fifo_tie_breaks() -> None:
    cands = [_c("a1", "a", t=1), _c("b1", "b", t=2), _c("a0", "a", t=0)]
    assert [c.run_id for c in plan_leases(cands, capacity=3)] == ["a0", "b1", "a1"]
    busy = plan_leases(cands, capacity=1, running_tasks={"a": 10})
    assert [c.run_id for c in busy] == ["b1"]


def test_capacity_concurrency_and_task_count_limits() -> None:
    cands = [_c("big", "a", tasks=8), _c("small", "b", t=1, tasks=2), _c("x", "c", t=2)]
    picks = plan_leases(cands, capacity=3)
    assert [c.run_id for c in picks] == ["x", "small"], "the 8-task fan-out waits for room"
    capped = plan_leases([_c("a0", "a"), _c("a1", "a", t=1)], capacity=5, concurrency_left={"a": 1})
    assert [c.run_id for c in capped] == ["a0"]
    assert plan_leases(cands, capacity=0) == []
    weighted = plan_leases(
        [_c("a0", "a"), _c("b0", "b")], capacity=1, running_tasks={"a": 3, "b": 1},
        tenant_weights={"a": 10.0},
    )  # fmt: skip
    assert [c.run_id for c in weighted] == ["a0"]


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["a", "b", "c"]),
            st.sampled_from(["interactive", "batch"]),
            st.integers(1, 4),
        ),
        max_size=25,
    ),
    st.integers(0, 30),
)
def test_lease_plan_invariants(rows: list[tuple[str, str, int]], capacity: int) -> None:
    cands = [_c(f"r{i}", t, p, t=i, tasks=n) for i, (t, p, n) in enumerate(rows)]
    picks = plan_leases(cands, capacity=capacity)
    assert sum(c.tasks for c in picks) <= capacity
    assert len({c.run_id for c in picks}) == len(picks)
    assert plan_leases(cands, capacity=capacity) == picks, "deterministic"
    leftover = [c for c in cands if c not in picks]
    room = capacity - sum(c.tasks for c in picks)
    assert all(c.tasks > room for c in leftover), "work conserving"


def test_backoff() -> None:
    assert [backoff_seconds(i) for i in range(1, 9)] == [1, 2, 4, 8, 16, 32, 64, 64]
    assert backoff_seconds(1, retry_after=30) == 30
    assert backoff_seconds(3, base=0.5, cap=1.5) == 1.5
    with pytest.raises(ValueError):
        backoff_seconds(0)
