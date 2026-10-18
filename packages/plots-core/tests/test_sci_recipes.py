"""The ``sci.*`` Plotly pack on small synthetic DataTrees (xarray only, no PyMC)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("plotly")

from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.packs import sci  # noqa: E402
from mapi_plots.packs.sci import (  # noqa: E402
    ROW_BUDGET,
    block_factor,
    block_mean,
    ensemble,
    field_map,
    profile_bands,
    sci_recipes,
    vector_field,
)
from mapi_plots.recipes import (  # noqa: E402
    Figure,
    Recipe,
    RecipeDataError,
    RecipeSettings,
    all_recipes,
    get_recipe,
    recipe,
)
from mapi_plots.sanitize import check_plot, sanitize_plot  # noqa: E402
from mapi_spec.plots import MAX_INLINE_ARROW_B64, PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(7)


# ------------------------------------------------------------------------------ fixtures


def field_tree(
    ny: int = 12,
    nx: int = 15,
    frames: int | None = None,
    draws: int = 8,
    *,
    grid: dict[str, Any] | None = None,
    manifest: bool = False,
    mask: bool = True,
) -> xr.DataTree:
    """``posterior.intensity(chain, draw, [period], row, col)`` with a masked corner."""
    shape = (2, draws, *((frames,) if frames else ()), ny, nx)
    a = RNG.gamma(2.0, 1.0, size=shape)
    if mask:
        a[..., :2, :3] = np.nan
    dims = ["chain", "draw", *(["period"] if frames else []), "row", "col"]
    coords: dict[str, Any] = {"col": np.linspace(0.0, 1.0, nx)}
    if frames:
        coords["period"] = [2000 + i for i in range(frames)]
    decl = grid or {"y": "row", "x": "col", **({"frame": "period"} if frames else {})}
    da = xr.DataArray(a, dims=dims, coords=coords, name="intensity")
    if manifest:
        outputs = {"intensity": {"dims": dims[2:], "grid": decl}}
        return xr.DataTree.from_dict(
            {
                "/": xr.Dataset(attrs={"bundle_id": "b-sci"}),
                "/model": xr.Dataset(attrs={"manifest": json.dumps({"outputs": outputs})}),
                "/model/posterior": da.to_dataset(),
            }
        )
    da.attrs["grid"] = json.dumps(decl)
    return xr.DataTree.from_dict({"/posterior": da.to_dataset()})


def vector_tree(frames: int | None = None) -> xr.DataTree:
    shape = (2, 6, *((frames,) if frames else ()), 2, 10, 14)
    a = RNG.normal(size=shape)
    a[:, :, ..., 0, :, :] += 3.0  # mostly pointing along +x
    dims = ["chain", "draw", *(["t"] if frames else []), "comp", "y", "x"]
    da = xr.DataArray(
        a,
        dims=dims,
        coords={"x": np.arange(14) * 2.0, "y": np.arange(10) * 2.0},
        name="traction",
        attrs={"grid": json.dumps({"y": "y", "x": "x", "component": "comp", "origin": "lower"})},
    )
    return xr.DataTree.from_dict({"/posterior": da.to_dataset()})


def profile_tree(n_t: int = 5, n_x: int = 30) -> xr.DataTree:
    t = np.arange(n_t)
    xg = np.linspace(0.0, 1.0, n_x)
    mean = np.sin(xg * 3)[None, :] * (1 + t[:, None])
    d = RNG.normal(size=(2, 25, n_t, n_x)) * 0.5 + mean
    obs = mean + RNG.normal(size=(n_t, n_x)) * 0.5
    obs[0, :5] = np.nan
    coords = {"t": t, "x": xg}
    return xr.DataTree.from_dict(
        {
            "/posterior_predictive": xr.Dataset({"u": (("chain", "draw", "t", "x"), d)}, coords),
            "/observed_data": xr.Dataset({"u": (("t", "x"), obs)}, coords),
        }
    )


def ensemble_tree(n_t: int = 20, temporal: bool = False) -> xr.DataTree:
    t: Any = pd.date_range("2024-01-01", periods=n_t, freq="D") if temporal else np.arange(n_t)
    walk = np.cumsum(RNG.normal(size=(1, 60, n_t)), axis=-1)
    obs = np.cumsum(RNG.normal(size=n_t))
    return xr.DataTree.from_dict(
        {
            "/predictions": xr.Dataset(
                {"y": (("chain", "draw", "time"), walk)}, coords={"time": t}
            ),
            "/observed_data": xr.Dataset({"y": (("time",), obs)}, coords={"time": t}),
        }
    )


# ------------------------------------------------------------------------------- helpers


def check_sci(plot: PlotSpec, r: Recipe[Any]) -> None:
    """The contract every sci recipe output satisfies."""
    assert plot.kind == "plotly"
    assert plot.id == r.id
    assert plot.provenance.recipe == r.ref
    assert plot.persona == r.persona
    assert len(plot.caption_plain or "") > 40
    assert plot.datasets
    for ds in plot.datasets.values():
        assert ds.kind == "inline_arrow"
        assert len(ds.data) < MAX_INLINE_ARROW_B64
    assert sum(ds.rows or 0 for ds in plot.datasets.values()) <= 250_000  # sandbox row limit
    text = json.dumps(plot.spec)
    assert '"url"' not in text and '"template"' not in text
    assert check_plot(plot) == []  # no errors and no sanitizer changes
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []
    assert PlotSpec.model_validate(plot.model_dump(mode="json")) == plot
    for name, col in refs(plot.spec):
        assert col in arrow_to_frame(plot.datasets[name]).columns, f"{name}.{col}"


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


def cells(plot: PlotSpec) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets["cells"])


def heatmaps(plot: PlotSpec) -> list[dict[str, Any]]:
    return [t for t in plot.spec["data"] if t["type"] == "heatmap"]


# ------------------------------------------------------------------------------ catalog


def test_pack_is_in_the_catalog() -> None:
    ids = [r.id for r in sci_recipes()]
    assert ids[:4] == ["sci.field_map", "sci.vector_field", "sci.profile_bands", "sci.ensemble"]
    assert "sci.point_pattern" in ids
    assert set(ids) <= {r.id for r in all_recipes()}
    assert get_recipe("sci.field_map") is field_map
    assert all(r.summary and r.tags[0] == "sci" for r in sci_recipes())


def test_entry_point_lists_the_pack() -> None:
    from importlib.metadata import entry_points

    eps = {ep.name: ep for ep in entry_points(group="mapi.plots")}
    if "sci" not in eps:
        pytest.skip("mapi-plots is not re-installed with the sci entry point")
    assert [r.id for r in eps["sci"].load()()] == [r.id for r in sci_recipes()]


# ---------------------------------------------------------------------------- field map


def test_field_map_default_panels() -> None:
    plot = field_map.render(field_tree())
    check_sci(plot, field_map)
    hm = heatmaps(plot)
    assert [t["z"]["$ref"] for t in hm] == ["cells.mean__0", "cells.sd__0"]
    assert [t["xaxis"] for t in hm] == ["x", "x2"]
    # every panel's colour scale is explicit stops, never a plotly.js name
    assert all(isinstance(t["colorscale"], list) for t in hm)
    assert plot.spec["layout"]["yaxis"]["autorange"] == "reversed"  # origin upper
    assert "frames" not in plot.spec and "sliders" not in plot.spec["layout"]
    cap = plot.caption_plain or ""
    assert "The sd panel shows the uncertainty" in cap and "do not fix the value" in cap
    assert "Empty cells have no value" in cap


def test_field_map_statistics_are_right() -> None:
    tree = field_tree(mask=False)
    plot = field_map.render(tree, stats=["mean", "sd", "z", "q05", "q95", "median"])
    check_sci(plot, field_map)
    da = tree["posterior"]["intensity"].stack(s=("chain", "draw")).transpose("s", "row", "col")
    v = da.values
    c = cells(plot)
    np.testing.assert_allclose(c["mean__0"], v.mean(0).ravel(), rtol=1e-5)
    np.testing.assert_allclose(c["sd__0"], v.std(0, ddof=1).ravel(), rtol=1e-5)
    np.testing.assert_allclose(c["q95__0"], np.quantile(v, 0.95, 0).ravel(), rtol=1e-5)
    np.testing.assert_allclose(c["z__0"], (v.mean(0) / v.std(0, ddof=1)).ravel(), rtol=1e-4)
    # x is the coordinate, y the row index (no coord)
    np.testing.assert_allclose(sorted(set(c["x"])), np.linspace(0, 1, 15), atol=1e-6)
    assert sorted(set(c["y"])) == list(range(12))
    z = next(t for t in heatmaps(plot) if t["z"]["$ref"] == "cells.z__0")
    assert z["zmin"] == -z["zmax"]  # diverging around zero


def test_field_map_masked_pixels_stay_empty() -> None:
    plot = field_map.render(field_tree())
    c = cells(plot)
    hole = (c["y"] < 2) & (c["x"] < 0.15)
    assert hole.sum() == 6
    assert c.loc[hole, "mean__0"].isna().all() and c.loc[~hole, "mean__0"].notna().all()


def test_field_map_draw_panels_and_log() -> None:
    tree = field_tree(mask=False)
    plot = field_map.render(tree, stats=["mean", "draws"], n_draws=2, log=True)
    check_sci(plot, field_map)
    keys = [t["z"]["$ref"] for t in heatmaps(plot)]
    assert keys == ["cells.mean__0", "cells.draw0__0", "cells.draw1__0"]
    v = tree["posterior"]["intensity"].stack(s=("chain", "draw")).transpose("s", ...).values
    np.testing.assert_allclose(cells(plot)["mean__0"], np.log10(v).mean(0).ravel(), rtol=1e-5)
    cap = plot.caption_plain or ""
    assert "log10" in cap and "2 draw panels" in cap
    # value panels share one colour range so they compare
    ranges = {(t["zmin"], t["zmax"]) for t in heatmaps(plot)}
    assert len(ranges) == 1


def test_field_map_frames_and_slider() -> None:
    plot = field_map.render(field_tree(frames=4, manifest=True), stats=["mean", "sd"])
    check_sci(plot, field_map)
    assert plot.provenance.bundle_id == "b-sci"
    frames = plot.spec["frames"]
    assert [f["name"] for f in frames] == ["2000", "2001", "2002", "2003"]
    assert frames[2]["traces"] == [0, 1]
    assert [t["z"]["$ref"] for t in frames[2]["data"]] == ["cells.mean__2", "cells.sd__2"]
    assert all(t["type"] == "heatmap" for f in frames for t in f["data"])
    slider = plot.spec["layout"]["sliders"][0]
    assert [s["label"] for s in slider["steps"]] == ["2000", "2001", "2002", "2003"]
    assert slider["steps"][1]["method"] == "animate" and slider["steps"][1]["args"][0] == ["2001"]
    buttons = plot.spec["layout"]["updatemenus"][0]["buttons"]
    assert [b["label"] for b in buttons] == ["Play", "Pause"]
    assert "slider" in (plot.caption_plain or "")
    # one colour range over all frames
    assert len(cells(plot).columns) == 2 + 2 * 4


def test_field_map_max_frames() -> None:
    plot = field_map.render(field_tree(frames=10), max_frames=4)
    assert [f["name"] for f in plot.spec["frames"]] == ["2000", "2003", "2006", "2009"]
    assert "4 of the 10 levels" in (plot.caption_plain or "")


def test_field_map_downsamples_by_block_mean() -> None:
    tree = field_tree(ny=40, nx=50, mask=False)
    plot = field_map.render(tree, max_cells=400)
    check_sci(plot, field_map)
    c = cells(plot)
    k = block_factor(40, 50, 400)
    assert k == 3 and len(c) == 14 * 17
    v = tree["posterior"]["intensity"].stack(s=("chain", "draw")).transpose("s", ...).values
    np.testing.assert_allclose(c["mean__0"], block_mean(v.mean(0), 3).ravel(), rtol=1e-5)
    assert "3 × 3 cells" in (plot.caption_plain or "")


@pytest.mark.parametrize("frames", [None, 6])
def test_a_189_by_193_field_fits_the_inline_cap(frames: int | None) -> None:
    tree = field_tree(ny=189, nx=193, frames=frames, draws=5)
    stats = ["mean", "sd", "z", "q05", "q95", "median"] if frames is None else ["mean", "sd"]
    plot = field_map.render(tree, stats=stats)
    check_sci(plot, field_map)
    rows = plot.datasets["cells"].rows or 0
    if frames is None:
        assert rows == 189 * 193  # six panels at full resolution
    else:
        assert rows < 189 * 193 and "average of 2 × 2 cells" in (plot.caption_plain or "")
    assert rows <= ROW_BUDGET
    assert len(plot.datasets["cells"].data) < 0.9 * MAX_INLINE_ARROW_B64


def test_field_map_dims_and_errors() -> None:
    tree = field_tree(grid={"y": "row", "x": "col"})
    with pytest.raises(RecipeDataError, match="select"):
        # a third dim that the grid does not name: pick a level
        field_map.render(
            xr.DataTree.from_dict(
                {"/posterior": tree["posterior"].to_dataset().expand_dims(band=[1, 2], axis=2)}
            )
        )
    bare = tree["posterior"].to_dataset()
    bare["intensity"].attrs.clear()
    plot = field_map.render(
        bare.expand_dims(band=[1, 2], axis=2), y="row", x="col", select={"band": 2}
    )
    check_sci(plot, field_map)
    with pytest.raises(RecipeDataError, match="has the dims"):
        field_map.render(bare, y="row", x="nope")
    with pytest.raises(RecipeDataError, match="no draws"):
        field_map.render(bare.isel(chain=0, draw=0, drop=True))
    with pytest.raises(RecipeDataError, match="no draws"):
        field_map.render(tree, group="predictions")
    with pytest.raises(RecipeDataError):
        field_map.render(tree, stats=[])
    lower = field_map.render(tree, origin="lower", aspect="auto")
    assert "autorange" not in lower.spec["layout"]["yaxis"]
    assert "scaleanchor" not in lower.spec["layout"]["yaxis"]


def test_block_mean_is_nan_aware() -> None:
    a = np.arange(25, dtype=float).reshape(5, 5)
    a[0, 0] = np.nan
    a[4, 4] = np.nan
    out = block_mean(a, 2)
    assert out.shape == (3, 3)
    assert out[0, 0] == pytest.approx((1 + 5 + 6) / 3)
    assert np.isnan(out[2, 2])  # the corner block has only the NaN cell
    assert out[0, 2] == pytest.approx((4 + 9) / 2)  # part block at the edge
    np.testing.assert_allclose(block_mean(np.arange(5.0), 2, axes=1), [0.5, 2.5, 4.0])
    assert block_mean(a, 1) is a
    assert block_factor(189, 193, 40_000) == 1 and block_factor(189, 193, 10_000) == 2


# ------------------------------------------------------------------------- vector field


def test_vector_field_magnitude_and_arrows() -> None:
    tree = vector_tree()
    plot = vector_field.render(tree, arrows=7)
    check_sci(plot, vector_field)
    v = tree["posterior"]["traction"].stack(s=("chain", "draw")).transpose("s", "comp", ...).values
    mag = np.hypot(v[:, 0], v[:, 1])
    np.testing.assert_allclose(cells(plot)["mean__0"], mag.mean(0).ravel(), rtol=1e-5)
    arrows = arrow_to_frame(plot.datasets["arrows"])
    assert len(arrows) == 5 * 7 * 6  # 2 x 2 blocks of the 10 x 14 grid, 6 points per arrow
    shafts = arrows["x__0"].to_numpy().reshape(-1, 6)
    assert (shafts[:, 1] > shafts[:, 0]).mean() > 0.9  # mean vector points along +x
    lines = [t for t in plot.spec["data"] if t["type"] == "scatter"]
    assert len(lines) == 2 and all(t["xaxis"] == "x" for t in lines)
    assert "autorange" not in plot.spec["layout"]["yaxis"]  # the grid says origin lower
    cap = plot.caption_plain or ""
    assert "arrows" in cap and "do not fix the length" in cap


def test_vector_field_frames_move_the_arrows() -> None:
    plot = vector_field.render(vector_tree(frames=3), frame="t", stats=["mean"])
    check_sci(plot, vector_field)
    frame = plot.spec["frames"][1]
    assert frame["traces"] == [0, 1, 2]
    assert [t["type"] for t in frame["data"]] == ["heatmap", "scatter", "scatter"]
    assert frame["data"][1]["x"] == {"$ref": "arrows.x__1"}


def test_vector_field_needs_a_component() -> None:
    with pytest.raises(RecipeDataError):
        vector_field.render(field_tree(grid={"y": "row", "x": "col"}))


# ------------------------------------------------------------------------ profile bands


def test_profile_bands_curves_slider_and_coverage() -> None:
    plot = profile_bands.render(profile_tree())
    check_sci(plot, profile_bands)
    traces = plot.spec["data"]
    assert len(traces) == 5 * 6  # per level: 2 x (lower, upper) bands, the median, the dots
    steps = plot.spec["layout"]["sliders"][0]["steps"]
    assert [s["label"] for s in steps] == ["All", "0", "1", "2", "3", "4"]
    for s in steps:
        assert s["method"] == "restyle" and len(s["args"][0]["visible"]) == len(traces)
    assert sum(steps[2]["args"][0]["visible"]) == 6
    assert not any(t["visible"] for t in traces if t.get("fill") == "tonexty")
    frame = arrow_to_frame(plot.datasets["profile"])
    assert {"x", "p0", "lo_90_0", "hi_50_4", "o0"} <= set(frame.columns)
    assert frame["o0"].isna().sum() == 5
    cap = plot.caption_plain or ""
    assert "for one t" in cap and "% of the actual values are in the 90% band" in cap


def test_profile_bands_one_curve_and_max_curves() -> None:
    one = profile_bands.render(profile_tree(), select={"t": 2})
    check_sci(one, profile_bands)
    assert "sliders" not in one.spec["layout"]
    assert all(t.get("visible", True) for t in one.spec["data"])
    few = profile_bands.render(profile_tree(n_t=9), max_curves=3)
    steps = few.spec["layout"]["sliders"][0]["steps"]
    assert [s["label"] for s in steps] == ["All", "0", "4", "8"]
    assert "3 of the 9 levels" in (few.caption_plain or "")
    swapped = profile_bands.render(profile_tree(), along="t", by="x", max_curves=2)
    check_sci(swapped, profile_bands)
    with pytest.raises(RecipeDataError):
        profile_bands.render(profile_tree(), by="nope")


# ----------------------------------------------------------------------------- ensemble


@pytest.mark.parametrize("temporal", [False, True])
def test_ensemble_spaghetti_band_and_observed(temporal: bool) -> None:
    plot = ensemble.render(ensemble_tree(temporal=temporal), n_draws=12)
    check_sci(plot, ensemble)
    spaghetti = arrow_to_frame(plot.datasets["spaghetti"])
    assert len(spaghetti) == 12 * 21 and spaghetti["y"].isna().sum() == 12
    summary = arrow_to_frame(plot.datasets["summary"])
    assert {"x", "lo_90", "hi_90", "point", "observed"} <= set(summary.columns)
    assert (summary["lo_90"] <= summary["hi_90"]).all()
    if temporal:
        assert plot.spec["layout"]["xaxis"]["type"] == "date"
        assert pd.api.types.is_datetime64_any_dtype(summary["x"])
    cap = plot.caption_plain or ""
    assert "12 draws" in cap and "60 draws" in cap and "actual values" in cap


def test_ensemble_without_observations() -> None:
    tree = ensemble_tree()
    bare = xr.DataTree.from_dict({"/predictions": tree["predictions"].to_dataset()})
    plot = ensemble.render(bare, point="none", levels=[0.5, 0.9])
    check_sci(plot, ensemble)
    assert "actual" not in (plot.caption_plain or "")
    assert len([t for t in plot.spec["data"] if t.get("fill") == "tonexty"]) == 2


def test_ensemble_prefers_an_output_without_a_grid() -> None:
    # a model with a gridded rate(period, row, col) and a total(period): the spaghetti is
    # of the total, not of a field cell
    rate = RNG.gamma(2.0, 1.0, size=(2, 10, 6, 3, 4))
    total = rate.sum(axis=(-2, -1))
    outputs = {
        "rate": {"dims": ["period", "row", "col"], "grid": {"y": "row", "x": "col"}},
        "total": {"dims": ["period"]},
    }
    tree = xr.DataTree.from_dict(
        {
            "/model": xr.Dataset(attrs={"manifest": json.dumps({"outputs": outputs})}),
            "/model/posterior": xr.Dataset(
                {
                    "rate": (("chain", "draw", "period", "row", "col"), rate),
                    "total": (("chain", "draw", "period"), total),
                }
            ),
        }
    )
    plot = ensemble.render(tree, n_draws=5)
    check_sci(plot, ensemble)
    assert "total" in (plot.caption_plain or "") + json.dumps(plot.spec)


# ------------------------------------------------------------------- plotly in Recipe.render


def test_a_recipe_can_return_a_plotly_figure() -> None:
    import plotly.graph_objects as go

    @recipe("test.plotly_bar", title="Bars", settings=RecipeSettings, persona="expert")
    def bars(inp: Any, s: RecipeSettings) -> Figure:
        fig = go.Figure(go.Bar(x=["a", "b", "c"], y=[1.0, 2.0, 3.0]))
        return Figure(chart=fig, datasets={}, caption="Three bars.")

    plot = bars.render(xr.Dataset({"v": ("i", [1.0])}), title="Mine")
    assert plot.kind == "plotly" and plot.title == "Mine" and plot.persona == "expert"
    assert plot.caption_plain == "Three bars." and plot.provenance.recipe == "test.plotly_bar@1.0.0"
    assert {"trace0"} == set(plot.datasets)
    assert check_plot(plot) == []


def test_plotly_dataset_strings_are_stripped() -> None:
    from mapi_plots.recipes._base import plotly_plot

    frame = pd.DataFrame({"x": [1.0, 2.0], "label": ["<b>ok</b>", "<script>x</script>bad"]})
    fig = {"data": [{"type": "scatter", "x": {"$ref": "t.x"}, "text": {"$ref": "t.label"}}]}
    plot = plotly_plot(
        fig,
        id="p",
        title="T",
        datasets={"t": frame},
        persona="both",
        caption_plain=None,
        bindings=None,
        recipe="test.p@1.0.0",
        bundle_id=None,
    )
    labels = arrow_to_frame(plot.datasets["t"])["label"].tolist()
    assert labels[0] == "<b>ok</b>" and "<script" not in labels[1]


def test_sci_module_exports() -> None:
    assert set(sci.__all__) >= {"field_map", "vector_field", "profile_bands", "ensemble"}
