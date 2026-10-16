"""OpenAPI ``responses`` entries for RFC 7807 problems (``application/problem+json``).

``problem_responses(ErrorCode.NOT_FOUND, ErrorCode.SCHEMA_INVALID)`` documents each code's
status with the shared ``Problem`` schema and one example per code, so generated clients
and the reference docs show which stable codes an operation answers with.
"""

from __future__ import annotations

from typing import Any

from mapi_spec.errors import ERROR_INFO, PROBLEM_CONTENT_TYPE, ErrorCode, Problem

__all__ = ["AUTH_PROBLEMS", "problem_responses"]

AUTH_PROBLEMS: tuple[ErrorCode, ...] = (ErrorCode.UNAUTHENTICATED, ErrorCode.FORBIDDEN)
"""Every authenticated route can answer these."""


def problem_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """``responses=`` for a route: one entry per HTTP status of ``codes``."""
    out: dict[int | str, dict[str, Any]] = {}
    for code in dict.fromkeys(codes):
        info = ERROR_INFO[code]
        entry = out.setdefault(
            info.status,
            {
                "description": "",
                "content": {
                    PROBLEM_CONTENT_TYPE: {
                        "schema": {"$ref": "#/components/schemas/Problem"},
                        "examples": {},
                    }
                },
            },
        )
        titles = [t for t in entry["description"].split("; ") if t]
        entry["description"] = "; ".join([*titles, f"{info.title} ({code.value})"])
        example = Problem.from_code(code, info.title).model_dump(mode="json", exclude_none=True)
        entry["content"][PROBLEM_CONTENT_TYPE]["examples"][code.value] = {
            "summary": info.title,
            "value": example,
        }
    return out
