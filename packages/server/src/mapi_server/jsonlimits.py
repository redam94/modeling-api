"""JSON parsing with limits for documents that come from executions (plan §9).

The control plane parses runner-written JSON (sidecars, manifests, plots, catalogs). Byte
caps are applied by the callers; this module adds a **nesting** limit, so hostile input like
``[[[[…]]]]`` is rejected as a plain :class:`ValueError` instead of escaping as
``RecursionError`` (from the C decoder, or later from Pydantic or the plot sanitizer).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["DEFAULT_MAX_DEPTH", "JsonTooDeep", "check_depth", "loads_limited"]

DEFAULT_MAX_DEPTH = 64
"""Deeper than any document the platform writes (Vega-Lite specs nest ~15 levels)."""


class JsonTooDeep(ValueError):
    """The document nests deeper than allowed."""

    def __init__(self, max_depth: int) -> None:
        super().__init__(f"JSON nesting exceeds depth {max_depth}")
        self.max_depth = max_depth


def check_depth(value: Any, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
    """Raise :class:`JsonTooDeep` when ``value`` nests deeper than ``max_depth`` (iterative)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Any = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > max_depth:
            raise JsonTooDeep(max_depth)
        stack.extend((child, depth + 1) for child in children if isinstance(child, dict | list))


def loads_limited(raw: str | bytes | bytearray, *, max_depth: int = DEFAULT_MAX_DEPTH) -> Any:
    """``json.loads`` that raises ``ValueError`` (never ``RecursionError``) on deep nesting."""
    try:
        value = json.loads(raw)
    except RecursionError:
        raise JsonTooDeep(max_depth) from None
    check_depth(value, max_depth)
    return value
