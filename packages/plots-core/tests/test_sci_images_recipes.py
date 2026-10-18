"""The image and spectrum recipes of the ``sci`` pack (``packs/sci_images.py``) on small
synthetic DataTrees (xarray only, no PyMC)."""

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
from mapi_plots.packs import sci_images  # noqa: E402
from mapi_plots.packs.sci import sci_recipes  # noqa: E402
from mapi_plots.packs.sci_images import (  # noqa: E402
    image_residuals,
    power_spectrum,
    ppc_image_grid,
    sci_image_recipes,
    swatches,
)
from mapi_plots.recipes import Recipe, RecipeDataError, get_recipe  # noqa: E402
from mapi_plots.sanitize import check_plot, sanitize_plot  # noqa: E402
from mapi_spec.plots import MAX_INLINE_ARROW_B64, PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(3)


# ------------------------------------------------------------------------------ fixtures


def image_tree(
    ny: int = 10,
    nx: int = 12,
    frames: int | None = None,
    draws: int = 20,
    *,
    group: str = "posterior_predictive",
    sigma: bool = False,
    obs_name: str = "img",
) -> xr.DataTree:
    """``<group>.img(chain, draw, [t], y, x)`` and ``observed_data.<obs_name>(…, y, x)``."""
    lead = (frames,) if frames else ()
    truth = RNG.gamma(3.0, 1.0, size=(*lead, ny, nx))
    pred = truth + RNG.normal(0, 0.5, size=(2, draws, *lead, ny, nx))
    obs = truth + RNG.normal(0, 0.5, size=truth.shape)
    obs[..., 0, 0] = np.nan
    dims = [*(["t"] if frames else []), "y", "x"]
    coords: dict[str, Any] = {"x": np.arange(nx) * 0.5}
    if frames:
        coords["t"] = [f"f{i}" for i in range(frames)]
    model = xr.Dataset({"img": (("chain", "draw", *dims), pred)}, coords)
    model["img"].attrs["grid"] = json.dumps(
        {"y": "y", "x": "x", **({"frame": "t"} if frames else {})}
    )
    if sigma:
        model["sigma"] = (("chain", "draw"), np.full((2, draws), 0.5))
    return xr.DataTree.from_dict(
        {
            f"/{group}": model,
            "/observed_data": xr.Dataset({obs_name: (dims, obs)}, coords),
        }
    )


def swatch_tree(grid: bool = True, n: int = 12) -> xr.DataTree:
    rgb = RNG.uniform(0.1, 0.9, size=(n, 3))
    draws = np.clip(rgb + RNG.normal(0, 0.05, size=(2, 15, n, 3)), 0, 1)
    coords: dict[str, Any] = {"item": [f"c{i}" for i in range(n)], "rgb": ["b", "g", "r"]}
    if grid:
        coords["row"] = ("item", [["A", "B", "C"][i // 4] for i in range(n)])
        coords["col"] = ("item", [i % 4 for i in range(n)])
    # channels stored as b, g, r: the recipe reorders them by label
    post = xr.Dataset(
        {
            "colour": (("chain", "draw", "item", "rgb"), draws[..., ::-1]),
            "p": (("chain", "draw", "item"), RNG.uniform(0, 1, size=(2, 15, n))),
        },
        coords,
    )
    obs = xr.Dataset({"colour": (("item", "rgb"), rgb[:, ::-1])}, coords)
    return xr.DataTree.from_dict({"/posterior": post, "/observed_data": obs})


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


def check_sci(plot: PlotSpec, r: Recipe[Any]) -> None:
    """The contract every sci recipe output satisfies."""
    assert plot.kind == "plotly"
    assert plot.id == r.id
    assert plot.provenance.recipe == r.ref
    assert plot.persona == r.persona
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


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


# ------------------------------------------------------------------------------- catalog


def test_the_recipes_are_in_the_sci_pack() -> None:
    ids = [r.id for r in sci_image_recipes()]
    assert ids == [
        "sci.image_residuals",
        "sci.ppc_image_grid",
        "sci.swatches",
        "sci.power_spectrum",
    ]
    assert set(ids) <= {r.id for r in sci_recipes()}
    assert get_recipe("sci.swatches") is swatches
    assert set(sci_images.__all__) >= {"image_residuals", "ppc_image_grid", "swatches"}
    assert image_residuals.requires_groups == ("observed_data",)
    assert "image" in image_residuals.tags and "colour" in swatches.tags


@pytest.mark.parametrize(
    "rid", ["sci.image_residuals", "sci.ppc_image_grid", "sci.swatches", "sci.power_spectrum"]
)
def test_examples_render(rid: str) -> None:
    data, options = ex.example_inputs()[rid]
    r = get_recipe(rid)
    check_sci(r.render(data, **options), r)


# ----------------------------------------------------------------------- image residuals


def test_image_residuals_panels_and_values() -> None:
    tree = image_tree()
    plot = image_residuals.render(tree)
    check_sci(plot, image_residuals)
    hm = [t for t in plot.spec["data"] if t["type"] == "heatmap"]
    assert [t["z"]["$ref"] for t in hm] == [
        "cells.observed__0",
        "cells.mean__0",
        "cells.resid__0",
        "cells.norm__0",
    ]
    # observed and mean share one value range; the residuals are symmetric around zero
    assert (hm[0]["zmin"], hm[0]["zmax"]) == (hm[1]["zmin"], hm[1]["zmax"])
    assert hm[2]["zmin"] == -hm[2]["zmax"] and hm[3]["zmin"] == -hm[3]["zmax"] <= -3
    c = frame(plot, "cells")
    d = tree["posterior_predictive"]["img"].stack(s=("chain", "draw")).transpose("s", ...).values
    o = tree["observed_data"]["img"].values
    mean, sd = d.mean(0), d.std(0, ddof=1)
    np.testing.assert_allclose(c["mean__0"], mean.ravel(), rtol=1e-5)
    np.testing.assert_allclose(c["resid__0"], (o - mean).ravel(), rtol=1e-4, atol=1e-5)
    # predictive draws hold the noise: no sigma term
    np.testing.assert_allclose(c["norm__0"], ((o - mean) / sd).ravel(), rtol=1e-4, atol=1e-5)
    assert np.isnan(c["norm__0"][0])  # the missing observed pixel stays empty
    hist = frame(plot, "hist")
    assert len(hist) == 36
    width = 0.25
    assert float(hist["density__0"].sum() * width) == pytest.approx(1.0, abs=1e-5)
    peak = np.exp(-(0.125**2) / 2) / np.sqrt(2 * np.pi)  # the bin centres nearest zero
    assert float(hist["normal"].max()) == pytest.approx(peak, rel=1e-4)
    bars = [t for t in plot.spec["data"] if t["type"] == "bar"]
    assert bars and bars[0]["xaxis"] == "x5"
    layout = plot.spec["layout"]
    assert layout["yaxis5"]["domain"][1] < min(layout["yaxis"]["domain"])
    cap = plot.caption_plain or ""
    assert "normalised residuals have an sd of" in cap and "Empty cells" in cap


def test_image_residuals_sigma_for_a_noise_free_output() -> None:
    tree = image_tree(group="posterior", sigma=True)
    plot = image_residuals.render(tree)
    check_sci(plot, image_residuals)
    c = frame(plot, "cells")
    d = tree["posterior"]["img"].stack(s=("chain", "draw")).transpose("s", ...).values
    o = tree["observed_data"]["img"].values
    mean, sd = d.mean(0), d.std(0, ddof=1)
    want = (o - mean) / np.sqrt(sd**2 + 0.25)
    np.testing.assert_allclose(c["norm__0"], want.ravel(), rtol=1e-4, atol=1e-5)
    assert "noise (sigma)" in (plot.caption_plain or "")
    # a number, as a variance
    num = image_residuals.render(tree, sigma=0.25, sigma_kind="variance")
    np.testing.assert_allclose(frame(num, "cells")["norm__0"], want.ravel(), rtol=1e-4, atol=1e-5)
    with pytest.raises(RecipeDataError, match="no sigma"):
        image_residuals.render(tree, sigma="nope")


def test_image_residuals_frames_slider_and_histogram_per_frame() -> None:
    plot = image_residuals.render(image_tree(frames=3), hist_height=150)
    check_sci(plot, image_residuals)
    frames = plot.spec["frames"]
    assert [f["name"] for f in frames] == ["f0", "f1", "f2"]
    assert frames[1]["traces"] == [0, 1, 2, 3, 4]
    assert frames[1]["data"][-1] == {"type": "bar", "y": {"$ref": "hist.density__1"}}
    assert {"density__0", "density__2"} <= set(frame(plot, "hist").columns)
    none = image_residuals.render(image_tree(), hist_height=0)
    check_sci(none, image_residuals)
    assert "hist" not in none.datasets


def test_image_residuals_observed_name_and_errors() -> None:
    # the only observed variable is used, whatever its name
    plot = image_residuals.render(image_tree(obs_name="counts"))
    assert "counts" in (plot.caption_plain or "")
    bare = image_tree()
    no_obs = xr.DataTree.from_dict(
        {"/posterior_predictive": bare["posterior_predictive"].to_dataset()}
    )
    with pytest.raises(RecipeDataError, match="no observed image"):
        image_residuals.render(no_obs)
    with pytest.raises(RecipeDataError, match="no draws"):
        image_residuals.render(bare, group="predictions")


def test_large_image_is_block_averaged_under_the_caps() -> None:
    plot = image_residuals.render(image_tree(ny=200, nx=210, draws=3), max_cells=10_000)
    check_sci(plot, image_residuals)
    assert (plot.datasets["cells"].rows or 0) <= 10_000
    assert "average of" in (plot.caption_plain or "")


# ------------------------------------------------------------------------ ppc image grid


def test_ppc_image_grid_in_order() -> None:
    tree = image_tree()
    plot = ppc_image_grid.render(tree, n_draws=4)
    check_sci(plot, ppc_image_grid)
    hm = [t for t in plot.spec["data"] if t["type"] == "heatmap"]
    assert hm[0]["z"] == {"$ref": "cells.observed__0"} and len(hm) == 5
    assert len({(t["zmin"], t["zmax"]) for t in hm}) == 1  # one colour scale
    c = frame(plot, "cells")
    np.testing.assert_allclose(
        c["observed__0"], tree["observed_data"]["img"].values.ravel(), rtol=1e-5
    )
    titles = [a["text"] for a in plot.spec["layout"]["annotations"]]
    assert titles[0] == "<b>Observed</b>" and "Draw 1" in titles[1]
    assert "first panel shows the observed" in (plot.caption_plain or "")


def test_ppc_image_grid_shuffle_hides_the_real_panel() -> None:
    plot = ppc_image_grid.render(image_tree(), n_draws=5, shuffle=True, seed=4)
    check_sci(plot, ppc_image_grid)
    hm = [t for t in plot.spec["data"] if t["type"] == "heatmap"]
    pos = next(i for i, t in enumerate(hm) if t["z"]["$ref"] == "cells.observed__0")
    titles = [
        a["text"].replace("<b>", "").replace("</b>", "") for a in plot.spec["layout"]["annotations"]
    ]
    assert titles == [f"Panel {chr(65 + i)}" for i in range(6)]
    assert f"the observed image is panel {chr(65 + pos)}." in (plot.caption_plain or "")
    tree = image_tree()
    a = ppc_image_grid.render(tree, n_draws=5, shuffle=True, seed=4)
    b = ppc_image_grid.render(tree, n_draws=5, shuffle=True, seed=4)
    assert a.caption_plain == b.caption_plain  # same seed, same order


def test_ppc_image_grid_frames() -> None:
    plot = ppc_image_grid.render(image_tree(frames=2), n_draws=2)
    check_sci(plot, ppc_image_grid)
    assert [f["name"] for f in plot.spec["frames"]] == ["f0", "f1"]


# ------------------------------------------------------------------------------ swatches


def test_swatches_map_reorders_channels_and_shows_prob() -> None:
    tree = swatch_tree()
    plot = swatches.render(tree, prob="p", n_draws=2)
    check_sci(plot, swatches)
    tiles = frame(plot, "tiles")
    rgb = tree["observed_data"]["colour"].values[:, ::-1]  # back to r, g, b
    want = [f"#{a:02x}{b:02x}{c:02x}" for a, b, c in np.round(rgb * 255).astype(int)]
    assert tiles["colour__0"].tolist() == want
    traces = plot.spec["data"]
    assert [t["name"] for t in traces] == ["Observed", "Mean", "Draw 1", "Draw 2"]
    assert traces[0]["marker"]["line"] == {"width": 0}
    assert traces[1]["marker"]["line"]["width"] == {"$ref": "tiles.border"}
    assert sorted(set(tiles["y"])) == [0, 1, 2]  # row letters A, B, C
    assert plot.spec["layout"]["yaxis"]["ticktext"] == ["A", "B", "C"]
    assert traces[3]["xaxis"] == "x4"
    cap = plot.caption_plain or ""
    assert "map" in cap and "units from the observed colour" in cap and "borders" in cap


def test_swatches_list_and_opacity() -> None:
    plot = swatches.render(swatch_tree(grid=False, n=30), prob="p", prob_as="opacity", max_items=10)
    check_sci(plot, swatches)
    tiles = frame(plot, "tiles")
    assert len(tiles) == 10 and {"x__0", "x__4", "opacity"} <= set(tiles.columns)
    assert plot.spec["layout"]["xaxis"]["ticktext"] == [
        "Observed",
        "Mean",
        "Draw 1",
        "Draw 2",
        "Draw 3",
    ]
    assert plot.spec["data"][1]["marker"]["opacity"] == {"$ref": "tiles.opacity"}
    assert "10 of the 30" in (plot.caption_plain or "")
    forced = swatches.render(swatch_tree(), arrange="list", n_draws=0)
    check_sci(forced, swatches)
    assert [t["name"] for t in forced.spec["data"]] == ["Observed", "Mean"]


def test_swatches_errors() -> None:
    with pytest.raises(RecipeDataError):
        swatches.render(swatch_tree(grid=False), arrange="map")
    with pytest.raises(RecipeDataError, match="no variable"):
        swatches.render(swatch_tree(), prob="nope")
    with pytest.raises(RecipeDataError):
        swatches.render(image_tree())  # no rgb dim


# ------------------------------------------------------------------------ power spectrum


def test_power_spectrum_bands_theory_and_error_bars() -> None:
    tree = ex.spectrum_tree()
    plot = power_spectrum.render(tree, theory="theory_dl", log_y=True)
    check_sci(plot, power_spectrum)
    spec = frame(plot, "spectrum")
    np.testing.assert_allclose(spec["x"], tree["posterior"]["ell"].values, rtol=1e-6)
    assert {"lo_90", "hi_90", "lo_50", "hi_50", "point", "observed", "error"} <= set(spec)
    assert (spec["lo_90"] <= spec["lo_50"]).all() and (spec["hi_50"] <= spec["hi_90"]).all()
    assert len(frame(plot, "theory")) == 300
    obs = next(t for t in plot.spec["data"] if t.get("name") == "observed")
    assert obs["error_y"]["array"] == {"$ref": "spectrum.error"}
    assert plot.spec["layout"]["yaxis"]["type"] == "log"
    assert plot.spec["layout"]["xaxis"]["title"]["text"] == "ell"
    cap = plot.caption_plain or ""
    assert "theory curve" in cap and "error bars" in cap and "in the 90% band" in cap


def test_power_spectrum_without_extras_and_errors() -> None:
    tree = ex.spectrum_tree()
    bare = xr.DataTree.from_dict({"/posterior": tree["posterior"].to_dataset()})
    plot = power_spectrum.render(bare, x_coord="bin", levels=[0.9])
    check_sci(plot, power_spectrum)
    assert set(plot.datasets) == {"spectrum"}
    assert "observed" not in (plot.caption_plain or "")
    with pytest.raises(RecipeDataError, match="no theory"):
        power_spectrum.render(tree, theory="nope")
    with pytest.raises(RecipeDataError, match="no coord"):
        power_spectrum.render(tree, x_coord="nope")
    two = xr.DataTree.from_dict(
        {
            "/posterior": xr.Dataset(
                {"p": (("chain", "draw", "a", "b"), RNG.normal(size=(1, 5, 3, 4)))}
            )
        }
    )
    with pytest.raises(RecipeDataError, match="along"):
        power_spectrum.render(two)
    one = power_spectrum.render(two, along="b", select={"a": 1})
    check_sci(one, power_spectrum)
