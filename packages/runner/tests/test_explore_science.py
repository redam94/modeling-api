"""The scientific Data Explorer sections: every case study gives valid, capped views with
plots that pass the sanitizer; the numbers match hand-computed values; the classic sections
behave on N-D data (no pixel series, no observation index as time, real groups, the right
correlation columns); and, when a pymc-challenges copy is given
(``MAPI_PYMC_CHALLENGES_DATA``), the same holds on the real data."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from gov_fixtures import FakeCtx
from mapi_plots.sanitize import sanitize_plot
from mapi_runner.worker.explore import write_data_explorer, write_sections
from mapi_runner.worker.explore_data import (
    DATA_SECTION_FUNCS,
    SectionSkipped,
    data_context,
    ingest_sections,
)
from mapi_runner.worker.explore_science import (
    _circ_stats,
    _js_rho,
    _lab_to_hex,
    _moran,
    science_plots,
)
from mapi_spec.explore import DataExploreRequest
from mapi_spec.schema import DataSchema
from mapi_spec.views import (
    DATA_SECTIONS,
    MAX_FIELD_CELLS,
    MAX_FIELD_FRAMES,
    MAX_FIELD_PIXELS,
    MAX_FIELDS,
    MAX_MAP_VERTICES,
    MAX_NET_EDGES,
    MAX_NET_NODES,
    MAX_POINTS,
    MAX_PROFILE_FRAMES,
    SCIENCE_SECTIONS,
    CircularView,
    CorrelationView,
    DataOverviewView,
    DistributionsView,
    ExplorerIndex,
    GroupComparisonView,
    ImagesView,
    MapView,
    MissingnessView,
    NetworkView,
    OutliersView,
    PointsView,
    ProfilesView,
    QualityView,
    StructureView,
    TimeSeriesView,
    TreeView,
)
from pydantic import BaseModel

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore::FutureWarning")]

science = pytest.importorskip("mapi_plugin_science.export")
CASES = sorted(science.EXPORTS)
VIEWS: dict[str, type[BaseModel]] = {
    "structure": StructureView,
    "images": ImagesView,
    "profiles": ProfilesView,
    "map": MapView,
    "network": NetworkView,
    "tree": TreeView,
    "circular": CircularView,
    "points": PointsView,
}


def case_data(case: str) -> tuple[xr.Dataset, DataSchema]:
    c = science.EXPORTS[case]
    ds = c.synthetic()
    assert isinstance(ds, xr.Dataset) and isinstance(c.schema, DataSchema)
    return ds, c.schema


def _ingest(
    tmp_path: Path, ds: xr.Dataset, schema: DataSchema | None
) -> tuple[FakeCtx, ExplorerIndex]:
    ctx = FakeCtx(tmp_path)
    index = write_data_explorer(ctx, ds, schema, ref="dataset://sci/case@v1")  # type: ignore[arg-type]
    assert index is not None
    return ctx, index


def _check_ingest(ctx: FakeCtx, index: ExplorerIndex) -> dict[str, Any]:
    """Every ok section validates and its plots pass the sanitizer; every section that is
    not written says why."""
    assert not ctx.warnings, ctx.warnings
    stored = ExplorerIndex.model_validate(ctx.written["ui/explore/index.json"])
    assert stored == index
    status = {s.section: s for s in index.sections}
    assert index.order is not None and index.shapes
    assert set(index.order) <= set(DATA_SECTIONS)
    for ref in index.sections:
        if ref.status == "skipped":
            assert ref.reason is not None and ref.message_plain
            continue
        assert ref.status == "ok", ref
        body = ctx.written[ref.view]
        json.dumps(body, allow_nan=False)
        if ref.section in VIEWS:
            VIEWS[ref.section].model_validate(body)
        for p in ctx.written.get(f"ui/explore/plots/{ref.section}.json", []):
            sanitize_plot(p)
    return status


@pytest.mark.parametrize("case", CASES)
def test_the_ingest_of_every_case_study(case: str, tmp_path: Path) -> None:
    ds, schema = case_data(case)
    ctx, index = _ingest(tmp_path, ds, schema)
    status = _check_ingest(ctx, index)
    c = data_context(ds, schema)
    # the ingest writes the applicable picture sections and lists the others as such
    for section in SCIENCE_SECTIONS:
        if section in c.shapes.not_applicable:
            assert status[section].reason == "not_applicable"
        else:
            assert status[section].status == "ok", section
    assert set(ingest_sections(c.shapes)) <= set(status)
    assert index.order == list(c.shapes.recommended)
    picture = [s for s in index.order if s in SCIENCE_SECTIONS and s != "structure"]
    assert picture, "every case study has a picture of its data"
    assert all(status[s].status == "ok" for s in picture)


@pytest.mark.parametrize("case", CASES)
def test_every_section_on_demand(case: str) -> None:
    """An explore run of all 21 sections: each one is a valid view or a reasoned skip."""
    ds, schema = case_data(case)
    c = data_context(ds, schema, DataExploreRequest(max_lag=4))
    for section in DATA_SECTIONS:
        try:
            view = DATA_SECTION_FUNCS[section](c)
        except SectionSkipped as exc:
            assert exc.reason in ("not_applicable", "insufficient_data", "needs_option")
            continue
        body = view.model_dump(mode="json", exclude_none=True)
        json.dumps(body, allow_nan=False)
        if section == "groups":
            assert body["items"], "groups never writes an empty view"


def test_science_plots_add_the_plotly_recipes() -> None:
    for case, section, rid in (
        ("e18", "map", "sci.area_map"),
        ("e21", "network", "net.graph"),
        ("e81", "circular", "sci.rose"),
    ):
        ds, schema = case_data(case)
        c = data_context(ds, schema)
        plots = science_plots(section, ds, c.shapes)
        assert plots and all(p["kind"] == "plotly" for p in plots), case
        assert plots[0]["id"].startswith(rid)
        ids = [p["id"] for p in plots]
        assert len(set(ids)) == len(ids), ids  # unique React keys and export names
        for p in plots:
            sanitize_plot(p)
    ds, schema = case_data("e07")
    assert science_plots("images", ds, data_context(ds, schema).shapes) == []


# ------------------------------------------------------------- classic sections, N-D


def test_an_observation_index_is_not_time() -> None:
    ds, schema = case_data("e81")
    c = data_context(ds, schema)
    assert c.time_dim is None
    for section in ("timeseries", "ccf", "acf_pacf", "stationarity", "stl"):
        assert section in c.shapes.not_applicable
        with pytest.raises(SectionSkipped) as exc:
            DATA_SECTION_FUNCS[section](c)
        assert exc.value.reason == "not_applicable"
    q = DATA_SECTION_FUNCS["quality"](c)
    assert isinstance(q, QualityView)
    assert "SHORT_HISTORY" not in {w.code for w in q.items}


def test_image_pixels_are_reduced_into_one_series() -> None:
    ds, schema = case_data("e07")
    c = data_context(ds, schema)
    ts = DATA_SECTION_FUNCS["timeseries"](c)
    assert isinstance(ts, TimeSeriesView)
    assert len(ts.series) == 1 and ts.omitted_series == 0 and ts.series[0].facet == {}
    total = ds["quakes"].sum(("row", "col")).values
    assert ts.series[0].values == pytest.approx([float(v) for v in total])
    miss = DATA_SECTION_FUNCS["missingness"](c)
    assert isinstance(miss, MissingnessView) and miss.grid is not None
    assert miss.grid.along == "quarter"


def test_a_sequence_is_not_time_and_a_short_series_is_reported() -> None:
    ds, schema = case_data("e61")
    c = data_context(ds, schema)
    assert c.time_dim == "t" and not c.strong_time
    ts = DATA_SECTION_FUNCS["timeseries"](c)
    assert isinstance(ts, TimeSeriesView) and len(ts.series) == 1
    with pytest.raises(SectionSkipped, match="sequence"):
        DATA_SECTION_FUNCS["stl"](c)
    q = DATA_SECTION_FUNCS["quality"](c)
    assert "SHORT_HISTORY" not in {w.code for w in q.items}  # type: ignore[attr-defined]
    ds, schema = case_data("e07")  # real time, 16 quarters: enough
    c = data_context(ds.isel(quarter=slice(0, 10)), schema)
    with pytest.raises(SectionSkipped) as exc:
        DATA_SECTION_FUNCS["acf_pacf"](c)
    assert exc.value.reason == "insufficient_data"


def test_correlation_uses_the_traits_and_the_table_columns() -> None:
    ds, schema = case_data("e23")
    c = data_context(ds, schema)
    assert not {"parent", "branch_length", "is_tip"} & set(c.vars)
    corr = DATA_SECTION_FUNCS["correlation"](c)
    assert isinstance(corr, CorrelationView)
    assert corr.vars == ["brain", "body", "group_size"]
    ds, schema = case_data("e19")
    c = data_context(ds, schema)
    corr = DATA_SECTION_FUNCS["correlation"](c)
    assert isinstance(corr, CorrelationView)
    proteins = [str(p) for p in ds["protein"].values]
    assert corr.vars == [f"level[{p}]" for p in proteins]
    dist = DATA_SECTION_FUNCS["distributions"](c)
    assert isinstance(dist, DistributionsView)
    assert [d.var for d in dist.items] == [f"level[{p}]" for p in proteins]
    ds, schema = case_data("e80")
    corr = DATA_SECTION_FUNCS["correlation"](data_context(ds, schema))
    assert isinstance(corr, CorrelationView) and corr.vars == ["image", "weight"]


def test_geometry_is_not_data() -> None:
    ds, schema = case_data("e18")
    c = data_context(ds, schema)
    assert c.vars == ["cases", "expected", "aff"]
    out = DATA_SECTION_FUNCS["outliers"](c)
    assert isinstance(out, OutliersView) and {i.var for i in out.items} == set(c.vars)
    q = DATA_SECTION_FUNCS["quality"](c)
    assert not {w.var for w in q.items} & {"ring_x", "ring_y", "adjacency"}  # type: ignore[attr-defined]
    ov = DATA_SECTION_FUNCS["overview"](c)
    assert isinstance(ov, DataOverviewView) and ov.shapes and ov.shapes[0].kind == "areal_map"
    used = {v.name: v.used_as for v in ov.vars}
    assert used["ring_x"] == "geometry" and used["cases"] == "data"
    assert "map of areas" in ov.summary_plain


def test_groups_use_real_categories_or_grouping_variables() -> None:
    ds, schema = case_data("e51")
    g = DATA_SECTION_FUNCS["groups"](data_context(ds, schema))
    assert isinstance(g, GroupComparisonView) and g.by == "run" and g.items
    ds, schema = case_data("e81b")
    g = DATA_SECTION_FUNCS["groups"](data_context(ds, schema))
    assert isinstance(g, GroupComparisonView) and g.by == "species"
    labels = [str(x.group) for x in g.items[0].groups]
    assert set(labels) <= {str(s) for s in ds["species"].values}
    for case in ("e07", "e31", "e58", "e19", "e23"):
        ds, schema = case_data(case)
        c = data_context(ds, schema)
        assert "groups" in c.shapes.not_applicable, case
    ds, schema = case_data("e31")
    c = data_context(ds, schema, DataExploreRequest(group_by="digit"))
    assert "groups" not in c.shapes.not_applicable


def test_the_overview_of_a_lens_names_its_images() -> None:
    ds, schema = case_data("e80")
    ov = DATA_SECTION_FUNCS["overview"](data_context(ds, schema))
    assert "2 images of 24 x 24 pixels and 1 of 7 x 7 pixels" in ov.summary_plain  # type: ignore[attr-defined]


# ------------------------------------------------------------------ section contents


def test_images_of_a_movie_a_vector_field_and_digits() -> None:
    ds, schema = case_data("e07")
    v = DATA_SECTION_FUNCS["images"](data_context(ds, schema))
    assert isinstance(v, ImagesView)
    (e,) = v.fields
    assert e.var == "quakes" and e.frame_dim == "quarter" and e.n_draws == 0
    assert len(e.frames) == min(ds.sizes["quarter"], MAX_FIELD_FRAMES)
    assert e.frame_stats is not None and len(e.frame_stats.coords) == ds.sizes["quarter"]
    assert e.frame_stats.mean[0] == pytest.approx(float(ds["quakes"][0].mean()))
    assert e.origin == "upper"
    north = ds.assign_coords(y_km=("row", np.arange(ds.sizes["row"]) * 25.0))
    (e,) = DATA_SECTION_FUNCS["images"](data_context(north, schema)).fields  # type: ignore[attr-defined]
    assert e.origin == "lower"  # y_km grows with the row: north is up
    ds, schema = case_data("e60")
    v = DATA_SECTION_FUNCS["images"](data_context(ds, schema))
    (e,) = v.fields  # type: ignore[attr-defined]
    assert e.kind == "vector" and e.frames[0].arrows is not None
    assert e.mask_var == "force_mask" and e.mask is not None and len(e.components) == 2
    ds, schema = case_data("e31")
    ds = ds.assign_coords(label=("digit", np.arange(ds.sizes["digit"]) % 10))
    v = DATA_SECTION_FUNCS["images"](data_context(ds, schema))
    (e,) = v.fields  # type: ignore[attr-defined]
    assert e.group_by == "label" and 2 <= len(e.groups) <= 12
    assert sum(g.n for g in e.groups) <= ds.sizes["digit"]


def test_images_respect_the_caps() -> None:
    rng = np.random.default_rng(0)
    big = xr.Dataset(
        {f"v{k}": (("frame", "row", "col"), rng.random((300, 48, 48))) for k in range(9)}
        | {"wide": (("row2", "col2"), rng.random((400, 401)))},
        coords={"frame": np.arange(300) * 0.1},
    )
    c = data_context(big, None)
    v = DATA_SECTION_FUNCS["images"](c)
    assert isinstance(v, ImagesView)
    assert len(v.fields) == MAX_FIELDS and v.omitted
    assert sum(f.cells() for f in v.fields) <= MAX_FIELD_CELLS
    for f in v.fields:
        assert len(f.frames) <= MAX_FIELD_FRAMES and f.height <= MAX_FIELD_PIXELS
        if f.frame_stats is not None:
            assert len(f.frame_stats.coords) == 300
    wide = data_context(big[["wide"]], None)
    (e,) = DATA_SECTION_FUNCS["images"](wide).fields  # type: ignore[attr-defined]
    assert e.downsampled and e.width <= MAX_FIELD_PIXELS and e.source_width == 401


def test_profiles_thin_frames_and_find_peaks() -> None:
    ds, schema = case_data("e50")
    v = DATA_SECTION_FUNCS["profiles"](data_context(ds, schema))
    assert isinstance(v, ProfilesView)
    (item,) = v.items
    assert item.along == "position" and item.frame_dim == "hours"
    assert len(item.frames) == ds.sizes["hours"] and len(item.peak) == ds.sizes["hours"]
    row = ds["cells"].isel(hours=0).values
    assert item.peak[0].value == pytest.approx(float(row.max()))
    many = xr.Dataset(
        {"s": (("trial", "frequency"), np.random.default_rng(1).random((100, 50)) + 1)},
        coords={"frequency": np.linspace(1, 100, 50), "trial": np.arange(100)},
    )
    (it,) = DATA_SECTION_FUNCS["profiles"](data_context(many, None)).items  # type: ignore[attr-defined]
    assert len(it.frames) == MAX_PROFILE_FRAMES and it.omitted_frames == 40 and it.log_x


def test_a_spectrum_has_no_unusual_values() -> None:
    """The points of a curve (E79 the power spectrum along the multipole) are not samples: the
    peak is not an unusual value, in the section and in the quality check. The image keeps
    its check."""
    ds, schema = case_data("e79")
    c = data_context(ds, schema)
    assert "reference_dl" in c.curve_vars and "temperature" not in c.curve_vars
    out = DATA_SECTION_FUNCS["outliers"](c)
    assert "reference_dl" not in {i.var for i in out.items}  # type: ignore[attr-defined]
    assert "temperature" in {i.var for i in out.items}  # type: ignore[attr-defined]
    quality = DATA_SECTION_FUNCS["quality"](c)
    assert not [w for w in quality.items if w.code == "OUTLIERS" and w.var == "reference_dl"]  # type: ignore[attr-defined]


def test_the_overview_counts_one_variable_in_the_singular() -> None:
    """E80 node /psf holds one image: "1 variable over 31 psf_row x 31 psf_col"."""
    psf = xr.Dataset({"kernel": (("psf_row", "psf_col"), np.ones((31, 31)))})
    v = DATA_SECTION_FUNCS["overview"](data_context(psf, None))
    assert v.summary_plain.startswith("1 variable over 31 psf_row x 31 psf_col.")  # type: ignore[attr-defined]


def test_the_map_has_rings_layers_neighbours_and_moran() -> None:
    ds, schema = case_data("e18")
    v = DATA_SECTION_FUNCS["map"](data_context(ds, schema))
    assert isinstance(v, MapView)
    assert len(v.areas) == ds.sizes["district"] and v.rings
    assert [layer.var for layer in v.layers] == ["cases", "expected", "aff", "cases / expected"]
    adj = ds["adjacency"].values
    assert len(v.edges) == int(np.triu(adj, 1).sum())
    assert {m.weights for m in v.moran} == {"adjacency"} and len(v.moran) == 4


def test_the_map_decimates_many_vertices() -> None:
    n, m = 100, 300
    t = np.linspace(0, 2 * np.pi, m)
    rx = np.stack([np.cos(t) + i * 3 for i in range(n)])
    ry = np.stack([np.sin(t) for _ in range(n)])
    ds = xr.Dataset(
        {
            "ring_x": (("area", "vertex"), rx),
            "ring_y": (("area", "vertex"), ry),
            "value": ("area", np.arange(n, dtype=float)),
        }
    )
    v = DATA_SECTION_FUNCS["map"](data_context(ds, None))
    assert isinstance(v, MapView) and v.decimated
    assert sum(len(r.x) for r in v.rings) <= MAX_MAP_VERTICES
    assert v.moran[0].weights == "knn4" and v.moran[0].morans_i is not None
    assert v.moran[0].morans_i > 0.5  # values grow along the row of circles


def test_networks_and_their_statistics() -> None:
    ds, schema = case_data("e21")
    v = DATA_SECTION_FUNCS["network"](data_context(ds, schema))
    assert isinstance(v, NetworkView)
    cowork = v.items[0]
    assert cowork.var == "cowork" and cowork.binary and cowork.matrix is not None
    assert cowork.node_attrs and "office" in cowork.node_attrs
    homophily = [x for x in v.items if x.var == "homophily"]
    assert len(homophily) == ds.sizes["covariate"] and all(x.facet for x in homophily)
    rng = np.random.default_rng(2)
    a = (rng.random((400, 400)) < 0.1).astype(float)
    a = np.triu(a, 1)
    a = a + a.T
    big = xr.Dataset({"tie": (("node", "node_"), a)})
    (e,) = DATA_SECTION_FUNCS["network"](data_context(big, None)).items  # type: ignore[attr-defined]
    assert len(e.labels) == MAX_NET_NODES and e.omitted_nodes == 100 and e.matrix is None
    assert len(e.edges) <= MAX_NET_EDGES and e.omitted_edges > 0
    assert e.density == pytest.approx(np.triu(a, 1).sum() / (400 * 399 / 2))


def test_transitivity_density_and_homophily_by_hand() -> None:
    a = np.zeros((4, 4))
    for i, j in ((0, 1), (1, 2), (0, 2), (2, 3)):
        a[i, j] = a[j, i] = 1
    ds = xr.Dataset(
        {"link": (("p", "p_"), a), "team": ("p", np.array([1, 1, 2, 2]))},
        coords={"p": list("abcd"), "p_": list("abcd")},
    )
    (e,) = DATA_SECTION_FUNCS["network"](data_context(ds, None)).items  # type: ignore[attr-defined]
    assert e.transitivity == pytest.approx(0.6)  # 3 x 1 triangle / 10 connected triples
    assert e.density == pytest.approx(4 / 6) and e.degree == [2, 2, 3, 1]
    assert e.components.count == 1 and not e.directed
    (h,) = e.homophily
    assert h.within_share == pytest.approx(0.5) and h.expected_share == pytest.approx(4 / 12)
    # one bar for each degree, centred on it: 1, 2 (two nodes) and 3
    assert e.degree_hist.edges == [0.5, 1.5, 2.5, 3.5] and e.degree_hist.counts == [1, 2, 1]


def test_the_tree_layout_traits_and_signal() -> None:
    ds, schema = case_data("e23")
    v = DATA_SECTION_FUNCS["tree"](data_context(ds, schema))
    assert isinstance(v, TreeView)
    assert v.n_nodes == ds.sizes["node"] and v.n_tips == int(ds["is_tip"].values.sum())
    assert len(v.roots) == 1 and v.matched_by == "label" and v.unmatched == 0
    assert [t.var for t in v.traits] == ["brain", "body", "group_size"]
    assert sorted(v.layout.y[t] for t in v.tips) == list(range(len(v.tips)))
    assert len(v.signal) == 3
    # tip order: the traits follow the drawing
    labels = [v.layout.label[t] for t in v.tips]
    brain = dict(zip([str(s) for s in ds["species"].values], ds["brain"].values, strict=True))
    for label, value in zip(labels, v.traits[0].values, strict=True):
        want = float(brain[str(label)])
        assert value is None if math.isnan(want) else value == pytest.approx(want)


def test_moran_by_hand() -> None:
    w = np.zeros((4, 4))
    for i in range(3):
        w[i, i + 1] = w[i + 1, i] = 1
    i_, e_, z_, p_ = _moran(np.array([1.0, 2.0, 3.0, 4.0]), w)
    assert i_ == pytest.approx(1 / 3) and e_ == pytest.approx(-1 / 3)
    assert z_ is not None and p_ is not None and 0 < p_ <= 1


def test_circular_statistics_by_hand() -> None:
    st = _circ_stats(np.radians([350.0, 10.0, 350.0, 10.0]), 360.0)
    assert st["mean_direction"] == pytest.approx(0.0, abs=1e-9) or st[
        "mean_direction"
    ] == pytest.approx(360.0)
    assert st["resultant_length"] == pytest.approx(math.cos(math.radians(10)))
    st = _circ_stats(np.radians([0.0, 90.0]), 360.0)
    assert st["mean_direction"] == pytest.approx(45.0)
    assert st["resultant_length"] == pytest.approx(math.sqrt(2) / 2)
    assert st["rayleigh_z"] == pytest.approx(2 * 0.5)
    uniform = _circ_stats(np.linspace(0, 2 * np.pi, 400, endpoint=False), 24.0)
    assert uniform["resultant_length"] == pytest.approx(0.0, abs=1e-9)
    assert uniform["rayleigh_p"] == pytest.approx(1.0, abs=1e-6)
    a = np.random.default_rng(3).uniform(0, 2 * np.pi, 200)
    assert _js_rho(a, a) == pytest.approx(1.0)


def test_circular_view_groups_bins_and_pairs() -> None:
    ds, schema = case_data("e81b")
    v = DATA_SECTION_FUNCS["circular"](data_context(ds, schema))
    assert isinstance(v, CircularView)
    (item,) = v.items
    assert item.units == "h" and item.period == 24.0 and len(item.bins) == 25
    assert item.group_by == "species" and item.groups[0].label == "all"
    assert {g.label for g in item.groups[1:]} <= {str(s) for s in ds["species"].values}
    assert sum(g.n for g in item.groups[1:]) == item.groups[0].n
    ds, schema = case_data("e81")
    v = DATA_SECTION_FUNCS["circular"](data_context(ds, schema))
    assert [i.var for i in v.items] == ["wind_from", "hour_lst", "day_of_year"]  # type: ignore[attr-defined]
    assert len(v.pairs) == 3 and v.items[0].zero == "north" and v.items[0].clockwise  # type: ignore[attr-defined]
    many = xr.Dataset(
        {
            "bearing": ("obs", np.random.default_rng(4).uniform(0, 360, 2000)),
            "grp": ("obs", np.arange(2000) % 20),
        }
    )
    many["bearing"].attrs["units"] = "deg"
    many["grp"].attrs = {}
    c = data_context(many, None)
    shape = c.shapes.first("circular")
    assert shape is not None


def test_points_lab_colours_answers_and_caps() -> None:
    ds, schema = case_data("e77")
    v = DATA_SECTION_FUNCS["points"](data_context(ds, schema))
    assert isinstance(v, PointsView)
    assert v.lattice and v.x == "wcs_col" and v.y == "wcs_row" and v.n == ds.sizes["chip"]
    assert v.srgb is not None and len(v.srgb) == v.n
    assert v.answers is not None and v.answers.by_dim == "speaker"
    assert all(a is None or 0 < a <= 1 for a in v.answers.agreement)
    assert v.clark_evans is None
    rng = np.random.default_rng(5)
    cloud = xr.Dataset(
        {
            "x": ("p", rng.random(8000), {"units": "km"}),
            "y": ("p", rng.random(8000), {"units": "km"}),
        }
    )
    p = DATA_SECTION_FUNCS["points"](data_context(cloud, None))
    assert isinstance(p, PointsView)
    assert len(p.sample.index) == MAX_POINTS and p.density is not None
    assert p.clark_evans is not None and p.clark_evans.r == pytest.approx(1.0, abs=0.05)


def test_lab_to_srgb() -> None:
    assert _lab_to_hex(np.array([[100.0, 0.0, 0.0], [0.0, 0.0, 0.0]])) == ["#ffffff", "#000000"]
    (red,) = _lab_to_hex(np.array([[53.24, 80.09, 67.20]]))
    assert red == "#ff0000"


def test_structure_quality_codes() -> None:
    ds, schema = case_data("e23")
    p = ds["parent"].values.copy()
    inner = int(np.nonzero(~ds["is_tip"].values & (p >= 0))[0][0])
    p[inner] = -1  # a second root
    bl = ds["branch_length"].values.copy()
    bl[3] = -1.0
    bad = ds.assign(parent=("node", p), branch_length=("node", bl))
    q = DATA_SECTION_FUNCS["quality"](data_context(bad, schema))
    tree_msgs = [w.message_plain for w in q.items if w.code == "TREE_INVALID"]  # type: ignore[attr-defined]
    assert any("2 roots" in m for m in tree_msgs) and any("negative" in m for m in tree_msgs)
    ds, schema = case_data("e21")
    m = ds["cowork"].values.copy()
    m[0, 1], m[1, 0] = 1, 0
    q = DATA_SECTION_FUNCS["quality"](
        data_context(ds.assign(cowork=(ds["cowork"].dims, m)), schema)
    )
    assert "NETWORK_ASYMMETRIC" in {w.code for w in q.items}  # type: ignore[attr-defined]
    ds, schema = case_data("e81")
    w = ds["wind_from"].values.copy()
    w[:3] = -20
    q = DATA_SECTION_FUNCS["quality"](data_context(ds.assign(wind_from=("report", w)), schema))
    assert "ANGLE_RANGE" in {x.code for x in q.items}  # type: ignore[attr-defined]


def test_an_explore_run_lists_reasons_and_order(tmp_path: Path) -> None:
    ds, schema = case_data("e60")
    ctx = FakeCtx(tmp_path)
    c = data_context(ds, schema)
    idx = write_sections(ctx, DATA_SECTION_FUNCS, list(DATA_SECTIONS), c, target="dataset", ref="x")  # type: ignore[arg-type]
    by = {s.section: s for s in idx.sections}
    assert by["images"].status == "ok" and by["stl"].reason == "not_applicable"
    assert by["map"].message_plain and "polygons" in by["map"].message_plain
    assert idx.shapes == ["vector_field"] and idx.order and idx.order[2] == "images"


def test_a_tree_dataset_node_is_explored_on_its_own(tmp_path: Path) -> None:
    from mapi_plugin_science import lens_data

    tree = lens_data.to_tree(science.EXPORTS["e80"].synthetic())
    for path, node_schema in lens_data.LENS_TREE_SCHEMA.nodes.items():
        if not node_schema.vars:
            continue
        ctx = FakeCtx(tmp_path)
        prefix = f"ui/explore/nodes/{path.strip('/')}/"
        idx = write_data_explorer(
            ctx,
            tree[path].to_dataset(),
            node_schema,
            ref="d",
            prefix=prefix,
            node=path,  # type: ignore[arg-type]
        )
        assert idx is not None and idx.node == path
        assert {s.section: s.status for s in idx.sections}["images"] == "ok"
        assert ctx.written[f"{prefix}images.json"]["node"] == path


# --------------------------------------------------------------------- real data


REAL = os.environ.get("MAPI_PYMC_CHALLENGES_DATA")


def _real(case: str) -> tuple[xr.Dataset, DataSchema]:
    if not REAL:
        pytest.skip("set MAPI_PYMC_CHALLENGES_DATA to the pymc-challenges data/ directory")
    c = science.EXPORTS[case]
    if c.missing(Path(REAL).expanduser()):
        pytest.skip(f"{case}: the pymc-challenges copy has no {', '.join(c.files)}")
    ds = c.load(Path(REAL).expanduser())
    assert isinstance(ds, xr.Dataset) and isinstance(c.schema, DataSchema)
    return ds, c.schema


@pytest.mark.parametrize("case", CASES)
def test_the_ingest_of_the_real_data(case: str, tmp_path: Path) -> None:
    ds, schema = _real(case)
    start = time.perf_counter()
    ctx, index = _ingest(tmp_path, ds, schema)
    seconds = time.perf_counter() - start
    status = _check_ingest(ctx, index)
    assert seconds < 60, f"{case}: the ingest views took {seconds:.1f} s"
    picture = [s for s in index.order or [] if s in SCIENCE_SECTIONS and s != "structure"]
    assert picture and all(status[s].status == "ok" for s in picture)
    if case in ("e81", "e81b"):
        assert status["timeseries"].reason == "not_applicable"
    if case == "e07":
        assert len(ctx.written["ui/explore/timeseries.json"]["series"]) == 1
    if case == "e19":
        groups = status.get("groups")
        assert groups is None or groups.status == "skipped"


# ------------------------------------------------------------------ review: big data, warnings


def test_image_statistics_match_numpy_and_use_float32_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The statistics are read a few frames at a time (here 3) and still match numpy on all
    the cells; the sample for the histogram is capped."""
    import mapi_runner.worker.explore_science as sci

    monkeypatch.setattr(sci, "_CHUNK_CELLS", 3 * 20 * 24)
    monkeypatch.setattr(sci, "_SAMPLE_CELLS", 2000)
    rng = np.random.default_rng(4)
    a = rng.gamma(2.0, 3.0, (50, 20, 24))
    a[3, 2, 2] = np.nan
    ds = xr.Dataset({"movie": (("frame", "row", "col"), a)}, coords={"frame": np.arange(50)})
    (e,) = DATA_SECTION_FUNCS["images"](data_context(ds, None)).fields  # type: ignore[attr-defined]
    fin = a[np.isfinite(a)]
    assert e.stats.min == pytest.approx(fin.min(), rel=1e-6)
    assert e.stats.max == pytest.approx(fin.max(), rel=1e-6)
    assert e.stats.mean == pytest.approx(fin.mean(), rel=1e-5)
    assert e.stats.sd == pytest.approx(fin.std(ddof=1), rel=1e-4)
    assert e.stats.missing_share == pytest.approx(1 / a.size)
    assert e.frame_stats is not None and len(e.frame_stats.mean) == 50
    assert e.frame_stats.mean[7] == pytest.approx(a[7].mean(), rel=1e-5)
    assert e.frame_stats.missing_share[3] == pytest.approx(1 / (20 * 24))
    assert e.histogram is not None and sum(e.histogram.counts) <= 2000


def test_the_ingest_leaves_a_large_movie_for_an_explore_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mapi_runner.worker.explore as explore_mod

    monkeypatch.setattr(explore_mod, "INGEST_IMAGE_CELLS", 10_000)
    rng = np.random.default_rng(5)
    ds = xr.Dataset(
        {"movie": (("frame", "row", "col"), rng.random((40, 20, 20)).astype(np.float32))},
        coords={"frame": np.arange(40)},
    )
    ctx = FakeCtx(tmp_path)
    index = write_data_explorer(ctx, ds, None, ref="dataset://sci/movie@v1")  # type: ignore[arg-type]
    assert index is not None
    assert "images" not in {r.section for r in index.sections}  # missing: computed on demand
    assert any(code == "EXPLORE_DEFERRED" for code, *_ in ctx.warnings)
    assert "images" in (index.order or [])


def test_saturation_needs_a_pile_up_at_the_maximum() -> None:
    from mapi_runner.worker.explore_science import saturated_share, structure_warnings

    rng = np.random.default_rng(6)
    # a heater set-point: few levels, many cells at the top level -> not saturated
    plate = np.zeros((3, 12, 24))
    plate[:, 5:7, 10:14] = 10.0
    # a 12-bit detector clipped at 4095
    det = np.minimum(rng.normal(3000, 800, (4, 32, 32)).round(), 4095)
    # a smooth field with no pile-up (the maximum is one cell)
    smooth = rng.normal(0, 1, (4, 32, 32))
    ds = xr.Dataset(
        {
            "heater_w": (("run", "row", "col"), plate),
            "counts": (("shot", "r2", "c2"), det),
            "smooth": (("shot", "r2", "c2"), smooth),
        },
        coords={
            "row": np.arange(12),
            "col": np.arange(24),
            "r2": np.arange(32),
            "c2": np.arange(32),
        },
    )
    flagged = {
        w.var for w in structure_warnings(data_context(ds, None)) if w.code == "IMAGE_SATURATED"
    }
    assert flagged == {"counts"}
    assert saturated_share(plate.ravel().astype(np.float32), 0.0, 10.0) == 0.0
    assert saturated_share(det.ravel(), float(det.min()), 4095.0) > 0.05
    for case in ("e51", "e79"):
        cds, sch = case_data(case)
        q = DATA_SECTION_FUNCS["quality"](data_context(cds, sch))
        assert not [w for w in q.items if w.code == "IMAGE_SATURATED"], case  # type: ignore[attr-defined]


def test_moran_of_a_map_layer_with_frames_uses_their_mean() -> None:
    ds, _schema = case_data("e18")
    rng = np.random.default_rng(7)
    n = ds.sizes["district"]
    base = ds["cases"].values.astype(float)
    # frame 0 is noise, the mean over the frames is the clustered cases
    noise = rng.permutation(base)
    framed = np.stack([noise, base * 2, base * 4 - noise])  # mean: 2 x cases
    ds = ds.assign(cases_by_year=(("year", "district"), framed))
    ds = ds.assign_coords(year=[2001, 2002, 2003])
    v = DATA_SECTION_FUNCS["map"](data_context(ds, None))
    assert isinstance(v, MapView)
    row = next(m for m in v.moran if m.var == "cases_by_year")
    plain = next(m for m in v.moran if m.var == "cases")
    assert row.morans_i == pytest.approx(plain.morans_i, abs=1e-9)
    assert any("mean of each area over the frames" in w for w in v.warnings_plain)
    assert n == len(v.areas)


# ---------------------------------------------------------------- v0.14: colour images, gallery


def _stack(
    n: int, *, rgb: bool = True, float_data: bool = False, label: bool = True, split: bool = True
) -> tuple[xr.Dataset, DataSchema]:
    """An image-folder dataset: ``pixels(image, y, x, channel)`` with a ``label`` variable
    (3 classes of different sizes) and a ``split`` coord."""
    from mapi_sdk.schema import infer_schema

    rng = np.random.default_rng(14)
    c = 3 if rgb else 1
    px = rng.integers(0, 256, (n, 12, 10, c)).astype(np.uint8)
    px[:, :, :, 0] = np.arange(n, dtype=np.uint8)[:, None, None] % 200  # red differs by image
    values: Any = px.astype(np.float64) / 255.0 if float_data else px
    classes = np.array(["cat"] * (n // 2) + ["dog"] * (n // 3) + ["bird"] * (n - n // 2 - n // 3))
    coords: dict[str, Any] = {
        "image": [f"{i:05d}" for i in range(n)],
        "y": np.arange(12),
        "x": np.arange(10),
        "channel": ["r", "g", "b"] if rgb else ["gray"],
        "path": ("image", [f"img/{i}.png" for i in range(n)]),
    }
    if split:
        coords["split"] = ("image", np.where(np.arange(n) % 4 == 0, "test", "train"))
    data: dict[str, Any] = {"pixels": (("image", "y", "x", "channel"), values)}
    if label:
        data["label"] = ("image", classes)
    ds = xr.Dataset(data, coords=coords)
    ds["pixels"].attrs["mapi_image"] = "1"
    return ds, infer_schema(ds, roles={"label": "target"} if label else None)


def test_a_colour_stack_is_an_rgb_image_with_class_means_by_label() -> None:
    from mapi_runner.worker.structure import detect_structure
    from mapi_spec.views import ImageEntry

    ds, schema = _stack(30)
    s = detect_structure(ds, schema)
    shape = s.first("image")
    assert shape is not None and dict(shape.dims) == {
        "y": "y", "x": "x", "frame": "image", "channel": "channel",
    }  # fmt: skip
    # the label variable wins over the split coord for the class means
    assert shape.extra["group_coord"] == "label"
    assert "gallery" not in s.not_applicable
    assert s.recommended.index("images") + 1 == s.recommended.index("gallery")
    v = DATA_SECTION_FUNCS["images"](data_context(ds, schema))
    assert isinstance(v, ImagesView) and len(v.fields) == 1  # the channel is not a facet
    e = v.fields[0]
    assert e.color == "rgb" and e.channel_dim == "channel" and e.rgb_scale == "uint8"
    assert e.group_by == "label" and {g.label for g in e.groups} == {"cat", "dog", "bird"}
    assert all(g.rgb is not None for g in e.groups)
    ImageEntry.model_validate(e.model_dump())
    assert any("brightness" in w for w in v.warnings_plain)
    # the first frame's colours are the data's (8-bit values as they are)
    import base64

    raw = np.frombuffer(base64.b64decode(e.frames[0].rgb.data), dtype=np.uint8)  # type: ignore[union-attr]
    assert np.array_equal(raw.reshape(12, 10, 3), ds["pixels"].values[0])
    # the luminance raster gives the statistics
    luma = (ds["pixels"].values.astype(float) * [0.299, 0.587, 0.114]).sum(-1)
    assert e.stats.mean == pytest.approx(luma.mean(), rel=1e-4)


def test_float_colours_are_stretched_and_gray_stacks_stay_single() -> None:
    ds, schema = _stack(12, float_data=True)
    e = DATA_SECTION_FUNCS["images"](data_context(ds, schema)).fields[0]  # type: ignore[attr-defined]
    assert e.color == "rgb" and e.rgb_scale == "stretch"
    ds, schema = _stack(12, rgb=False)
    e = DATA_SECTION_FUNCS["images"](data_context(ds, schema)).fields[0]  # type: ignore[attr-defined]
    assert e.color == "single" and e.frames[0].rgb is None and e.facet == {}


def test_the_gallery_shows_every_image_of_a_small_stack(tmp_path: Path) -> None:
    from mapi_spec.views import GalleryView

    ds, schema = _stack(40)
    v = DATA_SECTION_FUNCS["gallery"](data_context(ds, schema))
    assert isinstance(v, GalleryView)
    GalleryView.model_validate(v.model_dump())
    assert v.selection == "all" and len(v.items) == v.n_images == 40
    assert (v.color, v.frame_dim, v.channel_dim) == ("rgb", "image", "channel")
    assert (v.source_height, v.source_width, v.thumb_height, v.thumb_width) == (12, 10, 12, 10)
    assert [f.name for f in v.facets] == ["label", "split"]  # path: one image per level
    assert v.facets[0].counts == {"cat": 20, "dog": 13, "bird": 7}
    assert v.facets[1].source == "coord" and v.facets[1].counts == {"test": 10, "train": 30}
    first = v.items[0]
    assert first.label == "00000" and first.meta == {"label": "cat", "split": "test"}
    import base64
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(first.png)))
    assert im.size == (10, 12) and im.mode == "RGB"
    assert np.array_equal(np.asarray(im), ds["pixels"].values[0])
    # the ingest writes it with the other sections
    ctx, index = _ingest(tmp_path, ds, schema)
    status = _check_ingest(ctx, index)
    assert status["gallery"].status == "ok" and status["images"].status == "ok"


def test_a_large_stack_is_sampled_per_label_and_split_with_small_thumbnails() -> None:
    from mapi_runner.worker import explore_science as es
    from mapi_spec.views import MAX_GALLERY_ITEMS, MAX_GALLERY_THUMB

    ds, schema = _stack(900)
    big = ds.isel(y=np.arange(12).repeat(12), x=np.arange(10).repeat(10))  # 144 x 100 pixels
    big = big.assign_coords(y=np.arange(144), x=np.arange(100))
    v = DATA_SECTION_FUNCS["gallery"](data_context(big, schema.model_copy(update={"dims": {}})))
    assert v.selection == "stratified" and len(v.items) == MAX_GALLERY_ITEMS  # type: ignore[attr-defined]
    assert max(v.thumb_height, v.thumb_width) <= MAX_GALLERY_THUMB  # type: ignore[attr-defined]
    assert (v.thumb_height, v.thumb_width) == (48, 34)  # type: ignore[attr-defined]
    # every (label, split) cell is shown; the round-robin gives small classes their share
    cells = {(i.meta["label"], i.meta["split"]) for i in v.items}  # type: ignore[attr-defined]
    assert len(cells) == 6
    per: dict[tuple[str, str], int] = {}
    for i in v.items:  # type: ignore[attr-defined]
        key = (str(i.meta["label"]), str(i.meta["split"]))
        per[key] = per.get(key, 0) + 1
    # 40 a cell; the bird test cell has only 37 images, the 3 left go to the next cells
    assert per[("bird", "test")] == 37 and sum(per.values()) == 240
    assert sorted(per.values()) == [37, 40, 40, 41, 41, 41]
    # deterministic, sorted, distinct
    again = DATA_SECTION_FUNCS["gallery"](data_context(big, schema.model_copy(update={"dims": {}})))
    assert [i.index for i in again.items] == [i.index for i in v.items]  # type: ignore[attr-defined]
    idx = [i.index for i in v.items]  # type: ignore[attr-defined]
    assert idx == sorted(set(idx))
    assert es._stratified_pick({("a",): [0, 1, 2, 3], ("b",): [9]}, 3) == [0, 3, 9]


def test_a_stack_without_facets_is_an_even_sample_and_a_single_image_has_no_gallery() -> None:
    from mapi_spec.views import MAX_GALLERY_ITEMS

    ds, schema = _stack(300, label=False, split=False, float_data=True)
    v = DATA_SECTION_FUNCS["gallery"](data_context(ds, schema))
    assert v.selection == "even" and len(v.items) == MAX_GALLERY_ITEMS and not v.facets  # type: ignore[attr-defined]
    assert v.items[0].index == 0 and v.items[-1].index == 299  # type: ignore[attr-defined]
    assert any("stretched" in w for w in v.warnings_plain)  # type: ignore[attr-defined]
    one = ds.isel(image=0)
    c = data_context(one, None)
    assert c.shapes.not_applicable["gallery"] == "no variable holds a stack of images"
    with pytest.raises(SectionSkipped):
        DATA_SECTION_FUNCS["gallery"](c)


def test_pictures_at_255_are_not_a_saturated_detector() -> None:
    from mapi_runner.worker.explore_science import structure_warnings

    ds, schema = _stack(20, rgb=False)
    ds["pixels"].values[:, :6] = 255  # a white background
    assert not [
        w for w in structure_warnings(data_context(ds, schema)) if w.code == "IMAGE_SATURATED"
    ]
