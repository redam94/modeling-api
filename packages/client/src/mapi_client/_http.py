"""Transport-independent request descriptions shared by the sync and async clients.

An endpoint call is described once as a :class:`Call` (method, path, query, JSON body,
headers, expected statuses and a response parser); :class:`~mapi_client.Client` and
:class:`~mapi_client.AsyncClient` only differ in how they send it.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import quote

import httpx
from mapi_spec.api import IDEMPOTENCY_KEY_HEADER, check_idempotency_key
from pydantic import BaseModel, TypeAdapter, ValidationError

from mapi_client.errors import is_retryable_status, unexpected

__all__ = [
    "Call",
    "RetryPolicy",
    "artifact_path",
    "dump",
    "idempotency_headers",
    "parse_json",
    "parse_model",
    "segment",
]

_IDEMPOTENT_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


@dataclass(frozen=True, slots=True)
class Call[T]:
    """One API request and how to read its response."""

    method: str
    path: str
    parse: Callable[[httpx.Response], T]
    expect: frozenset[int] = frozenset({200})
    params: Mapping[str, str | int] = field(default_factory=dict)
    json: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    retry_safe: bool = False
    """The operation is idempotent by definition (e.g. ``:cancel``) without a key."""
    server_wait: float | None = None
    """Seconds the server may hold the response (``Prefer: wait``); the read timeout of
    this request is extended by it, so a long-poll is not cut by the client's own timeout."""

    @property
    def idempotent(self) -> bool:
        """Safe to resend after a lost response (safe method, Idempotency-Key, retry_safe)."""
        return (
            self.retry_safe
            or self.method in _IDEMPOTENT_METHODS
            or IDEMPOTENCY_KEY_HEADER in self.headers
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter; honours ``Retry-After`` (capped)."""

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 30.0

    def delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            after = _retry_after(response)
            if after is not None:
                return min(after, self.max_delay)
        cap = min(self.max_delay, self.base_delay * (2**attempt))
        return random.uniform(0, cap)

    def should_retry(self, call: Call[Any], attempt: int, response: httpx.Response | None) -> bool:
        if attempt >= self.max_retries or not call.idempotent:
            return False
        return response is None or is_retryable_status(response.status_code)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


# ------------------------------------------------------------------------------- encoding


def segment(value: str | int) -> str:
    """Percent-encode one path segment (ids, versions, names without ``/``)."""
    text = str(value)
    if text in ("", ".", ".."):
        raise ValueError(f"invalid path segment {text!r}")
    return quote(text, safe="")


def artifact_path(name: str) -> str:
    """Encode an artifact name that may contain ``/`` (``ui/summaries.json``)."""
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid artifact name {name!r}")
    return "/".join(quote(p, safe="") for p in parts)


def dump(model: BaseModel) -> Any:
    """JSON-ready request body (aliases such as ``apiVersion``; ``None`` fields omitted)."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def idempotency_headers(key: str | None, *, generate: bool = True) -> dict[str, str]:
    """``Idempotency-Key`` header: the given key (validated) or a fresh UUIDv4."""
    if key is None:
        if not generate:
            return {}
        key = str(uuid.uuid4())
    return {IDEMPOTENCY_KEY_HEADER: check_idempotency_key(key)}


# ------------------------------------------------------------------------------- decoding


def parse_model[M: BaseModel](model: type[M]) -> Callable[[httpx.Response], M]:
    def parse(response: httpx.Response) -> M:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise unexpected(
                response,
                f"{response.request.method} {response.request.url.path} returned a body that "
                f"is not a valid {model.__name__} ({exc.error_count()} error(s))",
            ) from None

    return parse


def parse_adapter[T](adapter: TypeAdapter[T], name: str) -> Callable[[httpx.Response], T]:
    def parse(response: httpx.Response) -> T:
        try:
            return adapter.validate_json(response.content)
        except ValidationError as exc:
            raise unexpected(
                response,
                f"{response.request.method} {response.request.url.path} returned a body that "
                f"is not a valid {name} ({exc.error_count()} error(s))",
            ) from None

    return parse


def parse_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise unexpected(response, "the response body is not JSON") from None
    if not isinstance(data, dict):
        raise unexpected(response, "expected a JSON object")
    return data
