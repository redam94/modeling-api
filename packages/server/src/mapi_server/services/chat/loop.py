"""One chat turn: the model, the read-only dashboard tools and the checks around them (v0.20,
plan D1-D11 and I-5).

:func:`run_chat_turn` answers one :class:`~mapi_spec.chat.ChatRequest`:

1. check the credential (:meth:`~.principal.ChatPrincipal.recheck`; the dashboard context
   comes from it, never from the model), chat availability and the per-minute turn limits
   (:func:`.budget.precheck`), and the signed history (:func:`.history.verify_history`);
2. send the fixed system instruction (:func:`.prompt.system_prompt`), the history as text
   turns and the 14 dashboard tools to the model;
3. run each function call the model asks for through
   :func:`~mapi_server.services.dashboards.tool_guard.call_tool_capped` for the caller's own
   context — at most :data:`~mapi_spec.chat.CHAT_MAX_TOOL_CALLS` per turn — and send the
   capped results back as ``functionResponse`` JSON (``{"result": …}`` or ``{"error":
   {"code", "message"}}``; never spliced into text). The credential, settings, kill switch
   and budgets are checked again before every model call, and the credential before every
   tool call;
4. clean the answer (:func:`.postfilter.clean_reply`), flag numbers that no tool result of
   the turn holds (``unverified_numbers``), sign it, keep the transcript when the dashboard
   keeps them, and write the usage row.

The turn has a deadline (``chat_turn_deadline_s``) and each model call a time limit
(``chat_request_timeout_s``). A model failure answers ``503 CHAT_UNAVAILABLE`` with fixed
text: provider text never reaches the caller, the logs or the usage row. Prompt and answer
text are never logged; the log line of a turn holds counts only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from mapi_spec.chat import (
    CHAT_MAX_TOOL_CALLS,
    CHAT_TOOL_RESULT_MAX_BYTES,
    ChatNoticeKind,
    ChatRequest,
    ChatResponse,
    ChatTurn,
    ChatUsage,
    ToolCallSummary,
    new_conversation_id,
)
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import JsonValue

from mapi_server.errors import NotFoundError
from mapi_server.services.chat import budget, transcripts
from mapi_server.services.chat.history import (
    chat_key,
    proven_conversation,
    sign_turn,
    verify_history,
)
from mapi_server.services.chat.model import (
    ChatModel,
    ChatModelError,
    FunctionCall,
    ModelReply,
    ModelTurn,
    ModelUsage,
    dashboard_function_decls,
    function_response_turn,
)
from mapi_server.services.chat.postfilter import clean_reply, ungrounded_numbers
from mapi_server.services.chat.prompt import readout_note, system_prompt
from mapi_server.services.chat.transcripts import TranscriptTurn
from mapi_server.services.dashboards.access import DashboardContext
from mapi_server.services.dashboards.chat_settings import ChatSettingsRow
from mapi_server.services.dashboards.tool_guard import call_tool_capped

if TYPE_CHECKING:
    from mapi_server.context import ServerContext
    from mapi_server.services.chat.principal import ChatPrincipal

__all__ = [
    "BLOCKED_REPLY",
    "EMPTY_REPLY",
    "TOOL_LIMIT_REPLY",
    "run_chat_turn",
]

log = logging.getLogger(__name__)

BLOCKED_REPLY: Final = "I cannot answer that question. Ask me about the numbers on this dashboard."
EMPTY_REPLY: Final = "I could not write an answer. Ask the question in a different way."
TOOL_LIMIT_REPLY: Final = (
    "I looked at the dashboard as many times as I can for one question. Ask a shorter or "
    "more specific question."
)
_TOOL_LIMIT_RESPONSE: Final[dict[str, Any]] = {
    "error": {
        "code": "TOOL_LIMIT",
        "message": "No more function calls for this question. Answer with the results that "
        "you have.",
    }
}
_TRUNCATING_FINISH: Final = frozenset({"MAX_TOKENS", "CONTINUATION"})
_ERROR_MESSAGE_CHARS: Final = 300
_UNKNOWN_TOOL: Final = "unknown_tool"
_MODEL_GRACE_S: Final = 2.0
"""Extra time the loop gives a model call beyond the limit the model was given (never past
the turn's deadline)."""


def _unavailable() -> MapiError:
    return MapiError(
        "the chat assistant did not answer; try again later", code=ErrorCode.CHAT_UNAVAILABLE
    )


@dataclass
class _Turn:
    """What one turn did so far (counts, results and notices; never logged as text)."""

    deadline: float
    request_timeout: float
    max_output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    limit_rounds: int = 0
    usage_in: int = 0
    usage_out: int = 0
    usage_thoughts: int = 0
    usage_cached: int = 0
    usage_total: int = 0
    summaries: list[ToolCallSummary] = field(default_factory=list)
    payloads: list[JsonValue] = field(default_factory=list)
    notices: list[ChatNoticeKind] = field(default_factory=list)
    outcome: budget.UsageOutcome = "ok"

    def notice(self, kind: ChatNoticeKind) -> None:
        if kind not in self.notices:
            self.notices.append(kind)

    def add(self, usage: ModelUsage) -> None:
        self.usage_in += max(0, usage.input)
        self.usage_out += max(0, usage.output)
        self.usage_thoughts += max(0, usage.thoughts)
        self.usage_cached += max(0, usage.cached)
        self.usage_total += max(0, usage.total)

    @property
    def usage(self) -> ModelUsage:
        return ModelUsage(
            input=self.usage_in,
            output=self.usage_out,
            thoughts=self.usage_thoughts,
            cached=self.usage_cached,
            total=self.usage_total,
        )

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


@dataclass(frozen=True, slots=True)
class _Anchor:
    """The context the turn started with: it never changes during the turn."""

    tenant_id: str
    dashboard_id: str
    principal_kind: str
    principal_id: str

    def same(self, dctx: DashboardContext) -> bool:
        return (
            dctx.tenant_id == self.tenant_id
            and dctx.dashboard_id == self.dashboard_id
            and dctx.principal_kind == self.principal_kind
            and dctx.principal_id == self.principal_id
        )


async def _recheck(principal: ChatPrincipal, anchor: _Anchor) -> DashboardContext:
    dctx = await principal.recheck()
    if not anchor.same(dctx):
        raise NotFoundError("dashboard", anchor.dashboard_id)
    return dctx


def _model_turns(req: ChatRequest) -> list[ModelTurn]:
    """The history as text turns; the readout note goes before the newest question."""
    turns: list[ModelTurn] = []
    last = len(req.messages) - 1
    for i, turn in enumerate(req.messages):
        if turn.role == "assistant":
            turns.append(ModelTurn(role="model", parts=({"text": turn.text},)))
        elif i == last and req.readout_seq is not None:
            parts = ({"text": readout_note(req.readout_seq)}, {"text": turn.text})
            turns.append(ModelTurn(role="user", parts=parts))
        else:
            turns.append(ModelTurn(role="user", parts=({"text": turn.text},)))
    return turns


def _estimated_usage(turns: Sequence[ModelTurn], state: _Turn) -> ModelUsage:
    """A conservative token count for a model call that timed out after it was sent: the
    provider may still bill it, and the daily budgets must stay a hard bound. Input is the
    request's characters / 4 (system, turns and tool declarations); output is the most the
    model may write."""
    chars = len(system_prompt())
    chars += sum(len(json.dumps(list(t.parts), ensure_ascii=False)) for t in turns)
    chars += sum(
        len(d.name) + len(d.description) + len(json.dumps(d.parameters_json_schema))
        for d in dashboard_function_decls()
    )
    tokens_in = chars // 4 + 1
    tokens_out = max(0, state.max_output_tokens)
    return ModelUsage(
        input=tokens_in, output=tokens_out, thoughts=0, cached=0, total=tokens_in + tokens_out
    )


async def _generate(model: ChatModel, turns: Sequence[ModelTurn], state: _Turn) -> ModelReply:
    remaining = state.remaining()
    if remaining <= 0:
        raise ChatModelError("timeout")  # nothing was sent: nothing to charge
    timeout = min(state.request_timeout, remaining)
    try:
        return await asyncio.wait_for(
            model.generate(
                system=system_prompt(),
                turns=tuple(turns),
                tools=dashboard_function_decls(),
                timeout_s=timeout,
            ),
            timeout=min(timeout + _MODEL_GRACE_S, remaining),
        )
    except TimeoutError:
        raise ChatModelError("timeout", usage=_estimated_usage(turns, state)) from None
    except ChatModelError as exc:
        # a client-side timeout (no HTTP status): the request was sent and may be billed
        if exc.kind == "timeout" and exc.status is None and exc.usage is None:
            exc.usage = _estimated_usage(turns, state)
        raise


def _tool_args(call: FunctionCall) -> dict[str, JsonValue]:
    args: Mapping[str, Any] = call.args if isinstance(call.args, Mapping) else {}
    return {str(k): v for k, v in args.items()}


async def _run_tool(
    ctx: ServerContext, dctx: DashboardContext, call: FunctionCall, state: _Turn
) -> dict[str, Any]:
    """One function call for the caller's context; its ``functionResponse`` body."""
    known = call.name in DASHBOARD_TOOL_NAMES
    args = _tool_args(call)
    remaining = state.remaining()
    if remaining <= 0:
        raise ChatModelError("timeout")
    try:
        capped, err, ms = await asyncio.wait_for(
            call_tool_capped(ctx, dctx, call.name, args, max_bytes=CHAT_TOOL_RESULT_MAX_BYTES),
            timeout=remaining,
        )
    except TimeoutError:
        raise ChatModelError("timeout") from None
    state.summaries.append(
        ToolCallSummary(
            name=call.name if known else _UNKNOWN_TOOL,
            args=args if known else {},
            ok=err is None,
            code=err.code if err is not None else None,
            ms=ms,
            truncated=bool(capped is not None and capped.truncated),
        )
    )
    if err is not None:
        return {"error": {"code": err.code.value, "message": err.detail[:_ERROR_MESSAGE_CHARS]}}
    assert capped is not None
    if capped.truncated:
        state.notice("truncated")
    state.payloads.append(capped.payload)
    return {"result": capped.payload}


async def _loop(
    ctx: ServerContext,
    principal: ChatPrincipal,
    anchor: _Anchor,
    dctx: DashboardContext,
    model: ChatModel,
    turns: list[ModelTurn],
    state: _Turn,
) -> tuple[str, DashboardContext, ChatSettingsRow | None]:
    """Model calls and tool calls until the model answers in text. Returns the raw answer,
    the last checked context and the settings read before the last model call."""
    row: ChatSettingsRow | None = None
    while True:
        if state.model_calls > 0:
            dctx = await _recheck(principal, anchor)
            row = await budget.precheck(ctx, dctx, principal)
        try:
            reply = await _generate(model, turns, state)
        except ChatModelError as exc:
            if exc.usage is not None:
                # a blocked, unusable or timed-out call is still billed: charge it so the
                # daily budgets bound what failing turns spend too
                state.model_calls += 1
                state.add(exc.usage)
                await budget.charge(ctx, dctx, principal, exc.usage)
            if exc.kind == "blocked":
                state.outcome = "refused"
                return BLOCKED_REPLY, dctx, row
            raise
        state.model_calls += 1
        state.add(reply.usage)
        await budget.charge(ctx, dctx, principal, reply.usage)
        turns.append(reply.turn)
        if reply.finish_reason in _TRUNCATING_FINISH:
            state.notice("truncated")
        if reply.finish_reason == "TOO_MANY_TOOL_CALLS":
            state.notice("tool_limit")
        if not reply.calls:
            return reply.text, dctx, row
        if state.tool_calls >= CHAT_MAX_TOOL_CALLS:
            state.limit_rounds += 1
            if state.limit_rounds > 1:  # it was told; stop instead of asking again
                state.notice("tool_limit")
                return TOOL_LIMIT_REPLY, dctx, row
        results: list[tuple[FunctionCall, dict[str, Any]]] = []
        for call in reply.calls:
            if state.tool_calls >= CHAT_MAX_TOOL_CALLS:
                state.notice("tool_limit")
                results.append((call, _TOOL_LIMIT_RESPONSE))
                continue
            dctx = await _recheck(principal, anchor)
            state.tool_calls += 1
            results.append((call, await _run_tool(ctx, dctx, call, state)))
        turns.append(function_response_turn(results))


async def _record_quietly(
    ctx: ServerContext,
    dctx: DashboardContext,
    principal: ChatPrincipal,
    state: _Turn,
    model: ChatModel,
    outcome: budget.UsageOutcome,
) -> None:
    """The usage row of a turn that failed after a model call (its tokens were spent). A
    failure to write it (the dashboard was deleted meanwhile) is logged, never raised."""
    if state.model_calls == 0:
        return
    try:
        await budget.record_usage(
            ctx, dctx, principal, state.usage, state.tool_calls, outcome, model_id=model.model_id
        )
    except Exception:  # the turn's own error is what the caller must see
        log.warning(
            "chat usage row not written: dashboard=%s outcome=%s", dctx.dashboard_id, outcome
        )


def _log_turn(dctx: DashboardContext, principal: ChatPrincipal, state: _Turn) -> None:
    log.info(
        "chat turn: dashboard=%s kind=%s model_calls=%d tool_calls=%d tokens=%d outcome=%s",
        dctx.dashboard_id,
        principal.kind,
        state.model_calls,
        state.tool_calls,
        state.usage_total,
        state.outcome,
    )


async def run_chat_turn(
    ctx: ServerContext,
    principal: ChatPrincipal,
    dashboard_id: str,
    req: ChatRequest,
    *,
    model: ChatModel,
) -> ChatResponse:
    """Answer one chat request for ``principal`` on ``dashboard_id``.

    ``dashboard_id`` must be the dashboard of the credential (``404`` otherwise); for a
    share session pass the link's dashboard or ``""`` (the session decides). Raises the
    credential's errors, ``409 CHAT_DISABLED``, ``429 CHAT_BUDGET_EXHAUSTED`` or
    ``RATE_LIMITED``, ``400 CHAT_HISTORY_INVALID`` and ``503 CHAT_UNAVAILABLE``."""
    settings = ctx.settings
    state = _Turn(
        deadline=time.monotonic() + settings.chat_turn_deadline_s,
        request_timeout=settings.chat_request_timeout_s,
        max_output_tokens=settings.chat_max_output_tokens,
    )
    dctx = await principal.recheck()
    if dashboard_id and dctx.dashboard_id != dashboard_id:
        raise NotFoundError("dashboard", dashboard_id)
    anchor = _Anchor(dctx.tenant_id, dctx.dashboard_id, dctx.principal_kind, dctx.principal_id)
    row = await budget.precheck(ctx, dctx, principal, turn=True)
    key = chat_key(ctx.secrets)
    verify_history(key, principal, dctx.dashboard_id, req)
    conversation_id = (
        req.conversation_id
        if proven_conversation(req) and req.conversation_id is not None
        else new_conversation_id()
    )
    turns = _model_turns(req)
    try:
        raw, dctx, latest = await _loop(ctx, principal, anchor, dctx, model, turns, state)
    except ChatModelError as exc:
        state.outcome = "error"
        await _record_quietly(ctx, dctx, principal, state, model, "error")
        _log_turn(dctx, principal, state)
        log.warning("chat model call failed: kind=%s status=%s", exc.kind, exc.status)
        raise _unavailable() from None
    except MapiError as exc:
        state.outcome = "budget" if exc.code is ErrorCode.CHAT_BUDGET_EXHAUSTED else "refused"
        await _record_quietly(ctx, dctx, principal, state, model, state.outcome)
        _log_turn(dctx, principal, state)
        raise
    if latest is not None:
        row = latest

    text = clean_reply(raw) or EMPTY_REPLY
    if ungrounded_numbers(text, state.payloads):
        state.notice("unverified_numbers")
    index = len(req.messages)
    sig = sign_turn(key, principal, dctx.dashboard_id, conversation_id, index, text)
    kept = False
    if row.keep_transcripts:
        await transcripts.append(
            ctx,
            dctx,
            principal,
            conversation_id,
            [
                TranscriptTurn(index=index - 1, role="user", text=req.messages[-1].text),
                TranscriptTurn(
                    index=index,
                    role="assistant",
                    text=text,
                    tool_calls=[(s.name, s.ok) for s in state.summaries],
                    ok=state.outcome == "ok",
                ),
            ],
        )
        kept = True
        state.notice("transcript_kept")
    if state.model_calls and await budget.budget_low(ctx, dctx, principal, row):
        state.notice("budget_low")
    await budget.record_usage(
        ctx, dctx, principal, state.usage, state.tool_calls, state.outcome, model_id=model.model_id
    )
    _log_turn(dctx, principal, state)
    usage = state.usage
    return ChatResponse(
        conversation_id=conversation_id,
        reply=ChatTurn(role="assistant", text=text, sig=sig),
        tool_calls=state.summaries,
        usage=ChatUsage(
            input_tokens=usage.input,
            output_tokens=usage.output,
            thought_tokens=usage.thoughts,
            total_tokens=usage.total,
            model=model.model_id[:64],
        ),
        notices=state.notices,
        transcript_kept=kept,
    )
