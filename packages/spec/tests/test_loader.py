from __future__ import annotations

import json
from typing import Any

import pytest
from mapi_spec import ModelSpec, ScoreRequest
from mapi_spec.errors import (
    ErrorCode,
    PayloadTooLarge,
    SpecParseError,
    UnsupportedApiVersion,
    ValidationFailed,
)
from mapi_spec.examples import E33_YAML, SCORE_YAML, XGB_YAML
from mapi_spec.loader import (
    _UPGRADERS,
    Limits,
    canonical_json,
    canonical_value,
    check_limits,
    dump_yaml,
    load_document,
    load_model_spec,
    load_score_request,
    parse_document,
    parse_json,
    parse_text,
    parse_yaml,
    register_upgrader,
    resolve_plain_scalar,
    spec_hash,
    upgrade,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------- YAML 1.2 scalar resolution


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("~", None),
        ("null", None),
        ("NULL", None),
        ("true", True),
        ("False", False),
        ("yes", "yes"),
        ("no", "no"),
        ("on", "on"),
        ("off", "off"),
        ("y", "y"),
        ("0777", 777),
        ("0o17", 15),
        ("0x1F", 31),
        ("-12", -12),
        ("+3", 3),
        ("1.5", 1.5),
        ("1e3", 1000.0),
        (".5", 0.5),
        ("1_000", "1_000"),
        ("2026-10-14", "2026-10-14"),
        ("1:20", "1:20"),
        ("W-SUN", "W-SUN"),
        ("=", "="),
    ],
)
def test_core_schema(text: str, expected: Any) -> None:
    got = resolve_plain_scalar(text)
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize("text", [".inf", "-.Inf", ".nan", ".NaN"])
def test_core_schema_rejects_non_finite(text: str) -> None:
    with pytest.raises(ValueError):
        resolve_plain_scalar(text)


def test_parse_yaml_basic_and_quoting() -> None:
    data = parse_yaml("a: yes\nb: 'true'\nc: [1, 2.0, null]\nd: {e: on}\nf: |\n  x\n")
    assert data == {"a": "yes", "b": "true", "c": [1, 2.0, None], "d": {"e": "on"}, "f": "x\n"}
    assert parse_yaml(b"%YAML 1.2\n---\nx: 1\n") == {"x": 1}
    assert parse_yaml("---\n") is None
    assert parse_yaml("- 1\n- [2, {a: b}]\n") == [1, [2, {"a": "b"}]]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("a: &x 1\nb: *x\n", "anchors"),
        ("a: &x {k: 1}\n", "anchors"),
        ("a: *x\n", "alias|anchor|YAML"),
        ("a: !!python/object:os.system x\n", "tags"),
        ("a: !!str 1\n", "tags"),
        ("a: !custom x\n", "tags"),
        ("a: ! x\n", "tags"),
        ("%TAG ! tag:evil.com,2000:\n---\na: 1\n", "TAG"),
        ("%YAML 1.1\n---\na: 1\n", "1.2"),
        ("a: 1\n---\nb: 2\n", "only one"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("1: x\n", "keys must be strings"),
        ("null: x\n", "keys must be strings"),
        ("? [a]\n: 1\n", "keys must be strings"),
        ("a: .nan\n", "NaN"),
        ("a: [1, 2\n", "invalid YAML"),
        ("", "empty"),
        ("<<: {a: 1}\n", None),
    ],
)
def test_yaml_rejections(text: str, match: str | None) -> None:
    if match is None:  # merge keys are just a string key '<<' under YAML 1.2
        assert parse_yaml(text) == {"<<": {"a": 1}}
        return
    with pytest.raises(SpecParseError, match=match):
        parse_yaml(text)


def test_billion_laughs_is_rejected_before_expansion() -> None:
    text = 'a: &a ["x","x"]\nb: &b [*a,*a]\nc: &c [*b,*b]\nd: [*c,*c]\n'
    with pytest.raises(SpecParseError, match="anchors"):
        parse_yaml(text)


def test_size_limit() -> None:
    big = "a: " + "x" * (256 * 1024) + "\n"
    with pytest.raises(PayloadTooLarge, match="larger than"):
        parse_yaml(big)
    with pytest.raises(PayloadTooLarge):
        parse_json('{"a": "' + "x" * (256 * 1024) + '"}')
    with pytest.raises(PayloadTooLarge, match="scalar"):
        parse_yaml("a: " + "x" * 100, Limits(max_scalar_bytes=10))


def test_depth_limit() -> None:
    ok = "a:" + " [" * 30 + "1" + "]" * 30 + "\n"
    parse_yaml(ok)
    deep = "a:" + " [" * 40 + "1" + "]" * 40 + "\n"
    with pytest.raises(PayloadTooLarge, match="depth 32"):
        parse_yaml(deep)
    with pytest.raises(PayloadTooLarge, match="depth"):
        parse_json("[" * 40 + "]" * 40)
    with pytest.raises(PayloadTooLarge):
        parse_json("[" * 100_000 + "]" * 100_000, Limits(max_bytes=10**6))


def test_node_limit() -> None:
    many = "a: [" + ", ".join(["1"] * 10_001) + "]\n"
    with pytest.raises(PayloadTooLarge, match="10000 nodes"):
        parse_yaml(many)
    with pytest.raises(PayloadTooLarge, match="nodes"):
        parse_json(json.dumps({"a": list(range(10_001))}))
    parse_yaml("a: [" + ", ".join(["1"] * 9_000) + "]\n")


def test_json_rules() -> None:
    assert parse_json('{"a": [1, 2.5, null, true]}') == {"a": [1, 2.5, None, True]}
    with pytest.raises(SpecParseError, match="duplicate"):
        parse_json('{"a": 1, "a": 2}')
    with pytest.raises(SpecParseError, match="NaN"):
        parse_json('{"a": NaN}')
    with pytest.raises(SpecParseError, match="invalid JSON"):
        parse_json("{")
    with pytest.raises(SpecParseError, match="UTF-8"):
        parse_json(b"\xff\xfe")
    assert parse_text('  {"a": 1}') == {"a": 1}
    assert parse_text(b"[1]") == [1]
    assert parse_text("a: 1") == {"a": 1}


def test_check_limits_on_python_values() -> None:
    check_limits({"a": [1, "x", None]})
    with pytest.raises(SpecParseError, match="keys must be strings"):
        check_limits({1: "x"})
    with pytest.raises(SpecParseError, match="NaN"):
        check_limits({"a": float("nan")})
    with pytest.raises(SpecParseError, match="unsupported value"):
        check_limits({"a": object()})


# ------------------------------------------------------------------------ typed documents


def test_load_documents() -> None:
    assert isinstance(load_model_spec(E33_YAML), ModelSpec)
    assert isinstance(load_model_spec(XGB_YAML), ModelSpec)
    assert isinstance(load_score_request(SCORE_YAML), ScoreRequest)
    with pytest.raises(ValidationFailed, match="expected kind"):
        load_score_request(E33_YAML)


def test_validation_errors_have_pointers() -> None:
    bad = E33_YAML.replace("last: 8", "last: 0").replace("unseen_levels: pooled}", "x: 1}", 1)
    bad = bad.replace("tune: 400", "tune: -1")
    with pytest.raises(ValidationFailed) as exc:
        load_document(bad)
    pointers = {i.pointer for i in exc.value.issues}
    assert "/data/split/last" in pointers
    assert "/model/fit/tune" in pointers
    assert all(i.code is ErrorCode.SCHEMA_INVALID for i in exc.value.issues)
    problem = exc.value.to_problem()
    assert problem.status == 422 and len(problem.errors) == len(exc.value.issues)


def test_missing_and_extra_field_pointers() -> None:
    doc = {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "mode": "predict", "bogus": 1}
    with pytest.raises(ValidationFailed) as exc:
        parse_document(doc)
    pointers = {i.pointer for i in exc.value.issues}
    assert pointers == {"/model", "/bogus"}
    doc = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://a/b",
        "output": {"aggregate": {"over": ["geo"], "how": "median"}},
        "interventions": [{"var": "spend", "multiply": 1, "add": 1}],
    }
    with pytest.raises(ValidationFailed) as exc:
        parse_document(doc)
    pointers = {i.pointer for i in exc.value.issues}
    assert pointers == {"/output/aggregate/how", "/interventions/0"}


def test_union_tag_segments_are_skipped() -> None:
    text = E33_YAML.replace("posterior_predictive: {thin: 4}", "posterior_predictive: {thin: 0}")
    with pytest.raises(ValidationFailed) as exc:
        load_document(text)
    assert {i.pointer for i in exc.value.issues} == {
        "/model/fit/keep/posterior_predictive/thin"
    } | {"/model/fit/keep/posterior_predictive"}
    text = E33_YAML.replace("last: 8,", "last: 8, bogus: 1,")
    with pytest.raises(ValidationFailed) as exc:
        load_document(text)
    assert [i.pointer for i in exc.value.issues] == ["/data/split/bogus"]


@pytest.mark.parametrize(
    ("doc", "pointer"),
    [
        ([1, 2], ""),
        ({"apiVersion": "mapi/v1", "kind": "Nope"}, "/kind"),
        ({"apiVersion": "mapi/v1"}, "/kind"),
    ],
)
def test_document_shape_errors(doc: Any, pointer: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        parse_document(doc)
    assert exc.value.issues[0].pointer == pointer


def test_api_version_handling() -> None:
    with pytest.raises(UnsupportedApiVersion, match="not accepted"):
        parse_document({"apiVersion": "mapi/v0", "kind": "ModelSpec"})
    with pytest.raises(UnsupportedApiVersion, match="missing"):
        parse_document({"kind": "ModelSpec"})


def test_upgrade_chain_n_minus_one() -> None:
    def from_alpha(doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)
        if doc.get("kind") == "ScoreRequest" and "endpoint" in doc:
            doc["model"] = doc.pop("endpoint")
        return doc

    register_upgrader("mapi/v1beta1", "mapi/v1", from_alpha)
    try:
        old = {"apiVersion": "mapi/v1beta1", "kind": "ScoreRequest", "endpoint": "endpoint://a/b"}
        accepted = ("mapi/v1", "mapi/v1beta1")
        up = upgrade(old, accepted=accepted)
        assert up == {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://a/b"}
        doc = parse_document(old, accepted=accepted)
        assert isinstance(doc, ScoreRequest)
        with pytest.raises(UnsupportedApiVersion):
            parse_document(old)  # not in the default accepted window
        with pytest.raises(UnsupportedApiVersion, match="no upgrade path"):
            upgrade({"apiVersion": "mapi/v0"}, accepted=("mapi/v0",))
        register_upgrader("mapi/loop", "mapi/loop", lambda d: d)
        with pytest.raises(UnsupportedApiVersion, match="no upgrade path"):
            upgrade({"apiVersion": "mapi/loop"}, accepted=("mapi/loop",))
    finally:
        _UPGRADERS.pop("mapi/v1beta1", None)
        _UPGRADERS.pop("mapi/loop", None)


# ------------------------------------------------------------------- canonical form & hash


def test_canonical_json_is_normalised() -> None:
    a = load_model_spec(E33_YAML)
    # Different key order / flow style / explicit defaults / int-vs-float / duration spelling
    b_text = (
        E33_YAML.replace("timeout: 45m", "timeout: 2700")
        .replace("sigma: 1.0", "sigma: 1")
        .replace("execution: {resource_class: M,", "execution: {resource_class: M,")
        .replace(
            "ui_draws: 300",
            "ui_draws: 300\n  derived: [contributions, response_curves, marginal_cost]",
        )
    )
    b_text = b_text.replace(
        "  derived: [contributions, response_curves, marginal_cost]\n  plots:", "  plots:", 1
    )
    b = load_model_spec(b_text)
    assert canonical_json(a) == canonical_json(b)
    assert spec_hash(a) == spec_hash(b)
    assert spec_hash(a).startswith("sha256:") and len(spec_hash(a)) == 71
    c = load_model_spec(E33_YAML.replace("seed: 20261014", "seed: 20261015"))
    assert spec_hash(c) != spec_hash(a)
    text = canonical_json(a)
    assert " " not in text.replace("HalfNormal", "")[:50]
    assert json.loads(text)["apiVersion"] == "mapi/v1"
    # canonical JSON parses back to an equal document
    assert load_model_spec(text) == a


def test_canonical_value() -> None:
    assert canonical_value({"a": 1.0, "b": [2.5, True, None]}) == {"a": 1, "b": [2.5, True, None]}
    assert isinstance(canonical_value(1.0), int)
    assert canonical_value(float(2**60)) == float(2**60)
    with pytest.raises(ValueError):
        canonical_value(float("inf"))
    with pytest.raises(TypeError):
        canonical_value({1, 2})
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_dump_yaml_roundtrip() -> None:
    for text in (E33_YAML, XGB_YAML, SCORE_YAML):
        doc = load_document(text)
        dumped = dump_yaml(doc)
        assert "&" not in dumped and "!!" not in dumped
        assert load_document(dumped) == doc
        assert dump_yaml(doc, exclude_defaults=False)
    tricky = {"a": "yes", "b": "0o17", "c": "1.5", "d": "null", "e": "multi\nline", "f": ".inf"}
    assert parse_yaml(dump_yaml(tricky)) == tricky
