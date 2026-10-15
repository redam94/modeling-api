"""RFC 7807 error responses (``application/problem+json``) with stable codes.

Every error leaving the API is a :class:`mapi_spec.errors.Problem`. Request validation
errors become 422 ``SCHEMA_INVALID`` with one issue per problem at an RFC 6901 pointer into
the request (``/body/kind``). Unexpected exceptions become 500 ``INTERNAL_ERROR``; their
traceback is logged and, only when ``MAPI_DEBUG`` is on, returned in ``untrusted_text`` —
never in ``detail``.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mapi_spec.api import loc_to_pointer
from mapi_spec.errors import (
    PROBLEM_CONTENT_TYPE,
    ErrorCode,
    Issue,
    MapiError,
    Problem,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "Conflict",
    "NotFoundError",
    "install_error_handlers",
    "problem_response",
]

log = logging.getLogger(__name__)


class NotFoundError(MapiError):
    default_code = ErrorCode.NOT_FOUND

    def __init__(self, what: str, ident: object) -> None:
        super().__init__(f"{what} {ident!s} not found")


class Conflict(MapiError):
    default_code = ErrorCode.CONFLICT


_HTTP_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.SCHEMA_INVALID,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    412: ErrorCode.PRECONDITION_FAILED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.SCHEMA_INVALID,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.INFRA_ERROR,
}


def problem_response(
    problem: Problem, *, status: int | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(mode="json", exclude_none=True),
        status_code=status or problem.status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def _validation_issues(errors: list[dict[str, Any]]) -> list[Issue]:
    issues: list[Issue] = []
    for err in errors[:100]:
        loc = tuple(err.get("loc") or ())
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        issues.append(
            Issue(code=ErrorCode.SCHEMA_INVALID, pointer=loc_to_pointer(loc), message=msg[:2000])
        )
    return issues


def install_error_handlers(app: FastAPI, *, debug: bool = False) -> None:
    @app.exception_handler(MapiError)
    async def _mapi(request: Request, exc: MapiError) -> JSONResponse:
        if exc.status >= 500:
            log.error("request failed: %s %s: %r", request.method, request.url.path, exc)
        return problem_response(exc.to_problem(instance=request.url.path))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = _validation_issues(list(exc.errors()))
        n = len(issues)
        detail = f"the request is invalid ({n} problem{'s' if n != 1 else ''})"
        if issues:
            detail += f"; first at {issues[0].pointer or '/'}: {issues[0].message}"
        problem = Problem.from_code(
            ErrorCode.SCHEMA_INVALID,
            detail[:2000],
            errors=issues,
            instance=request.url.path,
        )
        return problem_response(problem)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        detail = exc.detail if isinstance(exc.detail, str) else None
        problem = Problem.from_code(code, detail, instance=request.url.path)
        headers = dict(exc.headers) if exc.headers else None
        return problem_response(problem, status=exc.status_code, headers=headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        tb = "".join(traceback.format_exception(exc))[-60000:] if debug else None
        problem = Problem.from_code(
            ErrorCode.INTERNAL_ERROR,
            "an unexpected error occurred",
            untrusted_text=tb,
            instance=request.url.path,
        )
        return problem_response(problem)
