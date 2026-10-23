"""v0.20 WP3: the Gemini ``generateContent`` adapter against recorded fixtures.

No test here reaches the network: every model is built with an ``httpx.MockTransport`` that
serves a fixture from ``fixtures/gemini`` (made from the API reference, plan probe (c)), and
the session guard in ``conftest.py`` fails any test that tries to resolve the Gemini host.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.services.chat.gemini import (
    GEMINI_ENDPOINT,
    GEMINI_HOST,
    MAX_RESPONSE_BYTES,
    GeminiChatModel,
)
from mapi_server.services.chat.model import (
    ChatModelError,
    FunctionCall,
    ModelTurn,
    build_chat_model,
    dashboard_function_decls,
    function_response_turn,
    user_text_turn,
)
from mapi_server.settings import GEMINI_DEFAULT_ENDPOINT, Settings
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, tools_json_schema
from pydantic import SecretStr

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gemini"
KEY = "AIzaTEST-not-a-real-key-7f3c9e"
SYSTEM = "You explain one dashboard. Every number must come from a tool result."


def fixture(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
    return doc


class Recorder:
    """A mock transport that answers with fixtures in order and records each request."""

    def __init__(self, *names: str) -> None:
        self.names = list(names)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        doc = fixture(self.names.pop(0))
        return httpx.Response(doc["status"], json=doc["body"])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def body(self, i: int = -1) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.requests[i].content)
        return out


def model(
    transport: httpx.AsyncBaseTransport, **kwargs: Any
) -> GeminiChatModel:  # the settings' defaults unless overridden
    return GeminiChatModel(SecretStr(KEY), "gemini-3.1-flash-lite", transport=transport, **kwargs)


async def generate(m: GeminiChatModel, turns: list[ModelTurn] | None = None) -> Any:
    return await m.generate(
        system=SYSTEM,
        turns=turns or [user_text_turn("What changed since the last readout?")],
        tools=dashboard_function_decls(),
        timeout_s=5.0,
    )


def test_fixture_set_is_complete() -> None:
    names = {p.stem for p in FIXTURES.glob("*.json")}
    assert {
        "text_reply", "function_call", "function_call_thought_signature", "parallel_calls",
        "safety_block", "max_tokens", "malformed_call", "error_429", "error_400_bad_key",
        "error_401_bad_key", "error_500",
    } <= names  # fmt: skip
    for name in names:
        doc = fixture(name)
        assert doc["_source"] and isinstance(doc["status"], int)


def test_default_endpoint_is_the_settings_default() -> None:
    assert GEMINI_ENDPOINT == GEMINI_DEFAULT_ENDPOINT == f"https://{GEMINI_HOST}"
    assert Settings().gemini_endpoint == GEMINI_ENDPOINT


async def test_request_shape() -> None:
    rec = Recorder("text_reply")
    await generate(model(rec.transport))
    (request,) = rec.requests
    assert request.method == "POST"
    assert request.url.host == GEMINI_HOST
    assert request.url.path == "/v1beta/models/gemini-3.1-flash-lite:generateContent"
    # G16: the key goes in a header only, never in the URL (spans record URLs)
    assert request.headers["x-goog-api-key"] == KEY
    assert "key=" not in str(request.url) and KEY not in str(request.url)
    assert request.url.query == b""
    assert request.headers["content-type"] == "application/json"

    body = rec.body()
    assert list(body) == [
        "systemInstruction", "contents", "tools", "toolConfig", "generationConfig", "store",
    ]  # fmt: skip
    assert body["systemInstruction"] == {"parts": [{"text": SYSTEM}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "What changed since the last readout?"}]}
    ]
    (tool_block,) = body["tools"]
    decls = tool_block["functionDeclarations"]
    schema = tools_json_schema()
    assert [d["name"] for d in decls] == list(DASHBOARD_TOOL_NAMES)
    for decl, entry in zip(decls, schema, strict=True):
        assert set(decl) == {"name", "description", "parametersJsonSchema"}
        assert decl["description"] == entry["description"]
        assert decl["parametersJsonSchema"] == entry["parameters"]  # unchanged, no translation
    assert body["toolConfig"] == {
        "functionCallingConfig": {
            "mode": "VALIDATED",
            "allowedFunctionNames": list(DASHBOARD_TOOL_NAMES),
        }
    }
    assert body["generationConfig"] == {"maxOutputTokens": 1024, "temperature": 0.2}
    assert body["store"] is False
    assert KEY not in request.content.decode()


@pytest.mark.parametrize(("level", "expected"), [("minimal", "minimal"), ("low", "low")])
async def test_thinking_level_is_sent_only_when_set(level: Any, expected: str) -> None:
    rec = Recorder("text_reply")
    await generate(model(rec.transport, thinking_level=level))
    assert rec.body()["generationConfig"]["thinkingConfig"] == {"thinkingLevel": expected}


async def test_auto_mode_and_settings_limits() -> None:
    rec = Recorder("text_reply")
    await generate(model(rec.transport, function_mode="AUTO", max_output_tokens=512, temperature=0))
    body = rec.body()
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}
    assert body["generationConfig"] == {"maxOutputTokens": 512, "temperature": 0}


async def test_no_tools_sends_no_tool_config() -> None:
    rec = Recorder("text_reply")
    m = model(rec.transport)
    await m.generate(system=SYSTEM, turns=[user_text_turn("Hi")], tools=[], timeout_s=5)
    assert "tools" not in rec.body() and "toolConfig" not in rec.body()


async def test_text_reply() -> None:
    reply = await generate(model(Recorder("text_reply").transport))
    assert (
        reply.text == "Readout 12 shows 4,210 orders from media. That is 120 more than readout 11."
    )
    assert reply.calls == ()
    assert reply.finish_reason == "STOP"
    assert reply.turn.role == "model"
    assert (reply.usage.input, reply.usage.output, reply.usage.thoughts) == (1840, 22, 0)
    assert reply.usage.total == 1862


async def test_function_call_and_response_turn() -> None:
    rec = Recorder("function_call", "text_reply")
    m = model(rec.transport)
    question = user_text_turn("How are we doing?")
    reply = await generate(m, [question])
    assert reply.text == ""
    assert reply.calls == (FunctionCall(id="call_a1", name="get_latest_readout", args={}),)
    response = function_response_turn([(reply.calls[0], {"result": {"seq": 12}})])
    assert response == ModelTurn(
        role="user",
        parts=(
            {
                "functionResponse": {
                    "id": "call_a1",
                    "name": "get_latest_readout",
                    "response": {"result": {"seq": 12}},
                }
            },
        ),
    )
    await generate(m, [question, reply.turn, response])
    contents = rec.body()["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[2]["parts"][0]["functionResponse"]["id"] == "call_a1"


async def test_model_turn_with_thought_signature_is_sent_back_unchanged() -> None:
    rec = Recorder("function_call_thought_signature", "text_reply")
    m = model(rec.transport)
    question = user_text_turn("What about TV?")
    reply = await generate(m, [question])
    # the thought part is not answer text; the call keeps its args
    assert reply.text == ""
    assert reply.calls == (
        FunctionCall(id="call_b2", name="get_channel", args={"name": "tv", "last_n": 4}),
    )
    assert reply.usage.thoughts == 64
    assert reply.usage.total == 1610 + 18 + 64

    answer = function_response_turn([(reply.calls[0], {"result": {"name": "tv", "points": []}})])
    await generate(m, [question, reply.turn, answer])
    sent = rec.requests[1].content.decode()
    original = fixture("function_call_thought_signature")["body"]["candidates"][0]["content"]
    # byte-identical: the model's content is echoed exactly as it was received
    as_sent = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
    assert as_sent in sent
    assert rec.body(1)["contents"][1] == original


async def test_parallel_calls_without_ids() -> None:
    reply = await generate(model(Recorder("parallel_calls").transport))
    assert [c.args for c in reply.calls] == [{"seq": 11}, {"seq": 12}]
    assert all(c.id is None for c in reply.calls)
    response = function_response_turn([(c, {"result": {}}) for c in reply.calls])
    assert [p["functionResponse"] for p in response.parts] == [
        {"name": "get_readout", "response": {"result": {}}},
        {"name": "get_readout", "response": {"result": {}}},
    ]
    # tool-use prompt tokens are input; the total includes thoughts
    assert reply.usage.input == 1700 + 40
    assert reply.usage.thoughts == 12
    assert reply.usage.total == 1700 + 30 + 12 + 40


async def test_max_tokens_returns_the_partial_text() -> None:
    reply = await generate(model(Recorder("max_tokens").transport))
    assert reply.finish_reason == "MAX_TOKENS"
    assert reply.text.startswith("Readout 12 has six tiles.")
    assert reply.usage.cached == 512
    assert reply.usage.output == 1024


@pytest.mark.parametrize(
    ("name", "kind", "status"),
    [
        ("safety_block", "blocked", None),
        ("prompt_blocked", "blocked", None),
        ("malformed_call", "bad_response", None),
        ("error_429", "rate_limited", 429),
        ("error_401_bad_key", "auth", 401),
        ("error_400_bad_key", "auth", 400),
        ("error_400_failed_precondition", "unavailable", 400),
        ("error_500", "unavailable", 500),
    ],
)
async def test_error_mapping_carries_no_provider_text(
    name: str, kind: str, status: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ChatModelError) as info:
        await generate(model(Recorder(name).transport))
    err = info.value
    assert err.kind == kind
    assert err.status == status
    for text in (str(err), repr(err), caplog.text):
        assert "SECRET-ECHO" not in text
        assert "API key not valid" not in text
        assert KEY not in text
    assert err.__cause__ is None


@pytest.mark.parametrize(
    ("status", "kind"),
    [(403, "auth"), (402, "unavailable"), (404, "unavailable"), (408, "timeout"),
     (504, "timeout"), (503, "unavailable"), (418, "bad_response")],
)  # fmt: skip
async def test_status_mapping(status: int, kind: str) -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(status, json={"error": {"code": "x", "message": "SECRET-ECHO"}})
    )
    with pytest.raises(ChatModelError) as info:
        await generate(model(transport))
    assert info.value.kind == kind and "SECRET-ECHO" not in str(info.value)


async def test_redirects_are_not_followed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/collect"})

    with pytest.raises(ChatModelError) as info:
        await generate(model(httpx.MockTransport(handler)))
    assert info.value.kind == "unavailable" and info.value.status == 302
    assert len(seen) == 1


def raising(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.RemoteProtocolError("broken"), "unavailable"),
    ],
)
async def test_transport_failures(exc: Exception, kind: str) -> None:
    with pytest.raises(ChatModelError) as info:
        await generate(model(httpx.MockTransport(raising(exc))))
    assert info.value.kind == kind and info.value.status is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"candidates": []}',
        b'{"candidates": ["x"]}',
        b'{"candidates": [{"content": {"parts": "x"}, "finishReason": "STOP"}]}',
        b'{"candidates": [{"content": {"parts": [{"functionCall": {"name": 3}}]}}]}',
        b'{"candidates": [{"content": {"parts": [{"functionCall": {"name": "x", "args": [1]}}]}}]}',
        b'{"candidates": [{"content": {"parts": [{"text": "a"}]}, "finishReason": '
        b'"MISSING_THOUGHT_SIGNATURE"}]}',
    ],
)
async def test_invalid_responses(body: bytes) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    with pytest.raises(ChatModelError) as info:
        await generate(model(transport))
    assert info.value.kind == "bad_response"


@pytest.mark.parametrize(
    "candidate",
    [
        {"content": {"role": "model", "parts": []}, "finishReason": "STOP"},
        {"finishReason": "STOP"},
        {"content": {"role": "model"}, "finishReason": "OTHER"},
    ],
)
async def test_a_candidate_with_no_parts_is_an_empty_answer(candidate: dict[str, Any]) -> None:
    # backend-2: an empty STOP after function results is an empty answer, not a 503
    body = {"candidates": [candidate], "usageMetadata": {"promptTokenCount": 40,
                                                        "totalTokenCount": 40}}  # fmt: skip
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    reply = await generate(model(transport))
    assert reply.text == "" and reply.calls == () and reply.usage.total == 40


@pytest.mark.parametrize(
    ("name", "kind", "total"),
    [("safety_block", "blocked", 1500), ("prompt_blocked", "blocked", 900),
     ("malformed_call", "bad_response", 1507)],
)  # fmt: skip
async def test_a_failed_200_answer_carries_its_billed_usage(
    name: str, kind: str, total: int
) -> None:
    with pytest.raises(ChatModelError) as info:
        await generate(model(Recorder(name).transport))
    assert info.value.kind == kind
    assert info.value.usage is not None and info.value.usage.total == total
    assert "usage" not in str(info.value)  # the message stays fixed


async def test_http_errors_carry_no_usage() -> None:
    with pytest.raises(ChatModelError) as info:
        await generate(model(Recorder("error_500").transport))
    assert info.value.usage is None


async def test_oversized_response_is_refused() -> None:
    big = (
        b'{"candidates": [{"content": {"parts": [{"text": "'
        + b"a" * MAX_RESPONSE_BYTES
        + b'"}]}}]}'
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
    with pytest.raises(ChatModelError) as info:
        await generate(model(transport))
    assert info.value.kind == "bad_response"


async def test_bad_usage_counts_are_zero_and_total_is_never_below_its_parts() -> None:
    body = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": "12", "candidatesTokenCount": -3,
                          "thoughtsTokenCount": 5, "totalTokenCount": 1},
    }  # fmt: skip
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    reply = await generate(model(transport))
    assert (reply.usage.input, reply.usage.output, reply.usage.thoughts) == (0, 0, 5)
    assert reply.usage.total == 5


async def test_key_not_in_logs_or_spans(caplog: pytest.LogCaptureFixture) -> None:
    from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    caplog.set_level(logging.DEBUG)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    rec = Recorder("function_call", "error_429")
    # the same span code the production httpx instrumentation runs, around the mock
    traced = AsyncOpenTelemetryTransport(rec.transport, tracer_provider=provider)
    m = model(traced)
    reply = await generate(m)
    with pytest.raises(ChatModelError):
        await generate(m, [user_text_turn("q"), reply.turn])
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        dumped = json.dumps(
            {"name": span.name, "attributes": dict(span.attributes or {}),
             "events": [dict(e.attributes or {}) for e in span.events]},
            default=str,
        )  # fmt: skip
        assert KEY not in dumped
        assert "key=" not in dumped
        assert "x-goog-api-key" not in dumped.lower()
    assert KEY not in caplog.text
    assert "SECRET-ECHO" not in caplog.text


def test_model_repr_and_construction_guards() -> None:
    m = model(httpx.MockTransport(lambda r: httpx.Response(500)))
    assert KEY not in repr(m) and KEY not in str(vars(m))
    with pytest.raises(ValueError):
        GeminiChatModel(SecretStr(KEY), "../../v1/files?x=")
    with pytest.raises(ValueError):
        GeminiChatModel(SecretStr(""), "gemini-3.1-flash-lite")


def test_dashboard_function_decls() -> None:
    decls = dashboard_function_decls()
    assert len(decls) == 14
    assert tuple(d.name for d in decls) == DASHBOARD_TOOL_NAMES
    for d in decls:
        assert d.parameters_json_schema["type"] == "object"
        assert "$defs" not in d.parameters_json_schema


async def test_build_chat_model_uses_settings_and_transport() -> None:
    assert build_chat_model(Settings()) is None
    rec = Recorder("text_reply")
    settings = Settings(
        chat_provider="gemini",
        gemini_api_key=KEY,
        chat_function_mode="AUTO",
        chat_thinking_level="minimal",
        chat_max_output_tokens=256,
        chat_temperature=0.5,
    )
    built = build_chat_model(settings, transport=rec.transport)
    assert isinstance(built, GeminiChatModel)
    assert built.model_id == "gemini-3.1-flash-lite"
    await built.generate(system=SYSTEM, turns=[user_text_turn("q")], tools=[], timeout_s=5)
    assert rec.requests[0].headers["x-goog-api-key"] == KEY
    assert rec.body()["generationConfig"] == {
        "maxOutputTokens": 256,
        "temperature": 0.5,
        "thinkingConfig": {"thinkingLevel": "minimal"},
    }


async def test_the_guard_stops_a_real_gemini_call(gemini_attempts: list[str]) -> None:
    """Without an injected transport the adapter would reach the real host: the session
    guard refuses the name lookup (the adapter reports it as unavailable)."""
    m = GeminiChatModel(SecretStr(KEY), "gemini-3.1-flash-lite")
    try:
        with pytest.raises(ChatModelError) as info:
            await generate(m)
        assert info.value.kind == "unavailable"
        assert gemini_attempts == [GEMINI_HOST]
    finally:
        gemini_attempts.clear()  # this test made the attempt on purpose
