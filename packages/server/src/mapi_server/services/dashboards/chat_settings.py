"""A dashboard's chat settings (v0.20, plan D4 and I-6).

Chat is off on every dashboard until an editor turns it on. The settings live in
``dashboard_chat_settings``, not in the dashboard spec, so a change never makes a revision,
never changes a ``spec_hash`` and takes effect on the next chat step. No row means the
defaults (:func:`get_settings`); the first update writes the row.

* :func:`get_settings` reads the settings (or the defaults) in the caller's session; the chat
  loop reads them again before every model call.
* :func:`read_settings` and :func:`put_settings` serve ``GET``/``PUT …/chat/settings``
  (editors). An update needs ``If-Match`` with the current ``etag`` (``"c<version>"``):
  ``412 PRECONDITION_FAILED`` without it or when the settings changed since. Each update is
  audited (``dashboard.chat_settings``) with the new values only; no chat text is ever part
  of the settings or the audit row.
* ``used_today_tokens`` and ``est_micros_today`` add up the dashboard's usage rows of the
  current UTC day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from mapi_spec.audit import AuditAction
from mapi_spec.chat import (
    CHAT_DEFAULT_SHARE_TOKENS_PER_DAY,
    CHAT_DEFAULT_TOKENS_PER_DAY,
    ChatAudiences,
    ChatSettingsUpdate,
    ChatSettingsView,
    chat_settings_etag,
)
from mapi_spec.dashboard_tools import PrincipalKind
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import Action, ResourceKind
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.access import load_dashboard
from mapi_server.services.runs import now

__all__ = [
    "ChatSettingsRow",
    "DashboardIds",
    "DashboardKey",
    "get_settings",
    "put_settings",
    "read_settings",
    "settings_view",
    "usage_today",
]


class DashboardKey(Protocol):
    """Anything that names one dashboard: a
    :class:`~mapi_server.services.dashboards.access.DashboardContext` or :class:`DashboardIds`."""

    @property
    def tenant_id(self) -> str: ...

    @property
    def project_id(self) -> str: ...

    @property
    def dashboard_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class DashboardIds:
    """The ids of one dashboard (when no :class:`DashboardContext` is at hand)."""

    tenant_id: str
    project_id: str
    dashboard_id: str


@dataclass(frozen=True, slots=True)
class ChatSettingsRow:
    """A dashboard's chat settings as stored (or the defaults: ``stored=False``)."""

    tenant_id: str
    project_id: str
    dashboard_id: str
    enabled: bool = False
    audiences: ChatAudiences = field(default_factory=ChatAudiences)
    keep_transcripts: bool = False
    tokens_per_day: int = CHAT_DEFAULT_TOKENS_PER_DAY
    share_tokens_per_day: int = CHAT_DEFAULT_SHARE_TOKENS_PER_DAY
    version: int = 1
    updated_by: str | None = None
    updated_at: datetime | None = None
    stored: bool = False

    @property
    def etag(self) -> str:
        return chat_settings_etag(self.version)

    def audience_allows(self, kind: PrincipalKind) -> bool:
        """Whether this kind of caller is in the chat's audience (chat on or not)."""
        if kind == "member":
            return self.audiences.members
        if kind == "client":
            return self.audiences.clients
        return self.audiences.share_links


def _from_row(row: m.DashboardChatSettings) -> ChatSettingsRow:
    return ChatSettingsRow(
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        dashboard_id=row.dashboard_id,
        enabled=bool(row.enabled),
        audiences=ChatAudiences(
            members=bool(row.aud_members),
            clients=bool(row.aud_clients),
            share_links=bool(row.aud_share),
        ),
        keep_transcripts=bool(row.keep_transcripts),
        tokens_per_day=int(row.tokens_per_day),
        share_tokens_per_day=int(row.share_tokens_per_day),
        version=int(row.version),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        stored=True,
    )


async def _row(
    s: AsyncSession, key: DashboardKey, *, for_update: bool = False
) -> m.DashboardChatSettings | None:
    stmt = select(m.DashboardChatSettings).where(
        m.DashboardChatSettings.tenant_id == key.tenant_id,
        m.DashboardChatSettings.dashboard_id == key.dashboard_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await s.execute(stmt)).scalar_one_or_none()


async def get_settings(s: AsyncSession, key: DashboardKey) -> ChatSettingsRow:
    """The dashboard's chat settings, or the defaults (chat off) when none were saved."""
    row = await _row(s, key)
    if row is None:
        return ChatSettingsRow(
            tenant_id=key.tenant_id, project_id=key.project_id, dashboard_id=key.dashboard_id
        )
    return _from_row(row)


def _day_start(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def usage_today(
    s: AsyncSession, key: DashboardKey, at: datetime | None = None
) -> tuple[int, int]:
    """``(tokens, est_micros)`` of the dashboard's chat turns since the start of the current
    UTC day (thought tokens included, as the daily budgets count them)."""
    since = _day_start(at or now())
    u = m.DashboardChatUsage
    tokens, micros = (
        await s.execute(
            select(
                func.coalesce(func.sum(u.input_tokens + u.output_tokens + u.thought_tokens), 0),
                func.coalesce(func.sum(u.est_micros), 0),
            ).where(
                u.tenant_id == key.tenant_id,
                u.dashboard_id == key.dashboard_id,
                u.at >= since,
            )
        )
    ).one()
    return int(tokens), int(micros)


async def settings_view(
    s: AsyncSession, settings: ChatSettingsRow, at: datetime | None = None
) -> ChatSettingsView:
    """The editors' view of the settings, with today's usage."""
    tokens, micros = await usage_today(s, settings, at)
    return ChatSettingsView(
        dashboard_id=settings.dashboard_id,
        enabled=settings.enabled,
        audiences=settings.audiences,
        keep_transcripts=settings.keep_transcripts,
        tokens_per_day=settings.tokens_per_day,
        share_tokens_per_day=settings.share_tokens_per_day,
        used_today_tokens=tokens,
        est_micros_today=micros,
        version=settings.version,
        etag=settings.etag,
        updated_by=settings.updated_by,
        updated_at=settings.updated_at,
    )


def _require_editor(auth: RequestAuth) -> None:
    """Chat settings are for editors (``(dashboard, update)``); the route checks it first."""
    if auth.context is None or auth.tenant_id is None:
        raise unauthenticated()
    if not auth.allows(Action.UPDATE, ResourceKind.DASHBOARD):
        raise MapiError(
            "only editors can see and change the chat settings of a dashboard",
            code=ErrorCode.FORBIDDEN,
        )


def _bound(ctx: ServerContext, auth: RequestAuth) -> ServerContext:
    return ctx if ctx.request is auth else ctx.for_request(auth)


async def read_settings(
    ctx: ServerContext, auth: RequestAuth, dashboard_id: str
) -> ChatSettingsView:
    """``GET …/chat/settings`` (editors): the settings, their ``etag`` and today's usage."""
    _require_editor(auth)
    rctx = _bound(ctx, auth)
    assert auth.tenant_id is not None
    async with ctx.db.tenant_session(auth.tenant_id) as session:
        row = await load_dashboard(session, rctx, dashboard_id)
        key = DashboardIds(row.tenant_id, row.project_id, row.id)
        return await settings_view(session, await get_settings(session, key))


def _matches(if_match: str, etag: str) -> bool:
    return if_match.strip() in (etag, "W/" + etag)


async def put_settings(
    ctx: ServerContext,
    auth: RequestAuth,
    dashboard_id: str,
    body: ChatSettingsUpdate,
    if_match: str | None,
) -> ChatSettingsView:
    """``PUT …/chat/settings`` (editors, ``If-Match``): replace the settings and audit it.

    ``412 PRECONDITION_FAILED`` without ``If-Match`` or when it is not the current ``etag``;
    ``404`` when the caller cannot see the dashboard."""
    _require_editor(auth)
    if not if_match:
        raise MapiError(
            "send If-Match with the etag of the chat settings you read",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    rctx = _bound(ctx, auth)
    assert auth.tenant_id is not None
    at = now()
    async with ctx.db.tenant_session(auth.tenant_id) as session:
        # the dashboard row lock orders concurrent updates (also the first insert)
        dash = await load_dashboard(session, rctx, dashboard_id, for_update=True)
        key = DashboardIds(dash.tenant_id, dash.project_id, dash.id)
        row = await _row(session, key, for_update=True)
        current = 1 if row is None else int(row.version)
        if not _matches(if_match, chat_settings_etag(current)):
            raise MapiError(
                "the chat settings changed since you read them: read them again and repeat "
                "the change",
                code=ErrorCode.PRECONDITION_FAILED,
            )
        if row is None:
            row = m.DashboardChatSettings(
                tenant_id=dash.tenant_id, project_id=dash.project_id, dashboard_id=dash.id
            )
            session.add(row)
        row.enabled = body.enabled
        row.aud_members = body.audiences.members
        row.aud_clients = body.audiences.clients
        row.aud_share = body.audiences.share_links
        row.keep_transcripts = body.keep_transcripts
        row.tokens_per_day = body.tokens_per_day
        row.share_tokens_per_day = body.share_tokens_per_day
        row.version = current + 1
        row.updated_by = auth.user_id or auth.principal_id
        row.updated_at = at
        await session.flush()
        details: dict[str, JsonValue] = {
            "enabled": body.enabled,
            "members": body.audiences.members,
            "clients": body.audiences.clients,
            "share_links": body.audiences.share_links,
            "keep_transcripts": body.keep_transcripts,
            "tokens_per_day": body.tokens_per_day,
            "share_tokens_per_day": body.share_tokens_per_day,
            "version": current + 1,
        }
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    rctx,
                    AuditAction.DASHBOARD_CHAT_SETTINGS,
                    ResourceKind.DASHBOARD,
                    resource_id=dash.id,
                    details=details,
                )
            ],
            session=session,
        )
        return await settings_view(session, _from_row(row), at)
