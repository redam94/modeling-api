"""The provider-neutral chat model interface (v0.20, plan I-5).

A :class:`ChatModel` takes a fixed system instruction, the turns so far and the function
declarations, and returns one :class:`ModelReply`: text, function calls, or both. The chat
loop (``loop.py``) runs the calls through :func:`mapi_server.services.dashboards.tools.call_tool`
and sends the results back as a :func:`function_response_turn`.

Turn parts are **provider-native JSON objects** (the Gemini ``Part`` shape: ``{"text": …}``,
``{"functionCall": {…}}``, ``{"functionResponse": {…}}``, with ``thought`` and
``thoughtSignature`` when the model sends them). A model turn is kept exactly as the model
returned it and sent back unchanged on the next call: Gemini refuses a function-calling turn
whose thought signatures were changed or dropped.

:func:`build_chat_model` makes the process's model from the settings, once at start-up; it
returns ``None`` when ``MAPI_CHAT_PROVIDER=off`` (the default).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from mapi_spec.dashboard_tools import tools_json_schema

if TYPE_CHECKING:
    import httpx

    from mapi_server.settings import Settings

__all__ = [
    "MODEL_ERROR_MESSAGES",
    "ChatModel",
    "ChatModelError",
    "FunctionCall",
    "FunctionDecl",
    "ModelErrorKind",
    "ModelReply",
    "ModelTurn",
    "ModelUsage",
    "build_chat_model",
    "dashboard_function_decls",
    "function_response_turn",
    "user_text_turn",
]


@dataclass(frozen=True, slots=True)
class FunctionDecl:
    """One function the model may call: its name, plain description and argument JSON
    Schema (sent unchanged; never the result schema)."""

    name: str
    description: str
    parameters_json_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """One function call of a model turn. ``id`` is the provider's call id when it sends
    one (the function response must then carry the same id)."""

    id: str | None
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """One turn of the conversation sent to the model.

    ``parts`` are provider-native JSON objects. A ``model`` turn is the model's content
    exactly as it was returned (thought signatures included); ``user`` turns carry text or
    function responses."""

    role: Literal["user", "model"]
    parts: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Token counts of one model call. ``total`` is what the provider charges (prompt, tool
    prompt, thoughts and answer); budgets are charged with it."""

    input: int
    output: int
    thoughts: int
    cached: int
    total: int


@dataclass(frozen=True, slots=True)
class ModelReply:
    """The result of one model call.

    ``turn`` is the model turn to append to the conversation unchanged; ``text`` the
    answer text (thought parts excluded); ``calls`` the function calls in order;
    ``finish_reason`` the provider's reason (``STOP``, ``MAX_TOKENS``, …)."""

    turn: ModelTurn
    text: str
    calls: tuple[FunctionCall, ...]
    finish_reason: str
    usage: ModelUsage


ModelErrorKind = Literal[
    "timeout", "rate_limited", "unavailable", "bad_response", "blocked", "auth"
]

MODEL_ERROR_MESSAGES: Final[dict[str, str]] = {
    "timeout": "The chat model did not answer in time.",
    "rate_limited": "The chat model refused the request because of its rate limit.",
    "unavailable": "The chat model is not available.",
    "bad_response": "The chat model sent a response that is not valid.",
    "blocked": "The chat model stopped the answer because of its safety rules.",
    "auth": "The chat model refused the server's credentials.",
}
"""Fixed text per error kind. A model error never carries provider text."""


class ChatModelError(Exception):
    """A model call failed. ``kind`` says how; ``status`` is the provider's HTTP status
    when there was one. ``usage`` is the token count the provider billed for the failed call
    (a blocked or unusable ``200`` answer carries one; a timed-out call gets an estimate from
    the loop) so the budgets are charged for it. The message is fixed per kind: provider
    bodies, prompts and keys are never part of it."""

    def __init__(
        self,
        kind: ModelErrorKind,
        *,
        status: int | None = None,
        usage: ModelUsage | None = None,
    ) -> None:
        super().__init__(MODEL_ERROR_MESSAGES[kind])
        self.kind: ModelErrorKind = kind
        self.status = status
        self.usage = usage

    def __repr__(self) -> str:
        return f"ChatModelError(kind={self.kind!r}, status={self.status!r})"


class ChatModel(Protocol):
    """A language model that can call functions."""

    model_id: str

    async def generate(
        self,
        *,
        system: str,
        turns: Sequence[ModelTurn],
        tools: Sequence[FunctionDecl],
        timeout_s: float,
    ) -> ModelReply:
        """One model call. Raises :class:`ChatModelError` on any failure."""
        ...


def user_text_turn(text: str) -> ModelTurn:
    """A user turn with one text part."""
    return ModelTurn(role="user", parts=({"text": text},))


def function_response_turn(results: Sequence[tuple[FunctionCall, dict[str, Any]]]) -> ModelTurn:
    """The user turn that answers a model turn's function calls, in call order.

    Each response is a JSON object (``{"result": …}`` or ``{"error": {"code", "message"}}``);
    it carries the call's ``id`` when the call had one."""
    parts: list[dict[str, Any]] = []
    for call, response in results:
        body: dict[str, Any] = {"name": call.name, "response": response}
        if call.id is not None:
            body = {"id": call.id, **body}
        parts.append({"functionResponse": body})
    return ModelTurn(role="user", parts=tuple(parts))


@cache
def dashboard_function_decls() -> tuple[FunctionDecl, ...]:
    """The 14 dashboard tools as function declarations, in :data:`DASHBOARD_TOOLS` order:
    name, plain description and the argument schema of :func:`tools_json_schema` unchanged
    (result schemas are never sent to the model)."""
    out: list[FunctionDecl] = []
    for entry in tools_json_schema():
        params = entry["parameters"]
        assert isinstance(params, dict)
        out.append(
            FunctionDecl(
                name=str(entry["name"]),
                description=str(entry["description"]),
                parameters_json_schema=dict(params),
            )
        )
    return tuple(out)


def build_chat_model(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> ChatModel | None:
    """The process's chat model: ``None`` when ``chat_provider`` is ``off``, the fake model
    for ``fake`` (the settings refuse it in cloud deployments), else the Gemini adapter.
    ``transport`` replaces the network (tests)."""
    if settings.chat_provider == "off":
        return None
    if settings.chat_provider == "fake":
        from mapi_server.services.chat.fake import FakeChatModel

        return FakeChatModel()
    from mapi_server.services.chat.gemini import GeminiChatModel

    key = settings.gemini_api_key
    if key is None:  # the settings refuse this; keep the check for direct construction
        raise RuntimeError("MAPI_CHAT_PROVIDER=gemini needs MAPI_GEMINI_API_KEY")
    return GeminiChatModel(
        key,
        settings.chat_model,
        endpoint=settings.gemini_endpoint,
        thinking_level=settings.chat_thinking_level,
        function_mode=settings.chat_function_mode,
        max_output_tokens=settings.chat_max_output_tokens,
        temperature=settings.chat_temperature,
        transport=transport,
    )
