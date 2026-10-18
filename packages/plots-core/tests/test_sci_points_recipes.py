"""``sci.point_pattern`` on small synthetic DataTrees (xarray only, no PyMC)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("plotly")

from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.examples import point_pattern_tree  # noqa: E402
from mapi_plots.packs.sci import sci_recipes  # noqa: E402
from mapi_plots.packs.sci_points import ellipse_paths, point_pattern  # noqa: E402
from mapi_plots.recipes import Recipe, RecipeDataError, get_recipe  # noqa: E402
from mapi_plots.sanitize import check_plot, sanitize_plot  # noqa: E402
from mapi_spec.plots import MAX_INLINE_ARROW_B64, PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(5)


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
    assert plot.kind == "plotly"
    assert plot.id == r.id
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


def events_tree(n: int = 30) -> xr.DataTree:
    """Plain event positions with sd variables, in the root of the tree."""
    ds = xr.Dataset(
        {
            "x": (("event",), RNG.uniform(0, 10, n)),
            "y": (("event",), RNG.uniform(0, 5, n)),
            "sd_x": (("event",), np.full(n, 0.2)),
            "sd_y": (("event",), np.full(n, 0.1)),
        }
    )
    return xr.DataTree(ds)


def test_is_in_the_sci_pack() -> None:
    assert "sci.point_pattern" in [r.id for r in sci_recipes()]
    assert get_recipe("sci.point_pattern") is point_pattern


def test_example_draws_points_ellipses_and_field() -> None:
    plot = point_pattern.render(point_pattern_tree())
    check_sci(plot, point_pattern)
    assert {"cells", "ellipses", "points_0", "points_1"} <= set(plot.datasets)
    ell = arrow_to_frame(plot.datasets["ellipses"])
    assert len(ell) == 40 * 26  # 25 vertices + a NaN gap per point
    cap = plot.caption_plain or ""
    assert "90% probability" in cap and "render" in cap and "group" in cap
    assert plot.spec["layout"]["yaxis"]["scaleanchor"] == "x"


def test_plain_positions_with_sd_and_no_field() -> None:
    plot = point_pattern.render(events_tree(), field="none", group="none")
    check_sci(plot, point_pattern)
    assert set(plot.datasets) == {"points", "ellipses"}
    ell = arrow_to_frame(plot.datasets["ellipses"]).dropna()
    pts = arrow_to_frame(plot.datasets["points"])
    # axis-aligned: the x half-width is 0.2 * sqrt(-2 ln 0.1) around the first point
    half = 0.2 * np.sqrt(-2 * np.log(0.1))
    first = ell.iloc[:25]
    assert np.isclose(first["x"].max() - pts["x"].iloc[0], half, rtol=1e-3)
    assert "the value of" in (plot.caption_plain or "")


def test_draws_with_observed_points_and_options() -> None:
    n = 12
    truth = RNG.uniform(0, 1, (2, n))
    post = xr.Dataset(
        {
            "x": (("chain", "draw", "point"), truth[0] + RNG.normal(0, 0.01, (2, 20, n))),
            "y": (("chain", "draw", "point"), truth[1] + RNG.normal(0, 0.02, (2, 20, n))),
        }
    )
    obs = xr.Dataset({"x": (("point",), truth[0]), "y": (("point",), truth[1])})
    tree = xr.DataTree.from_dict({"/posterior": post, "/observed_data": obs})
    plot = point_pattern.render(tree, level=0.5)
    check_sci(plot, point_pattern)
    assert "observed" in plot.datasets
    assert "open circles" in (plot.caption_plain or "")
    off = point_pattern.render(tree, ellipses=False, observed=False, max_points=5)
    check_sci(off, point_pattern)
    assert "ellipses" not in off.datasets and "observed" not in off.datasets
    assert len(arrow_to_frame(off.datasets["points"])) == 5


def test_ellipse_follows_the_correlation() -> None:
    x, y = ellipse_paths(np.zeros(1), np.zeros(1), np.ones(1), np.ones(1), np.array([0.9]), 1.0)
    fin = np.isfinite(x)
    far = np.argmax(np.hypot(x[fin], y[fin]))
    assert x[fin][far] * y[fin][far] > 0  # the long axis is on the diagonal


def test_missing_inputs_are_typed_errors() -> None:
    with pytest.raises(RecipeDataError, match="no variable 'x'"):
        point_pattern.render(xr.DataTree(xr.Dataset({"a": (("p",), np.ones(3))})))
    bad = xr.DataTree(xr.Dataset({"x": (("p",), np.ones(3))}))
    with pytest.raises(RecipeDataError, match="no 'y'"):
        point_pattern.render(bad)
    with pytest.raises(RecipeDataError, match="no field variable"):
        point_pattern.render(events_tree(), field="image")
    with pytest.raises(RecipeDataError, match="no group variable"):
        point_pattern.render(events_tree(), group="cluster")
    nan = xr.DataTree(xr.Dataset({"x": (("p",), [np.nan]), "y": (("p",), [np.nan])}))
    with pytest.raises(RecipeDataError, match="no finite positions"):
        point_pattern.render(nan)
