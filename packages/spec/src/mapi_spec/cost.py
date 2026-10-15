"""Price tables, cost estimates and the per-run cost ledger (plan §8.2, §13 cost floor, §14 M2).

Money is integer **micros** of a currency (1 USD = 1,000,000 micros); unit prices are
integer **nanos** per vCPU-second and per GiB-second, so every amount is exact and
reproducible from a ledger row (round half to even at the micro). Every amount names the
:class:`PriceTable` ``version`` it was priced with; changing prices never rewrites history.

Usage of an attempt is ``vcpu × billed_seconds × tasks`` vCPU-seconds and
``ram_gib × billed_seconds × tasks`` GiB-seconds, from :data:`RESOURCE_CLASSES`;
``billed_seconds`` is wall time rounded up to the table's billing increment.

:data:`DEFAULT_PRICE_TABLE` mirrors Cloud Run's published list prices (CPU 0.000018 USD per
vCPU-second, memory 0.000002 USD per GiB-second); deployments configure their own. An E33
fit on class M for five minutes costs about 0.03 USD.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from mapi_spec._dto import EXAMPLE_TS, Id, ResponseModel, TenantId, dto_examples, new_id
from mapi_spec._types import StrictModel
from mapi_spec.resources import GIB, RESOURCE_CLASSES, ResourceClass

__all__ = [
    "DEFAULT_PRICE_TABLE",
    "MICROS_PER_UNIT",
    "NANOS_PER_MICRO",
    "ClassPrice",
    "CostEntryKind",
    "CostEstimate",
    "CostLedgerEntry",
    "CostTotals",
    "PriceTable",
    "ResourceUsage",
    "RunCost",
    "class_usage",
    "estimate_cost",
    "format_micros",
    "ledger_entry",
    "price_usage",
    "summarize_ledger",
]

MICROS_PER_UNIT: Final = 1_000_000
NANOS_PER_MICRO: Final = 1_000
_VERSION_RE = r"^[a-z0-9][a-z0-9._-]{0,63}$"
Currency = str


class ClassPrice(StrictModel):
    """Unit prices for one resource class, in nanos of the table's currency."""

    vcpu_second_nanos: int = Field(..., ge=0)
    gib_second_nanos: int = Field(..., ge=0)


class PriceTable(StrictModel):
    """A versioned, immutable price list. Classes without a row cannot be priced."""

    version: str = Field(..., pattern=_VERSION_RE, examples=["cloudrun-list-2026-10"])
    currency: Currency = Field("USD", pattern=r"^[A-Z]{3}$")
    classes: dict[ResourceClass, ClassPrice]
    billing_increment_ms: int = Field(100, ge=1, le=60_000)
    minimum_billable_seconds: float = Field(0, ge=0)
    source: str | None = Field(None, max_length=500)

    def price(self, resource_class: ResourceClass) -> ClassPrice:
        try:
            return self.classes[resource_class]
        except KeyError:
            raise ValueError(
                f"price table {self.version!r} has no price for class {resource_class.value}"
            ) from None

    def billed_seconds(self, wall_seconds: float) -> float:
        """Wall time rounded up to the billing increment (and the minimum)."""
        if wall_seconds < 0 or not math.isfinite(wall_seconds):
            raise ValueError("wall_seconds must be a finite, non-negative number")
        inc = self.billing_increment_ms
        ms = math.ceil(round(wall_seconds * 1000, 6))
        billed = (-(-ms // inc) * inc) / 1000
        return max(billed, self.minimum_billable_seconds) if wall_seconds > 0 else 0.0


_LIST = ClassPrice(vcpu_second_nanos=18_000, gib_second_nanos=2_000)
DEFAULT_PRICE_TABLE: Final = PriceTable(
    version="cloudrun-list-2026-10",
    currency="USD",
    classes={ResourceClass.S: _LIST, ResourceClass.M: _LIST, ResourceClass.L: _LIST},
    billing_increment_ms=100,
    source="Cloud Run list prices (CPU per vCPU-second, memory per GiB-second); "
    "a default for estimates, configure the deployment's own table",
)
"""Cloud Run-like list-price defaults. ``XL`` (GKE, M6) has no default price."""


class ResourceUsage(StrictModel):
    vcpu_seconds: float = Field(..., ge=0)
    gib_seconds: float = Field(..., ge=0)


def class_usage(
    resource_class: ResourceClass,
    wall_seconds: float,
    *,
    task_count: int = 1,
    table: PriceTable = DEFAULT_PRICE_TABLE,
) -> ResourceUsage:
    """Billable usage of ``task_count`` tasks of ``resource_class`` for ``wall_seconds`` each."""
    if task_count < 1:
        raise ValueError("task_count must be >= 1")
    spec = RESOURCE_CLASSES[resource_class]
    if spec.vcpu is None or spec.ram_bytes is None:
        raise ValueError(f"class {resource_class.value} has no fixed size; meter it directly")
    seconds = table.billed_seconds(wall_seconds) * task_count
    return ResourceUsage(
        vcpu_seconds=round(spec.vcpu * seconds, 6),
        gib_seconds=round(spec.ram_bytes / GIB * seconds, 6),
    )


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def price_usage(
    usage: ResourceUsage, resource_class: ResourceClass, table: PriceTable = DEFAULT_PRICE_TABLE
) -> int:
    """Amount in micros (round half to even)."""
    p = table.price(resource_class)
    nanos = _dec(usage.vcpu_seconds) * p.vcpu_second_nanos + _dec(usage.gib_seconds) * (
        p.gib_second_nanos
    )
    return int((nanos / NANOS_PER_MICRO).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def format_micros(amount_micros: int, currency: str) -> str:
    """``31200, "USD"`` → ``"0.031200 USD"`` (exact; no float rounding)."""
    value = Decimal(amount_micros) / MICROS_PER_UNIT
    return f"{value:.6f} {currency}"


class CostEstimate(ResponseModel):
    """A pre-run estimate (shown before submit; checked against vCPU quotas)."""

    resource_class: ResourceClass
    wall_seconds: float = Field(..., ge=0)
    task_count: int = Field(1, ge=1)
    vcpu_seconds: float = Field(..., ge=0)
    gib_seconds: float = Field(..., ge=0)
    amount_micros: int = Field(..., ge=0)
    currency: Currency = Field(..., pattern=r"^[A-Z]{3}$")
    price_version: str = Field(..., pattern=_VERSION_RE)

    model_config = dto_examples(
        {
            "resource_class": "M",
            "wall_seconds": 300,
            "task_count": 1,
            "vcpu_seconds": 1200,
            "gib_seconds": 4800,
            "amount_micros": 31200,
            "currency": "USD",
            "price_version": "cloudrun-list-2026-10",
        }
    )


def estimate_cost(
    resource_class: ResourceClass,
    wall_seconds: float,
    *,
    task_count: int = 1,
    table: PriceTable = DEFAULT_PRICE_TABLE,
) -> CostEstimate:
    """Price ``task_count`` tasks of ``resource_class`` running ``wall_seconds`` each."""
    usage = class_usage(resource_class, wall_seconds, task_count=task_count, table=table)
    return CostEstimate(
        resource_class=resource_class,
        wall_seconds=wall_seconds,
        task_count=task_count,
        vcpu_seconds=usage.vcpu_seconds,
        gib_seconds=usage.gib_seconds,
        amount_micros=price_usage(usage, resource_class, table),
        currency=table.currency,
        price_version=table.version,
    )


class CostEntryKind(StrEnum):
    """``actual``: metered from an attempt; ``adjustment``: an operator correction (may be
    negative). Estimates are never written to the ledger."""

    ACTUAL = "actual"
    ADJUSTMENT = "adjustment"


class CostLedgerEntry(ResponseModel):
    """One append-only ledger row: what one attempt of one run cost."""

    id: Id = Field(..., pattern=r"^cle_[0-9a-z]{26}$")
    tenant_id: TenantId
    project_id: TenantId
    run_id: Id
    attempt: int = Field(..., ge=1)
    kind: CostEntryKind = CostEntryKind.ACTUAL
    resource_class: ResourceClass
    executor: str | None = Field(None, max_length=64)
    token_id: Id | None = Field(None, description="The API token that submitted the run")
    task_count: int = Field(1, ge=1)
    wall_seconds: float = Field(..., ge=0)
    vcpu_seconds: float
    gib_seconds: float
    price_version: str = Field(..., pattern=_VERSION_RE)
    amount_micros: int
    currency: Currency = Field(..., pattern=r"^[A-Z]{3}$")
    recorded_at: AwareDatetime
    note: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "id": "cle_01jaf3t1v2w3x4y5z6a7b8c9d0",
            "tenant_id": "acme",
            "project_id": "growth",
            "run_id": "run_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "attempt": 1,
            "kind": "actual",
            "resource_class": "M",
            "executor": "cloudrun-jobs",
            "task_count": 1,
            "wall_seconds": 300,
            "vcpu_seconds": 1200,
            "gib_seconds": 4800,
            "price_version": "cloudrun-list-2026-10",
            "amount_micros": 31200,
            "currency": "USD",
            "recorded_at": EXAMPLE_TS,
        }
    )

    @model_validator(mode="after")
    def _signs(self) -> CostLedgerEntry:
        if (
            self.kind is CostEntryKind.ACTUAL
            and min(self.vcpu_seconds, self.gib_seconds, self.amount_micros) < 0
        ):
            raise ValueError("actual entries are non-negative")
        return self


def ledger_entry(
    *,
    tenant_id: str,
    project_id: str,
    run_id: str,
    attempt: int,
    resource_class: ResourceClass,
    wall_seconds: float,
    recorded_at: datetime,
    task_count: int = 1,
    executor: str | None = None,
    token_id: str | None = None,
    table: PriceTable = DEFAULT_PRICE_TABLE,
    entry_id: str | None = None,
) -> CostLedgerEntry:
    """Meter and price one finished attempt (``actual`` entry)."""
    usage = class_usage(resource_class, wall_seconds, task_count=task_count, table=table)
    return CostLedgerEntry(
        id=entry_id or new_id("cle"),
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        attempt=attempt,
        resource_class=resource_class,
        executor=executor,
        token_id=token_id,
        task_count=task_count,
        wall_seconds=wall_seconds,
        vcpu_seconds=usage.vcpu_seconds,
        gib_seconds=usage.gib_seconds,
        price_version=table.version,
        amount_micros=price_usage(usage, resource_class, table),
        currency=table.currency,
        recorded_at=recorded_at,
    )


class CostTotals(ResponseModel):
    """Sums over ledger entries (one currency)."""

    vcpu_seconds: float = 0
    gib_seconds: float = 0
    amount_micros: int = 0
    currency: Currency | None = Field(None, pattern=r"^[A-Z]{3}$")
    runs: int = Field(0, ge=0, description="Distinct runs")
    attempts: int = Field(0, ge=0, description="Distinct (run, attempt) pairs")

    model_config = dto_examples(
        {
            "vcpu_seconds": 3600,
            "gib_seconds": 14400,
            "amount_micros": 93600,
            "currency": "USD",
            "runs": 3,
            "attempts": 3,
        }
    )


GroupBy = Literal["resource_class", "project", "day", "month", "token", "run"]
_GROUP_KEYS: dict[str, Callable[[CostLedgerEntry], str]] = {
    "resource_class": lambda e: e.resource_class.value,
    "project": lambda e: e.project_id,
    "day": lambda e: e.recorded_at.date().isoformat(),
    "month": lambda e: e.recorded_at.strftime("%Y-%m"),
    "token": lambda e: e.token_id or "-",
    "run": lambda e: e.run_id,
}


def _totals(entries: list[CostLedgerEntry]) -> CostTotals:
    currencies = {e.currency for e in entries}
    if len(currencies) > 1:
        raise ValueError(f"cannot sum amounts in different currencies: {sorted(currencies)}")
    return CostTotals(
        vcpu_seconds=round(sum(e.vcpu_seconds for e in entries), 6),
        gib_seconds=round(sum(e.gib_seconds for e in entries), 6),
        amount_micros=sum(e.amount_micros for e in entries),
        currency=next(iter(currencies), None),
        runs=len({e.run_id for e in entries}),
        attempts=len({(e.run_id, e.attempt) for e in entries}),
    )


def summarize_ledger(
    entries: Iterable[CostLedgerEntry], group_by: GroupBy | None = None
) -> dict[str, CostTotals]:
    """Totals overall (key ``"total"``) or per group (``day``/``month`` are UTC dates)."""
    items = list(entries)
    if group_by is None:
        return {"total": _totals(items)}
    key = _GROUP_KEYS[group_by]
    groups: dict[str, list[CostLedgerEntry]] = defaultdict(list)
    for e in items:
        groups[key(e)].append(e)
    return {k: _totals(v) for k, v in sorted(groups.items())}


class RunCost(ResponseModel):
    """Cost of a run (all attempts). ``estimated`` is true until an ``actual`` ledger entry
    exists; ``usd`` is kept for v0.2 clients and is set only when the currency is USD."""

    vcpu_seconds: float | None = None
    gib_seconds: float | None = None
    usd: float | None = None
    estimated: bool = True
    amount_micros: int | None = None
    currency: Currency | None = Field(None, pattern=r"^[A-Z]{3}$")
    price_version: str | None = None
    estimate_micros: int | None = Field(None, ge=0, description="Pre-run estimate")
    attempts_billed: int = Field(0, ge=0)

    model_config = dto_examples(
        {"vcpu_seconds": None, "gib_seconds": None, "usd": None, "estimated": True},
        {
            "vcpu_seconds": 1200,
            "gib_seconds": 4800,
            "usd": 0.0312,
            "estimated": False,
            "amount_micros": 31200,
            "currency": "USD",
            "price_version": "cloudrun-list-2026-10",
            "estimate_micros": 30000,
            "attempts_billed": 1,
        },
    )

    @classmethod
    def from_ledger(
        cls, entries: Iterable[CostLedgerEntry], *, estimate: CostEstimate | None = None
    ) -> RunCost:
        """Fold a run's ledger entries (and its pre-run estimate) into the Run DTO field."""
        items = list(entries)
        est = estimate.amount_micros if estimate is not None else None
        if not items:
            if estimate is None:
                return cls()
            return cls(
                vcpu_seconds=estimate.vcpu_seconds,
                gib_seconds=estimate.gib_seconds,
                usd=estimate.amount_micros / MICROS_PER_UNIT
                if estimate.currency == "USD"
                else None,
                estimated=True,
                amount_micros=estimate.amount_micros,
                currency=estimate.currency,
                price_version=estimate.price_version,
                estimate_micros=est,
            )
        t = _totals(items)
        versions = sorted({e.price_version for e in items})
        return cls(
            vcpu_seconds=t.vcpu_seconds,
            gib_seconds=t.gib_seconds,
            usd=t.amount_micros / MICROS_PER_UNIT if t.currency == "USD" else None,
            estimated=not any(e.kind is CostEntryKind.ACTUAL for e in items),
            amount_micros=t.amount_micros,
            currency=t.currency,
            price_version=versions[-1],
            estimate_micros=est,
            attempts_billed=len({e.attempt for e in items if e.kind is CostEntryKind.ACTUAL}),
        )
