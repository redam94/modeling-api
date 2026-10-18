"""The ``net.*`` Plotly pack on small synthetic DataTrees (xarray only, no PyMC)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("plotly")

from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.examples import network_tree  # noqa: E402
from mapi_plots.packs.net import graph, net_recipes, spectral_layout  # noqa: E402
from mapi_plots.recipes import Recipe, RecipeDataError, all_recipes, get_recipe  # noqa: E402
from mapi_plots.sanitize import check_plot, sanitize_plot  # noqa: E402
from mapi_spec.plots import MAX_INLINE_ARROW_B64, PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(3)


def refs(node: Any) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        if set(node) == {"$ref"}:
            name, col = node["$ref"].split(".", 1)
            yield name, col
            return
        for v in node.values():
            yield from refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs(v)


def check_net(plot: PlotSpec, r: Recipe[Any]) -> None:
    assert plot.kind == "plotly"
    assert plot.id == r.id
    assert plot.provenance.recipe == r.ref
    assert len(plot.caption_plain or "") > 40
    for ds in plot.datasets.values():
        assert ds.kind == "inline_arrow"
        assert len(ds.data) < MAX_INLINE_ARROW_B64
    assert sum(ds.rows or 0 for ds in plot.datasets.values()) <= 250_000
    text = json.dumps(plot.spec)
    assert '"url"' not in text and '"template"' not in text
    assert check_plot(plot) == []
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []
    for name, col in refs(plot.spec):
        assert col in arrow_to_frame(plot.datasets[name]).columns, f"{name}.{col}"


def pcor_tree(n: int = 8, *, summary: bool = False) -> xr.DataTree:
    """``posterior.pcor(chain, draw, var, var_)``: a signed, symmetric chain graph."""
    m = np.zeros((n, n))
    for i in range(n - 1):
        m[i, i + 1] = m[i + 1, i] = 0.5 if i % 2 else -0.4
    draws = m[None, None] + RNG.normal(0, 0.05, (2, 30, n, n))
    draws = (draws + np.swapaxes(draws, -1, -2)) / 2
    names = [f"v{i}" for i in range(n)]
    coords = {"var": names, "var_": names}
    if summary:
        stats = np.stack([draws.mean((0, 1)), draws.std((0, 1))])
        ds = xr.Dataset(
            {"pcor": (("stat", "var", "var_"), stats)}, {**coords, "stat": ["mean", "sd"]}
        )
        return xr.DataTree.from_dict({"/summary": ds})
    return xr.DataTree.from_dict(
        {"/posterior": xr.Dataset({"pcor": (("chain", "draw", "var", "var_"), draws)}, coords)}
    )


def frame_of(plot: PlotSpec, name: str) -> Any:
    return arrow_to_frame(plot.datasets[name])


# ------------------------------------------------------------------------------ catalog


def test_pack_is_in_the_catalog() -> None:
    assert [r.id for r in net_recipes()] == ["net.graph"]
    assert {r.id for r in net_recipes()} <= {r.id for r in all_recipes()}
    assert get_recipe("net.graph") is graph
    assert graph.summary and "graph" in graph.tags


def test_entry_point_lists_the_pack() -> None:
    from importlib.metadata import entry_points

    eps = {ep.name: ep for ep in entry_points(group="mapi.plots")}
    if "net" not in eps:
        pytest.skip("mapi-plots is not re-installed with the net entry point")
    assert [r.id for r in eps["net"].load()()] == ["net.graph"]


# --------------------------------------------------------------------------------- graph


def test_example_uses_positions_groups_and_truth() -> None:
    plot = graph.render(network_tree())
    check_net(plot, graph)
    names = set(plot.datasets)
    assert {"edges", "mids", "cells", "ticks", "truth_cells"} <= names
    assert {"nodes_0", "nodes_1", "nodes_2"} <= names  # one trace per office
    cap = plot.caption_plain or ""
    assert "posterior mean of position" in cap and "node_group" in cap and "tie" in cap
    assert "undirected" in cap
    cells = frame_of(plot, "cells")
    assert len(cells) == 18 * 18 and cells["z"].isna().sum() == 18  # the diagonal is empty
    mids = frame_of(plot, "mids")
    assert mids["text"].str.contains("P\\(").all()  # draws give the share above threshold
    assert plot.spec["layout"]["showlegend"] is True


def test_signed_edges_get_two_colours_and_a_spectral_layout() -> None:
    plot = graph.render(pcor_tree())
    check_net(plot, graph)
    edges = frame_of(plot, "edges")
    pos = [c for c in edges.columns if c.startswith("x_positive")]
    neg = [c for c in edges.columns if c.startswith("x_negative")]
    assert pos and neg
    cap = plot.caption_plain or ""
    assert "Blue lines are positive" in cap and "layout" in cap
    assert "|pcor| > 0.05" in cap  # 10% of the largest |value|
    mids = frame_of(plot, "mids")
    assert len(mids) == 7  # the chain, undirected
    hm = next(t for t in plot.spec["data"] if t["type"] == "heatmap")
    assert hm["zmin"] == -hm["zmax"]  # a diverging scale centred on zero


def test_summary_input_and_explicit_threshold() -> None:
    plot = graph.render(pcor_tree(summary=True), threshold=0.45, panels="graph")
    check_net(plot, graph)
    assert "cells" not in plot.datasets
    assert len(frame_of(plot, "mids")) == 3  # only the +0.5 links
    assert "draws" not in (plot.caption_plain or "").split("(")[1].split(")")[0]


def test_directed_graph_has_arrowheads() -> None:
    n = 6
    m = np.zeros((n, n))
    for i in range(n - 1):
        m[i, i + 1] = 0.9
    tree = xr.DataTree.from_dict({"/": xr.Dataset({"edge_prob": (("node", "node_"), m)})})
    plot = graph.render(tree, panels="graph")
    check_net(plot, graph)
    assert "x_truth" not in frame_of(plot, "edges")  # a bare dataset has no observed group
    arrows = frame_of(plot, "arrows")
    assert len(arrows.dropna()) == n - 1
    tri = [t for t in plot.spec["data"] if t.get("marker", {}).get("symbol") == "triangle-up"]
    assert tri and tri[0]["marker"]["angle"] == {"$ref": "arrows.a_edge"}
    assert "Arrows point" in (plot.caption_plain or "")


def test_matrix_only_and_large_graph_limits() -> None:
    plot = graph.render(pcor_tree(30), panels="matrix")
    check_net(plot, graph)
    assert "edges" not in plot.datasets
    assert plot.spec["layout"]["xaxis"]["tickvals"] == {"$ref": "ticks.pos"}  # 30 nodes: named
    big = graph.render(pcor_tree(45), panels="matrix")
    assert big.spec["layout"]["xaxis"]["showticklabels"] is False
    with pytest.raises(RecipeDataError, match="max_nodes"):
        graph.render(pcor_tree(30), max_nodes=10)
    capped = graph.render(pcor_tree(30), max_edges=5, panels="graph")
    assert len(frame_of(capped, "mids")) == 5
    assert "strongest shown" in (capped.caption_plain or "")


def test_layout_is_deterministic_and_bounded() -> None:
    w = RNG.random((12, 12))
    a = spectral_layout(w, seed=1)
    b = spectral_layout(w, seed=1)
    assert np.array_equal(a, b)
    assert a.shape == (12, 2) and np.abs(a).max() <= 1.0 + 1e-9
    assert spectral_layout(np.zeros((1, 1))).shape == (1, 2)


def test_missing_inputs_are_typed_errors() -> None:
    flat = xr.Dataset({"a": (("chain", "draw"), np.ones((1, 3)))})
    tree = xr.DataTree.from_dict({"/posterior": flat})
    with pytest.raises(RecipeDataError, match="node, node_"):
        graph.render(tree)
    with pytest.raises(RecipeDataError, match="no edge variable 'nope'"):
        graph.render(pcor_tree(), var="nope")
    with pytest.raises(RecipeDataError, match="two node dims"):
        graph.render(tree, var="a")
    with pytest.raises(RecipeDataError, match="no node variable"):
        graph.render(pcor_tree(), group="office")
    with pytest.raises(RecipeDataError, match="no position variable"):
        graph.render(pcor_tree(), positions="xy")
    with pytest.raises(RecipeDataError):
        graph.render(pcor_tree(), panels="nope")
