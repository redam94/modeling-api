"""v0.20 WP3: the fake chat model (scripted steps, the hook, and the default behaviour that
the live check uses)."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_server.services.chat.fake import FAKE_MODEL_ID, FakeChatModel, FakeStep
from mapi_server.services.chat.model import (
    ChatModelError,
    ModelReply,
    ModelTurn,
    build_chat_model,
    dashboard_function_decls,
    function_response_turn,
    user_text_turn,
)
from mapi_server.settings import Settings

SYSTEM = "fixed system text"


async def ask(m: FakeChatModel, turns: list[ModelTurn]) -> ModelReply:
    return await m.generate(
        system=SYSTEM, turns=turns, tools=dashboard_function_decls(), timeout_s=5
    )


READOUT: dict[str, Any] = {
    "seq": 12,
    "kind": "scheduled",
    "status": "ready",
    "title": "Weekly readout",
    "snapshot": {
        "lead": "Media brought 4,210 orders.",
        "tiles": [
            {"key": "expected", "label": "Orders from media", "value": 4210.0, "unit": "count"},
            {"key": "roi", "label": "Return on media", "value": 1.857, "unit": "ratio"},
        ],
        "channels": [
            {"name": "tv", "label": "TV", "spend": 1000.0, "roi": 1.86},
            {"name": "paid_search", "label": "Search", "spend": 500.0, "roi": 2.4},
        ],
    },
}
LISTING: dict[str, Any] = {
    "items": [
        {"seq": 12, "status": "ready"},
        {"seq": 11, "status": "failed"},
        {"seq": 10, "status": "ready"},
    ]
}
COMPARISON: dict[str, Any] = {
    "a": {"seq": 10},
    "b": {"seq": 12},
    "same_model": True,
    "tiles": [
        {"key": "expected", "label": "Orders from media", "a": 4090, "b": 4210, "delta": 120}
    ],
    "lead": "Media brought 120 more orders.",
}
CHANNEL: dict[str, Any] = {
    "name": "tv",
    "label": "TV",
    "points": [
        {"seq": 11, "spend": 900.0, "contribution": 1700.0, "roi": 1.9},
        {"seq": 12, "spend": 1000.0, "contribution": 1860.0, "roi": 1.86},
    ],
}


async def run_default(question: str, results: dict[str, dict[str, Any]]) -> tuple[list[str], str]:
    """Drive the default behaviour like the loop does; return the tools called and the text."""
    m = FakeChatModel()
    turns: list[ModelTurn] = [user_text_turn(question)]
    called: list[str] = []
    for _ in range(6):
        reply = await ask(m, turns)
        if not reply.calls:
            return called, reply.text
        turns.append(reply.turn)
        answers = []
        for call in reply.calls:
            called.append(call.name)
            value = results.get(call.name)
            response = (
                {"result": value}
                if value is not None
                else {"error": {"code": "NOT_FOUND", "message": "Not found."}}
            )
            answers.append((call, response))
        turns.append(function_response_turn(answers))
    raise AssertionError("the default behaviour did not answer within 6 calls")


async def test_default_reads_the_latest_readout_and_quotes_its_numbers() -> None:
    called, text = await run_default("How are we doing?", {"get_latest_readout": READOUT})
    assert called == ["get_latest_readout"]
    assert "Readout 12" in text
    assert "4,210" in text and "1.86" in text
    assert "Check these numbers on the dashboard." in text


async def test_default_reads_a_named_channel() -> None:
    called, text = await run_default(
        "How is TV doing?", {"get_latest_readout": READOUT, "get_channel": CHANNEL}
    )
    assert called == ["get_latest_readout", "get_channel"]
    assert "TV spend in readout 12: 1,000" in text
    assert "TV return in readout 12: 1.86" in text


async def test_default_channel_by_name_with_underscore() -> None:
    m = FakeChatModel()
    turns = [user_text_turn("and paid search?")]
    first = await ask(m, turns)
    turns += [first.turn, function_response_turn([(first.calls[0], {"result": READOUT})])]
    second = await ask(m, turns)
    assert second.calls[0].name == "get_channel"
    assert second.calls[0].args == {"name": "paid_search"}


async def test_default_compares_with_the_previous_ready_readout() -> None:
    called, text = await run_default(
        "What changed since the last readout?",
        {"get_latest_readout": READOUT, "list_readouts": LISTING, "compare_readouts": COMPARISON},
    )
    assert called == ["get_latest_readout", "list_readouts", "compare_readouts"]
    assert "Changes since readout 10:" in text and "120" in text


async def test_default_compare_args() -> None:
    m = FakeChatModel()
    turns = [user_text_turn("What changed?")]
    for name, value in (("get_latest_readout", READOUT), ("list_readouts", LISTING)):
        reply = await ask(m, turns)
        assert reply.calls[0].name == name
        turns += [reply.turn, function_response_turn([(reply.calls[0], {"result": value})])]
    reply = await ask(m, turns)
    assert reply.calls[0].name == "compare_readouts"
    assert reply.calls[0].args == {"a_seq": 10, "b_seq": 12}


async def test_default_tool_error_is_said_plainly() -> None:
    called, text = await run_default("How are we doing?", {})
    assert called == ["get_latest_readout"]
    assert text.startswith("I could not read the latest readout.")


async def test_default_starts_again_for_each_new_question() -> None:
    m = FakeChatModel()
    turns = [user_text_turn("How are we doing?")]
    first = await ask(m, turns)
    turns += [first.turn, function_response_turn([(first.calls[0], {"result": READOUT})])]
    answer = await ask(m, turns)
    assert answer.calls == () and answer.text
    turns += [answer.turn, user_text_turn("And now?")]
    again = await ask(m, turns)
    assert again.calls[0].name == "get_latest_readout"


async def test_default_without_tools_answers_text() -> None:
    m = FakeChatModel()
    reply = await m.generate(system=SYSTEM, turns=[user_text_turn("hi")], tools=[], timeout_s=1)
    assert reply.calls == () and reply.text


async def test_scripted_steps_and_recording() -> None:
    m = FakeChatModel(
        [
            FakeStep(call=("get_readout", {"seq": 3}), usage=100, thoughts=40),
            FakeStep(text="Readout 3 shows 7 orders.", usage=60),
        ]
    )
    q = user_text_turn("Tell me about readout 3")
    first = await ask(m, [q])
    assert first.calls[0].name == "get_readout" and first.calls[0].args == {"seq": 3}
    assert first.calls[0].id == "fake-call-0"
    assert first.turn.parts == (
        {"functionCall": {"id": "fake-call-0", "name": "get_readout", "args": {"seq": 3}}},
    )
    assert first.usage.thoughts == 40
    assert first.usage.total == 140
    assert first.usage.input + first.usage.output == 100
    turns = [q, first.turn, function_response_turn([(first.calls[0], {"result": {}})])]
    second = await ask(m, turns)
    assert second.text == "Readout 3 shows 7 orders." and second.calls == ()
    assert second.finish_reason == "STOP"
    assert [c[0] for c in m.calls] == [SYSTEM, SYSTEM]
    assert list(m.calls[1][1]) == turns
    assert len(m.tools_seen[0]) == 14
    with pytest.raises(ChatModelError) as info:  # the script is used up
        await ask(m, turns)
    assert info.value.kind == "bad_response"


async def test_scripted_step_with_text_and_call_and_error() -> None:
    m = FakeChatModel(
        [
            FakeStep(call=("get_dashboard", {}), text="Let me look."),
            FakeStep(error="timeout"),
            FakeStep(text="cut", finish_reason="MAX_TOKENS"),
        ]
    )
    both = await ask(m, [user_text_turn("q")])
    assert both.text == "Let me look." and both.calls[0].name == "get_dashboard"
    assert [next(iter(p)) for p in both.turn.parts] == ["text", "functionCall"]
    with pytest.raises(ChatModelError) as info:
        await ask(m, [user_text_turn("q")])
    assert info.value.kind == "timeout"
    cut = await ask(m, [user_text_turn("q")])
    assert cut.finish_reason == "MAX_TOKENS"


async def test_on_generate_hook_runs_before_each_call() -> None:
    seen: list[tuple[int, int]] = []
    m: FakeChatModel

    async def hook(n: int) -> None:
        seen.append((n, len(m.calls)))
        if n == 1:
            raise RuntimeError("revoked")

    m = FakeChatModel([FakeStep(call=("get_dashboard", {})), FakeStep(text="x")], on_generate=hook)
    await ask(m, [user_text_turn("q")])
    with pytest.raises(RuntimeError):
        await ask(m, [user_text_turn("q")])
    assert seen == [(0, 0), (1, 1)]
    assert len(m.calls) == 1  # the call the hook stopped never reached the model


def test_build_fake_model() -> None:
    m = build_chat_model(Settings(chat_provider="fake"))
    assert isinstance(m, FakeChatModel) and m.model_id == FAKE_MODEL_ID
    assert m.script is None
