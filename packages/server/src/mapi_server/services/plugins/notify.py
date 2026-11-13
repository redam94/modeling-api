"""Mail and webhooks for uploaded-code events (v0.19 I-4, D12-D15).

Every plugin event goes out two ways: a tenant-scoped webhook
(:func:`~mapi_server.services.webhooks.outbox.emit_tenant_event`, in the caller's
transaction) and a plain-text mail to the uploader, the tenant admins or both. Neither
carries uploaded text: they name the ids, the namespace, the version, the state and the
digests only, never the uploaded title, About text or kit messages (T19.19
``mail_html_title``). Mail is best effort: a mail system that refuses a message is logged and
the action still succeeds.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Final, Literal

from mapi_spec.webhooks import WebhookEventType
from pydantic import JsonValue
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.mail import MailerError, MailMessage
from mapi_server.services.trust import trust_services
from mapi_server.services.webhooks.outbox import emit_tenant_event

__all__ = ["PLUGIN_EVENTS", "PluginEvent", "Recipients", "notify_plugin_event"]

log = logging.getLogger(__name__)

PluginEvent = Literal[
    "plugin.version.kit_passed",
    "plugin.version.kit_failed",
    "plugin.version.awaiting_approval",
    "plugin.version.approved",
    "plugin.version.rejected",
    "plugin.version.yanked",
]
PLUGIN_EVENTS: Final[tuple[PluginEvent, ...]] = (
    "plugin.version.kit_passed",
    "plugin.version.kit_failed",
    "plugin.version.awaiting_approval",
    "plugin.version.approved",
    "plugin.version.rejected",
    "plugin.version.yanked",
)
Recipients = Literal["uploader", "admins", "admins_and_uploader"]

_WHAT: Final[dict[PluginEvent, str]] = {
    "plugin.version.kit_passed": "passed its checks and tests. Its uploader can use it now",
    "plugin.version.kit_failed": "did not pass its checks or tests",
    "plugin.version.awaiting_approval": "is waiting for a tenant admin to review it",
    "plugin.version.approved": "was approved for the whole workspace",
    "plugin.version.rejected": "was not approved",
    "plugin.version.yanked": "was withdrawn. Runs that need it cannot start",
}


async def _namespace(session: AsyncSession, version: m.PluginVersion) -> str:
    ns = (
        await session.execute(
            select(m.Plugin.namespace).where(
                m.Plugin.id == version.plugin_id, m.Plugin.tenant_id == version.tenant_id
            )
        )
    ).scalar_one_or_none()
    return ns or "unknown"


async def _addresses(
    session: AsyncSession,
    tenant_id: str,
    *,
    uploader: str | None,
    admins: bool,
    exclude: Collection[str],
) -> list[str]:
    """E-mail addresses from the tenant's memberships (copied from the users, so the tenant
    session never reads the global users table), each once, in a stable order."""
    cond: ColumnElement[bool] | None = None
    if admins:
        cond = (m.Membership.project_id.is_(None)) & (m.Membership.role == "admin")
    if uploader is not None:
        mine = m.Membership.principal_id == uploader
        cond = mine if cond is None else (cond | mine)
    if cond is None:
        return []
    rows = (
        await session.execute(
            select(m.Membership.principal_id, m.Membership.email)
            .where(
                m.Membership.tenant_id == tenant_id,
                m.Membership.principal_kind == "user",
                m.Membership.email.is_not(None),
                cond,
            )
            .order_by(m.Membership.principal_id, m.Membership.id)
        )
    ).all()
    seen: set[str] = set()
    out: list[str] = []
    for principal, email in rows:
        if principal in exclude or not email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def _mail(
    to: str, event: PluginEvent, version: m.PluginVersion, namespace: str, web_origin: str
) -> MailMessage:
    label = f"{namespace} {version.version}"
    body = "\n".join(
        [
            f"The uploaded plugin {label} {_WHAT[event]}.",
            "",
            f"Plugin:   {version.plugin_id}",
            f"Version:  {version.id} ({version.version})",
            f"State:    {version.state}",
            f"SHA-256:  {version.upload_sha256}",
            "",
            "Open it in the web app:",
            f"{web_origin.rstrip('/')}/plugins/{version.plugin_id}/versions/{version.id}",
            "",
            "You get this message because you uploaded this version or you are a tenant admin.",
        ]
    )
    return MailMessage(
        to=to,
        subject=f"Uploaded code {label}: {version.state.replace('_', ' ')}",
        body=body,
        tags={"event": event, "plugin_version_id": version.id},
    )


async def notify_plugin_event(
    session: AsyncSession,
    ctx: ServerContext,
    version: m.PluginVersion,
    event: PluginEvent,
    *,
    to: Recipients,
    exclude: Collection[str] = (),
) -> None:
    """Queue the ``event`` webhook in ``session``'s transaction and mail ``to`` (minus the
    user ids in ``exclude``). The payload and the mail name ids, namespace, version, state
    and digests only."""
    namespace = await _namespace(session, version)
    data: dict[str, JsonValue] = {
        "plugin_id": version.plugin_id,
        "plugin_version_id": version.id,
        "namespace": namespace,
        "version": version.version,
        "state": version.state,
        "overlay_sha256": version.upload_sha256,
    }
    await emit_tenant_event(
        session,
        tenant_id=version.tenant_id,
        event_type=WebhookEventType(event),
        data=data,
    )
    addresses = await _addresses(
        session,
        version.tenant_id,
        uploader=version.uploaded_by if to in ("uploader", "admins_and_uploader") else None,
        admins=to in ("admins", "admins_and_uploader"),
        exclude=exclude,
    )
    if not addresses:
        return
    mailer = trust_services(ctx).mailer
    for address in addresses:
        try:
            await mailer.send(_mail(address, event, version, namespace, ctx.settings.web_origin))
        except (MailerError, ValueError) as exc:
            log.warning(
                "could not mail %s about plugin version %s: %s",
                event,
                version.id,
                type(exc).__name__,
            )
