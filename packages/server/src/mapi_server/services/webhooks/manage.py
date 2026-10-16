"""Webhook rows: DTOs, creation, secret rotation and replays (used by the API).

The signing secret is generated here, returned once (:class:`WebhookCreated`) and stored
only sealed. A rotation keeps the previous secret signing for
:data:`PREVIOUS_SECRET_GRACE` so receivers can switch without dropping deliveries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.webhooks import (
    DeliveryStatus,
    Webhook,
    WebhookCreated,
    WebhookDelivery,
    WebhookEventType,
    generate_webhook_secret,
)

from mapi_server.db import models as m
from mapi_server.services.webhooks.crypto import SecretBox, secret_hint

__all__ = [
    "PREVIOUS_SECRET_GRACE",
    "delivery_dto",
    "new_webhook",
    "replay_delivery",
    "rotate_secret",
    "webhook_dto",
]

PREVIOUS_SECRET_GRACE: Final = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(UTC)


def webhook_dto(row: m.Webhook) -> Webhook:
    return Webhook(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        url=row.url,
        events=[WebhookEventType(e) for e in row.events],
        description=row.description,
        active=row.active,
        secret_hint=row.secret_hint,
        failure_count=row.failure_count,
        disabled_reason=row.disabled_reason,
        last_delivery_at=row.last_delivery_at,
        last_success_at=row.last_success_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def delivery_dto(row: m.WebhookDelivery) -> WebhookDelivery:
    return WebhookDelivery(
        id=row.id,
        webhook_id=row.webhook_id,
        event_id=row.event_id,
        event_type=WebhookEventType(row.event_type),
        status=DeliveryStatus(row.status),
        attempt=row.attempt,
        response_code=row.response_code,
        duration_ms=row.duration_ms,
        error=row.error,
        response_excerpt=row.response_excerpt,
        next_retry_at=row.next_attempt_at
        if row.status in (DeliveryStatus.PENDING, DeliveryStatus.RETRYING)
        else None,
        replay_of=row.replay_of,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def new_webhook(
    box: SecretBox,
    *,
    tenant_id: str,
    project_id: str,
    url: str,
    events: list[WebhookEventType],
    description: str | None,
    active: bool,
    created_by: str | None,
) -> tuple[m.Webhook, str]:
    """A new row and its clear-text secret (to return once)."""
    ident = new_id("whk")
    secret = generate_webhook_secret()
    ts = _now()
    row = m.Webhook(
        id=ident,
        tenant_id=tenant_id,
        project_id=project_id,
        url=url,
        events=[e.value for e in events],
        description=description,
        active=active,
        secret_sealed=box.seal(secret, owner=ident),
        secret_hint=secret_hint(secret),
        failure_count=0,
        created_by=created_by,
        created_at=ts,
        updated_at=ts,
    )
    return row, secret


def rotate_secret(box: SecretBox, row: m.Webhook) -> WebhookCreated:
    """A new secret; the current one keeps signing for :data:`PREVIOUS_SECRET_GRACE`."""
    secret = generate_webhook_secret()
    ts = _now()
    row.previous_secret_sealed = row.secret_sealed
    row.previous_secret_expires_at = ts + PREVIOUS_SECRET_GRACE
    row.secret_sealed = box.seal(secret, owner=row.id)
    row.secret_hint = secret_hint(secret)
    row.updated_at = ts
    return WebhookCreated(webhook=webhook_dto(row), secret=secret)


def replay_delivery(original: m.WebhookDelivery) -> m.WebhookDelivery:
    """A new pending delivery of the same event to the same webhook (``replay_of`` set)."""
    ts = _now()
    return m.WebhookDelivery(
        id=new_id("whd"),
        tenant_id=original.tenant_id,
        project_id=original.project_id,
        webhook_id=original.webhook_id,
        event_id=original.event_id,
        event_type=original.event_type,
        status=DeliveryStatus.PENDING.value,
        attempt=1,
        next_attempt_at=ts,
        replay_of=original.id,
        created_at=ts,
        updated_at=ts,
    )
