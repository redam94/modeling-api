"""HTTP errors as :class:`mapi_spec.errors.MapiError` subclasses.

Every non-success response is turned into the most specific ``MapiError`` subclass for its
stable code (``CoordPlanError`` for ``UNSEEN_LEVELS``, ``QuotaExceeded`` ...), rehydrated
from the RFC 7807 :class:`~mapi_spec.errors.Problem` body with
:func:`mapi_spec.errors.error_from_problem`. The parsed problem and the HTTP status are
available through :func:`problem_of` and :func:`http_status_of`.

Responses without a valid problem body (a proxy's HTML 502, a newer server's unknown code)
still map to a code chosen from the HTTP status; the raw body goes to ``untrusted_text``.
"""

from __future__ import annotations

import json
from typing import Any, Final, NoReturn

import httpx
from mapi_spec.errors import (
    ErrorCode,
    InfraError,
    MapiError,
    Problem,
    error_for_code,
    error_from_problem,
)

__all__ = [
    "TransportError",
    "UnexpectedResponse",
    "error_from_response",
    "http_status_of",
    "is_retryable_status",
    "problem_of",
    "raise_for_response",
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


def _attach(err: MapiError, status: int, problem: Problem | None) -> MapiError:
    # MapiError has no slots: typed accessors are problem_of()/http_status_of()
    err.http_status = status  # type: ignore[attr-defined]
    err.problem = problem  # type: ignore[attr-defined]
    return err


def error_from_response(response: httpx.Response) -> MapiError:
    """Build the ``MapiError`` for an error response (its body must have been read)."""
    status = response.status_code
    body = response.content
    problem = _parse_problem(body)
    request = response.request
    where = f"{request.method} {request.url.path}"
    if problem is not None:
        return _attach(error_from_problem(problem), status, problem)
    code = _code_for_status(status)
    err = error_for_code(
        code,
        f"HTTP {status} from {where} (no problem document)",
        untrusted_text=_preview(body),
    )
    return _attach(err, status, None)


def raise_for_response(response: httpx.Response) -> NoReturn:
    raise error_from_response(response)


def unexpected(response: httpx.Response, detail: str) -> UnexpectedResponse:
    err = UnexpectedResponse(detail, untrusted_text=_preview(response.content))
    _attach(err, response.status_code, None)
    return err
