"""v0.20 chat and read-token contracts: DTO rules, the endpoint table and the registries."""

from __future__ import annotations

import re
from typing import Any, get_args

import pytest
from mapi_spec import api, chat
from mapi_spec.admin import KillSwitches, KillSwitchUpdate
from mapi_spec.api import (
    CLIENT_OPERATIONS,
    DASHBOARD_TOKEN_ROUTES,
    ROUTE_ACCESS,
    SESSION_ONLY_OPERATIONS,
    V020_ENDPOINTS,
    RouteScope,
)
from mapi_spec.audit import AuditAction
from mapi_spec.chat import (
    CHAT_MAX_HISTORY_CHARS,
    CHAT_MAX_REPLY_CHARS,
    CHAT_MAX_TOOL_ARGS_BYTES,
    CHAT_MAX_TOOL_CALLS,
    CHAT_MAX_TURNS,
    CHAT_MAX_USER_CHARS,
    CHAT_SUGGESTIONS,
    CHAT_TOOL_RESULT_MAX_BYTES,
    MCP_TOOL_RESULT_MAX_BYTES,
    ChatAudiences,
    ChatAvailability,
    ChatRequest,
    ChatResponse,
    ChatSettingsUpdate,
    ChatSettingsView,
    ChatTurn,
    ReadTokenCreate,
    ReadTokenCreated,
    ToolCallSummary,
    chat_settings_etag,
    new_conversation_id,
)
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.tenancy import PROJECT_HEADER, Action, ProjectRole, ResourceKind, can
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit

SIG = "q3Jx8m1V0bYH7kQf2nTzL5cWd9pR4sAe6uGiOjKvXyw"
I3_OPERATIONS = {
    "get_dashboard_chat", "post_dashboard_chat", "get_dashboard_chat_settings",
    "put_dashboard_chat_settings", "list_dashboard_chat_transcripts",
    "get_dashboard_chat_transcript", "delete_dashboard_chat_transcript", "get_shared_chat",
    "post_shared_chat", "list_dashboard_read_tokens", "create_dashboard_read_token",
    "revoke_dashboard_read_token", "mcp_endpoint",
}  # fmt: skip


def user(text: str = "What changed?") -> dict[str, Any]:
    return {"role": "user", "text": text}


def bot(text: str = "Revenue went up.", sig: str | None = SIG) -> dict[str, Any]:
    return {"role": "assistant", "text": text, "sig": sig}


# ======================================================================================= limits


def test_limits_are_the_plan_values() -> None:
    assert (CHAT_MAX_TURNS, CHAT_MAX_USER_CHARS, CHAT_MAX_HISTORY_CHARS) == (20, 2000, 24_000)
    assert (CHAT_MAX_REPLY_CHARS, CHAT_MAX_TOOL_CALLS) == (4000, 6)
    assert (CHAT_TOOL_RESULT_MAX_BYTES, MCP_TOOL_RESULT_MAX_BYTES) == (24_576, 65_536)
    assert len(CHAT_SUGGESTIONS) == 4 and all(s.strip() == s and s for s in CHAT_SUGGESTIONS)
    assert re.fullmatch(chat.CONVERSATION_ID_RE, new_conversation_id())


# ======================================================================================== turns


def test_user_turns_have_no_sig_and_a_short_text() -> None:
    assert ChatTurn.model_validate(user()).sig is None
    with pytest.raises(ValidationError, match="no sig"):
        ChatTurn.model_validate({**user(), "sig": SIG})
    ChatTurn.model_validate(user("x" * CHAT_MAX_USER_CHARS))
    with pytest.raises(ValidationError, match="at most 2000"):
        ChatTurn.model_validate(user("x" * (CHAT_MAX_USER_CHARS + 1)))
    with pytest.raises(ValidationError):
        ChatTurn.model_validate(user(""))


def test_assistant_turns_need_a_well_formed_sig() -> None:
    assert ChatTurn.model_validate(bot()).sig == SIG
    with pytest.raises(ValidationError, match="needs the sig"):
        ChatTurn.model_validate(bot(sig=None))
    for bad in ("short", SIG + "A", SIG[:-1] + "=", SIG[:-1] + "+"):
        with pytest.raises(ValidationError):
            ChatTurn.model_validate(bot(sig=bad))
    ChatTurn.model_validate(bot("x" * CHAT_MAX_REPLY_CHARS))
    with pytest.raises(ValidationError):
        ChatTurn.model_validate(bot("x" * (CHAT_MAX_REPLY_CHARS + 1)))


def test_function_calls_cannot_be_sent_as_turns() -> None:
    for extra in ({"function_call": {"name": "get_readout"}}, {"tool_result": {}}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            ChatTurn.model_validate({**user(), **extra})
    with pytest.raises(ValidationError):
        ChatTurn.model_validate({"role": "model", "text": "x"})
    with pytest.raises(ValidationError):
        ChatTurn.model_validate({"role": "function", "text": "x"})


def test_requests_alternate_and_end_with_a_user_turn() -> None:
    ok = ChatRequest.model_validate({"messages": [user(), bot(), user()]})
    assert ok.conversation_id is None and ok.readout_seq is None
    for messages, match in (
        ([user(), bot()], "end with a user turn"),
        ([bot(), user()], "start and end"),
        ([user(), user()], "alternate"),
        ([user(), bot(), bot(), user()], "alternate"),
    ):
        with pytest.raises(ValidationError, match=match):
            ChatRequest.model_validate({"messages": messages})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": []})


def test_request_caps() -> None:
    turns = [user() if i % 2 == 0 else bot() for i in range(CHAT_MAX_TURNS - 1)]
    ChatRequest.model_validate({"messages": turns})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": [*turns, bot(), user()]})
    # 24,000 characters in all: 7 turns of 4,000 are too many even when each is valid
    long = [user("u" * 2000), bot("a" * 4000)] * 6 + [user("u" * 1)]
    assert sum(len(t["text"]) for t in long) > CHAT_MAX_HISTORY_CHARS
    with pytest.raises(ValidationError, match="more than 24000"):
        ChatRequest.model_validate({"messages": long})


def test_request_ids_and_readout_hint() -> None:
    cnv = new_conversation_id()
    req = ChatRequest.model_validate(
        {"conversation_id": cnv, "messages": [user()], "readout_seq": 3}
    )
    assert req.conversation_id == cnv and req.readout_seq == 3
    for bad in ("cnv_SHORT", "conv_01jaf3t1v2w3x4y5z6a7b8c9d0", "cnv_01JAF3T1V2W3X4Y5Z6A7B8C9D0"):
        with pytest.raises(ValidationError):
            ChatRequest.model_validate({"conversation_id": bad, "messages": [user()]})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": [user()], "readout_seq": 0})
    with pytest.raises(ValidationError, match="Extra inputs"):
        ChatRequest.model_validate({"messages": [user()], "dashboard_id": "dsh_1"})


# ===================================================================================== responses


def _response(**over: Any) -> dict[str, Any]:
    return {**ChatResponse.model_json_schema()["examples"][0], **over}


def test_response_reply_is_a_signed_assistant_turn() -> None:
    assert ChatResponse.model_validate(_response()).reply.sig == SIG
    with pytest.raises(ValidationError, match="assistant"):
        ChatResponse.model_validate(_response(reply=user()))
    with pytest.raises(ValidationError, match="needs the sig"):
        ChatResponse.model_validate(_response(reply=bot(sig=None)))
    with pytest.raises(ValidationError, match="unique"):
        ChatResponse.model_validate(_response(notices=["tool_limit", "tool_limit"]))
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(_response(notices=["something_else"]))
    calls = [{"name": "get_readout", "args": {"seq": 1}, "ok": True, "ms": 1}] * 7
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(_response(tool_calls=calls))
    assert set(get_args(chat.ChatNoticeKind)) == {
        "unverified_numbers", "tool_limit", "truncated", "budget_low", "transcript_kept",
    }  # fmt: skip


def test_tool_call_summaries_keep_small_args_only() -> None:
    small = ToolCallSummary(name="get_readout", args={"seq": 4}, ok=True, ms=3)
    assert small.args == {"seq": 4} and small.code is None and not small.truncated
    big = ToolCallSummary(
        name="evaluate_whatif", args={"x": "y" * CHAT_MAX_TOOL_ARGS_BYTES}, ok=True, ms=3
    )
    assert big.args == {"_truncated": True}
    err = ToolCallSummary(name="get_panel", args={}, ok=False, code=ErrorCode.NOT_FOUND, ms=0)
    assert err.code is ErrorCode.NOT_FOUND
    with pytest.raises(ValidationError):
        ToolCallSummary(name="get_panel", args={}, ok=False, ms=-1)


def test_availability_gives_a_reason_exactly_when_off() -> None:
    on = ChatAvailability(enabled_for_me=True, suggestions=list(CHAT_SUGGESTIONS))
    assert on.reason is None and on.transcript_days == 30
    for reason in get_args(chat.ChatUnavailableReason):
        assert not ChatAvailability(enabled_for_me=False, reason=reason).enabled_for_me
    assert set(get_args(chat.ChatUnavailableReason)) == {
        "off", "audience", "layout", "region", "provider", "killswitch", "budget",
    }  # fmt: skip
    with pytest.raises(ValidationError, match="reason"):
        ChatAvailability(enabled_for_me=False)
    with pytest.raises(ValidationError, match="reason"):
        ChatAvailability(enabled_for_me=True, reason="off")
    with pytest.raises(ValidationError):
        ChatAvailability(enabled_for_me=True, suggestions=["a"] * 5)


# ====================================================================================== settings


def test_settings_defaults_and_bounds() -> None:
    assert ChatAudiences() == ChatAudiences(members=True, clients=True, share_links=False)
    view = ChatSettingsView(dashboard_id="dsh_1", etag=chat_settings_etag(1))
    assert not view.enabled and not view.keep_transcripts and view.version == 1
    assert (view.tokens_per_day, view.share_tokens_per_day) == (200_000, 50_000)
    assert chat_settings_etag(7) == '"c7"'
    with pytest.raises(ValidationError, match="etag"):
        ChatSettingsView(dashboard_id="dsh_1", version=2, etag='"c1"')
    body = ChatSettingsUpdate.model_json_schema()["examples"][0]
    ChatSettingsUpdate.model_validate({**body, "tokens_per_day": 5_000_000})
    for field, bad in (("tokens_per_day", 5_000_001), ("share_tokens_per_day", 1_000_001),
                       ("tokens_per_day", -1)):  # fmt: skip
        with pytest.raises(ValidationError):
            ChatSettingsUpdate.model_validate({**body, field: bad})
    with pytest.raises(ValidationError, match="Extra inputs"):
        ChatSettingsUpdate.model_validate({**body, "audiences": {"members": True, "public": True}})


# =================================================================================== read tokens


def test_read_token_create_bounds() -> None:
    assert ReadTokenCreate(name="Claude Desktop").expires_in_days == 30
    ReadTokenCreate(name="x", expires_in_days=90)
    for bad in ({"name": "x", "expires_in_days": 91}, {"name": "x", "expires_in_days": 0},
                {"name": ""}, {"name": "a\nb"}, {"name": "x", "scopes": ["admin"]}):  # fmt: skip
        with pytest.raises(ValidationError):
            ReadTokenCreate.model_validate(bad)


def test_read_token_secret_never_prints() -> None:
    created = ReadTokenCreated.model_validate(ReadTokenCreated.model_json_schema()["examples"][0])
    assert created.secret.startswith("mapi_")
    assert created.secret not in repr(created) and created.secret not in str(created)


# ======================================================================== errors, audit, admin


def test_error_codes() -> None:
    expected = {
        ErrorCode.CHAT_DISABLED: 409,
        ErrorCode.CHAT_UNAVAILABLE: 503,
        ErrorCode.CHAT_BUDGET_EXHAUSTED: 429,
        ErrorCode.CHAT_HISTORY_INVALID: 400,
        ErrorCode.READ_TOKEN_LIMIT: 409,
    }
    for code, status in expected.items():
        assert ERROR_INFO[code].status == status, code
    assert ERROR_INFO[ErrorCode.CHAT_UNAVAILABLE].retryable


def test_audit_actions() -> None:
    assert AuditAction.DASHBOARD_CHAT_SETTINGS.value == "dashboard.chat_settings"
    assert AuditAction.DASHBOARD_CHAT_TRANSCRIPT_DELETE.value == "dashboard.chat_transcript_delete"
    assert AuditAction.DASHBOARD_READ_TOKEN_CREATE.value == "dashboard.read_token_create"
    assert AuditAction.DASHBOARD_READ_TOKEN_REVOKE.value == "dashboard.read_token_revoke"


def test_chat_kill_switch_defaults_on_and_updates() -> None:
    assert KillSwitches().chat_enabled is True
    off = KillSwitchUpdate(chat_enabled=False, reason="provider incident")
    from datetime import UTC, datetime

    now = datetime(2026, 10, 23, tzinfo=UTC)
    switches = off.apply(KillSwitches(), now=now, by=None)
    assert switches.chat_enabled is False and switches.untrusted_execution_enabled is True
    keep = KillSwitchUpdate(reason="cap only", global_spend_cap_micros=1).apply(
        switches, now=now, by=None
    )
    assert keep.chat_enabled is False
    # documents stored before v0.20 have no chat_enabled: chat stays on
    legacy = {k: v for k, v in KillSwitches().model_dump().items() if k != "chat_enabled"}
    assert KillSwitches.model_validate(legacy).chat_enabled is True


# ===================================================================================== endpoints


def _resolve(name: str) -> type[BaseModel]:
    inner = re.sub(r"^(?:Page|list)\[(\w+)\]$", r"\1", name)
    obj = getattr(api, inner)
    assert isinstance(obj, type) and issubclass(obj, BaseModel), name
    return obj


def test_v020_table_is_last_and_complete() -> None:
    assert api.ENDPOINTS[-len(V020_ENDPOINTS) :] == V020_ENDPOINTS
    ops = [e.operation_id for e in V020_ENDPOINTS]
    assert len(ops) == len(set(ops)) and set(ops) == I3_OPERATIONS
    assert set(ROUTE_ACCESS) >= I3_OPERATIONS
    assert "V020_ENDPOINTS" in api.__all__
    keys = [(e.method, e.path) for e in api.ENDPOINTS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("e", V020_ENDPOINTS, ids=lambda e: e.operation_id)
def test_v020_rows_follow_the_conventions(e: api.EndpointDecl) -> None:
    assert e.success in (200, 201, 204)
    assert (e.success == 204) <= (e.response is None)
    for param in re.findall(r"\{([^}]+)\}", e.path):
        assert re.fullmatch(r"[a-z_]+", param), e.path
    if e.request is not None:
        assert e.method in ("POST", "PUT")
        _resolve(e.request)
    if e.response is not None:
        _resolve(e.response)
    assert e.paginated == (e.response or "").startswith("Page[")
    if e.paginated:
        assert "limit" in e.query
    for code in e.errors:
        assert code in ERROR_INFO
    acc = ROUTE_ACCESS[e.operation_id]
    if e.path.startswith(("/v1/shared/", "/v1/mcp")):
        assert acc.scope is RouteScope.PUBLIC and PROJECT_HEADER not in e.headers
        assert SHARE_SESSION_HEADER in e.headers
    else:
        assert e.path.startswith("/v1/dashboards/{dashboard_id}/")
        assert acc.scope is RouteScope.PROJECT and PROJECT_HEADER in e.headers
        assert acc.resource is ResourceKind.DASHBOARD and acc.name_param == "dashboard_id"
        assert not acc.also


def test_the_i3_contract() -> None:
    by_op = {e.operation_id: e for e in V020_ENDPOINTS}
    rows = {
        "get_dashboard_chat": ("GET", "/v1/dashboards/{dashboard_id}/chat", "ChatAvailability"),
        "post_dashboard_chat": ("POST", "/v1/dashboards/{dashboard_id}/chat", "ChatResponse"),
        "put_dashboard_chat_settings": (
            "PUT", "/v1/dashboards/{dashboard_id}/chat/settings", "ChatSettingsView",
        ),
        "list_dashboard_chat_transcripts": (
            "GET", "/v1/dashboards/{dashboard_id}/chat/transcripts", "Page[ChatTranscriptSummary]",
        ),
        "delete_dashboard_chat_transcript": (
            "DELETE", "/v1/dashboards/{dashboard_id}/chat/transcripts/{conversation_id}", None,
        ),
        "post_shared_chat": ("POST", "/v1/shared/chat", "ChatResponse"),
        "list_dashboard_read_tokens": (
            "GET", "/v1/dashboards/{dashboard_id}/read-tokens", "list[ReadToken]",
        ),
        "create_dashboard_read_token": (
            "POST", "/v1/dashboards/{dashboard_id}/read-tokens", "ReadTokenCreated",
        ),
        "revoke_dashboard_read_token": (
            "DELETE", "/v1/dashboards/{dashboard_id}/read-tokens/{token_id}", None,
        ),
        "mcp_endpoint": ("POST", "/v1/mcp", None),
    }  # fmt: skip
    for op, (method, path, response) in rows.items():
        e = by_op[op]
        assert (e.method, e.path, e.response) == (method, path, response), op
    assert by_op["create_dashboard_read_token"].success == 201
    assert by_op["post_dashboard_chat"].request == "ChatRequest"
    assert by_op["put_dashboard_chat_settings"].request == "ChatSettingsUpdate"
    assert "If-Match" in by_op["put_dashboard_chat_settings"].headers
    assert ErrorCode.PRECONDITION_FAILED in by_op["put_dashboard_chat_settings"].errors
    assert by_op["create_dashboard_read_token"].request == "ReadTokenCreate"
    assert ErrorCode.READ_TOKEN_LIMIT in by_op["create_dashboard_read_token"].errors
    assert by_op["mcp_endpoint"].request is None  # a generic JSON-RPC body
    for op in ("post_dashboard_chat", "post_shared_chat"):
        for code in ("CHAT_DISABLED", "CHAT_UNAVAILABLE", "CHAT_BUDGET_EXHAUSTED",
                     "CHAT_HISTORY_INVALID", "PAYLOAD_TOO_LARGE"):  # fmt: skip
            assert ErrorCode(code) in by_op[op].errors, (op, code)


def test_access_rules() -> None:
    read = {"get_dashboard_chat", "post_dashboard_chat", "list_dashboard_read_tokens",
            "create_dashboard_read_token", "revoke_dashboard_read_token"}  # fmt: skip
    update = {"get_dashboard_chat_settings", "put_dashboard_chat_settings",
              "list_dashboard_chat_transcripts", "get_dashboard_chat_transcript",
              "delete_dashboard_chat_transcript"}  # fmt: skip
    public = {"get_shared_chat", "post_shared_chat", "mcp_endpoint"}
    assert read | update | public == I3_OPERATIONS
    for op in read:
        assert (ROUTE_ACCESS[op].resource, ROUTE_ACCESS[op].action) == (
            ResourceKind.DASHBOARD, Action.READ,
        ), op  # fmt: skip
        assert can(ProjectRole.CLIENT, Action.READ, ResourceKind.DASHBOARD)
    for op in update:
        acc = ROUTE_ACCESS[op]
        assert (acc.resource, acc.action) == (ResourceKind.DASHBOARD, Action.UPDATE), op
        assert not can(ProjectRole.VIEWER, Action.UPDATE, ResourceKind.DASHBOARD)
        assert can(ProjectRole.EDITOR, Action.UPDATE, ResourceKind.DASHBOARD)
    for op in public:
        assert ROUTE_ACCESS[op].scope is RouteScope.PUBLIC, op


def test_client_operations_gain_exactly_chat_and_own_read_tokens() -> None:
    v020_client = {e.operation_id for e in V020_ENDPOINTS} & CLIENT_OPERATIONS
    assert v020_client == {
        "get_dashboard_chat", "post_dashboard_chat", "list_dashboard_read_tokens",
        "create_dashboard_read_token", "revoke_dashboard_read_token",
    }  # fmt: skip
    # settings and kept conversations stay with editors; clients can still not mint tokens
    for op in ("get_dashboard_chat_settings", "put_dashboard_chat_settings",
               "list_dashboard_chat_transcripts", "get_dashboard_chat_transcript",
               "delete_dashboard_chat_transcript", "create_token", "list_tokens"):  # fmt: skip
        assert op not in CLIENT_OPERATIONS, op


def test_dashboard_token_routes_and_session_only_operations() -> None:
    assert frozenset({"mcp_endpoint"}) == DASHBOARD_TOKEN_ROUTES
    assert ROUTE_ACCESS["mcp_endpoint"].scope is RouteScope.PUBLIC
    assert (
        frozenset(
            {
                "post_dashboard_chat",
                "create_dashboard_read_token",
                "list_dashboard_chat_transcripts",
                "get_dashboard_chat_transcript",
                "delete_dashboard_chat_transcript",
                "list_dashboard_read_tokens",
                "revoke_dashboard_read_token",
            }
        )
        == SESSION_ONLY_OPERATIONS
    )
    assert set(ROUTE_ACCESS) >= SESSION_ONLY_OPERATIONS
    # session-only routes resolve a credential (never public)
    for op in SESSION_ONLY_OPERATIONS:
        assert ROUTE_ACCESS[op].scope is RouteScope.PROJECT, op
    for name in ("DASHBOARD_TOKEN_ROUTES", "SESSION_ONLY_OPERATIONS", "ChatRequest",
                 "ChatResponse", "ReadTokenCreated"):  # fmt: skip
        assert name in api.__all__, name
