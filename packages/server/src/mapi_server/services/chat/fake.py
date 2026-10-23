"""A deterministic chat model for tests, development and the live check (v0.20, plan I-5).

``MAPI_CHAT_PROVIDER=fake`` uses it (the settings refuse it in cloud deployments). It never
makes a network call and speaks the same provider-native turn shape as the Gemini adapter,
so the loop runs unchanged.

Two modes:

* **scripted** — :class:`FakeStep` items, one per :meth:`FakeChatModel.generate` call: a
  function call, a text, or both, with a token count (and optionally thought tokens, a
  finish reason or a model error);
* **default** (no script) — a small, fixed behaviour that uses the real tools: call
  ``get_latest_readout``; when the question asks what changed, find the readout before it
  (``list_readouts``) and call ``compare_readouts``; when the question names a channel of the
  readout, call ``get_channel``; then answer with numbers copied from the tool results.

Every call is recorded in :attr:`FakeChatModel.calls` (the system text and the turns the
model saw), so tests can assert what reached the model. ``on_generate`` is awaited before
call *n* (0-based): the hook the mid-turn revoke tests use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

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

__all__ = ["FAKE_MODEL_ID", "FakeChatModel", "FakeStep"]

FAKE_MODEL_ID: Final = "fake-chat"

_CHANGE_WORDS: Final = re.compile(r"\b(chang\w*|compar\w*|differ\w*|since)\b", re.IGNORECASE)


@dataclass
class FakeStep:
    """One scripted model call.

    ``call`` is a function call ``(name, args)``; ``text`` the answer text (a step may have
    both, like a real model). ``usage`` is the prompt plus answer tokens and ``thoughts`` the
    thinking tokens; the charged total is their sum. ``finish_reason`` defaults to ``STOP``.
    ``error`` makes the call fail with that :class:`ChatModelError` kind instead."""

    call: tuple[str, dict[str, Any]] | None = None
    text: str | None = None
    usage: int = 50
    thoughts: int = 0
    finish_reason: str = "STOP"
    error: ModelErrorKind | None = None


class FakeChatModel(ChatModel):
    """A model with a script, or the fixed default behaviour (see the module text)."""

    model_id = FAKE_MODEL_ID

    def __init__(
        self,
        script: Sequence[FakeStep] | None = None,
        *,
        on_generate: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self.script: list[FakeStep] | None = list(script) if script is not None else None
        self.on_generate = on_generate
        self.calls: list[tuple[str, Sequence[ModelTurn]]] = []
        self.tools_seen: list[tuple[str, ...]] = []

    def __repr__(self) -> str:
        mode = "default" if self.script is None else f"script[{len(self.script)}]"
        return f"FakeChatModel({mode})"

    async def generate(
        self,
        *,
        system: str,
        turns: Sequence[ModelTurn],
        tools: Sequence[FunctionDecl],
        timeout_s: float,
    ) -> ModelReply:
        n = len(self.calls)
        if self.on_generate is not None:
            await self.on_generate(n)
        self.calls.append((system, tuple(turns)))
        self.tools_seen.append(tuple(t.name for t in tools))
        if self.script is None:
            step = _default_step(turns, {t.name for t in tools})
        elif n < len(self.script):
            step = self.script[n]
        else:
            raise ChatModelError("bad_response")
        if step.error is not None:
            raise ChatModelError(step.error)
        return _reply(step, n)


def _reply(step: FakeStep, n: int) -> ModelReply:
    parts: list[dict[str, Any]] = []
    calls: list[FunctionCall] = []
    if step.text is not None:
        parts.append({"text": step.text})
    if step.call is not None:
        name, args = step.call
        call = FunctionCall(id=f"fake-call-{n}", name=name, args=dict(args))
        calls.append(call)
        parts.append({"functionCall": {"id": call.id, "name": name, "args": dict(args)}})
    usage = max(0, step.usage)
    thoughts = max(0, step.thoughts)
    output = usage // 5
    return ModelReply(
        turn=ModelTurn(role="model", parts=tuple(parts)),
        text=step.text or "",
        calls=tuple(calls),
        finish_reason=step.finish_reason,
        usage=ModelUsage(
            input=usage - output, output=output, thoughts=thoughts, cached=0, total=usage + thoughts
        ),
    )


# ================================================================================= default


def _question_and_results(
    turns: Sequence[ModelTurn],
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """The last user question and the function responses after it (by tool name)."""
    question = ""
    start = 0
    for i, turn in enumerate(turns):
        if turn.role != "user":
            continue
        texts = [p["text"] for p in turn.parts if isinstance(p.get("text"), str)]
        if texts and not any("functionResponse" in p for p in turn.parts):
            question = texts[-1]
            start = i + 1
    results: dict[str, list[dict[str, Any]]] = {}
    for turn in turns[start:]:
        for part in turn.parts:
            resp = part.get("functionResponse")
            if isinstance(resp, dict) and isinstance(resp.get("response"), dict):
                results.setdefault(str(resp.get("name")), []).append(resp["response"])
    return question, results


def _result(results: dict[str, list[dict[str, Any]]], name: str) -> dict[str, Any] | None:
    for response in reversed(results.get(name, [])):
        value = response.get("result")
        if isinstance(value, dict):
            return value
    return None


def _channel_in(question: str, readout: dict[str, Any]) -> str | None:
    snapshot = readout.get("snapshot")
    channels = snapshot.get("channels") if isinstance(snapshot, dict) else None
    if not isinstance(channels, list):
        return None
    q = question.lower()
    for ch in channels:
        if not isinstance(ch, dict) or not isinstance(ch.get("name"), str):
            continue
        words = {ch["name"].lower(), ch["name"].replace("_", " ").lower()}
        if isinstance(ch.get("label"), str) and ch["label"].strip():
            words.add(ch["label"].lower())
        if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", q) for w in words if w):
            return str(ch["name"])
    return None


def _earlier_seq(listing: dict[str, Any] | None, seq: int) -> int | None:
    items = listing.get("items") if listing else None
    if not isinstance(items, list):
        return None
    for item in items:  # newest first
        if (
            isinstance(item, dict)
            and isinstance(item.get("seq"), int)
            and item["seq"] < seq
            and item.get("status") == "ready"
        ):
            return int(item["seq"])
    return None


def _default_step(turns: Sequence[ModelTurn], tools: set[str]) -> FakeStep:
    question, results = _question_and_results(turns)
    if "get_latest_readout" in tools and "get_latest_readout" not in results:
        return FakeStep(call=("get_latest_readout", {}), usage=_tokens(turns))
    latest = _result(results, "get_latest_readout")
    if latest is None:
        return FakeStep(
            text="I could not read the latest readout. Look at the dashboard for its numbers.",
            usage=_tokens(turns),
        )
    seq = latest.get("seq") if isinstance(latest.get("seq"), int) else None
    if seq is not None and _CHANGE_WORDS.search(question):
        if "list_readouts" in tools and "list_readouts" not in results:
            return FakeStep(call=("list_readouts", {"limit": 10}), usage=_tokens(turns))
        earlier = _earlier_seq(_result(results, "list_readouts"), seq)
        if (
            earlier is not None
            and "compare_readouts" in tools
            and "compare_readouts" not in results
        ):
            return FakeStep(
                call=("compare_readouts", {"a_seq": earlier, "b_seq": seq}), usage=_tokens(turns)
            )
    channel = _channel_in(question, latest)
    if channel is not None and "get_channel" in tools and "get_channel" not in results:
        return FakeStep(call=("get_channel", {"name": channel}), usage=_tokens(turns))
    return FakeStep(text=_answer(latest, results), usage=_tokens(turns))


def _num(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _answer(latest: dict[str, Any], results: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = []
    seq = latest.get("seq")
    raw_snapshot = latest.get("snapshot")
    snapshot: dict[str, Any] = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    lines.append(f"Readout {seq} is the latest readout.")
    tiles = [t for t in snapshot.get("tiles", []) if isinstance(t, dict)]
    for tile in tiles[:3]:
        value = _num(tile.get("value"))
        if value is not None:
            lines.append(f"- {tile.get('label', tile.get('key'))}: {value}")
    comparison = _result(results, "compare_readouts")
    if comparison is not None:
        a = comparison.get("a", {}).get("seq") if isinstance(comparison.get("a"), dict) else None
        lines.append(f"Changes since readout {a}:")
        deltas = [d for d in comparison.get("tiles", []) if isinstance(d, dict)]
        for d in deltas[:3]:
            delta = _num(d.get("delta"))
            if delta is not None:
                lines.append(f"- {d.get('label', d.get('key'))}: {delta}")
    history = _result(results, "get_channel")
    if history is not None:
        points = [p for p in history.get("points", []) if isinstance(p, dict)]
        if points:
            last = points[-1]
            label = history.get("label", history.get("name"))
            for key, word in (
                ("spend", "spend"),
                ("contribution", "contribution"),
                ("roi", "return"),
            ):
                value = _num(last.get(key))
                if value is not None:
                    lines.append(f"- {label} {word} in readout {last.get('seq')}: {value}")
    errors = [
        name
        for name, responses in results.items()
        if any(isinstance(r.get("error"), dict) for r in responses)
    ]
    if errors:
        lines.append("Some of the dashboard data was not available to me.")
    lines.append("Check these numbers on the dashboard.")
    return "\n".join(lines)


def _tokens(turns: Sequence[ModelTurn]) -> int:
    """A deterministic token count: about four characters per token, at least 50."""
    size = sum(len(json.dumps(t.parts, sort_keys=True, default=str)) for t in turns)
    return max(50, size // 4)
