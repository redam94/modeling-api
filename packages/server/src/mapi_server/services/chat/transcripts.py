"""Kept chat conversations (v0.20, plan D8).

Transcripts are **off by default**. When a dashboard's settings keep them, each turn stores
the user's text, the assistant's (cleaned) text and, for the assistant turn, the names of the
tools it called and whether each worked — never tool arguments or results — with
``expires_at = now + chat_transcript_ttl_days`` (30 days). Nothing of it goes to logs, spans,
webhooks or the audit log (a deletion is audited with counts only).

* :func:`append` — the chat loop, after a turn (only when kept);
* :func:`list_page`, :func:`get`, :func:`delete` — editors (``(dashboard, update)``), for
  ``GET/DELETE …/chat/transcripts``;
* :func:`purge_expired` — the housekeeping loop.

A conversation belongs to the caller that started it: a turn whose index already exists is
replaced only for the same caller and dashboard (a retried question), never across callers.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from mapi_spec._dto import new_id
from mapi_spec.api import Page
from mapi_spec.audit import AuditAction
from mapi_spec.chat import (
    CHAT_MAX_REPLY_CHARS,
    CHAT_MAX_TOOL_CALLS,
    ChatRole,
    ChatTranscript,
    ChatTranscriptSummary,
    ChatTranscriptTurn,
)
from mapi_spec.dashboard_tools import PrincipalKind
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import Action, ResourceKind
from pydantic import JsonValue
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.access import DashboardContext, load_dashboard
from mapi_server.services.runs import now

if TYPE_CHECKING:
    from mapi_server.auth.request import RequestAuth
    from mapi_server.context import ServerContext
    from mapi_server.services.chat.principal import ChatPrincipal

__all__ = [
    "TRANSCRIPT_PAGE_DEFAULT",
    "TRANSCRIPT_PAGE_MAX",
    "TranscriptTurn",
    "append",
    "delete",
    "get",
    "list_page",
    "purge_expired",
]

TRANSCRIPT_PAGE_DEFAULT: Final = 50
TRANSCRIPT_PAGE_MAX: Final = 200
_SHARE_LABEL: Final = "Share link"


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """One turn to keep: its position in the conversation, role and text, and for an
    assistant turn the tools it called (``(name, ok)``) and whether the turn worked."""

    index: int
    role: ChatRole
    text: str
    tool_calls: Sequence[tuple[str, bool]] = field(default_factory=tuple)
    ok: bool = True


T = m.DashboardChatTranscript


async def append(
    ctx: ServerContext,
    dctx: DashboardContext,
    principal: ChatPrincipal,
    conversation_id: str,
    turns: Sequence[TranscriptTurn],
) -> int:
    """Keep ``turns`` of ``conversation_id`` for ``ctx.settings.chat_transcript_ttl_days``.
    Returns how many rows were written (a turn index already kept for another caller is
    left alone)."""
    if not turns:
        return 0
    at = now()
    expires = at + timedelta(days=ctx.settings.chat_transcript_ttl_days)
    written = 0
    async with ctx.db.tenant_session(dctx.tenant_id) as s:
        for turn in turns:
            calls: list[dict[str, JsonValue]] = [
                {"name": name[:64], "ok": bool(ok)}
                for name, ok in list(turn.tool_calls)[:CHAT_MAX_TOOL_CALLS]
            ]
            values: dict[str, Any] = {
                "id": _new_row_id(),
                "tenant_id": dctx.tenant_id,
                "project_id": dctx.project_id,
                "dashboard_id": dctx.dashboard_id,
                "conversation_id": conversation_id,
                "principal_kind": principal.kind,
                "principal_id": principal.id[:64],
                "turn_index": int(turn.index),
                "role": turn.role,
                "text": turn.text[:CHAT_MAX_REPLY_CHARS],
                "tool_calls": calls,
                "ok": turn.ok,
                "created_at": at,
                "expires_at": expires,
            }
            stmt = (
                insert(T)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_dashboard_chat_transcripts_turn",
                    set_={
                        "role": values["role"],
                        "text": values["text"],
                        "tool_calls": values["tool_calls"],
                        "ok": values["ok"],
                        "created_at": at,
                        "expires_at": expires,
                    },
                    where=(
                        (T.principal_kind == principal.kind)
                        & (T.principal_id == values["principal_id"])
                        & (T.dashboard_id == dctx.dashboard_id)
                    ),
                )
                .returning(T.id)
            )
            if (await s.execute(stmt)).first() is not None:
                written += 1
    return written


def _new_row_id() -> str:
    return new_id("ctt")


# ================================================================================ editors


def _require_editor(auth: RequestAuth) -> None:
    if auth.context is None or auth.tenant_id is None:
        raise unauthenticated()
    if not auth.allows(Action.UPDATE, ResourceKind.DASHBOARD):
        raise MapiError(
            "only editors can see the kept conversations of a dashboard",
            code=ErrorCode.FORBIDDEN,
        )


def _bound(ctx: ServerContext, auth: RequestAuth) -> ServerContext:
    return ctx if ctx.request is auth else ctx.for_request(auth)


def _encode_cursor(last_at: datetime, conversation_id: str) -> str:
    raw = json.dumps({"k": [last_at.isoformat(), conversation_id]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, cid = json.loads(raw)["k"]
        return datetime.fromisoformat(str(at)), str(cid)
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise MapiError(
            "the cursor is invalid", code=ErrorCode.SCHEMA_INVALID, pointer="/query/cursor"
        ) from None


async def _labels(
    ctx: ServerContext, s: AsyncSession, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Display labels: a user's name (or e-mail), or ``Share link <label>``. Never an IP."""
    out: dict[tuple[str, str], str] = {}
    users = sorted(pid for kind, pid in keys if kind != "share")
    shares = sorted(pid for kind, pid in keys if kind == "share")
    if users:
        async with ctx.db.session() as ident:
            rows = await ident.execute(
                select(m.User.id, m.User.name, m.User.email).where(m.User.id.in_(users))
            )
            found = {uid: (name or email or "") for uid, name, email in rows}
        for kind, pid in keys:
            if kind != "share":
                out[(kind, pid)] = (found.get(pid) or "Former user")[:200]
    if shares:
        rows2 = await s.execute(
            select(m.DashboardShare.id, m.DashboardShare.label, m.DashboardShare.display).where(
                m.DashboardShare.id.in_(shares)
            )
        )
        found2 = {sid: (label or display or "") for sid, label, display in rows2}
        for kind, pid in keys:
            if kind == "share":
                name = found2.get(pid, "")
                out[(kind, pid)] = (f"{_SHARE_LABEL} {name}".strip())[:200]
    return out


def _summary(row: Any, label: str) -> ChatTranscriptSummary:
    cid, kind, _pid, turns, first_at, last_at, expires_at = row
    principal_kind: PrincipalKind = kind
    return ChatTranscriptSummary(
        conversation_id=cid,
        principal_kind=principal_kind,
        principal_label=label,
        turns=int(turns),
        first_at=first_at,
        last_at=last_at,
        expires_at=expires_at,
    )


def _grouped(dashboard_id: str, at: datetime) -> Any:
    last_at = func.max(T.created_at).label("last_at")
    return (
        select(
            T.conversation_id,
            func.min(T.principal_kind),
            func.min(T.principal_id),
            func.count(),
            func.min(T.created_at),
            last_at,
            func.max(T.expires_at),
        )
        .where(T.dashboard_id == dashboard_id, T.expires_at > at)
        .group_by(T.conversation_id)
    )


async def list_page(
    ctx: ServerContext,
    auth: RequestAuth,
    dashboard_id: str,
    *,
    cursor: str | None = None,
    limit: int = TRANSCRIPT_PAGE_DEFAULT,
) -> Page[ChatTranscriptSummary]:
    """``GET …/chat/transcripts`` (editors): kept conversations, latest first."""
    _require_editor(auth)
    limit = max(1, min(int(limit), TRANSCRIPT_PAGE_MAX))
    after = _decode_cursor(cursor)
    at = now()
    assert auth.tenant_id is not None
    async with ctx.db.tenant_session(auth.tenant_id) as s:
        dash = await load_dashboard(s, _bound(ctx, auth), dashboard_id)
        stmt = _grouped(dash.id, at)
        if after is not None:
            stmt = stmt.having(
                tuple_(func.max(T.created_at), T.conversation_id) < tuple_(after[0], after[1])
            )
        stmt = stmt.order_by(func.max(T.created_at).desc(), T.conversation_id.desc()).limit(
            limit + 1
        )
        rows = list((await s.execute(stmt)).all())
        more = len(rows) > limit
        rows = rows[:limit]
        labels = await _labels(ctx, s, {(str(r[1]), str(r[2])) for r in rows})
        items = [_summary(r, labels.get((str(r[1]), str(r[2])), "")) for r in rows]
    next_cursor = _encode_cursor(rows[-1][5], str(rows[-1][0])) if more and rows else None
    return Page[ChatTranscriptSummary](items=items, next_cursor=next_cursor)


async def get(
    ctx: ServerContext, auth: RequestAuth, dashboard_id: str, conversation_id: str
) -> ChatTranscript:
    """``GET …/chat/transcripts/{conversation_id}`` (editors): the kept turns, oldest first;
    ``404`` when nothing is kept (or it expired)."""
    _require_editor(auth)
    at = now()
    assert auth.tenant_id is not None
    async with ctx.db.tenant_session(auth.tenant_id) as s:
        dash = await load_dashboard(s, _bound(ctx, auth), dashboard_id)
        head = (
            await s.execute(_grouped(dash.id, at).where(T.conversation_id == conversation_id))
        ).first()
        if head is None:
            raise NotFoundError("conversation", conversation_id)
        rows = (
            await s.execute(
                select(T)
                .where(
                    T.dashboard_id == dash.id,
                    T.conversation_id == conversation_id,
                    T.expires_at > at,
                )
                .order_by(T.turn_index)
            )
        ).scalars()
        turns = [
            ChatTranscriptTurn(
                index=row.turn_index,
                role="assistant" if row.role == "assistant" else "user",
                text=row.text,
                tool_calls=[
                    str(c.get("name", ""))[:64]
                    for c in (row.tool_calls or [])
                    if isinstance(c, dict)
                ][:CHAT_MAX_TOOL_CALLS],
                ok=bool(row.ok),
                at=row.created_at,
            )
            for row in rows
        ]
        labels = await _labels(ctx, s, {(str(head[1]), str(head[2]))})
        summary = _summary(head, labels.get((str(head[1]), str(head[2])), ""))
    return ChatTranscript(summary=summary, turns=turns)


async def delete(
    ctx: ServerContext, auth: RequestAuth, dashboard_id: str, conversation_id: str
) -> None:
    """``DELETE …/chat/transcripts/{conversation_id}`` (editors): remove every kept turn of
    the conversation (audited with the number of turns only); ``404`` when none is kept."""
    _require_editor(auth)
    assert auth.tenant_id is not None
    rctx = _bound(ctx, auth)
    async with ctx.db.tenant_session(auth.tenant_id) as s:
        dash = await load_dashboard(s, rctx, dashboard_id)
        removed = (
            await s.execute(
                sql_delete(T)
                .where(T.dashboard_id == dash.id, T.conversation_id == conversation_id)
                .returning(T.id)
            )
        ).all()
        if not removed:
            raise NotFoundError("conversation", conversation_id)
        details: dict[str, JsonValue] = {
            "conversation_id": conversation_id,
            "turns": len(removed),
        }
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    rctx,
                    AuditAction.DASHBOARD_CHAT_TRANSCRIPT_DELETE,
                    ResourceKind.DASHBOARD,
                    resource_id=dash.id,
                    details=details,
                )
            ],
            session=s,
        )


async def purge_expired(ctx: ServerContext, at: datetime | None = None) -> int:
    """Delete every expired transcript row (all tenants; the housekeeping loop's platform
    session). Returns the number of rows removed."""
    when = at or now()
    async with ctx.db.session() as s:
        removed = (await s.execute(sql_delete(T).where(T.expires_at <= when).returning(T.id))).all()
    return len(removed)
