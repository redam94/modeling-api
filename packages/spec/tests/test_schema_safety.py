"""v0.18 JSON-Schema safety: the ReDoS corpus, the keyword rules, strip vs refuse, and every
built-in schema passing untouched."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.schema_safety import (
    ALLOWED_KEYWORDS,
    REGEX_MAX_LEN,
    SCHEMA_MAX_BYTES,
    is_safe_regex,
    require_safe_schema,
    sanitize_schema,
    sanitize_settings_schema,
)
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS

# ================================================================================ ReDoS corpus

EVIL: list[str] = [
    r"(a+)+$",
    r"(a|aa)*c",
    r"(\w+\s?)*$",
    r"(.*a){20}",
    r"(a)\1",
    r"(?P<x>a)(?P=x)",
    r"(a)?(?(1)b|c)",
    r"(?=a)b",
    r"(?!a)b",
    r"(?<=a)b",
    r"(?<!a)b",
    r"a++",
    r"[a-z]*+",
    r"(?>a+)b",
    r"(?>a|ab)c",
    r"(a*)*",
    r"(a+)*b",
    r"([a-z]+)*$",
    r"(x+x+)+y",
    r"((a+)+)+",
    r"(\d+)+$",
    r"(\s*a\s*)*$",
    r"^(a|b|ab)*c$",
    r"^(ab|a)(ab|a)+$",
    r"(.*)*x",
    r"(.+)+",
    r"^([a-zA-Z0-9])(([\-.]|[_]+)?([a-zA-Z0-9]+))*(@){1}[a-z0-9]+[.]{1}(([a-z]{2,3})|([a-z]{2,3}[.]{1}[a-z]{2,3}))$",
    r"^(([a-z])+.)+[A-Z]([a-z])+$",
    r"(a?){30}a{30}",
    r"a{1001}",
    r"a{0,5000}",
    r"(ab|a)+",
    r"(a{1,10}){1,10}",
    r"(\d*\.?\d*)+",
    r"^(\.\w+)+.*\1$",
    r"(a|a)*",
    r"(,?\d+)+",
    r"^(\s+|\S+)+$",
    r"(\w+\.?)+@",
    r"(?:a+){2,}",
    r"((ab)*)+",
    r"(a+a)+",
    r"(.*,){11}P",
    "a" * (REGEX_MAX_LEN + 1),
    r"(",
    r"[a-",
    r"a{2,1}",
]
"""Catastrophic, unsupported or invalid patterns: every one must be refused."""

BUILTIN_PATTERNS: list[str] = [
    r"^(\d{4}-\d{2}-\d{2}|-?\d+)$",
    r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
    r"^[a-z][a-z0-9_]{0,40}$",
    r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$",
    r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+(@\S+)?$",
    r"^[a-z0-9][a-z0-9_-]{0,62}$",
    r"^[A-Za-z_][A-Za-z0-9_/]{0,127}$",
    r"^workflow://\S+$",
    r"^[A-Z]{3}$",
    r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$",
]
"""Every ``pattern`` the built-in catalog used when v0.18 started (and the slug pattern)."""

NORMAL: list[str] = [
    *BUILTIN_PATTERNS,
    r"^\d{4}-\d{2}-\d{2}$",
    r"^[a-z]+$",
    r"abc",
    r"^$",
    r"^\w+$",
    r"^#[0-9a-fA-F]{6}$",
    r"^[^\s]+$",
    r"^(?:[a-z]+)$",
    r"^\d+(\.\d+)?$",
    r"^[+-]?\d+$",
    r"^v\d+\.\d+\.\d+$",
    r"^\d+(\.\d+)*$",
    r"^(\d+\.)+\d+$",
    r"^[a-z]{2}(-[A-Z]{2})?$",
    r"^https?$",
    r"(?i)^[a-z]+$",
    r"^\S+@\S+$",
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$",
    r"^[A-Z][a-z]+(?: [A-Z][a-z]+)*$",
    r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$",
    r"^[a-z]{1,1000}$",
    r"^[\w.-]+$",
    r"^\s*$",
    r"^[0-9a-f]{64}$",
    r"^sha256:[0-9a-f]{64}$",
    r"^(?:yes|no)$",
    r"^(red|green|blue)$",
    r"^\d{1,3}(,\d{3})*$",
    r"^[a-z]+(-[a-z]+)*$",
    r"^[a-z]+(_[a-z0-9]+)*$",
    r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$",
    r"^P(\d+D)?$",
    r"^[^@\s]{1,64}@[^@\s]{1,255}$",
    r"^\+?[0-9 ]{6,20}$",
    r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$",
    r"^/[a-z0-9/_-]*$",
    r"^\d{2}:\d{2}$",
    r"^[A-Z]{2,3}-\d{1,6}$",
    r"^.{0,300}$",
    r"^(?:\d{3}-){2}\d{4}$",
]
"""Ordinary patterns schema authors write: every one must be accepted."""

_ADVERSARIAL = ["a" * 255 + "!", "1" * 255 + "x", "." * 255, " " * 255 + "@", "a." * 127 + "!"]


def test_the_corpus_is_large_enough() -> None:
    assert len(EVIL) >= 40 and len(NORMAL) >= 40
    assert len(set(EVIL)) == len(EVIL) and len(set(NORMAL)) == len(NORMAL)


@pytest.mark.parametrize("pattern", EVIL)
def test_evil_patterns_are_refused_quickly(pattern: str) -> None:
    start = time.perf_counter()
    reason = is_safe_regex(pattern)
    elapsed = time.perf_counter() - start
    assert reason is not None, pattern
    assert reason[0].islower() and not reason.endswith(".")  # it is embedded in a sentence
    assert elapsed < 0.005, (pattern, elapsed)


@pytest.mark.parametrize("pattern", NORMAL)
def test_normal_patterns_are_accepted_and_fast_to_run(pattern: str) -> None:
    start = time.perf_counter()
    assert is_safe_regex(pattern) is None, (pattern, is_safe_regex(pattern))
    assert time.perf_counter() - start < 0.005
    compiled = re.compile(pattern)
    for text in _ADVERSARIAL:  # what the validator does with an accepted pattern
        start = time.perf_counter()
        compiled.search(text)
        assert time.perf_counter() - start < 0.05, (pattern, text[:10])


def test_a_256_character_pattern_is_checked_in_time() -> None:
    worst = [
        "(" * 85 + "a" + ")*" * 85,
        "(?:" * 60 + "a" + ")" * 60 + "b" * 15,
        "[a-z]" * 51 + "x",
        "(a|b)" * 51 + "c",
        r"(\.[a-z]+)+" * 23,
    ]
    for pattern in worst:
        pattern = pattern[:REGEX_MAX_LEN]
        start = time.perf_counter()
        is_safe_regex(pattern)
        assert time.perf_counter() - start < 0.005, pattern[:40]


def test_reasons_are_specific() -> None:
    assert "backreference" in (is_safe_regex(r"(a)\1") or "")
    assert "lookahead" in (is_safe_regex(r"(?=a)") or "")
    assert "possessive" in (is_safe_regex(r"a++") or "")
    assert "nested quantifiers" in (is_safe_regex(r"(a+)+") or "")
    assert "alternation" in (is_safe_regex(r"(ab|cd)+") or "")
    # single characters: Python's parser turns the alternation into one character class
    assert is_safe_regex(r"(a|b)+") is None
    assert "1000" in (is_safe_regex(r"a{1001}") or "")
    assert "longer than" in (is_safe_regex("a" * 300) or "")
    assert "not a valid" in (is_safe_regex("(") or "")
    assert "in a row" in (is_safe_regex(r"\d*\d*!") or "")
    assert "more than two" in (is_safe_regex(r".*x.*x.*") or "")


def test_sibling_quantifiers_cannot_hang_the_validator() -> None:
    # v0.18 review: '\d*' x 10 + '!' was accepted and took seconds on 25 digits
    for pattern in [r"\d*" * 10 + "!", r"\d*" * 60 + "!", "a*" * 8 + "b", ".*" * 6 + "="]:
        assert is_safe_regex(pattern) is not None, pattern
    # one overlap across a mandatory character splits only there: allowed and fast
    for pattern in [r"^\S+@\S+$", r".*x.*="]:
        assert is_safe_regex(pattern) is None, pattern
        start = time.perf_counter()
        re.search(pattern, "x" * 300)
        assert time.perf_counter() - start < 0.5, pattern


# ============================================================================== schema rules


def _codes(issues: list[Any]) -> set[str]:
    return {i.pointer for i in issues}


HOSTILE: dict[str, Any] = {
    "type": "object",
    "$id": "https://evil.example/schema.json",
    "properties": {
        "a": {"type": "string", "pattern": r"(a+)+$"},
        "b": {"$ref": "https://evil.example/remote.json"},
        "c": {"type": "string", "format": "uri"},
        "d": {"type": "string", "x-ui-widget": "html"},
        "e": {"type": "string", "description": "x" * 2001},
        "f": {"$dynamicRef": "#meta"},
        "g": {"type": "integer", "x-ui-level": "advanced", "x-ui-order": 2},
    },
    "patternProperties": {r"(\w+\s?)*$": {"type": "string"}, r"^x_": {"type": "number"}},
}


def test_strip_mode_removes_what_is_unsafe_and_warns() -> None:
    out, issues = sanitize_schema(HOSTILE, mode="strip", where="/models/0/settings_schema")
    assert out == {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {},
            "c": {"type": "string"},
            "d": {"type": "string"},
            "e": {"type": "string"},
            "f": {},
            "g": {"type": "integer", "x-ui-level": "advanced", "x-ui-order": 2},
        },
        "patternProperties": {r"^x_": {"type": "number"}},
    }
    assert {i.severity for i in issues} == {Severity.WARNING}
    assert {i.code for i in issues} == {ErrorCode.SCHEMA_UNSAFE}
    assert _codes(issues) == {
        "/models/0/settings_schema/$id",
        "/models/0/settings_schema/properties/a/pattern",
        "/models/0/settings_schema/properties/b/$ref",
        "/models/0/settings_schema/properties/c/format",
        "/models/0/settings_schema/properties/d/x-ui-widget",
        "/models/0/settings_schema/properties/e/description",
        "/models/0/settings_schema/properties/f/$dynamicRef",
        "/models/0/settings_schema/patternProperties/(\\w+\\s?)*$",
    }
    # what the plugin wrote is carried as untrusted text, clipped
    pattern_issue = next(i for i in issues if i.pointer.endswith("/a/pattern"))
    assert pattern_issue.untrusted_text == r"(a+)+$"
    assert all(len(i.untrusted_text or "") <= 500 for i in issues)


def test_refuse_mode_reports_errors_and_raises() -> None:
    _, issues = sanitize_schema(HOSTILE, mode="refuse", where="/declarative")
    assert issues and {i.severity for i in issues} == {Severity.ERROR}
    with pytest.raises(MapiError) as info:
        require_safe_schema(HOSTILE, where="/settings")
    assert info.value.code is ErrorCode.SCHEMA_UNSAFE
    assert info.value.pointer.startswith("/settings/")
    assert len(info.value.issues) == len(issues)
    require_safe_schema({"type": "object", "properties": {"n": {"type": "integer"}}}, where="")


@pytest.mark.parametrize(
    ("schema", "pointer"),
    [
        ({"$ref": "#/definitions/x", "definitions": {"x": {}}}, "/$ref"),
        ({"$ref": "#/$defs/missing"}, "/$ref"),
        ({"$ref": "#"}, "/$ref"),
        ({"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
         "/$ref"),
        ({"$defs": {"a": {"properties": {"x": {"$ref": "#/$defs/a"}}}}},
         "/$defs/a/properties/x/$ref"),
        ({"properties": {"x": {"$defs": {"y": {}}}}}, "/properties/x/$defs"),
        ({"$schema": "http://json-schema.org/draft-04/schema#"}, "/$schema"),
        ({"properties": {"x": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}},
         "/properties/x/$schema"),
        ({"enum": list(range(65))}, "/enum"),
        ({"enum": ["x" * 201]}, "/enum"),
        ({"type": "string", "description": "bell \x07"}, "/description"),
        ({"type": "string", "title": 5}, "/title"),
        ({"x-ui-live": True}, "/x-ui-live"),
        ({"x-ui-group": "g" * 65}, "/x-ui-group"),
        ({"x-ui-order": True}, "/x-ui-order"),
        ({"x-ui-level": "expert"}, "/x-ui-level"),
        ({"discriminator": {"propertyName": "k", "mapping": {"a": "http://x"}}}, "/discriminator"),
        ({"unknownKeyword": 1}, "/unknownKeyword"),
        ({"properties": {"x": 5}}, "/properties/x"),
    ],
)  # fmt: skip
def test_each_rule_has_a_pointer(schema: dict[str, Any], pointer: str) -> None:
    _, issues = sanitize_schema(schema, mode="refuse", where="")
    assert pointer in _codes(issues), [(i.pointer, i.message) for i in issues]
    for issue in issues:
        assert issue.message.endswith(".") and issue.message[0].isupper()


def test_size_depth_and_property_caps() -> None:
    big = {"type": "object", "description": "x", "default": "y" * SCHEMA_MAX_BYTES}
    out, issues = sanitize_schema(big, mode="strip", where="/s")
    assert out == {} and [i.pointer for i in issues] == ["/s"]
    deep: dict[str, Any] = {"type": "integer"}
    for _ in range(12):
        deep = {"type": "array", "items": deep}
    out, issues = sanitize_schema(deep, mode="strip", where="")
    assert [i.pointer for i in issues] == ["/items" * 12]
    inner = out
    for _ in range(11):
        inner = inner["items"]  # type: ignore[assignment]
    assert inner["items"] == {}  # type: ignore[index]
    wide = {"type": "object", "properties": {f"p{i}": {"type": "number"} for i in range(260)}}
    out, issues = sanitize_schema(wide, mode="strip", where="")
    assert len(out["properties"]) == 256  # type: ignore[arg-type]
    assert [i.pointer for i in issues] == [f"/properties/p{i}" for i in range(256, 260)]


def _doubling(steps: int, fan: int = 2) -> dict[str, Any]:
    defs: dict[str, Any] = {"d0": {"type": "integer", "minimum": 5}}
    for i in range(1, steps + 1):
        defs[f"d{i}"] = {"allOf": [{"$ref": f"#/$defs/d{i - 1}"}] * fan}
    return {"type": "object", "$defs": defs, "properties": {"x": {"$ref": f"#/$defs/d{steps}"}}}


def test_reference_expansion_is_bounded() -> None:
    # v0.18 review: a 1.7 KB acyclic doubling chain passed and validation ran for hours
    for mode in ("refuse", "strip"):
        start = time.perf_counter()
        out, issues = sanitize_schema(_doubling(24), mode=mode, where="/s")  # type: ignore[arg-type]
        assert time.perf_counter() - start < 0.5
        assert out == {} and [i.pointer for i in issues] == ["/s"]
        assert "'$ref's are followed" in issues[0].message
    assert sanitize_settings_schema(_doubling(24)) is None
    with pytest.raises(MapiError):
        require_safe_schema(_doubling(24), where="/s")
    # wide but shallow: the subschema count budget
    _, issues = sanitize_schema(_doubling(5, fan=8), mode="refuse", where="")
    assert issues and "subschemas" in issues[0].message
    # a long single chain is refused without deep recursion
    defs: dict[str, Any] = {"d0": {"type": "integer"}}
    for i in range(1, 2000):
        defs[f"d{i}"] = {"$ref": f"#/$defs/d{i - 1}"}
    _, issues = sanitize_schema({"$defs": defs, "$ref": "#/$defs/d1999"}, mode="refuse", where="")
    assert issues
    # small reuse is fine
    out, issues = sanitize_schema(_doubling(3), mode="refuse", where="")
    assert not issues and out == _doubling(3)


def test_unparseable_inputs_are_refused_whole_without_recursion() -> None:
    nested: Any = 0
    for _ in range(5000):
        nested = [nested]
    for schema in ({"default": nested}, {"default": float("nan")}, {"default": {1, 2}}):
        out, issues = sanitize_schema(schema, mode="strip", where="/x")
        assert out == {} and len(issues) == 1 and issues[0].pointer == "/x"
    out, issues = sanitize_schema({"type": 5}, mode="strip", where="")
    assert out == {} and "valid JSON Schema" in issues[-1].message
    out, issues = sanitize_schema([], mode="strip", where="")  # type: ignore[arg-type]
    assert out == {} and issues


def test_allowed_constructs_pass_untouched() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "$defs": {"Choice": {"type": "object", "properties": {"kind": {"const": "a"}}}},
        "properties": {
            "when": {"type": "string", "format": "date-time"},
            "day": {"type": "string", "format": "date"},
            "choice": {
                "oneOf": [{"$ref": "#/$defs/Choice"}],
                "discriminator": {"propertyName": "kind", "mapping": {"a": "#/$defs/Choice"}},
            },
            "level": {"type": "integer", "minimum": 0, "maximum": 3, "x-ui-widget": "slider",
                      "x-ui-group": "Advanced", "x-ui-order": 1, "x-ui-level": "basic"},
            "vars": {"type": "array", "items": {"type": "string", "pattern": r"^[a-z]+$"},
                     "x-ui-widget": "var_picker", "uniqueItems": True},
        },
        "required": ["level"],
    }  # fmt: skip
    for mode in ("strip", "refuse"):
        out, issues = sanitize_schema(schema, mode=mode, where="")  # type: ignore[arg-type]
        assert issues == [] and out == schema


def test_allow_list_is_documented_for_the_web_mirror() -> None:
    assert {"x-ui-widget", "x-ui-group", "x-ui-order", "x-ui-level"} <= ALLOWED_KEYWORDS
    assert not {"$id", "$anchor", "$dynamicRef", "$recursiveRef", "definitions"} & ALLOWED_KEYWORDS


def test_settings_schema_budget() -> None:
    small = {"type": "object", "properties": {"title": {"type": "string", "maxLength": 300}}}
    assert sanitize_settings_schema(small) == small
    medium = {"type": "object", "description": "x" * 1000,
              "properties": {f"p{i}": {"type": "string", "description": "y" * 200}
                             for i in range(100)}}  # fmt: skip
    assert len(json.dumps(medium)) > 16_384
    assert sanitize_settings_schema(medium) is None
    assert sanitize_settings_schema({"type": 5}) is None
    stripped = sanitize_settings_schema({"type": "string", "pattern": "(a+)+"})
    assert stripped == {"type": "string"}


# ===================================================================== every built-in schema


def test_every_builtin_node_schema_passes_untouched() -> None:
    for node in NODE_IMPLEMENTATIONS.values():
        out, issues = sanitize_schema(node.params_schema, mode="refuse", where="/params_schema")
        assert issues == [], (node.id, [(i.pointer, i.message) for i in issues])
        assert out == node.params_schema


def test_every_builtin_catalog_schema_passes_untouched() -> None:
    registry = pytest.importorskip("mapi_sdk.registry")
    recipes = pytest.importorskip("mapi_plots.recipes")
    catalog = registry.default_registry().catalog()
    schemas: dict[str, Any] = {}
    for s in catalog.steps:
        schemas[f"step {s.ref}"] = s.settings_schema
    for m in catalog.models:
        schemas[f"model {m.ref} settings"] = m.manifest.settings_schema
        schemas[f"model {m.ref} fit"] = m.manifest.fit_schema
    for r in recipes.all_recipes():
        schemas[f"plot {r.ref}"] = r.settings.model_json_schema()
    assert len(schemas) > 150
    for name, schema in schemas.items():
        out, issues = sanitize_schema(schema, mode="refuse", where="")
        assert issues == [], (name, [(i.pointer, i.message) for i in issues][:3])
        assert out == schema, name


# ============================================================ golden for the web mirror (W2)

GOLDEN = Path(__file__).parent / "golden" / "settings_schemas.json"

GOLDEN_CASES: list[tuple[str, list[str], dict[str, Any]]] = [
    ("plain object", ["keyword"], {"type": "object", "properties": {"n": {"type": "integer"}}}),
    ("unknown keyword", ["keyword"], {"type": "string", "contentEncoding": "base64"}),
    ("remote id", ["keyword"], {"$id": "https://x.example/s", "type": "object"}),
    ("dynamic ref", ["keyword", "ref"], {"properties": {"x": {"$dynamicRef": "#a"}}}),
    ("remote ref", ["ref"], {"properties": {"x": {"$ref": "https://x.example/s"}}}),
    ("local ref", ["ref"], {"$defs": {"A": {"type": "string"}},
                            "properties": {"x": {"$ref": "#/$defs/A"}}}),
    ("missing ref", ["ref"], {"properties": {"x": {"$ref": "#/$defs/B"}}}),
    ("ref cycle", ["ref"], {"$defs": {"A": {"items": {"$ref": "#/$defs/A"}}},
                            "properties": {"x": {"$ref": "#/$defs/A"}}}),
    ("formats", ["format"], {"properties": {"a": {"type": "string", "format": "date"},
                                            "b": {"type": "string", "format": "email"}}}),
    ("ui keywords", ["ui"], {"properties": {
        "a": {"type": "number", "x-ui-widget": "slider", "x-ui-group": "Basics",
              "x-ui-order": 1, "x-ui-level": "basic"},
        "b": {"type": "string", "x-ui-widget": "iframe"},
        "c": {"type": "string", "x-ui-live": True}}}),
    ("text", ["text"], {"title": "Ok", "description": "Line one\nline two",
                        "properties": {"a": {"type": "string", "description": "z" * 2001},
                                       "b": {"type": "string", "title": "nul\u0000"}}}),
    ("html is text", ["text"], {"title": "<script>alert(1)</script>", "type": "string"}),
    ("enum caps", ["enum"], {"properties": {"a": {"enum": [str(i) for i in range(65)]},
                                            "b": {"enum": ["ok", "fine"]}}}),
    ("unsafe pattern", ["regex"], {"properties": {"a": {"type": "string",
                                                        "pattern": "(a+)+$"},
                                                  "b": {"type": "string",
                                                        "pattern": "^[a-z]+$"}}}),
    ("pattern properties", ["regex"], {"patternProperties": {"(a|aa)*": {"type": "string"},
                                                             "^x_": {"type": "number"}}}),
    ("nested defs", ["keyword"], {"properties": {"a": {"$defs": {"b": {}}}}}),
    ("discriminator", ["keyword", "ref"], {
        "$defs": {"A": {"properties": {"k": {"const": "a"}}}},
        "oneOf": [{"$ref": "#/$defs/A"}],
        "discriminator": {"propertyName": "k", "mapping": {"a": "#/$defs/A"}}}),
]  # fmt: skip


def _golden() -> dict[str, Any]:
    cases = []
    for name, checks, schema in GOLDEN_CASES:
        out, issues = sanitize_schema(schema, mode="strip", where="")
        cases.append(
            {
                "name": name,
                "checks": checks,
                "input": schema,
                "output": out,
                "removed": sorted(i.pointer for i in issues),
            }
        )
    return {
        "about": "mapi_spec.schema_safety.sanitize_schema(mode='strip') on each input; the web "
        "mirror (features/forms/schema-sanitize.ts) must give the same output for the checks "
        "it implements ('regex' needs is_safe_regex; the web may drop every pattern instead).",
        "allowed_keywords": sorted(ALLOWED_KEYWORDS),
        "cases": cases,
    }


def test_settings_schemas_golden() -> None:
    want = _golden()
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS"):
        GOLDEN.write_text(json.dumps(want, indent=2, ensure_ascii=False) + "\n", "utf-8")
    got = json.loads(GOLDEN.read_text("utf-8"))
    assert got == want, "regenerate with MAPI_UPDATE_SNAPSHOTS=1 and update the web mirror"


def test_module_source_compiles_with_warnings_as_errors() -> None:
    # v0.18 review: an invalid escape in a docstring was a SyntaxWarning (an error under -W error)
    import warnings

    import mapi_spec.schema_safety as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(source, module.__file__, "exec")
