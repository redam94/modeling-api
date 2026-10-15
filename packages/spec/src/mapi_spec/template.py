"""A tiny, data-only template language used by schema templates and SchemaEffects.

Templates are plain JSON. Strings may contain placeholders that are filled from a
mapping of parameters (normally a step's or model's settings):

``"{p}"``
    The whole string is replaced by the *typed* value of ``p`` (list, number, ...).
``"x_{p}"``
    String interpolation; ``p`` must be a string, integer or boolean.
``"{p.sub}"``
    Dotted lookup into a mapping-valued parameter.
``"{min:p}"`` / ``"{max:p}"`` / ``"{len:p}"``
    Minimum / maximum / length of a list parameter (or the scalar itself for min/max).
``"{*p}"`` (list element)
    Splice the items of list ``p`` into the enclosing list.
``"pre{*p as x}post"`` (mapping key)
    Emit one entry per item of list ``p`` with key ``pre<item>post`` (literal prefix and
    suffix are optional); the item is bound to ``x`` while rendering the entry's value.
``"{{"`` / ``"}}"``
    Literal braces.

There are no loops beyond ``*``, no conditionals and no code: rendering is total and
bounded by the template size times the parameter list lengths.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue

__all__ = ["TemplateError", "placeholders", "render", "render_string"]


class TemplateError(ValueError):
    """Rendering failed (unknown parameter, wrong type ...). ``path`` locates the problem."""

    def __init__(self, message: str, path: tuple[str | int, ...] = ()) -> None:
        super().__init__(message)
        self.path = path


_PH_RE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
_EXPR_RE = re.compile(r"^(?:(min|max|len):)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$")
_SPLICE_RE = re.compile(r"^\{\*([A-Za-z_][A-Za-z0-9_]*)\}$")
_KEY_LOOP_RE = re.compile(
    r"^([^{}]*)\{\*([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\}([^{}]*)$"
)

MAX_EXPANSION = 10_000


def _lookup(expr: str, params: Mapping[str, Any], path: tuple[str | int, ...]) -> Any:
    m = _EXPR_RE.match(expr.strip())
    if m is None:
        raise TemplateError(f"invalid placeholder {{{expr}}}", path)
    func, dotted = m.group(1), m.group(2)
    head, *rest = dotted.split(".")
    if head not in params:
        raise TemplateError(f"unknown template parameter {head!r}", path)
    value = params[head]
    for part in rest:
        if not isinstance(value, Mapping) or part not in value:
            raise TemplateError(f"parameter {dotted!r} has no field {part!r}", path)
        value = value[part]
    if func is None:
        return value
    if func == "len":
        if not isinstance(value, list | str):
            raise TemplateError(f"len: needs a list parameter, {dotted!r} is not", path)
        return len(value)
    items = value if isinstance(value, list) else [value]
    nums = [v for v in items if isinstance(v, int | float) and not isinstance(v, bool)]
    if not nums or len(nums) != len(items):
        raise TemplateError(f"{func}: needs numeric values in {dotted!r}", path)
    return min(nums) if func == "min" else max(nums)


def _fmt(value: Any, expr: str, path: tuple[str | int, ...]) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TemplateError(f"placeholder {{{expr}}} inside a string needs a scalar value", path)


def render_string(text: str, params: Mapping[str, Any], path: tuple[str | int, ...] = ()) -> Any:
    """Render one string; a lone placeholder returns the typed value."""
    stripped = text
    m = _PH_RE.fullmatch(stripped)
    if m is not None and m.group(1) is not None:
        return _lookup(m.group(1), params, path)

    def sub(match: re.Match[str]) -> str:
        tok = match.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        return _fmt(_lookup(match.group(1), params, path), match.group(1), path)

    return _PH_RE.sub(sub, text)


def _as_list(name: str, params: Mapping[str, Any], path: tuple[str | int, ...]) -> list[Any]:
    if name not in params:
        raise TemplateError(f"unknown template parameter {name!r}", path)
    value = params[name]
    if value is None:
        return []
    if not isinstance(value, list):
        raise TemplateError(f"'*{name}' needs a list parameter", path)
    if len(value) > MAX_EXPANSION:
        raise TemplateError(f"'*{name}' expands to too many items", path)
    return value


def render(
    template: JsonValue, params: Mapping[str, Any], path: tuple[str | int, ...] = ()
) -> JsonValue:
    """Render a JSON template with ``params``. Raises :class:`TemplateError`."""
    if isinstance(template, str):
        out: JsonValue = render_string(template, params, path)
        return out
    if isinstance(template, list):
        result: list[JsonValue] = []
        for i, item in enumerate(template):
            if isinstance(item, str) and (sm := _SPLICE_RE.match(item)):
                result.extend(_as_list(sm.group(1), params, (*path, i)))
            else:
                result.append(render(item, params, (*path, i)))
        return result
    if isinstance(template, dict):
        rendered: dict[str, JsonValue] = {}
        for key, value in template.items():
            loop = _KEY_LOOP_RE.match(key)
            if loop:
                prefix, list_name, var, suffix = loop.groups()
                for item in _as_list(list_name, params, (*path, key)):
                    if not isinstance(item, str):
                        raise TemplateError(f"'*{list_name}' keys must be strings", (*path, key))
                    scoped = {**params, var: item}
                    new_key = f"{prefix}{item}{suffix}"
                    _put(rendered, new_key, render(value, scoped, (*path, key)), (*path, key))
                continue
            new_key = render_string(key, params, (*path, key))
            if not isinstance(new_key, str):
                raise TemplateError("mapping keys must render to strings", (*path, key))
            _put(rendered, new_key, render(value, params, (*path, key)), (*path, key))
        return rendered
    return template


def _put(
    target: dict[str, JsonValue], key: str, value: JsonValue, path: tuple[str | int, ...]
) -> None:
    if key in target:
        raise TemplateError(f"template renders duplicate key {key!r}", path)
    target[key] = value


def placeholders(template: JsonValue, bound: frozenset[str] = frozenset()) -> set[str]:
    """Top-level parameter names referenced by ``template`` (excluding loop variables)."""
    found: set[str] = set()

    def from_string(s: str, scope: frozenset[str]) -> None:
        sm = _SPLICE_RE.match(s)
        if sm:
            if sm.group(1) not in scope:
                found.add(sm.group(1))
            return
        for m in _PH_RE.finditer(s):
            if m.group(1) is None:
                continue
            em = _EXPR_RE.match(m.group(1).strip())
            name = em.group(2).split(".")[0] if em else m.group(1)
            if name not in scope:
                found.add(name)

    def walk(node: JsonValue, scope: frozenset[str]) -> None:
        if isinstance(node, str):
            from_string(node, scope)
        elif isinstance(node, list):
            for item in node:
                walk(item, scope)
        elif isinstance(node, dict):
            for key, value in node.items():
                loop = _KEY_LOOP_RE.match(key)
                if loop:
                    if loop.group(2) not in scope:
                        found.add(loop.group(2))
                    walk(value, scope | {loop.group(3)})
                else:
                    from_string(key, scope)
                    walk(value, scope)

    walk(template, bound)
    return found


def deep_merge(base: JsonValue, overlay: JsonValue) -> JsonValue:
    """Merge ``overlay`` into ``base``: mappings merge recursively, ``None`` deletes a key,
    anything else replaces."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            if v is None:
                out.pop(k, None)
            elif k in out:
                out[k] = deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    return overlay
