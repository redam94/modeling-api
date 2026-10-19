"""Scientific-model pack, part 2: maps of areas, phylogenetic trees and circular data.

Plotly figures, rendered like the other ``sci.*`` recipes (:mod:`mapi_plots.packs.sci`):

* ``sci.area_map``: a choropleth of a per-area output (mean, median, sd, the probability
  that the value is above a threshold) and the observed values, with the area polygons
  drawn as filled ``scatter`` shapes on cartesian axes (never plotly geo: the plot sandbox
  cannot fetch base maps);
* ``sci.phylogeny``: a rectangular tree drawn from ``parent`` / ``branch_length`` arrays
  next to a strip of the per-tip estimate (point and interval) and the observed trait;
* ``sci.rose``: polar charts of angles (directions, hours, days of the year): a rose
  histogram of observed angles with a predictive band per bin, or a density over an angle
  dim with bands, with a slider over a ``frame`` dim.

Input conventions (dataset variables, found in ``observed_data``, ``constant_data``, a
``geometry`` or ``tree`` group, the group of the plotted variable or the root):

* polygons: ``ring_x(area, vertex)`` and ``ring_y(area, vertex)``, NaN padded; a NaN inside
  the vertex list starts a new ring (islands). The first dim is the area dim. Optional
  ``adjacency(area, area_)`` (non-zero = neighbours) for the edge overlay;
* trees: ``parent(node)`` (integer position of the parent node; ``-1``, NaN or the node
  itself for a root), optional ``branch_length(node)`` (default 1), ``is_tip(node)`` (default:
  nodes that are nobody's parent) and ``tip_label(node)``. Tips map to the plotted
  variable's tip dim by label when both have labels, else in node order;
* angles: values in ``units`` (radians, degrees, hours or days) on a circle of ``period``.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field, field_validator

from mapi_plots.packs.sci import (
    CURVE_GROUPS,
    FIELD_GROUPS,
    OBS_GROUPS,
    ROW_BUDGET,
    Colormap,
    GroupName,
    _axis_id,
    _domains,
    _find_draws,
    _frame_controls,
    _label,
    _only,
    _range,
    _scale,
    _select,
    _thin,
)
from mapi_plots.palette import BAND, EMPTY, ESTIMATE, OBSERVED, REFERENCE, STRONG, SURFACE
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    recipe,
)
from mapi_plots.recipes._util import SAMPLE, coord_values, pct, sample_dims, stack_samples

__all__ = [
    "AreaMapSettings",
    "PhylogenySettings",
    "RoseSettings",
    "area_map",
    "phylogeny",
    "rose",
    "sci_map_recipes",
]

AUX_GROUPS = ("observed_data", "constant_data", "geometry", "tree")


# ------------------------------------------------------------------------------ lookups


def _aux(inp: RecipeInput, name: str, first: str | None = None) -> xr.DataArray | None:
    """Dataset variable ``name`` from the plotted variable's group, the data groups or the
    root (``None`` when there is none)."""
    for g in (first, *AUX_GROUPS):
        if g is None:
            continue
        ds = inp.find(g)
        if ds is not None and name in ds.data_vars:
            return ds[name]
    root = inp.root()
    if root is not None and name in root.data_vars:
        return root[name]
    return None


def _summary_var(
    inp: RecipeInput, var: str, groups: Sequence[str]
) -> tuple[xr.DataArray, str] | None:
    """``var`` without draws (a score summary or a point estimate) and its group."""
    for g in (*groups, "predictions", "posterior_predictive", "posterior", "derived"):
        ds = inp.find(g)
        if ds is not None and var in ds.data_vars:
            return ds[var], g
    root = inp.root()
    if root is not None and var in root.data_vars:
        return root[var], "root"
    return None


def _align(da: xr.DataArray, dim: str, ref: xr.DataArray, ref_dim: str) -> xr.DataArray:
    """``da`` reordered along ``dim`` to ``ref``'s ``ref_dim`` labels (NaN where missing), or
    unchanged when the lengths agree and there are no labels to match."""
    if dim in da.coords and ref_dim in ref.coords:
        mine = [str(v) for v in da[dim].values]
        theirs = [str(v) for v in ref[ref_dim].values]
        if mine != theirs and set(mine) & set(theirs):
            pos = {v: i for i, v in enumerate(mine)}
            idx = [pos.get(v, -1) for v in theirs]
            out = da.isel({dim: [max(i, 0) for i in idx]})
            miss = xr.DataArray(np.array([i < 0 for i in idx]), dims=[dim])
            return out.where(~miss)
    if da.sizes[dim] != ref.sizes[ref_dim]:
        raise RecipeDataError(
            f"{da.name!r} has {da.sizes[dim]} levels of {dim!r} but the geometry has "
            f"{ref.sizes[ref_dim]}"
        )
    return da


def _nan_stats(draws: np.ndarray) -> dict[str, np.ndarray]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return {
            "mean": np.nanmean(draws, axis=0),
            "median": np.nanmedian(draws, axis=0),
            "sd": np.nanstd(draws, axis=0, ddof=1 if draws.shape[0] > 1 else 0),
        }


def _f32(a: Any) -> np.ndarray:
    return np.asarray(a, dtype=np.float32)


# ============================================================================== area map

AreaStat = Literal["observed", "mean", "median", "sd", "exceedance"]

_AREA_TITLES = {
    "observed": "Observed",
    "mean": "Mean",
    "median": "Median",
    "sd": "SD",
}


class AreaMapSettings(RecipeSettings):
    var: str | None = Field(None, description="Per-area output (default: one with the area dim)")
    group: GroupName = Field(
        "auto", description="Group to read (auto: posterior, predictions, posterior_predictive)"
    )
    observed: str | None = Field(
        None, description="Observed variable for an extra panel (default: var in observed_data)"
    )
    stats: list[AreaStat] | None = Field(
        None,
        description="Panels: observed, mean, median, sd, exceedance (default: observed when "
        "there is one, mean, sd and exceedance)",
    )
    threshold: float | None = Field(
        None, description="Exceedance threshold (default: 1 for a positive output, else 0)"
    )
    mark_above: float = Field(
        0.95, gt=0, lt=1, description="Mark areas where P(value > threshold) is above this"
    )
    log_scale: bool = Field(False, description="Colour the value panels by log10 of the value")
    show_edges: bool = Field(False, description="Draw lines between neighbours (adjacency)")
    select: dict[str, str | int | float] | None = None
    colormap: Colormap = "viridis"
    classes: int = Field(9, ge=3, le=10, description="Colour classes of the area fills")
    max_draws: int = Field(2000, ge=1, le=20_000)
    max_vertices: int = Field(
        40_000, ge=200, le=ROW_BUDGET, description="Polygon points per panel before thinning"
    )
    columns: int = Field(2, ge=1, le=5)
    width: int = Field(760, ge=80, le=2000)
    height: int = Field(340, ge=60, le=2000, description="Height of one row of panels")

    @field_validator("stats")
    @classmethod
    def _stats(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and not v:
            raise ValueError("give at least one panel")
        return list(dict.fromkeys(v)) if v else v


@dataclass(slots=True)
class _Geometry:
    dim: str
    labels: list[str]
    rings: list[list[tuple[np.ndarray, np.ndarray]]]  # per area: (x, y) of each ring
    ref: xr.DataArray
    n_points: int


def _geometry(inp: RecipeInput, first: str | None) -> _Geometry:
    rx, ry = _aux(inp, "ring_x", first), _aux(inp, "ring_y", first)
    if rx is None or ry is None:
        raise RecipeDataError(
            "an area map needs polygons: ring_x(area, vertex) and ring_y(area, vertex) in "
            "observed_data, constant_data, a geometry group or the root"
        )
    if rx.ndim != 2 or rx.dims != ry.dims:
        raise RecipeDataError("ring_x and ring_y need the same two dims (area, vertex)")
    vdim = "vertex" if "vertex" in rx.dims else str(rx.dims[1])
    adim = next(str(d) for d in rx.dims if d != vdim)
    xs = np.asarray(rx.transpose(adim, vdim).values, dtype=float)
    ys = np.asarray(ry.transpose(adim, vdim).values, dtype=float)
    rings: list[list[tuple[np.ndarray, np.ndarray]]] = []
    n_points = 0
    for x, y in zip(xs, ys, strict=True):
        ok = np.isfinite(x) & np.isfinite(y)
        parts: list[tuple[np.ndarray, np.ndarray]] = []
        start = None
        for i, good in enumerate([*ok, False]):
            if good and start is None:
                start = i
            elif not good and start is not None:
                if i - start >= 3:
                    parts.append((x[start:i], y[start:i]))
                    n_points += i - start + 1
                start = None
        rings.append(parts)
    if not n_points:
        raise RecipeDataError("the polygons have no ring with three or more points")
    labels = [_label(v) for v in coord_values(rx, adim)]
    return _Geometry(adim, labels, rings, rx, n_points)


def _centroid(parts: list[tuple[np.ndarray, np.ndarray]]) -> tuple[float, float]:
    """Area-weighted centroid of the rings (the mean of the points for degenerate rings)."""
    tot, cx, cy = 0.0, 0.0, 0.0
    for x, y in parts:
        x1, y1 = np.roll(x, -1), np.roll(y, -1)
        cross = x * y1 - x1 * y
        a = cross.sum() / 2
        if abs(a) > 0:
            tot += abs(a)
            cx += abs(a) * float(((x + x1) * cross).sum() / (6 * a))
            cy += abs(a) * float(((y + y1) * cross).sum() / (6 * a))
    if tot > 0:
        return cx / tot, cy / tot
    if not parts:
        return math.nan, math.nan
    return float(np.mean(np.concatenate([p[0] for p in parts]))), float(
        np.mean(np.concatenate([p[1] for p in parts]))
    )


def _thin_ring(x: np.ndarray, y: np.ndarray, step: int) -> tuple[np.ndarray, np.ndarray]:
    if step <= 1 or len(x) <= 4:
        return x, y
    m = max(4, math.ceil(len(x) / step))
    idx = np.unique(np.linspace(0, len(x) - 1, m).round().astype(int))
    return x[idx], y[idx]


def _paths(geo: _Geometry, areas: Sequence[int], step: int) -> tuple[np.ndarray, np.ndarray]:
    """NaN-separated ring coordinates of ``areas`` (one closed shape per ring)."""
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    gap = np.array([np.nan])
    for a in areas:
        for x, y in geo.rings[a]:
            tx, ty = _thin_ring(x, y, step)
            xs += [tx, gap]
            ys += [ty, gap]
    if not xs:
        return np.array([]), np.array([])
    return np.concatenate(xs), np.concatenate(ys)


def _area_values(
    inp: RecipeInput, s: AreaMapSettings, geo: _Geometry
) -> tuple[str, str, np.ndarray | None, dict[str, np.ndarray]]:
    """``(var, group, draws (sample, area) or None, summary statistics)`` on the geometry's
    area order."""
    try:
        da, group = _find_draws(
            inp, s.var, s.group, FIELD_GROUPS, need=[geo.dim], prefer_grid=False
        )
    except RecipeDataError:
        if s.var is None:
            raise
        found = _summary_var(inp, s.var, FIELD_GROUPS)
        if found is None:
            raise
        da, group = found
    var = str(da.name)
    if geo.dim not in da.dims:
        raise RecipeDataError(
            f"{var!r} has no dim {geo.dim!r} (the area dim of the polygons; dims: "
            f"{', '.join(map(str, da.dims))})"
        )
    if sample_dims(da):
        st = _only(stack_samples(_select(da, s.select)), [geo.dim])
        st = _thin(st.transpose(SAMPLE, geo.dim), s.max_draws)
        st = _align(st, geo.dim, geo.ref, geo.dim)
        draws = np.asarray(st.values, dtype=float)
        return var, group, draws, _nan_stats(draws)
    da = _select(da, s.select)
    stats: dict[str, np.ndarray] = {}
    if "stat" in da.dims:
        names = [str(v) for v in coord_values(da, "stat")]
        for key, aliases in (
            ("mean", ("mean",)),
            ("median", ("median", "q50", "q0.5")),
            ("sd", ("sd", "std")),
        ):
            hit = next((a for a in aliases if a in names), None)
            if hit is not None:
                one = _only(da.sel(stat=hit, drop=True), [geo.dim])
                stats[key] = np.asarray(_align(one, geo.dim, geo.ref, geo.dim).values, float)
        if not stats:
            raise RecipeDataError(f"the summary of {var!r} has no mean, median or sd")
    else:
        one = _only(da, [geo.dim])
        stats["mean"] = np.asarray(_align(one, geo.dim, geo.ref, geo.dim).values, float)
    return var, group, None, stats


OBSERVED_ATTR = "mapi_observed"
"""Attribute of a plotted variable that names its observed counterpart (a model that keeps
the raw estimate under another name, for example ``smr`` for ``rr``, sets it, because a
spec cannot give settings to a plot)."""


def _observed_hint(inp: RecipeInput, var: str, group: str) -> str | None:
    ds = inp.root() if group == "root" else inp.find(group)
    if ds is None or var not in ds.data_vars:
        return None
    hint = ds[var].attrs.get(OBSERVED_ATTR)
    return hint if isinstance(hint, str) and hint else None


def _area_observed(
    inp: RecipeInput, s: AreaMapSettings, var: str, geo: _Geometry, hint: str | None = None
) -> tuple[str, np.ndarray] | None:
    name = s.observed or var
    obs = None
    for g in OBS_GROUPS:
        ds = inp.find(g)
        if ds is not None and name in ds.data_vars:
            obs = ds[name]
            break
    if obs is None and s.observed is None and hint is not None:
        obs = _aux(inp, hint)
        if obs is not None:
            name = hint
    if obs is None:
        if s.observed is not None:
            obs = _aux(inp, s.observed)
            if obs is None:
                raise RecipeDataError(f"no observed variable {s.observed!r}")
        else:
            return None
    if geo.dim not in obs.dims:
        if s.observed is not None:
            raise RecipeDataError(f"the observed {name!r} has no dim {geo.dim!r}")
        return None
    sel = {d: v for d, v in (s.select or {}).items() if d in obs.dims}
    one = _only(_select(obs, sel), [geo.dim])
    return name, np.asarray(_align(one, geo.dim, geo.ref, geo.dim).values, dtype=float)


def _edges(
    inp: RecipeInput, geo: _Geometry, cx: np.ndarray, cy: np.ndarray, first: str | None
) -> tuple[np.ndarray, np.ndarray, int]:
    adj = _aux(inp, "adjacency", first)
    if adj is None:
        raise RecipeDataError("show_edges needs adjacency(area, area_) next to the polygons")
    n = len(geo.rings)
    if adj.ndim != 2 or adj.shape != (n, n):
        raise RecipeDataError(f"adjacency must be a {n} x {n} matrix over the areas")
    a = np.nan_to_num(np.asarray(adj.values, dtype=float))
    a = (a != 0) | (a.T != 0)
    i, j = np.nonzero(np.triu(a, 1))
    gap = np.full(len(i), np.nan)
    ex = np.stack([cx[i], cx[j], gap], axis=1).ravel()
    ey = np.stack([cy[i], cy[j], gap], axis=1).ravel()
    return ex, ey, len(i)


def _class_colours(scale: list[list[Any]], k: int) -> list[str]:
    import plotly.colors as pc

    return [str(c) for c in pc.sample_colorscale(scale, [(i + 0.5) / k for i in range(k)])]


def _classes(values: np.ndarray, lo: float, hi: float, k: int) -> np.ndarray:
    """Class of each value in ``k`` equal steps over ``[lo, hi]`` (-1 for NaN)."""
    span = hi - lo if hi > lo else 1.0
    with np.errstate(invalid="ignore"):
        c = np.clip(np.floor((values - lo) / span * k), 0, k - 1)
    c[~np.isfinite(values)] = -1
    return c.astype(int)


@recipe(
    "sci.area_map",
    title="Area map",
    settings=AreaMapSettings,
    persona="both",
    summary="A map of areas coloured by the model's estimate, its uncertainty and the "
    "probability that it is above a threshold, next to the observed values.",
    tags=["sci", "map"],
)
def area_map(inp: RecipeInput, s: AreaMapSettings) -> Figure:
    """Choropleth panels (observed, mean, sd, exceedance) over polygons on cartesian axes."""
    first = s.group if s.group != "auto" else None
    geo = _geometry(inp, first)
    var, group, draws, stats = _area_values(inp, s, geo)
    observed = _area_observed(inp, s, var, geo, _observed_hint(inp, var, group))
    ref_vals = draws if draws is not None else stats.get("mean")
    positive = (
        ref_vals is not None
        and bool(np.isfinite(ref_vals).any())
        and bool(np.nanmin(ref_vals) >= 0)
    )
    threshold = s.threshold if s.threshold is not None else (1.0 if positive else 0.0)
    if draws is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            seen = np.isfinite(draws)
            stats["exceedance"] = np.where(
                seen.any(0), (draws > threshold).sum(0) / np.maximum(seen.sum(0), 1), np.nan
            )
    if observed is not None:
        stats["observed"] = observed[1]
    available = [k for k in ("observed", "mean", "median", "sd", "exceedance") if k in stats]
    panels: list[str]
    if s.stats is None:
        panels = [k for k in ("observed", "mean", "sd", "exceedance") if k in stats]
    else:
        missing = [k for k in s.stats if k not in stats]
        if missing:
            raise RecipeDataError(
                f"cannot draw {', '.join(missing)} for {var!r} (available: "
                f"{', '.join(available)}); exceedance and sd need draws, observed needs the "
                "observed values"
            )
        panels = list(s.stats)
    n_areas = len(geo.rings)
    cent = np.array([_centroid(r) for r in geo.rings], dtype=float).reshape(n_areas, 2)
    cx, cy = cent[:, 0], cent[:, 1]

    def shown(key: str) -> np.ndarray:
        v = stats[key]
        if s.log_scale and key in ("observed", "mean", "median"):
            with np.errstate(all="ignore"):
                return np.where(v > 0, np.log10(np.where(v > 0, v, 1.0)), np.nan)
        return v

    value_keys = [k for k in panels if k in ("observed", "mean", "median")]
    ranges: dict[str, tuple[float, float] | None] = {
        "value": _range([shown(k) for k in value_keys]),
        "sd": _range([stats["sd"]]) if "sd" in panels else None,
        "exceedance": (0.0, 1.0),
    }
    sd_map: Colormap = "magma" if s.colormap != "magma" else "viridis"
    scales = {
        "value": _scale(s.colormap),
        "sd": _scale(sd_map),
        "exceedance": _scale("diverging"),
    }
    # rows: every panel repeats the polygons, so thin the rings to fit the sandbox
    budget = min(s.max_vertices, (ROW_BUDGET - 4 * n_areas) // max(1, len(panels)))
    step = max(1, math.ceil(geo.n_points / max(budget, 1)))
    doms = _domains(len(panels), s.columns, hgap=0.12, vgap=0.12)
    cols = max(1, min(s.columns, len(panels)))
    rows = math.ceil(len(panels) / cols)
    layout: dict[str, Any] = {
        "height": rows * s.height + 40,
        "width": s.width,
        "margin": {"l": 16, "r": 56, "t": 36, "b": 16},
        "showlegend": False,
        "hovermode": "closest",
        "annotations": [],
    }
    datasets: dict[str, pd.DataFrame] = {}
    traces: list[dict[str, Any]] = []
    hover = {"x": _f32(cx), "y": _f32(cy), "label": geo.labels}
    for key in panels:
        hover[key] = _f32(stats[key])
    datasets["areas"] = pd.DataFrame(hover)
    edge_count = 0
    if s.show_edges:
        ex, ey, edge_count = _edges(inp, geo, cx, cy, first)
        datasets["edges"] = pd.DataFrame({"x": _f32(ex), "y": _f32(ey)})
    marked = np.zeros(n_areas, dtype=bool)
    if "exceedance" in stats:
        marked = np.nan_to_num(stats["exceedance"]) > s.mark_above
        datasets["marks"] = pd.DataFrame({"x": _f32(cx[marked]), "y": _f32(cy[marked])})
    for p, (key, (dx, dy)) in enumerate(zip(panels, doms, strict=True)):
        aid = _axis_id(p)
        kind = "value" if key in ("observed", "mean", "median") else key
        rng = ranges[kind] or (0.0, 1.0)
        values = shown(key)
        cls = _classes(values, rng[0], rng[1], s.classes)
        colours = _class_colours(scales[kind], s.classes)
        for c in range(-1, s.classes):
            members = [int(a) for a in np.nonzero(cls == c)[0]]
            if not members:
                continue
            px, py = _paths(geo, members, step)
            if not len(px):
                continue
            name = f"p{p}c{c + 1}"
            datasets[name] = pd.DataFrame({"x": _f32(px), "y": _f32(py)})
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": {"$ref": f"{name}.x"},
                    "y": {"$ref": f"{name}.y"},
                    "xaxis": f"x{aid}",
                    "yaxis": f"y{aid}",
                    "fill": "toself",
                    "fillcolor": colours[c] if c >= 0 else EMPTY,
                    "line": {"color": SURFACE, "width": 0.6},
                    "hoverinfo": "skip",
                    "showlegend": False,
                }
            )
        if "edges" in datasets:
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": {"$ref": "edges.x"},
                    "y": {"$ref": "edges.y"},
                    "xaxis": f"x{aid}",
                    "yaxis": f"y{aid}",
                    "line": {"color": REFERENCE, "width": 1},
                    "opacity": 0.7,
                    "hoverinfo": "skip",
                    "showlegend": False,
                }
            )
        title = f"P({var} > {threshold:g})" if key == "exceedance" else _AREA_TITLES[key]
        if key == "observed" and observed is not None and observed[0] != var:
            title = f"Observed {observed[0]}"
        log_note = " (log10)" if s.log_scale and kind == "value" else ""
        hover_value = "%{customdata:.4g}" if log_note else "%{marker.color:.4g}"
        marker: dict[str, Any] = {
            "color": {"$ref": f"areas.{key}_log" if log_note else f"areas.{key}"},
            "colorscale": scales[kind],
            "cmin": rng[0],
            "cmax": rng[1],
            "size": 7,
            "opacity": 0,
            "showscale": True,
            "colorbar": {
                "x": dx[1] + 0.01,
                "xanchor": "left",
                "y": (dy[0] + dy[1]) / 2,
                "yanchor": "middle",
                "len": dy[1] - dy[0],
                "thickness": 10,
                "outlinewidth": 0,
            },
        }
        trace: dict[str, Any] = {
            "type": "scatter",
            "mode": "markers",
            "x": {"$ref": "areas.x"},
            "y": {"$ref": "areas.y"},
            "text": {"$ref": "areas.label"},
            "xaxis": f"x{aid}",
            "yaxis": f"y{aid}",
            "marker": marker,
            "hovertemplate": f"%{{text}}<br>{title}: {hover_value}<extra></extra>",
            "showlegend": False,
        }
        if log_note:
            datasets["areas"][f"{key}_log"] = values.astype(np.float32)
            trace["customdata"] = {"$ref": f"areas.{key}"}
        traces.append(trace)
        if key == "exceedance" and marked.any():
            traces.append(
                {
                    "type": "scatter",
                    "mode": "markers",
                    "x": {"$ref": "marks.x"},
                    "y": {"$ref": "marks.y"},
                    "xaxis": f"x{aid}",
                    "yaxis": f"y{aid}",
                    "marker": {
                        "symbol": "cross",
                        "size": 9,
                        "color": STRONG,
                        "line": {"color": SURFACE, "width": 1},
                    },
                    "hoverinfo": "skip",
                    "showlegend": False,
                }
            )
        axis = {"visible": False, "showgrid": False, "zeroline": False}
        layout[f"xaxis{aid}"] = {**axis, "domain": dx, "anchor": f"y{aid}", "constrain": "domain"}
        layout[f"yaxis{aid}"] = {
            **axis,
            "domain": dy,
            "anchor": f"x{aid}",
            "scaleanchor": f"x{aid}",
            "scaleratio": 1,
            "constrain": "domain",
        }
        layout["annotations"].append(
            {
                "text": f"<b>{title}</b>{log_note}",
                "xref": "paper",
                "yref": "paper",
                "x": (dx[0] + dx[1]) / 2,
                "y": dy[1],
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
                "font": {"size": 12},
            }
        )
    what = "the posterior" if group == "posterior" else "the predictions"
    caption = f"Each panel colours the {n_areas} areas by one value of {var}"
    caption += f", from {draws.shape[0]} draws of {what}." if draws is not None else "."
    parts = {
        "observed": " The observed panel shows the data, before the model smooths it.",
        "mean": " The mean panel shows the average estimate.",
        "median": " The median panel shows the middle estimate.",
        "sd": " The sd panel shows the uncertainty. A high sd shows where the data do not fix "
        "the value.",
        "exceedance": f" The probability panel shows how likely it is that {var} is above "
        f"{threshold:g} in each area. A cross marks each area where this probability is "
        f"above {pct(s.mark_above)} ({int(marked.sum())} of {n_areas}).",
    }
    for key in panels:
        caption += parts[key]
    if s.log_scale and value_keys:
        caption += " The value panels use log10 colours."
    if edge_count:
        caption += f" The grey lines join the {edge_count} pairs of neighbours."
    if "observed" in panels and draws is not None:
        caption += " Small areas with few data often have extreme observed values; the model "
        caption += "pulls them towards their neighbours or the average."
    if step > 1:
        caption += f" To keep the plot small, the outlines use 1 point in {step}."
    caption += " Grey areas have no value."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets=datasets,
        caption=caption,
        title=f"Area map of {var}",
    )


# ============================================================================= phylogeny


class PhylogenySettings(RecipeSettings):
    var: str | None = Field(None, description="Per-tip output (default: one with a tip dim)")
    group: GroupName = Field(
        "auto", description="Group to read (auto: predictions, posterior_predictive, posterior)"
    )
    tip_dim: str | None = Field(None, description="Dim of the tips (default: found by size)")
    observed: str | None = Field(None, description="Observed trait (default: var in observed_data)")
    interval: float = Field(0.9, gt=0, lt=1, description="Width of the interval in the strip")
    point: Literal["mean", "median"] = "mean"
    max_tips: int = Field(400, ge=2, le=5000, description="Tips shown (evenly spaced)")
    max_draws: int = Field(2000, ge=1, le=20_000)
    labels: bool | None = Field(None, description="Tip labels (default: up to 80 tips)")
    select: dict[str, str | int | float] | None = None
    colormap: Colormap = "viridis"
    width: int = Field(780, ge=80, le=2000)
    height: int = Field(0, ge=0, le=4000, description="0: from the number of tips")


@dataclass(slots=True)
class _Tree:
    parent: np.ndarray  # parent position, -1 for a root
    length: np.ndarray
    tips: np.ndarray  # node positions of the tips, in node order
    labels: list[str]  # per node
    dim: str
    named: bool = True  # False when the labels are positions made up for a dim without coords


def _tree(inp: RecipeInput, first: str | None) -> _Tree:
    par = _aux(inp, "parent", first)
    if par is None:
        raise RecipeDataError(
            "a phylogeny needs parent(node) (and branch_length(node)) in observed_data, "
            "constant_data, a tree group or the root"
        )
    if par.ndim != 1:
        raise RecipeDataError("parent must have one dim (node)")
    dim = str(par.dims[0])
    n = par.sizes[dim]
    node_labels = [_label(v) for v in coord_values(par, dim)]
    raw = np.asarray(par.values)
    if raw.dtype.kind in "OUS":
        pos = {lab: i for i, lab in enumerate(node_labels)}
        parent = np.array([pos.get(str(v), -1) for v in raw], dtype=int)
    else:
        f = np.asarray(raw, dtype=float)
        parent = np.where(np.isfinite(f), f, -1).astype(int)
    parent[(parent < 0) | (parent >= n) | (parent == np.arange(n))] = -1
    bl = _aux(inp, "branch_length", first)
    length = np.nan_to_num(np.asarray(bl.values, dtype=float)) if bl is not None else np.ones(n)
    if length.shape != (n,):
        raise RecipeDataError("branch_length must have the same dim as parent")
    tip_flag = _aux(inp, "is_tip", first)
    if tip_flag is not None:
        tips = np.nonzero(np.asarray(tip_flag.values).astype(bool))[0]
    else:
        tips = np.setdiff1d(np.arange(n), parent[parent >= 0])
    lab = _aux(inp, "tip_label", first)
    labels = [str(v) for v in lab.values] if lab is not None else node_labels
    named = lab is not None or dim in par.coords
    # a cycle would hang the layout: every node must reach a root
    done = parent < 0
    for v in range(n):
        path: list[int] = []
        seen: set[int] = set()
        u = v
        while not done[u]:
            if u in seen:
                raise RecipeDataError("parent has a cycle: it is not a tree")
            path.append(u)
            seen.add(u)
            u = int(parent[u])
        done[path] = True
    if not len(tips):
        raise RecipeDataError("the tree has no tips")
    return _Tree(parent, length, tips, labels, dim, named)


def _align_tips(mine: list[str] | None, n: int, tree: _Tree) -> list[int] | None:
    """Per tip of ``tree``, the position in a dim of ``n`` levels (-1 for a missing tip),
    or None when the two cannot be lined up.

    Labels match only when the tree has real labels and every tip label is a level, or
    when the lengths differ and most tips are found. A few labels shared by chance (two
    0-based position dims) do not count: the dims then line up by position."""
    tip_labels = [tree.labels[t] for t in tree.tips]
    if mine is not None and tree.named:
        pos = {v: i for i, v in enumerate(mine)}
        found = sum(t in pos for t in tip_labels)
        if found == len(tip_labels) or (n != len(tip_labels) and 2 * found > len(tip_labels)):
            return [pos.get(t, -1) for t in tip_labels]
    if n == len(tip_labels):
        return list(range(n))
    return None


def _tip_draws(
    inp: RecipeInput, s: PhylogenySettings, tree: _Tree
) -> tuple[xr.DataArray, str, str]:
    """``(draws (sample, tip), var, tip dim)``: the per-tip output, on the tree's tip order."""
    n_tips = len(tree.tips)
    if s.var is not None or s.tip_dim is not None:
        da, group = _find_draws(
            inp,
            s.var,
            s.group,
            CURVE_GROUPS,
            need=[s.tip_dim] if s.tip_dim else (),
            prefer_grid=False,
        )
    else:
        hit = None
        for g in CURVE_GROUPS if s.group == "auto" else (s.group,):
            ds = inp.find(g)
            if ds is None:
                continue
            for v in ds.data_vars:
                d = ds[v]
                if sample_dims(d) and any(
                    d.sizes[x] == n_tips for x in d.dims if x not in sample_dims(d)
                ):
                    hit = (d, g)
                    break
            if hit:
                break
        if hit is None:
            raise RecipeDataError(
                f"no variable with draws has a dim of {n_tips} levels (one per tip): pass var="
            )
        da, group = hit
    sd = sample_dims(da)
    dims = [str(d) for d in da.dims if d not in sd and d not in (s.select or {})]
    tip_dim = s.tip_dim or next(
        (d for d in dims if da.sizes[d] == n_tips), dims[0] if len(dims) == 1 else None
    )
    if tip_dim is None or tip_dim not in dims:
        raise RecipeDataError(f"pass tip_dim= (dims of {da.name!r}: {', '.join(dims)})")
    st = _only(stack_samples(_select(da, s.select)), [tip_dim])
    st = _thin(st.transpose(SAMPLE, tip_dim), s.max_draws)
    mine = [_label(v) for v in coord_values(st, tip_dim)] if tip_dim in st.coords else None
    idx = _align_tips(mine, st.sizes[tip_dim], tree)
    if idx is not None:
        vals = np.asarray(st.values, dtype=float)[:, [max(i, 0) for i in idx]]
        vals[:, [i < 0 for i in idx]] = np.nan
    else:
        raise RecipeDataError(
            f"{da.name!r} has {st.sizes[tip_dim]} levels of {tip_dim!r} but the tree has "
            f"{n_tips} tips and too few labels in common"
        )
    out = xr.DataArray(vals, dims=[SAMPLE, "tip"], name=str(da.name))
    return out, group, tip_dim


def _tip_observed(
    inp: RecipeInput, s: PhylogenySettings, var: str, tip_dim: str, tree: _Tree
) -> tuple[str, np.ndarray] | None:
    name = s.observed or var
    obs = None
    for g in OBS_GROUPS:
        ds = inp.find(g)
        if ds is not None and name in ds.data_vars:
            obs = ds[name]
            break
    if obs is None and s.observed is not None:
        obs = _aux(inp, s.observed)
        if obs is None:
            raise RecipeDataError(f"no observed variable {s.observed!r}")
    if obs is None:
        return None
    sel = {d: v for d, v in (s.select or {}).items() if d in obs.dims}
    obs = _select(obs, sel)
    if tip_dim not in obs.dims:
        if s.observed is not None:
            raise RecipeDataError(f"the observed {name!r} has no dim {tip_dim!r}")
        return None
    obs = _only(obs, [tip_dim])
    vals = np.asarray(obs.values, dtype=float)
    mine = [_label(v) for v in obs[tip_dim].values] if tip_dim in obs.coords else None
    idx = _align_tips(mine, len(vals), tree)
    if idx is None:
        return None
    return name, np.array([vals[i] if i >= 0 else np.nan for i in idx])


def _layout_tree(
    tree: _Tree, keep_tips: np.ndarray
) -> tuple[np.ndarray, np.ndarray, list[int], np.ndarray, np.ndarray]:
    """``(x, y, tip order, segment x, segment y)``: a rectangular layout of the subtree
    spanned by ``keep_tips``. ``x`` is the distance from the root, ``y`` the tip rank."""
    n = len(tree.parent)
    keep = np.zeros(n, dtype=bool)
    for t in keep_tips:
        u = int(t)
        while u >= 0 and not keep[u]:
            keep[u] = True
            u = int(tree.parent[u])
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for node in np.nonzero(keep)[0].tolist():
        p = int(tree.parent[node])
        if p >= 0:
            children.setdefault(p, []).append(node)
        else:
            roots.append(node)
    x = np.full(n, np.nan)
    y = np.full(n, np.nan)
    order: list[int] = []
    stack = [(r, 0.0) for r in reversed(roots)]
    post: list[int] = []
    while stack:
        v, base = stack.pop()
        x[v] = base
        post.append(v)
        kids = children.get(v, [])
        if not kids:
            y[v] = len(order)
            order.append(v)
        for c in reversed(kids):
            stack.append((c, base + float(tree.length[c])))
    for v in reversed(post):  # children before parents
        kids = children.get(v, [])
        if kids:
            ys = y[kids]
            y[v] = (np.nanmin(ys) + np.nanmax(ys)) / 2
    sx: list[float] = []
    sy: list[float] = []
    nan = math.nan
    for v in post:
        p = int(tree.parent[v])
        if p >= 0:
            sx += [x[p], x[v], nan]
            sy += [y[v], y[v], nan]
        kids = children.get(v, [])
        if len(kids) > 1:
            ys = y[kids]
            sx += [x[v], x[v], nan]
            sy += [float(np.nanmin(ys)), float(np.nanmax(ys)), nan]
    if len(roots) > 1:  # a forest: join the roots at a common origin
        ry = y[roots]
        sx += [0.0, 0.0, nan]
        sy += [float(np.nanmin(ry)), float(np.nanmax(ry)), nan]
    return x, y, order, np.array(sx), np.array(sy)


@recipe(
    "sci.phylogeny",
    title="Phylogeny with tip estimates",
    settings=PhylogenySettings,
    persona="both",
    summary="A tree of related species with the model's estimate for each tip, its interval "
    "and the observed trait, so that close relatives sit side by side.",
    tags=["sci", "graph"],
)
def phylogeny(inp: RecipeInput, s: PhylogenySettings) -> Figure:
    """Rectangular dendrogram from parent / branch-length arrays plus a strip of tip estimates."""
    first = s.group if s.group != "auto" else None
    tree = _tree(inp, first)
    draws_da, group, tip_dim = _tip_draws(inp, s, tree)
    var = str(draws_da.name)
    draws = np.asarray(draws_da.values, dtype=float)
    observed = _tip_observed(inp, s, var, tip_dim, tree)
    n_tips = len(tree.tips)
    # tips in tree order (a first pass over all tips), then evenly spaced ones if too many
    _, _, full_order, _, _ = _layout_tree(tree, tree.tips)
    if n_tips > s.max_tips:
        pick = np.unique(np.linspace(0, n_tips - 1, s.max_tips).round().astype(int))
        keep_nodes = np.array([full_order[i] for i in pick])
    else:
        keep_nodes = np.array(full_order)
    x, y, order, sx, sy = _layout_tree(tree, keep_nodes)
    tip_pos = {int(t): i for i, t in enumerate(tree.tips)}  # node -> column of draws
    cols = [tip_pos[v] for v in order]
    d = draws[:, cols]
    lo_q, hi_q = (1 - s.interval) / 2, (1 + s.interval) / 2
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        point = np.nanmean(d, 0) if s.point == "mean" else np.nanmedian(d, 0)
        lo = np.nanquantile(d, lo_q, 0)
        hi = np.nanquantile(d, hi_q, 0)
    labels = [tree.labels[v] for v in order]
    tips: dict[str, Any] = {
        "x": _f32(x[order]),
        "y": _f32(y[order]),
        "label": labels,
        "point": _f32(point),
        "lo": _f32(lo),
        "hi": _f32(hi),
        "plus": _f32(hi - point),
        "minus": _f32(point - lo),
    }
    covered = None
    if observed is not None:
        ob = observed[1][cols]
        tips["observed"] = _f32(ob)
        seen = np.isfinite(ob) & np.isfinite(lo)
        if seen.any():
            covered = float(((ob >= lo) & (ob <= hi))[seen].mean())
    datasets = {
        "tree": pd.DataFrame({"x": _f32(sx), "y": _f32(sy)}),
        "tips": pd.DataFrame(tips),
    }
    rng = _range([point])
    scale = _scale(s.colormap)
    shown = len(order)
    show_labels = s.labels if s.labels is not None else shown <= 80
    height = s.height or int(min(1800, max(360, 14 * shown + 120)))
    traces: list[dict[str, Any]] = [
        {
            "type": "scatter",
            "mode": "lines",
            "x": {"$ref": "tree.x"},
            "y": {"$ref": "tree.y"},
            "line": {"color": REFERENCE, "width": 1.2},
            "hoverinfo": "skip",
            "showlegend": False,
        },
        {
            "type": "scatter",
            "mode": "markers",
            "x": {"$ref": "tips.x"},
            "y": {"$ref": "tips.y"},
            "text": {"$ref": "tips.label"},
            "marker": {
                "color": {"$ref": "tips.point"},
                "colorscale": scale,
                **({"cmin": rng[0], "cmax": rng[1]} if rng else {}),
                "size": 7 if shown <= 150 else 4,
                "line": {"color": SURFACE, "width": 0.5},
            },
            "hovertemplate": f"%{{text}}<br>{s.point} {var}: %{{marker.color:.4g}}<extra></extra>",
            "showlegend": False,
        },
        {
            "type": "scatter",
            "mode": "markers",
            "x": {"$ref": "tips.point"},
            "y": {"$ref": "tips.y"},
            "text": {"$ref": "tips.label"},
            "xaxis": "x2",
            "yaxis": "y",
            "error_x": {
                "type": "data",
                "symmetric": False,
                "array": {"$ref": "tips.plus"},
                "arrayminus": {"$ref": "tips.minus"},
                "color": BAND,
                "thickness": 1.5,
                "width": 0,
            },
            "marker": {"color": ESTIMATE, "size": 5},
            "name": s.point,
            "hovertemplate": (f"%{{text}}<br>{s.point} %{{x:.4g}}<extra></extra>"),
            "showlegend": observed is not None,
        },
    ]
    if observed is not None:
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "x": {"$ref": "tips.observed"},
                "y": {"$ref": "tips.y"},
                "text": {"$ref": "tips.label"},
                "xaxis": "x2",
                "yaxis": "y",
                "marker": {
                    "color": OBSERVED,
                    "size": 6,
                    "symbol": "circle-open",
                    "line": {"width": 1.5},
                },
                "name": "observed",
                "hovertemplate": "%{text}<br>observed %{x:.4g}<extra></extra>",
                "showlegend": True,
            }
        )
    yaxis: dict[str, Any] = {
        "autorange": "reversed",
        "showgrid": False,
        "zeroline": False,
        "anchor": "x",
        "side": "right",
        "domain": [0.0, 1.0],
    }
    if show_labels:
        yaxis.update(
            {
                "tickmode": "array",
                "tickvals": {"$ref": "tips.y"},
                "ticktext": {"$ref": "tips.label"},
                "tickfont": {"size": 9 if shown > 40 else 11},
                "ticks": "",
            }
        )
    else:
        yaxis["showticklabels"] = False
    layout: dict[str, Any] = {
        "height": height,
        "width": s.width,
        "margin": {"l": 16, "r": 16, "t": 24, "b": 48},
        "hovermode": "closest",
        "xaxis": {
            "domain": [0.0, 0.5],
            "anchor": "y",
            "title": {"text": "distance from the root"},
            "showgrid": False,
            "zeroline": False,
        },
        "xaxis2": {
            "domain": [0.72, 1.0],
            "anchor": "y",
            "title": {"text": f"{var} ({s.point}, {pct(s.interval)} interval)"},
            "zeroline": False,
        },
        "yaxis": yaxis,
        "legend": {"orientation": "h", "x": 1, "xanchor": "right", "y": 1.0, "yanchor": "bottom"},
    }
    what = "the posterior" if group == "posterior" else "the predictions"
    caption = (
        f"The tree shows how the {n_tips} tips are related: two tips that join near the right "
        "share a recent ancestor. The colour of each tip is the "
        f"{s.point} of {var}. The strip on the right shows the same {s.point} as a dot with "
        f"the {pct(s.interval)} interval as a line, from {d.shape[0]} draws of {what}."
    )
    if observed is not None:
        caption += " The open circles are the observed values."
        if covered is not None:
            caption += f" {pct(covered)} of them are inside the interval."
        missing = int(np.sum(~np.isfinite(tips["observed"])))
        if missing:
            caption += (
                f" {missing} tips have no observed value: their estimate comes from their "
                "relatives."
            )
    if shown < n_tips:
        caption += f" The plot shows {shown} of the {n_tips} tips, evenly spaced along the tree."
    caption += f" The tips are the levels of {inp.dim_label(tip_dim)}."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets=datasets,
        caption=caption,
        title=f"Phylogeny of {var}",
    )


# ================================================================================== rose

AngleUnits = Literal["radians", "degrees", "hours", "days"]
PERIODS: dict[str, float] = {
    "radians": 2 * math.pi,
    "degrees": 360.0,
    "hours": 24.0,
    "days": 365.25,
}
ANGLE_NAMES = ("angle", "direction", "theta", "bearing", "phase", "hour", "doy", "day_of_year")
_MONTH_STARTS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class RoseSettings(RecipeSettings):
    var: str | None = Field(
        None, description="Angles with draws, or a density over an angle dim (default: found)"
    )
    group: GroupName = Field(
        "auto", description="Group to read (auto: posterior_predictive, predictions, posterior)"
    )
    observed: str | None = Field(
        None, description="Observed angles (default: var in observed_data)"
    )
    angle_dim: str | None = Field(
        None, description="Dim along the circle: draws a density curve (default: found by name)"
    )
    units: AngleUnits = Field("radians", description="Units of the angles")
    period: float | None = Field(None, gt=0, description="Length of the circle (default: units)")
    bins: int = Field(24, ge=4, le=360)
    frame: str | None = Field(None, description="Dim with one chart per level (slider)")
    select: dict[str, str | int | float] | None = None
    interval: float = Field(0.9, gt=0, lt=1)
    scale: Literal["auto", "count", "share"] = Field(
        "auto", description="Bar heights (auto: share with a frame, else count)"
    )
    clockwise: bool | None = Field(
        None, description="Direction (default: clockwise from the top, except for radians)"
    )
    max_draws: int = Field(1000, ge=1, le=20_000)
    max_frames: int = Field(24, ge=1, le=60)
    width: int = Field(560, ge=80, le=2000)
    height: int = Field(520, ge=60, le=2000)


def _to_degrees(v: np.ndarray, period: float) -> np.ndarray:
    return np.mod(np.asarray(v, dtype=float), period) / period * 360.0


def _angular_axis(units: str, period: float, clockwise: bool) -> dict[str, Any]:
    axis: dict[str, Any] = {
        "direction": "clockwise" if clockwise else "counterclockwise",
        "rotation": 90 if clockwise else 0,
        "tickmode": "array",
    }
    std = abs(period - PERIODS[units]) < 1e-9
    if units == "hours" and std:
        axis["tickvals"] = [h * 15.0 for h in range(0, 24, 3)]
        axis["ticktext"] = [f"{h} h" for h in range(0, 24, 3)]
    elif units == "days" and std:
        axis["tickvals"] = [round(d / period * 360.0, 3) for d in _MONTH_STARTS]
        axis["ticktext"] = _MONTHS
    elif units == "radians" and std:
        axis["tickvals"] = [i * 45.0 for i in range(8)]
        axis["ticktext"] = ["0", "π/4", "π/2", "3π/4", "π", "5π/4", "3π/2", "7π/4"]
    else:
        axis["tickvals"] = [i * 45.0 for i in range(8)]
        axis["ticktext"] = [f"{period * i / 8:g}" for i in range(8)]
    return axis


def _rose_frames(da: xr.DataArray | None, frame: str | None, max_frames: int) -> list[int]:
    if da is None or frame is None:
        return [0]
    n = da.sizes[frame]
    if n <= max_frames:
        return list(range(n))
    return [int(i) for i in np.unique(np.linspace(0, n - 1, max_frames).round().astype(int))]


def _rose_data(
    inp: RecipeInput, s: RoseSettings
) -> tuple[xr.DataArray | None, str | None, xr.DataArray | None, str | None, str | None]:
    """``(draws, group, observed, observed name, angle dim)``: the plotted draws (stacked,
    ``None`` without any) and the observed angles (``None`` without any)."""
    order = ("posterior_predictive", "predictions", "posterior")
    draws: xr.DataArray | None = None
    group: str | None = None
    try:
        da, group = _find_draws(
            inp,
            s.var,
            s.group,
            order,
            need=[d for d in (s.angle_dim, s.frame) if d],
            prefer_grid=False,
        )
        draws = stack_samples(_select(da, s.select))
    except RecipeDataError:
        if s.var is not None and s.observed is None:
            obs_ds = inp.find(*OBS_GROUPS)
            if obs_ds is None or s.var not in obs_ds.data_vars:
                raise
    var = str(draws.name) if draws is not None else s.var
    angle_dim = s.angle_dim
    if draws is not None and angle_dim is None:
        dims = [str(d) for d in draws.dims if d != SAMPLE]
        angle_dim = next((d for d in dims if d.lower() in ANGLE_NAMES), None)
    name = s.observed or (var if angle_dim is None else None)
    obs: xr.DataArray | None = None
    if name is not None:
        for g in OBS_GROUPS:
            ds = inp.find(g)
            if ds is not None and name in ds.data_vars:
                obs = ds[name]
                break
        if obs is None and s.observed is not None:
            obs = _aux(inp, s.observed)
            if obs is None:
                raise RecipeDataError(f"no observed variable {s.observed!r}")
        if obs is not None:
            sel = {d: v for d, v in (s.select or {}).items() if d in obs.dims}
            obs = _select(obs, sel)
    if draws is None and obs is None:
        raise RecipeDataError(
            "a rose needs angles: draws of a variable (posterior_predictive, predictions or "
            "posterior) or observed angles (observed_data)"
        )
    return draws, group, obs, name if obs is not None else None, angle_dim


def _counts(values: np.ndarray, bins: int, period: float) -> np.ndarray:
    """Counts per bin of the last axis of ``values`` (NaN ignored)."""
    v = np.asarray(values, dtype=float)
    flat = v.reshape(-1, v.shape[-1]) if v.ndim > 1 else v[None]
    ok = np.isfinite(flat)
    idx = np.floor(np.mod(np.where(ok, flat, 0.0), period) / period * bins).astype(int)
    idx = np.clip(idx, 0, bins - 1) + np.arange(flat.shape[0])[:, None] * bins
    counts = np.bincount(idx[ok], minlength=flat.shape[0] * bins).reshape(flat.shape[0], bins)
    return counts.astype(float) if v.ndim > 1 else counts[0].astype(float)


def _step(values: np.ndarray) -> np.ndarray:
    """A closed step curve: each bin's value at its start and end edge."""
    return np.append(np.repeat(values, 2), values[0])


@recipe(
    "sci.rose",
    title="Rose of angles",
    settings=RoseSettings,
    persona="both",
    summary="A polar chart of directions or times of day: a rose of the observed angles with "
    "the model's range for each sector, or a curve around the circle with bands.",
    tags=["sci", "polar"],
)
def rose(inp: RecipeInput, s: RoseSettings) -> Figure:
    """Rose histogram (barpolar) with predictive bands, or a polar density with bands."""
    draws, _group, obs, obs_name, angle_dim = _rose_data(inp, s)
    period = s.period or PERIODS[s.units]
    clockwise = s.clockwise if s.clockwise is not None else s.units != "radians"
    frame = s.frame
    if obs is not None and frame is not None and frame not in obs.dims:
        if draws is None:
            raise RecipeDataError(f"the observed angles have no dim {frame!r}")
        obs = None  # one histogram cannot follow the slider
        obs_name = None
    levels = _rose_frames(draws if draws is not None else obs, frame, s.max_frames)
    ref = draws if draws is not None else obs
    assert ref is not None
    frame_names = (
        [_label(coord_values(ref, frame)[i]) for i in levels] if frame is not None else ["all"]
    )
    lo_q, hi_q = (1 - s.interval) / 2, (1 + s.interval) / 2
    width = 360.0 / s.bins
    centres = (np.arange(s.bins) + 0.5) * width
    edges = np.append(np.repeat(np.arange(s.bins + 1) * width, 2)[1:-1], 0.0)
    traces: list[dict[str, Any]] = []
    frame_traces: list[list[dict[str, Any]]] = [[] for _ in levels]
    datasets: dict[str, pd.DataFrame] = {}
    caption = ""
    density = draws is not None and angle_dim is not None
    share = s.scale == "share" or (s.scale == "auto" and frame is not None)
    unit_word = {"radians": "radian", "degrees": "degree", "hours": "hour", "days": "day"}
    covered: list[float] = []
    if density:
        assert draws is not None and angle_dim is not None
        if angle_dim not in draws.dims:
            raise RecipeDataError(f"{draws.name!r} has no dim {angle_dim!r}")
        st = _only(draws, [angle_dim, frame])
        st = _thin(st.transpose(SAMPLE, *(d for d in (frame, angle_dim) if d)), s.max_draws)
        if angle_dim in st.coords and np.issubdtype(st[angle_dim].dtype, np.number):
            ang = np.asarray(st[angle_dim].values, dtype=float)
        else:
            n = st.sizes[angle_dim]
            ang = np.arange(n) * period / n
        theta = _to_degrees(ang, period)
        srt = np.argsort(theta)
        theta = np.append(theta[srt], theta[srt][0])
        curve: dict[str, Any] = {"theta": _f32(theta)}
        bars: dict[str, Any] = {"theta": _f32(centres)}
        for k, lv in enumerate(levels):
            sub = st.isel({frame: lv}) if frame else st
            v = np.asarray(sub.values, dtype=float)[:, srt]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                for key, q in (("lo", lo_q), ("hi", hi_q), ("med", 0.5)):
                    c = np.nanquantile(v, q, 0)
                    curve[f"{key}__{k}"] = _f32(np.append(c, c[0]))
            if obs is not None:
                o = obs.isel({frame: lv}) if frame and frame in obs.dims else obs
                vals = np.asarray(o.values, dtype=float).ravel()
                vals = vals[np.isfinite(vals)]
                c = _counts(vals, s.bins, period)
                # observed density per unit of angle, comparable with the curve
                bars[f"obs__{k}"] = _f32(c / max(len(vals), 1) / (period / s.bins))
        datasets["curve"] = pd.DataFrame(curve)
        if obs is not None:
            datasets["bars"] = pd.DataFrame(bars)
            traces.append(
                {
                    "type": "barpolar",
                    "theta": {"$ref": "bars.theta"},
                    "r": {"$ref": "bars.obs__0"},
                    "width": width,
                    "marker": {"color": OBSERVED, "line": {"color": SURFACE, "width": 0.5}},
                    "opacity": 0.4,
                    "name": "observed",
                    "hovertemplate": "%{theta:.1f}°: %{r:.3g}<extra>observed</extra>",
                }
            )
            for k in range(len(levels)):
                frame_traces[k].append({"type": "barpolar", "r": {"$ref": f"bars.obs__{k}"}})
        band_name = "curve"
        var = str(draws.name)
        caption = (
            f"The curve is the median of {var} around the circle, from {st.sizes[SAMPLE]} "
            f"draws. The dotted lines are the limits of the {pct(s.interval)} interval."
        )
        if obs is not None:
            caption += (
                f" The bars are the observed {obs_name} as a density per {unit_word[s.units]}, "
                "so that they compare with the curve."
            )
    else:
        n_obs = 0
        bars = {"theta": _f32(centres)}
        steps: dict[str, Any] = {"theta": _f32(edges)}
        whiskers: dict[str, Any] = {"theta": _f32(np.repeat(centres, 3))}
        st = None
        if draws is not None:
            keep = [d for d in draws.dims if d != SAMPLE]
            st = _thin(draws.transpose(SAMPLE, *keep), s.max_draws)
        for k, lv in enumerate(levels):
            total = 1.0
            if obs is not None:
                o = obs.isel({frame: lv}) if frame else obs
                vals = np.asarray(o.values, dtype=float).ravel()
                vals = vals[np.isfinite(vals)]
                n_obs += len(vals)
                c = _counts(vals, s.bins, period)
                total = max(len(vals), 1) if share else 1.0
                bars[f"obs__{k}"] = _f32(c / total)
            if st is not None:
                sub = st.isel({frame: lv}) if frame else st
                v = np.asarray(sub.values, dtype=float).reshape(sub.sizes[SAMPLE], -1)
                c = _counts(v, s.bins, period)
                if share:
                    c = c / np.maximum(np.isfinite(v).sum(1, keepdims=True), 1)
                lo = np.quantile(c, lo_q, 0)
                hi = np.quantile(c, hi_q, 0)
                med = np.quantile(c, 0.5, 0)
                gap = np.full(s.bins, np.nan)
                whiskers[f"r__{k}"] = _f32(np.stack([lo, hi, gap], axis=1).ravel())
                steps[f"med__{k}"] = _f32(_step(med))
                if obs is not None:
                    ob = bars[f"obs__{k}"]
                    covered.append(float(((ob >= lo) & (ob <= hi)).mean()))
        if obs is not None:
            datasets["bars"] = pd.DataFrame(bars)
            traces.append(
                {
                    "type": "barpolar",
                    "theta": {"$ref": "bars.theta"},
                    "r": {"$ref": "bars.obs__0"},
                    "width": width,
                    "marker": {"color": OBSERVED, "line": {"color": SURFACE, "width": 0.5}},
                    "opacity": 0.45,
                    "name": "observed",
                    "hovertemplate": "%{theta:.1f}°: %{r:.3g}<extra>observed</extra>",
                }
            )
            for k in range(len(levels)):
                frame_traces[k].append({"type": "barpolar", "r": {"$ref": f"bars.obs__{k}"}})
        band_name = "steps" if st is not None else ""
        if st is not None:
            datasets["steps"] = pd.DataFrame(steps)
            datasets["whiskers"] = pd.DataFrame(whiskers)
        var = str(draws.name) if draws is not None else str(obs_name)
        height_word = "share" if share else "number"
        caption = f"Each sector of the rose covers {period / s.bins:g} {s.units}. "
        if obs is not None:
            caption += f"The bars show the {height_word} of the observed {obs_name} in it."
        if st is not None:
            caption += (
                f" The thick radial lines show the {pct(s.interval)} interval of the same "
                f"{height_word} in data simulated by the model ({st.sizes[SAMPLE]} draws of "
                f"{var}), and the dark outline is its median."
            )
            if covered:
                caption += (
                    f" {pct(float(np.mean(covered)))} of the bars end inside the interval: a "
                    "bar that ends outside it shows a direction that the model gets wrong."
                )
    # Plotly ignores the trace opacity of a polar fill, so the interval is drawn as lines:
    # radial bars per sector for the rose, the two limits for a curve
    band: list[tuple[str, str, dict[str, Any]]] = []
    if band_name == "steps":
        band.append(("whiskers", "r", {"line": {"width": 5, "color": BAND}}))
    elif band_name == "curve":
        for key in ("lo", "hi"):
            band.append(("curve", key, {"line": {"width": 1.2, "color": BAND, "dash": "dot"}}))
    if band_name:
        band.append((band_name, "med", {"line": {"width": 2, "color": ESTIMATE}}))
    for j, (table, key, extra) in enumerate(band):
        is_med = key == "med"
        traces.append(
            {
                "type": "scatterpolar",
                "mode": "lines",
                "theta": {"$ref": f"{table}.theta"},
                "r": {"$ref": f"{table}.{key}__0"},
                "name": "median" if is_med else f"{pct(s.interval)} interval",
                "showlegend": is_med or j == 0,
                "legendgroup": "median" if is_med else "band",
                **(
                    {"hovertemplate": "%{theta:.1f}°: %{r:.3g}<extra>median</extra>"}
                    if is_med
                    else {"hoverinfo": "skip"}
                ),
                **extra,
            }
        )
        for k in range(len(levels)):
            frame_traces[k].append({"type": "scatterpolar", "r": {"$ref": f"{table}.{key}__{k}"}})
    layout: dict[str, Any] = {
        "height": s.height + (90 if len(levels) > 1 else 0),
        "width": s.width,
        "margin": {"l": 40, "r": 40, "t": 40, "b": 40 + (90 if len(levels) > 1 else 0)},
        "showlegend": True,
        "legend": {"orientation": "h", "x": 0.5, "xanchor": "center", "y": -0.05},
        "polar": {
            "angularaxis": _angular_axis(s.units, period, clockwise),
            "radialaxis": {
                "angle": 90 if clockwise else 0,
                "tickangle": 90 if clockwise else 0,
                "rangemode": "tozero",
            },
        },
    }
    fig: dict[str, Any] = {"data": traces, "layout": layout}
    if len(levels) > 1 and frame is not None:
        fig["frames"] = [
            {"name": lab, "data": frame_traces[k], "traces": list(range(len(traces)))}
            for k, lab in enumerate(frame_names)
        ]
        layout.update(_frame_controls(frame_names, inp.dim_label(frame)))
        layout["legend"]["y"] = 1.08
        caption += f" Use the slider or Play to show a different {inp.dim_label(frame)}."
        if ref.sizes[frame] > len(levels):
            caption += f" The slider shows {len(levels)} of the {ref.sizes[frame]} levels."
    where = "the top" if clockwise else "the right"
    caption += (
        f" Zero is at {where} and the angles go "
        f"{'clockwise' if clockwise else 'counterclockwise'}; one turn is {period:g} {s.units}."
    )
    return Figure(chart=fig, datasets=datasets, caption=caption.strip(), title=f"Rose of {var}")


def sci_map_recipes() -> list[Recipe[Any]]:
    """The map, tree and polar recipes of the scientific-model pack."""
    return [area_map, phylogeny, rose]
