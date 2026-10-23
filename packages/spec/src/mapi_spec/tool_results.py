"""Size caps for dashboard tool results (v0.20; shared by the chat loop and the MCP server).

A tool result is the JSON of a dashboard DTO (:mod:`mapi_spec.dashboard_tools`). Most are a
few kilobytes, but a readout can be up to ``MAX_READOUT_BYTES``. Before a result goes to a
model (chat) or to an external agent (MCP), :func:`cap_tool_result` makes it fit a byte
budget, deterministically:

1. Serialise as canonical compact JSON (keys sorted, no whitespace, UTF-8). If it fits,
   return it unchanged (``truncated=False``).
2. Otherwise trim list fields named in :data:`TRIM_ORDER`, one key at a time and at every
   depth: keep the first and the last ``n`` items and put ``{"_truncated": k}`` (``k`` items
   removed) between them, with ``n`` going down :data:`KEEP_STEPS` until the result fits.
   A key trimmed to ``n = 0`` stays trimmed while the next key is tried.
3. Last resort: ``{"_truncated": true, "summary": {...}}`` with the top-level scalars only
   (long strings shortened, then keys dropped from the end until it fits).

The output is always at most ``max_bytes`` bytes, and the same input always gives the same
output (golden tests). The module is pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

__all__ = [
    "KEEP_STEPS",
    "MIN_CAP_BYTES",
    "TRIM_ORDER",
    "TRUNCATED_KEY",
    "CappedResult",
    "canonical_tool_json",
    "cap_tool_result",
]

TRUNCATED_KEY: Final = "_truncated"
"""The marker key: an item ``{"_truncated": k}`` in a list, or ``"_truncated": true`` at the
top of a last-resort summary."""

TRIM_ORDER: Final[tuple[str, ...]] = (
    "points",
    "series",
    "frames",
    "rows",
    "history",
    "accuracy_history",
    "panel_deltas",
    "channels",
    "panels",
    "tiles",
    "items",
)
"""List keys trimmed first to last. Series points are the bulk of every large readout
(WP0 (d)), so they go first; whole series, field frames (base64 codes a model cannot read)
and rows follow; readout headers (``items``) go last."""

_TOOL_FIRST: Final[Mapping[str, tuple[str, ...]]] = {
    # a page of readout headers: the headers are the result, trim nothing else first
    "list_readouts": ("items",),
}
"""Keys trimmed before :data:`TRIM_ORDER` for one tool."""

KEEP_STEPS: Final[tuple[int, ...]] = (256, 128, 96, 64, 48, 32, 24, 16, 12, 8, 6, 4, 3, 2, 1, 0)
"""How many items are kept at each end of a trimmed list, tried in this order."""

MIN_CAP_BYTES: Final = 64
"""The smallest budget accepted (a last-resort summary with no scalars is 34 bytes)."""

_SUMMARY_STR_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class CappedResult:
    """A tool result that fits its budget.

    ``payload`` is the (possibly trimmed) JSON value, ``text`` its canonical JSON, and
    ``original_bytes`` the size of the untrimmed canonical JSON. When ``truncated`` is
    false, ``payload`` is the original result object.
    """

    payload: JsonValue
    text: str
    truncated: bool
    original_bytes: int


def canonical_tool_json(value: JsonValue) -> str:
    """Canonical compact JSON: keys sorted, no whitespace, non-ASCII kept as UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _trim(value: JsonValue, keep: Mapping[str, int]) -> JsonValue:
    """``value`` with every list under a key of ``keep`` cut to its first and last ``n``."""
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for k, v in value.items():
            v2 = _trim(v, keep)
            n = keep.get(k)
            if n is not None and isinstance(v2, list) and len(v2) > 2 * n:
                removed = len(v2) - 2 * n
                marker: JsonValue = {TRUNCATED_KEY: removed}
                v2 = [*v2[:n], marker, *(v2[-n:] if n else [])]
            out[k] = v2
        return out
    if isinstance(value, list):
        return [_trim(v, keep) for v in value]
    return value


def _summary(result: JsonValue, max_bytes: int) -> tuple[JsonValue, str]:
    """The last-resort payload: top-level scalars only, shortened until it fits."""
    scalars: dict[str, JsonValue] = {}
    if isinstance(result, dict):
        for k in sorted(result):
            v = result[k]
            if v is None or isinstance(v, bool | int | float):
                scalars[k] = v
            elif isinstance(v, str):
                scalars[k] = v if len(v) <= _SUMMARY_STR_CHARS else v[:_SUMMARY_STR_CHARS] + "…"
    elif isinstance(result, list):
        scalars["length"] = len(result)
    keys = list(scalars)
    while True:
        payload: JsonValue = {TRUNCATED_KEY: True, "summary": {k: scalars[k] for k in keys}}
        text = canonical_tool_json(payload)
        if _size(text) <= max_bytes or not keys:
            return payload, text
        keys.pop()


def cap_tool_result(name: str, result: JsonValue, max_bytes: int) -> CappedResult:
    """Make the result of tool ``name`` fit ``max_bytes`` bytes of canonical JSON.

    See the module docstring for the steps. Raises ``ValueError`` when ``max_bytes`` is
    below :data:`MIN_CAP_BYTES`.
    """
    if max_bytes < MIN_CAP_BYTES:
        raise ValueError(f"max_bytes must be at least {MIN_CAP_BYTES}")
    text = canonical_tool_json(result)
    original = _size(text)
    if original <= max_bytes:
        return CappedResult(result, text, False, original)

    order: Sequence[str] = _TOOL_FIRST.get(name, ())
    order = (*order, *(k for k in TRIM_ORDER if k not in order))
    keep: dict[str, int] = {}
    for key in order:
        for n in KEEP_STEPS:
            keep[key] = n
            trimmed = _trim(result, keep)
            text = canonical_tool_json(trimmed)
            if _size(text) <= max_bytes:
                return CappedResult(trimmed, text, True, original)

    payload, text = _summary(result, max_bytes)
    return CappedResult(payload, text, True, original)
