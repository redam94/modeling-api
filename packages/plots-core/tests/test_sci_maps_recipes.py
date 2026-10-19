"""``sci.area_map``, ``sci.phylogeny`` and ``sci.rose`` on small synthetic DataTrees."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("plotly")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.packs import sci_maps  # noqa: E402
from mapi_plots.packs.sci import sci_recipes  # noqa: E402
from mapi_plots.packs.sci_maps import area_map, phylogeny, rose  # noqa: E402
from mapi_plots.palette import EMPTY  # noqa: E402
from mapi_plots.recipes import Recipe, RecipeDataError, all_recipes, get_recipe  # noqa: E402
from mapi_plots.sanitize import check_plot, sanitize_plot  # noqa: E402
from mapi_spec.plots import MAX_INLINE_ARROW_B64, PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(3)


# ------------------------------------------------------------------------------- helpers


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


def check(plot: PlotSpec, r: Recipe[Any]) -> None:
    """The contract every sci recipe output satisfies."""
    assert plot.kind == "plotly"
    assert plot.id == r.id
    assert plot.provenance.recipe == r.ref
    assert len(plot.caption_plain or "") > 40
    assert plot.datasets and len(plot.datasets) <= 64
    for ds in plot.datasets.values():
        assert ds.kind == "inline_arrow"
        assert len(ds.data) < MAX_INLINE_ARROW_B64
    assert sum(ds.rows or 0 for ds in plot.datasets.values()) <= 250_000
    text = json.dumps(plot.spec)
    assert '"url"' not in text and '"template"' not in text and '"geo"' not in text
    assert check_plot(plot) == []
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []
    for name, col in refs(plot.spec):
        assert col in arrow_to_frame(plot.datasets[name]).columns, f"{name}.{col}"


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def traces(plot: PlotSpec, kind: str) -> list[dict[str, Any]]:
    return [t for t in plot.spec["data"] if t["type"] == kind]


def squares(nx: int, ny: int, n_vertex: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """A grid of unit squares, one ring of ``n_vertex`` points each (closing point included)."""
    t = np.linspace(0, 4, n_vertex)
    sx = np.interp(t, [0, 1, 2, 3, 4], [0, 1, 1, 0, 0])
    sy = np.interp(t, [0, 1, 2, 3, 4], [0, 0, 1, 1, 0])
    rx = np.array([sx + a % nx for a in range(nx * ny)])
    ry = np.array([sy + a // nx for a in range(nx * ny)])
    return rx, ry


def area_tree(
    nx: int = 4,
    ny: int = 3,
    *,
    n_vertex: int = 5,
    shuffle: bool = False,
    signed: bool = False,
    summary: bool = False,
) -> xr.DataTree:
    n = nx * ny
    names = [f"a{i}" for i in range(n)]
    rx, ry = squares(nx, ny, n_vertex)
    mu = np.linspace(-1, 1, n) if signed else np.exp(np.linspace(-0.5, 0.5, n))
    noise = RNG.normal(size=(2, 50, n)) * 0.3
    draws = mu + noise if signed else mu * np.exp(noise)
    order = RNG.permutation(n) if shuffle else np.arange(n)
    post_names = [names[i] for i in order]
    if summary:
        stat = np.stack([mu[order], np.full(n, 0.3)])
        group = xr.Dataset(
            {"risk": (("stat", "area"), stat)}, {"stat": ["mean", "sd"], "area": post_names}
        )
        key = "/predictions"
    else:
        group = xr.Dataset(
            {"risk": (("chain", "draw", "area"), draws[..., order])}, {"area": post_names}
        )
        key = "/posterior"
    return xr.DataTree.from_dict(
        {
            key: group,
            "/constant_data": xr.Dataset(
                {"ring_x": (("area", "vertex"), rx), "ring_y": (("area", "vertex"), ry)},
                {"area": names},
            ),
        }
    )


# ------------------------------------------------------------------------------- catalog


def test_the_pack_lists_the_map_recipes() -> None:
    ids = [r.id for r in sci_recipes()]
    assert ids[-3:] == ["sci.area_map", "sci.phylogeny", "sci.rose"] or {
        "sci.area_map",
        "sci.phylogeny",
        "sci.rose",
    } <= set(ids)
    assert {"sci.area_map", "sci.phylogeny", "sci.rose"} <= {r.id for r in all_recipes()}
    assert get_recipe("sci.rose") is rose
    assert area_map.tags == ("sci", "map")
    assert phylogeny.tags == ("sci", "graph")
    assert rose.tags == ("sci", "polar")
    assert set(sci_maps.__all__) >= {"area_map", "phylogeny", "rose", "sci_map_recipes"}


@pytest.mark.parametrize("recipe_id", ["sci.area_map", "sci.phylogeny", "sci.rose"])
def test_examples_render_safely(recipe_id: str) -> None:
    data, options = ex.example_inputs()[recipe_id]
    r = get_recipe(recipe_id)
    check(r.render(data, **options), r)


# ------------------------------------------------------------------------------ area map


def test_area_map_example_panels_edges_and_marks() -> None:
    data, options = ex.example_inputs()["sci.area_map"]
    plot = area_map.render(data, **options)
    titles = [a["text"] for a in plot.spec["layout"]["annotations"]]
    assert titles == ["<b>Observed smr</b>", "<b>Mean</b>", "<b>SD</b>", "<b>P(rr > 1)</b>"]
    areas = frame(plot, "areas")
    draws = data["posterior"]["rr"].values.reshape(-1, 30)
    np.testing.assert_allclose(areas["mean"], draws.mean(0), rtol=1e-5)
    np.testing.assert_allclose(areas["exceedance"], (draws > 1).mean(0), rtol=1e-5)
    np.testing.assert_allclose(areas["observed"], data["observed_data"]["smr"].values, rtol=1e-5)
    assert len(frame(plot, "edges")) == 3 * 49  # a 6 x 5 grid has 49 neighbour pairs
    assert len(frame(plot, "marks")) == int(((draws > 1).mean(0) > 0.95).sum())
    assert "49 pairs of neighbours" in (plot.caption_plain or "")
    # every area is drawn once per panel (the last one has two rings)
    polys = [k for k in plot.datasets if k.startswith("p0c")]
    rows = sum(len(frame(plot, k)) for k in polys)
    assert rows == 29 * 6 + (6 + 5)
    layout = plot.spec["layout"]
    assert layout["yaxis2"]["scaleanchor"] == "x2" and layout["yaxis2"]["scaleratio"] == 1
    fills = [t for t in traces(plot, "scatter") if t.get("fill") == "toself"]
    assert fills and all(t["hoverinfo"] == "skip" for t in fills)


def test_area_map_aligns_areas_by_label() -> None:
    tree = area_tree(shuffle=True)
    plot = area_map.render(tree, stats=["mean"])
    check(plot, area_map)
    post = tree["posterior"]["risk"]
    want = post.mean(("chain", "draw")).sel(area=[f"a{i}" for i in range(12)]).values
    np.testing.assert_allclose(frame(plot, "areas")["mean"], want, rtol=1e-5)
    assert frame(plot, "areas")["label"].tolist() == [f"a{i}" for i in range(12)]


def test_area_map_threshold_defaults_and_log_scale() -> None:
    signed = area_map.render(area_tree(signed=True), stats=["exceedance"])
    assert "P(risk > 0)" in signed.spec["layout"]["annotations"][0]["text"]
    positive = area_map.render(area_tree(), stats=["mean", "exceedance"], log_scale=True)
    check(positive, area_map)
    assert "P(risk > 1)" in positive.spec["layout"]["annotations"][1]["text"]
    areas = frame(positive, "areas")
    np.testing.assert_allclose(areas["mean_log"], np.log10(areas["mean"]), rtol=1e-5)
    hover = next(t for t in traces(positive, "scatter") if t["mode"] == "markers")
    assert hover["marker"]["color"] == {"$ref": "areas.mean_log"}
    custom = area_map.render(area_tree(), stats=["exceedance"], threshold=10.0)
    assert frame(custom, "areas")["exceedance"].max() < 0.05


def test_area_map_reads_a_summary() -> None:
    tree = area_tree(summary=True)
    plot = area_map.render(tree, var="risk")
    check(plot, area_map)
    titles = [a["text"] for a in plot.spec["layout"]["annotations"]]
    assert titles == ["<b>Mean</b>", "<b>SD</b>"]
    with pytest.raises(RecipeDataError, match="exceedance"):
        area_map.render(tree, var="risk", stats=["exceedance"])


def test_area_map_empty_areas_are_grey() -> None:
    tree = area_tree()
    tree["posterior"]["risk"][..., 3] = np.nan
    plot = area_map.render(tree, stats=["mean"])
    empty = [t for t in traces(plot, "scatter") if t.get("fillcolor") == EMPTY]
    assert len(empty) == 1
    assert len(frame(plot, empty[0]["x"]["$ref"].split(".")[0])) == 6


def test_area_map_thins_large_polygons() -> None:
    tree = area_tree(20, 10, n_vertex=2_000)  # 200 areas x 2 000 points
    plot = area_map.render(tree, max_vertices=20_000)
    check(plot, area_map)
    rows = sum(len(frame(plot, k)) for k in plot.datasets if k.startswith("p0c"))
    assert rows <= 20_000 + 400
    assert "1 point in" in (plot.caption_plain or "")


def test_area_map_observed_panel_from_the_attribute_of_the_variable() -> None:
    tree = area_tree()
    post = tree["posterior"].to_dataset()
    const = tree["constant_data"].to_dataset()
    raw = np.linspace(0.5, 2.0, post.sizes["area"])
    const["raw_risk"] = xr.DataArray(raw, dims=["area"], coords={"area": const["area"].values})
    post["risk"].attrs[sci_maps.OBSERVED_ATTR] = "raw_risk"
    tagged = xr.DataTree.from_dict({"/posterior": post, "/constant_data": const})
    plot = area_map.render(tagged)
    titles = [a["text"] for a in plot.spec["layout"]["annotations"]]
    assert titles[0] == "<b>Observed raw_risk</b>"
    np.testing.assert_allclose(frame(plot, "areas")["observed"], raw, rtol=1e-5)
    # an attribute that names a missing variable leaves the panel out
    post["risk"].attrs[sci_maps.OBSERVED_ATTR] = "nope"
    plain = area_map.render(xr.DataTree.from_dict({"/posterior": post, "/constant_data": const}))
    assert "<b>Observed nope</b>" not in [a["text"] for a in plain.spec["layout"]["annotations"]]


def test_area_map_errors() -> None:
    tree = area_tree()
    no_geo = xr.DataTree.from_dict({"/posterior": tree["posterior"].to_dataset()})
    with pytest.raises(RecipeDataError, match="ring_x"):
        area_map.render(no_geo)
    with pytest.raises(RecipeDataError, match="adjacency"):
        area_map.render(tree, show_edges=True)
    with pytest.raises(RecipeDataError, match="observed"):
        area_map.render(tree, stats=["observed"])
    with pytest.raises(RecipeDataError, match="no observed variable"):
        area_map.render(tree, observed="nope")
    with pytest.raises(RecipeDataError, match="invalid settings"):
        area_map.render(tree, stats=[])


# ----------------------------------------------------------------------------- phylogeny


def ladder_tree(n_tips: int, labels: bool = True) -> dict[str, Any]:
    """A caterpillar tree: internal node i joins tip i and internal node i + 1."""
    n_int = n_tips - 1
    parent = [-1]
    for i in range(1, n_int):
        parent.append(i - 1)
    tips = list(range(n_int, n_int + n_tips))
    for k in range(n_tips):
        parent.append(min(k, n_int - 1))
    names = [f"t{k}" for k in range(n_tips)]
    ds = xr.Dataset(
        {
            "parent": (("node",), np.array(parent)),
            "branch_length": (("node",), np.full(len(parent), 0.5)),
        }
    )
    if labels:
        ds["tip_label"] = (("node",), [""] * n_int + names)
    return {"constant": ds, "tips": tips, "names": names}


def test_phylogeny_example_layout() -> None:
    data, options = ex.example_inputs()["sci.phylogeny"]
    plot = phylogeny.render(data, **options)
    tips = frame(plot, "tips")
    assert sorted(tips["y"]) == list(range(16))
    assert len(frame(plot, "tree")) == 3 * (30 + 15)  # one per branch + one per join
    draws = data["posterior_predictive"]["body_mass"]
    mean = draws.mean(("chain", "draw")).to_series()
    np.testing.assert_allclose(tips["point"], mean[tips["label"]].values, rtol=1e-5)
    assert (tips["lo"] <= tips["point"]).all() and (tips["point"] <= tips["hi"]).all()
    assert tips["observed"].isna().sum() == 3
    cap = plot.caption_plain or ""
    assert "3 tips have no observed value" in cap and "inside the interval" in cap
    assert plot.spec["layout"]["yaxis"]["autorange"] == "reversed"
    assert plot.spec["layout"]["yaxis"]["ticktext"] == {"$ref": "tips.label"}


def test_phylogeny_matches_tips_by_label_and_by_position() -> None:
    t = ladder_tree(6)
    shuffled = t["names"][::-1]
    vals = RNG.normal(size=(1, 40, 6)) + np.arange(6)
    tree = xr.DataTree.from_dict(
        {
            "/constant_data": t["constant"],
            "/posterior": xr.Dataset(
                {"z": (("chain", "draw", "taxon"), vals)}, {"taxon": shuffled}
            ),
        }
    )
    plot = phylogeny.render(tree)
    check(plot, phylogeny)
    tips = frame(plot, "tips")
    by_label = dict(zip(shuffled, vals.mean((0, 1)), strict=True))
    np.testing.assert_allclose(tips["point"], [by_label[n] for n in tips["label"]], rtol=1e-5)
    # no labels on either side: tips in node order are the levels in order
    nolab = ladder_tree(6, labels=False)
    tree2 = xr.DataTree.from_dict(
        {
            "/constant_data": nolab["constant"],
            "/posterior": xr.Dataset({"z": (("chain", "draw", "tip"), vals)}),
        }
    )
    plot2 = phylogeny.render(tree2, tip_dim="tip")
    tips2 = frame(plot2, "tips")
    node_of_tip = {node: k for k, node in enumerate(nolab["tips"])}
    want = [vals.mean((0, 1))[node_of_tip[int(lab)]] for lab in tips2["label"]]
    np.testing.assert_allclose(tips2["point"], want, rtol=1e-5)


@pytest.mark.parametrize("node_coords", [False, True])
def test_phylogeny_positions_win_over_labels_shared_by_chance(node_coords: bool) -> None:
    # tips are nodes 3..6; a 0-based ``tip`` coord shares only "3" with them
    parent = np.array([-1, 0, 0, 1, 1, 2, 2])
    vals = RNG.normal(size=(2, 30, 4)) * 0.01 + np.arange(4)
    tree_ds = xr.Dataset(
        {"parent": ("node", parent)}, {"node": np.arange(7)} if node_coords else None
    )
    obs = xr.Dataset({"trait": ("tip", np.arange(4.0) + 0.5)}, {"tip": np.arange(4)})
    tree = xr.DataTree.from_dict(
        {
            "/tree": tree_ds,
            "/observed_data": obs,
            "/posterior": xr.Dataset(
                {"trait": (("chain", "draw", "tip"), vals)}, {"tip": np.arange(4)}
            ),
        }
    )
    tips = frame(phylogeny.render(tree), "tips").set_index("label")
    np.testing.assert_allclose(tips.loc[["3", "4", "5", "6"], "point"], np.arange(4), atol=0.05)
    np.testing.assert_allclose(tips.loc[["3", "4", "5", "6"], "observed"], np.arange(4) + 0.5)


def test_phylogeny_prunes_to_max_tips() -> None:
    t = ladder_tree(60)
    tree = xr.DataTree.from_dict(
        {
            "/constant_data": t["constant"],
            "/posterior": xr.Dataset(
                {"z": (("chain", "draw", "tip"), RNG.normal(size=(1, 20, 60)))},
                {"tip": t["names"]},
            ),
        }
    )
    plot = phylogeny.render(tree, max_tips=10)
    check(plot, phylogeny)
    assert len(frame(plot, "tips")) == 10
    assert "10 of the 60 tips" in (plot.caption_plain or "")
    assert phylogeny.render(tree).spec["layout"]["yaxis"].get("showticklabels") is None
    assert (
        phylogeny.render(tree, max_tips=100, labels=False).spec["layout"]["yaxis"]["showticklabels"]
        is False
    )


def test_phylogeny_errors() -> None:
    post = xr.Dataset({"z": (("chain", "draw", "tip"), RNG.normal(size=(1, 5, 3)))})
    with pytest.raises(RecipeDataError, match="parent"):
        phylogeny.render(xr.DataTree.from_dict({"/posterior": post}))
    cyc = xr.Dataset({"parent": (("node",), np.array([1, 2, 0, 0, 1]))})
    with pytest.raises(RecipeDataError, match="cycle"):
        phylogeny.render(xr.DataTree.from_dict({"/posterior": post, "/constant_data": cyc}))
    t = ladder_tree(4)
    with pytest.raises(RecipeDataError, match="one per tip"):
        phylogeny.render(
            xr.DataTree.from_dict({"/posterior": post, "/constant_data": t["constant"]})
        )


# ---------------------------------------------------------------------------------- rose


def test_rose_example_counts_and_interval() -> None:
    data, options = ex.example_inputs()["sci.rose"]
    plot = rose.render(data, **options)
    bars = frame(plot, "bars")
    obs = data["observed_data"]["wind_dir"].values
    assert bars["obs__0"].sum() == len(obs)
    want = np.bincount((obs // 22.5).astype(int), minlength=16)
    np.testing.assert_array_equal(bars["obs__0"].to_numpy(), want)
    whiskers = frame(plot, "whiskers")["r__0"].to_numpy().reshape(16, 3)
    assert (whiskers[:, 0] <= whiskers[:, 1]).all() and np.isnan(whiskers[:, 2]).all()
    assert [t["type"] for t in plot.spec["data"]] == ["barpolar", "scatterpolar", "scatterpolar"]
    polar = plot.spec["layout"]["polar"]
    assert polar["angularaxis"]["direction"] == "clockwise"
    assert polar["angularaxis"]["rotation"] == 90
    assert "of the bars end inside the interval" in (plot.caption_plain or "")


def test_rose_hours_share_and_frames() -> None:
    hours = RNG.uniform(0, 24, size=(4, 300))  # (season, report)
    pred = RNG.uniform(0, 24, size=(1, 30, 4, 300))
    tree = xr.DataTree.from_dict(
        {
            "/observed_data": xr.Dataset(
                {"hour": (("season", "report"), hours)}, {"season": ["DJF", "MAM", "JJA", "SON"]}
            ),
            "/posterior_predictive": xr.Dataset(
                {"hour": (("chain", "draw", "season", "report"), pred)},
                {"season": ["DJF", "MAM", "JJA", "SON"]},
            ),
        }
    )
    plot = rose.render(tree, var="hour", units="hours", frame="season", bins=12)
    check(plot, rose)
    bars = frame(plot, "bars")
    for k in range(4):
        assert bars[f"obs__{k}"].sum() == pytest.approx(1.0)
    assert [f["name"] for f in plot.spec["frames"]] == ["DJF", "MAM", "JJA", "SON"]
    axis = plot.spec["layout"]["polar"]["angularaxis"]
    assert axis["ticktext"][:2] == ["0 h", "3 h"]
    assert plot.spec["layout"]["sliders"][0]["currentvalue"]["prefix"] == "season: "


def test_rose_density_curve_with_observed() -> None:
    angle = np.linspace(0, 2 * np.pi, 72, endpoint=False)
    dens = np.exp(2 * np.cos(angle - 1.0))
    dens /= dens.sum() * (2 * np.pi / 72)
    draws = dens * RNG.lognormal(0, 0.05, size=(2, 25, 72))
    obs = RNG.vonmises(1.0, 2.0, 500) % (2 * np.pi)
    tree = xr.DataTree.from_dict(
        {
            "/posterior": xr.Dataset(
                {"density": (("chain", "draw", "angle"), draws)}, {"angle": angle}
            ),
            "/observed_data": xr.Dataset({"heading": (("event",), obs)}),
        }
    )
    plot = rose.render(tree, observed="heading", bins=36)
    check(plot, rose)
    curve = frame(plot, "curve")
    assert len(curve) == 73 and curve["med__0"].iloc[0] == curve["med__0"].iloc[-1]
    assert (curve["lo__0"] <= curve["hi__0"]).all()
    bars = frame(plot, "bars")
    assert bars["obs__0"].sum() * (2 * np.pi / 36) == pytest.approx(1.0, rel=1e-5)
    axis = plot.spec["layout"]["polar"]["angularaxis"]
    assert axis["direction"] == "counterclockwise" and axis["ticktext"][2] == "π/2"
    assert "density per radian" in (plot.caption_plain or "")


def test_rose_observed_only_and_errors() -> None:
    tree = xr.DataTree.from_dict(
        {"/observed_data": xr.Dataset({"doy": (("event",), RNG.uniform(0, 365, 200))})}
    )
    plot = rose.render(tree, var="doy", units="days", bins=12)
    check(plot, rose)
    assert [t["type"] for t in plot.spec["data"]] == ["barpolar"]
    assert plot.spec["layout"]["polar"]["angularaxis"]["ticktext"][0] == "Jan"
    with pytest.raises(RecipeDataError, match="needs angles"):
        rose.render(xr.DataTree.from_dict({"/posterior": xr.Dataset({"a": (("x",), [1.0])})}))
    with pytest.raises(RecipeDataError, match="no observed variable"):
        rose.render(tree, var="doy", observed="nope")
