"""Append-only audit log (plan §9, §11 Admin): authentication, tokens, admin and run actions.

Successful actions are written in the request's own transaction (so a rolled-back request
leaves no "success" row); denials and failures are written in a separate platform-role
transaction (so they survive the error that follows them). Rows never hold raw client IPs,
user agents, token values or signed URLs (:class:`mapi_spec.audit.AuditEvent` refuses them).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from mapi_spec.audit import AuditAction, AuditEvent, AuditOutcome, AuditQuery
from mapi_spec.errors import ErrorCode
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import Database
from mapi_server.db import models as m

__all__ = ["add_audit", "audit_dto", "audit_query", "write_audit"]

log = logging.getLogger(__name__)


def _row(ev: AuditEvent) -> m.AuditEvent:
    return m.AuditEvent(
        id=ev.id,
        ts=ev.ts,
        tenant_id=ev.tenant_id,
        project_id=ev.project_id,
        actor_kind=ev.actor_kind.value if ev.actor_kind else None,
        actor_id=ev.actor_id,
        token_id=ev.token_id,
        operator=ev.operator,
        action=ev.action.value,
        resource_kind=ev.resource_kind.value,
        resource_id=ev.resource_id,
        outcome=ev.outcome.value,
        code=ev.code.value if ev.code else None,
        ip_hash=ev.ip_hash,
        user_agent_hash=ev.user_agent_hash,
        request_id=ev.request_id,
        details=dict(ev.details),
    )


def audit_dto(row: m.AuditEvent) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        ts=row.ts,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        actor_kind=PrincipalKind(row.actor_kind) if row.actor_kind else None,
        actor_id=row.actor_id,
        token_id=row.token_id,
        operator=row.operator,
        action=AuditAction(row.action),
        resource_kind=ResourceKind(row.resource_kind),
        resource_id=row.resource_id,
        outcome=AuditOutcome(row.outcome),
        code=ErrorCode(row.code) if row.code else None,
        ip_hash=row.ip_hash,
        user_agent_hash=row.user_agent_hash,
        request_id=row.request_id,
        details=row.details or {},
    )


async def add_audit(session: AsyncSession, events: Sequence[AuditEvent]) -> None:
    """Add ``events`` to ``session``'s transaction (they commit or roll back with it)."""
    for ev in events:
        session.add(_row(ev))
    await session.flush()


async def write_audit(
    db: Database, events: Sequence[AuditEvent], *, session: AsyncSession | None = None
) -> None:
    """Persist ``events``: in ``session`` (same transaction) or in a platform transaction.

    A platform-transaction failure is logged, never raised: auditing a denial must not turn
    it into a 500.
    """
    if not events:
        return
    if session is not None:
        await add_audit(session, events)
        return
    try:
        async with db.session() as s:
            for ev in events:
                s.add(_row(ev))
    except Exception:  # pragma: no cover - database outage while auditing
        log.exception("could not write %d audit event(s)", len(events))


FAILURE_AUDITS_PER_MINUTE: Final = 5
"""Unauthenticated failures (a wrong token secret, a failed sign-in) are audited at most this
many times per counter subject (token prefix, client) and minute: anyone can cause them, so
they must not be able to flood a tenant's audit log."""


async def write_failure_audit(
    db: Database,
    events: Sequence[AuditEvent],
    subject: str,
    *,
    now: datetime,
    limit: int = FAILURE_AUDITS_PER_MINUTE,
) -> bool:
    """Count one failure under ``subject`` and audit ``events`` unless ``limit`` failures were
    already audited this minute. Returns whether they were written. Never raises."""
    from mapi_server.services.quotas import count_requests

    try:
        async with db.session() as s:
            before = await count_requests(s, subject, now=now)
            if before >= limit:
                return False
            for ev in events:
                s.add(_row(ev))
        return True
    except Exception:  # pragma: no cover - database outage while auditing
        log.exception("could not write %d failure audit event(s)", len(events))
        return False


def audit_query(q: AuditQuery, *, after: tuple[datetime, str] | None = None) -> Select[Any]:
    """Newest first; ``after`` is the ``(ts, id)`` cursor of the previous page."""
    stmt = select(m.AuditEvent)
    if q.tenant_id is not None:
        stmt = stmt.where(m.AuditEvent.tenant_id == q.tenant_id)
    if q.project_id is not None:
        stmt = stmt.where(m.AuditEvent.project_id == q.project_id)
    if q.actor_id is not None:
        stmt = stmt.where(m.AuditEvent.actor_id == q.actor_id)
    if q.action is not None:
        stmt = stmt.where(m.AuditEvent.action == q.action.value)
    if q.outcome is not None:
        stmt = stmt.where(m.AuditEvent.outcome == q.outcome.value)
    if q.since is not None:
        stmt = stmt.where(m.AuditEvent.ts >= q.since)
    if q.until is not None:
        stmt = stmt.where(m.AuditEvent.ts < q.until)
    if after is not None:
        ts, ident = after
        stmt = stmt.where(
            (m.AuditEvent.ts < ts) | ((m.AuditEvent.ts == ts) & (m.AuditEvent.id < ident))
        )
    return stmt.order_by(m.AuditEvent.ts.desc(), m.AuditEvent.id.desc())
