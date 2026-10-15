"""Bases and constrained types shared by every REST DTO module.

* :class:`RequestModel` bodies reject unknown fields (``SCHEMA_INVALID``);
* :class:`ResponseModel` bodies are frozen and *ignore* unknown fields when a client parses
  them, so a newer server can add fields without breaking older clients.

Every DTO carries ``json_schema_extra`` examples (see :func:`dto_examples`); the test suite
validates and round-trips each one, and the OpenAPI document publishes them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from mapi_spec._types import StrictModel
from mapi_spec.storage import SEGMENT_RE

__all__ = [
    "EXAMPLE_TS",
    "TENANT_ID_RE",
    "Id",
    "RequestModel",
    "ResponseModel",
    "Sha256",
    "TenantId",
    "dto_examples",
    "keyed_digest",
    "new_id",
]

TENANT_ID_RE: Final = r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$"
"""Tenant and project ids are slugs (they appear in refs such as ``dataset://growth/x``)."""

Id = Annotated[str, Field(pattern=SEGMENT_RE, examples=["run_01jaf3q2x8m9v7k5h4g3f2e1d0"])]
TenantId = Annotated[str, Field(pattern=TENANT_ID_RE)]
Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

EXAMPLE_TS: Final = "2026-10-15T09:30:00Z"


class RequestModel(StrictModel):
    """Request bodies: unknown fields are rejected, instances are immutable."""


class ResponseModel(BaseModel):
    """Response bodies: frozen; unknown fields are ignored by (older) clients."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


def dto_examples(*items: dict[str, Any]) -> ConfigDict:
    """``model_config`` fragment publishing ``items`` as JSON Schema ``examples``."""
    return ConfigDict(json_schema_extra={"examples": list(items)})


def keyed_digest(value: str, key: bytes, *, length: int = 32) -> str:
    """``hmac-sha256:<hex>`` of ``value`` under ``key``, truncated to ``length`` hex digits.

    Used wherever a value must be comparable but not recoverable (client IPs and user agents
    in audit events, token secrets at rest). A *keyed* digest matters for low-entropy inputs:
    a plain SHA-256 of an IPv4 address is reversed by enumerating 2^32 candidates.
    """
    if not 16 <= length <= 64 or length % 2:
        raise ValueError("length must be an even number of hex digits between 16 and 64")
    mac = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{mac[:length]}"


_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def new_id(prefix: str) -> str:
    """A time-sortable id ``<prefix>_<26 Crockford base32 chars>`` (ULID layout, lowercase).

    The first 10 characters encode the millisecond timestamp, so ids sort by creation time
    (cursor pagination relies on it); the last 16 are 80 random bits.
    """
    if not re.match(r"^[a-z]{2,8}$", prefix):
        raise ValueError("id prefixes are 2-8 lowercase letters")
    value = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"
