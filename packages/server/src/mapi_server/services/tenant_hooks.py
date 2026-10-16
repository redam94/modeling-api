"""What the M3 integrations do when a tenant deletion starts and ends (plan §9 "Data
lifecycle and privacy").

The trust lifecycle (:mod:`mapi_server.services.deletion`) owns the deletion; it calls hooks
registered on :class:`~mapi_server.services.trust.TrustServices`:

* :class:`EmbedRevoker` revokes every embed link of the tenant when the deletion starts, so
  no public view resolves while the purge runs (the public route also refuses tenants that
  are not active; this makes the links dead for good, even if a deletion is retried).
* :class:`WebhookRevoker` disables the tenant's webhooks when the deletion starts: the relay
  sends nothing more for a tenant that is going away. Pending deliveries end ``failed``
  when the relay next claims them.
* :class:`WebhookDeletionNotifier` delivers ``tenant.deletion.completed``. ``capture`` runs at
  the start, before the purge deletes the webhooks, and keeps for each subscribed webhook its
  id, URL and **sealed** secrets (never the secret itself); ``deliver`` runs after every
  store is purged and posts one signed envelope per webhook, through the same SSRF-guarded
  sender as every delivery, with a few quick retries. The outbox cannot carry this event: its
  rows belong to the tenant and are purged.

:func:`install_tenant_hooks` registers all three on a server context.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.webhooks import (
    DELIVERY_HEADERS,
    WebhookEnvelope,
    WebhookEventType,
    signature_header,
)
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.sharing import clear_embed_cache
from mapi_server.services.trust import trust_services
from mapi_server.services.webhooks.crypto import SealError, SecretBox
from mapi_server.services.webhooks.delivery import SendResult, WebhookSender, sender_for
from mapi_server.services.webhooks.outbox import envelope_body

__all__ = [
    "DELETION_DISABLED_REASON",
    "NOTIFY_RETRY_DELAYS_S",
    "EmbedRevoker",
    "WebhookDeletionNotifier",
    "WebhookRevoker",
    "install_tenant_hooks",
]

log = logging.getLogger(__name__)

DELETION_DISABLED_REASON: Final = (
    "The workspace is being deleted: this webhook only receives tenant.deletion.completed."
)
"""``disabled_reason`` of the webhooks :class:`WebhookRevoker` disabled (``capture`` still
notifies them, and only them among the disabled ones)."""
NOTIFY_RETRY_DELAYS_S: Final = (2.0, 10.0)
"""Pauses before the second and third try of a ``tenant.deletion.completed`` delivery."""


class EmbedRevoker:
    """Revokes the tenant's live embed links (``plot_embeds.revoked_at``)."""

    name = "embeds"

    async def revoke(self, session: AsyncSession, tenant_id: str, *, now: datetime) -> int:
        res = await session.execute(
            update(m.PlotEmbed)
            .where(m.PlotEmbed.tenant_id == tenant_id, m.PlotEmbed.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        clear_embed_cache()
        return int(getattr(res, "rowcount", 0) or 0)


class WebhookRevoker:
    """Disables the tenant's active webhooks (the relay then sends them nothing)."""

    name = "webhooks"

    async def revoke(self, session: AsyncSession, tenant_id: str, *, now: datetime) -> int:
        res = await session.execute(
            update(m.Webhook)
            .where(m.Webhook.tenant_id == tenant_id, m.Webhook.active.is_(True))
            .values(active=False, disabled_reason=DELETION_DISABLED_REASON, updated_at=now)
        )
        return int(getattr(res, "rowcount", 0) or 0)


class _Target(BaseModel):
    """One webhook to notify, as stored with the deletion (JSON; secrets stay sealed)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    webhook_id: str
    project_id: str
    url: str
    secret_sealed: str
    previous_secret_sealed: str | None = None
    requested_at: str | None = None


class WebhookDeletionNotifier:
    """Delivers ``tenant.deletion.completed`` to the webhooks subscribed to it."""

    name = "webhooks"

    def __init__(
        self,
        ctx: ServerContext,
        *,
        sender: WebhookSender | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = NOTIFY_RETRY_DELAYS_S,
    ) -> None:
        self.ctx = ctx
        self._sender = sender
        self.sleep = sleep
        self.retry_delays = tuple(retry_delays)

    @property
    def sender(self) -> WebhookSender:
        if self._sender is None:
            self._sender = sender_for(self.ctx)
        return self._sender

    async def capture(self, session: AsyncSession, tenant_id: str) -> list[JsonValue]:
        event = WebhookEventType.TENANT_DELETION_COMPLETED.value
        hooks = (
            await session.execute(
                select(m.Webhook)
                .where(
                    m.Webhook.tenant_id == tenant_id,
                    m.Webhook.events.contains([event]),
                    or_(
                        m.Webhook.active.is_(True),
                        m.Webhook.disabled_reason == DELETION_DISABLED_REASON,
                    ),
                )
                .order_by(m.Webhook.id)
            )
        ).scalars()
        deletion = (
            await session.execute(
                select(m.TenantDeletion.requested_at)
                .where(
                    m.TenantDeletion.tenant_id == tenant_id,
                    m.TenantDeletion.state.in_(("scheduled", "deleting", "failed")),
                )
                .order_by(m.TenantDeletion.requested_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        requested = deletion.isoformat() if deletion is not None else None
        at = datetime.now(UTC)
        out: list[JsonValue] = []
        for hook in hooks:
            previous = hook.previous_secret_sealed
            expires = hook.previous_secret_expires_at
            if expires is None or expires <= at:
                previous = None
            target = _Target(
                webhook_id=hook.id,
                project_id=hook.project_id,
                url=hook.url,
                secret_sealed=hook.secret_sealed,
                previous_secret_sealed=previous,
                requested_at=requested,
            )
            out.append(target.model_dump(mode="json", exclude_none=True))
        return out

    async def deliver(
        self, tenant_id: str, deletion_id: str, captured: Sequence[JsonValue], *, now: datetime
    ) -> int:
        targets: list[_Target] = []
        for raw in captured:
            try:
                targets.append(_Target.model_validate(raw))
            except ValidationError:
                log.error("tenant %s: an unreadable webhook target was skipped", tenant_id)
        if not targets:
            return 0
        envelope = WebhookEnvelope(
            id=new_id("evt"),
            type=WebhookEventType.TENANT_DELETION_COMPLETED,
            created=now,
            tenant=tenant_id,
            project=None,
            data={
                "deletion_id": deletion_id,
                "requested_at": targets[0].requested_at,
                "completed_at": now.isoformat(),
            },
        )
        body = envelope_body(envelope.model_dump(mode="json"))
        obs_metrics.record_webhook_event(envelope.type.value, len(targets))
        box = SecretBox(self.ctx.secrets.webhook_key)
        attempted = 0
        for target in targets:
            try:
                keys = [box.open(target.secret_sealed, owner=target.webhook_id)]
                if target.previous_secret_sealed:
                    keys.append(box.open(target.previous_secret_sealed, owner=target.webhook_id))
            except SealError:
                log.error(
                    "tenant %s: webhook %s has an unusable secret; tenant.deletion.completed "
                    "was not sent",
                    tenant_id,
                    target.webhook_id,
                )
                continue
            attempted += 1
            result = await self._send(target, envelope, body, keys)
            log.info(
                "tenant %s: tenant.deletion.completed to webhook %s → %s",
                tenant_id,
                target.webhook_id,
                result.status if result.ok else (result.error or "failed"),
            )
        return attempted

    async def _send(
        self, target: _Target, envelope: WebhookEnvelope, body: bytes, keys: list[str]
    ) -> SendResult:
        delivery_id = new_id("whd")
        result = SendResult(error="not sent")
        for attempt in range(len(self.retry_delays) + 1):
            if attempt:
                await self.sleep(self.retry_delays[attempt - 1])
            headers = {
                DELIVERY_HEADERS["signature"]: signature_header(body, keys, int(time.time())),
                DELIVERY_HEADERS["event_id"]: envelope.id,
                DELIVERY_HEADERS["event_type"]: envelope.type.value,
                DELIVERY_HEADERS["webhook_id"]: target.webhook_id,
                DELIVERY_HEADERS["delivery_id"]: delivery_id,
            }
            result = await self.sender.send(target.url, body, headers)
            if result.ok:
                obs_metrics.record_webhook_delivery("succeeded", result.duration_ms / 1000.0)
                return result
            if result.blocked:
                obs_metrics.record_webhook_blocked("delivery")
                obs_metrics.record_webhook_delivery("blocked", result.duration_ms / 1000.0)
                return result
            if result.status == 410:
                break
        obs_metrics.record_webhook_delivery("failed", result.duration_ms / 1000.0)
        return result


def install_tenant_hooks(ctx: ServerContext) -> None:
    """Register the embed and webhook revokers and the deletion notifier on ``ctx`` (once)."""
    trust = trust_services(ctx)
    names = set(trust.revoker_names())
    for revoker in (EmbedRevoker(), WebhookRevoker()):
        if revoker.name not in names:
            trust.add_revoker(revoker)
    if not isinstance(trust.notifier, WebhookDeletionNotifier):
        trust.notifier = WebhookDeletionNotifier(ctx)
