"""v0.15 GPU resource class: ordering, the retry ladder, budgets, prices and quotas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mapi_spec.cost import (
    DEFAULT_PRICE_TABLE,
    ClassPrice,
    ResourceUsage,
    class_usage,
    estimate_cost,
    price_usage,
)
from mapi_spec.quotas import (
    DEFAULT_TIER_LIMITS,
    QuotaDemand,
    QuotaLimits,
    QuotaPolicy,
    QuotaScope,
    QuotaUsage,
    evaluate_quota,
)
from mapi_spec.resources import CPU_CLASSES, RESOURCE_CLASSES, ResourceClass
from mapi_spec.tenancy import TrustTier

pytestmark = pytest.mark.unit

S, M, L, XL, GPU = (ResourceClass(c) for c in ("S", "M", "L", "XL", "GPU"))
NOW = datetime(2026, 10, 15, 23, 0, 30, tzinfo=UTC)


def test_gpu_is_the_last_class() -> None:
    assert list(ResourceClass) == [S, M, L, XL, GPU]
    assert GPU.rank == 4 and max(ResourceClass) is GPU
    assert all(c < GPU for c in CPU_CLASSES)
    assert CPU_CLASSES == (S, M, L, XL) and GPU not in CPU_CLASSES


def test_next_up_never_offers_the_gpu() -> None:
    assert [c.next_up() for c in ResourceClass] == [M, L, XL, None, None]


def test_is_gpu_and_budget() -> None:
    assert [c.is_gpu for c in ResourceClass] == [False, False, False, False, True]
    spec = RESOURCE_CLASSES[GPU]
    assert (spec.vcpu, spec.ram_bytes) == (8, 32 * 1024**3)
    assert (spec.accelerator, spec.accelerators) == ("nvidia-l4", 1)
    assert all(RESOURCE_CLASSES[c].accelerators == 0 for c in CPU_CLASSES)
    assert RESOURCE_CLASSES[S].accelerator is None


def test_gpu_price_adds_accelerator_seconds() -> None:
    price = DEFAULT_PRICE_TABLE.price(GPU)
    assert price.accelerator_second_nanos == 186_700
    usage = class_usage(GPU, 10)
    assert (usage.vcpu_seconds, usage.gib_seconds, usage.accelerator_seconds) == (80, 320, 10)
    # 80 x 18,000 + 320 x 2,000 + 10 x 186,700 nanos = 3,947,000 nanos = 3,947 micros
    assert price_usage(usage, GPU) == 3_947
    assert estimate_cost(GPU, 10).amount_micros == 3_947
    assert class_usage(GPU, 10, task_count=2).accelerator_seconds == 20


def test_cpu_documents_keep_their_shape() -> None:
    # accelerator fields are left out when zero, so stored prices and usages do not change
    assert class_usage(L, 10).model_dump() == {"vcpu_seconds": 80, "gib_seconds": 320}
    cpu = ClassPrice(vcpu_second_nanos=1, gib_second_nanos=2)
    assert cpu.model_dump() == {"vcpu_second_nanos": 1, "gib_second_nanos": 2}
    assert ResourceUsage(vcpu_seconds=1, gib_seconds=1).accelerator_seconds == 0


def _tenant(max_class: ResourceClass | None) -> list[tuple[QuotaPolicy, QuotaUsage]]:
    policy = QuotaPolicy(
        scope=QuotaScope.TENANT,
        subject_id="acme",
        limits=QuotaLimits(max_resource_class=max_class),
    )
    return [(policy, QuotaUsage())]


def test_quotas_below_gpu_refuse_it() -> None:
    demand = QuotaDemand(resource_class=GPU)
    assert not evaluate_quota(_tenant(L), demand, now=NOW).allowed
    assert not evaluate_quota(_tenant(XL), demand, now=NOW).allowed
    assert evaluate_quota(_tenant(GPU), demand, now=NOW).allowed
    assert evaluate_quota(_tenant(None), demand, now=NOW).allowed
    # default tiers: public and verified stop below GPU; internal and self-hosted allow it
    assert DEFAULT_TIER_LIMITS[TrustTier.PUBLIC].max_resource_class is S
    assert DEFAULT_TIER_LIMITS[TrustTier.VERIFIED].max_resource_class is L
    assert DEFAULT_TIER_LIMITS[TrustTier.INTERNAL].max_resource_class is None
    assert DEFAULT_TIER_LIMITS[TrustTier.SELF_HOSTED_TRUSTED].max_resource_class is None
