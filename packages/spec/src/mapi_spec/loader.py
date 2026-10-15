"""Strict YAML 1.2 / JSON loading, apiVersion upgrades, canonical JSON and spec hashing.

The loader is safe for hostile input:

* size ≤ 256 KB, nesting depth ≤ 32, ≤ 10 000 nodes (all configurable via :class:`Limits`);
* YAML anchors, aliases, explicit tags (``!foo``, ``!!python/...``), ``%TAG`` directives and
  multiple documents are rejected;
* plain scalars are resolved with the **YAML 1.2 core schema** (``yes``/``on``/``0777``/dates
  stay strings), NaN/Infinity are rejected, duplicate keys are rejected and every mapping key
  must be a string;
* JSON input is parsed with the same limits.

:func:`load_document` returns a typed document (discriminated on ``kind``) after running the
apiVersion upgrade chain (the API accepts N and N-1). :func:`canonical_json` and
:func:`spec_hash` give a normalised, deterministic serialisation for immutable revisions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from mapi_spec.errors import (
    ErrorCode,
    Issue,
    PayloadTooLarge,
    SpecParseError,
    UnsupportedApiVersion,
    ValidationFailed,
    json_pointer,
)
from mapi_spec.spec import API_VERSION, DOCUMENT_TYPES, ModelSpec, ScoreRequest

__all__ = [
    "ACCEPTED_API_VERSIONS",
    "Limits",
    "canonical_json",
    "canonical_value",
    "dump_yaml",
    "load_document",
    "load_model_spec",
    "load_score_request",
    "parse_document",
    "parse_json",
    "parse_yaml",
    "register_upgrader",
    "spec_hash",
    "upgrade",
    "validation_issues",
]


@dataclass(frozen=True, slots=True)
class Limits:
    """Parser limits for untrusted documents."""

    max_bytes: int = 256 * 1024
    max_depth: int = 32
    max_nodes: int = 10_000
    max_scalar_bytes: int = 64 * 1024


DEFAULT_LIMITS = Limits()

# ------------------------------------------------------------------- YAML 1.2 core schema

_NULL_RE = re.compile(r"^(?:~|null|Null|NULL)?$")
_BOOL_TRUE = frozenset({"true", "True", "TRUE"})
_BOOL_FALSE = frozenset({"false", "False", "FALSE"})
_INT10_RE = re.compile(r"^[-+]?[0-9]+$")
_INT8_RE = re.compile(r"^0o[0-7]+$")
_INT16_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")
_INF_NAN_RE = re.compile(r"^(?:[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")


def resolve_plain_scalar(text: str) -> Any:
    """Resolve an untagged plain scalar per the YAML 1.2 core schema."""
    if _NULL_RE.match(text):
        return None
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    if _INT10_RE.match(text):
        return int(text)
    if _INT8_RE.match(text):
        return int(text[2:], 8)
    if _INT16_RE.match(text):
        return int(text[2:], 16)
    if _FLOAT_RE.match(text):
        return float(text)
    if _INF_NAN_RE.match(text):
        raise ValueError("NaN and Infinity are not allowed in documents")
    return text


class _Budget:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.nodes = 0

    def node(self, depth: int, where: str) -> None:
        self.nodes += 1
        if self.nodes > self.limits.max_nodes:
            raise PayloadTooLarge(
                f"document has more than {self.limits.max_nodes} nodes", pointer=where
            )
        if depth > self.limits.max_depth:
            raise PayloadTooLarge(
                f"document nesting exceeds depth {self.limits.max_depth}", pointer=where
            )


def _check_size(text: str | bytes, limits: Limits) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > limits.max_bytes:
        raise PayloadTooLarge(f"document is larger than {limits.max_bytes} bytes")
    if isinstance(text, bytes):
        try:
            return text.decode("utf-8")
        except UnicodeDecodeError:
            raise SpecParseError("document is not valid UTF-8") from None
    return text


def _mark(event: yaml.Event) -> str:
    m = event.start_mark
    return f"line {m.line + 1}, column {m.column + 1}" if m is not None else "unknown position"


def parse_yaml(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> Any:
    """Parse exactly one YAML 1.2 document into plain JSON-compatible Python values."""
    src = _check_size(text, limits)
    budget = _Budget(limits)
    try:
        events: Iterator[yaml.Event] = yaml.parse(src, Loader=yaml.SafeLoader)
        return _build(events, budget, limits)
    except yaml.YAMLError as exc:
        raise SpecParseError(f"invalid YAML: {_yaml_problem(exc)}") from None
    except RecursionError:  # pragma: no cover - depth limit triggers first
        raise PayloadTooLarge("document nesting is too deep") from None


def _yaml_problem(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError):
        where = ""
        if exc.problem_mark is not None:
            where = f" at line {exc.problem_mark.line + 1}, column {exc.problem_mark.column + 1}"
        return f"{exc.problem or exc.context or 'syntax error'}{where}"
    return "syntax error"


def _build(events: Iterator[yaml.Event], budget: _Budget, limits: Limits) -> Any:
    docs = 0
    result: Any = None
    stack: list[tuple[str, Any, Any]] = []  # (kind, container, pending_key)
    path: list[str | int] = []

    def emit(value: Any, ev: yaml.Event) -> bool:
        """Attach a finished value to its parent; True if it was the document root."""
        nonlocal result
        if not stack:
            result = value
            return True
        kind, container, pending = stack[-1]
        if kind == "seq":
            container.append(value)
        elif pending is _NO_KEY:
            if not isinstance(value, str):
                raise SpecParseError(f"mapping keys must be strings ({_mark(ev)})")
            if value in container:
                raise SpecParseError(
                    f"duplicate key {value!r} ({_mark(ev)})", pointer=json_pointer(*path)
                )
            stack[-1] = (kind, container, value)
        else:
            container[pending] = value
            stack[-1] = (kind, container, _NO_KEY)
        return False

    for ev in events:
        if isinstance(ev, yaml.StreamStartEvent | yaml.StreamEndEvent):
            continue
        if isinstance(ev, yaml.DocumentStartEvent):
            docs += 1
            if docs > 1:
                raise SpecParseError("only one YAML document is allowed")
            if ev.tags:
                raise SpecParseError(f"%TAG directives are not allowed ({_mark(ev)})")
            if ev.version is not None and ev.version != (1, 2):
                raise SpecParseError("only %YAML 1.2 documents are accepted")
            continue
        if isinstance(ev, yaml.DocumentEndEvent):
            continue
        if isinstance(ev, yaml.AliasEvent):
            raise SpecParseError(f"YAML aliases are not allowed ({_mark(ev)})")
        if isinstance(ev, yaml.NodeEvent) and ev.anchor is not None:
            raise SpecParseError(f"YAML anchors are not allowed ({_mark(ev)})")
        if isinstance(ev, yaml.ScalarEvent | yaml.CollectionStartEvent) and ev.tag is not None:
            raise SpecParseError(f"YAML tags are not allowed ({_mark(ev)})")
        if isinstance(ev, yaml.ScalarEvent):
            budget.node(len(stack) + 1, json_pointer(*path))
            if len(ev.value) > limits.max_scalar_bytes:
                raise PayloadTooLarge("a scalar exceeds the maximum length")
            if ev.style is None or ev.style == "":
                try:
                    value = resolve_plain_scalar(ev.value)
                except ValueError as exc:
                    raise SpecParseError(f"{exc} ({_mark(ev)})") from None
            else:
                value = ev.value
            emit(value, ev)
            _advance_path(stack, path)
            continue
        if isinstance(ev, yaml.SequenceStartEvent | yaml.MappingStartEvent):
            budget.node(len(stack) + 1, json_pointer(*path))
            if stack and stack[-1][0] == "map" and stack[-1][2] is _NO_KEY:
                raise SpecParseError(f"mapping keys must be strings ({_mark(ev)})")
            if isinstance(ev, yaml.SequenceStartEvent):
                stack.append(("seq", [], None))
            else:
                stack.append(("map", {}, _NO_KEY))
            continue
        if isinstance(ev, yaml.SequenceEndEvent | yaml.MappingEndEvent):
            _kind, container, _pending = stack.pop()
            emit(container, ev)
            _advance_path(stack, path)
            continue
    if docs == 0:
        raise SpecParseError("empty document")
    return result


class _NoKey:
    def __repr__(self) -> str:  # pragma: no cover
        return "<no key>"


_NO_KEY: Any = _NoKey()


def _advance_path(stack: list[tuple[str, Any, Any]], path: list[str | int]) -> None:
    # Best-effort pointer for limit errors: recompute from the open containers.
    path.clear()
    for kind, container, pending in stack:
        if kind == "seq":
            path.append(len(container))
        elif pending is not _NO_KEY:
            path.append(pending)


def parse_json(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> Any:
    """Parse JSON with the same limits and rules (no duplicate keys, no NaN)."""
    src = _check_size(text, limits)

    def no_dups(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise SpecParseError(f"duplicate key {k!r}")
            out[k] = v
        return out

    def bad_constant(name: str) -> Any:
        raise SpecParseError("NaN and Infinity are not allowed in documents")

    try:
        value = json.loads(src, object_pairs_hook=no_dups, parse_constant=bad_constant)
    except json.JSONDecodeError as exc:
        raise SpecParseError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from None
    except RecursionError:
        raise PayloadTooLarge("document nesting is too deep") from None
    check_limits(value, limits)
    return value


def check_limits(value: Any, limits: Limits = DEFAULT_LIMITS) -> None:
    """Enforce depth/node limits (and JSON-compatibility) on an already-parsed value."""
    budget = _Budget(limits)
    todo: list[tuple[Any, int, tuple[str | int, ...]]] = [(value, 1, ())]
    while todo:
        node, depth, path = todo.pop()
        budget.node(depth, json_pointer(*path))
        if isinstance(node, dict):
            for k, v in node.items():
                if not isinstance(k, str):
                    raise SpecParseError(
                        "mapping keys must be strings", pointer=json_pointer(*path)
                    )
                todo.append((v, depth + 1, (*path, k)))
        elif isinstance(node, list):
            todo.extend((v, depth + 1, (*path, i)) for i, v in enumerate(node))
        elif isinstance(node, float) and not math.isfinite(node):
            raise SpecParseError("NaN and Infinity are not allowed", pointer=json_pointer(*path))
        elif not (node is None or isinstance(node, str | int | float | bool)):
            raise SpecParseError(f"unsupported value type {type(node).__name__}")


def parse_text(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> Any:
    """Parse JSON if the text looks like JSON, otherwise strict YAML."""
    head = text.lstrip()[:1]
    if head in ("{", "[", b"{", b"["):
        return parse_json(text, limits)
    return parse_yaml(text, limits)


# ------------------------------------------------------------------------ apiVersion upgrades

Upgrader = Callable[[dict[str, Any]], dict[str, Any]]
_UPGRADERS: dict[str, tuple[str, Upgrader]] = {}

ACCEPTED_API_VERSIONS: tuple[str, ...] = (API_VERSION,)
"""apiVersions accepted on input (current N, plus N-1 once a successor ships)."""


def register_upgrader(from_version: str, to_version: str, fn: Upgrader) -> None:
    """Register a converter ``from_version -> to_version`` (used by ``mapi spec upgrade``)."""
    _UPGRADERS[from_version] = (to_version, fn)


def upgrade(
    doc: Mapping[str, Any],
    *,
    target: str = API_VERSION,
    accepted: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Upgrade a raw document to ``target`` through registered converters.

    Raises :class:`UnsupportedApiVersion` if the version is not accepted or no chain exists.
    """
    accepted = ACCEPTED_API_VERSIONS if accepted is None else accepted
    out = dict(doc)
    version = out.get("apiVersion")
    if not isinstance(version, str):
        raise UnsupportedApiVersion("missing apiVersion", pointer="/apiVersion")
    if version not in accepted and version != target:
        raise UnsupportedApiVersion(
            f"apiVersion {version!r} is not accepted (accepted: {', '.join(accepted)})",
            pointer="/apiVersion",
        )
    seen: set[str] = set()
    while version != target:
        if version in seen or version not in _UPGRADERS:
            raise UnsupportedApiVersion(
                f"no upgrade path from {version!r} to {target!r}", pointer="/apiVersion"
            )
        seen.add(version)
        nxt, fn = _UPGRADERS[version]
        out = fn(dict(out))
        out["apiVersion"] = nxt
        version = nxt
    return out


# ------------------------------------------------------------------------- typed documents


def _pointer_from_loc(loc: tuple[int | str, ...], data: Any, *, missing: bool = False) -> str:
    """Map a pydantic error location to a JSON pointer into ``data``.

    Segments that do not exist in ``data`` are union/discriminator tags or validator names
    and are skipped — except the final segment of a ``missing`` error, which names the
    absent field.
    """
    parts: list[str | int] = []
    node = data
    for i, seg in enumerate(loc):
        in_dict = isinstance(node, dict) and isinstance(seg, str) and seg in node
        in_list = isinstance(node, list) and isinstance(seg, int) and 0 <= seg < len(node)
        if in_dict or in_list:
            parts.append(seg)
            node = node[seg]
        elif missing and i == len(loc) - 1:
            parts.append(seg)
    return json_pointer(*parts)


def validation_issues(exc: ValidationError, data: Any, base: str = "") -> list[Issue]:
    """Convert a pydantic ``ValidationError`` into :class:`Issue` objects with pointers."""
    issues: list[Issue] = []
    for err in exc.errors(include_url=False, include_input=False):
        msg = str(err["msg"]).removeprefix("Value error, ")
        issues.append(
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                pointer=base
                + _pointer_from_loc(tuple(err["loc"]), data, missing=err["type"] == "missing"),
                message=msg[:2000],
            )
        )
    return issues


def parse_document(data: Any, *, accepted: tuple[str, ...] | None = None) -> Any:
    """Validate a parsed mapping as a typed document (``ModelSpec``, ``ScoreRequest`` ...)."""
    if not isinstance(data, dict):
        raise ValidationFailed(
            [Issue(code=ErrorCode.SCHEMA_INVALID, message="a document must be a mapping")]
        )
    data = upgrade(data, accepted=accepted)
    kind = data.get("kind")
    cls = DOCUMENT_TYPES.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise ValidationFailed(
            [
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/kind",
                    message="kind must be one of " + ", ".join(DOCUMENT_TYPES),
                )
            ]
        )
    try:
        return cls.model_validate(data)
    except ValidationError as exc:
        raise ValidationFailed(validation_issues(exc, data)) from None


def load_document(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> Any:
    """Parse YAML/JSON text and validate it as a typed document."""
    return parse_document(parse_text(text, limits))


def _load_as[D: BaseModel](text: str | bytes, cls: type[D], limits: Limits) -> D:
    doc = load_document(text, limits)
    if not isinstance(doc, cls):
        raise ValidationFailed(
            [
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/kind",
                    message=f"expected kind {cls.__name__}",
                )
            ]
        )
    return doc


def load_model_spec(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> ModelSpec:
    return _load_as(text, ModelSpec, limits)


def load_score_request(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> ScoreRequest:
    return _load_as(text, ScoreRequest, limits)


# --------------------------------------------------------------------------- canonical form


def canonical_value(value: Any) -> Any:
    """Normalise JSON values: integral floats become ints (JCS-style), keys stay strings."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN/Infinity cannot be canonicalised")
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {str(k): canonical_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonical_value(v) for v in value]
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def _dump(obj: BaseModel | Mapping[str, Any]) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    return dict(obj)


def canonical_json(obj: BaseModel | Mapping[str, Any]) -> str:
    """Deterministic JSON: defaults omitted, keys sorted, no whitespace, UTF-8, ints for
    integral floats. Equal documents (after normalisation) produce identical text."""
    data = canonical_value(_dump(obj))
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_hash(obj: BaseModel | Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON (the immutable revision identity)."""
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------- dumping


class _StrictDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _repr_str(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    try:
        plain_ok = isinstance(resolve_plain_scalar(value), str)
    except ValueError:
        plain_ok = False
    style = None if plain_ok else "'"
    if "\n" in value:
        style = "|"
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_StrictDumper.add_representer(str, _repr_str)


def dump_yaml(obj: BaseModel | Mapping[str, Any], *, exclude_defaults: bool = True) -> str:
    """YAML text that :func:`parse_yaml` reads back to the same value (no anchors/tags)."""
    if isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json", by_alias=True, exclude_defaults=exclude_defaults)
    else:
        data = dict(obj)
    text: str = yaml.dump(
        data, Dumper=_StrictDumper, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return text
