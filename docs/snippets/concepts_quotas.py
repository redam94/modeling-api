"""Concepts: quota decisions, fair-share leasing and cost (mapi_spec only).

Included by ``docs/concepts/quotas-and-cost.md``; run by ``conformance/tests/test_docs_snippets.py``.
"""

from __future__ import annotations

# --8<-- [start:decide]
from datetime import UTC, datetime

from mapi_spec.quotas import (
    QuotaDemand,
    QuotaLimits,
    QuotaPolicy,
    QuotaScope,
    QuotaUsage,
    evaluate_quota,
)
from mapi_spec.resources import ResourceClass

now = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
tenant = QuotaPolicy(
    scope=QuotaScope.TENANT,
    subject_id="acme",
    limits=QuotaLimits(vcpu_seconds_per_day=86_400, max_resource_class=ResourceClass.L),
)
used = QuotaUsage(vcpu_seconds_today=85_000)

fit = QuotaDemand(new_runs=1, vcpu_seconds=2_400, resource_class=ResourceClass.M)
decision = evaluate_quota([(tenant, used)], fit, now=now)
print(decision.allowed, decision.code, decision.limit_name)
# False QUOTA_EXCEEDED vcpu_seconds_per_day
print(decision.retry_after_seconds)  # 7200: the UTC day resets in two hours
print(decision.to_problem().status, decision.headers())  # 429 {'Retry-After': '7200'}
# --8<-- [end:decide]

# --8<-- [start:lease]
from datetime import timedelta

from mapi_spec.quotas import LeaseCandidate, PriorityClass, plan_leases

t0 = datetime(2026, 10, 15, tzinfo=UTC)
queue = [
    LeaseCandidate(f"sbc_{i}", "acme", PriorityClass.BATCH, t0 + timedelta(seconds=i))
    for i in range(200)
] + [LeaseCandidate("score_1", "beta", PriorityClass.INTERACTIVE, t0 + timedelta(minutes=5))]

picked = plan_leases(queue, capacity=3, running_tasks={"acme": 40})
print([c.run_id for c in picked])  # ['score_1', 'sbc_0', 'sbc_1']
# --8<-- [end:lease]

# --8<-- [start:cost]
from mapi_spec.cost import DEFAULT_PRICE_TABLE, estimate_cost, format_micros

est = estimate_cost(ResourceClass.M, wall_seconds=300)  # an E33-style fit, five minutes on M
print(est.vcpu_seconds, est.gib_seconds)  # 1200.0 4800.0
print(est.amount_micros, format_micros(est.amount_micros, est.currency))  # 31200 0.031200 USD
print(est.price_version == DEFAULT_PRICE_TABLE.version)  # True

sweep = estimate_cost(ResourceClass.S, wall_seconds=120, task_count=50)
print(format_micros(sweep.amount_micros, sweep.currency))  # 0.312000 USD
# --8<-- [end:cost]
