"""The :class:`~mapi_spec.views.FieldsView` of the outputs that declare a ``grid``: quantised
mean / sd / 5% / 95% / draw rasters per frame, for the Model Explorer ``fields`` section and
for score runs (``ui/fields.json``).

The data comes from a :data:`FieldSource` (an output name to its values, in dataset names);
grid dims declared under canonical names are matched through the bundle's binding.
"""

from __future__ import annotations

import itertools
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.metrics import point_estimate
from mapi_spec.capabilities import OutputDecl, Representation
from mapi_spec.views import (
    MAX_FIELD_CELLS,
    MAX_FIELD_DRAWS,
    MAX_FIELD_FRAMES,
    MAX_FIELD_PIXELS,
    MAX_FIELDS,
    FieldEntry,
    FieldFrame,
    FieldRaster,
    FieldsView,
)

from mapi_runner.worker.explore_data import SectionSkipped, jsonable
from mapi_runner.worker.rasters import arrows as _arrows
from mapi_runner.worker.rasters import block_labels as _block_labels
from mapi_runner.worker.rasters import block_mean, encode_raster
from mapi_runner.worker.rasters import even as _even

__all__ = ["MAX_FIELD_SAMPLES", "FieldSource", "field_dim", "field_grids", "fields_view"]

FieldSource = Callable[[str], xr.DataArray | None]
"""An output name to its values (draws or another representation), or ``None``."""

MAX_FIELD_SAMPLES: Final = 400
"""Posterior draws behind the field statistics (more are thinned evenly)."""
_STAT_RASTERS: Final = 4  # mean, sd, q05, q95


def _labels(da: xr.DataArray, dim: str) -> list[Any]:
    return (
        [jsonable(v) for v in np.asarray(da[dim].values)]
        if dim in da.coords
        else list(range(da.sizes[dim]))
    )


def field_dim(bundle: Bundle, da: xr.DataArray, name: str | None) -> str | None:
    """The dim of ``da`` a grid declares as ``name`` (canonical or dataset name), if any."""
    if name is None:
        return None
    if name in da.dims:
        return name
    alias = bundle.binding.to_dataset(name)
    return alias if alias in da.dims else None


@dataclass
class _Grid:
    """One field to encode: its data as ``(sample?, frame, component, y, x)``."""

    output: str
    decl: OutputDecl
    facet: dict[str, Any]
    values: np.ndarray  # (S, F, C, H, W): S = 1 for point-like statistics
    stats: dict[str, np.ndarray] | None  # non-draw representations: stat -> (F, C, H, W)
    y: str
    x: str
    frame: str | None
    component: str | None
    y_labels: list[Any]
    x_labels: list[Any]
    frame_labels: list[Any]


def field_grids(bundle: Bundle, source: FieldSource) -> tuple[list[_Grid], list[str]]:
    """The fields to encode (one per output and facet) and the outputs left out."""
    man = bundle.model.manifest
    rep = man.capabilities.representation
    decls = {n: d for n, d in man.outputs.items() if d.grid is not None}
    if not decls:
        raise SectionSkipped("this model has no outputs on a 2-D grid (images, maps or fields)")
    out: list[_Grid] = []
    omitted: list[str] = []
    for name, decl in decls.items():
        g = decl.grid
        assert g is not None
        da = source(name)
        if da is None or not np.issubdtype(da.dtype, np.number):
            omitted.append(name)
            continue
        y, x = field_dim(bundle, da, g.y), field_dim(bundle, da, g.x)
        frame, comp = field_dim(bundle, da, g.frame), field_dim(bundle, da, g.component)
        if y is None or x is None or (g.component is not None and comp is None):
            omitted.append(name)
            continue
        if comp is not None and da.sizes[comp] != 2:
            omitted.append(name)
            continue
        draws = "chain" in da.dims and "draw" in da.dims
        rep_dims = {"chain", "draw"} if draws else {"quantile", "bound", "moment"} & set(da.dims)
        grid_dims = [d for d in (frame, comp, y, x) if d is not None]
        other = [str(d) for d in da.dims if d not in grid_dims and d not in rep_dims]
        levels = [_labels(da, d) for d in other]
        for idx in itertools.product(*[range(len(lv)) for lv in levels]):
            if len(out) >= MAX_FIELDS:
                omitted.append(name if not other else f"{name}[{idx}]")
                break
            facet = {d: lv[i] for d, lv, i in zip(other, levels, idx, strict=True)}
            sub = da.isel(dict(zip(other, idx, strict=True))) if other else da
            if frame is None:
                sub = sub.expand_dims("__frame")
            if comp is None:
                sub = sub.expand_dims("__comp")
            f_dim, c_dim = frame or "__frame", comp or "__comp"
            stats: dict[str, np.ndarray] | None = None
            if draws:
                s = sub.stack(__sample=("chain", "draw"))
                vals = np.asarray(s.transpose("__sample", f_dim, c_dim, y, x).values, float)
                vals = vals[_even(vals.shape[0], MAX_FIELD_SAMPLES)]
            else:
                order = (f_dim, c_dim, y, x)
                mean = point_estimate(sub, rep).transpose(*order)
                vals = np.asarray(mean.values, dtype=float)[None]
                stats = {}
                if comp is None and rep is not Representation.POINT:
                    from mapi_runner.worker.modelview import _band

                    band = _band(sub, rep, 0.9)
                    if band is not None:
                        stats["q05"] = np.asarray(band[0].transpose(*order).values, float)
                        stats["q95"] = np.asarray(band[1].transpose(*order).values, float)
                    if rep is Representation.MOMENTS:
                        sd = sub.sel(moment="sd", drop=True).transpose(*order)
                        stats["sd"] = np.asarray(sd.values, dtype=float)
                    elif "q05" in stats:  # a normal approximation of the spread
                        stats["sd"] = (stats["q95"] - stats["q05"]) / (2 * 1.6448536269514722)
            out.append(
                _Grid(
                    output=name,
                    decl=decl,
                    facet=facet,
                    values=vals,
                    stats=stats,
                    y=y,
                    x=x,
                    frame=frame,
                    component=comp,
                    y_labels=_labels(da, y),
                    x_labels=_labels(da, x),
                    frame_labels=_labels(da, frame) if frame is not None else [None],
                )
            )
    return out, omitted


def _plan(g: _Grid, budget: int) -> tuple[int, int, list[int], int]:
    """Block factor, number of draw rasters and frames that fit ``budget`` cells."""
    h, w = g.values.shape[-2:]
    frames = _even(len(g.frame_labels), MAX_FIELD_FRAMES)
    f = max(1, -(-max(h, w) // MAX_FIELD_PIXELS))
    if g.stats is None:
        n_stats, max_draws = _STAT_RASTERS, min(MAX_FIELD_DRAWS, g.values.shape[0])
    else:
        n_stats, max_draws = 1 + len(g.stats), 0
    while True:
        cells = -(-h // f) * -(-w // f) * len(frames)
        spare = budget // cells - n_stats if cells else 0
        if spare >= 0:
            return f, max(0, min(max_draws, spare)), frames, len(g.frame_labels) - len(frames)
        f += 1


def _entry(g: _Grid, budget: int) -> FieldEntry:
    decl, grid = g.decl, g.decl.grid
    assert grid is not None
    f, n_draws, frames, omitted = _plan(g, budget)
    enc = "log10" if grid.log else "linear"
    vector = g.component is not None
    s_total = g.values.shape[0]
    picks = _even(s_total, n_draws) if g.stats is None and n_draws else []
    out: list[FieldFrame] = []
    for i in frames:
        comps = block_mean(g.values[:, i], f, f)  # (S, C, h, w)
        mag = np.sqrt(np.sum(comps**2, axis=1)) if vector else comps[:, 0]  # (S, h, w)
        sd: FieldRaster | None
        q05: FieldRaster | None
        q95: FieldRaster | None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(mag, axis=0)
            if g.stats is None and s_total > 1:
                sd = encode_raster(np.nanstd(mag, axis=0, ddof=1), enc)
                q = np.nanquantile(mag, [0.05, 0.95], axis=0)
                q05, q95 = encode_raster(q[0], enc), encode_raster(q[1], enc)
            elif g.stats is not None:
                st = {k: block_mean(v[i, 0], f, f) for k, v in g.stats.items()}
                sd = encode_raster(st["sd"], enc) if "sd" in st else None
                q05 = encode_raster(st["q05"], enc) if "q05" in st else None
                q95 = encode_raster(st["q95"], enc) if "q95" in st else None
            else:
                sd = q05 = q95 = None
        out.append(
            FieldFrame(
                label=jsonable(g.frame_labels[i]) if g.frame is not None else None,
                mean=encode_raster(mean, enc),
                sd=sd,
                q05=q05,
                q95=q95,
                draws=[encode_raster(mag[k], enc) for k in picks],
                arrows=_arrows(np.nanmean(comps, axis=0)) if vector else None,
            )
        )
    h, w = g.values.shape[-2:]
    return FieldEntry(
        output=g.output[:128],
        units=decl.units,
        description=decl.description,
        facet=g.facet,
        kind="vector" if vector else "scalar",
        y_dim=g.y,
        x_dim=g.x,
        frame_dim=g.frame,
        component_dim=g.component,
        height=-(-h // f),
        width=-(-w // f),
        source_height=h,
        source_width=w,
        downsampled=f > 1,
        y_coords=_block_labels(g.y_labels, f),
        x_coords=_block_labels(g.x_labels, f),
        colormap=grid.colormap,
        log=grid.log,
        origin=grid.origin,
        encoding=enc,
        n_draws=s_total if g.stats is None else 0,
        frames=out,
        omitted_frames=omitted,
    )


def fields_view(bundle: Bundle, source: FieldSource, *, caption: str | None = None) -> FieldsView:
    """Every grid output of ``source`` as a FieldsView, within the ``MAX_FIELD_*`` budgets.

    Raises :class:`SectionSkipped` when the model declares no grid or none is in ``source``.
    """
    grids, omitted = field_grids(bundle, source)
    if not grids:
        raise SectionSkipped("the predictions do not hold the outputs of the model on a 2-D grid")
    budget = MAX_FIELD_CELLS // len(grids)
    entries = [_entry(g, budget) for g in grids]
    warn: list[str] = []
    if any(e.downsampled for e in entries):
        warn.append("Some fields have more pixels than the viewer shows. It shows block means.")
    if any(e.omitted_frames for e in entries):
        warn.append(f"Some fields have more than {MAX_FIELD_FRAMES} frames. It shows a sample.")
    return FieldsView(
        fields=entries,
        omitted=omitted[:256],
        warnings_plain=warn,
        caption_plain=caption
        or "Shows each output of the model on its grid. Select the mean, the uncertainty or a "
        "posterior draw. Use the slider to go through the frames.",
    )
