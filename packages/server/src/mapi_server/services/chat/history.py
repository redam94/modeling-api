"""Signed chat history (v0.20, plan D5).

The client holds the conversation as **text only** and sends it back with each question. The
server is stateless (transcripts are off by default), so it must not trust the assistant's
words the client sends: each assistant turn it returns carries

    sig = base64url(HMAC-SHA256(k_chat,
        principal_kind | principal_id | dashboard_id | conversation_id | index | sha256(text)))

without padding (43 characters), with ``k_chat = HKDF-SHA256(server keys, "mapi.chat.v1")``.
:func:`verify_history` checks every assistant turn of a request: a forged, edited, re-ordered
or replayed turn (another caller, dashboard or conversation) answers ``400
CHAT_HISTORY_INVALID``. User turns are the caller's own words and are not signed.

A ``conversation_id`` is accepted only together with at least one signed assistant turn
(:func:`proven_conversation`); otherwise the server makes a new one, so a client cannot attach
its first question to someone else's conversation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING, Final, Protocol

from mapi_spec.chat import ChatRequest
from mapi_spec.errors import ErrorCode, MapiError

if TYPE_CHECKING:
    from mapi_server.services.secrets import ServerSecrets

__all__ = [
    "CHAT_KEY_INFO",
    "SignedParty",
    "chat_key",
    "history_invalid",
    "proven_conversation",
    "sign_turn",
    "verify_history",
]

CHAT_KEY_INFO: Final = b"mapi.chat.v1"
_SALT: Final = b"mapi/chat-history"


class SignedParty(Protocol):
    """Who a signature is bound to (a :class:`~.principal.ChatPrincipal`)."""

    @property
    def kind(self) -> str: ...

    @property
    def id(self) -> str: ...


def _hkdf_sha256(ikm: bytes, *, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF with SHA-256 (extract, then expand)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def chat_key(secrets: ServerSecrets) -> bytes:
    """The history-signing key ``k_chat``: HKDF of the server's root-derived keys with the
    purpose ``mapi.chat.v1``. Every API instance derives the same key from the shared
    server secret; rotating the secret invalidates every signed history."""
    return _hkdf_sha256(secrets.token_pepper, salt=_SALT, info=CHAT_KEY_INFO)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def sign_turn(
    key: bytes,
    p: SignedParty,
    dashboard_id: str,
    conversation_id: str,
    index: int,
    text: str,
) -> str:
    """The signature of the assistant turn ``text`` at position ``index`` of the
    conversation (43 base64url characters)."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    message = "|".join((p.kind, p.id, dashboard_id, conversation_id, str(int(index)), digest))
    return _b64(hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest())


def history_invalid() -> MapiError:
    return MapiError(
        "the conversation history is not valid: start a new conversation",
        code=ErrorCode.CHAT_HISTORY_INVALID,
    )


def proven_conversation(req: ChatRequest) -> bool:
    """Whether the request continues a conversation (it holds an assistant turn)."""
    return any(t.role == "assistant" for t in req.messages)


def verify_history(key: bytes, p: SignedParty, dashboard_id: str, req: ChatRequest) -> None:
    """Check every assistant turn of ``req`` against its signature for this caller,
    dashboard and conversation: ``400 CHAT_HISTORY_INVALID`` when one does not match, or
    when assistant turns come without a ``conversation_id``. (The request model already
    refused turn shapes other than alternating user/assistant text.)"""
    if not proven_conversation(req):
        return
    if req.conversation_id is None:
        raise history_invalid()
    for index, turn in enumerate(req.messages):
        if turn.role != "assistant":
            continue
        if turn.sig is None:
            raise history_invalid()
        want = sign_turn(key, p, dashboard_id, req.conversation_id, index, turn.text)
        if not hmac.compare_digest(want.encode(), turn.sig.encode()):
            raise history_invalid()
