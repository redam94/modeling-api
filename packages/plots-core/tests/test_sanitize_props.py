"""Property and fuzz tests for the sanitizer (Hypothesis)."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from mapi_plots._testing import make_plot_doc
from mapi_plots.sanitize import (
    ALLOWED_TAGS,
    SanitizeError,
    SanitizeRule,
    check_expression,
    check_plot,
    sanitize_plot,
    sanitize_spec,
    strip_html,
)

pytestmark = pytest.mark.unit

SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**6), 10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=20),
)
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=12), children, max_size=5),
    ),
    max_leaves=40,
)

# -------------------------------------------------------------------------------- HTML

html_fragments = st.lists(
    st.one_of(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=8),
        st.sampled_from(
            [
                "<",
                ">",
                "</",
                "<b>",
                "</b>",
                "<i>",
                "<br>",
                "<br/>",
                "<sub>",
                "<sup>",
                "<script>",
                "</script>",
                "<a href='javascript:x'>",
                "<img src=x onerror=y>",
                "<!--",
                "-->",
                "<b onclick=x>",
                "&lt;",
                "<span style='x'>",
                "<svg/onload=x>",
                "<<b>",
                "<b<i>>",
                "< b>",
                "<extra>",
                "\n",
            ]
        ),
    ),
    max_size=12,
).map("".join)

_KEPT = re.compile(r"</?(?:b|i|sub|sup)>|<br>")


@SETTINGS
@given(html_fragments)
def test_strip_html_leaves_only_allowed_tags(text: str) -> None:
    out = strip_html(text)
    assert "<" not in _KEPT.sub("", out)
    assert strip_html(out) == out  # idempotent
    if "<" not in text:
        assert out == text


@SETTINGS
@given(html_fragments)
def test_strip_html_allowed_set_is_respected(text: str) -> None:
    out = strip_html(text, frozenset({"b"}))
    assert "<" not in re.sub(r"</?b>", "", out)
    assert set(ALLOWED_TAGS) >= {"b", "i", "br", "sub", "sup"}


# --------------------------------------------------------------------------- expressions

identifiers = st.sampled_from(["datum.a", "datum.b", "datum['a b']", "brush.x", "1", "2.5", "'s'"])
functions = st.sampled_from(["abs", "round", "isValid", "toNumber", "upper", "length"])


@st.composite
def safe_exprs(draw: st.DrawFn, depth: int = 0) -> str:
    if depth > 2 or draw(st.booleans()):
        return draw(identifiers)
    choice = draw(st.integers(0, 3))
    a = draw(safe_exprs(depth + 1))
    b = draw(safe_exprs(depth + 1))
    if choice == 0:
        return (
            f"{a} {draw(st.sampled_from(['+', '-', '*', '/', '>', '<=', '===', '&&', '||']))} {b}"
        )
    if choice == 1:
        return f"{draw(functions)}({a})"
    if choice == 2:
        return f"({a}) ? {b} : {a}"
    return f"if({a}, {b}, null)"


DENIED = ["constructor", "__proto__", "prototype", "eval", "Function", "fetch", "window",
          "document", "globalThis", "import", "XMLHttpRequest", "postMessage"]  # fmt: skip


@SETTINGS
@given(safe_exprs())
def test_generated_safe_expressions_pass(expr: str) -> None:
    assert check_expression(expr) == []


@SETTINGS
@given(safe_exprs(), st.sampled_from(DENIED), st.integers(0, 3))
def test_denied_names_are_always_caught(expr: str, word: str, how: int) -> None:
    variants = [
        f"{expr} + datum.{word}",
        f"{word}({expr})",
        f"datum['{word}'] + {expr}",
        f"{expr} || {word}",
    ]
    assert check_expression(variants[how]) != []


# -------------------------------------------------------------------------------- fuzzing


@SETTINGS
@given(st.sampled_from(["vega-lite", "plotly"]), json_values)
def test_arbitrary_json_only_raises_sanitize_error(kind: str, value: Any) -> None:
    spec = value if isinstance(value, dict) else {"x": value}
    try:
        sanitize_spec(kind, spec)
    except SanitizeError as exc:
        assert exc.issues
        assert all(i.is_error for i in exc.issues)


@SETTINGS
@given(json_values)
def test_arbitrary_documents_never_crash(value: Any) -> None:
    doc = value if isinstance(value, dict) else {"spec": value}
    for issue in check_plot(doc):
        assert issue.context["rule"] in SanitizeRule._value2member_map_


@SETTINGS
@given(st.binary(max_size=200))
def test_arbitrary_bytes_never_crash(data: bytes) -> None:
    for issue in check_plot(data):
        assert issue.is_error


# ------------------------------------------------------------------------------ mutation

BASE_VL: dict[str, Any] = {
    "data": {"name": "d"},
    "layer": [
        {"mark": "line", "encoding": {"x": {"field": "t", "type": "temporal"}}},
        {"mark": {"type": "point"}, "transform": [{"calculate": "datum.a * 2", "as": "b"}]},
    ],
    "config": {"axis": {"labelColor": "#333"}},
}
BASE_PLOTLY: dict[str, Any] = {
    "data": [{"type": "scatter", "x": [1, 2], "marker": {"color": "red"}}],
    "layout": {
        "title": {"text": "t"},
        "xaxis": {"title": {"text": "x"}},
        "annotations": [{"text": "a"}],
    },
}


def _dict_paths(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    if isinstance(node, dict):
        out.append(path)
        for k, v in node.items():
            out += _dict_paths(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _dict_paths(v, (*path, i))
    return out


def _at(node: Any, path: tuple[Any, ...]) -> Any:
    for p in path:
        node = node[p]
    return node


INJECTIONS: list[tuple[str, Any, SanitizeRule]] = [
    ("url", "https://evil.example/x", SanitizeRule.URL_DATA),
    ("__proto__", {"x": 1}, SanitizeRule.PROTO_KEY),
    ("constructor", 1, SanitizeRule.PROTO_KEY),
    ("href", "https://evil.example", SanitizeRule.HREF),
    ("zz_note", "javascript:alert(1)", SanitizeRule.SCRIPT_URL),
    ("zz_note", "url(https://evil.example/)", SanitizeRule.CSS_URL),
    ("zz_note", "data:text/html,<script>", SanitizeRule.SCRIPT_URL),
]


@SETTINGS
@given(st.sampled_from(["vega-lite", "plotly"]), st.data(), st.sampled_from(INJECTIONS))
def test_injected_payloads_are_rejected_anywhere(
    kind: str, data: st.DataObject, injection: tuple[str, Any, SanitizeRule]
) -> None:
    base = copy.deepcopy(BASE_VL if kind == "vega-lite" else BASE_PLOTLY)
    path = data.draw(st.sampled_from(_dict_paths(base)))
    key, value, rule = injection
    target = _at(base, path)
    assume(key not in target)
    target[key] = value
    with pytest.raises(SanitizeError) as info:
        sanitize_spec(kind, base, validate_schema=False)
    if kind == "plotly" and key == "url":
        assert SanitizeRule.URL_DATA in info.value.rules
    else:
        assert rule in info.value.rules or (
            key == "url" and SanitizeRule.EXTERNAL_IMAGE in info.value.rules
        )


# --------------------------------------------------------------------------- fixed point

plot_texts = st.text(alphabet="ab<>/ i", max_size=12)


@SETTINGS
@given(plot_texts, plot_texts, st.lists(st.floats(-1e6, 1e6), min_size=1, max_size=5))
def test_sanitized_plotly_is_a_fixed_point(name: str, title: str, ys: list[float]) -> None:
    spec = {
        "data": [{"type": "scatter", "y": ys, "name": name, "text": [name] * len(ys)}],
        "layout": {"title": {"text": title}},
    }
    try:
        res = sanitize_plot(make_plot_doc("plotly", spec))
    except SanitizeError:
        return
    again = sanitize_plot(res.plot)
    assert again.plot == res.plot
    assert again.changes == []
    json.dumps(again.plot.spec)  # stays JSON
