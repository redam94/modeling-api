"""The Gemini ``generateContent`` adapter (v0.20, plan D2 and I-5).

One REST call per :meth:`GeminiChatModel.generate`:
``POST {endpoint}/v1beta/models/{model_id}:generateContent`` through ``httpx`` (no Google
SDK). The body has the fixed system instruction, the turns, the dashboard tools as
``functionDeclarations`` with ``parametersJsonSchema`` (the argument schemas unchanged), the
function-calling mode, the generation limits and ``store: false`` (the request is not kept
by the provider's request logging).

Secrets and untrusted text:

* the API key goes in the ``x-goog-api-key`` header only, never in the URL (the httpx
  instrumentation records URLs in spans);
* redirects are not followed, proxy variables are not read (``trust_env=False``);
* request and response bodies are never logged, and a :class:`ChatModelError` never
  carries provider text: only its kind and the HTTP status.

Error mapping (``docs/api-errors``, plan probe (c)): 401, 403 and a 400 whose reason is
``API_KEY_INVALID`` → ``auth``; 429 →
``rate_limited``; 408, 504 and client timeouts → ``timeout``; 402, 404, 400
``failed_precondition``, other 5xx, connection errors and redirects → ``unavailable``; any
other 4xx and a body that is not a valid response → ``bad_response``. A candidate stopped by
a safety rule → ``blocked``; a malformed function call or a missing thought signature →
``bad_response`` (both carry the billed ``usageMetadata`` as :attr:`ChatModelError.usage`); a
candidate with no parts and a normal finish reason is an empty answer. ``MAX_TOKENS`` and
``TOO_MANY_TOOL_CALLS`` are returned as the ``finish_reason`` for the loop to handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Sequence
from typing import Any, Final, Literal

import httpx
from pydantic import SecretStr

from mapi_server.services.chat.model import (
    ChatModel,
    ChatModelError,
    FunctionCall,
    FunctionDecl,
    ModelErrorKind,
    ModelReply,
    ModelTurn,
    ModelUsage,
)

__all__ = [
    "BLOCKED_FINISH_REASONS",
    "GEMINI_API_VERSION",
    "GEMINI_ENDPOINT",
    "GEMINI_HOST",
    "INVALID_FINISH_REASONS",
    "MAX_RESPONSE_BYTES",
    "MODEL_ID_RE",
    "FunctionMode",
    "GeminiChatModel",
    "ThinkingLevel",
]

log = logging.getLogger(__name__)

GEMINI_HOST: Final = "generativelanguage.googleapis.com"
GEMINI_ENDPOINT: Final = f"https://{GEMINI_HOST}"
GEMINI_API_VERSION: Final = "v1beta"
MODEL_ID_RE: Final = r"^[a-z0-9][a-z0-9.\-]{1,62}$"
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
"""Largest response body read (the answer is capped at ``maxOutputTokens``)."""

ThinkingLevel = Literal["omit", "minimal", "low"]
FunctionMode = Literal["VALIDATED", "AUTO"]

BLOCKED_FINISH_REASONS: Final = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "ESCALATION",
    }
)
"""Finish reasons of a candidate that a provider rule stopped."""

INVALID_FINISH_REASONS: Final = frozenset(
    {
        "MALFORMED_FUNCTION_CALL",
        "MISSING_THOUGHT_SIGNATURE",
        "MALFORMED_RESPONSE",
        "UNEXPECTED_TOOL_CALL",
    }
)
"""Finish reasons of a candidate that cannot be used."""

_HTTP_KIND: Final[dict[int, ModelErrorKind]] = {
    401: "auth",
    403: "auth",
    402: "unavailable",
    404: "unavailable",
    408: "timeout",
    429: "rate_limited",
    504: "timeout",
}


class GeminiChatModel(ChatModel):
    """The Gemini Developer API through REST ``generateContent``."""

    def __init__(
        self,
        api_key: SecretStr,
        model_id: str,
        *,
        endpoint: str = GEMINI_ENDPOINT,
        thinking_level: ThinkingLevel = "omit",
        function_mode: FunctionMode = "VALIDATED",
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not re.fullmatch(MODEL_ID_RE, model_id):
            raise ValueError("the chat model id is not valid")
        if not api_key.get_secret_value():
            raise ValueError("the Gemini API key is empty")
        self.model_id = model_id
        self._key = api_key
        self._url = f"{endpoint.rstrip('/')}/{GEMINI_API_VERSION}/models/{model_id}:generateContent"
        self._thinking_level: ThinkingLevel = thinking_level
        self._function_mode: FunctionMode = function_mode
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._transport = transport

    def __repr__(self) -> str:
        return f"GeminiChatModel(model_id={self.model_id!r})"

    # ----------------------------------------------------------------------------- request
    def request_body(
        self, *, system: str, turns: Sequence[ModelTurn], tools: Sequence[FunctionDecl]
    ) -> dict[str, Any]:
        """The ``generateContent`` request body (no secret in it)."""
        generation: dict[str, Any] = {
            "maxOutputTokens": self._max_output_tokens,
            "temperature": self._temperature,
        }
        if self._thinking_level != "omit":
            generation["thinkingConfig"] = {"thinkingLevel": self._thinking_level}
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": t.role, "parts": list(t.parts)} for t in turns],
        }
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parametersJsonSchema": t.parameters_json_schema,
                        }
                        for t in tools
                    ]
                }
            ]
            calling: dict[str, Any] = {"mode": self._function_mode}
            if self._function_mode == "VALIDATED":
                # only these names, even if the declarations were ever changed upstream
                calling["allowedFunctionNames"] = [t.name for t in tools]
            body["toolConfig"] = {"functionCallingConfig": calling}
        body["generationConfig"] = generation
        body["store"] = False
        return body

    async def generate(
        self,
        *,
        system: str,
        turns: Sequence[ModelTurn],
        tools: Sequence[FunctionDecl],
        timeout_s: float,
    ) -> ModelReply:
        content = json.dumps(
            self.request_body(system=system, turns=turns, tools=tools),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        headers = {
            "x-goog-api-key": self._key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with (
                asyncio.timeout(timeout_s + 1.0),
                httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout_s),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream("POST", self._url, content=content, headers=headers) as response,
            ):
                status = response.status_code
                raw = await _read_capped(response)
        except TimeoutError:
            raise self._fail("timeout") from None
        except httpx.TimeoutException:
            raise self._fail("timeout") from None
        except _TooLarge:
            raise self._fail("bad_response") from None
        except httpx.HTTPError:
            raise self._fail("unavailable") from None
        if status != 200:
            raise self._fail(_status_kind(status, raw), status=status)
        return self._parse(raw)

    # ---------------------------------------------------------------------------- response
    def _fail(self, kind: ModelErrorKind, *, status: int | None = None) -> ChatModelError:
        # status and kind only: never the body, the prompt or the key
        log.warning(
            "chat model call failed: model=%s kind=%s status=%s", self.model_id, kind, status
        )
        return ChatModelError(kind, status=status)

    def _parse(self, raw: bytes) -> ModelReply:
        try:
            doc = json.loads(raw)
        except ValueError:
            raise self._fail("bad_response") from None
        if not isinstance(doc, dict):
            raise self._fail("bad_response")
        usage = _usage(doc.get("usageMetadata"))
        try:
            return self._parse_doc(doc, usage)
        except ChatModelError as exc:
            # a blocked or unusable 200 answer is still billed: carry its usage to the budgets
            if exc.usage is None and usage.total > 0:
                exc.usage = usage
            raise

    def _parse_doc(self, doc: dict[str, Any], usage: ModelUsage) -> ModelReply:
        candidates = doc.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            feedback = doc.get("promptFeedback")
            if isinstance(feedback, dict) and feedback.get("blockReason"):
                raise self._fail("blocked")
            raise self._fail("bad_response")
        cand = candidates[0]
        if not isinstance(cand, dict):
            raise self._fail("bad_response")
        finish = cand.get("finishReason")
        finish_reason = finish if isinstance(finish, str) else "FINISH_REASON_UNSPECIFIED"
        if finish_reason in BLOCKED_FINISH_REASONS:
            raise self._fail("blocked")
        if finish_reason in INVALID_FINISH_REASONS:
            raise self._fail("bad_response")
        content = cand.get("content")
        if content is None:
            content = {"role": "model", "parts": []}
        if not isinstance(content, dict):
            raise self._fail("bad_response")
        parts = content.get("parts", [])
        if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
            raise self._fail("bad_response")
        texts: list[str] = []
        calls: list[FunctionCall] = []
        for part in parts:
            if "functionCall" in part:
                calls.append(self._call(part["functionCall"]))
            elif isinstance(part.get("text"), str) and part.get("thought") is not True:
                texts.append(part["text"])
        # a candidate with no parts and a normal finish (STOP after function results, for
        # example) is an empty answer, not a broken body: the loop answers EMPTY_REPLY
        return ModelReply(
            turn=ModelTurn(role="model", parts=tuple(parts)),
            text="".join(texts),
            calls=tuple(calls),
            finish_reason=finish_reason,
            usage=usage,
        )

    def _call(self, value: object) -> FunctionCall:
        if not isinstance(value, dict):
            raise self._fail("bad_response")
        name = value.get("name")
        args = value.get("args", {})
        call_id = value.get("id")
        if args is None:
            args = {}
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(args, dict)
            or not (call_id is None or isinstance(call_id, str))
        ):
            raise self._fail("bad_response")
        return FunctionCall(id=call_id, name=name, args=args)


class _TooLarge(Exception):
    pass


async def _read_capped(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _status_kind(status: int, raw: bytes) -> ModelErrorKind:
    kind = _HTTP_KIND.get(status)
    if kind is not None:
        return kind
    if 300 <= status < 400 or status >= 500:
        return "unavailable"
    if status == 400:
        codes = _error_codes(raw)
        if "api_key_invalid" in codes or "authentication" in codes:
            # older surfaces answer a bad key with 400 INVALID_ARGUMENT, reason API_KEY_INVALID
            return "auth"
        if "failed_precondition" in codes:
            # for example disabled billing: an operator problem, not a bad request
            return "unavailable"
    if 400 <= status < 500:
        return "bad_response"
    return "unavailable"


def _error_codes(raw: bytes) -> set[str]:
    """The machine-readable codes of an error body, lower case: ``error.code`` when it is a
    string, ``error.status`` (older surfaces send an integer code with a status name) and
    each ``error.details[].reason``. Never raises; the body is never logged."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return set()
    err = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(err, dict):
        return set()
    out: set[str] = set()
    for value in (err.get("code"), err.get("status")):
        if isinstance(value, str):
            out.add(value.lower())
    details = err.get("details")
    if isinstance(details, list):
        for item in details:
            reason = item.get("reason") if isinstance(item, dict) else None
            if isinstance(reason, str):
                out.add(reason.lower())
    return out


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(0, int(value))


def _usage(meta: object) -> ModelUsage:
    m = meta if isinstance(meta, dict) else {}
    prompt = _count(m.get("promptTokenCount"))
    tool_prompt = _count(m.get("toolUsePromptTokenCount"))
    output = _count(m.get("candidatesTokenCount"))
    thoughts = _count(m.get("thoughtsTokenCount"))
    cached = _count(m.get("cachedContentTokenCount"))
    total = _count(m.get("totalTokenCount"))
    # the total is what is charged; never trust a total below the sum of its parts
    total = max(total, prompt + tool_prompt + output + thoughts)
    return ModelUsage(
        input=prompt + tool_prompt, output=output, thoughts=thoughts, cached=cached, total=total
    )
