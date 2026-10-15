from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mapi_plots._testing import make_plot_doc
from mapi_plots.sanitize import (
    PLOTLY_REF_SCHEMA,
    VEGA_LITE_SCHEMA_URL,
    SanitizeError,
    SanitizeLimits,
    SanitizeRule,
    check_expression,
    check_plot,
    is_plotly_ref,
    parse_plotly_ref,
    sanitize_plot,
    sanitize_spec,
    strip_html,
)
from mapi_spec.errors import ErrorCode, Severity
from mapi_spec.plots import PlotSpec

pytestmark = pytest.mark.unit

CORPUS = Path(__file__).parent / "corpus"


def load_corpus(name: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads((CORPUS / name).read_text("utf-8"))
    return cases


def envelope(kind: str, spec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return make_plot_doc(kind, spec, **extra)


VL_BAD = load_corpus("vega_lite_malicious.json")
PL_BAD = load_corpus("plotly_malicious.json")


# ------------------------------------------------------------------------- malicious corpus


@pytest.mark.parametrize("case", VL_BAD, ids=[c["name"] for c in VL_BAD])
def test_vega_lite_corpus_is_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(SanitizeError) as info:
        sanitize_spec("vega-lite", case["spec"])
    expected = {SanitizeRule(r) for r in case["rules"]}
    assert expected <= info.value.rules, (case["name"], info.value.rules)
    # the same spec inside a full PlotSpec document is rejected too
    with pytest.raises(SanitizeError):
        sanitize_plot(envelope("vega-lite", case["spec"]))


@pytest.mark.parametrize("case", PL_BAD, ids=[c["name"] for c in PL_BAD])
def test_plotly_corpus_is_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(SanitizeError) as info:
        sanitize_spec("plotly", case["spec"])
    expected = {SanitizeRule(r) for r in case["rules"]}
    assert expected <= info.value.rules, (case["name"], info.value.rules)
    with pytest.raises(SanitizeError):
        sanitize_plot(json.dumps(envelope("plotly", case["spec"]), default=str))


def test_errors_are_typed_problems_with_pointers() -> None:
    with pytest.raises(SanitizeError) as info:
        sanitize_plot(envelope("vega-lite", {"data": {"url": "https://x"}, "mark": "point"}))
    err = info.value
    assert err.code is ErrorCode.PLOT_UNSAFE
    assert err.status == 422
    assert err.issues[0].pointer == "/spec/data/url"
    assert err.issues[0].context["rule"] == "url_data"
    problem = err.to_problem()
    assert problem.code is ErrorCode.PLOT_UNSAFE and problem.errors


def test_user_text_only_in_untrusted_text() -> None:
    payload = "<img src=x onerror=alert(1)>"
    spec = {
        "data": {"name": "d"},
        "mark": "point",
        "transform": [{"filter": f"fetch('{payload}')"}],
    }
    with pytest.raises(SanitizeError) as info:
        sanitize_spec("vega-lite", spec)
    for issue in info.value.issues:
        assert payload not in issue.message
    assert any(payload in (i.untrusted_text or "") for i in info.value.issues)


def test_issue_count_is_capped() -> None:
    spec = {
        "data": {"name": "d"},
        "mark": "point",
        "usermeta": {f"k{i}": {"url": "x"} for i in range(200)},
    }
    with pytest.raises(SanitizeError) as info:
        sanitize_spec("vega-lite", spec, limits=SanitizeLimits(max_issues=7))
    assert len(info.value.issues) == 7


# ------------------------------------------------------------------------------- benign


BENIGN_VL: list[dict[str, Any]] = [
    {
        "data": {"name": "d"},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {
                "field": "week",
                "type": "temporal",
                "axis": {"labelExpr": "datum.label + ' wk'"},
            },
            "y": {"field": "revenue", "type": "quantitative", "title": "Revenue <EUR>"},
            "color": {
                "condition": {"test": "datum.location != null && datum.view > 3", "value": "red"},
                "value": "grey",
            },
        },
        "transform": [
            {"calculate": "datum.y * 2 + pow(datum.x, 2)", "as": "z"},
            {"filter": "isValid(datum.z) && datum['a b'] > 0"},
            {"filter": {"and": ["datum.z > 0", {"field": "g", "oneOf": ["a", "b"]}]}},
            {"calculate": "if(datum.z > 1, 'hi', 'lo')", "as": "band"},
            {"calculate": "timeFormat(toDate(datum.week), '%Y') + peek([1, 2])", "as": "yr"},
        ],
        "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
    },
    {
        "datasets": {"inline": [{"a": 1}, {"a": 2}]},
        "data": {"name": "inline"},
        "facet": {"row": {"field": "g", "type": "nominal"}},
        "spec": {
            "layer": [
                {"mark": "area", "encoding": {"x": {"field": "a", "type": "quantitative"}}},
                {
                    "mark": "rule",
                    "encoding": {"x": {"aggregate": "min", "field": "split", "type": "temporal"}},
                },
            ]
        },
    },
    {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"name": "d"},
        "mark": "text",
        "encoding": {"text": {"field": "a", "format": ".2f", "formatType": "number"}},
    },
]


@pytest.mark.parametrize("spec", BENIGN_VL)
def test_benign_vega_lite_passes(spec: dict[str, Any]) -> None:
    out, changes = sanitize_spec("vega-lite", spec)
    assert out["$schema"] == VEGA_LITE_SCHEMA_URL
    assert {c.context["rule"] for c in changes} <= {"schema_url"}
    assert all(c.severity is Severity.INFO for c in changes)
    rest = {k: v for k, v in out.items() if k != "$schema"}
    assert rest == {k: v for k, v in spec.items() if k != "$schema"}


BENIGN_PLOTLY: list[dict[str, Any]] = [
    {
        "data": [
            {
                "type": "scatter",
                "mode": "lines+markers",
                "x": {"$ref": "d.week"},
                "y": {"dtype": "f8", "bdata": "AAAAAAAA8D8AAAAAAAAAQA=="},
                "marker": {"color": ["red", "#00ff00"], "size": 4},
                "hovertemplate": "%{y:.2f}<extra></extra>",
                "name": "<b>fit</b>",
                "xaxis": "x2",
            },
            {"type": "heatmap", "z": {"dtype": "i1", "bdata": "AAECAwQF", "shape": "2, 3"}},
            {"type": "bar", "x": ["a", "b"], "y": [1, 2], "orientation": "v"},
        ],
        "layout": {
            "title": {"text": "Fit <i>vs</i> actual<br>weekly"},
            "xaxis2": {"anchor": "y", "domain": [0.5, 1]},
            "images": [
                {"source": "data:image/png;base64,iVBORw0KGgo=", "x": 0, "y": 1, "xref": "paper"}
            ],
            "annotations": [{"text": "H<sub>2</sub>O", "x": 1, "y": 1, "showarrow": False}],
            "updatemenus": [
                {"buttons": [{"method": "restyle", "label": "hide", "args": [{"visible": False}]}]}
            ],
            "template": {"layout": {"annotationdefaults": {"arrowcolor": "#2a3f5f"}}},
        },
        "config": {"responsive": True, "displaylogo": False},
        "frames": [{"name": "f0", "data": [{"type": "scatter", "y": [1, 2]}]}],
    },
    {
        "data": [{"type": "scattergeo", "lat": [1.0], "lon": [2.0]}],
        "layout": {"geo": {"scope": "europe"}},
    },
    {  # positional [attr, value] button arguments that are safe
        "data": [{"type": "scatter", "x": [1, 2], "y": [1, 2]}],
        "layout": {
            "updatemenus": [
                {
                    "buttons": [
                        {"method": "restyle", "label": "bars", "args": ["type", "bar"]},
                        {"method": "relayout", "label": "t", "args": ["title.text", "Weekly"]},
                        {"method": "animate", "label": "play", "args": [None, {"frame": {}}]},
                        {
                            "method": "relayout",
                            "label": "logo",
                            "args": ["images[0].source", "data:image/png;base64,iVBORw0KGgo="],
                        },
                    ]
                }
            ],
            "sliders": [{"steps": [{"method": "animate", "label": "f0", "args": [["f0"], {}]}]}],
        },
    },
]


@pytest.mark.parametrize("spec", BENIGN_PLOTLY)
def test_benign_plotly_passes_unchanged(spec: dict[str, Any]) -> None:
    out, changes = sanitize_spec("plotly", spec)
    assert out == spec
    assert changes == []


# ------------------------------------------------------------------------------ rewrites


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("<b>bold</b> and <i>it</i><br>x<sub>1</sub><sup>2</sup>", None),
        ('<a href="javascript:alert(1)">click</a>', "click"),
        ("<img src=x onerror=alert(1)>", ""),
        ("<script>alert(1)</script>", "alert(1)"),
        ('<b onclick="x()">b</b>', "<b>b</b>"),
        ("<BR/>", "<br>"),
        ("a < b > c", "a &lt; b > c"),
        ("<scr<script>ipt>", "&lt;scr" + "ipt>"),
        ("<!-- hidden --> ok", " ok"),
        ("<!-- unterminated", ""),
        ('<span style="background:url(x)">s</span>', "s"),
    ],
)
def test_strip_html(text: str, expected: str | None) -> None:
    assert strip_html(text) == (text if expected is None else expected)


def test_plotly_text_is_stripped_and_reported() -> None:
    spec = {
        "data": [{"type": "scatter", "x": [1], "name": "<span>a</span>", "text": ["<img>t"]}],
        "layout": {"title": {"text": '<a href="https://evil.example">t</a>'}},
    }
    out, changes = sanitize_spec("plotly", spec)
    assert out["data"][0]["name"] == "a"
    assert out["data"][0]["text"] == ["t"]
    assert out["layout"]["title"]["text"] == "t"
    pointers = {c.pointer for c in changes}
    assert pointers == {"/data/0/name", "/data/0/text/0", "/layout/title/text"}
    assert spec["data"][0]["name"] == "<span>a</span>"  # input is never mutated


def test_extra_tag_only_kept_in_hovertemplate() -> None:
    spec = {
        "data": [
            {"type": "scatter", "hovertemplate": "%{y}<extra>e</extra>", "name": "<extra>n</extra>"}
        ]
    }
    out, _ = sanitize_spec("plotly", spec)
    assert out["data"][0]["hovertemplate"] == "%{y}<extra>e</extra>"
    assert out["data"][0]["name"] == "n"


def test_template_is_pruned_not_rejected() -> None:
    spec = {
        "data": [],
        "layout": {
            "template": {
                "data": {
                    "scattermap": [{"type": "scattermap"}],
                    "bogus": [{}],
                    "bar": [{"opacity": 0.5, "nope": 1}],
                },
                "layout": {"mapbox": {"style": "light"}, "font": {"color": "#333"}, "zzz": 1},
                "other": {},
            }
        },
    }
    out, changes = sanitize_spec("plotly", spec)
    tmpl = out["layout"]["template"]
    assert set(tmpl) == {"data", "layout"}
    assert set(tmpl["data"]) == {"bar"} and tmpl["data"]["bar"] == [{"opacity": 0.5}]
    assert tmpl["layout"] == {"font": {"color": "#333"}}
    assert all(c.context["rule"] == "template_pruned" for c in changes)
    assert len(changes) == 6


def test_vega_lite_schema_is_pinned_and_unknown_schema_rejected() -> None:
    out, changes = sanitize_spec("vega-lite", {"data": {"name": "d"}, "mark": "point"})
    assert out["$schema"] == VEGA_LITE_SCHEMA_URL
    assert changes[0].context["rule"] == "schema_url"
    with pytest.raises(SanitizeError):
        sanitize_spec("vega-lite", {"$schema": 5, "data": {"name": "d"}, "mark": "point"})


def test_schema_validation_can_be_skipped_but_safety_cannot() -> None:
    sanitize_spec("vega-lite", {"mark": "not-a-mark"}, validate_schema=False)
    sanitize_spec("plotly", {"data": [{"type": "scatter", "nope": 1}]}, validate_schema=False)
    with pytest.raises(SanitizeError):
        sanitize_spec("vega-lite", {"mark": "x", "data": {"url": "u"}}, validate_schema=False)
    with pytest.raises(SanitizeError):
        sanitize_spec("plotly", {"data": [{"type": "scattermap"}]}, validate_schema=False)
    with pytest.raises(SanitizeError):
        sanitize_spec("plotly", {"layout": {"mapbox": {}}}, validate_schema=False)


# ------------------------------------------------------------------------------- envelope


def test_full_plot_roundtrip_returns_plotspec() -> None:
    spec = {"data": {"name": "d"}, "mark": "point"}
    res = sanitize_plot(envelope("vega-lite", spec))
    assert isinstance(res.plot, PlotSpec)
    assert res.plot.spec["$schema"] == VEGA_LITE_SCHEMA_URL
    again = sanitize_plot(res.plot)
    assert again.plot == res.plot and again.changes == []
    text = res.plot.model_dump_json()
    assert sanitize_plot(text).plot == res.plot
    assert sanitize_plot(text.encode()).plot == res.plot


def test_envelope_errors() -> None:
    issues = check_plot(envelope("vega-lite", {"data": {"name": "missing"}, "mark": "point"}))
    assert [i.context["rule"] for i in issues if i.is_error] == ["envelope"]
    assert issues[0].code is ErrorCode.SCHEMA_INVALID
    doc = envelope("vega-lite", {"data": {"name": "d"}, "mark": "point"})
    doc["datasets"] = {"__proto__": {"kind": "inline_arrow", "data": ""}}
    assert "proto_key" in {i.context["rule"] for i in check_plot(doc)}
    assert check_plot(42)[0].context["rule"] == "envelope"  # type: ignore[arg-type]
    assert check_plot("[1, 2]")[0].context["rule"] == "envelope"
    svg = {**envelope("plotly", {"data": []}), "kind": "svg"}
    assert check_plot(svg)[0].context["rule"] == "envelope"


def test_check_plot_reports_changes_without_raising() -> None:
    issues = check_plot(envelope("plotly", {"data": [{"type": "scatter", "name": "<u>x</u>"}]}))
    assert [i.severity for i in issues] == [Severity.INFO]
    assert check_plot(envelope("plotly", {"data": [{"type": "scattermap"}]}))[0].is_error


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('{"a": 1, "a": 2}', "parse"),
        ('{"a": NaN}', "parse"),
        ("{nope", "parse"),
        ("[" * 100_000 + "]" * 100_000, "too_deep"),
    ],
)
def test_strict_json(text: str, rule: str) -> None:
    issues = check_plot(text)
    assert issues[0].context["rule"] == rule


def test_size_limits() -> None:
    lim = SanitizeLimits(
        max_document_bytes=50_000, max_spec_bytes=20_000, max_depth=10, max_nodes=500,
        max_string_bytes=100, max_array_items=50, max_typed_array_bytes=16,
    )  # fmt: skip
    deep: Any = {"data": {"name": "d"}, "mark": "point"}
    node = deep
    for _ in range(12):
        node["usermeta"] = {}
        node = node["usermeta"]
    rules = {i.context["rule"] for i in check_plot(envelope("vega-lite", deep), limits=lim)}
    assert rules == {"too_deep"}
    wide = {"data": {"name": "d"}, "mark": "point", "usermeta": {f"k{i}": i for i in range(600)}}
    assert (
        check_plot(envelope("vega-lite", wide), limits=lim)[0].context["rule"] == "too_many_nodes"
    )
    longs = {"data": {"name": "d"}, "mark": "point", "description": "x" * 101}
    assert (
        check_plot(envelope("vega-lite", longs), limits=lim)[0].context["rule"] == "string_too_long"
    )
    arr = {"data": [{"type": "scatter", "x": list(range(51))}]}
    assert check_plot(envelope("plotly", arr), limits=lim)[0].context["rule"] == "array_too_long"
    big = {"data": [{"type": "scatter", "x": ["y" * 90] * 40} for _ in range(10)]}
    rule = check_plot(envelope("plotly", big), limits=lim)[0].context["rule"]
    assert rule == "too_large"
    typed = {"data": [{"type": "scatter", "x": {"dtype": "f8", "bdata": "A" * 40}}]}
    assert check_plot(envelope("plotly", typed), limits=lim)[0].context["rule"] == "too_large"
    huge_text = json.dumps(envelope("plotly", {"data": []})) + " " * 60_000
    assert check_plot(huge_text, limits=lim)[0].context["rule"] == "too_large"
    with pytest.raises(SanitizeError) as info:
        sanitize_spec("plotly", big, limits=lim)
    assert info.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_non_json_values_rejected() -> None:
    spec = {"data": {"name": "d"}, "mark": "point", "usermeta": {"x": float("nan"), "y": {1, 2}}}
    rules = {i.context["rule"] for i in check_plot(envelope("vega-lite", spec))}
    assert rules == {"schema"}
    assert check_plot({"kind": "plotly", 1: 2})[0].context["rule"] == "schema"  # type: ignore[dict-item]


def test_sanitize_spec_rejects_unknown_kind() -> None:
    with pytest.raises(SanitizeError):
        sanitize_spec("svg", {})


# ---------------------------------------------------------------------------- expressions


@pytest.mark.parametrize(
    "expr",
    [
        "datum.a + 1",
        "datum['a b'] * 2 > 3 ? 'x' : 'y'",
        "if(isValid(datum.x), round(datum.x * 100) / 100, null)",
        "utcFormat(datum.t, '%Y-%m') + ''",
        "[1, 2, 3][0] === 1",
        "{a: 1}.a",
        "brush.x",
        "datum.€ == 1".replace("€", "eur"),
        "datum.location",
        "-1e-3 <= datum.v && datum.v != 0x1F",
    ],
)
def test_safe_expressions(expr: str) -> None:
    assert check_expression(expr) == []


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ("datum.x = 1", "assignment"),
        ("a\\u0062", "escapes"),
        ("datum.é", "non-ASCII"),
        ("datum.a @ 1", "unexpected"),
        ("(datum.f)(1)", "calling"),
        ("datum[datum.k]", "computed member"),
        ("x" * 3000, "longer than"),
        ("globalThis", "not allowed"),
        ("new Date()", "not allowed"),
        ("self", "not allowed"),
    ],
)
def test_unsafe_expressions(expr: str, fragment: str) -> None:
    problems = check_expression(expr)
    assert problems and any(fragment in p for p in problems), problems


# ------------------------------------------------------------------------------- $ref


def test_plotly_ref_helpers_and_schema() -> None:
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(PLOTLY_REF_SCHEMA)
    v = jsonschema.Draft202012Validator(PLOTLY_REF_SCHEMA)
    assert v.is_valid({"$ref": "trace0.marker.color"})
    assert not v.is_valid({"$ref": "nodot"})
    assert not v.is_valid({"$ref": "a.b", "x": 1})
    assert is_plotly_ref({"$ref": "trace0.x"})
    assert not is_plotly_ref({"$ref": "../x"})
    assert parse_plotly_ref({"$ref": "trace0.marker.color"}) == ("trace0", "marker.color")
    assert parse_plotly_ref("d.x") == ("d", "x")
    with pytest.raises(ValueError):
        parse_plotly_ref("bad")
    shipped = resources.files("mapi_plots.sanitize.schemas").joinpath("plotly-ref.schema.json")
    assert json.loads(shipped.read_text("utf-8")) == PLOTLY_REF_SCHEMA


def test_plotly_refs_must_name_declared_datasets() -> None:
    spec = {"data": [{"type": "scatter", "x": {"$ref": "other.x"}}]}
    issues = check_plot(envelope("plotly", spec))
    assert issues and issues[0].context["rule"] == "envelope"
    assert (
        check_plot(envelope("plotly", {"data": [{"type": "scatter", "x": {"$ref": "d.x"}}]})) == []
    )


# ------------------------------------------------------------------------- vendored files


def test_vendored_schema_digests() -> None:
    pkg = resources.files("mapi_plots.sanitize.schemas")
    meta = json.loads(pkg.joinpath("VENDORED.json").read_text("utf-8"))
    for entry in meta.values():
        data = pkg.joinpath(entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert meta["vega-lite"]["url"] == VEGA_LITE_SCHEMA_URL


def test_core_import_pulls_no_heavy_dependencies() -> None:
    code = (
        "import sys, mapi_plots, mapi_plots.sanitize, mapi_plots.templates\n"
        "mapi_plots.sanitize.sanitize_spec('vega-lite', {'data': {'name': 'd'}, 'mark': 'point'})\n"
        "mapi_plots.sanitize.sanitize_spec('plotly', {'data': [{'type': 'scatter', 'x': [1]}]})\n"
        "heavy = {'numpy', 'pandas', 'xarray', 'altair', 'plotly', 'pyarrow'} & set(sys.modules)\n"
        "assert not heavy, heavy\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
