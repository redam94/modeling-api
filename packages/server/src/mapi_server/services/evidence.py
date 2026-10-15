"""Executor evidence: the shared-tier gate of plan §8.4.

An executor takes ``verified`` and ``public`` tenants only when (a) its caps allow it
(isolation >= gVisor with egress and DNS enforced, :func:`mapi_server.executors.check_placement`)
and (b) a passing, unexpired executor-conformance report *and* security-suite report were
recorded for exactly those caps (:func:`mapi_spec.admin.shared_tiers_problem`). The caps are
what the orchestrator declares; the reports are what makes the declaration count.

Reports are recorded by operators (``PUT /v1/admin/executors/{name}/evidence``, audited) or
by the nightly suites through the same endpoint, and kept in ``platform_settings`` under
``evidence/<executor>``. ``internal`` and ``self_hosted_trusted`` tenants are not gated.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from mapi_spec.admin import (
    ExecutorAttestation,
    ExecutorEvidence,
    ExecutorEvidenceUpdate,
    shared_tiers_problem,
)
from mapi_spec.errors import MapiError
from mapi_spec.executor import ExecutorCaps
from mapi_spec.resources import ResourceClass
from mapi_spec.tenancy import TIER_PLACEMENT, TrustTier
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.executors import check_placement

__all__ = [
    "EVIDENCE_KEY_PREFIX",
    "FRESH_REPORT",
    "evidence_key",
    "gated_tier",
    "load_evidence",
    "placement_problem",
    "record_evidence",
    "shared_tiers_gate",
]

EVIDENCE_KEY_PREFIX: Final = "evidence/"
FRESH_REPORT: Final = timedelta(minutes=10)
"""Executor reports older than this are ignored when admitting runs (a retired config)."""


def evidence_key(executor: str) -> str:
    key = EVIDENCE_KEY_PREFIX + executor
    if len(key) > 64:
        raise MapiError(f"executor name {executor!r} is too long to record evidence for")
    return key


def gated_tier(tier: str) -> bool:
    """Is ``tier`` a shared tier that needs recorded evidence (``verified``, ``public``)?
    Unknown tiers are treated as ``public``."""
    try:
        t = TrustTier(tier)
    except ValueError:
        t = TrustTier.PUBLIC
    return TIER_PLACEMENT[t].egress_enforced


async def load_evidence(session: AsyncSession, executor: str) -> ExecutorEvidence | None:
    if len(EVIDENCE_KEY_PREFIX + executor) > 64:
        return None
    row = await session.get(m.PlatformSetting, evidence_key(executor))
    return None if row is None else ExecutorEvidence.model_validate(row.value)


async def shared_tiers_gate(
    session: AsyncSession, executor: str, caps: ExecutorCaps, *, now: datetime
) -> str | None:
    """Why ``executor`` (reporting ``caps``) may not take shared tiers; ``None`` if it may."""
    if not caps.allows_public_tenants():
        return "its caps do not allow shared tiers (isolation >= gVisor, egress and DNS enforced)"
    return shared_tiers_problem(await load_evidence(session, executor), caps, now=now)


async def record_evidence(
    session: AsyncSession,
    update: ExecutorEvidenceUpdate,
    *,
    by: str | None,
    now: datetime,
    max_age: timedelta,
) -> ExecutorEvidence:
    """Store each report of ``update`` with its verdict for ``update.target`` (row locked)."""
    name = update.target.executor
    key = evidence_key(name)
    await session.execute(
        select(m.PlatformSetting).where(m.PlatformSetting.key == key).with_for_update()
    )
    current = await load_evidence(session, name) or ExecutorEvidence(executor=name)
    caps = update.target.caps
    changes: dict[str, object] = {}
    if update.conformance is not None:
        report = update.conformance
        at = min(report.finished_at or report.started_at, now)
        changes["conformance"] = report
        changes["conformance_attestation"] = ExecutorAttestation(
            caps=caps,
            passed=report.passed_for(update.target),
            recorded_at=now,
            expires_at=at + max_age,
            recorded_by=by,
        )
    if update.security is not None:
        sec = update.security
        at = min(sec.started_at or now, now)
        changes["security"] = sec
        changes["security_attestation"] = ExecutorAttestation(
            caps=caps,
            passed=sec.passed_for(name, caps),
            recorded_at=now,
            expires_at=at + max_age,
            recorded_by=by,
        )
    evidence = current.model_copy(update=changes)
    value = evidence.model_dump(mode="json")
    await session.execute(
        insert(m.PlatformSetting)
        .values(key=key, value=value, updated_at=now)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
    )
    return evidence


async def placement_problem(
    system: AsyncSession,
    *,
    tier: str,
    resource_class: ResourceClass,
    residency: str | None,
    now: datetime,
) -> str | None:
    """Can any executor that reported recently place this run? ``None`` when one can, or
    when no executor has reported yet (the dispatcher decides then); otherwise why not.

    Checked at submission so a run nobody can place is refused up front instead of being
    queued and then failed as an infrastructure error at lease."""
    rows = list(
        (
            await system.execute(
                select(m.ExecutorReport).where(
                    m.ExecutorReport.enabled.is_(True),
                    m.ExecutorReport.checked_at >= now - FRESH_REPORT,
                )
            )
        ).scalars()
    )
    if not rows:
        return None
    problems: list[str] = []
    for row in rows:
        caps = ExecutorCaps.model_validate(row.caps)
        try:
            check_placement(
                caps, trust_tier=tier, resource_class=resource_class, residency=residency
            )
        except MapiError as exc:
            problems.append(f"{row.name}: {exc.detail}")
            continue
        if gated_tier(tier):
            problem = await shared_tiers_gate(system, row.name, caps, now=now)
            if problem is not None:
                problems.append(f"{row.name}: {problem}")
                continue
        return None
    return "; ".join(problems)
