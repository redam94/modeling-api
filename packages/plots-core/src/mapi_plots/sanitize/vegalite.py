"""Vega-Lite sanitizer: pinned JSON Schema validation plus the safety rules of plan §10.3.

Rejected: ``url`` data (anywhere, including lookups), ``href`` encodings and mark properties,
``image`` marks and ``url`` channels, custom format types, prototype-reaching keys and field
paths, script/CSS URLs in strings and any expression outside the allow-listed subset (see
:mod:`mapi_plots.sanitize.expr`). ``$schema`` is normalised to the pinned version.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError, best_match, relevance

from mapi_plots.sanitize._common import (
    SanitizeReport,
    SanitizeRule,
    child_pointer,
    scan_string,
    walk,
)
from mapi_plots.sanitize.expr import check_expression

__all__ = [
    "VEGA_LITE_SCHEMA_URL",
    "VEGA_LITE_VERSION",
    "sanitize_vega_lite",
    "vega_lite_schema",
]

VEGA_LITE_VERSION = "6.4.1"
VEGA_LITE_SCHEMA_URL = f"https://vega.github.io/schema/vega-lite/v{VEGA_LITE_VERSION}.json"
_SCHEMA_FILE = f"vega-lite-v{VEGA_LITE_VERSION}.json"
_ACCEPTED_SCHEMA = re.compile(
    r"^https?://vega\.github\.io/schema/vega-lite/v[56](\.\d+){0,2}\.json$"
)

_EXPR_KEYS = frozenset({"expr", "calculate", "test"})
#: string predicates (``filter``, ``test`` and their logical compositions) are expressions
_PREDICATE_KEYS = frozenset({"filter", "and", "or", "not"})
_EVENT_KEYS = frozenset({"on", "clear", "translate", "zoom"})
_FORMAT_TYPES = frozenset({"number", "time", "utc"})
_BRACKETS = re.compile(r"\[([^\[\]]*)\]")


@lru_cache(maxsize=1)
def vega_lite_schema() -> dict[str, Any]:
    """The vendored Vega-Lite JSON Schema (draft-07)."""
    text = resources.files("mapi_plots.sanitize.schemas").joinpath(_SCHEMA_FILE).read_text("utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


@lru_cache(maxsize=1)
def _validator() -> Draft7Validator:
    # Local "#/definitions/..." refs only; jsonschema never fetches remote refs by default.
    return Draft7Validator(vega_lite_schema())


def _is_expr_key(key: str | None) -> bool:
    return key is not None and (key in _EXPR_KEYS or key.endswith("Expr"))


def _check_expr(expr: str, ptr: str, report: SanitizeReport) -> None:
    for problem in check_expression(expr, max_chars=report.limits.max_expr_chars):
        report.error(
            SanitizeRule.EXPRESSION,
            ptr,
            f"expression rejected: {problem}",
            untrusted=expr[:500],
            suggested_action="use a transform or an allow-listed Vega expression function",
        )


def _check_safety(spec: dict[str, Any], base: str, report: SanitizeReport) -> None:
    for node, ptr, key in walk(spec, base):
        if report.full:
            return
        if isinstance(node, str):
            scan_string(node, ptr, report)
            if _is_expr_key(key) or key in _PREDICATE_KEYS:
                _check_expr(node, ptr, report)
            elif key in _EVENT_KEYS and "[" in node:
                for inner in _BRACKETS.findall(node):
                    _check_expr(inner, ptr, report)
            elif key == "formatType" and node not in _FORMAT_TYPES:
                report.error(
                    SanitizeRule.FORMAT_TYPE,
                    ptr,
                    "only the number, time and utc format types are allowed",
                    untrusted=node[:200],
                )
            continue
        if not isinstance(node, dict):
            continue
        in_encoding = "/encoding/" in ptr + "/" or ptr.endswith("/encoding")
        for k, v in node.items():
            cptr = child_pointer(ptr, k)
            if k == "url":
                if in_encoding:
                    report.error(
                        SanitizeRule.EXTERNAL_IMAGE,
                        cptr,
                        "url encodings (external images) are not allowed",
                    )
                else:
                    report.error(
                        SanitizeRule.URL_DATA,
                        cptr,
                        "data must be referenced by dataset name, never by URL",
                        suggested_action='use {"name": "<dataset>"} and declare the dataset',
                    )
            elif k == "href":
                report.error(SanitizeRule.HREF, cptr, "href links are not allowed in plots")
            elif k == "mark" and (
                v == "image" or (isinstance(v, dict) and v.get("type") == "image")
            ):
                report.error(SanitizeRule.EXTERNAL_IMAGE, cptr, "image marks are not allowed")
            elif k == "customFormatTypes" and v is not False:
                report.error(SanitizeRule.FORMAT_TYPE, cptr, "custom format types are disabled")


def _schema_errors(spec: dict[str, Any], base: str, report: SanitizeReport) -> None:
    errors = sorted(_validator().iter_errors(spec), key=relevance)
    for err in errors[:5]:
        leaf: ValidationError = best_match(err.context) if err.context else err
        if leaf is None:  # pragma: no cover - best_match of a non-empty list
            leaf = err
        ptr = base
        for part in leaf.absolute_path:
            ptr = child_pointer(ptr, part)
        report.error(
            SanitizeRule.SCHEMA,
            ptr,
            f"does not match the Vega-Lite v{VEGA_LITE_VERSION} schema",
            untrusted=leaf.message[:1000],
        )


def sanitize_vega_lite(
    spec: dict[str, Any],
    report: SanitizeReport,
    *,
    base: str = "/spec",
    validate_schema: bool = True,
) -> dict[str, Any]:
    """Check a Vega-Lite spec; returns a copy with ``$schema`` pinned. Errors go to ``report``."""
    out = copy.deepcopy(spec)
    declared = out.get("$schema")
    if declared is None:
        report.change(SanitizeRule.SCHEMA_URL, child_pointer(base, "$schema"), "added $schema")
    elif not (isinstance(declared, str) and _ACCEPTED_SCHEMA.match(declared)):
        report.error(
            SanitizeRule.SCHEMA,
            child_pointer(base, "$schema"),
            "$schema must be an official Vega-Lite v5 or v6 schema URL",
            untrusted=str(declared)[:200],
        )
    elif declared != VEGA_LITE_SCHEMA_URL:
        report.change(
            SanitizeRule.SCHEMA_URL,
            child_pointer(base, "$schema"),
            f"$schema pinned to Vega-Lite v{VEGA_LITE_VERSION}",
        )
    out["$schema"] = VEGA_LITE_SCHEMA_URL
    _check_safety(out, base, report)
    if validate_schema and not report.full:
        _schema_errors(out, base, report)
    return out
