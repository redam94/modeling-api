"""Run-event ingestion (plan §8.7): HMAC verification, dedupe, coalesced progress.

Both transports end here:

* **local** — the executor reads the supervisor's stdout pipe and yields
  :class:`~mapi_spec.run.RunEvent` objects (:meth:`EventIngestor.pump`);
* **cloud** — Pub/Sub pushes Cloud Logging entries to ``POST /internal/events``
  (:func:`events_from_envelope` unwraps them).

For every event: the per-attempt HMAC key verifies it (a bad HMAC is dropped and counted as
a sandbox violation — it never changes the run); ``(run, attempt, seq)`` deduplicates it;
``progress`` upserts the single ``run_progress`` row; ``log`` lines go to ``run_logs``;
everything else is appended to ``run_events``. The first verified event of an attempt moves
the run to ``RUNNING`` (events never move it anywhere else).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Any

from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode
from mapi_spec.executor import RunEvent
from mapi_spec.run import (
    Actor,
    CompletePayload,
    ErrorPayload,
    InvalidEvent,
    LogPayload,
    ProgressPayload,
    RunState,
    RunTrigger,
    can_transition,
    event_from_log_payload,
    parse_payload,
    verify_event,
)
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors.cloudrun import same_execution
from mapi_server.jsonlimits import loads_limited
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.audit import add_audit
from mapi_server.services.notify import notify_run
from mapi_server.services.runs import append_event, apply_transition, now

__all__ = ["EventIngestor", "IngestResult", "events_from_envelope"]

log = logging.getLogger(__name__)

EXECUTION_LABEL = "run.googleapis.com/execution_name"


class IngestResult(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    BAD_HMAC = "bad_hmac"
    INVALID = "invalid"
    UNKNOWN_ATTEMPT = "unknown_attempt"
    STALE_ATTEMPT = "stale_attempt"
    LATE = "late"
    IDENTITY_MISMATCH = "identity_mismatch"


def events_from_envelope(body: Mapping[str, Any]) -> tuple[RunEvent, str | None]:
    """Unwrap one event from a Pub/Sub push, a Cloud Logging entry or a bare line object.

    Returns the event and the execution name label (if present) for identity cross-checks.
    Raises :class:`~mapi_spec.run.InvalidEvent` for anything malformed.
    """
    entry: Mapping[str, Any] = body
    message = body.get("message")
    if isinstance(message, Mapping):
        data = message.get("data")
        if not isinstance(data, str):
            raise InvalidEvent("Pub/Sub message has no data")
        try:
            decoded = loads_limited(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise InvalidEvent("Pub/Sub message data is not base64 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise InvalidEvent("Pub/Sub message data is not a JSON object")
        entry = decoded
    execution: str | None = None
    labels = entry.get("labels")
    if isinstance(labels, Mapping) and isinstance(labels.get(EXECUTION_LABEL), str):
        execution = labels[EXECUTION_LABEL]
    payload = entry.get("jsonPayload", entry)
    if not isinstance(payload, Mapping):
        raise InvalidEvent("log entry has no jsonPayload object")
    return event_from_log_payload(payload), execution


def _stream_label(payload: BaseModel) -> str:
    return payload.stream if isinstance(payload, LogPayload) else ""


def _violation_reason(why: str) -> str:
    """Map a violation message to a bounded metric label."""
    if "HMAC" in why:
        return "bad_hmac"
    if "execution label" in why:
        return "identity_mismatch"
    if "payload" in why:
        return "bad_payload"
    return "other"


class EventIngestor:
    """Verifies and persists run events; shared by the pipe pump and the HTTP endpoint."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    async def ingest(self, event: RunEvent, *, execution: str | None = None) -> IngestResult:
        """Verify, deduplicate and persist one event, recording the outcome to metrics."""
        result = await self._ingest(event, execution=execution)
        obs_metrics.record_event_ingested(result.value)
        if result is IngestResult.ACCEPTED:
            obs_metrics.record_ingestion_lag((now() - event.ts).total_seconds())
        return result

    async def _ingest(self, event: RunEvent, *, execution: str | None = None) -> IngestResult:
        # Only the attempt's tenant is looked up with the platform role (the gateway, in the
        # events service); everything is then read and written in a tenant session of that
        # tenant, under row-level security (plan §9).
        async with self.ctx.db.session() as platform:
            tenant_id = (
                await platform.execute(
                    select(m.RunAttempt.tenant_id).where(
                        m.RunAttempt.run_id == event.run_id,
                        m.RunAttempt.attempt == event.attempt,
                    )
                )
            ).scalar_one_or_none()
        if tenant_id is None:
            log.warning("event for unknown run/attempt %s/%s", event.run_id, event.attempt)
            return IngestResult.UNKNOWN_ATTEMPT
        async with self.ctx.db.tenant_session(tenant_id) as session:
            attempt = await session.get(m.RunAttempt, (event.run_id, event.attempt))
            if attempt is None:
                return IngestResult.UNKNOWN_ATTEMPT
            if not verify_event(event, attempt.hmac_key):
                await self._violation(session, attempt, "event HMAC verification failed")
                return IngestResult.BAD_HMAC
            if not same_execution(attempt.external_id, execution):
                await self._violation(session, attempt, "event execution label mismatch")
                return IngestResult.IDENTITY_MISMATCH
            try:
                payload = parse_payload(event)
            except ValidationError:
                await self._violation(session, attempt, "event payload does not match its type")
                return IngestResult.INVALID
            run = (
                await session.execute(
                    select(m.Run).where(m.Run.id == event.run_id).with_for_update()
                )
            ).scalar_one()
            if run.attempt != event.attempt:
                return IngestResult.STALE_ATTEMPT
            state = RunState(run.state)
            if state.terminal:
                return IngestResult.LATE
            result = await self._persist(session, run, attempt, event, payload)
            if result is IngestResult.ACCEPTED:
                run.last_event_at = now()
                attempt.last_seq = max(attempt.last_seq, event.seq)
                if state in (RunState.PROVISIONING, RunState.STALLED) and can_transition(
                    state, RunTrigger.EVENT
                ):
                    await apply_transition(session, run, RunTrigger.EVENT, Actor.EVENTS)
            return result

    async def _violation(self, session: AsyncSession, attempt: m.RunAttempt, why: str) -> None:
        log.warning(
            "sandbox violation on run %s attempt %s: %s",
            attempt.run_id,
            attempt.attempt,
            why,
            extra={"security_alert": True, "run_id": attempt.run_id},
        )
        obs_metrics.record_sandbox_violation(_violation_reason(why))
        await session.execute(
            update(m.RunAttempt)
            .where(m.RunAttempt.run_id == attempt.run_id, m.RunAttempt.attempt == attempt.attempt)
            .values(violations=m.RunAttempt.violations + 1)
        )
        # an authenticated push (or pipe line) whose event does not verify is a security
        # signal in the run's tenant: audit it there (the operator and the tenant see it)
        owner = (
            await session.execute(
                select(m.Run.tenant_id, m.Run.project_id).where(m.Run.id == attempt.run_id)
            )
        ).one_or_none()
        await add_audit(
            session,
            [
                make_audit_event(
                    action=AuditAction.EVENT_REJECTED,
                    resource_kind=ResourceKind.RUN,
                    outcome=AuditOutcome.DENIED,
                    ts=now(),
                    fingerprint_key=b"",  # no client data to fingerprint
                    tenant_id=owner.tenant_id if owner else None,
                    project_id=owner.project_id if owner else None,
                    actor_kind=PrincipalKind.SERVICE,
                    actor_id=f"svc_{Actor.EVENTS.value}",
                    resource_id=attempt.run_id,
                    code=ErrorCode.SANDBOX_VIOLATION,
                    details={"attempt": attempt.attempt, "reason": _violation_reason(why)},
                )
            ],
        )

    async def _persist(
        self,
        session: AsyncSession,
        run: m.Run,
        attempt: m.RunAttempt,
        event: RunEvent,
        payload: BaseModel,
    ) -> IngestResult:
        if isinstance(payload, ProgressPayload):
            values = {
                "run_id": run.id,
                "tenant_id": run.tenant_id,
                "project_id": run.project_id,
                "attempt": event.attempt,
                "seq": event.seq,
                "phase": payload.phase.value,
                "fraction": payload.fraction,
                "message": payload.message,
                "eta_seconds": payload.eta_seconds,
                "chains": [c.model_dump(mode="json") for c in payload.chains],
                "updated_at": event.ts,
            }
            ins = insert(m.RunProgress).values(**values)
            upsert = ins.on_conflict_do_update(
                index_elements=["run_id"],
                set_={k: ins.excluded[k] for k in values if k != "run_id"},
                where=(m.RunProgress.attempt < ins.excluded.attempt)
                | (
                    (m.RunProgress.attempt == ins.excluded.attempt)
                    & (m.RunProgress.seq < ins.excluded.seq)
                ),
            ).returning(m.RunProgress.run_id)
            changed = (await session.execute(upsert)).scalar_one_or_none()
            if changed is None:
                return IngestResult.DUPLICATE
            await notify_run(session, run.id, "progress")
            return IngestResult.ACCEPTED

        if isinstance(payload, LogPayload):
            line_bytes = len(payload.line.encode("utf-8"))
            log_stmt = (
                insert(m.RunLog)
                .values(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                    attempt=event.attempt,
                    seq=event.seq,
                    ts=event.ts,
                    stream=_stream_label(payload),
                    line=payload.line,
                    truncated=payload.truncated,
                )
                .on_conflict_do_nothing()
                .returning(m.RunLog.seq)
            )
            if (await session.execute(log_stmt)).scalar_one_or_none() is None:
                return IngestResult.DUPLICATE
            attempt.log_bytes += line_bytes
            await notify_run(session, run.id, "log")
            return IngestResult.ACCEPTED

        dup = await session.execute(
            select(m.RunEvent.id).where(
                m.RunEvent.run_id == run.id,
                m.RunEvent.attempt == event.attempt,
                m.RunEvent.seq == event.seq,
                m.RunEvent.source == "runner",
            )
        )
        if dup.scalar_one_or_none() is not None:
            return IngestResult.DUPLICATE
        if isinstance(payload, CompletePayload):
            attempt.complete_payload = payload.model_dump(mode="json")
        elif isinstance(payload, ErrorPayload):
            attempt.error_payload = payload.model_dump(mode="json")
        await append_event(
            session,
            run,
            type=event.type,
            payload=event.payload,
            attempt=event.attempt,
            seq=event.seq,
            ts=event.ts,
            source="runner",
        )
        return IngestResult.ACCEPTED

    async def pump(self, source: AsyncIterator[RunEvent]) -> None:
        """Consume an executor's event source forever (one task per worker process)."""
        async for event in source:
            try:
                result = await self.ingest(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "failed to ingest event %s/%s#%s", event.run_id, event.attempt, event.seq
                )
                continue
            if result not in (IngestResult.ACCEPTED, IngestResult.DUPLICATE):
                log.info("event %s/%s#%s: %s", event.run_id, event.attempt, event.seq, result)
