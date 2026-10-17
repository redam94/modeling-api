"""The webhook outbox: events written in the transaction that causes them.

:func:`emit_event` is called with the session of the change itself - the run finalizer's
terminal transition, a promotion, a tenant deletion - so an event row (and one pending
delivery per subscribed webhook) exists exactly when its cause committed. The delivery
worker (:mod:`mapi_server.services.webhooks.delivery`) sends them later, from the
orchestrator; nothing here does network I/O.

Envelopes never carry data values, signed URLs or secrets (:class:`WebhookEnvelope`): only
ids, states and names, so receivers fetch results through the API with their own token.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.executor import RunKind
from mapi_spec.run import RunState
from mapi_spec.webhooks import DeliveryStatus, WebhookEnvelope, WebhookEventType
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.observability import metrics as obs_metrics

__all__ = [
    "MAX_ENVELOPE_BYTES",
    "emit_event",
    "emit_tenant_event",
    "envelope_body",
    "run_finished",
]

log = logging.getLogger(__name__)


def now() -> datetime:
    # not services.runs.now: runs imports this module (terminal transitions emit events)
    return datetime.now(UTC)


MAX_ENVELOPE_BYTES: Final = 64 * 1024
"""Envelopes larger than this are sent with ``data`` reduced to its id fields."""
_ID_KEYS: Final = frozenset(
    {"run_id", "kind", "state", "model_id", "model_version_id", "alias", "deletion_id", "ref"}
)


def envelope_body(envelope: Mapping[str, Any]) -> bytes:
    """The exact bytes a delivery sends (and signs) for a stored envelope."""
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )


async def _subscribers(
    session: AsyncSession,
    tenant_id: str,
    project_id: str | None,
    event_type: WebhookEventType,
) -> list[m.Webhook]:
    stmt = select(m.Webhook).where(
        m.Webhook.tenant_id == tenant_id,
        m.Webhook.active.is_(True),
        m.Webhook.events.contains([event_type.value]),
    )
    if project_id is not None:
        stmt = stmt.where(m.Webhook.project_id == project_id)
    return list((await session.execute(stmt.order_by(m.Webhook.id))).scalars())


def _envelope(
    event_type: WebhookEventType,
    tenant_id: str,
    project_id: str | None,
    data: Mapping[str, JsonValue],
) -> dict[str, Any]:
    env = WebhookEnvelope(
        id=new_id("evt"),
        type=event_type,
        created=now(),
        tenant=tenant_id,
        project=project_id,
        data=dict(data),
    )
    doc = env.model_dump(mode="json")
    if len(envelope_body(doc)) > MAX_ENVELOPE_BYTES:
        doc["data"] = {k: v for k, v in data.items() if k in _ID_KEYS} | {"truncated": True}
    return doc


async def _store(
    session: AsyncSession,
    hooks: Sequence[m.Webhook],
    event_type: WebhookEventType,
    tenant_id: str,
    project_id: str | None,
    data: Mapping[str, JsonValue],
) -> str | None:
    if not hooks:
        return None
    doc = _envelope(event_type, tenant_id, project_id, data)
    ts = now()
    session.add(
        m.WebhookEvent(
            id=doc["id"],
            tenant_id=tenant_id,
            project_id=project_id,
            type=event_type.value,
            envelope=doc,
            created_at=ts,
        )
    )
    await session.flush()
    for hook in hooks:
        session.add(
            m.WebhookDelivery(
                id=new_id("whd"),
                tenant_id=hook.tenant_id,
                project_id=hook.project_id,
                webhook_id=hook.id,
                event_id=doc["id"],
                event_type=event_type.value,
                status=DeliveryStatus.PENDING.value,
                attempt=1,
                next_attempt_at=ts,
                created_at=ts,
                updated_at=ts,
            )
        )
    await session.flush()
    obs_metrics.record_webhook_event(event_type.value, len(hooks))
    return str(doc["id"])


async def emit_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str,
    event_type: WebhookEventType,
    data: Mapping[str, JsonValue],
) -> str | None:
    """Queue ``event_type`` for the project's active webhooks subscribed to it, in
    ``session``'s transaction. Returns the event id, or ``None`` when nobody subscribed
    (then nothing is stored)."""
    hooks = await _subscribers(session, tenant_id, project_id, event_type)
    return await _store(session, hooks, event_type, tenant_id, project_id, data)


async def emit_tenant_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    event_type: WebhookEventType,
    data: Mapping[str, JsonValue],
) -> str | None:
    """A tenant-wide event (``tenant.deletion.completed``) for every project's subscribed
    webhooks; the envelope's ``project`` is ``null``.

    The trust-lifecycle purge calls this before it deletes the tenant; deliveries are sent
    by the worker afterwards only while their rows still exist, so the purge must keep the
    tenant's ``webhooks``/``webhook_events``/``webhook_deliveries`` rows until they are done
    (or accept that the event is best effort).
    """
    hooks = await _subscribers(session, tenant_id, None, event_type)
    return await _store(session, hooks, event_type, tenant_id, None, data)


async def _endpoint_of(session: AsyncSession, run_id: str) -> m.EndpointRun | None:
    return await session.get(m.EndpointRun, run_id)


async def run_finished(session: AsyncSession, run: m.Run, state: RunState) -> None:
    """The events of a run that just became terminal (called by
    :func:`mapi_server.services.runs.apply_transition` in the same transaction).

    * ``run.succeeded`` / ``run.failed`` for every user run (not platform ``describe`` runs);
    * ``model.version.ready`` when a fit registered a ready model version;
    * ``score.completed`` when a score run succeeded (with its endpoint, if it came through
      one);
    * ``optimize.completed`` when an optimize run succeeded (with the optimization's URL).
    """
    if run.kind == RunKind.DESCRIBE.value or state not in (RunState.SUCCEEDED, RunState.FAILED):
        return
    tenant, project = run.tenant_id, run.project_id
    hooks = list(
        (
            await session.execute(
                select(m.Webhook).where(
                    m.Webhook.tenant_id == tenant,
                    m.Webhook.project_id == project,
                    m.Webhook.active.is_(True),
                )
            )
        ).scalars()
    )
    if not hooks:
        return

    def subscribed(t: WebhookEventType) -> list[m.Webhook]:
        return [h for h in hooks if t.value in (h.events or [])]

    mv_id = run.output_model_version_id or run.model_version_id
    if state is RunState.SUCCEEDED:
        data: dict[str, JsonValue] = {"run_id": run.id, "kind": run.kind, "state": state.value}
        if mv_id:
            data["model_version_id"] = mv_id
        await _store(
            session,
            subscribed(WebhookEventType.RUN_SUCCEEDED),
            WebhookEventType.RUN_SUCCEEDED,
            tenant,
            project,
            data,
        )
    else:
        problem = run.problem or {}
        data = {
            "run_id": run.id,
            "kind": run.kind,
            "state": state.value,
            "failure_class": run.failure_class,
            "code": problem.get("code") if isinstance(problem, dict) else None,
        }
        if mv_id:
            data["model_version_id"] = mv_id
        await _store(
            session,
            subscribed(WebhookEventType.RUN_FAILED),
            WebhookEventType.RUN_FAILED,
            tenant,
            project,
            data,
        )
        return
    if run.kind == RunKind.FIT.value and run.output_model_version_id:
        ready = subscribed(WebhookEventType.MODEL_VERSION_READY)
        mv = await session.get(m.ModelVersion, run.output_model_version_id) if ready else None
        if mv is not None and mv.status == "ready":
            model = await session.get(m.Model, mv.model_id)
            ref = f"model://{mv.project_id}/{model.name}@v{mv.version}" if model else None
            scorecard = mv.scorecard or {}
            await _store(
                session,
                ready,
                WebhookEventType.MODEL_VERSION_READY,
                tenant,
                project,
                {
                    "model_id": mv.model_id,
                    "model_version_id": mv.id,
                    "ref": ref,
                    "scorecard_overall": scorecard.get("overall"),
                },
            )
    if run.kind == RunKind.OPTIMIZE.value:
        planned = subscribed(WebhookEventType.OPTIMIZE_COMPLETED)
        if planned:
            await _store(
                session,
                planned,
                WebhookEventType.OPTIMIZE_COMPLETED,
                tenant,
                project,
                {
                    "run_id": run.id,
                    "model_version_id": run.model_version_id,
                    "optimization": f"/v1/optimizations/{run.id}",
                },
            )
        return
    if run.kind == RunKind.SCORE.value:
        done = subscribed(WebhookEventType.SCORE_COMPLETED)
        if not done:
            return
        att = await session.get(m.RunAttempt, (run.id, run.attempt))
        via = await _endpoint_of(session, run.id)
        outputs: list[JsonValue] = [str(k) for k in sorted((att.outputs if att else {}) or {})]
        payload: dict[str, JsonValue] = {
            "run_id": run.id,
            "model_version_id": run.model_version_id,
            "outputs": outputs,
        }
        if via is not None:
            payload["endpoint"] = {
                "alias": via.alias,
                "ref": f"endpoint://{project}/{via.alias}",
                "revision": via.revision,
            }
        await _store(session, done, WebhookEventType.SCORE_COMPLETED, tenant, project, payload)
