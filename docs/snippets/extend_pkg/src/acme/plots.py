"""acme.weekly_total: a plot recipe scaffolded by ``mapi plugin init --kind recipe``.

``weekly_total`` adds up a model output (``y_hat`` by default) over every group and draws
one bar per week. A recipe is a function ``(inp, settings) -> Figure`` that returns an
Altair (Vega-Lite) chart, the tables it reads by name and a plain-language caption; the
platform sanitizes the result and shows it in a sandboxed frame. Replace the function with
your chart, update the About card and keep ``mapi plugin test`` (the contract kit) green.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from mapi_plots.palette import ESTIMATE
from mapi_plots.recipes import Figure, RecipeDataError, RecipeInput, RecipeSettings, named, recipe
from mapi_spec.about import About
from pydantic import Field

__all__ = ["RECIPES", "WeeklyTotalSettings", "weekly_total"]


class WeeklyTotalSettings(RecipeSettings):
    """The settings form of the recipe (the playground renders it from this schema)."""

    var: str = Field("y_hat", max_length=128, description="The variable to add up")
    group: str = Field("predictions", max_length=128, description="The group that holds it")
    time_dim: str = Field("time", max_length=128, description="The weekly time dim")


def _example() -> tuple[Any, dict[str, Any]]:
    """Synthetic predictions of three stores over twelve weeks (the catalog example)."""
    rng = np.random.default_rng(0)
    time = pd.date_range("2025-01-05", periods=12, freq="W-SUN")
    y_hat = 200.0 + rng.normal(0, 15, (3, 12)) + np.linspace(0, 30, 12)
    ds = xr.Dataset(
        {"y_hat": (("geo", "time"), y_hat)},
        coords={"geo": ["north", "south", "east"], "time": time},
    )
    return xr.DataTree.from_dict({"predictions": ds}), {}


ABOUT = About(
    title="Weekly totals",
    summary_plain="Adds up a value over every store or region and shows one bar per week. "
    "Use it to see the total at a glance.",
    use_when=["You want the total of all groups for each week"],
    avoid_when=["You must compare groups with each other"],
    data_needs=["A model output with a weekly time axis"],
    assumptions=["The values can be added up (sales, spend)"],
    outputs=["One bar per week with the total"],
    difficulty="beginner",
    typical_runtime="instant",
    example="Three stores that each sell about 200 a week give bars of about 600.",
)


@recipe(
    "acme.weekly_total",
    title="Weekly totals",
    settings=WeeklyTotalSettings,
    persona="both",
    summary="The total of a value over every group, one bar per week.",
    tags=["totals", "weekly"],
    about=ABOUT,
    example=_example,
    pack_label="Acme",
)
def weekly_total(inp: RecipeInput, s: WeeklyTotalSettings) -> Figure:
    """One bar per week: ``var`` summed over every other dim."""
    ds = inp.require(s.group)
    if s.var not in ds:
        raise RecipeDataError(f"the group {s.group!r} has no variable {s.var!r}")
    da = ds[s.var]
    if s.time_dim not in da.dims:
        raise RecipeDataError(f"{s.var!r} has no {s.time_dim!r} dim")
    total = da.sum([d for d in da.dims if d != s.time_dim])
    frame = pd.DataFrame(
        {"week": pd.to_datetime(total[s.time_dim].values), "total": total.values.astype(float)}
    )
    chart = (
        alt.Chart(named("totals"))
        .mark_bar(color=ESTIMATE)
        .encode(
            x=alt.X("week:T", title="Week"),
            y=alt.Y("total:Q", title=f"Total {s.var}"),
            tooltip=["week:T", "total:Q"],
        )
        .properties(width=s.width, height=s.height)
    )
    peak = frame.loc[frame["total"].idxmax()]
    caption = (
        f"Each bar is the total {s.var} of one week. The highest week is "
        f"{peak['week']:%d %b %Y} with {peak['total']:,.0f}."
    )
    return Figure(chart=chart, datasets={"totals": frame}, caption=caption)


RECIPES = [weekly_total]
"""The ``mapi.plots`` entry point: every recipe listed here is registered."""
