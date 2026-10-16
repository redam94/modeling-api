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
from typing import Any, Final
from urllib.parse import quote

import httpx
from mapi_spec.api import IDEMPOTENCY_KEY_HEADER, check_idempotency_key
from mapi_spec.errors import ErrorCode
from mapi_spec.quotas import backoff_seconds
from pydantic import BaseModel, TypeAdapter, ValidationError

from mapi_client.errors import (
    is_retryable_status,
    retry_after_seconds,
    throttle_code,
    unexpected,
)

__all__ = [
    "Call",
    "RetryPolicy",
    "RetryState",
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
    params: Mapping[str, str | int | list[str]] = field(default_factory=dict)
    """Query parameters; a list value is sent as a repeated parameter (``?a=x&a=y``)."""
    json: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    retry_safe: bool = False
    """The operation is idempotent by definition (e.g. ``:cancel``) without a key."""
    server_wait: float | None = None
    """Seconds the server may hold the response (``Prefer: wait``); the read timeout of
    this request is extended by it, so a long-poll is not cut by the client's own timeout."""
    scoped: bool = True
    """The operation acts in the selected project (``X-Mapi-Project`` is sent). ``False``
    for platform, tenant and identity routes, and for routes whose path names the project
    (a header naming another project would be rejected)."""
    public: bool = False
    """A public route (sign-up, e-mail verification, embed views): sent **without** the
    client's credentials, which those routes neither need nor read."""

    @property
    def idempotent(self) -> bool:
        """Safe to resend after a lost response (safe method, Idempotency-Key, retry_safe)."""
        return (
            self.retry_safe
            or self.method in _IDEMPOTENT_METHODS
            or IDEMPOTENCY_KEY_HEADER in self.headers
        )


@dataclass(slots=True)
class RetryState:
    """Retries spent while sending one :class:`Call` (kept by the client per request)."""

    attempts: int = 0
    """Retries after transport errors and 502/503/504 (bounded by ``max_retries``)."""
    throttled: int = 0
    """Retries after ``429`` answers (bounded by ``rate_limit_retries``)."""
    waited: float = 0.0
    """Seconds slept so far for this request."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """When and how long to wait before resending a request.

    * Transport errors and ``502``/``503``/``504``: idempotent calls only (safe methods,
      ``Idempotency-Key``, or by definition), ``max_retries`` times, exponential backoff with
      full jitter, honouring ``Retry-After`` (capped at ``max_delay``).
    * ``429 RATE_LIMITED``: the server refused the request before acting on it
      (:mod:`mapi_spec.quotas` runs before any handler), so *every* method is resent, up to
      ``rate_limit_retries`` times, after ``max(Retry-After, base·2^n)`` plus a little jitter.
      A ``Retry-After`` longer than ``max_rate_limit_wait`` is not waited out: the error is
      raised with :func:`~mapi_client.errors.retry_after_of` set.
    * ``429 QUOTA_EXCEEDED``: raised at once, unless the quota resets within
      ``max_quota_wait`` seconds (default ``0``: never wait for a quota window).
    * A bare ``429`` without a problem document (a proxy) is treated like ``RATE_LIMITED``
      for idempotent calls only.
    """

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 30.0
    rate_limit_retries: int = 4
    max_rate_limit_wait: float = 120.0
    max_quota_wait: float = 0.0
    jitter: float = 0.1
    """Fraction of a throttle delay added as random jitter (at most one second)."""

    def __post_init__(self) -> None:
        for name in ("max_retries", "rate_limit_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("base_delay", "max_delay", "max_rate_limit_wait", "max_quota_wait"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        """Backoff before retry ``attempt`` (0-based) of a transport or gateway failure."""
        if response is not None:
            after = retry_after_seconds(response)
            if after is not None:
                return min(after, self.max_delay)
        cap = min(self.max_delay, self.base_delay * (2**attempt))
        return random.uniform(0, cap)

    def should_retry(self, call: Call[Any], attempt: int, response: httpx.Response | None) -> bool:
        """Retry a transport error (``response=None``) or a 429/502/503/504 answer?"""
        if attempt >= self.max_retries or not call.idempotent:
            return False
        return response is None or is_retryable_status(response.status_code)

    def plan(
        self, call: Call[Any], state: RetryState, response: httpx.Response | None
    ) -> float | None:
        """Seconds to wait before resending ``call``, or ``None`` to give up.

        ``response`` is ``None`` after a transport error. ``state`` is updated.
        """
        if response is not None and response.status_code == 429:
            delay = self._throttle_delay(call, state, response)
            if delay is not None:
                state.throttled += 1
        elif self.should_retry(call, state.attempts, response):
            delay = self.delay(state.attempts, response)
            state.attempts += 1
        else:
            delay = None
        if delay is not None:
            state.waited += delay
        return delay

    def _throttle_delay(
        self, call: Call[Any], state: RetryState, response: httpx.Response
    ) -> float | None:
        code = throttle_code(response)
        after = retry_after_seconds(response)
        if code is ErrorCode.QUOTA_EXCEEDED:
            # a quota window resets with the UTC day or month: wait only if told to
            give_up = after is None or after > self.max_quota_wait
        else:
            # a bare 429 (no problem document) may come from a proxy that already
            # forwarded the request: resend only what is safe to resend
            unsafe = code is None and not call.idempotent
            give_up = unsafe or (after is not None and after > self.max_rate_limit_wait)
        if give_up or state.throttled >= self.rate_limit_retries:
            return None
        base = backoff_seconds(
            state.throttled + 1, base=self.base_delay, cap=self.max_delay, retry_after=after
        )
        return base + random.uniform(0, min(1.0, self.jitter * base))


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
