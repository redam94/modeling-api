"""Scientific-model domain pack: gridded fields, vector fields, profiles and ensembles.

Plotly figures (heatmaps, quivers, bands with sliders), rendered through
:func:`~mapi_plots.recipes._base.plotly_plot`. Plugins opt in by listing the ids in their
manifest's ``plots``:

* ``sci.field_map``: statistics of a gridded output (mean, sd, mean / sd, 5 % and 95 %
  quantiles, a few draws), one heatmap panel per statistic and a slider over a ``frame`` dim;
* ``sci.vector_field``: the length of a vector field output (a ``component`` dim of length 2
  with the ``(x, y)`` components) with arrows of the mean vector, plus its sd;
* ``sci.profile_bands``: median and 50 / 90 % bands of an output along one dim, one curve per
  level of a second dim, observed points and a slider that shows one level at a time;
* ``sci.ensemble``: a spaghetti of posterior draws along one dim, the 90 % band, the median
  and the observed points.

The image and spectrum recipes of the pack (``sci.image_residuals``, ``sci.ppc_image_grid``,
``sci.swatches``, ``sci.power_spectrum``) are in :mod:`mapi_plots.packs.sci_images`, built on
the field helpers here; :func:`sci_recipes` lists them too.

Inputs are bundle or score trees, as for the core recipes: draws (``chain``/``draw`` or
``sample`` dims) in ``posterior``, ``predictions`` or ``posterior_predictive`` and actual values
in ``observed_data``. The grid dims default to the output's ``grid`` declaration
(:class:`mapi_spec.capabilities.GridDecl`), read from the variable's ``grid`` attr or from the
model manifest in the tree (``/model`` attrs ``manifest``).

Size: every field figure keeps its cells in one shared table (``x``, ``y`` and one column per
statistic and frame), so the inline Arrow stays under the 2 MB cap and the plot sandbox's row
limit. Large rasters are block-averaged until the table fits.
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field, field_validator

from mapi_plots.palette import BAND, ESTIMATE, MUTED, OBSERVED
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    recipe,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    check_levels,
    coord_values,
    interval_columns,
    observed_name,
    pct,
    sample_dims,
    stack_samples,
    time_dim,
)

__all__ = [
    "EnsembleSettings",
    "FieldMapSettings",
    "ProfileBandsSettings",
    "VectorFieldSettings",
    "block_mean",
    "ensemble",
    "field_map",
    "grid_decl",
    "profile_bands",
    "sci_recipes",
    "vector_field",
]

GroupName = Literal["auto", "posterior", "predictions", "posterior_predictive"]
Colormap = Literal["viridis", "magma", "cividis", "gray", "diverging"]
FieldStat = Literal["mean", "median", "sd", "z", "q05", "q95", "draws"]
VectorStat = Literal["mean", "sd", "q05", "q95"]

FIELD_GROUPS = ("posterior", "predictions", "posterior_predictive")
CURVE_GROUPS = ("predictions", "posterior_predictive", "posterior")
OBS_GROUPS = ("observed_data", "observed")

#: values in one field table: float32 plus a validity bit is ~4.1 bytes, x 4/3 for base64,
#: so 300 000 values are ~1.65 MB, under the 2 MB inline Arrow cap.
VALUE_BUDGET = 300_000
#: the plot sandbox draws at most 250 000 rows over all datasets of a plot
ROW_BUDGET = 200_000

_STAT_TITLES = {
    "mean": "Mean",
    "median": "Median",
    "sd": "SD",
    "z": "Mean ÷ SD",
    "q05": "5% quantile",
    "q95": "95% quantile",
}


# ------------------------------------------------------------------------------ lookups


def _loads(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def grid_decl(inp: RecipeInput, var: str, da: xr.DataArray | None = None) -> dict[str, Any]:
    """The output's ``GridDecl`` as a dict (``{}`` when there is none).

    Looked up on the variable's ``grid`` attr, then in the model manifest stored in the tree
    (``/model`` or root attrs ``manifest``: ``outputs[var].grid``).
    """
    if da is not None:
        g = _loads(da.attrs.get("grid"))
        if isinstance(g, Mapping):
            return dict(g)
    for path in ("model", "/"):
        try:
            node = inp.tree[path] if path != "/" else inp.tree
        except KeyError:
            continue
        manifest = _loads(node.attrs.get("manifest"))
        if not isinstance(manifest, Mapping):
            continue
        out = (manifest.get("outputs") or {}).get(var)
        g = out.get("grid") if isinstance(out, Mapping) else None
        if isinstance(g, Mapping):
            return dict(g)
    return {}


def _grid_outputs(inp: RecipeInput, *, grid: bool = True) -> list[str]:
    """Output names with a ``grid`` in the manifest (or, with ``grid=False``, without one), in
    declaration order."""
    for path in ("model", "/"):
        try:
            node = inp.tree[path] if path != "/" else inp.tree
        except KeyError:
            continue
        manifest = _loads(node.attrs.get("manifest"))
        if isinstance(manifest, Mapping):
            outs = manifest.get("outputs") or {}
            return [
                str(k)
                for k, v in outs.items()
                if isinstance(v, Mapping) and bool(v.get("grid")) is grid
            ]
    return []


def _find_draws(
    inp: RecipeInput,
    var: str | None,
    group: GroupName,
    order: tuple[str, ...],
    *,
    need: Sequence[str] = (),
    min_dims: int = 1,
    prefer_grid: bool = True,
) -> tuple[xr.DataArray, str]:
    """``(draws of var, group name)``: the first group (in ``order``) with ``var`` as draws.

    Without ``var``: an output that declares a ``grid`` in the manifest (with
    ``prefer_grid=False``: an output that declares none, for curve recipes), else the first
    variable with draws that has the ``need`` dims and at least ``min_dims`` other dims.
    """
    groups = order if group == "auto" else (group,)
    preferred = _grid_outputs(inp, grid=prefer_grid)
    seen: list[str] = []
    for g in groups:
        ds = inp.find(g)
        if ds is None:
            continue
        names = [str(v) for v in ds.data_vars if sample_dims(ds[v])]
        if var is not None:
            if var in names:
                return ds[var], g
            continue
        seen += names
        ok = [
            n
            for n in names
            if all(d in ds[n].dims for d in need)
            and len([d for d in ds[n].dims if d not in sample_dims(ds[n])]) >= min_dims
        ]
        for n in [*(p for p in preferred if p in ok), *ok]:
            return ds[n], g
    if var is None and seen and need:
        raise RecipeDataError(
            f"no variable with draws has the dims {', '.join(need)} "
            f"(variables: {', '.join(dict.fromkeys(seen))})"
        )
    what = f"{var!r}" if var else "a variable"
    raise RecipeDataError(
        f"no draws of {what} found: expected chain/draw or sample dims in one of the groups "
        + ", ".join(groups)
    )


def _select(da: xr.DataArray, select: Mapping[str, Any] | None) -> xr.DataArray:
    """Pick one level of each dim in ``select`` (by label, else by position)."""
    for dim, value in (select or {}).items():
        if dim not in da.dims:
            raise RecipeDataError(f"select: no dim {dim!r} (dims: {', '.join(map(str, da.dims))})")
        try:
            da = da.sel({dim: value})
        except (KeyError, TypeError, ValueError):
            if isinstance(value, int) and -da.sizes[dim] <= value < da.sizes[dim]:
                da = da.isel({dim: value})
            else:
                raise RecipeDataError(f"select: {dim!r} has no level {value!r}") from None
    return da


def _only(da: xr.DataArray, keep: Sequence[str | None]) -> xr.DataArray:
    """Squeeze length-1 dims outside ``keep`` (and the sample dims); others are an error."""
    kept = {k for k in keep if k} | {SAMPLE}
    extra = [str(d) for d in da.dims if d not in kept]
    big = [d for d in extra if da.sizes[d] > 1]
    if big:
        raise RecipeDataError(
            f"{da.name!r} has more dims than the plot uses: {', '.join(big)}. "
            "Pick one level of each with select={dim: level}."
        )
    return da.squeeze(extra, drop=True) if extra else da


def _thin(st: xr.DataArray, max_draws: int) -> xr.DataArray:
    n = st.sizes[SAMPLE]
    if n <= max_draws:
        return st
    idx = np.unique(np.linspace(0, n - 1, max_draws).round().astype(int))
    return st.isel({SAMPLE: idx})


def _numeric_coord(da: xr.DataArray, dim: str) -> tuple[np.ndarray, bool]:
    """``(float positions, True)`` for a numeric coord, else ``(0..n-1, False)``."""
    vals = coord_values(da, dim)
    if np.issubdtype(np.asarray(vals).dtype, np.number) and dim in da.coords:
        return np.asarray(vals, dtype=float), True
    return np.arange(da.sizes[dim], dtype=float), False


def _label(v: Any) -> str:
    if isinstance(v, np.datetime64):
        return str(pd.Timestamp(v).date()) if pd.Timestamp(v).normalize() == v else str(v)
    if isinstance(v, float | np.floating):
        return f"{float(v):g}"
    return str(v)


def _to_log(x: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        return np.where(x > 0, np.log10(np.where(x > 0, x, 1.0)), np.nan)


# ---------------------------------------------------------------------------- rasters


def block_factor(ny: int, nx: int, max_cells: int) -> int:
    """The smallest block size ``k`` with ``ceil(ny/k) * ceil(nx/k) <= max_cells``."""
    k = 1
    while math.ceil(ny / k) * math.ceil(nx / k) > max(1, max_cells):
        k += 1
    return k


def block_mean(a: np.ndarray, k: int, axes: int = 2) -> np.ndarray:
    """NaN-aware mean over ``k x k`` blocks of the last two axes (or ``k`` of the last one).

    Edges are padded with NaN, so a part block is the mean of the cells it has; a block with
    no finite cell stays NaN (masked pixels survive).
    """
    if k <= 1:
        return a
    a = np.asarray(a, dtype=float)
    pads = [(0, 0)] * (a.ndim - axes) + [(0, (-n) % k) for n in a.shape[-axes:]]
    a = np.pad(a, pads, constant_values=np.nan)
    shape = list(a.shape[:-axes])
    for n in a.shape[-axes:]:
        shape += [n // k, k]
    red = tuple(range(a.ndim - axes, a.ndim - axes + 2 * axes))[1::2]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out: np.ndarray = np.nanmean(a.reshape(shape), axis=red)
    return out


def _stats(draws: np.ndarray, which: Sequence[str]) -> dict[str, np.ndarray]:
    """Per-pixel statistics of ``(sample, ...)`` draws; NaN where every draw is NaN."""
    out: dict[str, np.ndarray] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = np.nanmean(draws, axis=0)
        sd = np.nanstd(draws, axis=0, ddof=1 if draws.shape[0] > 1 else 0)
        for w in which:
            if w == "mean":
                out[w] = mean
            elif w == "median":
                out[w] = np.nanmedian(draws, axis=0)
            elif w == "sd":
                out[w] = sd
            elif w == "z":
                out[w] = np.where(sd > 0, mean / np.where(sd > 0, sd, 1.0), np.nan)
            elif w == "q05":
                out[w] = np.nanquantile(draws, 0.05, axis=0)
            elif w == "q95":
                out[w] = np.nanquantile(draws, 0.95, axis=0)
    return out


def _scale(name: Colormap) -> list[list[Any]]:
    """An explicit ``[[t, colour], ...]`` colour scale (no named scales in the spec)."""
    import plotly.colors as pc

    if name == "gray":
        return [[0.0, "rgb(0,0,0)"], [1.0, "rgb(250,250,250)"]]
    cols = {
        "viridis": pc.sequential.Viridis,
        "magma": pc.sequential.Magma,
        "cividis": pc.sequential.Cividis,
        "diverging": pc.diverging.RdBu[::-1],
    }[name]
    n = len(cols) - 1
    return [[round(i / n, 6), str(c)] for i, c in enumerate(cols)]


def _range(arrays: Sequence[np.ndarray], symmetric: bool = False) -> tuple[float, float] | None:
    vals = np.concatenate([np.ravel(a) for a in arrays]) if arrays else np.array([])
    vals = vals[np.isfinite(vals)]
    if not vals.size:
        return None
    lo, hi = (float(v) for v in np.percentile(vals, [0.5, 99.5]))
    if symmetric:
        m = max(abs(lo), abs(hi)) or 1.0
        return -m, m
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0) * 1e-6
    return lo, hi


@dataclass(slots=True)
class _Panel:
    key: str  # column stem in the cell table
    title: str
    scale: Literal["value", "sd", "z"]


@dataclass(slots=True)
class _Raster:
    """Block-averaged statistics of one field over its frames, ready for a figure."""

    panels: list[_Panel]
    values: dict[str, list[np.ndarray]]  # panel key -> one (ny, nx) raster per frame
    xs: np.ndarray
    ys: np.ndarray
    x_label: str
    y_label: str
    frame_label: str | None
    frames: list[str]
    block: int
    n_draws: int
    shape: tuple[int, int]
    masked: bool


def _budget(max_cells: int, columns: int) -> int:
    """Cells per raster so that ``columns`` values per cell fit the value budget."""
    return max(1, min(max_cells, VALUE_BUDGET // max(1, columns), ROW_BUDGET))


def _frame_levels(da: xr.DataArray, frame: str | None, max_frames: int) -> list[int]:
    if frame is None:
        return [0]
    n = da.sizes[frame]
    if n <= max_frames:
        return list(range(n))
    return [int(i) for i in np.unique(np.linspace(0, n - 1, max_frames).round().astype(int))]


# ------------------------------------------------------------------------------ figures


def _domains(
    n: int, columns: int, hgap: float, vgap: float
) -> list[tuple[list[float], list[float]]]:
    cols = max(1, min(columns, n))
    rows = math.ceil(n / cols)
    w = (1 - (cols - 1) * hgap) / cols
    h = (1 - (rows - 1) * vgap) / rows
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        x0 = c * (w + hgap)
        y1 = 1 - r * (h + vgap)
        out.append(([round(x0, 4), round(x0 + w, 4)], [round(y1 - h, 4), round(y1, 4)]))
    return out


def _axis_id(i: int) -> str:
    return "" if i == 0 else str(i + 1)


def _frame_controls(labels: list[str], prefix: str) -> dict[str, Any]:
    """``layout.sliders`` + ``updatemenus`` (play / pause) for animation frames."""
    anim = {
        "mode": "immediate",
        "frame": {"duration": 0, "redraw": True},
        "transition": {"duration": 0},
    }
    return {
        "sliders": [
            {
                "active": 0,
                "pad": {"t": 60},
                "currentvalue": {"prefix": f"{prefix}: "},
                "steps": [
                    {"label": lab, "method": "animate", "args": [[lab], anim]} for lab in labels
                ],
            }
        ],
        "updatemenus": [
            {
                "type": "buttons",
                "direction": "left",
                "showactive": False,
                "x": 0,
                "xanchor": "right",
                "y": 0,
                "yanchor": "top",
                "pad": {"t": 68, "r": 8},
                "buttons": [
                    {
                        "label": "Play",
                        "method": "animate",
                        "args": [
                            None,
                            {
                                "mode": "immediate",
                                "fromcurrent": True,
                                "frame": {"duration": 600, "redraw": True},
                                "transition": {"duration": 0},
                            },
                        ],
                    },
                    {"label": "Pause", "method": "animate", "args": [[None], anim]},
                ],
            }
        ],
    }


def _field_figure(
    r: _Raster,
    *,
    colormap: Colormap,
    origin: Literal["upper", "lower"],
    aspect: Literal["equal", "auto"],
    columns: int,
    width: int,
    height: int,
    overlay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    """Heatmap panels over one shared cell table (+ an optional arrow overlay per frame)."""
    yy, xx = np.meshgrid(r.ys, r.xs, indexing="ij")
    cells: dict[str, np.ndarray] = {"x": xx.ravel(), "y": yy.ravel()}
    for p in r.panels:
        for k, arr in enumerate(r.values[p.key]):
            cells[f"{p.key}__{k}"] = arr.ravel()
    table = pd.DataFrame({k: v.astype(np.float32) for k, v in cells.items()})
    datasets = {"cells": table}
    seq_sd: Colormap = "viridis" if colormap == "magma" else "magma"
    ranges = {
        "value": _range(
            [a for p in r.panels if p.scale == "value" for a in r.values[p.key]],
            symmetric=colormap == "diverging",
        ),
        "sd": _range([a for p in r.panels if p.scale == "sd" for a in r.values[p.key]]),
        "z": _range([a for p in r.panels if p.scale == "z" for a in r.values[p.key]], True),
    }
    scales = {"value": _scale(colormap), "sd": _scale(seq_sd), "z": _scale("diverging")}
    n = len(r.panels)
    cols = max(1, min(columns, n))
    rows = math.ceil(n / cols)
    doms = _domains(n, cols, hgap=0.1 if cols > 1 else 0.0, vgap=0.16 if rows > 1 else 0.0)
    layout: dict[str, Any] = {
        "height": rows * height + (140 if len(r.frames) > 1 else 50),
        "width": width,
        "margin": {"l": 56, "r": 56, "t": 36, "b": 48 + (100 if len(r.frames) > 1 else 0)},
        "showlegend": False,
        "annotations": [],
    }
    traces: list[dict[str, Any]] = []
    frame_traces: list[list[dict[str, Any]]] = [[] for _ in r.frames]
    overlay_on = next((i for i, p in enumerate(r.panels) if p.key == "mean"), 0)
    for i, (p, (dx, dy)) in enumerate(zip(r.panels, doms, strict=True)):
        aid = _axis_id(i)
        rng = ranges[p.scale]
        tr: dict[str, Any] = {
            "type": "heatmap",
            "x": {"$ref": "cells.x"},
            "y": {"$ref": "cells.y"},
            "z": {"$ref": f"cells.{p.key}__0"},
            "xaxis": f"x{aid}",
            "yaxis": f"y{aid}",
            "colorscale": scales[p.scale],
            "hoverongaps": False,
            "colorbar": {
                "x": dx[1] + 0.008,
                "xanchor": "left",
                "y": (dy[0] + dy[1]) / 2,
                "yanchor": "middle",
                "len": dy[1] - dy[0],
                "thickness": 10,
                "outlinewidth": 0,
            },
            "hovertemplate": (
                f"{r.x_label} %{{x:.4g}}<br>{r.y_label} %{{y:.4g}}<br>"
                f"{p.title} %{{z:.4g}}<extra></extra>"
            ),
        }
        if rng is not None:
            tr["zmin"], tr["zmax"] = rng
        traces.append(tr)
        for k in range(len(r.frames)):
            frame_traces[k].append({"type": "heatmap", "z": {"$ref": f"cells.{p.key}__{k}"}})
        xaxis: dict[str, Any] = {
            "domain": dx,
            "anchor": f"y{aid}",
            "showgrid": False,
            "zeroline": False,
        }
        yaxis: dict[str, Any] = {
            "domain": dy,
            "anchor": f"x{aid}",
            "showgrid": False,
            "zeroline": False,
        }
        if i // cols == rows - 1 or i + cols >= n:
            xaxis["title"] = {"text": r.x_label}
        if i % cols == 0:
            yaxis["title"] = {"text": r.y_label}
        else:
            yaxis["showticklabels"] = False  # the panels share the grid: labels once per row
        if origin == "upper":
            yaxis["autorange"] = "reversed"
        if aspect == "equal":
            yaxis["scaleanchor"] = f"x{aid}"
            yaxis["scaleratio"] = 1
        layout[f"xaxis{aid}"] = xaxis
        layout[f"yaxis{aid}"] = yaxis
        layout["annotations"].append(
            {
                "text": f"<b>{p.title}</b>" if p.scale != "value" or i == 0 else p.title,
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
    if overlay is not None:
        datasets["arrows"] = overlay["table"]
        aid = _axis_id(overlay_on)
        for width_px, colour in ((3.0, "rgb(20,20,20)"), (1.3, "rgb(250,250,250)")):
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": {"$ref": "arrows.x__0"},
                    "y": {"$ref": "arrows.y__0"},
                    "xaxis": f"x{aid}",
                    "yaxis": f"y{aid}",
                    "line": {"color": colour, "width": width_px},
                    "hoverinfo": "skip",
                    "showlegend": False,
                }
            )
            for k in range(len(r.frames)):
                frame_traces[k].append(
                    {
                        "type": "scatter",
                        "x": {"$ref": f"arrows.x__{k}"},
                        "y": {"$ref": f"arrows.y__{k}"},
                    }
                )
    fig: dict[str, Any] = {"data": traces, "layout": layout}
    if len(r.frames) > 1:
        idx = list(range(len(traces)))
        fig["frames"] = [
            {"name": lab, "data": frame_traces[k], "traces": idx} for k, lab in enumerate(r.frames)
        ]
        layout.update(_frame_controls(r.frames, r.frame_label or "frame"))
    return fig, datasets


# ----------------------------------------------------------------------- shared settings


class _FieldSettings(RecipeSettings):
    var: str | None = Field(None, description="Output to draw (default: a grid output)")
    group: GroupName = Field(
        "auto", description="Group to read (auto: posterior, predictions, posterior_predictive)"
    )
    y: str | None = Field(None, description="Raster row dim (default: the output's grid)")
    x: str | None = Field(None, description="Raster column dim (default: the output's grid)")
    frame: str | None = Field(None, description="Dim with one raster per level (slider)")
    select: dict[str, str | int | float] | None = Field(
        None, description="Pick one level of other dims, for example {species: 0}"
    )
    max_draws: int = Field(400, ge=1, le=10_000, description="Draws used for the statistics")
    max_cells: int = Field(
        40_000, ge=16, le=ROW_BUDGET, description="Cells per panel before block averaging"
    )
    max_frames: int = Field(24, ge=1, le=60)
    colormap: Colormap | None = Field(None, description="Default: the grid's, else viridis")
    log: bool | None = Field(None, description="Draw log10 of the value (default: the grid's)")
    origin: Literal["upper", "lower"] | None = Field(
        None, description="Where row 0 is (default: the grid's, else upper)"
    )
    aspect: Literal["equal", "auto"] = "equal"
    columns: int = Field(3, ge=1, le=6)
    width: int = Field(760, ge=80, le=2000)
    height: int = Field(280, ge=60, le=2000, description="Height of one row of panels")


@dataclass(slots=True)
class _FieldInput:
    st: xr.DataArray  # (sample, [frame], [component], y, x), thinned
    var: str
    group: str
    y: str
    x: str
    frame: str | None
    component: str | None
    grid: dict[str, Any]


def _field_input(
    inp: RecipeInput, s: _FieldSettings, *, component: str | None = None
) -> _FieldInput:
    need = [d for d in (s.y, s.x, s.frame, component) if d]
    da, group = _find_draws(inp, s.var, s.group, FIELD_GROUPS, need=need, min_dims=2)
    var = str(da.name)
    grid = grid_decl(inp, var, da)
    sdims = sample_dims(da)
    dims = [str(d) for d in da.dims if d not in sdims]
    y = s.y or grid.get("y")
    x = s.x or grid.get("x")
    frame = s.frame or (grid.get("frame") if grid.get("frame") in dims else None)
    comp = component
    if y is None or x is None:
        rest = [d for d in dims if d not in (frame, comp, *(s.select or {}))]
        if len(rest) != 2:
            raise RecipeDataError(
                f"{var!r} has dims {', '.join(dims)}: pass y=<row dim> and x=<column dim>"
            )
        y, x = y or rest[0], x or rest[1]
    for d in (y, x, frame, comp):
        if d is not None and d not in dims:
            raise RecipeDataError(f"{var!r} has no dim {d!r} (dims: {', '.join(dims)})")
    st = stack_samples(_select(da, s.select))
    st = _only(st, [y, x, frame, comp])
    order = [SAMPLE, *(d for d in (frame, comp) if d), y, x]
    st = _thin(st.transpose(*order), s.max_draws)
    return _FieldInput(st, var, group, str(y), str(x), frame, comp, grid)


def _settings_from_grid(
    s: _FieldSettings, grid: Mapping[str, Any]
) -> tuple[Colormap, bool, Literal["upper", "lower"]]:
    colormap = s.colormap or grid.get("colormap") or "viridis"
    if colormap not in ("viridis", "magma", "cividis", "gray", "diverging"):
        colormap = "viridis"
    log = s.log if s.log is not None else bool(grid.get("log", False))
    origin = s.origin or grid.get("origin") or "upper"
    return colormap, log, "lower" if origin == "lower" else "upper"


def _coords(fi: _FieldInput, inp: RecipeInput, k: int) -> tuple[np.ndarray, np.ndarray, str, str]:
    xs, _ = _numeric_coord(fi.st, fi.x)
    ys, _ = _numeric_coord(fi.st, fi.y)
    return (
        block_mean(xs, k, axes=1),
        block_mean(ys, k, axes=1),
        inp.dim_label(fi.x),
        inp.dim_label(fi.y),
    )


def _frame_names(fi: _FieldInput, levels: list[int]) -> list[str]:
    if fi.frame is None:
        return ["all"]
    vals = coord_values(fi.st, fi.frame)
    names: list[str] = []
    for i in levels:
        lab = _label(vals[i])
        while lab in names:
            lab += "'"
        names.append(lab)
    return names


def _size_note(r: _Raster) -> str:
    out = ""
    if r.block > 1:
        ny, nx = r.shape
        out += (
            f" The field has {ny} × {nx} cells. To keep the plot small, each square shows the "
            f"average of {r.block} × {r.block} cells."
        )
    if r.masked:
        out += " Empty cells have no value."
    return out


# --------------------------------------------------------------------------- field map


class FieldMapSettings(_FieldSettings):
    stats: list[FieldStat] = Field(
        default_factory=lambda: list[FieldStat](["mean", "sd"]),
        description="Panels: mean, median, sd, z (mean ÷ sd), q05, q95, draws",
    )
    n_draws: int = Field(3, ge=1, le=12, description="Panels for the 'draws' statistic")

    @field_validator("stats")
    @classmethod
    def _stats(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("give at least one statistic")
        return list(dict.fromkeys(v))


def _field_caption(
    var: str, stats: Sequence[str], n_draws: int, group: str, log: bool, r: _Raster
) -> str:
    what = "the posterior" if group == "posterior" else "the predictions"
    text = (
        f"Each panel shows one statistic of {var} for each cell, from {r.n_draws} draws of {what}."
    )
    if log:
        text += " The colours show log10 of the value."
    parts = {
        "mean": " The mean panel shows the average value.",
        "median": " The median panel shows the middle value of the draws.",
        "sd": " The sd panel shows the uncertainty. A high sd shows where the data do not fix "
        "the value.",
        "z": " The mean ÷ sd panel shows where the value is clear. Values far from zero are "
        "clear. Values near zero are not clear.",
        "q05": " The 5% panel shows a low value that the field is above with 95% probability.",
        "q95": " The 95% panel shows a high value that the field is below with 95% probability.",
        "draws": f" The {n_draws} draw panels show possible fields from the model. Compare them "
        "to see the uncertainty.",
    }
    for st in stats:
        text += parts[st]
    if r.frame_label is not None and len(r.frames) > 1:
        text += f" Use the slider or Play to show a different {r.frame_label}."
    return text + _size_note(r)


@recipe(
    "sci.field_map",
    title="Field map",
    settings=FieldMapSettings,
    persona="both",
    summary="Maps of a gridded output: the mean, the uncertainty and possible fields from the "
    "model.",
    tags=["sci"],
)
def field_map(inp: RecipeInput, s: FieldMapSettings) -> Figure:
    """Heatmap panels of posterior statistics of a gridded output, with a frame slider."""
    fi = _field_input(inp, s)
    colormap, log, origin = _settings_from_grid(s, fi.grid)
    st = fi.st
    n_s = st.sizes[SAMPLE]
    panels: list[_Panel] = []
    stat_names = [w for w in s.stats if w != "draws"]
    for w in s.stats:
        if w == "draws":
            picks = np.unique(np.linspace(0, n_s - 1, min(s.n_draws, n_s)).round().astype(int))
            panels += [_Panel(f"draw{j}", f"Draw {j + 1}", "value") for j in range(len(picks))]
        else:
            panels.append(
                _Panel(w, _STAT_TITLES[w], "sd" if w == "sd" else "z" if w == "z" else "value")
            )
    picks = np.unique(np.linspace(0, n_s - 1, min(s.n_draws, n_s)).round().astype(int))
    levels = _frame_levels(st, fi.frame, s.max_frames)
    ny, nx = st.sizes[fi.y], st.sizes[fi.x]
    k = block_factor(ny, nx, _budget(s.max_cells, 2 + len(panels) * len(levels)))
    values: dict[str, list[np.ndarray]] = {p.key: [] for p in panels}
    masked = False
    for lv in levels:
        sub = st.isel({fi.frame: lv}) if fi.frame else st
        draws = np.asarray(sub.values, dtype=float)
        if log:
            draws = _to_log(draws)
        masked = masked or bool(np.isnan(draws).all(axis=0).any())
        got = _stats(draws, stat_names)
        for j, pi in enumerate(picks if "draws" in s.stats else []):
            got[f"draw{j}"] = draws[pi]
        for p in panels:
            values[p.key].append(block_mean(got[p.key], k))
    xs, ys, xl, yl = _coords(fi, inp, k)
    r = _Raster(
        panels=panels,
        values=values,
        xs=xs,
        ys=ys,
        x_label=xl,
        y_label=yl,
        frame_label=inp.dim_label(fi.frame) if fi.frame else None,
        frames=_frame_names(fi, levels),
        block=k,
        n_draws=n_s,
        shape=(ny, nx),
        masked=masked,
    )
    fig, datasets = _field_figure(
        r,
        colormap=colormap,
        origin=origin,
        aspect=s.aspect,
        columns=s.columns,
        width=s.width,
        height=s.height,
    )
    caption = _field_caption(fi.var, s.stats, len(picks), fi.group, log, r)
    if fi.frame and st.sizes[fi.frame] > len(levels):
        caption += f" The slider shows {len(levels)} of the {st.sizes[fi.frame]} levels."
    return Figure(chart=fig, datasets=datasets, caption=caption, title=f"Field map of {fi.var}")


# -------------------------------------------------------------------------- vector field


class VectorFieldSettings(_FieldSettings):
    component: str | None = Field(
        None, description="Dim of length 2 with the (x, y) components (default: the grid's)"
    )
    stats: list[VectorStat] = Field(
        default_factory=lambda: list[VectorStat](["mean", "sd"]),
        description="Panels of the vector length: mean, sd, q05, q95",
    )
    arrows: int = Field(20, ge=2, le=60, description="Arrows along the longer side")

    @field_validator("stats")
    @classmethod
    def _stats(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("give at least one statistic")
        return list(dict.fromkeys(v))


def _arrow_paths(
    u: np.ndarray, v: np.ndarray, xs: np.ndarray, ys: np.ndarray, scale: float
) -> tuple[np.ndarray, np.ndarray]:
    """Polyline points (tail, tip, wing, tip, wing, gap) of one arrow per cell, centred."""
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    du, dv = u.ravel() * scale, v.ravel() * scale
    cx, cy = xx.ravel(), yy.ravel()
    tx, ty = cx - du / 2, cy - dv / 2
    hx, hy = cx + du / 2, cy + dv / 2
    ang = np.arctan2(dv, du)
    head = 0.3 * np.hypot(du, dv)
    w1x, w1y = hx - head * np.cos(ang - 0.45), hy - head * np.sin(ang - 0.45)
    w2x, w2y = hx - head * np.cos(ang + 0.45), hy - head * np.sin(ang + 0.45)
    gap = np.full_like(cx, np.nan)
    ok = np.isfinite(du) & np.isfinite(dv)
    px = np.stack([tx, hx, w1x, hx, w2x, gap], axis=1)[ok].ravel()
    py = np.stack([ty, hy, w1y, hy, w2y, gap], axis=1)[ok].ravel()
    return px, py


@recipe(
    "sci.vector_field",
    title="Vector field",
    settings=VectorFieldSettings,
    persona="both",
    summary="The length of a vector field as colours, arrows of the mean direction and the "
    "uncertainty of the length.",
    tags=["sci"],
)
def vector_field(inp: RecipeInput, s: VectorFieldSettings) -> Figure:
    """Heatmap of the vector length with a quiver of the mean vector, plus its sd."""
    da, _ = _find_draws(inp, s.var, s.group, FIELD_GROUPS, min_dims=3)
    grid = grid_decl(inp, str(da.name), da)
    comp = s.component or grid.get("component")
    if comp is None:
        comp = next(
            (str(d) for d in da.dims if da.sizes[d] == 2 and d not in sample_dims(da)), None
        )
    if comp is None or comp not in da.dims or da.sizes[comp] != 2:
        raise RecipeDataError("a vector field needs a component dim of length 2: pass component=")
    fi = _field_input(inp, s.model_copy(update={"var": str(da.name)}), component=comp)
    colormap, log, origin = _settings_from_grid(s, fi.grid)
    st = fi.st
    panels = [_Panel(w, _STAT_TITLES[w], "sd" if w == "sd" else "value") for w in s.stats]
    levels = _frame_levels(st, fi.frame, s.max_frames)
    ny, nx = st.sizes[fi.y], st.sizes[fi.x]
    k = block_factor(ny, nx, _budget(s.max_cells, 2 + len(panels) * len(levels)))
    ka = max(1, math.ceil(max(ny, nx) / s.arrows))
    values: dict[str, list[np.ndarray]] = {p.key: [] for p in panels}
    mean_uv: list[tuple[np.ndarray, np.ndarray]] = []
    masked = False
    for lv in levels:
        sub = st.isel({fi.frame: lv}) if fi.frame else st
        draws = np.asarray(sub.transpose(SAMPLE, comp, fi.y, fi.x).values, dtype=float)
        mag = np.hypot(draws[:, 0], draws[:, 1])
        if log:
            mag = _to_log(mag)
        masked = masked or bool(np.isnan(mag).all(axis=0).any())
        got = _stats(mag, [p.key for p in panels])
        for p in panels:
            values[p.key].append(block_mean(got[p.key], k))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mu = np.nanmean(draws, axis=0)
        mean_uv.append((block_mean(mu[0], ka), block_mean(mu[1], ka)))
    xs, ys, xl, yl = _coords(fi, inp, k)
    xs_full, _ = _numeric_coord(st, fi.x)
    ys_full, _ = _numeric_coord(st, fi.y)
    axs, ays = block_mean(xs_full, ka, axes=1), block_mean(ys_full, ka, axes=1)
    step = min(
        float(np.nanmedian(np.abs(np.diff(axs)))) if len(axs) > 1 else 1.0,
        float(np.nanmedian(np.abs(np.diff(ays)))) if len(ays) > 1 else 1.0,
    )
    biggest = max(
        (float(np.nanmax(np.hypot(u, v))) for u, v in mean_uv if np.isfinite(np.hypot(u, v)).any()),
        default=0.0,
    )
    scale = 0.9 * step / biggest if biggest > 0 else 0.0
    paths = [_arrow_paths(u, v, axs, ays, scale) for u, v in mean_uv]
    n_pts = max((len(px) for px, _ in paths), default=0)
    arrows: dict[str, np.ndarray] = {}
    for j, (px, py) in enumerate(paths):
        pad = np.full(n_pts - len(px), np.nan)
        arrows[f"x__{j}"] = np.concatenate([px, pad]).astype(np.float32)
        arrows[f"y__{j}"] = np.concatenate([py, pad]).astype(np.float32)
    r = _Raster(
        panels=panels,
        values=values,
        xs=xs,
        ys=ys,
        x_label=xl,
        y_label=yl,
        frame_label=inp.dim_label(fi.frame) if fi.frame else None,
        frames=_frame_names(fi, levels),
        block=k,
        n_draws=st.sizes[SAMPLE],
        shape=(ny, nx),
        masked=masked,
    )
    fig, datasets = _field_figure(
        r,
        colormap=colormap,
        origin=origin,
        aspect=s.aspect,
        columns=s.columns,
        width=s.width,
        height=s.height,
        overlay={"table": pd.DataFrame(arrows)} if n_pts else None,
    )
    on = "mean" if "mean" in s.stats else _STAT_TITLES[s.stats[0]].lower()
    caption = (
        f"The colours show the length of the {fi.var} vector in each cell, from "
        f"{r.n_draws} draws of the model."
    )
    if log:
        caption += " The colours show log10 of the length."
    if n_pts:
        caption += (
            f" The arrows on the {on} panel show the mean direction. A longer arrow shows a "
            "larger mean vector."
        )
    if "sd" in s.stats:
        caption += " The sd panel shows where the data do not fix the length of the vector."
    if "q05" in s.stats or "q95" in s.stats:
        caption += " The 5% and 95% panels show the limits of the 90% interval of the length."
    if r.frame_label is not None and len(r.frames) > 1:
        caption += f" Use the slider or Play to show a different {r.frame_label}."
    caption += _size_note(r)
    return Figure(chart=fig, datasets=datasets, caption=caption, title=f"Vector field of {fi.var}")


# ------------------------------------------------------------------------ curve helpers


def _level_colours(n: int, colormap: Colormap) -> list[str]:
    import plotly.colors as pc

    if n == 1:
        return [ESTIMATE]
    scale = _scale("viridis" if colormap in ("gray", "diverging") else colormap)
    # stop before the light end of the scale: it has no contrast on the light card
    return [str(c) for c in pc.sample_colorscale(scale, list(np.linspace(0.0, 0.85, n)))]


def _x_column(da: xr.DataArray, dim: str) -> tuple[np.ndarray, bool]:
    vals = coord_values(da, dim)
    if np.issubdtype(np.asarray(vals).dtype, np.datetime64):
        return np.asarray(vals), True
    if np.issubdtype(np.asarray(vals).dtype, np.number):
        return np.asarray(vals, dtype=float), False
    return np.arange(da.sizes[dim], dtype=float), False


def _observed(
    inp: RecipeInput,
    var: str,
    explicit: str | None,
    ref: xr.DataArray,
    keep: Sequence[str],
    select: Mapping[str, Any] | None,
) -> tuple[xr.DataArray | None, str | None]:
    """Observed values on ``ref``'s coords over ``keep`` (None when they do not line up)."""
    obs_ds = inp.find(*OBS_GROUPS)
    name = observed_name(obs_ds, var, explicit)
    if obs_ds is None or name is None:
        return None, None
    o = obs_ds[name]
    sel = {d: v for d, v in (select or {}).items() if d in o.dims}
    o = _select(o, sel)
    if any(d not in keep for d in o.dims if o.sizes[d] > 1) or not set(o.dims) & set(keep):
        return None, None
    o = o.squeeze([d for d in o.dims if d not in keep], drop=True)
    for d in o.dims:
        if d in ref.coords and d in o.coords:
            o = o.reindex({d: ref[d].values})
    o = o.expand_dims([d for d in keep if d not in o.dims]).broadcast_like(ref)
    return o.transpose(*keep), name


def _band_text(levels: Sequence[float]) -> str:
    lv = " and ".join(pct(x) for x in sorted(levels))
    return f"the {lv} interval" if len(levels) == 1 else f"the {lv} intervals"


# ------------------------------------------------------------------------ profile bands


class ProfileBandsSettings(RecipeSettings):
    var: str | None = None
    group: GroupName = Field(
        "auto", description="Group to read (auto: predictions, posterior_predictive, posterior)"
    )
    observed_var: str | None = Field(None, description="Observed variable (default: same name)")
    along: str | None = Field(None, description="Dim along the x axis (default: the grid's x)")
    by: str | None = Field(None, description="Dim with one curve per level (default: the other)")
    select: dict[str, str | int | float] | None = None
    levels: list[float] = Field(default_factory=lambda: [0.5, 0.9], max_length=3)
    point: Literal["median", "mean"] = "median"
    max_curves: int = Field(12, ge=1, le=24)
    max_draws: int = Field(1000, ge=1, le=20_000)
    colormap: Colormap = "viridis"
    log: bool = Field(False, description="Log scale for the y axis")
    width: int = Field(640, ge=80, le=2000)
    height: int = Field(360, ge=60, le=2000)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        return check_levels(v)


def _curve_dims(
    da: xr.DataArray, grid: Mapping[str, Any], along: str | None, by: str | None, select: Any
) -> tuple[str, str | None]:
    dims = [str(d) for d in da.dims if d not in (SAMPLE, *(select or {}))]
    if along is None:
        g = grid.get("x")
        t = time_dim(da)
        along = g if g in dims else next((d for d in dims if d != t), dims[0] if dims else None)
    if along is None or along not in dims:
        raise RecipeDataError(f"no dim {along!r} to plot along (dims: {', '.join(dims)})")
    if by is None:
        rest = [d for d in dims if d != along]
        g = grid.get("frame") or grid.get("y")
        by = g if g in rest else (rest[0] if len(rest) == 1 else None)
        if len(rest) > 1 and by is None:
            raise RecipeDataError(
                f"pass by=<dim> (one of {', '.join(rest)}) and select= for the other dims"
            )
    elif by not in dims or by == along:
        raise RecipeDataError(f"no dim {by!r} for the curves (dims: {', '.join(dims)})")
    return along, by


@recipe(
    "sci.profile_bands",
    title="Profiles with bands",
    settings=ProfileBandsSettings,
    persona="both",
    summary="The model estimate along one dim with 50% and 90% bands, one curve per level of a "
    "second dim and the actual values.",
    tags=["sci"],
)
def profile_bands(inp: RecipeInput, s: ProfileBandsSettings) -> Figure:
    """Median + bands along ``along``, one curve per level of ``by``, with a level slider."""
    da, _ = _find_draws(inp, s.var, s.group, CURVE_GROUPS)
    var = str(da.name)
    grid = grid_decl(inp, var, da)
    st = stack_samples(_select(da, s.select))
    along, by = _curve_dims(st, grid, s.along, s.by, None)
    st = _only(st, [along, by])
    if by is None:
        st = st.expand_dims({"_one": [0]})
    keep = [along, by or "_one"]
    st = _thin(st.transpose(SAMPLE, keep[1], keep[0]), s.max_draws)
    n_by = st.sizes[keep[1]]
    picks = np.unique(np.linspace(0, n_by - 1, min(n_by, s.max_curves)).round().astype(int))
    st = st.isel({keep[1]: picks})
    draws = np.asarray(st.values, dtype=float)  # (sample, by, along)
    x, temporal = _x_column(st, along)
    obs, obs_name = _observed(
        inp, var, s.observed_var, st.isel({SAMPLE: 0}, drop=True), [keep[1], along], s.select
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        point = np.nanmedian(draws, 0) if s.point == "median" else np.nanmean(draws, 0)
        bands = {
            lv: (np.nanquantile(draws, (1 - lv) / 2, 0), np.nanquantile(draws, (1 + lv) / 2, 0))
            for lv in s.levels
        }
    names = [_label(v) for v in coord_values(st, by)] if by is not None else [var]
    by_label = inp.dim_label(by) if by is not None else ""
    cols: dict[str, Any] = {"x": x}
    for i in range(len(names)):
        cols[f"p{i}"] = point[i].astype(np.float32)
        for lv, lo_c, hi_c in interval_columns(s.levels):
            cols[f"{lo_c}_{i}"] = bands[lv][0][i].astype(np.float32)
            cols[f"{hi_c}_{i}"] = bands[lv][1][i].astype(np.float32)
        if obs is not None:
            cols[f"o{i}"] = np.asarray(obs.values[i], dtype=np.float32)
    table = pd.DataFrame(cols)
    colours = _level_colours(len(names), s.colormap)
    traces: list[dict[str, Any]] = []
    kind: list[str] = []  # band / point / obs, per trace
    owner: list[int] = []
    single = len(names) == 1
    for i, (name, colour) in enumerate(zip(names, colours, strict=True)):
        grp = f"c{i}"
        label = f"{by_label} = {name}" if by is not None else name
        for j, (_lv, lo_c, hi_c) in enumerate(interval_columns(s.levels)):
            # a palette hex (not rgba) so the plot sandbox swaps it in dark mode
            fill = {
                "fill": "tonexty",
                "fillcolor": BAND if single else colour,
                "opacity": round(0.16 + 0.12 * j, 3),
            }
            for col, extra in ((lo_c, {}), (hi_c, fill)):
                traces.append(
                    {
                        "type": "scatter",
                        "mode": "lines",
                        "x": {"$ref": "profile.x"},
                        "y": {"$ref": f"profile.{col}_{i}"},
                        "line": {"width": 0, "color": colour},
                        "legendgroup": grp,
                        "showlegend": False,
                        "hoverinfo": "skip",
                        "visible": single,
                        **extra,
                    }
                )
                kind.append("band")
                owner.append(i)
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "name": label,
                "x": {"$ref": "profile.x"},
                "y": {"$ref": f"profile.p{i}"},
                "line": {"color": colour, "width": 2},
                "legendgroup": grp,
                "showlegend": not single,
                "hovertemplate": f"{label}<br>%{{x}}: %{{y:.4g}}<extra></extra>",
            }
        )
        kind.append("point")
        owner.append(i)
        if obs is not None:
            traces.append(
                {
                    "type": "scatter",
                    "mode": "markers",
                    "name": f"{label} (actual)",
                    "x": {"$ref": "profile.x"},
                    "y": {"$ref": f"profile.o{i}"},
                    "marker": {
                        "color": OBSERVED if single else colour,
                        "size": 5,
                        "line": {"color": OBSERVED, "width": 0.5},
                    },
                    "legendgroup": grp,
                    "showlegend": False,
                    "hovertemplate": f"{label} (actual)<br>%{{x}}: %{{y:.4g}}<extra></extra>",
                }
            )
            kind.append("obs")
            owner.append(i)
    layout: dict[str, Any] = {
        "height": s.height + (90 if not single else 0),
        "width": s.width,
        "margin": {"l": 56, "r": 16, "t": 24, "b": 48 + (80 if not single else 0)},
        "xaxis": {
            "title": {"text": inp.dim_label(along)},
            **({"type": "date"} if temporal else {}),
        },
        "yaxis": {"title": {"text": obs_name or var}, **({"type": "log"} if s.log else {})},
        "legend": {"title": {"text": by_label}} if by is not None else {},
        "hovermode": "closest",
    }
    if not single:
        steps = [
            {
                "label": "All",
                "method": "restyle",
                "args": [{"visible": [k != "band" for k in kind]}],
            }
        ]
        for i, name in enumerate(names):
            steps.append(
                {
                    "label": name,
                    "method": "restyle",
                    "args": [{"visible": [o == i for o in owner]}],
                }
            )
        layout["sliders"] = [
            {
                "active": 0,
                "pad": {"t": 48},
                "currentvalue": {"prefix": f"{by_label}: "},
                "steps": steps,
            }
        ]
    point_word = "median" if s.point == "median" else "mean"
    bands_txt = _band_text(s.levels)
    if single:
        caption = (
            f"The line is the {point_word} of {var} along {inp.dim_label(along)}. "
            f"The shaded bands are {bands_txt}."
        )
    else:
        caption = (
            f"Each line is the {point_word} of {var} along {inp.dim_label(along)} for one "
            f"{by_label}. Use the slider to show one {by_label} with its bands. The bands are "
            f"{bands_txt}."
        )
        if n_by > len(names):
            caption += f" The plot shows {len(names)} of the {n_by} levels of {by_label}."
    if obs is not None:
        caption += " The dots are the actual values."
        lo, hi = bands[max(s.levels)]
        seen = np.isfinite(obs.values)
        if seen.any():
            inside = float(((obs.values >= lo) & (obs.values <= hi))[seen].mean())
            caption += f" {pct(inside)} of the actual values are in the {pct(max(s.levels))} band."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets={"profile": table},
        caption=caption,
        title=f"Profiles of {var}",
    )


# ------------------------------------------------------------------------------ ensemble


class EnsembleSettings(RecipeSettings):
    var: str | None = None
    group: GroupName = Field(
        "auto", description="Group to read (auto: predictions, posterior_predictive, posterior)"
    )
    observed_var: str | None = None
    along: str | None = Field(None, description="Dim along the x axis (default: time-like)")
    select: dict[str, str | int | float] | None = None
    n_draws: int = Field(40, ge=1, le=300, description="Draws in the spaghetti")
    levels: list[float] = Field(default_factory=lambda: [0.9], max_length=3)
    point: Literal["median", "mean", "none"] = "median"
    max_draws: int = Field(2000, ge=1, le=20_000, description="Draws used for the band")
    seed: int = 0
    log: bool = Field(False, description="Log scale for the y axis")
    width: int = Field(640, ge=80, le=2000)
    height: int = Field(320, ge=60, le=2000)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        return check_levels(v)


@recipe(
    "sci.ensemble",
    title="Ensemble of draws",
    settings=EnsembleSettings,
    persona="both",
    summary="Possible trajectories from the model with the 90% band and the actual values.",
    tags=["sci"],
)
def ensemble(inp: RecipeInput, s: EnsembleSettings) -> Figure:
    """Spaghetti of posterior draws along one dim, the band, the median and observations."""
    da, _ = _find_draws(inp, s.var, s.group, CURVE_GROUPS, prefer_grid=False)
    var = str(da.name)
    grid = grid_decl(inp, var, da)
    st = stack_samples(_select(da, s.select))
    dims = [str(d) for d in st.dims if d != SAMPLE]
    along = s.along or time_dim(st) or (grid.get("x") if grid.get("x") in dims else None)
    along = along or (dims[0] if dims else None)
    if along is None or along not in dims:
        raise RecipeDataError(f"no dim {along!r} to plot along (dims: {', '.join(dims)})")
    st = _thin(_only(st, [along]).transpose(SAMPLE, along), s.max_draws)
    draws = np.asarray(st.values, dtype=float)  # (sample, along)
    n_s = draws.shape[0]
    x, temporal = _x_column(st, along)
    rng = np.random.default_rng(s.seed)
    picks = np.sort(rng.choice(n_s, size=min(s.n_draws, n_s), replace=False))
    gap_x: Any = np.datetime64("NaT") if temporal else np.nan
    sx = np.concatenate([np.append(x, gap_x) for _ in picks]) if len(picks) else x[:0]
    sy = np.concatenate([np.append(draws[p], np.nan) for p in picks]).astype(np.float32)
    spaghetti = pd.DataFrame({"x": sx, "y": sy})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        cols: dict[str, Any] = {"x": x}
        for lv, lo_c, hi_c in interval_columns(s.levels):
            cols[lo_c] = np.nanquantile(draws, (1 - lv) / 2, 0).astype(np.float32)
            cols[hi_c] = np.nanquantile(draws, (1 + lv) / 2, 0).astype(np.float32)
        if s.point != "none":
            pt = np.nanmedian(draws, 0) if s.point == "median" else np.nanmean(draws, 0)
            cols["point"] = pt.astype(np.float32)
    obs, obs_name = _observed(
        inp, var, s.observed_var, st.isel({SAMPLE: 0}, drop=True), [along], s.select
    )
    if obs is not None:
        cols["observed"] = np.asarray(obs.values, dtype=np.float32)
    summary = pd.DataFrame(cols)
    traces: list[dict[str, Any]] = []
    for j, (_lv, lo_c, hi_c) in enumerate(interval_columns(s.levels)):
        for col, extra in (
            (lo_c, {}),
            (hi_c, {"fill": "tonexty", "fillcolor": BAND, "opacity": round(0.14 + 0.1 * j, 3)}),
        ):
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": {"$ref": "summary.x"},
                    "y": {"$ref": f"summary.{col}"},
                    "line": {"width": 0, "color": BAND},
                    "showlegend": False,
                    "hoverinfo": "skip",
                    **extra,
                }
            )
    traces.append(
        {
            "type": "scatter",
            "mode": "lines",
            "name": "draws",
            "x": {"$ref": "spaghetti.x"},
            "y": {"$ref": "spaghetti.y"},
            "line": {"color": BAND, "width": 1},
            "opacity": 0.35 if len(picks) > 10 else 0.6,
            "hoverinfo": "skip",
            "showlegend": False,
        }
    )
    if s.point != "none":
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "name": s.point,
                "x": {"$ref": "summary.x"},
                "y": {"$ref": "summary.point"},
                "line": {"color": ESTIMATE, "width": 2.5},
                "showlegend": False,
                "hovertemplate": f"{s.point}<br>%{{x}}: %{{y:.4g}}<extra></extra>",
            }
        )
    if obs is not None:
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": "actual",
                "x": {"$ref": "summary.x"},
                "y": {"$ref": "summary.observed"},
                "marker": {"color": OBSERVED, "size": 5},
                "showlegend": False,
                "hovertemplate": "actual<br>%{x}: %{y:.4g}<extra></extra>",
            }
        )
    layout: dict[str, Any] = {
        "height": s.height,
        "width": s.width,
        "margin": {"l": 56, "r": 16, "t": 24, "b": 48},
        "xaxis": {
            "title": {"text": inp.dim_label(along)},
            **({"type": "date"} if temporal else {}),
        },
        "yaxis": {
            "title": {"text": obs_name or var},
            "linecolor": MUTED,
            **({"type": "log"} if s.log else {}),
        },
        "hovermode": "closest",
        "showlegend": False,
    }
    caption = (
        f"The thin lines are {len(picks)} draws from the model. Each line is one possible "
        f"path of {var} along {inp.dim_label(along)}. The shaded band is {_band_text(s.levels)} "
        f"of all {n_s} draws."
    )
    if s.point != "none":
        caption += f" The thick line is the {s.point}."
    if obs is not None:
        caption += " The dots are the actual values."
        lv, lo_c, hi_c = interval_columns(s.levels)[0]
        seen = summary["observed"].notna()
        if seen.any():
            o = summary.loc[seen]
            inside = float(((o["observed"] >= o[lo_c]) & (o["observed"] <= o[hi_c])).mean())
            caption += f" {pct(inside)} of the actual values are in the {pct(lv)} band."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets={"summary": summary, "spaghetti": spaghetti},
        caption=caption,
        title=f"Ensemble of {var}",
    )


def sci_recipes() -> list[Recipe[Any]]:
    """The scientific-model pack (entry point ``mapi.plots: sci``)."""
    from mapi_plots.packs.sci_images import sci_image_recipes

    return [field_map, vector_field, profile_bands, ensemble, *sci_image_recipes()]
