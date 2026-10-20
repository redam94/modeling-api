"""The Data Explorer sections for scientific data (pictures of the data shapes that
:func:`~mapi_runner.worker.structure.detect_structure` finds).

=============  ============================================================================
section        what it shows
=============  ============================================================================
``structure``  the data shapes, every dim's part in the data and every variable's use
``images``     variables on a 2-D grid as rasters for the field viewer, per-frame
               statistics, a histogram of every cell, class means, vector-field arrows;
               v0.14: colour pictures (a red, green and blue channel dim) as RGB rasters
``gallery``    v0.14: thumbnails of a stack of images (an image dim), an even sample of
               each class and split, with the image counts of each facet level
``profiles``   curves along a numeric axis that is not time (one per frame), with peaks
``map``        polygons coloured per variable, a ratio layer, neighbour links, Moran's I
``network``    adjacency matrix (seriation order), node-link layout, degree statistics
``tree``       a dendrogram (``parent``/``branch_length``) with the traits of its tips and
               their phylogenetic signal
``circular``   roses of angles and times of day per group, mean direction, Rayleigh test,
               the circular correlation of two such variables
``points``     positions as dots or lattice cells, CIE Lab colours, modal answers
=============  ============================================================================

Every view is capped (``MAX_*`` of :mod:`mapi_spec.views`). :func:`science_plots` adds the
Plotly recipes of ``sci.area_map``, ``net.graph`` and ``sci.rose`` (which work on raw data)
to the exported plots of a section.
"""

from __future__ import annotations

import base64
import itertools
import math
import re
import warnings
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import xarray as xr
from mapi_spec.schema import Role
from mapi_spec.views import (
    MAX_CIRC_BINS,
    MAX_CIRC_GROUPS,
    MAX_FIELD_CELLS,
    MAX_FIELD_FRAMES,
    MAX_FIELD_PIXELS,
    MAX_FIELDS,
    MAX_FRAME_STATS,
    MAX_GALLERY_FACETS,
    MAX_GALLERY_ITEMS,
    MAX_GALLERY_LEVELS,
    MAX_GALLERY_THUMB,
    MAX_IMAGE_GROUPS,
    MAX_MAP_AREAS,
    MAX_MAP_EDGES,
    MAX_MAP_VERTICES,
    MAX_NET_EDGES,
    MAX_NET_MATRIX,
    MAX_NET_NODES,
    MAX_POINT_DENSITY,
    MAX_POINTS,
    MAX_PROFILE_FRAMES,
    MAX_PROFILE_POINTS,
    MAX_TREE_NODES,
    CircularEntry,
    CircularGroup,
    CircularPair,
    CircularView,
    ClarkEvans,
    FieldFrame,
    FrameStats,
    GalleryFacet,
    GalleryItem,
    GalleryView,
    Histogram,
    Homophily,
    ImageComponent,
    ImageEntry,
    ImageGroupMean,
    ImageStats,
    ImagesView,
    MapFrame,
    MapLayer,
    MapPoints,
    MapRing,
    MapView,
    MoranRow,
    NetComponents,
    NetPositions,
    NetworkEdge,
    NetworkEntry,
    NetworkView,
    PhyloSignal,
    PointAnswers,
    PointColour,
    PointDensity,
    PointSample,
    PointsView,
    ProfileEntry,
    ProfileFrame,
    ProfilePeak,
    ProfilesView,
    QualityWarning,
    ShapeView,
    StructureDim,
    StructureVar,
    StructureView,
    TipTrait,
    TreeNodeLayout,
    TreeView,
)
from pydantic import BaseModel

from mapi_runner.worker.explore_common import InsufficientData, NotApplicable, fv, jsonable
from mapi_runner.worker.rasters import (
    arrows,
    block_labels,
    block_mean,
    encode_raster,
    encode_rgb,
    even,
    luminance,
    to_uint8,
)
from mapi_runner.worker.structure import Shape, Structure

if TYPE_CHECKING:
    from mapi_runner.worker.explore_data import DataContext

__all__ = [
    "INGEST_IMAGE_CELLS",
    "SCIENCE_SECTION_FUNCS",
    "image_cells",
    "saturated_share",
    "science_plots",
    "shape_views",
    "structure_warnings",
]

_PERMUTATIONS: Final = 999
_MAX_SIGNAL_TIPS: Final = 1000
_HIST_BINS: Final = 50
_ADDITIVE: Final = frozenset({Role.TARGET, Role.MEDIA, Role.SPEND, Role.EXPOSURE})
_MAX_PLOT_BYTES: Final = 2_000_000
_SECTION_OF: Final = {
    "image": "images", "vector_field": "images", "profile": "profiles", "areal_map": "map",
    "network": "network", "tree": "tree", "circular": "circular", "points": "points",
    "timeseries": "timeseries", "sequence": "timeseries", "table": "correlation",
}  # fmt: skip


# --------------------------------------------------------------------------- helpers


def _labels(ds: xr.Dataset, dim: str) -> list[Any]:
    if dim in ds.coords:
        return [jsonable(v) for v in np.asarray(ds[dim].values)]
    return list(range(int(ds.sizes[dim])))


def _floats(a: Any) -> list[float | None]:
    return [fv(v) for v in np.asarray(a, dtype=float).ravel()]


def _histogram(values: np.ndarray, bins: int = _HIST_BINS) -> Histogram | None:
    y = np.asarray(values, dtype=float)
    y = y[np.isfinite(y)]
    if y.size == 0 or float(y.max()) <= float(y.min()):
        return None
    counts, edges = np.histogram(y, bins=bins)
    return Histogram(edges=[float(e) for e in edges], counts=[int(k) for k in counts])


def _count_histogram(values: np.ndarray) -> Histogram | None:
    """A histogram of counts (the degree of the nodes of a binary network): one bar for each
    integer, centred on it (edges at the halves), when there are at most 50 integers. Equal
    bins of the range put two counts in one bar and none in the next (E19: degree 2 to 7)."""
    y = np.asarray(values, dtype=float)
    y = y[np.isfinite(y)]
    if y.size == 0 or not np.all(y == np.round(y)):
        return _histogram(y, bins=min(20, max(1, len(np.unique(y)))))
    lo, hi = int(y.min()), int(y.max())
    if hi - lo > 50:
        return _histogram(y, bins=20)
    edges = np.arange(lo - 0.5, hi + 1.0, 1.0)
    counts, edges = np.histogram(y, bins=edges)
    return Histogram(edges=[float(e) for e in edges], counts=[int(k) for k in counts])


def _units(c: DataContext, var: str) -> str | None:
    spec = c.schema.vars.get(var) if c.schema is not None else None
    units = (spec.units if spec is not None else None) or c.ds[var].attrs.get("units")
    return str(units)[:64] if units else None


def _role(c: DataContext, var: str) -> str | None:
    r = c.roles.get(var)
    return r.value if r is not None else None


def _need(c: DataContext, *kinds: str) -> list[Shape]:
    shapes = c.shapes.of(*kinds)
    if not shapes:
        section = _SECTION_OF[kinds[0]]
        raise NotApplicable(
            c.shapes.not_applicable.get(section, f"the data has no {kinds[0].replace('_', ' ')}")
        )
    return shapes


def _rng() -> np.random.Generator:
    return np.random.default_rng(20_241_019)


def _moran(
    values: np.ndarray, w: np.ndarray
) -> tuple[float | None, float, float | None, float | None]:
    """Moran's I of ``values`` with weights ``w`` (finite values only), its expectation, the
    z score and the two-sided p value of :data:`_PERMUTATIONS` seeded permutations."""
    ok = np.isfinite(values)
    x = values[ok]
    ww = w[np.ix_(ok, ok)].astype(float)
    n = x.size
    expected = -1.0 / (n - 1) if n > 1 else 0.0
    s0 = float(ww.sum())
    z = x - x.mean() if n else x
    denom = float((z**2).sum())
    if n < 4 or s0 <= 0 or denom <= 0:
        return None, expected, None, None

    def stat(v: np.ndarray) -> float:
        return float(n / s0 * (v @ ww @ v) / denom)

    obs = stat(z)
    rng = _rng()
    perms = np.array([stat(rng.permutation(z)) for _ in range(_PERMUTATIONS)])
    sd = float(perms.std(ddof=1))
    zscore = (obs - float(perms.mean())) / sd if sd > 0 else None
    extreme = int(np.sum(np.abs(perms - expected) >= abs(obs - expected) - 1e-12))
    p = (extreme + 1) / (_PERMUTATIONS + 1)
    return obs, expected, zscore, min(1.0, p)


# ------------------------------------------------------------------------- structure


def shape_views(s: Structure) -> list[ShapeView]:
    """The shapes of ``s`` as :class:`~mapi_spec.views.ShapeView` (best first)."""
    out: list[ShapeView] = []
    for shape in s.shapes[:64]:
        section = _SECTION_OF[shape.kind]
        out.append(
            ShapeView(
                kind=shape.kind,
                vars=list(shape.vars)[:256],
                dims={k: v for k, v in shape.dims.items() if v is not None},
                score=round(float(shape.score), 3),
                label_plain=shape.label_plain[:300],
                sections=[section],
            )
        )
    return out


def _evenly(values: np.ndarray) -> bool | None:
    if values.size < 3:
        return None
    if values.dtype.kind == "M":
        d = np.diff(values.astype("datetime64[ns]").astype(np.int64)).astype(float)
    elif values.dtype.kind in "iuf":
        d = np.diff(values.astype(float))
    else:
        return None
    if not np.all(np.isfinite(d)) or d.size == 0:
        return None
    return bool(np.allclose(d, d[0], rtol=1e-6, atol=0))


def structure(c: DataContext) -> StructureView:
    s = c.shapes
    ds = c.ds
    dims: list[StructureDim] = []
    for d in list(ds.dims)[:32]:
        name = str(d)
        kind = "categorical"
        if c.schema is not None and name in c.schema.dims:
            kind = str(c.schema.dims[name].kind)
        elif name in ds.coords:
            k = np.asarray(ds[name].values).dtype.kind
            kind = {"M": "time", "i": "integer", "u": "integer", "f": "float"}.get(k, kind)
        else:
            kind = "integer"
        labels = _labels(ds, name)
        ordered = kind in ("time", "integer", "float")
        values = np.asarray(ds[name].values) if name in ds.coords else np.arange(ds.sizes[name])
        dims.append(
            StructureDim(
                name=name[:256],
                size=int(ds.sizes[name]),
                kind=kind[:32],
                role_in_data=s.dim_roles.get(name, "other"),
                start=labels[0] if labels and ordered else None,
                end=labels[-1] if labels and ordered else None,
                evenly_spaced=_evenly(values) if ordered else None,
            )
        )
    var_rows: list[StructureVar] = []
    for v in list(ds.data_vars)[:256]:
        name = str(v)
        da = ds[name]
        var_rows.append(
            StructureVar(
                name=name[:256],
                dims=[str(x) for x in da.dims],
                shape=[int(n) for n in da.shape],
                dtype=str(da.dtype)[:64],
                role=_role(c, name),
                units=_units(c, name),
                used_as=s.var_use.get(name, "data"),
            )
        )
    caption = (
        "Shows the shapes the explorer found in the data, the part each dimension plays "
        "and how each variable is used."
    )
    return StructureView(
        shapes=shape_views(s),
        dims=dims,
        vars=var_rows,
        recommended=list(s.recommended),
        not_applicable=dict(s.not_applicable),
        time_dim=s.time_dim,
        sequence_dim=s.sequence_dim,
        unit_dim=s.unit_dim,
        categorical_dims=list(s.categorical_dims),
        caption_plain=caption,
    )


# ---------------------------------------------------------------------------- images


_GRAY_NAMES: Final = ("image", "images", "frames", "frame", "pixels", "pixel", "intensity")
_NORTH: Final = ("lat", "latitude", "north", "northing", "y_km", "y_m", "y_cm", "y_mm", "y_deg")


def _origin(ds: xr.Dataset, y: str) -> str:
    names = [y, *(str(k) for k, v in ds.coords.items() if v.dims == (y,) and str(k) != y)]
    for name in names:
        low = name.lower()
        if any(low == w or low.startswith(w + "_") or low.endswith("_" + w) for w in _NORTH):
            vals = np.asarray(ds[name].values) if name in ds.coords else None
            if vals is not None and vals.dtype.kind in "iuf" and vals.size > 1:
                return "lower" if bool(np.all(np.diff(vals.astype(float)) > 0)) else "upper"
    return "upper"


def _colormap(var: str, lo: float, hi: float, single: bool) -> str:
    if lo < 0 < hi and 0.5 <= abs(lo) / hi <= 2.0:
        return "diverging"
    if single and var.lower() in _GRAY_NAMES:
        return "gray"
    return "viridis"


_RGB_LABELS: Final = (("r", "g", "b"), ("red", "green", "blue"))
_IMAGE_ATTR: Final = "mapi_image"
"""The attribute of the pixels an ``image_folder`` ingest writes (pictures, not detector
readings: a white background at 255 is not saturation)."""


def rgb_channel(ds: xr.Dataset, chan: str | None) -> bool:
    """The channel dim holds red, green and blue: 3 or 4 levels (the 4th, alpha, is left
    out), or labels ``r, g, b`` / ``red, green, blue`` first."""
    if chan is None or chan not in ds.dims:
        return False
    n = int(ds.sizes[chan])
    if n in (3, 4):
        return True
    labels = tuple(str(v).lower() for v in _labels(ds, chan)[:3])
    return n >= 3 and labels in _RGB_LABELS


_SAMPLE_CELLS: Final = 5_000_000
"""Cells of the sample that gives the histogram, the skew and the saturated share."""
_CHUNK_CELLS: Final = 4_000_000
"""Cells read at a time (float32) for the statistics of all the frames."""
INGEST_IMAGE_CELLS: Final = 50_000_000
"""Above this many cells in all the images, the ingest does not compute ``images``: the
section is computed on demand (an explore run)."""


def image_cells(structure: Structure, ds: xr.Dataset) -> int:
    """The cells of all the variables of the image and vector-field shapes."""
    return sum(int(ds[v].size) for s in structure.of("image", "vector_field") for v in s.vars)


def saturated_share(sample: np.ndarray, lo: float, hi: float) -> float:
    """The share of ``sample`` (finite values) at the maximum ``hi`` when it is a pile-up of
    a saturated detector, else 0.

    A pile-up: the values have 32 or more distinct levels (not a field with a few levels,
    such as a heater set-point), and the maximum is a detector ceiling (``2**k - 1``, 1.0) or
    has 4 times more values than the bin of width ``(hi - lo) / 50`` below it."""
    if sample.size == 0 or not hi > lo:
        return 0.0
    at_max = int(np.count_nonzero(sample == hi))
    if at_max < 2:
        return 0.0
    distinct = np.unique(sample if sample.size <= 200_000 else sample[:: sample.size // 200_000])
    if distinct.size < 32:
        return 0.0
    ceiling = hi == 1.0 or (
        hi >= 1 and float(hi) == float(int(hi)) and (int(hi) + 1) & int(hi) == 0
    )
    below = int(np.count_nonzero((sample >= hi - (hi - lo) / 50) & (sample < hi)))
    if ceiling or at_max >= 4 * max(below, 1):
        return at_max / sample.size
    return 0.0


class _Img:
    """One image entry to encode: values ``(F, C, H, W)`` (C = 2 for a vector field), read a
    few frames at a time as float32 (never all the frames as float64). A colour picture
    (``rgb``) reads ``(F, 1, H, W, 3)`` and its values are the luminance; :meth:`colours`
    gives the red, green and blue."""

    def __init__(
        self,
        c: DataContext,
        shape: Shape,
        var: str,
        facet: dict[str, Any],
        sel: dict[str, int],
    ) -> None:
        ds = c.ds
        self.var, self.shape, self.facet = var, shape, facet
        y, x = shape.dims["y"], shape.dims["x"]
        frame, comp = shape.dims.get("frame"), shape.dims.get("component")
        self.y, self.x, self.frame, self.comp = y, x, frame, comp
        da = ds[var].isel(sel) if sel else ds[var]
        chan = shape.dims.get("channel")
        self.rgb = comp is None and chan is not None and chan in da.dims and rgb_channel(ds, chan)
        self.chan = chan if self.rgb else None
        self.uint8 = da.dtype == np.uint8
        if self.rgb:
            assert chan is not None
            da = da.isel({chan: slice(0, 3)})
        elif chan is not None and chan in da.dims:
            da = da.isel({chan: 0})
        if frame is None:
            da = da.expand_dims("__frame")
        if comp is None:
            da = da.expand_dims("__comp")
        order: tuple[str, ...] = (frame or "__frame", comp or "__comp", y, x)
        if self.rgb:
            assert chan is not None
            order = (*order, chan)
        self.fdim = order[0]
        self.da = da.transpose(*order)
        self.n_frames = int(self.da.sizes[self.fdim])
        self.h, self.w = int(self.da.sizes[y]), int(self.da.sizes[x])
        self.frame_labels = _labels(ds, frame) if frame is not None else [None]
        self.y_labels, self.x_labels = _labels(ds, y), _labels(ds, x)
        mask_var = shape.extra.get("mask")
        self.mask_var = mask_var if isinstance(mask_var, str) else None
        self.mask: np.ndarray | None = None
        if self.mask_var is not None:
            m = ds[self.mask_var]
            msel = {d: i for d, i in sel.items() if d in m.dims}
            m = m.isel(msel) if msel else m
            arr = np.asarray(m.astype(float).transpose(*[d for d in order if d in m.dims]).values)
            while arr.ndim > 2:
                arr = arr.mean(axis=0)
            self.mask = arr
        self.group_coord = shape.extra.get("group_coord") if frame is not None else None

    def values(self, frames: Sequence[int] | slice) -> np.ndarray:
        """``(k, C, H, W)`` float32 of some frames (a colour picture: the luminance)."""
        sel = frames if isinstance(frames, slice) else list(frames)
        vals = np.asarray(self.da.isel({self.fdim: sel}).values, dtype=np.float32)
        return luminance(vals) if self.rgb else vals

    def colours(self, frames: Sequence[int] | slice) -> np.ndarray:
        """``(k, 3, H, W)`` float32: the red, green and blue of some frames (``rgb`` only)."""
        sel = frames if isinstance(frames, slice) else list(frames)
        vals = np.asarray(self.da.isel({self.fdim: sel}).values, dtype=np.float32)
        return np.moveaxis(vals[:, 0], -1, 1)

    def magnitude(self, vals: np.ndarray) -> np.ndarray:
        """``(k, H, W)``: the value, or the length of the vector."""
        if self.comp is None:
            return vals[:, 0]
        return np.asarray(np.sqrt(np.sum(vals * vals, axis=1)), dtype=np.float32)

    def chunks(self, frames: Sequence[int] | None = None) -> Iterator[tuple[list[int], np.ndarray]]:
        """``(frame indices, values (k, C, H, W))`` of ``frames`` (default all), a few
        frames at a time."""
        idx = list(range(self.n_frames)) if frames is None else list(frames)
        per_frame = self.h * self.w * (2 if self.comp else 3 if self.rgb else 1)
        per = max(1, _CHUNK_CELLS // max(1, per_frame))
        for start in range(0, len(idx), per):
            part = idx[start : start + per]
            contiguous = part == list(range(part[0], part[-1] + 1))
            yield part, self.values(slice(part[0], part[-1] + 1) if contiguous else part)


class _Summary:
    """Statistics of all the cells of an image, computed a few frames at a time, and a
    sample of at most :data:`_SAMPLE_CELLS` cells for the histogram and the skew."""

    def __init__(self, img: _Img) -> None:
        n_frames, cells = img.n_frames, img.h * img.w
        self.stat_idx = even(n_frames, MAX_FRAME_STATS) if img.frame is not None else []
        stat_pos = {f: k for k, f in enumerate(self.stat_idx)}
        k = len(self.stat_idx)
        self.f_mean, self.f_sd = np.full(k, np.nan), np.full(k, np.nan)
        self.f_lo, self.f_hi, self.f_miss = np.full(k, np.nan), np.full(k, np.nan), np.zeros(k)
        sample_frames = set(even(n_frames, max(1, _SAMPLE_CELLS // max(1, cells))))
        n, mean, m2 = 0, 0.0, 0.0
        lo, hi = np.inf, -np.inf
        missing, total = 0, 0
        parts: list[np.ndarray] = []
        comp_parts: list[np.ndarray] = []
        for frames, vals in img.chunks():
            mag = img.magnitude(vals)
            flat = mag.reshape(len(frames), -1)
            ok = np.isfinite(flat)
            total += flat.size
            missing += int(flat.size - np.count_nonzero(ok))
            fin = flat[ok].astype(np.float64)
            if fin.size:
                lo, hi = min(lo, float(fin.min())), max(hi, float(fin.max()))
                cm = float(fin.mean())
                cm2 = float(((fin - cm) ** 2).sum())
                tot = n + fin.size
                delta = cm - mean
                m2 += cm2 + delta * delta * n * fin.size / tot
                mean += delta * fin.size / tot
                n = tot
            del fin
            picked = [i for i, f in enumerate(frames) if f in stat_pos]
            if picked:
                sub = flat[picked]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    pos = [stat_pos[frames[i]] for i in picked]
                    self.f_mean[pos] = np.nanmean(sub, axis=1, dtype=np.float64)
                    if sub.shape[1] > 1:
                        self.f_sd[pos] = np.nanstd(sub, axis=1, ddof=1, dtype=np.float64)
                    self.f_lo[pos] = np.nanmin(sub, axis=1)
                    self.f_hi[pos] = np.nanmax(sub, axis=1)
                self.f_miss[pos] = np.mean(~np.isfinite(sub), axis=1)
            keep = [i for i, f in enumerate(frames) if f in sample_frames]
            if keep:
                s = flat[keep]
                parts.append(s[np.isfinite(s)])
                if img.comp is not None:
                    comp_parts.append(vals[keep].transpose(1, 0, 2, 3).reshape(2, -1))
        sample = np.concatenate(parts) if parts else np.zeros(0, np.float32)
        if sample.size > _SAMPLE_CELLS:
            sample = sample[:: -(-sample.size // _SAMPLE_CELLS)]
        self.sample = sample
        self.components = np.concatenate(comp_parts, axis=1) if comp_parts else None
        self.n, self.lo, self.hi = n, (lo if n else 0.0), (hi if n else 0.0)
        self.mean = mean if n else None
        self.sd = math.sqrt(m2 / (n - 1)) if n > 1 else None
        self.missing_share = missing / total if total else 0.0


def _plan(img: _Img, budget: int) -> tuple[int, list[int], int]:
    """Block factor, frames and group rasters that fit ``budget`` cells."""
    h, w = img.h, img.w
    f = max(1, -(-max(h, w) // MAX_FIELD_PIXELS))
    n_groups = 0
    if img.group_coord is not None:
        n_groups = min(MAX_IMAGE_GROUPS, len({str(v) for v in img.frame_labels}))
    n_frames = min(MAX_FIELD_FRAMES, len(img.frame_labels))
    extra = 1 if img.mask is not None else 0
    per = 3 if img.rgb else 1  # an rgb raster counts as two rasters, next to the luminance
    while True:
        cells = -(-h // f) * -(-w // f) * per
        while n_frames > 1 and cells * (n_frames + extra + n_groups) > budget:
            n_frames -= 1
        while n_groups > 0 and cells * (n_frames + extra + n_groups) > budget:
            n_groups -= 1
        if cells * (n_frames + extra + n_groups) <= budget:
            return f, even(len(img.frame_labels), n_frames), n_groups
        f += 1


def _frame_stats(img: _Img, s: _Summary) -> FrameStats | None:
    if img.frame is None:
        return None
    return FrameStats(
        coords=[img.frame_labels[i] for i in s.stat_idx],
        mean=_floats(s.f_mean),
        sd=_floats(s.f_sd),
        min=_floats(s.f_lo),
        max=_floats(s.f_hi),
        missing_share=[float(m) for m in s.f_miss],
        downsampled=len(s.stat_idx) < img.n_frames,
    )


def _image_entry(c: DataContext, img: _Img, budget: int) -> ImageEntry:
    f, frames, n_groups = _plan(img, budget)
    s = _Summary(img)
    lo, hi = s.lo, s.hi
    log = bool(s.n and lo > 0 and hi / lo > 1e3) and not img.rgb
    enc = "log10" if log else "linear"
    vector = img.comp is not None
    # colour pictures: 8-bit values as they are, other values stretched from the min to the max
    span = (0.0, 255.0) if img.uint8 else (lo, hi)

    def colour(rgb: np.ndarray) -> Any:
        return encode_rgb(np.moveaxis(block_mean(rgb, f, f), 0, -1), *span)

    out: list[FieldFrame] = []
    for i in frames:
        vals = img.values([i])
        out.append(
            FieldFrame(
                label=img.frame_labels[i] if img.frame is not None else None,
                mean=encode_raster(block_mean(img.magnitude(vals)[0], f, f), enc),
                arrows=arrows(block_mean(vals[0], f, f)) if vector else None,
                rgb=colour(img.colours([i])[0]) if img.rgb else None,
            )
        )
    groups: list[ImageGroupMean] = []
    if n_groups and img.group_coord is not None and img.frame is not None:
        labels = [jsonable(v) for v in np.asarray(c.ds[img.group_coord].values)]
        levels = list(dict.fromkeys(labels))
        for level in levels[:n_groups]:
            pick = [k for k, lab in enumerate(labels) if lab == level]
            total: np.ndarray | None = None
            count: np.ndarray | None = None
            rgb_total: np.ndarray | None = None
            for part, vals in img.chunks(pick):
                bm = block_mean(img.magnitude(vals), f, f)
                ok = np.isfinite(bm)
                add = np.where(ok, bm, 0.0).sum(axis=0)
                cnt = ok.sum(axis=0)
                total = add if total is None else total + add
                count = cnt if count is None else count + cnt
                if img.rgb:
                    cb = np.nan_to_num(block_mean(img.colours(part), f, f)).sum(axis=0)
                    rgb_total = cb if rgb_total is None else rgb_total + cb
            assert total is not None and count is not None
            with np.errstate(invalid="ignore", divide="ignore"):
                m = np.where(count > 0, total / np.maximum(count, 1), np.nan)
            rgb_mean = (
                encode_rgb(np.moveaxis(rgb_total / max(1, len(pick)), 0, -1), *span)
                if rgb_total is not None
                else None
            )
            groups.append(
                ImageGroupMean(label=level, n=len(pick), mean=encode_raster(m, enc), rgb=rgb_mean)
            )
    mask = encode_raster(block_mean(img.mask, f, f)) if img.mask is not None else None
    fin = s.sample
    hist = _histogram(fin)
    skew = 0.0
    if fin.size > 2 and hi > lo:
        f64 = fin.astype(np.float64)
        sd0 = float(f64.std())
        skew = float(np.mean(((f64 - f64.mean()) / sd0) ** 3)) if sd0 > 0 else 0.0
        del f64
    stats = ImageStats(
        min=fv(lo) if s.n else None,
        max=fv(hi) if s.n else None,
        mean=fv(s.mean) if s.mean is not None else None,
        sd=fv(s.sd) if s.sd is not None else None,
        missing_share=s.missing_share,
        saturated_share=saturated_share(fin, lo, hi),
    )
    components: list[ImageComponent] = []
    if vector and img.comp is not None and s.components is not None:
        for k, label in enumerate(_labels(c.ds, img.comp)[:2]):
            components.append(ImageComponent(label=label, histogram=_histogram(s.components[k])))
    h, w = img.h, img.w
    spec = c.schema.vars.get(img.var) if c.schema is not None else None
    return ImageEntry(
        output=img.var[:128],
        var=img.var,
        role=_role(c, img.var),
        units=_units(c, img.var),
        description=spec.description if spec is not None else None,
        facet=img.facet,
        kind="vector" if vector else "scalar",
        y_dim=img.y,
        x_dim=img.x,
        frame_dim=img.frame,
        component_dim=img.comp,
        height=-(-h // f),
        width=-(-w // f),
        source_height=h,
        source_width=w,
        downsampled=f > 1,
        y_coords=block_labels(img.y_labels, f),
        x_coords=block_labels(img.x_labels, f),
        colormap="magma" if vector else ("gray" if img.rgb else _colormap(img.var, lo, hi, True)),
        log=log,
        origin=_origin(c.ds, img.y),
        encoding=enc,
        n_draws=0,
        frames=out,
        omitted_frames=len(img.frame_labels) - len(frames) if img.frame is not None else 0,
        mask_var=img.mask_var,
        mask=mask,
        frame_stats=_frame_stats(img, s),
        histogram=hist,
        log_counts=skew > 2.0,
        stats=stats,
        group_by=img.group_coord if groups else None,
        groups=groups,
        components=components,
        color="rgb" if img.rgb else "single",
        channel_dim=img.chan,
        rgb_scale="uint8" if img.uint8 else "stretch",
    )


def images(c: DataContext) -> ImagesView:
    shapes = _need(c, "image", "vector_field")
    todo: list[tuple[Shape, str, dict[str, Any], dict[str, int]]] = []
    omitted: list[str] = []
    for shape in shapes:
        facets = [d for d in shape.extra.get("facets", ()) if d in c.ds.dims]
        chan = shape.dims.get("channel")
        # a colour picture shows its channels together; one gray channel is not a facet
        if chan is not None and not rgb_channel(c.ds, chan) and c.ds.sizes[chan] > 1:
            facets.append(chan)
        levels = [_labels(c.ds, d) for d in facets]
        for var in shape.vars:
            for idx in itertools.product(*[range(len(lv)) for lv in levels]):
                facet = {d: lv[i] for d, lv, i in zip(facets, levels, idx, strict=True)}
                if len(todo) >= MAX_FIELDS:
                    omitted.append(f"{var}{list(facet.values()) if facet else ''}"[:256])
                    continue
                todo.append((shape, var, facet, dict(zip(facets, idx, strict=True))))
    budget = MAX_FIELD_CELLS // max(1, len(todo))
    # one image at a time: its frames are read a few at a time, never all at once
    entries = [_image_entry(c, _Img(c, *item), budget) for item in todo]
    warn: list[str] = []
    if any(e.downsampled for e in entries):
        warn.append("Some images have more pixels than the viewer shows. It shows block means.")
    if any(e.omitted_frames for e in entries):
        warn.append(
            f"Some images have more than {MAX_FIELD_FRAMES} frames. The viewer shows an even "
            "sample. The frame statistics use all the frames."
        )
    if omitted:
        warn.append(f"The view does not show {len(omitted)} more images.")
    if any(e.color == "rgb" for e in entries):
        warn.append(
            "The colour images show red, green and blue. The statistics, the histogram and "
            "the frame line use the brightness (0.299 red + 0.587 green + 0.114 blue)."
        )
    return ImagesView(
        fields=entries,
        omitted=omitted[:256],
        warnings_plain=warn,
        caption_plain="Shows each variable on its grid. Use the slider to go through the "
        "frames. The line under the image shows the mean of each frame.",
    )


# --------------------------------------------------------------------------- gallery


_FACET_FIRST: Final = ("label", "split", "class")


def _gallery_shape(c: DataContext) -> Shape:
    """The first image shape whose frame dim has 2 or more levels (a stack of images)."""
    for shape in c.shapes.of("image"):
        frame = shape.dims.get("frame")
        if frame is not None and int(c.ds.sizes[frame]) >= 2:
            return shape
    raise NotApplicable(
        c.shapes.not_applicable.get("gallery", "no variable holds a stack of images")
    )


def _facet_values(da: xr.DataArray) -> list[str]:
    return [str(jsonable(v)) for v in np.asarray(da.values).tolist()]


def _gallery_facets(c: DataContext, frame: str) -> list[tuple[GalleryFacet, list[str]]]:
    """Text, integer and boolean variables and coords along the frame dim with 2 to
    :data:`MAX_GALLERY_LEVELS` levels: ``label``, ``split`` and ``class`` first, at most
    :data:`MAX_GALLERY_FACETS` (with the level of every image)."""
    found: list[tuple[int, int, GalleryFacet, list[str]]] = []
    names = [(str(k), "coord") for k in c.ds.coords] + [(str(k), "var") for k in c.ds.data_vars]
    for k, (name, source) in enumerate(names):
        da = c.ds[name]
        if name == frame or da.dims != (frame,) or da.dtype.kind not in "iuUOSTb":
            continue
        levels = _facet_values(da)
        counts: dict[str, int] = {}
        for v in levels:
            counts[v] = counts.get(v, 0) + 1
        if not 2 <= len(counts) <= MAX_GALLERY_LEVELS or len(counts) == len(levels):
            continue  # one level, too many, or one image per level (an id or a path)
        rank = _FACET_FIRST.index(name.lower()) if name.lower() in _FACET_FIRST else 99
        facet = GalleryFacet(name=name[:128], source=source, counts=counts)
        found.append((rank, k, facet, levels))
    found.sort(key=lambda t: (t[0], t[1]))
    return [(f, lv) for _r, _k, f, lv in found[:MAX_GALLERY_FACETS]]


def _stratified_pick(cells: dict[tuple[str, ...], list[int]], budget: int) -> list[int]:
    """At most ``budget`` positions: one image of each cell in turn (round-robin over the
    cells in sorted order) until the budget is spent, then an even sample within each cell."""
    order = sorted(cells)
    take = dict.fromkeys(order, 0)
    left = budget
    while left > 0:
        moved = False
        for key in order:
            if left and take[key] < len(cells[key]):
                take[key] += 1
                left -= 1
                moved = True
        if not moved:
            break
    out: list[int] = []
    for key in order:
        rows = cells[key]
        out.extend(rows[i] for i in even(len(rows), take[key]))
    return sorted(out)


def _thumbnail(values: np.ndarray, rgb: bool, uint8: bool) -> tuple[np.ndarray, int]:
    """``values`` ``(H, W, C)`` as uint8 ``(h, w[, 3])`` of at most :data:`MAX_GALLERY_THUMB`
    pixels a side (block means): 8-bit data as it is, other data stretched from the image's
    own minimum to its maximum."""
    h, w = values.shape[:2]
    f = max(1, -(-max(h, w) // MAX_GALLERY_THUMB))
    chans = np.moveaxis(values[..., :3] if rgb else values[..., :1], -1, 0)
    small = np.moveaxis(block_mean(chans.astype(np.float32), f, f), 0, -1)
    if uint8:
        out = to_uint8(small)
    else:
        fin = small[np.isfinite(small)]
        lo, hi = (float(fin.min()), float(fin.max())) if fin.size else (0.0, 1.0)
        out = to_uint8(small, lo, hi if hi > lo else lo + 1.0)
    return (out if rgb else out[..., 0]), f


def gallery(c: DataContext) -> GalleryView:
    """``gallery``: thumbnails of the images of the first stack of images (an image shape
    with a frame dim), an even sample of each class and split when there are more than
    :data:`MAX_GALLERY_ITEMS`, and the image counts of each facet level."""
    from mapi_sdk.media import encode_png

    shape = _gallery_shape(c)
    var = shape.vars[0]
    frame, y, x = shape.dims["frame"], shape.dims["y"], shape.dims["x"]
    chan = shape.dims.get("channel")
    ds = c.ds
    n = int(ds.sizes[frame])
    rgb = rgb_channel(ds, chan)
    da = ds[var]
    extra = [d for d in da.dims if d not in (frame, y, x, chan)]
    if extra:
        da = da.isel(dict.fromkeys(extra, 0))
    da = da.transpose(frame, y, x, *([chan] if chan is not None else []))
    uint8 = da.dtype == np.uint8
    facets = _gallery_facets(c, frame)
    if n <= MAX_GALLERY_ITEMS:
        picked, selection = list(range(n)), "all"
    elif facets:
        first = facets[0]
        split = next((f for f in facets[1:] if f[0].name.lower() == "split"), None)
        cells: dict[tuple[str, ...], list[int]] = {}
        for i in range(n):
            key = (first[1][i], split[1][i]) if split is not None else (first[1][i],)
            cells.setdefault(key, []).append(i)
        picked, selection = _stratified_pick(cells, MAX_GALLERY_ITEMS), "stratified"
    else:
        picked, selection = even(n, MAX_GALLERY_ITEMS), "even"
    labels = _labels(ds, frame)
    items: list[GalleryItem] = []
    th = tw = 1
    for i in picked:
        vals = np.asarray(da.isel({frame: i}).values)
        if vals.ndim == 2:
            vals = vals[..., None]
        thumb, _f = _thumbnail(vals, rgb, uint8)
        th, tw = int(thumb.shape[0]), int(thumb.shape[1])
        png, _w, _h = encode_png(thumb)
        items.append(
            GalleryItem(
                index=i,
                label=labels[i],
                meta={f.name: lv[i] for f, lv in facets},
                png=base64.b64encode(png).decode("ascii"),
            )
        )
    h, w = int(ds.sizes[y]), int(ds.sizes[x])
    if not items:
        f0 = max(1, -(-max(h, w) // MAX_GALLERY_THUMB))
        th, tw = -(-h // f0), -(-w // f0)
    others = [v for s in c.shapes.of("image") for v in s.vars if v != var]
    warn: list[str] = []
    if extra:
        warn.append(
            f"{var} has more dims ({', '.join(str(d) for d in extra)}). The gallery shows the "
            "first level of each."
        )
    if not uint8:
        warn.append(
            "The values are not 8-bit pixels. Each thumbnail is stretched from its smallest "
            "to its largest value."
        )
    return GalleryView(
        var=var,
        frame_dim=frame,
        y_dim=y,
        x_dim=x,
        channel_dim=chan,
        color="rgb" if rgb else "gray",
        source_height=h,
        source_width=w,
        thumb_height=th,
        thumb_width=tw,
        n_images=n,
        items=items,
        facets=[f for f, _lv in facets],
        selection=selection,
        other_vars=list(dict.fromkeys(others))[:256],
        warnings_plain=warn,
        caption_plain="Shows small copies of the images. Use the filters to show one class or "
        "split. Click an image to see it larger.",
    )


# -------------------------------------------------------------------------- profiles


def profiles(c: DataContext) -> ProfilesView:
    shapes = _need(c, "profile")
    items: list[ProfileEntry] = []
    omitted: list[str] = []
    for shape in shapes:
        along = shape.dims["along"]
        frame_dim = shape.dims.get("frame")
        coords_all = _labels(c.ds, along)
        idx = even(len(coords_all), MAX_PROFILE_POINTS)
        log_x = bool(shape.extra.get("log_x"))
        for var in shape.vars:
            if len(items) >= 64:
                omitted.append(var)
                continue
            da = c.ds[var]
            fd = frame_dim if frame_dim in da.dims else None
            arr = np.asarray(da.transpose(*([fd] if fd else []), along).values, dtype=float)
            if fd is None:
                arr = arr[None]
            labels = _labels(c.ds, fd) if fd else [None]
            picks = even(len(labels), MAX_PROFILE_FRAMES)
            frames: list[ProfileFrame] = []
            peaks: list[ProfilePeak] = []
            for i in picks:
                row = arr[i]
                frames.append(ProfileFrame(label=labels[i], values=_floats(row[idx])))
                ok = np.isfinite(row)
                if ok.any():
                    k = int(np.nanargmax(np.where(ok, row, -np.inf)))
                    peaks.append(ProfilePeak(frame=labels[i], at=coords_all[k], value=fv(row[k])))
            fin = arr[np.isfinite(arr)]
            log_y = bool(fin.size and fin.min() > 0 and fin.max() / fin.min() > 1e3)
            items.append(
                ProfileEntry(
                    var=var,
                    units=_units(c, var),
                    along=along,
                    coords=[coords_all[i] for i in idx],
                    frame_dim=fd,
                    frames=frames,
                    omitted_frames=len(labels) - len(picks),
                    log_x=log_x,
                    log_y=log_y,
                    peak=peaks,
                    downsampled=len(idx) < len(coords_all),
                )
            )
    if not items:
        raise NotApplicable("no variable lies along a numeric axis that is not time")
    return ProfilesView(
        items=items,
        omitted=omitted[:256],
        caption_plain="Shows each variable along its axis. Each line is one frame (for "
        "example one time point). The dot marks the highest value.",
    )


# ------------------------------------------------------------------------------- map


def _rings(xs: np.ndarray, ys: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """The NaN-separated parts of one area's padded ring."""
    ok = np.isfinite(xs) & np.isfinite(ys)
    out: list[tuple[np.ndarray, np.ndarray]] = []
    start = None
    for i, good in enumerate([*ok.tolist(), False]):
        if good and start is None:
            start = i
        elif not good and start is not None:
            if i - start >= 1:
                out.append((xs[start:i], ys[start:i]))
            start = None
    return out


def _centroid(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """``(cx, cy, |area|)`` of a polygon ring (the vertex mean when it has no area)."""
    if x.size < 3:
        return float(np.mean(x)), float(np.mean(y)), 0.0
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    cross = x * y1 - x1 * y
    a = float(cross.sum()) / 2
    if abs(a) < 1e-12:
        return float(np.mean(x)), float(np.mean(y)), 0.0
    cx = float(((x + x1) * cross).sum()) / (6 * a)
    cy = float(((y + y1) * cross).sum()) / (6 * a)
    return cx, cy, abs(a)


def _decimate(x: np.ndarray, y: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    if k <= 1 or x.size <= 4:
        return x, y
    idx = sorted({*range(0, x.size, k), x.size - 1})
    return x[idx], y[idx]


def map_(c: DataContext) -> MapView:
    shape = _need(c, "areal_map")[0]
    ds = c.ds
    area, vert = shape.dims["area"], shape.dims["vertex"]
    n = int(ds.sizes[area])
    if n > MAX_MAP_AREAS:
        raise InsufficientData(f"the map has {n} areas; the explorer draws {MAX_MAP_AREAS}")
    rx = np.asarray(ds["ring_x"].transpose(area, vert).values, dtype=float)
    ry = np.asarray(ds["ring_y"].transpose(area, vert).values, dtype=float)
    parts = [_rings(rx[i], ry[i]) for i in range(n)]
    total = sum(p[0].size for ps in parts for p in ps)
    k = max(1, -(-total // MAX_MAP_VERTICES))
    while True:
        dec = [[_decimate(px, py, k) for px, py in ps] for ps in parts]
        if sum(p[0].size for ps in dec for p in ps) <= MAX_MAP_VERTICES:
            break
        k += 1
    rings: list[MapRing] = []
    cx: list[float | None] = []
    cy: list[float | None] = []
    for i, ps in enumerate(dec):
        best: tuple[float | None, float | None, float] = (None, None, -1.0)
        for (px, py), (fx, fy) in zip(ps, parts[i], strict=True):
            rings.append(MapRing(area=i, x=[float(v) for v in px], y=[float(v) for v in py]))
            ccx, ccy, a = _centroid(fx, fy)
            if a > best[2]:
                best = (ccx, ccy, a)
        cx.append(fv(best[0]))
        cy.append(fv(best[1]))
    fx_all, fy_all = rx[np.isfinite(rx)], ry[np.isfinite(ry)]
    bbox = (
        [float(fx_all.min()), float(fy_all.min()), float(fx_all.max()), float(fy_all.max())]
        if fx_all.size and fy_all.size
        else None
    )
    layers: list[MapLayer] = []
    for var in shape.vars:
        da = ds[var]
        if da.dims == (area,):
            layers.append(
                MapLayer(
                    var=var, units=_units(c, var), role=_role(c, var), values=_floats(da.values)
                )
            )
        else:
            other = next(str(d) for d in da.dims if d != area)
            arr = np.asarray(da.transpose(other, area).values, dtype=float)
            labels = _labels(ds, other)
            picks = even(len(labels), MAX_PROFILE_FRAMES)
            layers.append(
                MapLayer(
                    var=var,
                    units=_units(c, var),
                    role=_role(c, var),
                    values=_floats(arr[0]),
                    frame_dim=other,
                    frames=[MapFrame(label=labels[i], values=_floats(arr[i])) for i in picks],
                )
            )
    target = next(
        (v for v in shape.vars if c.roles.get(v) is Role.TARGET and ds[v].dims == (area,)), None
    )
    exposure = next(
        (v for v in shape.vars if c.roles.get(v) is Role.EXPOSURE and ds[v].dims == (area,)), None
    )
    if target and exposure:
        t = np.asarray(ds[target].values, dtype=float)
        e = np.asarray(ds[exposure].values, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(e > 0, t / e, np.nan)
        layers.append(
            MapLayer(
                var=f"{target} / {exposure}"[:256],
                values=_floats(ratio),
                derived_from=[target, exposure],
            )
        )
    nb = shape.extra.get("neighbours")
    edges: list[list[int]] = []
    isolates: list[int] = []
    weights = "adjacency"
    if isinstance(nb, str) and nb in ds.data_vars:
        adj = np.asarray(ds[nb].values, dtype=float)
        adj = np.nan_to_num(adj)
        w = ((adj != 0) | (adj.T != 0)).astype(float)
        np.fill_diagonal(w, 0.0)
        ii, jj = np.nonzero(np.triu(w, 1))
        edges = [
            [int(i), int(j)] for i, j in zip(ii[:MAX_MAP_EDGES], jj[:MAX_MAP_EDGES], strict=True)
        ]
        isolates = [int(i) for i in np.nonzero(w.sum(1) == 0)[0]]
    else:
        weights = "knn4"
        pts = np.array(
            [
                [x if x is not None else np.nan, y if y is not None else np.nan]
                for x, y in zip(cx, cy, strict=True)
            ]
        )
        d = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1))
        np.fill_diagonal(d, np.inf)
        d = np.nan_to_num(d, nan=np.inf)
        w = np.zeros((n, n))
        for i in range(n):
            for j in np.argsort(d[i])[: min(4, n - 1)]:
                if np.isfinite(d[i, j]):
                    w[i, j] = w[j, i] = 1.0
    moran: list[MoranRow] = []
    framed: list[str] = []
    for layer in layers:
        vals = np.array([np.nan if v is None else v for v in layer.values], dtype=float)
        if layer.frame_dim is not None:
            # a variable on (area, other): Moran's I of the mean over the frames, not of the
            # first frame that the map shows first
            arr = np.asarray(ds[layer.var].transpose(layer.frame_dim, area).values, dtype=float)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                vals = np.nanmean(arr, axis=0)
            framed.append(f"{layer.var} (mean over {layer.frame_dim})")
        i_, e_, z_, p_ = _moran(vals, w)
        moran.append(
            MoranRow(
                var=layer.var,
                morans_i=fv(i_),
                expected=fv(e_),
                z=fv(z_),
                p_value=p_,
                weights=weights,
                permutations=_PERMUTATIONS,
            )
        )
    warn: list[str] = []
    if k > 1:
        warn.append("The polygons have many points. The map shows a simplified outline.")
    if weights == "knn4":
        warn.append("The data has no neighbour variable. Moran's I uses the 4 nearest areas.")
    if isolates:
        warn.append(f"{len(isolates)} area(s) have no neighbour.")
    if framed:
        warn.append(
            "Moran's I uses the mean of each area over the frames for "
            + ", ".join(framed[:8])
            + "."
        )
    return MapView(
        area_dim=area,
        areas=_labels(ds, area),
        rings=rings,
        decimated=k > 1,
        bbox=bbox,
        centroids=MapPoints(x=cx, y=cy),
        layers=layers,
        edges=edges,
        isolates=isolates,
        moran=moran,
        warnings_plain=warn,
        caption_plain="Shows each variable by area. Lines join neighbouring areas. Moran's I "
        "above 0 shows that neighbours have similar values.",
    )


# --------------------------------------------------------------------------- network


def _components(a: np.ndarray) -> list[int]:
    """Sizes of the connected components of a boolean adjacency."""
    n = a.shape[0]
    seen = np.zeros(n, dtype=bool)
    sizes: list[int] = []
    nbrs = [np.nonzero(a[i])[0] for i in range(n)]
    for s in range(n):
        if seen[s]:
            continue
        stack, size = [s], 0
        seen[s] = True
        while stack:
            u = stack.pop()
            size += 1
            for v in nbrs[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(int(v))
        sizes.append(size)
    return sizes


def _network_entry(
    c: DataContext, shape: Shape, var: str, facet: dict[str, Any], sel: dict[str, int]
) -> NetworkEntry:
    from mapi_plots.packs.net import fiedler_order, spectral_layout

    ds = c.ds
    node = shape.dims["node"]
    da = ds[var].isel(sel) if sel else ds[var]
    m = np.asarray(da.transpose(node, node + "_").values, dtype=float)
    n = m.shape[0]
    off = ~np.eye(n, dtype=bool)
    fin = m[off & np.isfinite(m)]
    binary = bool(fin.size and np.all(np.isin(fin, (0.0, 1.0))))
    signed = bool(fin.size and fin.min() < 0)
    mz = np.nan_to_num(m)
    directed = not bool(np.allclose(mz, mz.T, equal_nan=True))
    absw = np.abs(mz) * off
    sym = np.maximum(absw, absw.T)
    if binary:
        threshold = None
        linked = sym > 0
    else:
        top = float(sym.max()) if sym.size else 0.0
        threshold = 0.5 if fin.size and fin.min() >= 0 and fin.max() <= 1 else 0.1 * top
        linked = sym > threshold if top > 0 else np.zeros_like(sym, dtype=bool)
    degree_all = linked.sum(1).astype(float) if binary else (sym * linked).sum(1)
    shown = np.arange(n)
    if n > MAX_NET_NODES:
        shown = np.sort(np.argsort(-degree_all, kind="stable")[:MAX_NET_NODES])
    labels_all = _labels(ds, node)
    sub = sym[np.ix_(shown, shown)]
    sub_link = linked[np.ix_(shown, shown)]
    pos = spectral_layout(np.where(sub_link, sub, 0.0), seed=0)
    order: list[int] = []
    matrix: list[list[float | None]] | None = None
    if shown.size <= MAX_NET_MATRIX:
        order = [int(i) for i in fiedler_order(sub)]
        mm = m[np.ix_(shown, shown)]
        matrix = [[fv(mm[a, b]) for b in order] for a in order]
    ii, jj = np.nonzero(np.triu(sub_link, 1))
    ws = np.array(
        [
            mz[shown[i], shown[j]] if mz[shown[i], shown[j]] != 0 else mz[shown[j], shown[i]]
            for i, j in zip(ii, jj, strict=True)
        ]
    )
    keep = (
        np.argsort(-np.abs(ws), kind="stable")[:MAX_NET_EDGES]
        if ws.size
        else np.array([], dtype=int)
    )
    edges = [NetworkEdge(i=int(ii[k]), j=int(jj[k]), w=float(ws[k])) for k in sorted(keep.tolist())]
    n_links = int(np.triu(linked, 1).sum())
    if directed:
        dl = (absw > (threshold or 0)) & off
        arcs = int(dl.sum())
        density = arcs / (n * (n - 1)) if n > 1 else None
        recip = float((dl & dl.T).sum()) / arcs if arcs else None
    else:
        density = n_links / (n * (n - 1) / 2) if n > 1 else None
        recip = None
    a = linked.astype(float)
    deg = a.sum(1)
    triples = float((deg * (deg - 1)).sum())
    transitivity = float(np.trace(a @ a @ a)) / triples if triples > 0 and n <= 3000 else None
    comps = _components(linked)
    attrs: dict[str, list[Any]] = {}
    homophily: list[Homophily] = []
    for attr in shape.extra.get("node_attrs", ())[:32]:
        vals = [jsonable(v) for v in np.asarray(ds[attr].values)]
        attrs[attr] = [vals[i] for i in shown]
        uniq = {str(v) for v in vals}
        if 2 <= len(uniq) <= 50 and n_links:
            iu, ju = np.nonzero(np.triu(linked, 1))
            same = sum(1 for i, j in zip(iu, ju, strict=True) if str(vals[i]) == str(vals[j]))
            counts = [sum(1 for v in vals if str(v) == u) for u in uniq]
            expected = sum(k * (k - 1) for k in counts) / (n * (n - 1)) if n > 1 else None
            homophily.append(
                Homophily(attr=attr, within_share=same / n_links, expected_share=fv(expected))
            )
    return NetworkEntry(
        var=var,
        node_dim=node,
        facet=facet,
        binary=binary,
        signed=signed,
        directed=directed,
        weighted=not binary,
        n_nodes=n,
        labels=[labels_all[i] for i in shown],
        omitted_nodes=n - int(shown.size),
        matrix=matrix,
        order=order,
        positions=NetPositions(x=[float(v) for v in pos[:, 0]], y=[float(v) for v in pos[:, 1]]),
        edges=edges,
        omitted_edges=max(0, int(ii.size) - len(edges)),
        threshold=threshold,
        degree=[float(degree_all[i]) for i in shown],
        degree_hist=_count_histogram(degree_all),
        density=fv(min(1.0, density)) if density is not None else None,
        transitivity=fv(min(1.0, transitivity)) if transitivity is not None else None,
        reciprocity=fv(recip) if recip is not None else None,
        components=NetComponents(count=len(comps), largest_share=max(comps) / n if n else 0.0),
        node_attrs=attrs,
        homophily=homophily,
    )


def network(c: DataContext) -> NetworkView:
    shapes = _need(c, "network")
    items: list[NetworkEntry] = []
    omitted: list[str] = []
    for shape in shapes:
        facets = [d for d in shape.extra.get("facets", ()) if d in c.ds.dims]
        levels = [_labels(c.ds, d) for d in facets]
        for var in shape.vars:
            for idx in itertools.product(*[range(len(lv)) for lv in levels]):
                facet = {d: lv[i] for d, lv, i in zip(facets, levels, idx, strict=True)}
                if len(items) >= 16:
                    omitted.append(f"{var}{list(facet.values()) if facet else ''}"[:256])
                    continue
                items.append(
                    _network_entry(c, shape, var, facet, dict(zip(facets, idx, strict=True)))
                )
    return NetworkView(
        items=items,
        omitted=omitted[:256],
        warnings_plain=[f"The view does not show {len(omitted)} more networks."] if omitted else [],
        caption_plain="Shows which nodes are linked: as a matrix (similar nodes together) and "
        "as a drawing. Larger dots have more links.",
    )


# ------------------------------------------------------------------------------ tree


def _tree_arrays(
    ds: xr.Dataset, node: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str | None]]:
    p = np.asarray(ds["parent"].values).astype(np.int64)
    n = p.size
    idx = np.arange(n)
    p = np.where(p == idx, -1, p)
    if "branch_length" in ds.data_vars and ds["branch_length"].dims == (node,):
        bl = np.nan_to_num(np.asarray(ds["branch_length"].values, dtype=float), nan=0.0)
    else:
        bl = np.ones(n)
    has_child = np.zeros(n, dtype=bool)
    has_child[p[p >= 0]] = True
    if "is_tip" in ds.data_vars and ds["is_tip"].dims == (node,):
        tip = np.asarray(ds["is_tip"].values).astype(bool)
    else:
        tip = ~has_child
    if "tip_label" in ds.data_vars and ds["tip_label"].dims == (node,):
        raw = [jsonable(v) for v in np.asarray(ds["tip_label"].values)]
        labels: list[str | None] = [str(v)[:256] if v not in (None, "") else None for v in raw]
    else:
        labels = [str(v)[:256] if t else None for v, t in zip(_labels(ds, node), tip, strict=True)]
    return p, bl, tip, labels


def _tree_problems(p: np.ndarray, bl: np.ndarray) -> list[str]:
    n = p.size
    problems: list[str] = []
    roots = int(np.sum(p == -1))
    if roots != 1:
        problems.append(f"the tree has {roots} roots (one is expected)")
    if np.any(bl < 0):
        problems.append(f"{int(np.sum(bl < 0))} branch length(s) are negative")
    state = np.zeros(n, dtype=np.int8)  # 0 new, 1 on the path, 2 done
    for s in range(n):
        path = []
        u = s
        while u >= 0 and state[u] == 0:
            state[u] = 1
            path.append(u)
            u = int(p[u])
        if u >= 0 and state[u] == 1:
            problems.append("the parent links have a cycle")
            break
        for v in path:
            state[v] = 2
    return problems


def _layout(p: np.ndarray, bl: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """``(x, y, tips in drawing order)``: x = distance from the root, tips at y = 0, 1, ..."""
    n = p.size
    children: list[list[int]] = [[] for _ in range(n)]
    for v in range(n):
        if p[v] >= 0:
            children[int(p[v])].append(v)
    x = np.zeros(n)
    y = np.full(n, np.nan)
    order: list[int] = []
    post: list[int] = []
    stack = [(int(r), 0.0) for r in reversed(np.nonzero(p == -1)[0].tolist())]
    while stack:
        v, base = stack.pop()
        x[v] = base
        post.append(v)
        if not children[v]:
            y[v] = len(order)
            order.append(v)
        for ch in reversed(children[v]):
            stack.append((ch, base + float(bl[ch])))
    for v in reversed(post):
        if children[v]:
            ys = y[children[v]]
            y[v] = (np.nanmin(ys) + np.nanmax(ys)) / 2
    return x, np.nan_to_num(y), order


def _patristic(p: np.ndarray, x: np.ndarray, tips: Sequence[int]) -> np.ndarray:
    """Distances between ``tips`` along the tree (x_a + x_b - 2 x_lca)."""
    n = p.size
    pos = {t: k for k, t in enumerate(tips)}
    children: list[list[int]] = [[] for _ in range(n)]
    for v in range(n):
        if p[v] >= 0:
            children[int(p[v])].append(v)
    below: dict[int, list[int]] = {}
    d = np.zeros((len(tips), len(tips)))
    stack = [(int(r), False) for r in np.nonzero(p == -1)[0]]
    while stack:
        v, done = stack.pop()
        if not done:
            stack.append((v, True))
            stack.extend((ch, False) for ch in children[v])
            continue
        lists = [below.pop(ch, []) for ch in children[v]]
        mine = [pos[v]] if v in pos else []
        groups = [g for g in [mine, *lists] if g]
        for a_i, ga in enumerate(groups):
            for gb in groups[a_i + 1 :]:
                ia, ib = np.array(ga), np.array(gb)
                block = (
                    x[[tips[i] for i in ga]][:, None] + x[[tips[i] for i in gb]][None, :] - 2 * x[v]
                )
                d[np.ix_(ia, ib)] = block
                d[np.ix_(ib, ia)] = block.T
        below[v] = [i for g in groups for i in g]
    return d


def tree(c: DataContext) -> TreeView:
    shape = _need(c, "tree")[0]
    ds = c.ds
    node = shape.dims["node"]
    p, bl, tip, labels = _tree_arrays(ds, node)
    n = p.size
    if n > MAX_TREE_NODES:
        raise InsufficientData(f"the tree has {n} nodes; the explorer draws {MAX_TREE_NODES}")
    problems = _tree_problems(p, bl)
    if any("cycle" in x for x in problems):
        raise InsufficientData("the parent links have a cycle, so the tree cannot be drawn")
    x, y, order = _layout(p, bl)
    tips = [v for v in order if tip[v]] or order
    tip_dim = shape.dims.get("tip")
    traits: list[TipTrait] = []
    matched_by = None
    unmatched = 0
    rows: list[int | None] = [None] * len(tips)
    if tip_dim is not None and shape.vars:
        tlabels = [str(v) for v in _labels(ds, tip_dim)]
        where = {lab: k for k, lab in enumerate(tlabels)}
        by_label = [where.get(str(labels[v])) if labels[v] is not None else None for v in tips]
        if sum(r is not None for r in by_label) >= max(1, len(tips) // 2):
            rows, matched_by = by_label, "label"
        elif int(tip.sum()) == len(tlabels):
            node_order = [v for v in range(n) if tip[v]]
            rank = {v: k for k, v in enumerate(node_order)}
            rows, matched_by = [rank.get(v) for v in tips], "count"
        else:
            rows = [k if k < len(tlabels) else None for k in range(len(tips))]
            matched_by = "order"
        unmatched = sum(r is None for r in rows)
        for var in shape.vars[:64]:
            vals = np.asarray(ds[var].values, dtype=float)
            traits.append(
                TipTrait(
                    var=var,
                    units=_units(c, var),
                    values=[fv(vals[r]) if r is not None else None for r in rows],
                )
            )
    signal: list[PhyloSignal] = []
    if traits and len(tips) <= _MAX_SIGNAL_TIPS:
        d = _patristic(p, x, tips)
        with np.errstate(divide="ignore"):
            w = np.where(d > 0, 1.0 / d, 0.0)
        np.fill_diagonal(w, 0.0)
        for t in traits:
            vals = np.array([np.nan if v is None else v for v in t.values], dtype=float)
            i_, _e, _z, p_ = _moran(vals, w)
            signal.append(PhyloSignal(var=t.var, morans_i=fv(i_), p_value=p_))
    tip_x = x[tips] if tips else np.array([0.0])
    depth = float(x.max()) if n else None
    ultrametric = bool(depth and np.allclose(tip_x, depth, rtol=1e-4, atol=1e-9))
    return TreeView(
        node_dim=node,
        n_nodes=n,
        n_tips=int(tip.sum()),
        roots=[int(r) for r in np.nonzero(p == -1)[0]],
        depth=fv(depth),
        ultrametric=ultrametric,
        layout=TreeNodeLayout(
            x=[float(v) for v in x],
            y=[float(v) for v in y],
            parent=[int(v) for v in p],
            is_tip=[bool(v) for v in tip],
            label=labels,
        ),
        tips=[int(v) for v in tips],
        tip_dim=tip_dim,
        matched_by=matched_by,
        unmatched=unmatched,
        traits=traits,
        signal=signal,
        warnings_plain=[f"{p_.capitalize()}." for p_ in problems][:10],
        caption_plain="Shows the tree with the traits of its tips beside it. Moran's I above "
        "0 shows that close relatives have similar traits (a phylogenetic signal). This is a "
        "tree stored in the variables, not a tree dataset.",
    )


# -------------------------------------------------------------------------- circular


_DEFAULT_BINS: Final = {"deg": 36, "rad": 36, "h": 24, "month": 12, "d": 52}


def _circ_stats(theta: np.ndarray, period: float) -> dict[str, float | None]:
    n = theta.size
    if n == 0:
        return {
            "mean_direction": None,
            "resultant_length": None,
            "circular_sd": None,
            "rayleigh_z": None,
            "rayleigh_p": None,
        }
    cbar, sbar = float(np.mean(np.cos(theta))), float(np.mean(np.sin(theta)))
    r = min(1.0, math.hypot(cbar, sbar))
    mean = (math.atan2(sbar, cbar) % (2 * math.pi)) / (2 * math.pi) * period
    sd = math.sqrt(-2 * math.log(r)) / (2 * math.pi) * period if r > 0 else None
    z = n * r * r
    rn = n * r
    p = math.exp(math.sqrt(1 + 4 * n + 4 * (n * n - rn * rn)) - (1 + 2 * n))
    return {
        "mean_direction": mean if r > 1e-12 else None,
        "resultant_length": r,
        "circular_sd": sd,
        "rayleigh_z": z,
        "rayleigh_p": min(1.0, max(0.0, p)),
    }


def _circ_group(label: Any, v: np.ndarray, period: float, edges: np.ndarray) -> CircularGroup:
    v = np.mod(v[np.isfinite(v)], period)
    counts, _ = np.histogram(v, bins=edges)
    st = _circ_stats(v / period * 2 * math.pi, period)
    return CircularGroup(
        label=label,
        n=int(v.size),
        counts=[int(k) for k in counts],
        mean_direction=fv(st["mean_direction"]),
        resultant_length=fv(st["resultant_length"]),
        circular_sd=fv(st["circular_sd"]),
        rayleigh_z=fv(st["rayleigh_z"]),
        rayleigh_p=fv(st["rayleigh_p"]),
    )


def _js_rho(a: np.ndarray, b: np.ndarray) -> float | None:
    """Jammalamadaka-SenGupta circular correlation of two angle arrays (radians)."""
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 3:
        return None
    ma = math.atan2(float(np.mean(np.sin(a))), float(np.mean(np.cos(a))))
    mb = math.atan2(float(np.mean(np.sin(b))), float(np.mean(np.cos(b))))
    sa, sb = np.sin(a - ma), np.sin(b - mb)
    den = math.sqrt(float((sa**2).sum()) * float((sb**2).sum()))
    return max(-1.0, min(1.0, float((sa * sb).sum()) / den)) if den > 0 else None


def circular(c: DataContext) -> CircularView:
    shapes = _need(c, "circular")
    items: list[CircularEntry] = []
    pairs: list[CircularPair] = []
    for shape in shapes:
        periods: dict[str, float] = dict(shape.extra.get("periods", {}))
        units: dict[str, str] = dict(shape.extra.get("units", {}))
        gvar = shape.extra.get("group_var")
        gdim = shape.extra.get("group_dim")
        codes = np.asarray(c.ds[gvar].values).ravel() if gvar else None
        names = _labels(c.ds, gdim) if gdim and gdim in c.ds.dims else None
        for var in shape.vars:
            if len(items) >= 16:
                break
            period, u = periods[var], units[var]
            nb = min(MAX_CIRC_BINS, _DEFAULT_BINS.get(u, 36))
            edges = np.linspace(0.0, period, nb + 1)
            vals = np.asarray(c.ds[var].values, dtype=float).ravel()
            groups = [_circ_group("all", vals, period, edges)]
            omitted = 0
            if codes is not None and codes.shape == vals.shape:
                levels, counts = np.unique(codes, return_counts=True)
                ranked = [levels[i] for i in np.argsort(-counts, kind="stable")]
                omitted = max(0, len(ranked) - MAX_CIRC_GROUPS)
                for code in ranked[:MAX_CIRC_GROUPS]:
                    k = int(code) if np.issubdtype(type(code), np.integer) else None
                    label = (
                        names[k]
                        if names is not None and k is not None and 0 <= k < len(names)
                        else jsonable(code)
                    )
                    groups.append(_circ_group(label, vals[codes == code], period, edges))
            compass = u in ("deg", "h", "d", "month")
            items.append(
                CircularEntry(
                    var=var,
                    units=u,
                    period=period,
                    zero="north" if compass else "east",
                    clockwise=compass,
                    group_by=(gdim or gvar) if gvar else None,
                    bins=[float(e) for e in edges],
                    groups=groups,
                    omitted_groups=omitted,
                )
            )
        for a, b in itertools.combinations(shape.vars, 2):
            if len(pairs) >= 8:
                break
            va = np.asarray(c.ds[a].values, dtype=float).ravel()
            vb = np.asarray(c.ds[b].values, dtype=float).ravel()
            pa, pb = periods[a], periods[b]
            na = min(24, _DEFAULT_BINS.get(units[a], 24))
            nbb = min(24, _DEFAULT_BINS.get(units[b], 24))
            ea, eb = np.linspace(0, pa, na + 1), np.linspace(0, pb, nbb + 1)
            ok = np.isfinite(va) & np.isfinite(vb)
            h2, _, _ = np.histogram2d(np.mod(va[ok], pa), np.mod(vb[ok], pb), bins=[ea, eb])
            rho = _js_rho(va / pa * 2 * math.pi, vb / pb * 2 * math.pi)
            pairs.append(
                CircularPair(
                    a=a,
                    b=b,
                    a_bins=[float(e) for e in ea],
                    b_bins=[float(e) for e in eb],
                    counts=[[int(k) for k in row] for row in h2],
                    rho=fv(rho),
                )
            )
    return CircularView(
        items=items,
        pairs=pairs,
        caption_plain="Shows each angle or time of day as a rose: a longer bar has more "
        "values. The line shows the mean direction; a longer line shows a stronger direction.",
    )


# ---------------------------------------------------------------------------- points


def _lab_to_hex(lab: np.ndarray) -> list[str]:
    """CIE L*a*b* (D65) rows as sRGB ``#rrggbb`` (clipped to the gamut)."""
    lightness, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    fy = (lightness + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    eps = 6 / 29

    def finv(t: np.ndarray) -> np.ndarray:
        return np.where(t > eps, t**3, 3 * eps**2 * (t - 4 / 29))

    xyz = np.stack([0.95047 * finv(fx), finv(fy), 1.08883 * finv(fz)], axis=1)
    m = np.array(
        [[3.2404542, -1.5371385, -0.4985314], [-0.9692660, 1.8760108, 0.0415560],
         [0.0556434, -0.2040259, 1.0572252]]
    )  # fmt: skip
    rgb = xyz @ m.T
    rgb = np.where(
        rgb <= 0.0031308, 12.92 * rgb, 1.055 * np.power(np.clip(rgb, 0, None), 1 / 2.4) - 0.055
    )
    rgb = np.clip(np.nan_to_num(rgb), 0, 1)
    return ["#{:02x}{:02x}{:02x}".format(*(int(v * 255 + 0.5) for v in row)) for row in rgb]


def points(c: DataContext) -> PointsView:
    from scipy import stats as sps
    from scipy.spatial import cKDTree

    shape = _need(c, "points")[0]
    ds = c.ds
    pdim = shape.dims["point"]
    xv, yv = str(shape.extra["x"]), str(shape.extra["y"])
    xs = np.asarray(ds[xv].values, dtype=float)
    ys = np.asarray(ds[yv].values, dtype=float)
    n = xs.size
    idx = np.array(even(n, MAX_POINTS), dtype=int)
    labels = _labels(ds, pdim)
    density = None
    if n > MAX_POINTS:
        ok = np.isfinite(xs) & np.isfinite(ys)
        h2, ye, xe = np.histogram2d(ys[ok], xs[ok], bins=MAX_POINT_DENSITY)
        density = PointDensity(
            x_edges=[float(e) for e in xe],
            y_edges=[float(e) for e in ye],
            counts=[[int(k) for k in row] for row in h2],
        )
    colour = [
        PointColour(var=v, units=_units(c, v), values=_floats(np.asarray(ds[v].values)[idx]))
        for v in shape.vars[:32]
        if ds[v].dims == (pdim,)
    ]
    lab = shape.extra.get("lab")
    srgb = None
    if lab:
        arr = np.stack([np.asarray(ds[v].values, dtype=float)[idx] for v in lab], axis=1)
        srgb = _lab_to_hex(arr)
    answers = None
    avar = shape.extra.get("answers")
    if isinstance(avar, str) and avar in ds.data_vars:
        by = next(str(d) for d in ds[avar].dims if d != pdim)
        raw = np.asarray(ds[avar].transpose(pdim, by).values).astype(str)
        valid = (raw != "") & (raw != "nan") & (raw != "None")
        terms = sorted({t for t in raw[valid].tolist()})[:64]
        tpos = {t: k for k, t in enumerate(terms)}
        modes: list[str | None] = []
        agree: list[float | None] = []
        counts: list[list[int]] = []
        for i in idx:
            row = [0] * len(terms)
            for t in raw[i][valid[i]]:
                if t in tpos:
                    row[tpos[t]] += 1
            total = sum(row)
            if total:
                k = int(np.argmax(row))
                modes.append(terms[k][:256])
                agree.append(row[k] / total)
            else:
                modes.append(None)
                agree.append(None)
            counts.append(row)
        answers = PointAnswers(
            var=avar,
            by_dim=by,
            terms=[t[:256] for t in terms],
            mode=modes,
            agreement=agree,
            counts=counts,
        )
    lattice = bool(shape.extra.get("lattice"))
    ce = None
    ok = np.isfinite(xs) & np.isfinite(ys)
    if not lattice and ok.sum() >= 10:
        pts = np.c_[xs[ok], ys[ok]][:20_000]
        m = pts.shape[0]
        area = float(np.ptp(pts[:, 0]) * np.ptp(pts[:, 1]))
        if area > 0:
            dist, _ = cKDTree(pts).query(pts, k=2)
            r_obs = float(dist[:, 1].mean())
            r_exp = 0.5 / math.sqrt(m / area)
            se = 0.26136 / math.sqrt(m * m / area)
            z = (r_obs - r_exp) / se
            ce = ClarkEvans(r=fv(r_obs / r_exp), z=fv(z), p_value=fv(2 * sps.norm.sf(abs(z))))
    return PointsView(
        point_dim=pdim,
        x=xv,
        y=yv,
        lattice=lattice,
        n=n,
        sample=PointSample(
            x=_floats(xs[idx]),
            y=_floats(ys[idx]),
            index=[int(i) for i in idx],
            labels=[labels[i] for i in idx],
        ),
        density=density,
        colour=colour,
        srgb=srgb,
        lab_vars=list(lab) if lab else [],
        answers=answers,
        clark_evans=ce,
        warnings_plain=[f"The view shows {idx.size} of {n} points."] if idx.size < n else [],
        caption_plain="Shows each point at its place. Select what gives the colour: a "
        "variable, the colour of the point, the most frequent answer or the agreement.",
    )


# --------------------------------------------------------------------------- quality


_NOT_MEASURED: Final = frozenset(
    {Role.CONSTANT, Role.WEIGHT, Role.CONTROL, Role.EXPOSURE, Role.MASK, Role.INDEX}
)


def _num_da(da: xr.DataArray) -> bool:
    return bool(np.issubdtype(da.dtype, np.number))


def _sample_cells(da: xr.DataArray) -> np.ndarray:
    """At most about :data:`_SAMPLE_CELLS` finite values of ``da`` (float32): an even
    sample along its first dim when it is larger."""
    if da.size > _SAMPLE_CELLS and da.ndim >= 1:
        d = str(da.dims[0])
        per = max(1, da.size // int(da.sizes[d]))
        da = da.isel({d: even(int(da.sizes[d]), max(1, _SAMPLE_CELLS // per))})
    a = np.asarray(da.values, dtype=np.float32).ravel()
    return a[np.isfinite(a)]


def _qw(
    code: str, severity: str, message: str, action: str | None = None, var: str | None = None
) -> QualityWarning:
    return QualityWarning.model_validate(
        {
            "code": code,
            "severity": severity,
            "var": var,
            "message_plain": message[:500],
            "suggested_action": action[:500] if action else None,
        }
    )


def structure_warnings(c: DataContext) -> list[QualityWarning]:
    """Quality warnings about the data shapes (polygons, trees, networks, angles, images)."""
    out: list[QualityWarning] = []
    ds = c.ds
    s = c.shapes
    for shape in s.of("areal_map"):
        area, vert = shape.dims["area"], shape.dims["vertex"]
        rx = np.asarray(ds["ring_x"].transpose(area, vert).values, dtype=float)
        ry = np.asarray(ds["ring_y"].transpose(area, vert).values, dtype=float)
        empty = [i for i in range(rx.shape[0]) if not np.isfinite(rx[i]).any()]
        if empty:
            out.append(
                _qw(
                    "GEOMETRY_INVALID",
                    "warning",
                    f"{len(empty)} area(s) have no polygon in ring_x and ring_y.",
                    "Add the outline of each area, or remove the area.",
                    "ring_x",
                )
            )
        open_rings = 0
        for i in range(rx.shape[0]):
            for px, py in _rings(rx[i], ry[i]):
                if px.size >= 3 and (px[0] != px[-1] or py[0] != py[-1]):
                    open_rings += 1
        if open_rings:
            out.append(
                _qw(
                    "GEOMETRY_INVALID",
                    "info",
                    f"{open_rings} polygon ring(s) do not end at their first point.",
                    "The map closes them. Close the rings in the source data to be sure.",
                    "ring_x",
                )
            )
    for shape in s.of("tree"):
        p, bl, _tip, _lab = _tree_arrays(ds, shape.dims["node"])
        for problem in _tree_problems(p, bl):
            sev = "error" if "cycle" in problem else "warning"
            out.append(
                _qw(
                    "TREE_INVALID",
                    sev,
                    f"In parent and branch_length, {problem}.",
                    "Correct the tree links in the source data.",
                    "parent",
                )
            )
    for shape in s.of("network"):
        v = shape.vars[0]
        m = np.nan_to_num(np.asarray(ds[v].values, dtype=float))
        node = shape.dims["node"]
        m2 = np.asarray(ds[v].transpose(node, node + "_", ...).values, dtype=float)
        m2 = np.nan_to_num(m2)
        if m.size and not np.allclose(m2, np.swapaxes(m2, 0, 1)):
            out.append(
                _qw(
                    "NETWORK_ASYMMETRIC",
                    "info",
                    f"{v!r} is not symmetric: a link from A to B differs from B to A.",
                    "The explorer reads it as a directed network. If links have no "
                    "direction, make the matrix symmetric.",
                    v,
                )
            )
    for shape in s.of("circular"):
        for v in shape.vars:
            period = float(shape.extra["periods"][v])
            a = np.asarray(ds[v].values, dtype=float)
            a = a[np.isfinite(a)]
            outside = int(np.sum((a < 0) | (a >= period + (1 if period > 300 else 0))))
            if outside:
                out.append(
                    _qw(
                        "ANGLE_RANGE",
                        "info",
                        f"{outside} value(s) of {v!r} are outside 0 to {period:g}.",
                        "The explorer wraps them around the circle. Examine if they are errors.",
                        v,
                    )
                )
    for shape in s.of("image"):
        for v in shape.vars:
            if c.roles.get(v) in _NOT_MEASURED:
                continue  # a setting or a weight, not a reading of a detector
            da = ds[v]
            if da.attrs.get(_IMAGE_ATTR) == "1":
                continue  # pictures (an image folder): white at 255 is not a saturated detector
            if da.size < 100 or not _num_da(da):
                continue
            lo, hi = float(da.min(skipna=True)), float(da.max(skipna=True))
            if not np.isfinite(lo) or not hi > lo:
                continue
            share = saturated_share(_sample_cells(da), lo, hi)
            if share >= 0.01:
                out.append(
                    _qw(
                        "IMAGE_SATURATED",
                        "warning",
                        f"{share:.1%} of the cells of {v!r} are at the maximum value.",
                        "The detector or the scale can be saturated. Values at the "
                        "maximum can be larger in reality.",
                        v,
                    )
                )
    return out


# ------------------------------------------------------------------------ recipe plots


def _plain_tree(ds: xr.Dataset, geometry: Sequence[str] = ()) -> xr.DataTree:
    clean = ds.copy()
    clean.attrs = {}
    for name in clean.variables:
        clean[name].attrs = {k: v for k, v in clean[name].attrs.items() if k == "units"}
    return xr.DataTree(clean)


_ROSE_UNITS: Final = {"deg": "degrees", "rad": "radians", "h": "hours", "d": "days"}


def science_plots(section: str, ds: xr.Dataset, structure: Structure) -> list[dict[str, Any]]:
    """The Plotly recipes that work on raw data for a section (``sci.area_map``,
    ``net.graph``, ``sci.rose``), as PlotSpec documents. A recipe that fails, or a plot above
    2 MB, is left out."""
    try:
        import mapi_plots.packs.net
        import mapi_plots.packs.sci_maps  # noqa: F401 - registers sci.area_map, sci.rose
        from mapi_plots.recipes._base import registered
    except ImportError:  # pragma: no cover - the recipes extra is in the runner image
        return []
    calls: list[tuple[str, dict[str, Any]]] = []
    if section == "map":
        shape = structure.first("areal_map")
        if shape is not None and shape.vars:
            calls.append(("sci.area_map", {"var": shape.vars[0], "group": "auto"}))
    elif section == "network":
        for shape in structure.of("network")[:2]:
            calls.append(("net.graph", {"var": shape.vars[0], "node_dim": shape.dims["node"]}))
    elif section == "circular":
        for shape in structure.of("circular")[:1]:
            for v in shape.vars[:3]:
                u = _ROSE_UNITS.get(str(shape.extra["units"][v]))
                if u is not None:
                    calls.append(("sci.rose", {"observed": v, "units": u}))
    if not calls:
        return []
    recipes = registered()
    data = _plain_tree(ds)
    out: list[dict[str, Any]] = []
    for rid, opts in calls:
        recipe = recipes.get(rid)
        if recipe is None:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                spec = recipe.render(data, **opts)
            doc = spec.model_dump(mode="json", exclude_none=True)
        except Exception:
            continue
        import json

        if len(json.dumps(doc, separators=(",", ":"))) > _MAX_PLOT_BYTES:
            continue
        # one id per plot: three roses (or two networks) of a section must not share one
        which = opts.get("observed") or opts.get("var")
        if which and sum(1 for r, _o in calls if r == rid) > 1:
            safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(which))
            doc["id"] = f"{doc.get('id') or rid}.{safe}"[:128]
        out.append(doc)
    return out


SCIENCE_SECTION_FUNCS: Final[dict[str, Callable[[DataContext], BaseModel]]] = {
    "structure": structure,
    "images": images,
    "gallery": gallery,
    "profiles": profiles,
    "map": map_,
    "network": network,
    "tree": tree,
    "circular": circular,
    "points": points,
}
"""The scientific sections (keys = :data:`mapi_spec.views.SCIENCE_SECTIONS`)."""
