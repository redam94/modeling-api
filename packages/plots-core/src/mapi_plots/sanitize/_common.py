"""Shared sanitizer machinery: limits, rules, the issue collector and structural checks.

Everything here is pure JSON + stdlib: the control plane imports it, so it must never pull in
numpy, pandas, altair or plotly.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, json_pointer

__all__ = [
    "DENY_NAMES",
    "SanitizeError",
    "SanitizeLimits",
    "SanitizeReport",
    "SanitizeRule",
    "check_structure",
    "child_pointer",
    "is_safe_image_uri",
    "parse_json_strict",
    "scan_string",
]


class SanitizeRule(StrEnum):
    """Why the sanitizer rejected (or changed) part of a plot spec. Values are stable."""

    # size and shape
    TOO_LARGE = "too_large"
    TOO_DEEP = "too_deep"
    TOO_MANY_NODES = "too_many_nodes"
    STRING_TOO_LONG = "string_too_long"
    ARRAY_TOO_LONG = "array_too_long"
    # structure
    PARSE = "parse"
    ENVELOPE = "envelope"
    SCHEMA = "schema"
    DATASET_REF = "dataset_ref"
    TYPED_ARRAY = "typed_array"
    # security
    PROTO_KEY = "proto_key"
    URL_DATA = "url_data"
    HREF = "href"
    EXTERNAL_IMAGE = "external_image"
    TILE_MAP = "tile_map"
    SCRIPT_URL = "script_url"
    CSS_URL = "css_url"
    EXPRESSION = "expression"
    FORMAT_TYPE = "format_type"
    CONFIG = "config"
    # changes (not errors)
    HTML_STRIPPED = "html_stripped"
    SCHEMA_URL = "schema_url"
    TEMPLATE_PRUNED = "template_pruned"


_SIZE_RULES = {
    SanitizeRule.TOO_LARGE,
    SanitizeRule.TOO_DEEP,
    SanitizeRule.TOO_MANY_NODES,
    SanitizeRule.STRING_TOO_LONG,
    SanitizeRule.ARRAY_TOO_LONG,
}
_SHAPE_RULES = {
    SanitizeRule.PARSE,
    SanitizeRule.ENVELOPE,
    SanitizeRule.SCHEMA,
    SanitizeRule.DATASET_REF,
    SanitizeRule.TYPED_ARRAY,
}


def rule_code(rule: SanitizeRule) -> ErrorCode:
    """The stable :class:`ErrorCode` reported for a rule."""
    if rule in _SIZE_RULES:
        return ErrorCode.PAYLOAD_TOO_LARGE
    if rule is SanitizeRule.PARSE:
        return ErrorCode.SPEC_PARSE_ERROR
    if rule in _SHAPE_RULES:
        return ErrorCode.SCHEMA_INVALID
    return ErrorCode.PLOT_UNSAFE


@dataclass(frozen=True, slots=True)
class SanitizeLimits:
    """Caps applied before anything else looks at a plot document."""

    max_document_bytes: int = 16 * 1024 * 1024  # whole PlotSpec JSON incl. inline Arrow
    max_spec_bytes: int = 2 * 1024 * 1024  # the ``spec`` member alone
    max_depth: int = 48
    max_nodes: int = 200_000
    max_string_bytes: int = 64 * 1024
    max_array_items: int = 100_000
    max_expr_chars: int = 2_000
    max_typed_array_bytes: int = 8 * 1024 * 1024
    max_issues: int = 50


DEFAULT_LIMITS = SanitizeLimits()

#: Keys and path segments that reach JavaScript prototypes. Rejected everywhere.
DENY_NAMES = frozenset({"__proto__", "constructor", "prototype"})


@dataclass(slots=True)
class SanitizeReport:
    """Collects errors (blocking) and changes (what was rewritten) while sanitizing."""

    limits: SanitizeLimits = DEFAULT_LIMITS
    errors: list[Issue] = field(default_factory=list)
    changes: list[Issue] = field(default_factory=list)

    @property
    def full(self) -> bool:
        return len(self.errors) >= self.limits.max_issues

    def error(
        self,
        rule: SanitizeRule,
        pointer: str,
        message: str,
        *,
        untrusted: str | None = None,
        suggested_action: str | None = None,
    ) -> None:
        if self.full:
            return
        self.errors.append(
            Issue(
                code=rule_code(rule),
                severity=Severity.ERROR,
                pointer=pointer,
                message=message[:2000],
                suggested_action=suggested_action,
                untrusted_text=None if untrusted is None else untrusted[:8000],
                context={"rule": rule.value},
            )
        )

    def change(self, rule: SanitizeRule, pointer: str, message: str) -> None:
        if len(self.changes) < 1000:
            self.changes.append(
                Issue(
                    code=ErrorCode.PLOT_UNSAFE,
                    severity=Severity.INFO,
                    pointer=pointer,
                    message=message[:2000],
                    context={"rule": rule.value},
                )
            )

    def raise_if_errors(self) -> None:
        if self.errors:
            raise SanitizeError(self.errors)


class SanitizeError(MapiError):
    """A plot document was rejected. ``issues`` lists every violation (capped)."""

    default_code = ErrorCode.PLOT_UNSAFE

    def __init__(self, issues: list[Issue], detail: str | None = None) -> None:
        codes = {i.code for i in issues}
        code = next(
            (
                c
                for c in (
                    ErrorCode.PLOT_UNSAFE,
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    ErrorCode.SPEC_PARSE_ERROR,
                    ErrorCode.SCHEMA_INVALID,
                )
                if c in codes
            ),
            ErrorCode.PLOT_UNSAFE,
        )
        first = next((i for i in issues if i.code is code), issues[0] if issues else None)
        n = len(issues)
        summary = f"plot rejected with {n} issue{'s' if n != 1 else ''}"
        if first is not None:
            summary += f"; first at {first.pointer or '/'}: {first.message}"
        super().__init__(
            (detail or summary)[:2000],
            code=code,
            pointer=first.pointer if first else "",
            issues=issues,
        )

    @property
    def rules(self) -> set[SanitizeRule]:
        """The rules that fired."""
        out: set[SanitizeRule] = set()
        for issue in self.issues:
            rule = issue.context.get("rule")
            if isinstance(rule, str) and rule in SanitizeRule._value2member_map_:
                out.add(SanitizeRule(rule))
        return out


def child_pointer(base: str, key: str | int) -> str:
    return base + json_pointer(key)


# ------------------------------------------------------------------------------ parsing


class _DuplicateKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey(k)
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not JSON")


def parse_json_strict(text: str | bytes, limits: SanitizeLimits = DEFAULT_LIMITS) -> Any:
    """Parse JSON rejecting duplicate keys, NaN/Infinity, oversize and over-deep input."""
    report = SanitizeReport(limits)
    size = len(text.encode() if isinstance(text, str) else text)
    if size > limits.max_document_bytes:
        report.error(
            SanitizeRule.TOO_LARGE,
            "",
            f"document is {size} bytes; the limit is {limits.max_document_bytes}",
        )
        report.raise_if_errors()
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constants)
    except _DuplicateKey as exc:
        report.error(SanitizeRule.PARSE, "", "duplicate object key", untrusted=str(exc))
    except RecursionError:
        report.error(SanitizeRule.TOO_DEEP, "", "document nesting is too deep")
    except (ValueError, UnicodeDecodeError) as exc:
        report.error(SanitizeRule.PARSE, "", "document is not valid JSON", untrusted=str(exc))
    report.raise_if_errors()
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------- structural checks


def _iter_nodes(root: Any, base: str) -> Iterator[tuple[Any, str, int, str | None]]:
    """Depth-first (node, pointer, depth, parent_key) without recursion."""
    stack: list[tuple[Any, str, int, str | None]] = [(root, base, 0, None)]
    while stack:
        node, ptr, depth, key = stack.pop()
        yield node, ptr, depth, key
        if isinstance(node, dict):
            for k, v in reversed(list(node.items())):
                stack.append((v, child_pointer(ptr, str(k)), depth + 1, str(k)))
        elif isinstance(node, list | tuple):
            for i in range(len(node) - 1, -1, -1):
                stack.append((node[i], child_pointer(ptr, i), depth + 1, key))


_PATH_SPLIT = re.compile(r"[.\[\]{}%'\"]+")


def path_segments(text: str) -> list[str]:
    """Dotted/bracketed path segments of a field name or attribute path."""
    return [s for s in _PATH_SPLIT.split(text) if s]


def check_structure(root: Any, base: str, report: SanitizeReport) -> bool:
    """Caps on depth, node count, string and array sizes; JSON-only types; prototype keys.

    Returns False when a hard cap was hit (further passes are skipped).
    """
    lim = report.limits
    for nodes, (node, ptr, depth, _key) in enumerate(_iter_nodes(root, base), start=1):
        if nodes > lim.max_nodes:
            report.error(SanitizeRule.TOO_MANY_NODES, base, f"more than {lim.max_nodes} nodes")
            return False
        if depth > lim.max_depth:
            report.error(SanitizeRule.TOO_DEEP, ptr, f"nesting deeper than {lim.max_depth}")
            return False
        if isinstance(node, dict):
            for k in node:
                if not isinstance(k, str):
                    report.error(SanitizeRule.SCHEMA, ptr, "object keys must be strings")
                    return False
                if k in DENY_NAMES or any(s in DENY_NAMES for s in path_segments(k)):
                    report.error(
                        SanitizeRule.PROTO_KEY,
                        child_pointer(ptr, k),
                        "keys named __proto__, constructor or prototype are not allowed",
                    )
                if len(k.encode()) > 1024:
                    report.error(SanitizeRule.STRING_TOO_LONG, ptr, "object key is too long")
        elif isinstance(node, list | tuple):
            if len(node) > lim.max_array_items:
                report.error(
                    SanitizeRule.ARRAY_TOO_LONG, ptr, f"array longer than {lim.max_array_items}"
                )
                return False
        elif isinstance(node, str):
            if len(node) > lim.max_string_bytes or len(node.encode()) > lim.max_string_bytes:
                report.error(
                    SanitizeRule.STRING_TOO_LONG,
                    ptr,
                    f"string longer than {lim.max_string_bytes} bytes",
                )
        elif isinstance(node, bool) or node is None or isinstance(node, int):
            pass
        elif isinstance(node, float):
            if not math.isfinite(node):
                report.error(SanitizeRule.SCHEMA, ptr, "numbers must be finite")
        else:
            report.error(SanitizeRule.SCHEMA, ptr, f"{type(node).__name__} is not a JSON value")
        if report.full:
            return False
    return True


# ---------------------------------------------------------------------- string scanning

_SCRIPT_SCHEME = re.compile(
    r"^(?:javascript|vbscript|livescript|data\s*:\s*(?:text/html|application/|image/svg))",
    re.I,
)
_CSS_URL = re.compile(r"url\s*\(|@import|expression\s*\(", re.I)
_CTRL = re.compile(r"[\x00-\x20\x7f]+")
_SAFE_IMAGE = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$")


def is_safe_image_uri(value: Any) -> bool:
    """Only raster ``data:`` URIs (png/jpeg/gif/webp, base64) may be used as image sources."""
    return isinstance(value, str) and bool(_SAFE_IMAGE.match(value))


def scan_string(value: str, ptr: str, report: SanitizeReport) -> None:
    """Rules applied to every string value in every plot kind."""
    squashed = _CTRL.sub("", value)
    if _SCRIPT_SCHEME.match(squashed):
        report.error(
            SanitizeRule.SCRIPT_URL,
            ptr,
            "script or HTML URLs (javascript:, data:text/html, ...) are not allowed",
            untrusted=value[:200],
        )
    if _CSS_URL.search(value):
        report.error(
            SanitizeRule.CSS_URL,
            ptr,
            "CSS url(), @import and expression() are not allowed in plot strings",
            untrusted=value[:200],
        )
    if any(s in DENY_NAMES for s in path_segments(value)):
        report.error(
            SanitizeRule.PROTO_KEY,
            ptr,
            "field paths through __proto__, constructor or prototype are not allowed",
            untrusted=value[:200],
        )


def iter_strings(root: Any, base: str) -> Iterator[tuple[str, str, str | None]]:
    """Every string value with its pointer and the key that holds it."""
    for node, ptr, _depth, key in _iter_nodes(root, base):
        if isinstance(node, str):
            yield node, ptr, key


def walk(root: Any, base: str) -> Iterator[tuple[Any, str, str | None]]:
    """Every node with its pointer and parent key (dicts, lists and scalars)."""
    for node, ptr, _depth, key in _iter_nodes(root, base):
        yield node, ptr, key


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None
