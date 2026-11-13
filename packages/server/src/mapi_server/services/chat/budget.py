"""Whether a caller may chat, and what a turn costs (v0.20, plan D4, D7 and D20).

:func:`availability` answers ``GET …/chat`` (``ChatAvailability``) and :func:`precheck`
refuses a model call with the same reasons, checked in this order:

1. ``provider`` — no chat model is configured (``MAPI_CHAT_PROVIDER=off``);
2. ``killswitch`` — the operator turned all chat off (``KillSwitches.chat_enabled``);
3. ``region`` — the tenant's data residency is not in ``chat_allowed_residencies`` (the Gemini
   Developer API is a global endpoint);
4. ``layout`` — the published revision is not layout 2;
5. ``off`` — the dashboard's chat is off (the default);
6. ``audience`` — chat is on, but not for this kind of caller;
7. ``budget`` — the daily token budget of the dashboard (or of the share link) is spent.

Reasons 1-6 answer ``409 CHAT_DISABLED`` (:class:`ChatDisabled`, ``reason`` attached) and 7
answers ``429 CHAT_BUDGET_EXHAUSTED`` with ``Retry-After`` until the next UTC midnight.

Budgets are rate counters of the current UTC day (``chattok:<dashboard_id>`` and, for share
links, ``chattokshare:<share_id>``): :func:`charge` adds each model call's total tokens
(thoughts included) and :func:`precheck` refuses the next call once a counter reached its
limit. Per-minute turn limits (``chat:<dashboard_id>:<principal_id>``, ``chatshare:<share_id>``
and ``chatshareip:<digest>``) are counted once per turn (``precheck(..., turn=True)``).
:func:`record_usage` writes the turn's ``dashboard_chat_usage`` row (counts and cost only,
never text) with :func:`estimate_micros` from the pinned price table.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from mapi_spec.chat import (
    CHAT_MAX_TOOL_CALLS,
    CHAT_SUGGESTIONS,
    ChatAvailability,
    ChatUnavailableReason,
)
from mapi_spec.errors import ErrorCode, MapiError

from mapi_server.db import models as m
from mapi_server.errors import ProblemWithHeaders
from mapi_server.services.chat.fake import FAKE_MODEL_ID
from mapi_server.services.chat.model import ModelUsage
from mapi_server.services.dashboards.access import DashboardContext, chat_layout_ok
from mapi_server.services.dashboards.chat_settings import ChatSettingsRow, get_settings
from mapi_server.services.quotas import charge_daily, count_requests, kill_switches, peek_daily
from mapi_server.services.runs import now

if TYPE_CHECKING:
    from mapi_server.context import ServerContext
    from mapi_server.services.chat.principal import ChatPrincipal
    from mapi_server.settings import Settings

__all__ = [
    "BUDGET_LOW_FRACTION",
    "ChatDisabled",
    "Subjects",
    "UsageOutcome",
    "Verdict",
    "availability",
    "budget_low",
    "charge",
    "check",
    "estimate_micros",
    "precheck",
    "record_usage",
    "subjects",
]

UsageOutcome = Literal["ok", "budget", "error", "refused"]
BUDGET_LOW_FRACTION: Final = 0.1
"""The notice ``budget_low`` shows when less than this part of a daily budget is left."""
_SUBJECT_MAX: Final = 96

_DISABLED_TEXT: Final[dict[str, str]] = {
    "provider": "chat is not set up on this server",
    "killswitch": "chat is off on this platform for now",
    "region": "chat is not available for this workspace's data region",
    "layout": "chat needs the layout 2 dashboard: ask your analyst to upgrade the layout",
    "off": "chat is off for this dashboard",
    "audience": "chat on this dashboard is not open to you",
}


class ChatDisabled(MapiError):
    """``409 CHAT_DISABLED`` with the reason (``ChatAvailability.reason``)."""

    def __init__(self, reason: ChatUnavailableReason) -> None:
        super().__init__(_DISABLED_TEXT.get(reason, "chat is off"), code=ErrorCode.CHAT_DISABLED)
        self.reason: ChatUnavailableReason = reason


def _subject(*parts: str) -> str:
    """A rate-counter subject (at most 96 characters: long ids are hashed)."""
    subject = ":".join(parts)
    if len(subject) <= _SUBJECT_MAX:
        return subject
    digest = hashlib.sha256(subject.encode()).hexdigest()[:40]
    return f"{parts[0]}:{digest}"


@dataclass(frozen=True, slots=True)
class Subjects:
    """The rate-counter subjects of one caller on one dashboard."""

    dashboard_tokens: str
    share_tokens: str | None
    turns: str
    share_ip_turns: str | None


def subjects(dctx: DashboardContext, principal: ChatPrincipal) -> Subjects:
    if principal.kind == "share":
        share_id = principal.share_id or dctx.principal_id
        return Subjects(
            dashboard_tokens=_subject("chattok", dctx.dashboard_id),
            share_tokens=_subject("chattokshare", share_id),
            turns=_subject("chatshare", share_id),
            share_ip_turns=_subject("chatshareip", principal.ip_key or "-"),
        )
    return Subjects(
        dashboard_tokens=_subject("chattok", dctx.dashboard_id),
        share_tokens=None,
        turns=_subject("chat", dctx.dashboard_id, principal.id),
        share_ip_turns=None,
    )


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of :func:`check`: ``reason`` is ``None`` when the caller may chat."""

    reason: ChatUnavailableReason | None
    settings: ChatSettingsRow | None
    used: int = 0
    """Tokens used today against the tightest budget (``limit`` below)."""
    limit: int = 0


def _model_id(settings: Settings) -> str | None:
    if settings.chat_provider == "off":
        return None
    if settings.chat_provider == "fake":
        return FAKE_MODEL_ID
    return settings.chat_model


async def check(ctx: ServerContext, dctx: DashboardContext, principal: ChatPrincipal) -> Verdict:
    """Read everything that decides whether ``principal`` may chat on the dashboard now."""
    settings = ctx.settings
    if settings.chat_provider == "off":
        return Verdict("provider", None)
    at = now()
    async with ctx.db.session() as s:  # platform tables (gateway role inside a request)
        switches = await kill_switches(s)
        if not switches.chat_enabled:
            return Verdict("killswitch", None)
        tenant = await s.get(m.Tenant, dctx.tenant_id)
        residency = tenant.data_residency if tenant is not None else ""
        if not settings.chat_residency_allowed(residency):
            return Verdict("region", None)
        subj = subjects(dctx, principal)
        used_dashboard = await peek_daily(s, subj.dashboard_tokens, at)
        used_share = (
            await peek_daily(s, subj.share_tokens, at) if subj.share_tokens is not None else 0
        )
    async with ctx.db.tenant_session(dctx.tenant_id) as s:
        if not await chat_layout_ok(s, dctx):
            return Verdict("layout", None)
        row = await get_settings(s, dctx)
    if not row.enabled:
        return Verdict("off", row)
    if not row.audience_allows(principal.kind):
        return Verdict("audience", row)
    budgets = [(used_dashboard, row.tokens_per_day)]
    if principal.kind == "share":
        budgets.append((used_share, row.share_tokens_per_day))
    # the budget nearest to its end decides
    used, limit = min(budgets, key=lambda b: b[1] - b[0])
    if used >= limit:
        return Verdict("budget", row, used, limit)
    return Verdict(None, row, used, limit)


async def availability(
    ctx: ServerContext, dctx: DashboardContext, principal: ChatPrincipal
) -> ChatAvailability:
    """``GET …/chat`` and ``GET /v1/shared/chat``: may the caller chat, and what to show."""
    verdict = await check(ctx, dctx, principal)
    keeps = bool(
        verdict.settings and verdict.settings.enabled and verdict.settings.keep_transcripts
    )
    if verdict.reason is not None:
        return ChatAvailability(
            enabled_for_me=False,
            reason=verdict.reason,
            keeps_transcripts=keeps,
            transcript_days=ctx.settings.chat_transcript_ttl_days,
            model=None,
            suggestions=[],
        )
    return ChatAvailability(
        enabled_for_me=True,
        reason=None,
        keeps_transcripts=keeps,
        transcript_days=ctx.settings.chat_transcript_ttl_days,
        model=_model_id(ctx.settings),
        suggestions=list(CHAT_SUGGESTIONS),
    )


def _next_midnight(at: datetime) -> datetime:
    day = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=1)


def _exhausted(at: datetime) -> ProblemWithHeaders:
    wait = max(1, math.ceil((_next_midnight(at) - at.astimezone(UTC)).total_seconds()))
    return ProblemWithHeaders(
        "the chat budget of this dashboard is spent for today; try again after midnight UTC",
        code=ErrorCode.CHAT_BUDGET_EXHAUSTED,
        headers={"Retry-After": str(wait)},
    )


async def _count_turn(ctx: ServerContext, dctx: DashboardContext, principal: ChatPrincipal) -> None:
    """The per-minute turn limits (counted once per turn)."""
    settings = ctx.settings
    if not settings.rate_limits_enabled:
        return
    at = now()
    subj = subjects(dctx, principal)
    if principal.kind == "share":
        limits = [(subj.turns, settings.chat_share_turns_per_minute)]
        if subj.share_ip_turns is not None:
            limits.append((subj.share_ip_turns, settings.chat_share_ip_turns_per_minute))
    else:
        limits = [(subj.turns, settings.chat_member_turns_per_minute)]
    async with ctx.db.session() as s:
        for subject, limit in limits:
            before = await count_requests(s, subject, now=at)
            if before >= limit:
                raise ProblemWithHeaders(
                    "too many chat questions in a short time; wait a minute and ask again",
                    code=ErrorCode.RATE_LIMITED,
                    headers={"Retry-After": str(max(1, 60 - at.second))},
                )


async def precheck(
    ctx: ServerContext,
    dctx: DashboardContext,
    principal: ChatPrincipal,
    *,
    turn: bool = False,
) -> ChatSettingsRow:
    """Refuse the next model call when chat is not available to ``principal``: ``409
    CHAT_DISABLED`` (:class:`ChatDisabled`) or ``429 CHAT_BUDGET_EXHAUSTED``. With ``turn``
    (once, at the start of a turn) also count the per-minute turn limits (``429
    RATE_LIMITED``). Returns the settings read."""
    verdict = await check(ctx, dctx, principal)
    if verdict.reason == "budget":
        raise _exhausted(now())
    if verdict.reason is not None:
        raise ChatDisabled(verdict.reason)
    assert verdict.settings is not None
    if turn:
        await _count_turn(ctx, dctx, principal)
    return verdict.settings


async def charge(
    ctx: ServerContext, dctx: DashboardContext, principal: ChatPrincipal, usage: ModelUsage
) -> None:
    """Add one model call's total tokens to the daily budgets of the dashboard (and of the
    share link)."""
    if usage.total <= 0:
        return
    at = now()
    subj = subjects(dctx, principal)
    async with ctx.db.session() as s:
        await charge_daily(s, subj.dashboard_tokens, usage.total, at)
        if subj.share_tokens is not None:
            await charge_daily(s, subj.share_tokens, usage.total, at)


async def budget_low(
    ctx: ServerContext, dctx: DashboardContext, principal: ChatPrincipal, row: ChatSettingsRow
) -> bool:
    """Whether less than :data:`BUDGET_LOW_FRACTION` of a daily budget is left now."""
    at = now()
    subj = subjects(dctx, principal)
    budgets = [(subj.dashboard_tokens, row.tokens_per_day)]
    if subj.share_tokens is not None:
        budgets.append((subj.share_tokens, row.share_tokens_per_day))
    async with ctx.db.session() as s:
        for subject, limit in budgets:
            used = await peek_daily(s, subject, at)
            if limit - used < limit * BUDGET_LOW_FRACTION:
                return True
    return False


def estimate_micros(usage: ModelUsage, settings: Settings) -> int:
    """The estimated cost of ``usage`` in micro-dollars under the pinned price table
    (``chat_price_*``; rounded up). Input is everything charged that is not answer or
    thought tokens; thoughts are priced as output."""
    output = max(0, usage.output) + max(0, usage.thoughts)
    billed_in = max(max(0, usage.input), max(0, usage.total) - output)
    micros = (
        billed_in * settings.chat_price_in_micros_per_mtok
        + output * settings.chat_price_out_micros_per_mtok
    )
    return -(-micros // 1_000_000)


async def record_usage(
    ctx: ServerContext,
    dctx: DashboardContext,
    principal: ChatPrincipal,
    usage: ModelUsage,
    tool_calls: int,
    outcome: UsageOutcome,
    *,
    model_id: str | None = None,
) -> None:
    """Write the turn's usage row: tokens (thoughts apart), tool calls, estimated cost and
    outcome. Never any text."""
    settings = ctx.settings
    thoughts = max(0, usage.thoughts)
    output = max(0, usage.output)
    billed_in = max(max(0, usage.input), max(0, usage.total) - output - thoughts)
    async with ctx.db.tenant_session(dctx.tenant_id) as s:
        s.add(
            m.DashboardChatUsage(
                tenant_id=dctx.tenant_id,
                project_id=dctx.project_id,
                dashboard_id=dctx.dashboard_id,
                principal_kind=principal.kind,
                principal_id=principal.id[:64],
                share_id=principal.share_id if principal.kind == "share" else None,
                model=(model_id or _model_id(settings) or "none")[:64],
                input_tokens=min(billed_in, 2**31 - 1),
                output_tokens=min(output, 2**31 - 1),
                thought_tokens=min(thoughts, 2**31 - 1),
                tool_calls=max(0, min(tool_calls, CHAT_MAX_TOOL_CALLS)),
                est_micros=estimate_micros(usage, settings),
                price_version=settings.chat_price_version[:64],
                outcome=outcome,
                at=now(),
            )
        )
