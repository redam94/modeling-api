"""Cost math: exact integer pricing, billing increments, ledger folding into RunCost."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_spec.api import Run
from mapi_spec.cost import (
    DEFAULT_PRICE_TABLE,
    ClassPrice,
    CostEntryKind,
    CostLedgerEntry,
    PriceTable,
    ResourceUsage,
    RunCost,
    class_usage,
    estimate_cost,
    format_micros,
    ledger_entry,
    price_usage,
    summarize_ledger,
)
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from pydantic import ValidationError

M, S, L, XL = ResourceClass.M, ResourceClass.S, ResourceClass.L, ResourceClass.XL
T0 = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


def test_e33_fit_on_m_for_five_minutes_costs_about_three_cents() -> None:
    est = estimate_cost(M, 300)
    assert (est.vcpu_seconds, est.gib_seconds) == (1200, 4800)
    # 1200 vCPU-s x 18,000 nanos + 4800 GiB-s x 2,000 nanos = 31,200,000 nanos = 31,200 micros
    assert est.amount_micros == 31_200
    assert format_micros(est.amount_micros, est.currency) == "0.031200 USD"
    assert est.price_version == DEFAULT_PRICE_TABLE.version


@pytest.mark.parametrize("rc", [S, M, L])
def test_usage_scales_with_class_size_and_tasks(rc: ResourceClass) -> None:
    spec = RESOURCE_CLASSES[rc]
    assert spec.vcpu is not None and spec.ram_bytes is not None
    one = class_usage(rc, 10)
    four = class_usage(rc, 10, task_count=4)
    assert one.vcpu_seconds == spec.vcpu * 10
    assert one.gib_seconds == spec.ram_bytes / 1024**3 * 10
    assert four.vcpu_seconds == 4 * one.vcpu_seconds
    assert price_usage(four, rc) == 4 * price_usage(one, rc)


@pytest.mark.parametrize(
    ("wall", "billed"),
    [(0, 0.0), (0.001, 0.1), (0.1, 0.1), (0.30001, 0.4), (0.3, 0.3), (59.95, 60.0), (60, 60.0)],
)
def test_billing_increment_rounds_up(wall: float, billed: float) -> None:
    assert DEFAULT_PRICE_TABLE.billed_seconds(wall) == billed


def test_minimum_billable_seconds_and_invalid_inputs() -> None:
    table = DEFAULT_PRICE_TABLE.model_copy(update={"minimum_billable_seconds": 60.0})
    assert table.billed_seconds(5) == 60 and table.billed_seconds(0) == 0
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            DEFAULT_PRICE_TABLE.billed_seconds(bad)
    with pytest.raises(ValueError, match="task_count"):
        class_usage(M, 1, task_count=0)


def test_xl_and_unpriced_classes_cannot_be_estimated() -> None:
    with pytest.raises(ValueError, match="no fixed size"):
        class_usage(XL, 10)
    only_s = PriceTable(
        version="s-only", classes={S: ClassPrice(vcpu_second_nanos=1, gib_second_nanos=1)}
    )
    with pytest.raises(ValueError, match="no price"):
        estimate_cost(M, 10, table=only_s)


def test_rounding_is_half_even_at_the_micro() -> None:
    table = PriceTable(
        version="t", classes={S: ClassPrice(vcpu_second_nanos=500, gib_second_nanos=0)}
    )
    assert price_usage(ResourceUsage(vcpu_seconds=1, gib_seconds=0), S, table) == 0  # 0.5 -> 0
    assert price_usage(ResourceUsage(vcpu_seconds=3, gib_seconds=0), S, table) == 2  # 1.5 -> 2
    assert price_usage(ResourceUsage(vcpu_seconds=5, gib_seconds=0), S, table) == 2  # 2.5 -> 2


@given(st.integers(0, 10_000_000), st.integers(0, 10_000))
def test_pricing_is_exact_against_decimal(vcpu_tenths: int, gib_tenths: int) -> None:
    usage = ResourceUsage(vcpu_seconds=vcpu_tenths / 10, gib_seconds=gib_tenths / 10)
    exact = (Decimal(vcpu_tenths) * 18_000 + Decimal(gib_tenths) * 2_000) / 10 / 1000
    assert price_usage(usage, M) == int(exact.to_integral_value(rounding="ROUND_HALF_EVEN"))


def test_price_table_validation() -> None:
    with pytest.raises(ValidationError):
        PriceTable(version="Bad Version", classes={})
    with pytest.raises(ValidationError):
        PriceTable(version="v", currency="usd", classes={})
    with pytest.raises(ValidationError):
        ClassPrice(vcpu_second_nanos=-1, gib_second_nanos=0)
    assert format_micros(-1_500_000, "EUR") == "-1.500000 EUR"


def _entry(run: str = "run_1", attempt: int = 1, **kw: object) -> CostLedgerEntry:
    e = ledger_entry(
        tenant_id="acme",
        project_id="growth",
        run_id=run,
        attempt=attempt,
        resource_class=M,
        wall_seconds=300,
        recorded_at=T0,
    )
    return e.model_copy(update=kw) if kw else e


def test_ledger_entries_are_priced_and_identified() -> None:
    e = _entry()
    assert e.id.startswith("cle_") and len(e.id) == 30
    assert e.amount_micros == 31_200 and e.kind is CostEntryKind.ACTUAL
    assert e.price_version == DEFAULT_PRICE_TABLE.version
    with pytest.raises(ValidationError, match="non-negative"):
        CostLedgerEntry.model_validate({**e.model_dump(), "amount_micros": -1})
    adj = CostLedgerEntry.model_validate(
        {**e.model_dump(), "kind": "adjustment", "amount_micros": -31_200, "vcpu_seconds": 0}
    )
    assert adj.amount_micros < 0


def test_summaries_group_and_refuse_mixed_currencies() -> None:
    entries = [
        _entry("run_1", 1),
        _entry("run_1", 2),
        _entry("run_2", 1, project_id="retail", resource_class=S),
    ]
    total = summarize_ledger(entries)["total"]
    assert total.runs == 2 and total.attempts == 3 and total.amount_micros == 3 * 31_200
    by_project = summarize_ledger(entries, "project")
    assert set(by_project) == {"growth", "retail"} and by_project["growth"].attempts == 2
    assert set(summarize_ledger(entries, "resource_class")) == {"M", "S"}
    assert set(summarize_ledger(entries, "day")) == {"2026-10-15"}
    assert set(summarize_ledger(entries, "month")) == {"2026-10"}
    assert set(summarize_ledger(entries, "token")) == {"-"}
    assert summarize_ledger([])["total"].currency is None
    with pytest.raises(ValueError, match="currencies"):
        summarize_ledger([*entries, _entry("run_3", currency="EUR")])


def test_run_cost_from_ledger() -> None:
    est = estimate_cost(M, 280)
    assert RunCost.from_ledger([]) == RunCost()
    pre = RunCost.from_ledger([], estimate=est)
    assert pre.estimated and pre.amount_micros == est.amount_micros == pre.estimate_micros
    cost = RunCost.from_ledger([_entry(attempt=1), _entry(attempt=2)], estimate=est)
    assert not cost.estimated and cost.attempts_billed == 2
    assert cost.amount_micros == 62_400 and cost.usd == pytest.approx(0.0624)
    assert cost.vcpu_seconds == 2400 and cost.estimate_micros == est.amount_micros
    eur = RunCost.from_ledger([_entry(currency="EUR")])
    assert eur.usd is None and eur.currency == "EUR"
    adj_only = RunCost.from_ledger([_entry(kind=CostEntryKind.ADJUSTMENT)])
    assert adj_only.estimated and adj_only.attempts_billed == 0


def test_run_dto_keeps_accepting_v02_cost_payloads() -> None:
    example = Run.model_json_schema()["examples"][0]
    run = Run.model_validate({**example, "cost": {"vcpu_seconds": None, "usd": None}})
    assert run.cost.estimated and run.cost.amount_micros is None
    later = Run.model_validate({**example, "cost": RunCost.model_json_schema()["examples"][1]})
    assert later.cost.amount_micros == 31_200 and later.cost.usd == 0.0312
