"""Point patterns: localisations, events or emitters over an optional intensity field.

``sci.point_pattern`` (part of the ``sci`` pack) draws one marker per point at its ``x`` and
``y`` (the posterior mean for draws, the point statistic for a score summary, or the values
as given), with an error ellipse per point and, below the points, an image or intensity
field. The helpers that find a variable in any group and reduce it to one value per element
(:func:`find_var`, :func:`point_values`) and the categorical colours (:func:`category_colours`)
are shared with the ``net`` pack.
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
from pydantic import Field

from mapi_plots.packs.sci import (
    ROW_BUDGET,
    VALUE_BUDGET,
    Colormap,
    _grid_outputs,
    _numeric_coord,
    _range,
    _scale,
    _select,
    block_factor,
    block_mean,
    grid_decl,
)
from mapi_plots.palette import BAND, ESTIMATE, OBSERVED, SURFACE
from mapi_plots.recipes._base import Figure, RecipeDataError, RecipeInput, RecipeSettings, recipe
from mapi_plots.recipes._util import SAMPLE, fmt, pct, sample_dims, stack_samples, summary_bands

__all__ = [
    "PointPatternSettings",
    "category_colours",
    "find_var",
    "point_pattern",
    "point_values",
]

#: where a variable is looked for, in order (the root of the tree comes last)
SEARCH_GROUPS = (
    "posterior",
    "predictions",
    "posterior_predictive",
    "summary",
    "observed_data",
    "constant_data",
)
_DRAW_GROUPS = ("posterior", "predictions", "posterior_predictive")
#: the most groups drawn with one colour each (more fall back to a continuous scale)
MAX_CATEGORIES = 12
_ELLIPSE_VERTICES = 24


# ------------------------------------------------------------------------------ helpers


def _groups(inp: RecipeInput, order: Sequence[str]) -> list[tuple[str, xr.Dataset]]:
    out: list[tuple[str, xr.Dataset]] = []
    if not inp.tree.children:  # a bare dataset: every group lookup would return it
        order = ()
    for g in order:
        ds = inp.find(g)
        if ds is not None:
            out.append((g, ds))
    root = inp.root()
    if root is not None and root.data_vars:
        out.append(("root", root))
    return out


def find_var(
    inp: RecipeInput, name: str, order: Sequence[str] = SEARCH_GROUPS
) -> tuple[xr.DataArray, str] | None:
    """``(variable, group)`` for the first group in ``order`` (then the root) that has
    ``name``; None when no group has it."""
    for g, ds in _groups(inp, order):
        if name in ds.data_vars:
            return ds[name], g
    return None


def point_values(
    da: xr.DataArray, *, stat: Literal["mean", "median", "sd"] = "mean"
) -> tuple[xr.DataArray, xr.DataArray | None]:
    """``(one value per element, stacked draws or None)``.

    Draws (``chain``/``draw`` or ``sample`` dims) give their ``stat``; a score summary (a
    ``stat`` dim) gives its point statistic (or its ``sd`` for ``stat="sd"``); anything else
    is taken as it is.
    """
    if sample_dims(da):
        st = stack_samples(da)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            if stat == "median":
                val = st.median(SAMPLE, skipna=True)
            elif stat == "sd":
                val = st.std(SAMPLE, skipna=True, ddof=1 if st.sizes[SAMPLE] > 1 else 0)
            else:
                val = st.mean(SAMPLE, skipna=True)
        return val, st
    if "stat" in da.dims:
        labels = [str(v) for v in da["stat"].values]
        if stat == "sd":
            if "sd" not in labels:
                raise RecipeDataError(f"the summary of {da.name!r} has no sd statistic")
            return da.sel(stat="sd", drop=True), None
        return summary_bands(da, [])["point"], None
    return da, None


def category_colours(k: int) -> list[str]:
    """``k`` distinct colours for categories (the viridis scale at even steps)."""
    if k <= 1:
        return [ESTIMATE]
    import plotly.colors as pc

    return [str(c) for c in pc.sample_colorscale(_scale("viridis"), np.linspace(0.0, 0.9, k))]


def categories(values: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """``(integer code per element, level names)``; missing values get code -1."""
    vals = np.asarray(values)
    labels = np.array([str(v) for v in vals.ravel()], dtype=object)
    if vals.dtype.kind == "f":
        bad = ~np.isfinite(vals.ravel().astype(float))
        labels = np.array([_fmt_level(v) for v in vals.ravel()], dtype=object)
    else:
        bad = np.zeros(labels.shape, dtype=bool)
    levels = list(dict.fromkeys(str(v) for v, b in zip(labels, bad, strict=True) if not b))
    try:
        levels.sort(key=float)
    except ValueError:
        levels.sort()
    index = {lab: i for i, lab in enumerate(levels)}
    codes = np.array(
        [-1 if b else index[str(v)] for v, b in zip(labels, bad, strict=True)], dtype=int
    )
    return codes, levels


def _fmt_level(v: Any) -> str:
    f = float(v)
    return str(int(f)) if math.isfinite(f) and f == int(f) else f"{f:g}"


def group_codes(da: xr.DataArray, dim: str) -> tuple[np.ndarray, list[str]] | None:
    """Categorical codes along ``dim`` from a group variable (draws give the most common
    level of each element); None when the variable does not have ``dim`` or is continuous."""
    if dim not in da.dims:
        return None
    if sample_dims(da):
        st = stack_samples(da)
        if st.dtype.kind == "f" and not np.allclose(
            np.nan_to_num(st.values), np.round(np.nan_to_num(st.values))
        ):
            return None
        st = st.transpose(SAMPLE, dim, ...)
        if st.ndim != 2:
            return None
        raw = np.asarray(st.values)
        modes = []
        for j in range(raw.shape[1]):
            col = raw[:, j]
            col = col[np.isfinite(col)] if col.dtype.kind == "f" else col
            if not col.size:
                modes.append(np.nan)
                continue
            u, n = np.unique(col, return_counts=True)
            modes.append(u[np.argmax(n)])
        return categories(np.asarray(modes))
    other = [d for d in da.dims if d != dim]
    if other:
        return None
    vals = np.asarray(da.values)
    if vals.dtype.kind == "f":
        finite = vals[np.isfinite(vals)]
        if finite.size and not np.allclose(finite, np.round(finite)):
            return None
    return categories(vals)


def ellipse_paths(
    cx: np.ndarray, cy: np.ndarray, vx: np.ndarray, vy: np.ndarray, cxy: np.ndarray, r: float
) -> tuple[np.ndarray, np.ndarray]:
    """Closed outlines of the ``r``-sigma ellipses of 2 x 2 covariances, NaN between them."""
    t = np.linspace(0.0, 2 * np.pi, _ELLIPSE_VERTICES + 1)
    tr = (vx + vy) / 2
    det = vx * vy - cxy**2
    disc = np.sqrt(np.maximum(tr**2 - det, 0.0))
    l1, l2 = np.maximum(tr + disc, 0.0), np.maximum(tr - disc, 0.0)
    axis = np.where(vx >= vy, 0.0, np.pi / 2)
    angle = np.where(np.abs(cxy) > 1e-300, np.arctan2(l1 - vx, cxy), axis)
    a, b = r * np.sqrt(l1), r * np.sqrt(l2)
    ct, sn = np.cos(angle)[:, None], np.sin(angle)[:, None]
    ex = a[:, None] * np.cos(t)[None, :]
    ey = b[:, None] * np.sin(t)[None, :]
    px = cx[:, None] + ex * ct - ey * sn
    py = cy[:, None] + ex * sn + ey * ct
    gap = np.full((len(cx), 1), np.nan)
    return np.hstack([px, gap]).ravel(), np.hstack([py, gap]).ravel()


# --------------------------------------------------------------------------- the recipe


class PointPatternSettings(RecipeSettings):
    points: str | None = Field(None, description="The point dim (default: the dim of x)")
    x: str = Field("x", description="Variable with the x position of each point")
    y: str = Field("y", description="Variable with the y position of each point")
    sd_x: str = Field("sd_x", description="Variable with the sd of x (used when it exists)")
    sd_y: str = Field("sd_y", description="Variable with the sd of y (used when it exists)")
    radius: str = Field("radius", description="Variable with a credible radius per point")
    group: str | None = Field(
        "auto", description="Variable with a category per point (auto: 'group'; none: off)"
    )
    field: str | None = Field(
        "auto", description="Background image or intensity (auto: a grid output; none: off)"
    )
    field_stat: Literal["mean", "median", "sd"] = "mean"
    ellipses: bool | None = Field(None, description="Error ellipses (default: when known)")
    level: float = Field(0.9, gt=0, lt=1, description="Probability inside each ellipse")
    observed: bool = Field(True, description="Also draw actual points from observed_data")
    select: dict[str, str | int | float] | None = None
    max_points: int = Field(20_000, ge=1, le=100_000)
    max_ellipses: int = Field(2_000, ge=0, le=5_000)
    max_cells: int = Field(40_000, ge=16, le=ROW_BUDGET)
    colormap: Colormap | None = Field(None, description="Field colours (default: grid's, gray)")
    origin: Literal["upper", "lower"] | None = Field(
        None, description="Where the smallest y is (default: the field grid's, else lower)"
    )
    marker_size: float = Field(6.0, gt=0, le=30)
    width: int = Field(640, ge=80, le=2000)
    height: int = Field(560, ge=60, le=2000)


@dataclass(slots=True)
class _Points:
    dim: str
    group: str
    x: np.ndarray
    y: np.ndarray
    labels: list[str]
    cov: tuple[np.ndarray, np.ndarray, np.ndarray] | None  # var x, var y, cov xy
    circle: np.ndarray | None  # radius per point (drawn as given)
    n_draws: int | None


def _flat(da: xr.DataArray, dim: str, select: dict[str, Any] | None) -> xr.DataArray:
    sel = {d: v for d, v in (select or {}).items() if d in da.dims}
    da = _select(da, sel)
    extra = [d for d in da.dims if d not in (dim, SAMPLE, "chain", "draw", "stat")]
    big = [str(d) for d in extra if da.sizes[d] > 1]
    if big:
        raise RecipeDataError(
            f"{da.name!r} has more dims than the plot uses: {', '.join(big)}. "
            "Pick one level of each with select={dim: level}."
        )
    return da.squeeze(extra, drop=True) if extra else da


def _point_dim(da: xr.DataArray, s: PointPatternSettings) -> str:
    dims = [str(d) for d in da.dims if d not in (*sample_dims(da), "stat")]
    if s.points is not None:
        if s.points not in dims:
            raise RecipeDataError(f"{da.name!r} has no dim {s.points!r} (dims: {', '.join(dims)})")
        return s.points
    rest = [d for d in dims if d not in (s.select or {})]
    if len(rest) != 1:
        raise RecipeDataError(
            f"{da.name!r} has dims {', '.join(dims) or 'none'}: pass points=<point dim>"
        )
    return rest[0]


def _read_points(inp: RecipeInput, s: PointPatternSettings) -> _Points:
    hit = find_var(inp, s.x)
    if hit is None:
        raise RecipeDataError(
            f"no variable {s.x!r} with the x positions of the points (set x=<variable>)"
        )
    xda, group = hit
    ds = inp.find(group) if group != "root" else inp.root()
    assert ds is not None
    if s.y not in ds.data_vars:
        raise RecipeDataError(f"the group {group!r} has {s.x!r} but no {s.y!r} (set y=<variable>)")
    dim = _point_dim(xda, s)
    xda = _flat(xda, dim, s.select)
    yda = _flat(ds[s.y], dim, s.select)
    if dim not in yda.dims:
        raise RecipeDataError(f"{s.y!r} has no dim {dim!r}")
    xv, xst = point_values(xda)
    yv, yst = point_values(yda)
    labels = (
        [str(v) for v in xv[dim].values]
        if dim in xv.coords
        else [str(i) for i in range(xv.sizes[dim])]
    )
    x = np.asarray(xv.transpose(dim).values, dtype=float)
    y = np.asarray(yv.transpose(dim).values, dtype=float)
    cov: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    n_draws: int | None = None
    if xst is not None and yst is not None and xst.sizes[SAMPLE] > 1:
        a = np.asarray(xst.transpose(SAMPLE, dim).values, dtype=float)
        b = np.asarray(yst.transpose(SAMPLE, dim).values, dtype=float)
        n_draws = a.shape[0]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            ma, mb = np.nanmean(a, 0), np.nanmean(b, 0)
            cov = (
                np.nanvar(a, 0, ddof=1),
                np.nanvar(b, 0, ddof=1),
                np.nanmean((a - ma) * (b - mb), 0) * n_draws / (n_draws - 1),
            )
    elif "stat" in xda.dims and "stat" in yda.dims and "sd" in [str(v) for v in xda["stat"].values]:
        sx = np.asarray(point_values(xda, stat="sd")[0].transpose(dim).values, dtype=float)
        sy = np.asarray(point_values(yda, stat="sd")[0].transpose(dim).values, dtype=float)
        cov = (sx**2, sy**2, np.zeros_like(sx))
    else:
        sx_da = ds[s.sd_x] if s.sd_x in ds.data_vars else None
        sy_da = ds[s.sd_y] if s.sd_y in ds.data_vars else None
        if sx_da is not None and sy_da is not None:
            sx = np.asarray(point_values(_flat(sx_da, dim, s.select))[0].values, dtype=float)
            sy = np.asarray(point_values(_flat(sy_da, dim, s.select))[0].values, dtype=float)
            if sx.shape == x.shape and sy.shape == x.shape:
                cov = (sx**2, sy**2, np.zeros_like(sx))
    circle = None
    if cov is None and s.radius in ds.data_vars:
        rv = np.asarray(point_values(_flat(ds[s.radius], dim, s.select))[0].values, dtype=float)
        if rv.shape == x.shape:
            circle = rv
    return _Points(dim, group, x, y, labels, cov, circle, n_draws)


def _field(
    inp: RecipeInput, s: PointPatternSettings
) -> tuple[pd.DataFrame, str, dict[str, Any], int, tuple[int, int]] | None:
    """``(cells table, variable name, grid declaration, block size, raster shape)``."""
    if s.field is None or s.field == "none":
        return None
    if s.field == "auto":
        names = list(_grid_outputs(inp))
        for _g, ds in _groups(inp, _DRAW_GROUPS):
            names += [str(v) for v in ds.data_vars if ds[v].attrs.get("grid")]
        hit = None
        for n in dict.fromkeys(names):
            hit = find_var(inp, n, _DRAW_GROUPS)
            if hit is not None:
                break
        if hit is None:
            return None
    else:
        hit = find_var(inp, s.field)
        if hit is None:
            raise RecipeDataError(f"no field variable {s.field!r} found")
    da, _ = hit
    name = str(da.name)
    grid = grid_decl(inp, name, da)
    sel = {d: v for d, v in (s.select or {}).items() if d in da.dims}
    da = _select(da, sel)
    val, _ = point_values(da, stat=s.field_stat)
    dims = [str(d) for d in val.dims]
    y, x = grid.get("y"), grid.get("x")
    if y not in dims or x not in dims:
        big = [d for d in dims if val.sizes[d] > 1]
        if len(big) != 2:
            raise RecipeDataError(
                f"the field {name!r} has dims {', '.join(dims)}: it needs two (rows, columns)"
            )
        y, x = big
    extra = [d for d in dims if d not in (y, x)]
    if any(val.sizes[d] > 1 for d in extra):
        raise RecipeDataError(
            f"the field {name!r} has more dims than the plot uses: pick one level with select"
        )
    val = val.squeeze(extra, drop=True).transpose(y, x)
    xs, _ = _numeric_coord(val, str(x))
    ys, _ = _numeric_coord(val, str(y))
    ny, nx = val.shape
    k = block_factor(ny, nx, max(1, min(s.max_cells, VALUE_BUDGET // 3)))
    z = block_mean(np.asarray(val.values, dtype=float), k)
    xs, ys = block_mean(xs, k, axes=1), block_mean(ys, k, axes=1)
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    table = pd.DataFrame(
        {
            "x": xx.ravel().astype(np.float32),
            "y": yy.ravel().astype(np.float32),
            "z": z.ravel().astype(np.float32),
        }
    )
    return table, name, grid, k, (ny, nx)


@recipe(
    "sci.point_pattern",
    title="Point pattern",
    settings=PointPatternSettings,
    persona="both",
    summary="Points such as events or light sources at their positions, with an error ellipse "
    "for each point, over an image or an intensity map.",
    tags=["sci", "image"],
)
def point_pattern(inp: RecipeInput, s: PointPatternSettings) -> Figure:
    """Points (localisations, events) with error ellipses over an optional intensity field."""
    p = _read_points(inp, s)
    n_all = len(p.x)
    keep = np.isfinite(p.x) & np.isfinite(p.y)
    idx = np.flatnonzero(keep)
    if not idx.size:
        raise RecipeDataError(f"{s.x!r} and {s.y!r} have no finite positions")
    if idx.size > s.max_points:
        idx = idx[np.unique(np.linspace(0, idx.size - 1, s.max_points).round().astype(int))]
    x, y = p.x[idx], p.y[idx]
    labels = [p.labels[i] for i in idx]
    codes: np.ndarray | None = None
    levels: list[str] = []
    gname: str | None = None
    if s.group is not None and s.group != "none":
        gname = "group" if s.group == "auto" else s.group
        g_hit = find_var(inp, gname)
        if g_hit is None:
            if s.group != "auto":
                raise RecipeDataError(f"no group variable {s.group!r} found")
            gname = None
        else:
            got = group_codes(_flat(g_hit[0], p.dim, s.select), p.dim)
            if got is None or len(got[0]) != n_all:
                if s.group != "auto":
                    raise RecipeDataError(
                        f"{s.group!r} must give one category for each level of {p.dim!r}"
                    )
                gname = None
            else:
                codes, levels = got[0][idx], got[1]

    datasets: dict[str, pd.DataFrame] = {}
    traces: list[dict[str, Any]] = []
    field = _field(inp, s)
    origin = s.origin
    colorbar_used = False
    if field is not None:
        table, fname, grid, block, shape = field
        datasets["cells"] = table
        colormap: Colormap = s.colormap or grid.get("colormap") or "gray"
        if colormap not in ("viridis", "magma", "cividis", "gray", "diverging"):
            colormap = "gray"
        origin = origin or grid.get("origin") or "lower"
        rng = _range([table["z"].to_numpy()], symmetric=colormap == "diverging")
        hm: dict[str, Any] = {
            "type": "heatmap",
            "x": {"$ref": "cells.x"},
            "y": {"$ref": "cells.y"},
            "z": {"$ref": "cells.z"},
            "colorscale": _scale(colormap),
            "hoverongaps": False,
            "colorbar": {"thickness": 10, "outlinewidth": 0, "title": {"text": fname}},
            "hovertemplate": f"x %{{x:.4g}}<br>y %{{y:.4g}}<br>{fname} %{{z:.4g}}<extra></extra>",
            "showlegend": False,
        }
        if rng is not None:
            hm["zmin"], hm["zmax"] = rng
        traces.append(hm)
        colorbar_used = True
    origin = origin or "lower"

    # error ellipses (or credible circles)
    n_ell = 0
    want = s.ellipses if s.ellipses is not None else True
    r_sigma = math.sqrt(-2.0 * math.log(1.0 - s.level))
    if want and s.max_ellipses and (p.cov is not None or p.circle is not None):
        sub = (
            idx
            if idx.size <= s.max_ellipses
            else idx[np.unique(np.linspace(0, idx.size - 1, s.max_ellipses).round().astype(int))]
        )
        if p.cov is not None:
            vx, vy, cxy = (c[sub] for c in p.cov)
            r = r_sigma
        else:
            assert p.circle is not None
            vx = vy = p.circle[sub] ** 2
            cxy = np.zeros_like(vx)
            r = 1.0
        ok = np.isfinite(vx) & np.isfinite(vy) & np.isfinite(cxy)
        sub, vx, vy, cxy = sub[ok], vx[ok], vy[ok], cxy[ok]
        if sub.size:
            ex, ey = ellipse_paths(p.x[sub], p.y[sub], vx, vy, cxy, r)
            datasets["ellipses"] = pd.DataFrame(
                {"x": ex.astype(np.float32), "y": ey.astype(np.float32)}
            )
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": {"$ref": "ellipses.x"},
                    "y": {"$ref": "ellipses.y"},
                    "line": {"color": SURFACE if field is not None else BAND, "width": 1},
                    "opacity": 0.75,
                    "hoverinfo": "skip",
                    "showlegend": False,
                    "name": "ellipses",
                }
            )
            n_ell = int(sub.size)

    # the points: one trace per category (a legend), or one coloured by a continuous scale
    hover = f"{p.dim} %{{text}}<br>x %{{x:.4g}}<br>y %{{y:.4g}}<extra></extra>"
    base_marker: dict[str, Any] = {
        "size": s.marker_size,
        "line": {"color": SURFACE, "width": 0.8},
    }
    if codes is not None and len(levels) <= MAX_CATEGORIES:
        cols = category_colours(len(levels))
        for k, lab in enumerate(levels):
            sel = codes == k
            if not sel.any():
                continue
            key = f"points_{k}"
            datasets[key] = pd.DataFrame(
                {
                    "x": x[sel].astype(np.float32),
                    "y": y[sel].astype(np.float32),
                    "label": [labels[i] for i in np.flatnonzero(sel)],
                }
            )
            traces.append(
                {
                    "type": "scatter",
                    "mode": "markers",
                    "name": f"{gname} {lab}",
                    "x": {"$ref": f"{key}.x"},
                    "y": {"$ref": f"{key}.y"},
                    "text": {"$ref": f"{key}.label"},
                    "marker": {**base_marker, "color": cols[k]},
                    "hovertemplate": f"{gname} {lab}<br>" + hover,
                }
            )
    else:
        cols_d: dict[str, Any] = {
            "x": x.astype(np.float32),
            "y": y.astype(np.float32),
            "label": labels,
        }
        marker = {**base_marker, "color": ESTIMATE}
        if codes is not None:
            cols_d["code"] = codes.astype(np.float32)
            marker = {
                **base_marker,
                "color": {"$ref": "points.code"},
                "colorscale": _scale("viridis"),
                "showscale": not colorbar_used,
            }
        datasets["points"] = pd.DataFrame(cols_d)
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": "points",
                "x": {"$ref": "points.x"},
                "y": {"$ref": "points.y"},
                "text": {"$ref": "points.label"},
                "marker": marker,
                "hovertemplate": hover,
                "showlegend": False,
            }
        )

    # actual points next to inferred ones
    n_obs = 0
    if s.observed and p.group in _DRAW_GROUPS:
        obs = inp.find("observed_data")
        if obs is not None and s.x in obs.data_vars and s.y in obs.data_vars:
            ox_da, oy_da = obs[s.x], obs[s.y]
            od = [str(d) for d in ox_da.dims]
            if len(od) == 1 and oy_da.dims == ox_da.dims:
                ox = np.asarray(ox_da.values, dtype=float)
                oy = np.asarray(oy_da.values, dtype=float)
                fin = np.isfinite(ox) & np.isfinite(oy)
                ox, oy = ox[fin][: s.max_points], oy[fin][: s.max_points]
                if ox.size:
                    datasets["observed"] = pd.DataFrame(
                        {"x": ox.astype(np.float32), "y": oy.astype(np.float32)}
                    )
                    traces.append(
                        {
                            "type": "scatter",
                            "mode": "markers",
                            "name": "actual",
                            "x": {"$ref": "observed.x"},
                            "y": {"$ref": "observed.y"},
                            "marker": {
                                "symbol": "circle-open",
                                "color": OBSERVED if field is None else SURFACE,
                                "size": s.marker_size + 3,
                                "line": {"width": 1.2},
                            },
                            "hovertemplate": "actual<br>x %{x:.4g}<br>y %{y:.4g}<extra></extra>",
                        }
                    )
                    n_obs = int(ox.size)

    show_legend = sum(1 for t in traces if t.get("showlegend", True) is not False) > 1
    layout: dict[str, Any] = {
        "height": s.height,
        "width": s.width,
        "margin": {"l": 56, "r": 24, "t": 24, "b": 48},
        "xaxis": {
            "title": {"text": s.x},
            "showgrid": False,
            "zeroline": False,
            "constrain": "domain",
        },
        "yaxis": {
            "title": {"text": s.y},
            "showgrid": False,
            "zeroline": False,
            "scaleanchor": "x",
            "scaleratio": 1,
            "constrain": "domain",
            **({"autorange": "reversed"} if origin == "upper" else {}),
        },
        "hovermode": "closest",
        "showlegend": show_legend,
        "legend": {"orientation": "h", "y": -0.12, "x": 0, "xanchor": "left"},
    }
    if show_legend:
        layout["margin"]["b"] = 80

    what = "the posterior mean of" if p.n_draws else "the value of"
    caption = (
        f"Each dot is one {p.dim} at {what} its position ({s.x}, {s.y}). "
        f"The plot shows {idx.size} of the {n_all} points."
        if idx.size < n_all
        else f"Each dot is one {p.dim} at {what} its position ({s.x}, {s.y}). "
        f"There are {idx.size} points."
    )
    if n_ell:
        if p.cov is not None:
            src = f"{p.n_draws} draws" if p.n_draws else "the sd of each position"
            caption += (
                f" Each ellipse contains the true position with {pct(s.level)} probability "
                f"(from {src}). A large ellipse shows that the position is not certain."
            )
        else:
            caption += f" Each circle shows the credible radius ({s.radius}) of the point."
        if n_ell < idx.size:
            caption += f" The plot shows {n_ell} ellipses."
    if codes is not None:
        caption += f" The colours show {gname}."
        if len(levels) > MAX_CATEGORIES:
            caption += f" There are {len(levels)} levels, so the colours are a scale."
    if field is not None:
        table, fname, _grid, block, shape = field
        caption += f" The background shows the {s.field_stat} of {fname}."
        if block > 1:
            caption += (
                f" The field has {shape[0]} × {shape[1]} cells. To keep the plot small, each "
                f"square shows the average of {block} × {block} cells."
            )
    if n_obs:
        caption += f" The open circles are the {n_obs} actual points."
    if p.cov is not None and n_ell:
        sd = np.sqrt(np.nanmedian(np.concatenate([p.cov[0][idx], p.cov[1][idx]])))
        if np.isfinite(sd):
            caption += f" The median sd of a coordinate is {fmt(float(sd))}."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets=datasets,
        caption=caption,
        title=f"Points of {s.x} and {s.y}",
    )
