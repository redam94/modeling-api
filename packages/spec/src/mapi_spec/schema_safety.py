"""JSON-Schema safety: the keywords, sizes and regular expressions a schema may use (v0.18).

The control plane validates settings with ``jsonschema`` (Python ``re`` for ``pattern``), and
the web renders forms from the same schemas. A schema that comes from an image catalog (a
plugin's ``settings_schema``, a node's ``params_schema``) or from a tenant's library document
must therefore be checked before it is used: a catastrophic ``pattern`` can hang the API, a
remote ``$ref`` makes the validator fetch a URL, and a huge or deep schema costs memory.

Rules (every one has a plain reason in the issue it raises):

* size ≤ :data:`SCHEMA_MAX_BYTES` (compact JSON), subschema depth ≤ :data:`SCHEMA_MAX_DEPTH`,
  ≤ :data:`SCHEMA_MAX_PROPS` property definitions, ``enum`` ≤ :data:`ENUM_MAX_ITEMS` items of
  ≤ :data:`ENUM_ITEM_MAX_CHARS` characters;
* only the keywords in :data:`ALLOWED_KEYWORDS`; ``$ref`` only to a local ``#/$defs/<name>``
  that exists, with no reference cycle; no ``$id``, ``$anchor``, ``$dynamicRef`` (not in the
  allow-list); ``format`` only :data:`ALLOWED_FORMATS`;
* ``x-ui-*`` keywords from :data:`UI_KEYWORDS` only (``x-ui-widget`` from :data:`UI_WIDGETS`);
* ``title``/``description``/``$comment`` plain text of ≤ :data:`TEXT_MAX_CHARS` characters;
* ``pattern`` values and ``patternProperties`` keys only when :func:`is_safe_regex` accepts
  them.

Two modes: ``strip`` (trusted image catalogs: drop what is unsafe and report a warning; a
built-in schema never trips it, which a test checks) and ``refuse`` (library documents now,
uploads in v0.19: every finding is an error). This module is JSON-only: the web mirrors the
keyword allow-list in ``features/forms/schema-sanitize.ts`` (parity golden in
``packages/spec/tests/golden/settings_schemas.json``).
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, json_pointer, parse_pointer

__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_KEYWORDS",
    "ENUM_ITEM_MAX_CHARS",
    "ENUM_MAX_ITEMS",
    "REGEX_MAX_LEN",
    "REGEX_MAX_REPEAT",
    "SCHEMA_MAX_BYTES",
    "SCHEMA_MAX_DEPTH",
    "SCHEMA_MAX_PROPS",
    "SETTINGS_SCHEMA_MAX_BYTES",
    "TEXT_MAX_CHARS",
    "UI_KEYWORDS",
    "UI_WIDGETS",
    "SafetyMode",
    "is_safe_regex",
    "require_safe_schema",
    "sanitize_schema",
    "sanitize_settings_schema",
]

SafetyMode = Literal["strip", "refuse"]

SCHEMA_MAX_BYTES: Final = 65_536
SCHEMA_MAX_DEPTH: Final = 12
SCHEMA_MAX_PROPS: Final = 256
REGEX_MAX_LEN: Final = 256
SETTINGS_SCHEMA_MAX_BYTES: Final = 16_384
ENUM_MAX_ITEMS: Final = 64
ENUM_ITEM_MAX_CHARS: Final = 200
TEXT_MAX_CHARS: Final = 2000
REGEX_MAX_REPEAT: Final = 1000
"""The largest finite bound of a ``{m,n}`` quantifier."""
_JSON_MAX_NESTING: Final = 64
"""Containers nested deeper than this are refused before any recursion (any keyword)."""

ALLOWED_FORMATS: Final = frozenset({"date", "date-time"})
UI_WIDGETS: Final = frozenset(
    {"slider", "select", "textarea", "var_picker", "dim_picker", "checkbox"}
)
UI_LEVELS: Final = frozenset({"basic", "advanced"})
UI_KEYWORDS: Final = frozenset({"x-ui-widget", "x-ui-group", "x-ui-order", "x-ui-level"})
"""``x-ui-level`` (``basic``/``advanced``) predates v0.18: the built-in model forms use it."""

_SCHEMA_MAPS: Final = frozenset({"properties", "patternProperties", "$defs", "dependentSchemas"})
_SCHEMA_LISTS: Final = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_ONE: Final = frozenset(
    {
        "additionalProperties",
        "items",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)
_TEXT: Final = frozenset({"title", "description", "$comment"})
_VALUES: Final = frozenset(
    {
        "type",
        "required",
        "enum",
        "const",
        "default",
        "examples",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minContains",
        "maxContains",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "format",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$ref",
        "$schema",
        "discriminator",
    }
)
ALLOWED_KEYWORDS: Final = _SCHEMA_MAPS | _SCHEMA_LISTS | _SCHEMA_ONE | _TEXT | _VALUES | UI_KEYWORDS
"""Every keyword a safe schema may use (anything else is dropped or refused)."""

_DRAFT_2020_12: Final = "https://json-schema.org/draft/2020-12/schema"
_LOCAL_REF: Final = re.compile(r"^#/\$defs/([^/]{1,128})$")
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNTRUSTED_MAX: Final = 500
_DROP: Final[Any] = object()
"""Returned by a keyword check to remove the keyword."""


# ======================================================================== regular expressions


class _Unsafe(Exception):
    """A pattern feature that can make the backtracking engine slow (the plain reason)."""


# The stdlib parser behind ``re.compile`` (``re._parser``, ``sre_parse`` before 3.11): the
# same tokenizer the validator later uses, so there is no second grammar to drift. Loaded by
# name: it is a private module without a public stub.
_sre: Any = importlib.import_module("re._parser")
_MAXREPEAT: Final[int] = int(_sre.MAXREPEAT)

_CATEGORY_RE: Final[dict[str, str]] = {
    "CATEGORY_DIGIT": r"\d",
    "CATEGORY_NOT_DIGIT": r"\D",
    "CATEGORY_SPACE": r"\s",
    "CATEGORY_NOT_SPACE": r"\S",
    "CATEGORY_WORD": r"\w",
    "CATEGORY_NOT_WORD": r"\W",
}


def _in_set(items: Sequence[tuple[Any, Any]], ch: str) -> bool:
    """Whether a character class (``IN``) matches ``ch`` (``True`` when unsure)."""
    negate = False
    hit = False
    for op, av in items:
        name = str(op)
        if name == "NEGATE":
            negate = True
        elif name == "LITERAL":
            hit = hit or chr(av) == ch
        elif name == "RANGE":
            hit = hit or av[0] <= ord(ch) <= av[1]
        elif name == "CATEGORY":
            cls = _CATEGORY_RE.get(str(av))
            hit = hit or cls is None or re.fullmatch(cls, ch) is not None
        else:  # an item this check does not know: assume it matches
            return True
    return hit != negate


def _can_match(items: Sequence[tuple[Any, Any]], ch: str) -> bool:
    """Whether any single-character atom anywhere in ``items`` could match ``ch``."""
    for op, av in items:
        name = str(op)
        if name == "LITERAL":
            if chr(av).lower() == ch.lower():
                return True
        elif name == "NOT_LITERAL":
            if chr(av) != ch:
                return True
        elif name == "ANY":
            return True
        elif name == "IN":
            if _in_set(av, ch) or _in_set(av, ch.swapcase()):
                return True
        elif name in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
            if _can_match(av[2], ch):
                return True
        elif name == "SUBPATTERN":
            if _can_match(av[3], ch):
                return True
        elif name == "BRANCH":
            if any(_can_match(alt, ch) for alt in av[1]):
                return True
        elif name not in {"AT"}:
            return True
    return False


def _edge_literal(items: Sequence[tuple[Any, Any]], *, last: bool) -> str | None:
    """The mandatory literal character a sequence starts (or ends) with, through groups."""
    if not items:
        return None
    op, av = items[-1 if last else 0]
    name = str(op)
    if name == "LITERAL":
        return chr(av)
    if name == "SUBPATTERN":
        return _edge_literal(av[3], last=last)
    return None


def _delimited(body: Sequence[tuple[Any, Any]], inner: Sequence[Sequence[tuple[Any, Any]]]) -> bool:
    """Whether a repeated ``body`` starts or ends with a literal that none of its ``inner``
    variable quantifiers can match: then every repetition splits the input one way only
    (``(\.[a-z]+)+`` and ``(\d+\.)+`` are safe, ``(\w+\s?)+`` is not)."""
    for sep in (_edge_literal(body, last=False), _edge_literal(body, last=True)):
        if sep is not None and not any(_can_match(b, sep) for b in inner):
            return True
    return False


def _variable_repeats(items: Sequence[tuple[Any, Any]]) -> Iterator[Sequence[tuple[Any, Any]]]:
    """The bodies of every quantifier with a variable count (``min != max``) in ``items``."""
    for op, av in items:
        name = str(op)
        if name in {"MAX_REPEAT", "MIN_REPEAT"}:
            if av[0] != av[1]:
                yield av[2]
            yield from _variable_repeats(av[2])
        elif name == "SUBPATTERN":
            yield from _variable_repeats(av[3])
        elif name == "BRANCH":
            for alt in av[1]:
                yield from _variable_repeats(alt)


def _check_regex(items: Sequence[tuple[Any, Any]], *, in_repeat: bool, quantified: bool) -> None:
    """Raise :class:`_Unsafe` for the first unsafe feature of a parsed pattern.

    ``in_repeat``: inside a repeated group whose body is not delimited (a variable quantifier
    here would be a star height of 2); ``quantified``: inside any group repeated more than once
    (alternation there is refused).
    """
    for op, av in items:
        name = str(op)
        if name in {"GROUPREF", "GROUPREF_EXISTS"}:
            raise _Unsafe("backreferences are not allowed")
        if name in {"ASSERT", "ASSERT_NOT"}:
            raise _Unsafe("lookahead and lookbehind are not allowed")
        if name in {"POSSESSIVE_REPEAT", "ATOMIC_GROUP"}:
            raise _Unsafe("possessive quantifiers and atomic groups are not allowed")
        if name == "SUBPATTERN":
            _check_regex(av[3], in_repeat=in_repeat, quantified=quantified)
        elif name == "BRANCH":
            if quantified:
                raise _Unsafe("alternation ('|') inside a repeated group is not allowed")
            for alt in av[1]:
                _check_regex(alt, in_repeat=in_repeat, quantified=quantified)
        elif name in {"MAX_REPEAT", "MIN_REPEAT"}:
            lo, hi, body = av
            if lo > REGEX_MAX_REPEAT or (hi != _MAXREPEAT and hi > REGEX_MAX_REPEAT):
                raise _Unsafe(f"a repeat count is larger than {REGEX_MAX_REPEAT}")
            if in_repeat and lo != hi:
                raise _Unsafe("nested quantifiers (such as '(a+)+') are not allowed")
            if hi <= 1:  # '?' and '{0,1}': the body runs at most once
                _check_regex(body, in_repeat=in_repeat, quantified=quantified)
                continue
            inner = list(_variable_repeats(body))
            if inner:
                # a repeated body with its own variable quantifier (star height 2) is only
                # unambiguous when a literal delimits each repetition
                if not _delimited(body, inner):
                    raise _Unsafe("nested quantifiers (such as '(a+)+') are not allowed")
                _check_regex(body, in_repeat=False, quantified=True)
            else:
                _check_regex(body, in_repeat=True, quantified=True)


def is_safe_regex(pattern: str) -> str | None:
    """``None`` if ``pattern`` is safe to run with Python ``re`` on untrusted input, else a
    plain reason.

    Refused: longer than :data:`REGEX_MAX_LEN`; does not compile; backreferences, lookaround,
    possessive quantifiers or atomic groups; a star height above 1 (a variable quantifier
    inside a repeated group, unless the group starts or ends with a literal the inner
    quantifiers cannot match); alternation inside a repeated group; ``{m,n}`` bounds above
    :data:`REGEX_MAX_REPEAT`.
    """
    if not isinstance(pattern, str):
        return "the pattern is not a string"
    if len(pattern) > REGEX_MAX_LEN:
        return f"the pattern is longer than {REGEX_MAX_LEN} characters"
    try:
        parsed = _sre.parse(pattern)
    except Exception:  # re.error, OverflowError, RecursionError: all mean "not usable"
        return "the pattern is not a valid regular expression"
    try:
        _check_regex(list(parsed), in_repeat=False, quantified=False)
    except _Unsafe as exc:
        return str(exc)
    except RecursionError:
        return "the pattern is nested too deeply"
    try:
        re.compile(pattern)
    except Exception:
        return "the pattern is not a valid regular expression"
    return None


# ============================================================================== schema walking


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)[:_UNTRUSTED_MAX]
    return text[:_UNTRUSTED_MAX]


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return "it is not a string"
    if len(value) > TEXT_MAX_CHARS:
        return f"it is longer than {TEXT_MAX_CHARS} characters"
    if _CONTROL.search(value):
        return "it contains control characters"
    return None


def _too_deep(obj: Any, limit: int) -> bool:
    """True if ``obj`` nests containers deeper than ``limit`` (iterative: no recursion)."""
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, Mapping):
            if depth > limit:
                return True
            stack.extend((v, depth + 1) for v in cur.values())
        elif isinstance(cur, list | tuple):
            if depth > limit:
                return True
            stack.extend((v, depth + 1) for v in cur)
    return False


def _ref_name(ref: Any) -> str | None:
    if not isinstance(ref, str):
        return None
    m = _LOCAL_REF.match(ref)
    if m is None:
        return None
    return parse_pointer("/" + m.group(1))[0]


def _refs_in(obj: Any) -> Iterator[str]:
    """Every local def name referenced anywhere inside ``obj`` (``$ref`` values)."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k == "$ref":
                name = _ref_name(v)
                if name is not None:
                    yield name
            else:
                yield from _refs_in(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _refs_in(v)


def _cyclic_defs(defs: Mapping[str, Any]) -> set[str]:
    """The ``$defs`` names on a reference cycle (each one could expand forever)."""
    graph = {name: set(_refs_in(body)) & set(defs) for name, body in defs.items()}
    cyclic: set[str] = set()
    for start in graph:
        # is ``start`` reachable from itself?
        seen: set[str] = set()
        todo = list(graph[start])
        while todo:
            cur = todo.pop()
            if cur == start:
                cyclic.add(start)
                break
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(graph.get(cur, ()))
    return cyclic


class _Walker:
    def __init__(self, mode: SafetyMode, where: str, defs: Mapping[str, Any]) -> None:
        self.mode: SafetyMode = mode
        self.where = where
        self.issues: list[Issue] = []
        self.props = 0
        self.defs = set(defs)
        self.cyclic = _cyclic_defs(defs)

    def flag(self, path: Sequence[str | int], message: str, value: Any = None) -> None:
        self.issues.append(
            Issue(
                code=ErrorCode.SCHEMA_UNSAFE,
                severity=Severity.ERROR if self.mode == "refuse" else Severity.WARNING,
                pointer=self.where + json_pointer(*path),
                message=message,
                suggested_action=None
                if self.mode == "refuse"
                else "The keyword was removed from the schema.",
                untrusted_text=None if value is None else _clip(value),
                context={"mode": self.mode},
            )
        )

    def schema(self, node: Any, path: list[str | int], depth: int) -> JsonValue:
        if isinstance(node, bool):
            return node
        if not isinstance(node, Mapping):
            self.flag(path, "A subschema must be an object or a boolean.", node)
            return {}
        if depth > SCHEMA_MAX_DEPTH:
            self.flag(path, f"The schema is nested deeper than {SCHEMA_MAX_DEPTH} levels.")
            return {}
        out: dict[str, JsonValue] = {}
        for key, value in node.items():
            here = [*path, key]
            if not isinstance(key, str):
                self.flag(path, "A keyword must be a string.", key)
                continue
            kept = self.keyword(key, value, here, depth, at_root=not path)
            if kept is not _DROP:
                out[key] = kept
        return out

    def keyword(
        self, key: str, value: Any, here: list[str | int], depth: int, *, at_root: bool
    ) -> Any:
        if key not in ALLOWED_KEYWORDS:
            if key.startswith("x-ui-"):
                self.flag(here, f"The form keyword {key!r} is not supported.", key)
            else:
                self.flag(here, f"The keyword {key!r} is not allowed in a schema.", key)
            return _DROP
        if key in _SCHEMA_MAPS:
            if not isinstance(value, Mapping):
                self.flag(here, f"The keyword {key!r} must hold an object.", value)
                return _DROP
            if key == "$defs" and not at_root:
                self.flag(here, "The '$defs' keyword is only allowed at the top of the schema.")
                return _DROP
            sub: dict[str, JsonValue] = {}
            for name, body in value.items():
                if key == "properties":
                    self.props += 1
                    if self.props > SCHEMA_MAX_PROPS:
                        self.flag(
                            [*here, name],
                            f"The schema defines more than {SCHEMA_MAX_PROPS} properties.",
                        )
                        continue
                if key == "patternProperties":
                    reason = is_safe_regex(name)
                    if reason is not None:
                        self.flag([*here, name], f"The property pattern is not safe: {reason}.",
                                  name)  # fmt: skip
                        continue
                sub[name] = self.schema(body, [*here, name], depth + 1)
            return sub
        if key in _SCHEMA_LISTS:
            if not isinstance(value, list):
                self.flag(here, f"The keyword {key!r} must hold a list.", value)
                return _DROP
            return [self.schema(body, [*here, i], depth + 1) for i, body in enumerate(value)]
        if key in _SCHEMA_ONE:
            return self.schema(value, here, depth + 1)
        if key in _TEXT:
            reason = _plain_text(value)
            if reason is not None:
                self.flag(here, f"The {key!r} text is not allowed: {reason}.", value)
                return _DROP
            return value
        if key == "pattern":
            reason = is_safe_regex(value)
            if reason is not None:
                self.flag(here, f"The pattern is not safe: {reason}.", value)
                return _DROP
            return value
        if key == "enum":
            if not isinstance(value, list) or len(value) > ENUM_MAX_ITEMS:
                self.flag(here, f"An enum must be a list of at most {ENUM_MAX_ITEMS} values.")
                return _DROP
            long = [v for v in value if len(v if isinstance(v, str) else json.dumps(v))
                    > ENUM_ITEM_MAX_CHARS]  # fmt: skip
            if long:
                self.flag(
                    here,
                    f"An enum value is longer than {ENUM_ITEM_MAX_CHARS} characters.",
                    long[0],
                )
                return _DROP
            return value
        if key == "format":
            if value not in ALLOWED_FORMATS:
                self.flag(here, "Only the formats 'date' and 'date-time' are allowed.", value)
                return _DROP
            return value
        if key == "$ref":
            name = _ref_name(value)
            if name is None:
                self.flag(here, "A '$ref' must point to '#/$defs/<name>' in the same schema.",
                          value)  # fmt: skip
                return _DROP
            if name not in self.defs:
                self.flag(here, "The '$ref' points to a definition that does not exist.", value)
                return _DROP
            if name in self.cyclic:
                self.flag(here, "The '$ref' is part of a reference cycle.", value)
                return _DROP
            return value
        if key == "$schema":
            if not at_root or value != _DRAFT_2020_12:
                msg = f"The '$schema' keyword must be {_DRAFT_2020_12!r}, at the top only."
                self.flag(here, msg, value)
                return _DROP
            return value
        if key == "discriminator":
            return self.discriminator(value, here)
        if key == "x-ui-widget":
            if value not in UI_WIDGETS:
                self.flag(here, "The form widget is not supported.", value)
                return _DROP
            return value
        if key == "x-ui-level":
            if value not in UI_LEVELS:
                self.flag(here, "The form level must be 'basic' or 'advanced'.", value)
                return _DROP
            return value
        if key == "x-ui-group":
            if not isinstance(value, str) or len(value) > 64 or _CONTROL.search(value):
                self.flag(here, "The form group must be a short text.", value)
                return _DROP
            return value
        if key == "x-ui-order":
            if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 10_000:
                self.flag(here, "The form order must be an integer.", value)
                return _DROP
            return value
        return value  # the other value keywords: their size is in the total budget

    def discriminator(self, value: Any, here: list[str | int]) -> Any:
        """OpenAPI ``discriminator`` (Pydantic writes it): a hint the validator ignores."""
        ok = isinstance(value, Mapping) and isinstance(value.get("propertyName"), str)
        mapping = value.get("mapping", {}) if ok else None
        if ok and isinstance(mapping, Mapping):
            for target in mapping.values():
                name = _ref_name(target)
                if name is None or name not in self.defs:
                    ok = False
        if not ok or not isinstance(mapping, Mapping) or set(value) - {"propertyName", "mapping"}:
            self.flag(here, "The discriminator must name a property and local definitions.",
                      value)  # fmt: skip
            return _DROP
        return dict(value)


def sanitize_schema(
    schema: Mapping[str, Any], *, mode: SafetyMode, where: str
) -> tuple[dict[str, JsonValue], list[Issue]]:
    """Check a JSON Schema against the safety rules; return the safe schema and the issues.

    ``where`` is the JSON pointer of the schema inside the document it came from (for example
    ``/models/3/manifest/settings_schema``); issue pointers start with it. In ``strip`` mode
    unsafe parts are removed (issues are warnings); in ``refuse`` mode every finding is an
    error (the returned schema is the stripped one, for display only). A schema too large or
    too deeply nested to inspect comes back as ``{}``.
    """

    def whole(message: str) -> tuple[dict[str, JsonValue], list[Issue]]:
        w = _Walker(mode, where, {})
        w.flag([], message)
        return {}, w.issues

    if not isinstance(schema, Mapping):
        return whole("The schema must be a JSON object.")
    if _too_deep(schema, _JSON_MAX_NESTING):
        return whole(f"The schema nests objects and lists deeper than {_JSON_MAX_NESTING}.")
    try:
        size = len(
            json.dumps(schema, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        )
    except (TypeError, ValueError):
        return whole("The schema is not plain JSON.")
    if size > SCHEMA_MAX_BYTES:
        return whole(f"The schema is larger than {SCHEMA_MAX_BYTES // 1024} KiB.")
    defs = schema.get("$defs", {})
    walker = _Walker(mode, where, defs if isinstance(defs, Mapping) else {})
    out = walker.schema(schema, [], 1)
    assert isinstance(out, dict)
    try:
        Draft202012Validator.check_schema(out)
    except SchemaError as exc:
        walker.flag(
            [str(p) for p in exc.path],
            "The schema is not a valid JSON Schema (2020-12).",
            exc.message,
        )
        out = {}
    return out, walker.issues


def sanitize_settings_schema(schema: Mapping[str, Any]) -> dict[str, JsonValue] | None:
    """A plot recipe's settings schema made safe for the catalog (``strip`` mode), or ``None``
    when it is still larger than :data:`SETTINGS_SCHEMA_MAX_BYTES` (the entry then has none)."""
    out, _ = sanitize_schema(schema, mode="strip", where="")
    if not out:
        return None
    size = len(json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode())
    return out if size <= SETTINGS_SCHEMA_MAX_BYTES else None


def require_safe_schema(schema: Mapping[str, Any], *, where: str) -> None:
    """Refuse an unsafe schema (``refuse`` mode): :class:`~mapi_spec.errors.MapiError` with
    code ``SCHEMA_UNSAFE`` and every finding as an issue."""
    _, issues = sanitize_schema(schema, mode="refuse", where=where)
    if issues:
        raise MapiError(
            "The JSON Schema uses features that are not safe. Remove them and try again.",
            code=ErrorCode.SCHEMA_UNSAFE,
            pointer=issues[0].pointer,
            issues=issues,
        )
