"""Webhook signing secrets at rest: AES-256-GCM under a key derived from the server secret.

The platform must *use* a webhook's secret on every delivery (HMAC signing), so it cannot be
stored as a one-way hash like API tokens. It is sealed instead: AES-GCM with a fresh 96-bit
nonce, the webhook id as associated data (a sealed secret copied onto another webhook's row
does not open), and a ``v1.`` version prefix for future key rotation. A database dump alone
therefore neither reveals secrets nor lets anyone forge deliveries.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["SEAL_VERSION", "SealError", "SecretBox", "secret_hint"]

SEAL_VERSION: Final = "v1"
_NONCE_BYTES: Final = 12


class SealError(ValueError):
    """A sealed value does not open (wrong key, wrong row, or tampered)."""


class SecretBox:
    """Seal and open short secrets bound to an owner id."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("the sealing key must be 32 bytes")
        self._aead = AESGCM(key)

    def seal(self, secret: str, *, owner: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ct = self._aead.encrypt(nonce, secret.encode("utf-8"), owner.encode("utf-8"))
        blob = base64.urlsafe_b64encode(nonce + ct).decode("ascii").rstrip("=")
        return f"{SEAL_VERSION}.{blob}"

    def open(self, sealed: str, *, owner: str) -> str:
        version, sep, blob = sealed.partition(".")
        if not sep or version != SEAL_VERSION:
            raise SealError("unknown sealed-secret format")
        try:
            raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        except (binascii.Error, ValueError):
            raise SealError("malformed sealed secret") from None
        if len(raw) <= _NONCE_BYTES:
            raise SealError("malformed sealed secret")
        try:
            plain = self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], owner.encode())
        except InvalidTag:
            raise SealError("the sealed secret does not open with this key and owner") from None
        return plain.decode("utf-8")

    def __repr__(self) -> str:
        return "SecretBox(<redacted>)"


def secret_hint(secret: str) -> str:
    """``whsec_…abcd``: the last four characters, for recognising a secret in the UI."""
    return f"whsec_…{secret[-4:]}"
