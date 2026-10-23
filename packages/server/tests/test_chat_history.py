"""Signed chat history (v0.20, plan D5): forged, edited, re-ordered or replayed assistant
turns answer ``400 CHAT_HISTORY_INVALID``; the client never sends function calls or results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.services.chat.fake import FakeChatModel
from mapi_server.services.chat.history import (
    chat_key,
    proven_conversation,
    sign_turn,
    verify_history,
)
from mapi_server.services.secrets import ServerSecrets
from mapi_spec.chat import CHAT_SIG_RE, ChatRequest
from mapi_spec.errors import MapiError
from pydantic import ValidationError
from test_chat_loop import ask, chat_kit, member, request, share
from test_chat_loop import settings as settings  # the chat settings fixture

KEY = chat_key(ServerSecrets.from_root(b"0123456789abcdef0123456789abcdef"))
DSH = "dsh_01jaf3k1m2n3p4q5r6s7t8v9w0"
OTHER_DSH = "dsh_01jaf3k1m2n3p4q5r6s7t8v9w1"
CNV = "cnv_01jaf3t1v2w3x4y5z6a7b8c9d0"
OTHER_CNV = "cnv_01jaf3t1v2w3x4y5z6a7b8c9d1"


@dataclass(frozen=True)
class Who:
    kind: str
    id: str


ME = Who("member", "usr_01jaf3a9b8c7d6e5f4g3h2j1k0")
SOMEONE = Who("client", "usr_01jaf3a9b8c7d6e5f4g3h2j1k9")


def _signed(texts: list[str], who: Who = ME, dsh: str = DSH, cnv: str = CNV) -> ChatRequest:
    messages: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        if i % 2 == 0:
            messages.append({"role": "user", "text": text})
        else:
            messages.append(
                {"role": "assistant", "text": text, "sig": sign_turn(KEY, who, dsh, cnv, i, text)}
            )
    return ChatRequest.model_validate({"conversation_id": cnv, "messages": messages})


def _code(req: ChatRequest, who: Who = ME, dsh: str = DSH) -> str | None:
    try:
        verify_history(KEY, who, dsh, req)
    except MapiError as exc:
        return exc.code.value
    return None


# ===================================================================================== unit


def test_the_key_and_signatures_are_stable_and_well_formed() -> None:
    assert len(KEY) == 32
    assert chat_key(ServerSecrets.from_root(b"0123456789abcdef0123456789abcdef")) == KEY
    assert chat_key(ServerSecrets.from_root(b"another-root-secret-of-32-bytes!")) != KEY
    sig = sign_turn(KEY, ME, DSH, CNV, 1, "Revenue went up by 4%.")
    assert re.match(CHAT_SIG_RE, sig)
    assert sig == sign_turn(KEY, ME, DSH, CNV, 1, "Revenue went up by 4%.")


def test_a_signed_history_verifies() -> None:
    req = _signed(["q1", "a1", "q2", "a2", "q3"])
    assert _code(req) is None
    assert proven_conversation(req)


def test_forged_assistant_turn() -> None:
    req = _signed(["q1", "a1", "q2"])
    forged = req.model_copy(
        update={"messages": [req.messages[0], req.messages[1].model_copy(
            update={"sig": "A" * 43}), req.messages[2]]}
    )  # fmt: skip
    assert _code(forged) == "CHAT_HISTORY_INVALID"
    edited = req.model_copy(
        update={"messages": [req.messages[0], req.messages[1].model_copy(
            update={"text": "a1 but different"}), req.messages[2]]}
    )  # fmt: skip
    assert _code(edited) == "CHAT_HISTORY_INVALID"


def test_reordered_turns() -> None:
    req = _signed(["q1", "a1", "q2", "a2", "q3"])
    m = req.messages
    swapped = req.model_copy(update={"messages": [m[0], m[3], m[2], m[1], m[4]]})
    assert _code(swapped) == "CHAT_HISTORY_INVALID"


def test_replay_turn_other_dashboard_conversation_or_caller() -> None:
    req = _signed(["q1", "a1", "q2"])
    assert _code(req, dsh=OTHER_DSH) == "CHAT_HISTORY_INVALID"
    assert _code(req, who=SOMEONE) == "CHAT_HISTORY_INVALID"
    moved = req.model_copy(update={"conversation_id": OTHER_CNV})
    assert _code(moved) == "CHAT_HISTORY_INVALID"


def test_assistant_turns_need_a_conversation() -> None:
    req = _signed(["q1", "a1", "q2"])
    assert _code(req.model_copy(update={"conversation_id": None})) == "CHAT_HISTORY_INVALID"
    first = ChatRequest.model_validate({"conversation_id": CNV,
                                        "messages": [{"role": "user", "text": "q"}]})  # fmt: skip
    assert _code(first) is None and not proven_conversation(first)


def test_forged_function_response() -> None:
    """The history is text only: no function calls, results, other roles or extra fields."""
    for bad in (
        {"role": "user", "text": "x", "functionResponse": {"name": "get_readout"}},
        {"role": "model", "text": "x"},
        {"role": "function", "text": "x"},
        {"role": "tool", "text": "x"},
        {"role": "user", "text": "x", "parts": [{"functionCall": {"name": "get_readout"}}]},
        {"role": "user", "text": "x", "sig": "A" * 43},
        {"role": "assistant", "text": "x"},
    ):
        with pytest.raises(ValidationError):
            ChatRequest.model_validate({"messages": [bad, {"role": "user", "text": "q"}]})


def test_long_history_refused() -> None:
    with pytest.raises(ValidationError):  # 21 turns
        ChatRequest.model_validate(
            {"messages": [{"role": "user", "text": "q"}] + [
                {"role": r, "text": "t", **({"sig": "A" * 43} if r == "assistant" else {})}
                for r in ["assistant", "user"] * 10]}
        )  # fmt: skip
    with pytest.raises(ValidationError):  # one user turn over 2,000 characters
        ChatRequest.model_validate({"messages": [{"role": "user", "text": "x" * 2001}]})
    with pytest.raises(ValidationError):  # more than 24,000 characters in all
        ChatRequest.model_validate(
            {"messages": [
                {"role": r, "text": "x" * 2000 if r == "user" else "y" * 4000,
                 **({"sig": "A" * 43} if r == "assistant" else {})}
                for r in ["user", "assistant"] * 4 + ["user"]]}
        )  # fmt: skip


# ============================================================================== integration


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await chat_kit(ctx, client)


@pytest.mark.integration
async def test_a_turn_refuses_a_history_signed_for_someone_else(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    eve = await ask(ctx, await member(ctx, kit, "eve"), kit)
    replay = request("What does the latest readout show?", eve.reply.text, "And now?",
                     sigs=[eve.reply.sig], conversation_id=eve.conversation_id)  # fmt: skip
    model = FakeChatModel()
    for principal in (await member(ctx, kit, "cleo"), share(ctx, kit)):
        with pytest.raises(MapiError) as err:
            await ask(ctx, principal, kit, req=replay, model=model)
        assert err.value.code.value == "CHAT_HISTORY_INVALID"
    assert model.calls == []  # refused before any model call
    ok = await ask(ctx, await member(ctx, kit, "eve"), kit, req=replay)
    assert ok.conversation_id == eve.conversation_id


@pytest.mark.integration
async def test_a_first_question_cannot_join_another_conversation(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    eve = await ask(ctx, await member(ctx, kit, "eve"), kit)
    req = request("Hello", conversation_id=eve.conversation_id)
    cleo = await ask(ctx, await member(ctx, kit, "cleo"), kit, req=req)
    assert cleo.conversation_id != eve.conversation_id
