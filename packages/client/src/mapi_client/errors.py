"""HTTP errors as :class:`mapi_spec.errors.MapiError` subclasses.

Every non-success response is turned into the most specific ``MapiError`` subclass for its
stable code (``CoordPlanError`` for ``UNSEEN_LEVELS``, ``QuotaExceeded`` ...), rehydrated
from the RFC 7807 :class:`~mapi_spec.errors.Problem` body with
:func:`mapi_spec.errors.error_from_problem`. The parsed problem and the HTTP status are
available through :func:`problem_of` and :func:`http_status_of`.

Responses without a valid problem body (a proxy's HTML 502, a newer server's unknown code)
still map to a code chosen from the HTTP status; the raw body goes to ``untrusted_text``.

``429`` answers carry one of two codes (plan §8.8, :mod:`mapi_spec.quotas`):

* ``RATE_LIMITED`` (:class:`RateLimited`): too many requests per minute; the client waits
  for ``Retry-After`` and resends (see :class:`~mapi_client.RetryPolicy`);
* ``QUOTA_EXCEEDED`` (:class:`~mapi_spec.errors.QuotaExceeded`): a compute, storage or
  concurrency quota; raised at once, with :func:`retry_after_of` telling when the window
  resets (``None`` when only finishing other runs frees capacity).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, NoReturn

import httpx
from mapi_spec.errors import (
    ErrorCode,
    InfraError,
    Issue,
    MapiError,
    Problem,
    error_for_code,
    error_from_problem,
)

__all__ = [
    "RateLimited",
    "TransportError",
    "UnexpectedResponse",
    "error_from_response",
    "http_status_of",
    "is_retryable_status",
    "problem_of",
    "raise_for_response",
    "retry_after_of",
    "retry_after_seconds",
    "throttle_code",
]

_STATUS_CODES: Final[dict[int, ErrorCode]] = {
    400: ErrorCode.SCHEMA_INVALID,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    412: ErrorCode.PRECONDITION_FAILED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.SCHEMA_INVALID,
    429: ErrorCode.RATE_LIMITED,
    502: ErrorCode.INFRA_ERROR,
    503: ErrorCode.INFRA_ERROR,
    504: ErrorCode.INFRA_ERROR,
}
_RETRYABLE_STATUSES: Final = frozenset({429, 502, 503, 504})
_BODY_PREVIEW: Final = 2000


class TransportError(InfraError):
    """The request never produced an HTTP response (connection refused, timeout, TLS ...)."""


class RateLimited(MapiError):
    """``429 RATE_LIMITED``: the request rate of a token, tenant or the platform is exceeded.

    Raised only after the client's own back-off gave up; :func:`retry_after_of` is the
    server's ``Retry-After`` in seconds.
    """

    default_code = ErrorCode.RATE_LIMITED


class UnexpectedResponse(MapiError):
    """A response the client cannot interpret (wrong status, undecodable body)."""

    default_code = ErrorCode.INTERNAL_ERROR


def is_retryable_status(status: int) -> bool:
    """Statuses that are safe to retry for idempotent requests."""
    return status in _RETRYABLE_STATUSES


def problem_of(exc: BaseException) -> Problem | None:
    """The RFC 7807 problem a client error was built from (``None`` if there was none)."""
    value = getattr(exc, "problem", None)
    return value if isinstance(value, Problem) else None


def http_status_of(exc: BaseException) -> int | None:
    """The HTTP status of the response a client error was built from."""
    value = getattr(exc, "http_status", None)
    return value if isinstance(value, int) else None


def retry_after_of(exc: BaseException) -> float | None:
    """Seconds the server asked to wait (``Retry-After``) before retrying, if it said."""
    value = getattr(exc, "retry_after", None)
    return float(value) if isinstance(value, int | float) else None


def retry_after_seconds(response: httpx.Response, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds from now (delta-seconds or an HTTP date); ``None`` if absent
    or unparsable."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def throttle_code(response: httpx.Response) -> ErrorCode | None:
    """``RATE_LIMITED`` or ``QUOTA_EXCEEDED`` for a ``429`` with a problem body, else ``None``
    (a proxy's bare 429, or any other status)."""
    if response.status_code != 429:
        return None
    problem = _parse_problem(response.content)
    if problem is None or problem.code not in (ErrorCode.RATE_LIMITED, ErrorCode.QUOTA_EXCEEDED):
        return None
    return problem.code


def _code_for_status(status: int) -> ErrorCode:
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    return ErrorCode.INTERNAL_ERROR if status >= 500 else ErrorCode.SCHEMA_INVALID


def _parse_problem(body: bytes) -> Problem | None:
    try:
        data: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or "code" not in data:
        return None
    # tolerate fields added by newer servers (Problem itself forbids unknown fields)
    known = {k: v for k, v in data.items() if k in Problem.model_fields}
    try:
        return Problem.model_validate(known)
    except ValueError:
        return None


def _preview(body: bytes) -> str | None:
    text = body[:_BODY_PREVIEW].decode("utf-8", errors="replace").strip()
    return text or None


def _attach(
    err: MapiError, status: int, problem: Problem | None, retry_after: float | None = None
) -> MapiError:
    # MapiError has no slots: typed accessors are problem_of()/http_status_of()/retry_after_of()
    err.http_status = status  # type: ignore[attr-defined]
    err.problem = problem  # type: ignore[attr-defined]
    err.retry_after = retry_after  # type: ignore[attr-defined]
    return err


def _rate_limited(detail: str, *, untrusted_text: str | None, issues: list[Issue]) -> MapiError:
    pointer = issues[0].pointer if issues else ""
    return RateLimited(detail, untrusted_text=untrusted_text, issues=issues, pointer=pointer)


def error_from_response(response: httpx.Response) -> MapiError:
    """Build the ``MapiError`` for an error response (its body must have been read)."""
    status = response.status_code
    body = response.content
    problem = _parse_problem(body)
    request = response.request
    where = f"{request.method} {request.url.path}"
    retry_after = retry_after_seconds(response) if status in (429, 503) else None
    if problem is not None:
        if problem.code is ErrorCode.RATE_LIMITED:
            err = _rate_limited(
                problem.detail or problem.title,
                untrusted_text=problem.untrusted_text,
                issues=list(problem.errors),
            )
        else:
            err = error_from_problem(problem)
        return _attach(err, status, problem, retry_after)
    code = _code_for_status(status)
    detail = f"HTTP {status} from {where} (no problem document)"
    if code is ErrorCode.RATE_LIMITED:
        err = _rate_limited(detail, untrusted_text=_preview(body), issues=[])
    else:
        err = error_for_code(code, detail, untrusted_text=_preview(body))
    return _attach(err, status, None, retry_after)


def raise_for_response(response: httpx.Response) -> NoReturn:
    raise error_from_response(response)


def unexpected(response: httpx.Response, detail: str) -> UnexpectedResponse:
    err = UnexpectedResponse(detail, untrusted_text=_preview(response.content))
    _attach(err, response.status_code, None)
    return err
