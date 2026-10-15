from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.pandas import column, data_frames

alt = pytest.importorskip("altair")
go = pytest.importorskip("plotly.graph_objects")

from mapi_plots.convert import (  # noqa: E402
    arrow_to_frame,
    decode_typed_array,
    frame_to_arrow,
    from_altair,
    from_plotly,
    make_provenance,
)
from mapi_plots.sanitize import SanitizeError, parse_plotly_ref, sanitize_plot  # noqa: E402
from mapi_spec.plots import MAX_INLINE_ARROW_B64, InlineArrow  # noqa: E402

pytestmark = pytest.mark.unit


def _refs(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        if set(node) == {"$ref"}:
            out.append(node["$ref"])
        for v in node.values():
            out += _refs(v)
    elif isinstance(node, list):
        for v in node:
            out += _refs(v)
    return out


# --------------------------------------------------------------------------------- arrow


def test_frame_roundtrip_with_datetimes_and_categories() -> None:
    df = pd.DataFrame(
        {
            "t": pd.date_range("2024-01-01", periods=3, freq="D"),
            "tz": pd.date_range("2024-01-01", periods=3, freq="h", tz="Europe/Paris"),
            "g": pd.Categorical(["a", "b", "a"]),
            "v": [1.5, np.nan, 3.0],
        }
    )
    arrow = frame_to_arrow(df)
    assert isinstance(arrow, InlineArrow) and arrow.rows == 3
    back = arrow_to_frame(arrow)
    assert str(back["t"].dtype) == "datetime64[ms]"
    assert back["t"].tolist() == df["t"].tolist()
    assert str(back["tz"].dt.tz) == "UTC"
    assert back["g"].tolist() == ["a", "b", "a"]
    assert np.isnan(back["v"][1])
    assert arrow_to_frame(arrow.data).equals(back)


def test_compact_encoding_halves_payload() -> None:
    n = 2000
    df = pd.DataFrame(
        {"x": np.linspace(0, 1, n), "i": np.arange(n), "s": ["series"] * n, "big": [2**40] * n}
    )
    full, small = frame_to_arrow(df), frame_to_arrow(df, compact=True)
    assert len(small.data) < 0.75 * len(full.data)
    back = arrow_to_frame(small)
    assert back["x"].dtype == np.float32 and back["i"].dtype == np.int32
    assert back["big"].dtype == np.int64  # does not fit in int32
    assert back["s"].astype(str).tolist() == df["s"].tolist()
    np.testing.assert_allclose(back["x"], df["x"], rtol=1e-6)


def test_inline_size_limit() -> None:
    df = pd.DataFrame({"x": np.random.default_rng(0).random(MAX_INLINE_ARROW_B64 // 6)})
    with pytest.raises(ValueError, match="Downsample"):
        frame_to_arrow(df)


@settings(max_examples=40, deadline=None)
@given(
    data_frames(
        [
            column("a", dtype=float, elements=st.floats(-1e9, 1e9, allow_nan=False)),
            column("b", dtype=int, elements=st.integers(-(2**40), 2**40)),
            column("c", elements=st.text(max_size=5)),
        ]
    )
)
def test_arrow_roundtrip_property(df: pd.DataFrame) -> None:
    back = arrow_to_frame(frame_to_arrow(df))
    assert len(back) == len(df)
    np.testing.assert_array_equal(back["a"].to_numpy(), df["a"].to_numpy())
    np.testing.assert_array_equal(back["b"].to_numpy(), df["b"].to_numpy())
    assert back["c"].tolist() == df["c"].tolist()


# --------------------------------------------------------------------------- typed arrays


@pytest.mark.parametrize(
    ("dtype", "values"),
    [("f8", [0.5, -2.0]), ("f4", [1.5, 2.5]), ("i1", [-3, 4]), ("u2", [1, 65535]), ("i4", [7])],
)
def test_decode_typed_array(dtype: str, values: list[float]) -> None:
    np_dtype = {"f8": "<f8", "f4": "<f4", "i1": "<i1", "u2": "<u2", "i4": "<i4"}[dtype]
    raw = np.asarray(values, dtype=np_dtype).tobytes()
    arr = decode_typed_array({"dtype": dtype, "bdata": base64.b64encode(raw).decode()})
    np.testing.assert_array_equal(arr, np.asarray(values))
    grid = np.arange(6.0).reshape(2, 3)
    enc = {"dtype": "f8", "bdata": base64.b64encode(grid.tobytes()).decode(), "shape": "2, 3"}
    np.testing.assert_array_equal(decode_typed_array(enc), grid)
    enc["shape"] = [3, 2]
    assert decode_typed_array(enc).shape == (3, 2)
    with pytest.raises(ValueError):
        decode_typed_array({"dtype": "i8", "bdata": ""})


# ---------------------------------------------------------------------------------- plotly


def test_from_plotly_moves_typed_arrays_into_named_datasets() -> None:
    fig = go.Figure(
        [
            go.Scatter(
                x=pd.date_range("2024-01-01", periods=4),
                y=np.arange(4.0),
                text=["a", "b", "c", "d"],
                marker={"color": np.arange(4), "size": 6},
                name="<b>fit</b><script>x</script>",
            ),
            go.Heatmap(z=np.arange(6.0).reshape(2, 3), x=[1, 2, 3]),
            go.Bar(x=["only"], y=[1]),
        ]
    )
    fig.update_layout(title={"text": "Demand <i>by</i> store"})
    plot = from_plotly(fig, id="demo", author="tests", min_length=2)
    assert plot.kind == "plotly" and plot.schema_version == "4.1.1"
    assert plot.title == "Demand <i>by</i> store"
    assert plot.provenance.author == "tests"
    data = plot.spec["data"]
    assert data[0]["name"] == "<b>fit</b>x"  # sanitized
    refs = _refs(plot.spec)
    datasets = {parse_plotly_ref(r)[0] for r in refs}
    assert datasets <= set(plot.datasets)
    t0 = arrow_to_frame(plot.datasets["trace0"])
    assert set(t0.columns) == {"x", "y", "text", "marker.color"}
    assert t0["y"].tolist() == [0.0, 1.0, 2.0, 3.0]
    assert data[0]["marker"]["size"] == 6
    # heatmap: z (2 rows of 3) and x (length 3) live in different-length datasets
    z_ref = data[1]["z"]["$ref"]
    zds, zcol = parse_plotly_ref(z_ref)
    z = arrow_to_frame(plot.datasets[zds])[zcol]
    assert [list(r) for r in z] == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]]
    assert parse_plotly_ref(data[1]["x"]["$ref"])[0] != zds
    # arrays shorter than min_length stay inline
    assert data[2]["x"] == ["only"]
    assert "template" not in plot.spec["layout"]
    assert sanitize_plot(plot).changes == []


def test_from_plotly_frames_template_and_dicts() -> None:
    fig = go.Figure(
        data=[go.Scatter(x=[1, 2], y=[3, 4])],
        frames=[go.Frame(name="f1", data=[go.Scatter(x=[1, 2], y=[5, 6])])],
    )
    plot = from_plotly(fig, id="anim", keep_template=True)
    assert "frame0_trace0" in plot.datasets
    assert "template" in plot.spec["layout"]
    plain = from_plotly(
        {"data": [{"type": "bar", "x": ["a", "b"], "y": [1, None]}], "layout": {}}, id="d"
    )
    assert arrow_to_frame(plain.datasets["trace0"])["y"].isna().tolist() == [False, True]
    assert plain.title == "d"


def _px_figures() -> dict[str, Any]:
    px = pytest.importorskip("plotly.express")
    rng = np.random.default_rng(0)
    df = pd.DataFrame(
        {
            "x": rng.normal(size=60),
            "y": rng.normal(size=60),
            "z": rng.normal(size=60),
            "g": rng.choice(list("abc"), 60),
            "t": np.tile(np.arange(6), 10),
            "iso": rng.choice(["USA", "FRA", "DEU"], 60),
        }
    )
    return {
        "facets": px.scatter(df, x="x", y="y", color="g", facet_col="g", marginal_x="histogram"),
        "bar": px.bar(df, x="g", y="y", color="g", barmode="group"),
        "box": px.box(df, x="g", y="y", points="all"),
        "imshow": px.imshow(rng.random((4, 4)), text_auto=True),
        "geo": px.scatter_geo(df, locations="iso", color="g"),
        "choropleth": px.choropleth(df, locations="iso", color="y"),
        "scatter3d": px.scatter_3d(df, x="x", y="y", z="z"),
        "animation": px.scatter(df, x="x", y="y", animation_frame="t"),
        "sunburst": px.sunburst(df, path=["g", "iso"]),
    }


def test_plotly_express_figures_pass_the_sanitizer() -> None:
    for name, fig in _px_figures().items():
        for keep in (False, True):
            plot = from_plotly(fig, id=name, keep_template=keep)
            assert plot.kind == "plotly", name


def test_from_plotly_strips_html_from_strings_moved_into_datasets() -> None:
    """Text arrays become Arrow columns the JSON sanitizer never decodes; plotly.js still
    renders their HTML, so the conversion strips it (sandbox side) like inline text."""
    fig = go.Figure(
        go.Scatter(
            x=["<a href='https://evil.example/?q=1'>click</a>", "<b>ok</b>"],
            y=[1, 2],
            text=[
                "<a href='https://evil.example/?q=1'>click</a>",
                "<span style='background:url(https://evil.example/x)'>s</span>",
            ],
            hovertext=["<img src=x onerror=alert(1)>h", "<i>i</i>"],
            hovertemplate=["%{y}<extra>a</extra>", "<a href='x'>b</a>"],
            mode="markers+text",
        )
    )
    plot = from_plotly(fig, id="p")
    trace = plot.spec["data"][0]
    assert trace["text"] == {"$ref": "trace0.text"}
    frame = arrow_to_frame(plot.datasets["trace0"])
    assert frame["text"].tolist() == ["click", "s"]
    assert frame["x"].tolist() == ["click", "<b>ok</b>"]
    assert frame["hovertext"].tolist() == ["h", "<i>i</i>"]
    assert frame["hovertemplate"].tolist() == ["%{y}<extra>a</extra>", "b"]
    for col in frame.columns:
        for value in frame[col]:
            assert not isinstance(value, str) or ("href" not in value and "url(" not in value)
    # a script URL left as plain text is rejected just like an inline string
    bad = go.Figure(go.Scatter(x=[1, 2], y=[1, 2], text=["javascript:alert(1)", "b"]))
    with pytest.raises(SanitizeError) as info:
        from_plotly(bad, id="bad")
    assert info.value.issues[0].pointer.startswith("/datasets/trace0")
    # sanitize=False leaves the strings alone
    raw = from_plotly(fig, id="raw", sanitize=False)
    assert "href" in arrow_to_frame(raw.datasets["trace0"])["text"][0]


def test_from_plotly_rejects_unsafe_figures() -> None:
    fig = go.Figure(go.Scatter(x=[1], y=[1]))
    fig.update_layout(images=[{"source": "https://evil.example/x.png", "x": 0, "y": 0}])
    with pytest.raises(SanitizeError):
        from_plotly(fig, id="bad")
    unsafe = from_plotly(fig, id="bad", sanitize=False)
    assert unsafe.spec["layout"]["images"][0]["source"].startswith("https://")


# ---------------------------------------------------------------------------------- altair


def test_from_altair_names_inline_and_explicit_datasets() -> None:
    df = pd.DataFrame({"t": pd.date_range("2024-01-07", periods=4, freq="W"), "y": [1, 2, 3, 4.0]})
    inline = alt.Chart(df).mark_line().encode(x="t:T", y="y:Q")
    named = alt.Chart(alt.Data(name="pts")).mark_point().encode(x="t:T", y="y:Q")
    values = alt.Chart(alt.Data(values=[{"a": 1}, {"a": 2}])).mark_tick().encode(x="a:Q")
    chart = alt.vconcat(inline + named, values).properties(title="Demo")
    plot = from_altair(chart, id="demo", datasets={"pts": df}, recipe="x.y@1.0.0")
    assert set(plot.datasets) == {"pts", "inline_0", "values_0"}
    text = json.dumps(plot.spec)
    assert '"values"' not in text and "data-" not in text
    assert plot.title == "Demo" and plot.provenance.recipe == "x.y@1.0.0"
    assert arrow_to_frame(plot.datasets["values_0"])["a"].tolist() == [1, 2]
    scalars = alt.Chart(alt.Data(values=[1, 2, 3])).mark_tick().encode(x="data:Q")
    p2 = from_altair(scalars, id="s")
    assert arrow_to_frame(p2.datasets["values_0"])["data"].tolist() == [1, 2, 3]


def test_altair_feature_corpus_passes_the_sanitizer() -> None:
    rng = np.random.default_rng(0)
    df = pd.DataFrame(
        {
            "x": rng.normal(size=30),
            "y": rng.normal(size=30),
            "g": rng.choice(list("abc"), 30),
            "t": pd.date_range("2024", periods=30, freq="D"),
        }
    )
    base = alt.Chart(df)
    brush = alt.selection_interval(encodings=["x"])
    click = alt.selection_point(fields=["g"], bind="legend")
    cutoff = alt.param(value=1.0, bind=alt.binding_range(min=0, max=3, step=0.1))
    charts = [
        base.mark_point().encode(x="x", y="y").interactive(),
        base.mark_point()
        .encode(x="x", y="y", color=alt.condition(brush, "g:N", alt.value("gray")))
        .add_params(brush),
        base.mark_point()
        .encode(x="x", opacity=alt.condition(click, alt.value(1), alt.value(0.2)))
        .add_params(click),
        base.mark_point()
        .encode(x="x", color=alt.condition(alt.datum.y > cutoff, alt.value("red"), alt.value("b")))
        .add_params(cutoff),
        base.transform_calculate(z="datum.x * 2 + abs(datum.y)")
        .transform_filter(alt.datum.z > 0)
        .transform_window(r="rank()", sort=[alt.SortField("z")])
        .mark_bar()
        .encode(x="g:N", y="mean(z):Q"),
        base.transform_regression("x", "y").mark_line().encode(x="x", y="y"),
        base.transform_density("x", as_=["x", "density"])
        .mark_area()
        .encode(x="x:Q", y="density:Q"),
        base.mark_point().encode(x=alt.X("x", axis=alt.Axis(labelExpr="datum.value + ' u'"))),
        alt.Chart(df)
        .mark_point()
        .encode(alt.X(alt.repeat("column"), type="quantitative"))
        .repeat(column=["x", "y"]),
        base.mark_errorband(extent="ci").encode(x="t:T", y="y:Q"),
    ]
    for i, chart in enumerate(charts):
        assert from_altair(chart, id=f"c{i}").kind == "vega-lite"


def test_from_altair_rejects_url_data_and_duplicate_names() -> None:
    url_chart = alt.Chart("https://evil.example/data.csv").mark_point().encode(x="a:Q")
    with pytest.raises(SanitizeError):
        from_altair(url_chart, id="u")
    spec = {
        "data": {"values": [{"a": 1}]},
        "mark": "point",
        "encoding": {"x": {"field": "a", "type": "quantitative"}},
    }
    with pytest.raises(ValueError, match="used twice"):
        from_altair(spec, id="dup", datasets={"values_0": pd.DataFrame({"a": [1]})})


def test_make_provenance_defaults() -> None:
    p = make_provenance()
    assert p.author == "notebook" and p.source == "author"
    q = make_provenance(plugin="mmm.geo@1.0.0", generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert q.plugin and q.author is None
