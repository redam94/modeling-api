"""Dashboard chat and dashboard read tokens: the v0.20 contracts.

**Chat** ("Ask about your dashboard") runs in the control plane, never in the browser. A
member, a client user or a share-link session sends a :class:`ChatRequest` to the dashboard's
chat route; the server builds the dashboard context from the caller's credential, lets the
model call the read-only tools of :mod:`mapi_spec.dashboard_tools` (at most
:data:`CHAT_MAX_TOOL_CALLS` per turn), and answers with a :class:`ChatResponse`.

* History is held by the client as **text only**: user and assistant turns, never function
  calls or results (the server recomputes those every turn). Each assistant turn carries a
  server signature ``sig`` (HMAC-SHA256, base64url without padding) bound to the caller, the
  dashboard, the conversation and the turn index, so a client cannot forge or re-order the
  assistant's words.
* Chat is off by default on every dashboard. An editor turns it on and picks the audiences
  (:class:`ChatSettingsUpdate`); settings are a table, not part of the dashboard spec, so a
  change never makes a new revision.
* Transcripts are kept only when the dashboard opts in, for :data:`CHAT_TRANSCRIPT_DAYS`.

**Read tokens** are API tokens with the single scope ``dashboard:<dashboard_id>``
(:func:`mapi_spec.tokens.dashboard_scope`). A signed-in member or client mints one for a
dashboard they can read, to connect a chat tool over MCP. The server accepts them only on
the MCP endpoint.

This module is JSON-only (no numpy, pandas, pyarrow or xarray).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, dto_examples, new_id
from mapi_spec._types import StrictModel
from mapi_spec.dashboard_tools import PrincipalKind
from mapi_spec.errors import ErrorCode
from mapi_spec.tokens import TOKEN_ID_RE, TOKEN_RE, ParsedToken, token_checksum

__all__ = [
    "CHAT_DEFAULT_SHARE_TOKENS_PER_DAY",
    "CHAT_DEFAULT_TOKENS_PER_DAY",
    "CHAT_MAX_HISTORY_CHARS",
    "CHAT_MAX_REPLY_CHARS",
    "CHAT_MAX_SHARE_TOKENS_PER_DAY",
    "CHAT_MAX_TOKENS_PER_DAY",
    "CHAT_MAX_TOOL_ARGS_BYTES",
    "CHAT_MAX_TOOL_CALLS",
    "CHAT_MAX_TURNS",
    "CHAT_MAX_USER_CHARS",
    "CHAT_SIG_RE",
    "CHAT_SUGGESTIONS",
    "CHAT_TOOL_RESULT_MAX_BYTES",
    "CHAT_TRANSCRIPT_DAYS",
    "CONVERSATION_ID_RE",
    "MCP_TOOL_RESULT_MAX_BYTES",
    "READ_TOKEN_DEFAULT_DAYS",
    "READ_TOKEN_MAX_DAYS",
    "ChatAudiences",
    "ChatAvailability",
    "ChatNoticeKind",
    "ChatRequest",
    "ChatResponse",
    "ChatRole",
    "ChatSettingsUpdate",
    "ChatSettingsView",
    "ChatTranscript",
    "ChatTranscriptSummary",
    "ChatTranscriptTurn",
    "ChatTurn",
    "ChatUnavailableReason",
    "ChatUsage",
    "ConversationId",
    "ReadToken",
    "ReadTokenCreate",
    "ReadTokenCreated",
    "ToolCallSummary",
    "chat_settings_etag",
    "new_conversation_id",
]

# ================================================================================== limits

CHAT_MAX_TURNS: Final = 20
"""Turns in one request: the history plus the new user turn."""
CHAT_MAX_USER_CHARS: Final = 2000
CHAT_MAX_HISTORY_CHARS: Final = 24_000
"""All turn texts of one request together."""
CHAT_MAX_REPLY_CHARS: Final = 4000
CHAT_MAX_TOOL_CALLS: Final = 6
"""Function calls in one turn (a constant, not a setting)."""
CHAT_MAX_TOOL_ARGS_BYTES: Final = 1024
"""A :class:`ToolCallSummary` keeps the call's arguments only up to this size."""
CHAT_TOOL_RESULT_MAX_BYTES: Final = 24_576
"""Budget of one tool result sent to the model (:func:`mapi_spec.tool_results.cap_tool_result`)."""
MCP_TOOL_RESULT_MAX_BYTES: Final = 65_536
"""Budget of one tool result returned over MCP."""
CHAT_TRANSCRIPT_DAYS: Final = 30
CHAT_DEFAULT_TOKENS_PER_DAY: Final = 200_000
CHAT_MAX_TOKENS_PER_DAY: Final = 5_000_000
CHAT_DEFAULT_SHARE_TOKENS_PER_DAY: Final = 50_000
CHAT_MAX_SHARE_TOKENS_PER_DAY: Final = 1_000_000
READ_TOKEN_DEFAULT_DAYS: Final = 30
READ_TOKEN_MAX_DAYS: Final = 90

CONVERSATION_ID_RE: Final = r"^cnv_[0-9a-z]{26}$"
CHAT_SIG_RE: Final = r"^[A-Za-z0-9_-]{43}$"
"""An assistant turn signature: HMAC-SHA256, base64url without padding (43 characters)."""

ConversationId = Annotated[str, StringConstraints(pattern=CONVERSATION_ID_RE)]
ChatRole = Literal["user", "assistant"]
ChatNoticeKind = Literal[
    "unverified_numbers", "tool_limit", "truncated", "budget_low", "transcript_kept"
]
"""Notices shown under an answer: numbers not found in a tool result, the tool-call limit was
reached, the answer or a tool result was cut, the daily budget is nearly spent, the
conversation is kept."""
ChatUnavailableReason = Literal[
    "off", "audience", "layout", "region", "provider", "killswitch", "budget"
]
"""Why chat is not available (also ``details.reason`` of ``CHAT_DISABLED``)."""

CHAT_SUGGESTIONS: Final[tuple[str, ...]] = (
    "What does the latest readout show?",
    "What changed since the last readout?",
    "How accurate is the model?",
    "Explain the main numbers in plain words.",
)
"""The fixed questions the chat panel offers before the first message (never tenant text)."""

_EX_CNV = "cnv_01jaf3t1v2w3x4y5z6a7b8c9d0"
_EX_SIG = "q3Jx8m1V0bYH7kQf2nTzL5cWd9pR4sAe6uGiOjKvXyw"
_EX_DSH = "dsh_01jaf3k1m2n3p4q5r6s7t8v9w0"
_EX_USER = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
_EX_TOK = "tok_01jaf3r1s2t3v4w5x6y7z8a9b0"


def new_conversation_id() -> str:
    """A new conversation id ``cnv_<26 base32 characters>``."""
    return new_id("cnv")


def chat_settings_etag(version: int) -> str:
    """The ``ETag`` of chat settings at ``version``: ``"c<version>"`` (quotes included)."""
    return f'"c{version}"'


# ==================================================================================== turns


class ChatTurn(RequestModel):
    """One turn of a conversation: plain text.

    A ``user`` turn has no ``sig`` and at most :data:`CHAT_MAX_USER_CHARS` characters. An
    ``assistant`` turn carries the ``sig`` the server returned with it, unchanged.
    """

    role: ChatRole
    text: str = Field(..., min_length=1, max_length=CHAT_MAX_REPLY_CHARS)
    sig: str | None = Field(None, pattern=CHAT_SIG_RE)

    model_config = dto_examples(
        {"role": "user", "text": "What changed since the last readout?"},
        {"role": "assistant", "text": "Revenue went up by 4% (readout 12).", "sig": _EX_SIG},
    )

    @model_validator(mode="after")
    def _by_role(self) -> ChatTurn:
        if self.role == "user":
            if self.sig is not None:
                raise ValueError("a user turn has no sig")
            if len(self.text) > CHAT_MAX_USER_CHARS:
                raise ValueError(f"a user turn has at most {CHAT_MAX_USER_CHARS} characters")
        elif self.sig is None:
            raise ValueError("an assistant turn needs the sig the server returned")
        return self


class ChatRequest(RequestModel):
    """``POST /v1/dashboards/{dashboard_id}/chat`` and ``POST /v1/shared/chat``.

    ``messages`` is the conversation so far plus the new question: it starts and ends with a
    ``user`` turn and the roles alternate. ``conversation_id`` is ``None`` on the first
    message (the server makes one). ``readout_seq`` tells the assistant which readout the
    person is looking at.
    """

    conversation_id: ConversationId | None = None
    messages: list[ChatTurn] = Field(..., min_length=1, max_length=CHAT_MAX_TURNS)
    readout_seq: int | None = Field(None, ge=1)

    model_config = dto_examples(
        {"messages": [{"role": "user", "text": "What changed since the last readout?"}]},
        {
            "conversation_id": _EX_CNV,
            "messages": [
                {"role": "user", "text": "What changed since the last readout?"},
                {"role": "assistant", "text": "Revenue went up by 4%.", "sig": _EX_SIG},
                {"role": "user", "text": "Which tile changed most?"},
            ],
            "readout_seq": 12,
        },
    )

    @model_validator(mode="after")
    def _conversation(self) -> ChatRequest:
        if self.messages[0].role != "user" or self.messages[-1].role != "user":
            raise ValueError("messages start and end with a user turn")
        for prev, turn in zip(self.messages, self.messages[1:], strict=False):
            if prev.role == turn.role:
                raise ValueError("user and assistant turns alternate")
        if sum(len(t.text) for t in self.messages) > CHAT_MAX_HISTORY_CHARS:
            raise ValueError(f"the messages have more than {CHAT_MAX_HISTORY_CHARS} characters")
        return self


class ToolCallSummary(ResponseModel):
    """One tool call the assistant made in a turn (shown as a chip; never the result).

    ``args`` larger than :data:`CHAT_MAX_TOOL_ARGS_BYTES` are replaced by
    ``{"_truncated": true}``.
    """

    name: str = Field(..., min_length=1, max_length=64)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    ok: bool
    code: ErrorCode | None = None
    ms: int = Field(..., ge=0)
    truncated: bool = Field(False, description="The result was trimmed to fit the budget")

    model_config = dto_examples(
        {"name": "get_readout", "args": {"seq": 12}, "ok": True, "ms": 41},
        {
            "name": "get_panel",
            "args": {"panel_id": "nope"},
            "ok": False,
            "code": "NOT_FOUND",
            "ms": 12,
        },
    )

    @field_validator("args", mode="before")
    @classmethod
    def _small_args(cls, value: Any) -> Any:
        if isinstance(value, dict):
            try:
                size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())
            except (TypeError, ValueError):
                return {"_truncated": True}
            if size > CHAT_MAX_TOOL_ARGS_BYTES:
                return {"_truncated": True}
        return value


class ChatUsage(ResponseModel):
    """Model tokens spent by one turn (all model calls of the turn together)."""

    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    thought_tokens: int = Field(..., ge=0)
    total_tokens: int = Field(..., ge=0, description="What the daily budgets count")
    model: str = Field(..., max_length=64)

    model_config = dto_examples(
        {
            "input_tokens": 3120,
            "output_tokens": 96,
            "thought_tokens": 0,
            "total_tokens": 3216,
            "model": "gemini-3.1-flash-lite",
        }
    )


class ChatResponse(ResponseModel):
    """The answer to a :class:`ChatRequest`. Send ``reply`` back unchanged in the next
    request's ``messages``."""

    conversation_id: ConversationId
    reply: ChatTurn
    tool_calls: list[ToolCallSummary] = Field(default_factory=list, max_length=CHAT_MAX_TOOL_CALLS)
    usage: ChatUsage
    notices: list[ChatNoticeKind] = Field(default_factory=list)
    transcript_kept: bool = False

    model_config = dto_examples(
        {
            "conversation_id": _EX_CNV,
            "reply": {
                "role": "assistant",
                "text": "Revenue went up by 4% from readout 11 to readout 12.",
                "sig": _EX_SIG,
            },
            "tool_calls": [
                {"name": "compare_readouts", "args": {"a_seq": 11, "b_seq": 12}, "ok": True,
                 "ms": 38},
            ],
            "usage": ChatUsage.model_json_schema()["examples"][0],
            "notices": [],
            "transcript_kept": False,
        }
    )  # fmt: skip

    @model_validator(mode="after")
    def _assistant(self) -> ChatResponse:
        if self.reply.role != "assistant":
            raise ValueError("the reply is an assistant turn")
        if len(set(self.notices)) != len(self.notices):
            raise ValueError("notices must be unique")
        return self


class ChatAvailability(ResponseModel):
    """``GET …/chat``: may the caller chat on this dashboard, and what to show first."""

    enabled_for_me: bool
    reason: ChatUnavailableReason | None = None
    keeps_transcripts: bool = False
    transcript_days: int = Field(CHAT_TRANSCRIPT_DAYS, ge=0)
    model: str | None = Field(None, max_length=64)
    suggestions: list[str] = Field(default_factory=list, max_length=4)

    model_config = dto_examples(
        {
            "enabled_for_me": True,
            "keeps_transcripts": False,
            "transcript_days": CHAT_TRANSCRIPT_DAYS,
            "model": "gemini-3.1-flash-lite",
            "suggestions": list(CHAT_SUGGESTIONS),
        },
        {"enabled_for_me": False, "reason": "off", "suggestions": []},
    )

    @model_validator(mode="after")
    def _reason(self) -> ChatAvailability:
        if self.enabled_for_me != (self.reason is None):
            raise ValueError("a reason is given exactly when chat is not available")
        return self


# ================================================================================= settings


class ChatAudiences(StrictModel):
    """Who may chat when chat is on: members, client users with a grant, share links."""

    members: bool = True
    clients: bool = True
    share_links: bool = False


class ChatSettingsView(ResponseModel):
    """``GET``/``PUT …/chat/settings`` (editors). ``etag`` is the ``If-Match`` value of the
    next update; ``used_today_tokens`` and ``est_micros_today`` cover the current UTC day;
    ``transcript_days`` is how long the server keeps chats (``MAPI_CHAT_TRANSCRIPT_TTL_DAYS``)."""

    dashboard_id: Id
    enabled: bool = False
    audiences: ChatAudiences = Field(default_factory=ChatAudiences)
    keep_transcripts: bool = False
    tokens_per_day: int = Field(CHAT_DEFAULT_TOKENS_PER_DAY, ge=0, le=CHAT_MAX_TOKENS_PER_DAY)
    share_tokens_per_day: int = Field(
        CHAT_DEFAULT_SHARE_TOKENS_PER_DAY, ge=0, le=CHAT_MAX_SHARE_TOKENS_PER_DAY
    )
    used_today_tokens: int = Field(0, ge=0)
    est_micros_today: int = Field(0, ge=0)
    transcript_days: int = Field(CHAT_TRANSCRIPT_DAYS, ge=0)
    version: int = Field(1, ge=1)
    etag: str = Field(..., pattern=r'^"c[0-9]+"$')
    updated_by: Id | None = None
    updated_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "dashboard_id": _EX_DSH,
            "enabled": True,
            "audiences": {"members": True, "clients": True, "share_links": False},
            "keep_transcripts": False,
            "tokens_per_day": CHAT_DEFAULT_TOKENS_PER_DAY,
            "share_tokens_per_day": CHAT_DEFAULT_SHARE_TOKENS_PER_DAY,
            "used_today_tokens": 12_480,
            "est_micros_today": 4_210,
            "transcript_days": CHAT_TRANSCRIPT_DAYS,
            "version": 3,
            "etag": '"c3"',
            "updated_by": _EX_USER,
            "updated_at": EXAMPLE_TS,
        }
    )

    @model_validator(mode="after")
    def _etag(self) -> ChatSettingsView:
        if self.etag != chat_settings_etag(self.version):
            raise ValueError("etag must be chat_settings_etag(version)")
        return self


class ChatSettingsUpdate(RequestModel):
    """``PUT …/chat/settings`` with ``If-Match: <etag>``: the whole settings document."""

    enabled: bool
    audiences: ChatAudiences
    keep_transcripts: bool
    tokens_per_day: int = Field(..., ge=0, le=CHAT_MAX_TOKENS_PER_DAY)
    share_tokens_per_day: int = Field(..., ge=0, le=CHAT_MAX_SHARE_TOKENS_PER_DAY)

    model_config = dto_examples(
        {
            "enabled": True,
            "audiences": {"members": True, "clients": True, "share_links": False},
            "keep_transcripts": False,
            "tokens_per_day": CHAT_DEFAULT_TOKENS_PER_DAY,
            "share_tokens_per_day": CHAT_DEFAULT_SHARE_TOKENS_PER_DAY,
        }
    )


# ============================================================================== transcripts


class ChatTranscriptSummary(ResponseModel):
    """One kept conversation (editors). ``principal_label`` is a user's display name or
    ``Share link <name>``; it is never an IP address."""

    conversation_id: ConversationId
    principal_kind: PrincipalKind
    principal_label: str = Field(..., max_length=200)
    turns: int = Field(..., ge=0)
    first_at: AwareDatetime
    last_at: AwareDatetime
    expires_at: AwareDatetime

    model_config = dto_examples(
        {
            "conversation_id": _EX_CNV,
            "principal_kind": "client",
            "principal_label": "Dana Client",
            "turns": 4,
            "first_at": EXAMPLE_TS,
            "last_at": "2026-10-15T09:34:00Z",
            "expires_at": "2026-11-14T09:34:00Z",
        }
    )


class ChatTranscriptTurn(ResponseModel):
    """One kept turn: its text and the names of the tools called (never their results)."""

    index: int = Field(..., ge=0, le=63)
    role: ChatRole
    text: str = Field(..., max_length=CHAT_MAX_REPLY_CHARS)
    tool_calls: list[str] = Field(default_factory=list, max_length=CHAT_MAX_TOOL_CALLS)
    ok: bool = True
    at: AwareDatetime

    model_config = dto_examples(
        {
            "index": 1,
            "role": "assistant",
            "text": "Revenue went up by 4%.",
            "tool_calls": ["compare_readouts"],
            "ok": True,
            "at": EXAMPLE_TS,
        }
    )


class ChatTranscript(ResponseModel):
    """``GET …/chat/transcripts/{conversation_id}``: a kept conversation, oldest turn first."""

    summary: ChatTranscriptSummary
    turns: list[ChatTranscriptTurn]

    model_config = dto_examples(
        {
            "summary": ChatTranscriptSummary.model_json_schema()["examples"][0],
            "turns": [
                {"index": 0, "role": "user", "text": "What changed?", "at": EXAMPLE_TS},
                ChatTranscriptTurn.model_json_schema()["examples"][0],
            ],
        }
    )


# ============================================================================== read tokens


class ReadTokenCreate(RequestModel):
    """``POST …/read-tokens``: a read token for this dashboard, owned by the caller. The
    server sets the scope (``dashboard:<dashboard_id>``) and the owner."""

    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    expires_in_days: int = Field(READ_TOKEN_DEFAULT_DAYS, ge=1, le=READ_TOKEN_MAX_DAYS)

    model_config = dto_examples({"name": "Claude Desktop", "expires_in_days": 30})


class ReadToken(ResponseModel):
    """A dashboard read token as listed (never its secret or hash)."""

    id: Id = Field(..., pattern=TOKEN_ID_RE)
    name: str
    prefix: str = Field(..., pattern=r"^[0-9a-hjkmnp-tv-z]{8}$", examples=["7k2m9q4x"])
    dashboard_id: Id
    owner_id: Id
    owner_label: str = Field(..., max_length=200)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    last_used_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "id": _EX_TOK,
            "name": "Claude Desktop",
            "prefix": "7k2m9q4x",
            "dashboard_id": _EX_DSH,
            "owner_id": _EX_USER,
            "owner_label": "Dana Client",
            "created_at": EXAMPLE_TS,
            "expires_at": "2026-11-14T09:30:00Z",
            "last_used_at": "2026-10-16T08:00:00Z",
        }
    )


_EX_SECRET = "0123456789abcdefghjkmnpqrstvwxyz"


class ReadTokenCreated(ReadToken):
    """``201`` body of ``POST …/read-tokens``: the only time ``secret`` is returned."""

    secret: str = Field(
        ...,
        pattern=TOKEN_RE.pattern,
        description="The full token. Store it now; it is not shown again.",
    )

    model_config = dto_examples(
        {
            **ReadToken.model_json_schema()["examples"][0],
            "secret": str(
                ParsedToken("7k2m9q4x", _EX_SECRET, token_checksum("7k2m9q4x", _EX_SECRET))
            ),
        }
    )

    def __repr__(self) -> str:  # keep the secret out of logs and tracebacks
        return f"ReadTokenCreated(id={self.id!r}, secret=<redacted>)"

    __str__ = __repr__
