"""Vega-Lite recipes of the explorer views (M3.5): JSON-only, inline named datasets, capped
rows, recipe provenance, and every plot passes the platform sanitizer."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from mapi_plots.explorer import EXPLORER_RECIPES, MAX_ROWS, explorer_plots
from mapi_plots.sanitize import sanitize_plot
from mapi_spec.plots import PlotSpec
from mapi_spec.views import (
    BacktestView,
    ComparisonView,
    CorrelationView,
    DecompositionView,
    StationarityView,
    TimeSeriesView,
)

pytestmark = pytest.mark.unit


def _check(view: dict[str, object]) -> list[dict[str, object]]:
    plots = explorer_plots(view)
    for p in plots:
        PlotSpec.model_validate(sanitize_plot(p).plot.model_dump(mode="json"))
        assert p["provenance"]["recipe"].startswith("explore.")  # type: ignore[index]
        assert "url" not in json.dumps(p["spec"])
    return plots


def test_recipes_cover_every_view_format() -> None:
    assert len(EXPLORER_RECIPES) == 33
    assert explorer_plots({"format": "mapi.unknown/v1"}) == []
    assert explorer_plots({"format": "mapi.model-explorer.diagnostics/v1"}) == []


def test_long_series_are_thinned_to_the_row_cap() -> None:
    n = 5000
    view = TimeSeriesView(
        dim="t",
        series=[
            {"var": f"v{i}", "coords": list(range(n)), "values": [float(j) for j in range(n)]}
            for i in range(6)
        ],  # type: ignore[misc]
    ).model_dump(mode="json")
    (plot,) = _check(view)
    rows = plot["spec"]["datasets"]["series"]  # type: ignore[index]
    assert len(rows) == MAX_ROWS
    assert plot["spec"]["spec"]["encoding"]["x"]["type"] == "quantitative"  # type: ignore[index]


def test_dates_become_temporal_axes_and_tables_render() -> None:
    ts = TimeSeriesView(
        dim="week",
        series=[{"var": "y", "coords": ["2024-01-07", "2024-01-14"], "values": [1.0, None]}],
    )  # type: ignore[list-item]
    (plot,) = _check(ts.model_dump(mode="json"))
    assert plot["spec"]["spec"]["encoding"]["x"]["type"] == "temporal"  # type: ignore[index]
    corr = CorrelationView(vars=["a", "b"], matrix=[[1.0, 0.5], [0.5, 1.0]])
    assert len(_check(corr.model_dump(mode="json"))) == 1
    st = StationarityView(
        rows=[
            {
                "var": "y",
                "verdict": "difference",
                "verdict_plain": "wanders",
                "adf": {
                    "test": "test.adf",
                    "p_value": 0.4,
                    "reject": False,
                    "decision": "unit_root",
                    "verdict_plain": "w",
                    "null_plain": "n",
                },
            }
        ]
    )  # type: ignore[list-item]
    assert len(_check(st.model_dump(mode="json"))) == 1


def test_model_views_plot() -> None:
    dec = DecompositionView(
        dim="t", coords=[1, 2, 3], components={"base": [1.0, 1.0, 1.0], "tv": [0.5, 0.2, None]}
    )
    assert len(_check(dec.model_dump(mode="json"))) == 1
    bt = BacktestView(
        folds=[{"fold": {"index": 0}, "metrics": {"mae": 1.2}}], benchmarks={"naive": {"mae": 2.0}}
    )  # type: ignore[list-item]
    assert len(_check(bt.model_dump(mode="json"))) == 1
    cmp = ComparisonView(
        models=["model://p/a@v1", "model://p/b@v1"],
        metrics=["rmse"],
        table={"model://p/a@v1": {"rmse": 1.0}, "model://p/b@v1": {"rmse": 2.0}},
    )
    assert len(_check(cmp.model_dump(mode="json"))) == 1


def test_backtest_plots_error_by_horizon() -> None:
    folds = [
        {
            "fold": {"index": j},
            "metrics": {"mae": 1.0 + j},
            "by_horizon": {str(h): {"mae": h * (1.0 + j)} for h in (1, 2, 3)},
        }
        for j in range(2)
    ]
    view = BacktestView(
        folds=folds,  # type: ignore[arg-type]
        by_horizon={"1": {"mae": 1.5}, "2": {"mae": 3.0}, "3": {"mae": 4.5}},
    ).model_dump(mode="json")
    view["by_horizon"]["x"] = {"mae": 9.0}  # type: ignore[index]  # not a horizon: ignored
    folds_plot, horizons = _check(view)
    assert folds_plot["id"] == "explore.backtest.folds"
    assert horizons["id"] == "explore.backtest.horizons"
    assert horizons["title"] == "Backtest error by horizon"
    assert horizons["caption_plain"] == (
        "Shows the error for each number of periods ahead. The error usually increases with "
        "the horizon."
    )
    data = horizons["spec"]["datasets"]  # type: ignore[index]
    assert [r["h"] for r in data["pooled"]] == [1, 2, 3]
    assert len(data["folds"]) == 6
    # one pooled horizon (or none) draws only the fold chart
    view["by_horizon"] = {"1": {"mae": 1.5}, "2": {"mae": None}}
    assert [p["id"] for p in _check(view)] == ["explore.backtest.folds"]


@pytest.mark.parametrize(
    ("section", "count"),
    [
        ("structure", 1),
        ("images", 2),
        ("profiles", 2),
        ("map", 2),
        ("network", 2),
        ("tree", 1),
        ("circular", 2),
        ("points", 1),
    ],
)
def test_the_scientific_views_plot(section: str, count: int) -> None:
    from mapi_spec.explorer_examples import SCIENCE_VIEW_EXAMPLES

    plots = _check(dict(SCIENCE_VIEW_EXAMPLES[section]))
    assert len(plots) == count
    for p in plots:
        assert p["kind"] == "vega-lite"
        datasets = p["spec"]["datasets"]  # type: ignore[index]
        assert all(len(rows) <= MAX_ROWS for rows in datasets.values())


def test_scientific_recipes_cap_rows_and_refuse_bad_colours() -> None:
    from mapi_spec.explorer_examples import SCIENCE_VIEW_EXAMPLES

    n = 30_000
    big = {
        "format": "mapi.data-explorer.points/v1",
        "point_dim": "p",
        "x": "x",
        "y": "y",
        "n": n,
        "sample": {"x": list(range(n)), "y": list(range(n)), "index": list(range(n))},
    }
    (plot,) = _check(big)
    assert len(plot["spec"]["datasets"]["points"]) == MAX_ROWS  # type: ignore[index]
    pts = json.loads(json.dumps(SCIENCE_VIEW_EXAMPLES["points"]))
    pts["srgb"][0] = "red;url(x)"
    (plot,) = _check(pts)
    assert "color" not in plot["spec"]["encoding"]  # type: ignore[index]
    assert all(
        "hex" not in r or r["hex"].startswith("#") for r in plot["spec"]["datasets"]["points"]
    )  # type: ignore[index]
    assert explorer_plots({"format": "mapi.data-explorer.tree/v1", "layout": {}}) == []


def test_the_module_is_json_only() -> None:
    code = (
        "import sys, mapi_plots.explorer\n"
        "mods = ('numpy', 'pandas', 'altair', 'xarray', 'pyarrow')\n"
        "bad = [m for m in mods if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
