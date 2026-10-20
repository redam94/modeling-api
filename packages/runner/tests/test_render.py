"""Static rendering (``render`` runs): golden image sizes, the no-network rules, data inputs.

Vega-Lite tests need ``vl-convert-python`` (a pinned runner dependency) and are skipped
where it is missing. Plotly needs kaleido and a Chrome, which the runner images do not ship:
that path must refuse with ``MODE_NOT_SUPPORTED`` and never download a browser.
"""

from __future__ import annotations

import base64
import io
import json
import re
import struct
from typing import Any

import pyarrow as pa
import pytest
from mapi_runner.worker import render as render_mod
from mapi_runner.worker.render import find_fetching_constructs, render_plot
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.plots import PlotSpec
from mapi_spec.run import ErrorPayload, RunnerExit
from runner_helpers import make_run, run_supervisor

vl_convert = pytest.importorskip("vl_convert", reason="vl-convert-python is not installed")

GENERATED = "2026-10-15T09:40:00Z"


def _arrow(rows: list[dict[str, Any]]) -> bytes:
    table = pa.Table.from_pylist(rows)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


ROWS = [{"t": i, "y": float(i * i)} for i in range(10)]


def _plot(spec: dict[str, Any], datasets: dict[str, Any] | None = None, **kw: Any) -> PlotSpec:
    doc = {
        "id": kw.pop("id", "core.ppc_fan"),
        "title": "Fan",
        "kind": kw.pop("kind", "vega-lite"),
        "schema_version": "v5",
        "spec": spec,
        "datasets": datasets
        if datasets is not None
        else {"fan": {"kind": "inline_arrow", "data": base64.b64encode(_arrow(ROWS)).decode()}},
        "provenance": {"recipe": "core.ppc_fan@1.0.0", "generated_at": GENERATED},
        **kw,
    }
    return PlotSpec.model_validate(doc)


# A view whose pixel size is exactly width x height: no padding, no axes, no view stroke.
BARE = {
    "mark": "line",
    "data": {"name": "fan"},
    "width": 300,
    "height": 200,
    "padding": 0,
    "autosize": {"type": "none"},
    "config": {"view": {"stroke": None}},
    "encoding": {
        "x": {"field": "t", "type": "quantitative", "axis": None},
        "y": {"field": "y", "type": "quantitative", "axis": None},
    },
}


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    return int(w), int(h)


@pytest.mark.parametrize(("scale", "expected"), [(1.0, (300, 200)), (2.0, (600, 400))])
def test_vega_lite_png_has_the_golden_size(scale: float, expected: tuple[int, int]) -> None:
    assert _png_size(render_plot(_plot(BARE), "png", scale=scale)) == expected


def test_width_and_height_override_the_plot_size() -> None:
    png = render_plot(_plot(BARE), "png", scale=1, width=500, height=120)
    assert _png_size(png) == (500, 120)
    svg = render_plot(_plot(BARE), "svg", width=640, height=480).decode()
    head = svg[: svg.index(">")]
    assert 'width="640"' in head and 'height="480"' in head


def test_svg_is_text_with_the_golden_size_and_no_external_references() -> None:
    svg = render_plot(_plot(BARE), "svg").decode()
    assert svg.startswith("<svg") and 'width="300"' in svg[:300] and 'height="200"' in svg[:300]
    assert not re.search(r'(?:href|src)="(?:https?:)?//', svg)


def test_pdf_page_has_the_golden_size() -> None:
    pdf = render_plot(_plot(BARE), "pdf", scale=1)
    assert pdf.startswith(b"%PDF-")
    box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
    assert box is not None
    assert (round(float(box.group(1))), round(float(box.group(2)))) == (300, 200)


def test_composite_views_take_the_size_per_cell() -> None:
    spec = {
        "hconcat": [
            {k: v for k, v in BARE.items() if k not in ("width", "height", "padding", "config")},
            {k: v for k, v in BARE.items() if k not in ("width", "height", "padding", "config")},
        ],
        "padding": 0,
        "config": {"view": {"stroke": None}},
    }
    w, h = _png_size(render_plot(_plot(spec), "png", scale=1, width=100, height=50))
    assert 50 <= h <= 56 and 200 <= w <= 240  # two 100x50 cells plus concat spacing/bounds


def test_a_raster_larger_than_the_pixel_cap_is_refused_before_it_is_drawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scale <= 8 and width/height <= 4096 allow a 32768^2 RGBA raster (4 GiB); the plot's
    own size (here a composite of four 4000x4000 cells) counts too, and is only known once
    Vega-Lite lays it out."""
    from mapi_spec.sharing import MAX_RENDER_PIXELS

    drawn: list[str] = []
    real_png = vl_convert.vegalite_to_png
    monkeypatch.setattr(
        vl_convert, "vegalite_to_png", lambda *a, **k: drawn.append("png") or real_png(*a, **k)
    )
    with pytest.raises(MapiError) as info:
        render_plot(_plot(BARE), "png", scale=8, width=4096, height=4096)
    assert info.value.code is ErrorCode.PAYLOAD_TOO_LARGE and "megapixels" in info.value.detail
    cell = {k: v for k, v in BARE.items() if k not in ("padding", "config")}
    cell = {**cell, "width": 4000, "height": 4000}
    grid = {"vconcat": [{"hconcat": [cell, cell]}, {"hconcat": [cell, cell]}], "padding": 0}
    for fmt in ("png", "pdf"):
        with pytest.raises(MapiError) as info:
            render_plot(_plot(grid), fmt, scale=1)
        assert info.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert drawn == []  # nothing was rasterised
    # at the cap's edge a plain render still works
    side = int((MAX_RENDER_PIXELS / 4) ** 0.5) - 8
    assert _png_size(render_plot(_plot(BARE), "png", scale=2, width=side, height=50))[0] > side
    with pytest.raises(MapiError) as info:
        render_plot(
            _plot({"data": [{"type": "scatter"}], "layout": {"width": 9000, "height": 9000}},
                  kind="plotly", schema_version="3.1.0"),
            "png",
            scale=1,
        )  # fmt: skip
    assert info.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_data_ref_inputs_are_read_through_the_loader() -> None:
    plot = _plot(
        BARE,
        {"fan": {"kind": "data_ref", "path": "plotdata.0", "format": "arrow"}},
    )
    asked: list[str] = []

    def load(name: str) -> bytes:
        asked.append(name)
        return _arrow(ROWS)

    assert _png_size(render_plot(plot, "png", scale=1, load=load)) == (300, 200)
    assert asked == ["plotdata.0"]
    json_plot = _plot(BARE, {"fan": {"kind": "data_ref", "path": "plotdata.0", "format": "json"}})
    out = render_plot(json_plot, "png", scale=1, load=lambda _n: json.dumps(ROWS).encode())
    assert _png_size(out) == (300, 200)


def test_other_data_refs_are_refused() -> None:
    plot = _plot(BARE, {"fan": {"kind": "data_ref", "path": "ui/plotdata/fan.arrow"}})
    with pytest.raises(NotImplementedError, match="plotdata"):
        render_plot(plot, "png", load=lambda _n: b"")


@pytest.mark.parametrize(
    "spec",
    [
        {
            "mark": "image",
            "data": {"name": "fan"},
            "encoding": {"url": {"value": "https://attacker.example/beacon.png"}},
        },
        {"mark": {"type": "image"}, "data": {"name": "fan"}},
        {"mark": "text", "data": {"name": "fan"}, "encoding": {"href": {"field": "t"}}},
    ],
)
def test_constructs_that_could_fetch_are_refused_before_rendering(spec: dict[str, Any]) -> None:
    assert find_fetching_constructs("vega-lite", spec)
    with pytest.raises(MapiError) as info:
        render_plot(_plot(spec), "png")
    assert info.value.code is ErrorCode.PLOT_UNSAFE


def test_vl_convert_itself_runs_without_network_access() -> None:
    """The second line of defence: the converter refuses loader requests we would never
    make (url data cannot even be expressed in a PlotSpec)."""
    spec = {"mark": "point", "data": {"url": "https://attacker.example/x.json"}}
    with pytest.raises(ValueError, match="not allowed"):
        vl_convert.vegalite_to_svg(spec, allowed_base_urls=[])


def test_plotly_without_a_chrome_is_mode_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_mod, "find_chrome", lambda: None)
    plot = _plot(
        {"data": [{"type": "scatter", "x": {"$ref": "fan.t"}, "y": {"$ref": "fan.y"}}]},
        kind="plotly",
        schema_version="3.1.0",
    )
    with pytest.raises(MapiError) as info:
        render_plot(plot, "png")
    assert info.value.code is ErrorCode.MODE_NOT_SUPPORTED
    assert "Chrome" in info.value.detail


def test_plotly_tile_maps_and_images_are_refused() -> None:
    assert find_fetching_constructs("plotly", {"data": [{"type": "scattermap"}]})
    assert find_fetching_constructs("plotly", {"data": [{"type": "image", "source": "x"}]})
    assert find_fetching_constructs("plotly", {"layout": {"images": [{"source": "x"}]}})
    assert not find_fetching_constructs("plotly", {"data": [{"type": "heatmap"}]})
    # An image trace with inline pixels (ml.misclassified) fetches nothing.
    assert not find_fetching_constructs("plotly", {"data": [{"type": "image", "z": [[[0, 0, 0]]]}]})


def test_render_run_reads_plot_data_inputs_and_writes_renders(gcs: Any) -> None:
    runs = gcs.buckets.runs
    plot = _plot(BARE, {"fan": {"kind": "data_ref", "path": "plotdata.0", "format": "arrow"}})
    gcs.put(
        runs,
        "t/default/runs/x/inputs/plots.json",
        json.dumps([plot.model_dump(mode="json")]).encode(),
    )
    gcs.put(runs, "t/default/x/fan.arrow", _arrow(ROWS))
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.RENDER,
            {"plots": ["core.ppc_fan"], "format": "png", "scale": 1, "width": 320, "height": 160},
            inputs={
                "plots": (runs, "t/default/runs/x/inputs/plots.json"),
                "plotdata.0": (runs, "t/default/x/fan.arrow"),
            },
        )
    )
    assert result.code == RunnerExit.OK, result.stderr[-2000:]
    assert _png_size(result.output("renders/core.ppc_fan.png")) == (320, 160)
    assert "renders/core.ppc_fan.png" in result.manifest().objects


def test_render_run_reports_unsafe_plots_with_a_stable_code(gcs: Any) -> None:
    runs = gcs.buckets.runs
    spec = {
        "mark": "image",
        "data": {"name": "fan"},
        "encoding": {"url": {"value": "https://attacker.example/beacon.png"}},
    }
    doc = _plot(spec).model_dump(mode="json")
    gcs.put(runs, "t/default/y/plots.json", json.dumps([doc]).encode())
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.RENDER,
            {"format": "png"},
            inputs={"plots": (runs, "t/default/y/plots.json")},
        )
    )
    assert result.code == RunnerExit.USER_ERROR
    err = ErrorPayload.model_validate(result.of("error")[0].payload)
    # the supervisor never trusts the worker's own code; it names it in the message
    assert err.code is ErrorCode.USER_CODE_ERROR and "PLOT_UNSAFE" in err.message
