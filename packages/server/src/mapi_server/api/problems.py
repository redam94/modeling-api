"""OpenAPI documentation of RFC 7807 problems, and request-body limits for large JSON.

:func:`problem_responses` turns the stable error codes an operation can answer (its row of
:data:`mapi_spec.api.ALL_ENDPOINTS`, plus the codes every authenticated route can answer)
into OpenAPI ``responses``: one entry per HTTP status, ``application/problem+json`` with the
:class:`~mapi_spec.errors.Problem` schema and one example per code. Generated clients can
then type every failure.

:class:`JsonBodyLimit` guards routes that accept large inline JSON (endpoint score calls
with XrJSON data, up to 5 MB): it refuses bodies over the cap with ``413
PAYLOAD_TOO_LARGE`` before anything is parsed, and deeply nested documents with ``422
SCHEMA_INVALID`` (instead of a recursion error deep in a parser).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any, Final

from mapi_spec.api import ALL_ENDPOINTS
from mapi_spec.errors import ERROR_INFO, PROBLEM_CONTENT_TYPE, ErrorCode, Problem
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mapi_server.jsonlimits import JsonTooDeep, loads_limited

__all__ = ["COMMON_CODES", "JsonBodyLimit", "problem_responses", "responses_for"]

COMMON_CODES: Final = (ErrorCode.UNAUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.RATE_LIMITED)
"""What every authenticated route can answer (credentials, scopes and roles, rate limits)."""

_BY_OP: Final = {e.operation_id: e for e in ALL_ENDPOINTS}


def problem_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` for ``codes`` (grouped by status, one example per code)."""
    by_status: dict[int, list[ErrorCode]] = {}
    for code in dict.fromkeys(codes):
        by_status.setdefault(ERROR_INFO[code].status, []).append(code)
    out: dict[int | str, dict[str, Any]] = {}
    for status, group in sorted(by_status.items()):
        examples = {
            code.value: {
                "summary": ERROR_INFO[code].title,
                "value": Problem.from_code(code, ERROR_INFO[code].title).model_dump(
                    mode="json", exclude_none=True
                ),
            }
            for code in group
        }
        out[status] = {
            "description": "Problem: " + ", ".join(c.value for c in group),
            "content": {
                PROBLEM_CONTENT_TYPE: {
                    "schema": {"$ref": "#/components/schemas/Problem"},
                    "examples": examples,
                }
            },
        }
    return out


def responses_for(
    operation_id: str, *, extra: dict[int | str, dict[str, Any]] | None = None
) -> dict[int | str, dict[str, Any]]:
    """The problem responses of an operation of the endpoint table (plus ``extra``)."""
    decl = _BY_OP[operation_id]
    out = problem_responses(*COMMON_CODES, ErrorCode.NOT_FOUND, *decl.errors)
    for status, spec in (extra or {}).items():
        out[status] = spec
    return out


class JsonBodyLimit:
    """Pure ASGI middleware: cap and depth-check JSON bodies of matching ``POST`` routes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: Sequence[str],
        max_bytes: int,
        max_depth: int = 64,
    ) -> None:
        self.app = app
        self.patterns = [re.compile(p) for p in paths]
        self.max_bytes = max_bytes
        self.max_depth = max_depth

    def _matches(self, scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and any(p.fullmatch(scope.get("path", "")) for p in self.patterns)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._matches(scope):
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _problem(send, ErrorCode.PAYLOAD_TOO_LARGE, self._too_large())
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await _problem(send, ErrorCode.PAYLOAD_TOO_LARGE, self._too_large())
                return
            chunks.append(chunk)
            more = bool(message.get("more_body", False))
        body = b"".join(chunks)
        if body:
            try:
                if len(body) > 64 * 1024:
                    await asyncio.to_thread(loads_limited, body, max_depth=self.max_depth)
                else:
                    loads_limited(body, max_depth=self.max_depth)
            except JsonTooDeep as exc:
                await _problem(send, ErrorCode.SCHEMA_INVALID, str(exc))
                return
            except ValueError:
                pass  # not JSON: the route's own parser answers 422 with a pointer
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    def _too_large(self) -> str:
        return (
            f"the request body exceeds {self.max_bytes} bytes; upload large data as a dataset "
            "and reference it (dataset://…)"
        )


async def _problem(send: Send, code: ErrorCode, detail: str) -> None:
    body = Problem.from_code(code, detail).model_dump_json(exclude_none=True).encode()
    await send(
        {
            "type": "http.response.start",
            "status": ERROR_INFO[code].status,
            "headers": [
                (b"content-type", PROBLEM_CONTENT_TYPE.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
