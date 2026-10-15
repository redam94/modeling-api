"""Plotly attribute value checks against the vendored plotly.js structure table."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_plots.sanitize import SanitizeError, SanitizeRule, sanitize_spec
from mapi_plots.sanitize.plotly import plotly_structure

pytestmark = pytest.mark.unit


def ok(spec: dict[str, Any]) -> dict[str, Any]:
    out, _ = sanitize_spec("plotly", spec)
    return out


def rules(spec: dict[str, Any]) -> set[SanitizeRule]:
    with pytest.raises(SanitizeError) as info:
        sanitize_spec("plotly", spec)
    return info.value.rules


def trace(**attrs: Any) -> dict[str, Any]:
    return {"data": [{"type": "scatter", **attrs}]}


def layout(**attrs: Any) -> dict[str, Any]:
    return {"data": [], "layout": attrs}


@pytest.mark.parametrize(
    "spec",
    [
        trace(x=[1, 2], y=[1, None], mode="markers+text", visible="legendonly"),
        trace(opacity=0.5, marker={"size": [1, 2], "color": 3, "colorscale": "Viridis"}),
        trace(marker={"colorscale": [[0, "red"], [1, "blue"]], "line": {"width": "2"}}),
        trace(xaxis="x3", yaxis="y", hoverinfo="x+y", textposition=["top left", "bottom center"]),
        trace(selectedpoints=[0, 1], customdata=[{"any": "thing"}], ids=["a", "b"]),
        {"data": [{"type": "bar", "orientation": "h", "width": 0.4}]},
        {"data": [{"type": "pie", "values": [1, 2], "labels": ["a", "b"], "rotation": 90}]},
        {"data": [{"type": "scatterpolar", "r": [1], "theta": [10]}], "layout": {"polar2": {}}},
        layout(xaxis={"range": [0, 1], "type": "date", "anchor": "y2"}, yaxis2={"overlaying": "y"}),
        layout(annotations=[{"xref": "x2 domain", "text": "a", "showarrow": False}]),
        layout(shapes=[{"type": "rect", "x0": 0, "x1": 1, "y0": 0, "y1": 1}], legend2={}),
        layout(colorway=["#111", "#222"], hovermode=False, barmode="stack", bargap=0.1),
        layout(
            sliders=[{"steps": [{"label": "a", "method": "restyle", "args": ["visible", True]}]}]
        ),
        layout(template={"data": {"bar": [{"marker": {"color": "red"}}]}}),
        {
            "data": [],
            "config": {"displayModeBar": "hover", "toImageButtonOptions": {"format": "svg"}},
        },
        {"data": [], "frames": [{"name": "a", "group": "g", "traces": [0], "baseframe": "b"}]},
        {"data": [{"type": "heatmap", "z": [[1, 2], [3, 4]]}]},
    ],
)
def test_valid_attributes_pass(spec: dict[str, Any]) -> None:
    assert ok(spec) == spec


@pytest.mark.parametrize(
    "spec",
    [
        trace(mode="sideways"),  # enumerated
        trace(mode=5),  # flaglist type
        trace(hoverinfo="x+nope"),  # flag not allowed
        trace(opacity="lots"),  # number
        trace(opacity=True),  # bool is not a number
        layout(xaxis={"nticks": 1.5}),  # integer
        trace(visible=[True]),  # not arrayOk
        trace(x="1,2,3"),  # data_array must be a list
        trace(xaxis="y2"),  # subplot id of the wrong axis
        trace(marker={"colorscale": [[0, "red"], ["x", 1]]}),  # colorscale stops
        trace(marker={"colorscale": 5}),
        trace(marker="red"),  # object expected
        trace(name={"text": "x"}),  # object where a string is expected
        trace(textposition=[["nested"]]),
        layout(annotations={"text": "not a list"}),
        layout(xaxis={"range": "0-1"}),  # info_array
        layout(colorway="red"),  # colorlist
        {"data": "not a list"},
        {"data": ["not an object"]},
        {"data": [{"type": 5}]},
        {"data": [], "layout": []},
        {"data": [], "layout": {"template": []}},
        {"data": [], "layout": {"template": {"data": []}}},
        {"data": [], "layout": {"template": {"data": {"bar": {}}}}},
        {"data": [], "frames": {}},
        {"data": [], "frames": ["x"]},
        {"data": [], "frames": [{"name": "a", "bogus": 1}]},
        {"data": [], "config": []},
        {"data": [], "config": {"nope": 1}},
        trace(x={"dtype": "f8", "bdata": "AAAAAAAAAAA=", "shape": "a, b"}),
        trace(x={"dtype": "f8", "bdata": "AAAAAAAAAAA=", "shape": [1, 2, 3, 4]}),
        trace(x={"dtype": "f8", "bdata": "AAAAAAAAAAA=", "shape": 3}),
        trace(x={"dtype": "f8", "bdata": "AAAAAAAAAAA=", "extra": 1}),
        layout(title={"text": {"dtype": "f8", "bdata": "AAAAAAAAAAA="}}),
    ],
)
def test_invalid_attributes_fail(spec: dict[str, Any]) -> None:
    found = rules(spec)
    assert found & {SanitizeRule.SCHEMA, SanitizeRule.TYPED_ARRAY}, found


def test_typed_array_shapes_accepted() -> None:
    six = "AAAAAAAAAAAAAAAAAADwPwAAAAAAAABAAAAAAAAACEAAAAAAAAAQQAAAAAAAABRA"
    ok({"data": [{"type": "heatmap", "z": {"dtype": "f8", "bdata": six, "shape": [2, 3]}}]})
    ok(trace(x={"dtype": "f8", "bdata": six}))
    ok(layout(xaxis={"range": {"dtype": "f8", "bdata": "AAAAAAAAAAAAAAAAAADwPw=="}}))


def test_structure_table_is_complete_enough() -> None:
    s = plotly_structure()
    assert s.version == "4.1.1"
    for kind in ("scatter", "bar", "heatmap", "scattergeo", "choropleth", "surface", "sankey"):
        assert s.trace(kind) is not None
    assert s.trace("scattermapbox") is None  # removed upstream; tile maps rejected anyway
    assert "images" in s.layout["k"] and "xaxis" in s.layout["k"]
