"""The cost ledger: meter finished attempts, fold them into ``Run.cost``, report usage.

An attempt is metered once its execution is over (``submitted_at`` and ``finished_at`` are
both set by the dispatcher/reconciler): wall time × the class's vCPU and memory × the price
table (:func:`mapi_spec.cost.ledger_entry`). :func:`bill_attempts` runs inside every state
transition that ends an attempt (a new attempt, ``FINALIZING`` or a terminal state); the
partial unique index on ``(run_id, attempt) WHERE kind = 'actual'`` makes it idempotent.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from mapi_spec.cost import (
    DEFAULT_PRICE_TABLE,
    CostEntryKind,
    CostEstimate,
    CostLedgerEntry,
    PriceTable,
    RunCost,
    estimate_cost,
    ledger_entry,
)
from mapi_spec.resources import ResourceClass
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m

__all__ = [
    "PRICE_TABLE_INFO_KEY",
    "bill_attempts",
    "fold_cost",
    "ledger_dto",
    "load_price_table",
    "run_costs",
    "run_estimate",
]

log = logging.getLogger(__name__)

PRICE_TABLE_INFO_KEY = "price_table"
"""``Session.info`` key under which :class:`~mapi_server.db.Database` carries the table."""


def load_price_table(path: Path | None) -> PriceTable:
    """The deployment's price table (JSON :class:`PriceTable`) or the default list prices."""
    if path is None:
        return DEFAULT_PRICE_TABLE
    return PriceTable.model_validate(json.loads(path.read_text()))


def _table(session: AsyncSession) -> PriceTable:
    table = session.info.get(PRICE_TABLE_INFO_KEY)
    return table if isinstance(table, PriceTable) else DEFAULT_PRICE_TABLE


def run_estimate(
    table: PriceTable, resource_class: ResourceClass, wall_seconds: float, task_count: int = 1
) -> CostEstimate | None:
    """The pre-run estimate (``None`` for classes the table cannot price, e.g. XL)."""
    try:
        return estimate_cost(resource_class, wall_seconds, task_count=task_count, table=table)
    except ValueError:
        return None


def ledger_dto(row: m.CostLedgerEntry) -> CostLedgerEntry:
    return CostLedgerEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        run_id=row.run_id,
        attempt=row.attempt,
        kind=CostEntryKind(row.kind),
        resource_class=ResourceClass(row.resource_class),
        executor=row.executor,
        token_id=row.token_id,
        task_count=row.task_count,
        wall_seconds=row.wall_seconds,
        vcpu_seconds=row.vcpu_seconds,
        gib_seconds=row.gib_seconds,
        price_version=row.price_version,
        amount_micros=row.amount_micros,
        currency=row.currency,
        recorded_at=row.recorded_at,
        note=row.note,
    )


async def bill_attempts(
    session: AsyncSession, run: m.Run, *, at: datetime | None = None
) -> list[CostLedgerEntry]:
    """Write an ``actual`` ledger entry for every finished, not yet metered attempt."""
    attempts = (
        await session.execute(
            select(m.RunAttempt).where(
                m.RunAttempt.run_id == run.id,
                m.RunAttempt.submitted_at.is_not(None),
                m.RunAttempt.finished_at.is_not(None),
            )
        )
    ).scalars()
    billed = set(
        (
            await session.execute(
                select(m.CostLedgerEntry.attempt).where(
                    m.CostLedgerEntry.run_id == run.id,
                    m.CostLedgerEntry.kind == CostEntryKind.ACTUAL.value,
                )
            )
        ).scalars()
    )
    table = _table(session)
    written: list[CostLedgerEntry] = []
    for att in attempts:
        if att.attempt in billed or att.submitted_at is None or att.finished_at is None:
            continue
        wall = max(0.0, (att.finished_at - att.submitted_at).total_seconds())
        try:
            entry = ledger_entry(
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                run_id=run.id,
                attempt=att.attempt,
                resource_class=ResourceClass(run.resource_class),
                wall_seconds=round(wall, 3),
                recorded_at=at or att.finished_at,
                task_count=max(1, run.task_count or 1),
                executor=att.executor,
                token_id=run.token_id,
                table=table,
            )
        except ValueError as exc:
            log.warning("run %s/%s cannot be priced: %s", run.id, att.attempt, exc)
            continue
        await session.execute(
            insert(m.CostLedgerEntry)
            .values(
                **entry.model_dump(mode="python", exclude={"kind", "resource_class"}),
                kind=entry.kind.value,
                resource_class=entry.resource_class.value,
            )
            .on_conflict_do_nothing()
        )
        written.append(entry)
    return written


async def run_costs(session: AsyncSession, runs: Sequence[m.Run]) -> dict[str, RunCost]:
    """``Run.cost`` for each run: its ledger entries folded with its pre-run estimate."""
    if not runs:
        return {}
    rows = (
        await session.execute(
            select(m.CostLedgerEntry).where(m.CostLedgerEntry.run_id.in_([r.id for r in runs]))
        )
    ).scalars()
    by_run: dict[str, list[CostLedgerEntry]] = defaultdict(list)
    for row in rows:
        by_run[row.run_id].append(ledger_dto(row))
    out: dict[str, RunCost] = {}
    for run in runs:
        out[run.id] = fold_cost(by_run.get(run.id, ()), run.estimate)
    return out


def fold_cost(entries: Iterable[CostLedgerEntry], estimate: dict[str, Any] | None) -> RunCost:
    est = None
    if estimate:
        try:
            est = CostEstimate.model_validate(estimate)
        except ValueError:
            est = None
    try:
        return RunCost.from_ledger(entries, estimate=est)
    except ValueError:  # mixed currencies after a price-table change: show the estimate only
        return RunCost.from_ledger((), estimate=est)
