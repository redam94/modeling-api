"""Data overview: series facets, a missingness heatmap and group sizes."""

from __future__ import annotations

from typing import Any

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field

from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    VlType,
    named,
    recipe,
)
from mapi_plots.recipes._util import coord_values, facet_dim, pct, time_dim

__all__ = ["DataOverviewSettings", "data_overview"]


class DataOverviewSettings(RecipeSettings):
    group: str | None = Field(None, description="Group holding the data (default: auto)")
    vars: list[str] | None = Field(None, max_length=32)
    along: str | None = Field(None, description="Time-like dim (default: auto-detected)")
    facet: str | None = Field(None, description="Categorical dim to colour by (default: auto)")
    max_vars: int = Field(8, ge=1, le=32)
    max_levels: int = Field(12, ge=1, le=64)
    max_rows: int = Field(20_000, ge=100, le=200_000)


def _dataset(inp: RecipeInput, s: DataOverviewSettings) -> xr.Dataset:
    if s.group is not None:
        return inp.require(s.group)
    ds = inp.find("observed_data", "data", "constant_data")
    if ds is None:
        ds = inp.root()
    if ds is None:
        raise RecipeDataError("no data to plot: pass a Dataset or a tree with observed_data")
    return ds


@recipe(
    "core.data_overview",
    title="Data overview",
    settings=DataOverviewSettings,
    persona="both",
    summary="Each variable over time for each group, the missing values and the size of each "
    "group.",
)
def data_overview(inp: RecipeInput, s: DataOverviewSettings) -> Figure:
    """Series per variable, a missingness heatmap and observations per group."""
    ds = _dataset(inp, s)
    names = s.vars or [
        str(v) for v in ds.data_vars if np.issubdtype(ds[v].dtype, np.number) and ds[v].ndim
    ]
    names = names[: s.max_vars]
    if not names:
        raise RecipeDataError("the data has no numeric variables")
    missing = [n for n in names if n not in ds.data_vars]
    if missing:
        raise RecipeDataError(f"unknown variables: {', '.join(missing)}")
    t = time_dim(ds[names], s.along)
    x_dim = t or str(ds[names[0]].dims[0])
    facet = facet_dim(ds[names], [x_dim], prefer=s.facet, max_levels=s.max_levels)

    series_parts: list[pd.DataFrame] = []
    missing_parts: list[pd.DataFrame] = []
    summed: set[str] = set()
    for name in names:
        da = ds[name].astype(float)
        if x_dim not in da.dims:
            continue
        keep = [x_dim] + ([facet] if facet and facet in da.dims else [])
        other = [d for d in da.dims if d not in keep]
        summed |= set(map(str, other))
        miss = da.isnull().mean([d for d in da.dims if d != x_dim]) if da.ndim > 1 else da.isnull()
        missing_parts.append(
            pd.DataFrame(
                {
                    "x": coord_values(da, x_dim),
                    "variable": name,
                    "missing": miss.values.astype(float),
                }
            )
        )
        agg = da.sum(other, min_count=1) if other else da
        frame = agg.to_dataframe(name="value").reset_index()
        frame = frame.rename(columns={x_dim: "x"})
        frame["group"] = frame[facet].astype(str) if facet and facet in frame else "all"
        frame["variable"] = name
        series_parts.append(frame[["x", "group", "variable", "value"]])
    if not series_parts:
        raise RecipeDataError(f"no variable has the dim {x_dim!r}")
    series = pd.concat(series_parts, ignore_index=True)
    if len(series) > s.max_rows:
        step = int(np.ceil(len(series) / s.max_rows))
        xs = np.sort(series["x"].unique())[::step]
        series = series[series["x"].isin(xs)]
    miss_frame = pd.concat(missing_parts, ignore_index=True)
    x_type: VlType = "temporal" if np.issubdtype(ds[x_dim].dtype, np.datetime64) else "quantitative"
    if x_dim not in ds.coords:
        x_type = "quantitative"

    lines = (
        alt.Chart(named("series"))
        .mark_line(strokeWidth=1.2)
        .encode(
            x=alt.X("x", type=x_type, title=x_dim),
            y=alt.Y("value:Q", title=None),
            color=alt.Color("group:N", title=facet or ""),
            tooltip=[alt.Tooltip("x", type=x_type), "group:N", "value:Q"],
        )
        .properties(width=s.width, height=max(60, s.height // 2))
        .facet(row=alt.Row("variable:N", title=None))
        .resolve_scale(y="independent")
    )
    heat = (
        alt.Chart(named("missing"))
        .mark_rect()
        .encode(
            x=alt.X("x", type=x_type, title=x_dim),
            y=alt.Y("variable:N", title=None),
            color=alt.Color(
                "missing:Q",
                title="share missing",
                scale=alt.Scale(
                    domain=[0, max(0.1, float(miss_frame["missing"].max()))], scheme="oranges"
                ),
            ),
            tooltip=[
                "variable:N",
                alt.Tooltip("x", type=x_type),
                alt.Tooltip("missing:Q", format=".0%"),
            ],
        )
        .properties(width=s.width, height=18 * len(names), title="Missing values")
    )
    charts: list[Any] = [lines, heat]
    datasets: dict[str, pd.DataFrame] = {"series": series, "missing": miss_frame}
    sizes_txt = ""
    if facet is not None:
        first = ds[names[0]]
        counts = (
            first.notnull().sum([d for d in first.dims if d != facet])
            if facet in first.dims
            else None
        )
        if counts is not None:
            groups = pd.DataFrame(
                {"group": coord_values(first, facet).astype(str), "n": counts.values.astype(int)}
            )
            datasets["groups"] = groups
            charts.append(
                alt.Chart(named("groups"))
                .mark_bar()
                .encode(
                    x=alt.X("n:Q", title=f"observed values of {names[0]}"),
                    y=alt.Y("group:N", title=facet, sort="-x"),
                    tooltip=["group:N", "n:Q"],
                )
                .properties(width=s.width // 2, height=18 * len(groups), title="Group sizes")
            )
            sizes_txt = (
                f" The groups ({facet}) have from {int(groups.n.min())} to "
                f"{int(groups.n.max())} observed values of {names[0]}."
            )
    worst = miss_frame.groupby("variable")["missing"].mean().sort_values(ascending=False)
    if worst.iloc[0] > 0:
        miss_txt = (
            f" {worst.index[0]} has the most missing values ({pct(float(worst.iloc[0]))} overall)."
        )
    else:
        miss_txt = " No values are missing."
    agg_txt = f" The values show the sum over {', '.join(sorted(summed))}." if summed else ""
    caption = (
        f"Each panel shows one variable over {x_dim}"
        + (f", with one line for each {facet}" if facet else "")
        + "."
        + agg_txt
        + miss_txt
        + sizes_txt
    )
    return Figure(chart=alt.vconcat(*charts), datasets=datasets, caption=caption)
