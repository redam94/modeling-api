"""The Data Explorer (M3.5): one :mod:`mapi_spec.views` document per section, computed in
the sandbox from a dataset and its schema.

========================  ====================================================================
section                   what it shows
========================  ====================================================================
``overview``              dims, variables, roles, missing share, a plain summary, a quality score
``distributions``         histogram, moments, quantiles and zero share per variable
``timeseries``            every variable over time, faceted by ``facet_by`` (or every series)
``missingness``           the missingness heatmap, share and longest gap per variable
``outliers``              unusual values by a robust score (``mad``, ``iqr`` or ``zscore``)
``correlation``           Pearson/Spearman matrix with p-values between the drivers and KPI
``ccf``                   correlation of each driver with the KPI by lag (driver leads: k > 0)
``acf_pacf``              (partial) autocorrelations per series with suggested ARMA orders
``stationarity``          ADF + KPSS per series and the combined verdict
``stl``                   robust STL trend/season/remainder with their strengths
``vif``                   variance inflation per driver and the condition number
``groups``                each variable across the levels of ``group_by`` (ANOVA/Kruskal)
``quality``               plain-language data-quality warnings with suggested actions
========================  ====================================================================

plus the sections for scientific data (``structure``, ``images``, ``gallery``, ``profiles``,
``map``, ``network``, ``tree``, ``circular``, ``points``;
:mod:`~mapi_runner.worker.explore_science`). v0.14: the tabular statistics read an even sample
of a variable with more than 5,000,000 cells (a stack of images), never the whole array.

Every section reads the data shapes of :func:`~mapi_runner.worker.structure.detect_structure`
(``DataContext.structure``): geometry (polygon rings, neighbour and tree links, x/y
positions), masks and labels are not data; an observation index is not time; the spatial
dims of an image are reduced, never split into one series per pixel.

:func:`ingest_sections` are written by every ingest run (``ui/explore/``); the others run
on demand in an ``explore`` run. Columns are built at the time level: panel dims (geo,
store) are summed for additive roles (target, media, spend) and averaged otherwise, and a
small categorical dim (up to :data:`MAX_SPLIT_LEVELS` levels, e.g. ``channel``) splits a
variable into one column per level (``spend[tv]``). Without time, columns are the variables
along the unit dim (areas, species), the columns of a table (``level[raf]``) or the pixels
of images that share a grid.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.future import season_length
from mapi_sdk.schema import infer_freq
from mapi_spec.explore import DataExploreRequest
from mapi_spec.schema import DataSchema, Role
from mapi_spec.views import (
    MAX_EXPLORE_POINTS,
    MAX_EXPLORE_SERIES,
    MAX_EXPLORE_VARS,
    AcfPacfView,
    CorrelationView,
    CrossCorrelationView,
    DataOverviewView,
    DimOverview,
    DistributionsView,
    GroupComparison,
    GroupComparisonView,
    GroupStats,
    Histogram,
    LagProfile,
    MissingnessGrid,
    MissingnessView,
    OutlierPoint,
    OutliersView,
    QualityView,
    QualityWarning,
    SeriesView,
    StationarityRow,
    StationarityView,
    StlComponents,
    StlView,
    TimeSeriesView,
    VarDistribution,
    VarOutliers,
    VarOverview,
    VifView,
)
from pydantic import BaseModel

from mapi_runner.worker import stats
from mapi_runner.worker.explore_common import (
    InsufficientData,
    NeedsOption,
    NotApplicable,
    SectionSkipped,
    jsonable,
)
from mapi_runner.worker.explore_common import fv as _fv
from mapi_runner.worker.explore_science import SCIENCE_SECTION_FUNCS
from mapi_runner.worker.rasters import even
from mapi_runner.worker.structure import Structure, detect_structure, time_strength

__all__ = [
    "CHEAP_SECTIONS",
    "DATA_SECTION_FUNCS",
    "MAX_SPLIT_LEVELS",
    "MIN_TIME_PERIODS",
    "DataContext",
    "InsufficientData",
    "NeedsOption",
    "NotApplicable",
    "SectionSkipped",
    "data_columns",
    "data_context",
    "explore_period",
    "ingest_sections",
    "jsonable",
    "quality_warnings",
]

CHEAP_SECTIONS: Final = (
    "overview",
    "distributions",
    "timeseries",
    "missingness",
    "outliers",
    "correlation",
    "quality",
)
"""Sections every ingest run writes (seconds even for large panels); :func:`ingest_sections`
adds ``structure`` and the picture sections that apply."""
MAX_SPLIT_LEVELS: Final = 12
"""A categorical dim with at most this many levels splits a variable into columns."""
MIN_TIME_PERIODS: Final = 16
"""Periods a series needs for the lag, stationarity and trend sections."""
_ADDITIVE: Final = frozenset({Role.TARGET, Role.MEDIA, Role.SPEND, Role.EXPOSURE, Role.WEIGHT})
_DRIVERS: Final = frozenset({Role.MEDIA, Role.SPEND, Role.COVARIATE, Role.CONTROL, Role.EXPOSURE})
_HIST_BINS: Final = 30
_REDUCED_ROLES: Final = frozenset({"y", "x", "along", "vertex", "index", "item"})
"""Dims of these parts (``role_in_data``) are reduced in series, never split per level."""


def explore_period(freq: str | None) -> int | None:
    """The season length used for STL and seasonal benchmarks: a week for daily data, a day
    for hourly data, a year otherwise (``None`` when the frequency is unknown)."""
    if not freq:
        return None
    head = freq.upper().lstrip("0123456789")
    if head.startswith("D") or head.startswith("B"):
        return 7 if head.startswith("D") else 5
    if head.startswith("H"):
        return 24
    n = season_length(freq)
    return n if n is None or n >= 2 else None


@dataclass
class DataContext:
    """A dataset prepared for exploration."""

    ds: xr.Dataset
    schema: DataSchema | None
    req: DataExploreRequest
    time_dim: str | None
    freq: str | None
    period: int | None
    vars: list[str]
    roles: dict[str, Role]
    kpi: str | None
    warnings: list[str] = field(default_factory=list)
    structure: Structure | None = None

    @property
    def shapes(self) -> Structure:
        """The detected structure (computed on first use when the context was built by
        hand)."""
        if self.structure is None:
            self.structure = detect_structure(self.ds, self.schema, self.req.hints)
        return self.structure

    @property
    def strong_time(self) -> bool:
        """The time dim is real time (``kind: time``, a datetime coord, or the request's)."""
        return self.time_dim is not None and (
            self.time_dim == self.req.time_dim or self.shapes.time_dim == self.time_dim
        )

    @property
    def curve_vars(self) -> frozenset[str]:
        """Variables of a profile or a spectrum (E79 ``reference_dl`` along ``multipole``):
        each value is a point of a curve, not a sample, so a value far from the median (the
        peak of a spectrum) is not an unusual value."""
        return frozenset(v for shape in self.shapes.of("profile") for v in shape.vars)

    def reduced_dims(self) -> set[str]:
        """Dims that are reduced in a series: the pixels, positions and observation ids of a
        picture shape (image, profile, map, points, circular). The other dims (an integer
        geo or channel code of a panel) still split the series."""
        s = self.shapes
        if s.plain:
            return set()
        return {
            d
            for shape in s.shapes
            if shape.kind not in ("timeseries", "sequence")
            for d in shape.dims.values()
            if s.dim_roles.get(d) in _REDUCED_ROLES
        }

    @property
    def max_series(self) -> int:
        return min(self.req.max_series, MAX_EXPLORE_SERIES)

    def role(self, var: str) -> Role:
        return self.roles.get(var, Role.COVARIATE)

    def drivers(self) -> list[str]:
        return [v for v in self.vars if v != self.kpi and self.role(v) in _DRIVERS]

    def panel_dims(self) -> set[str]:
        """Dims the KPI varies over besides time (geo, store): reduced, never split. Without
        a KPI every non-time dim counts as a panel dim."""
        if self.kpi is None:
            return {str(d) for d in self.ds.dims if d != self.time_dim}
        return {str(d) for d in self.ds[self.kpi].dims if d != self.time_dim}

    def time_coords(self) -> list[Any]:
        assert self.time_dim is not None
        return [jsonable(v) for v in np.asarray(self.ds[self.time_dim].values)]


def _numeric(da: xr.DataArray) -> bool:
    return bool(np.issubdtype(da.dtype, np.number) or np.issubdtype(da.dtype, np.bool_))


def _find_time_dim(ds: xr.Dataset, schema: DataSchema | None, given: str | None) -> str | None:
    if given is not None:
        if given not in ds.dims:
            raise SectionSkipped(f"the data has no dimension called {given!r}")
        return given
    if schema is not None:
        for name, d in schema.dims.items():
            if name in ds.dims and d.kind == "time":
                return name
        for name, d in schema.dims.items():
            # an observation index (report 0..n-1) is not time, even when it can extend
            if name in ds.dims and d.policy.value == "extend" and time_strength(ds, schema, name):
                return name
    for dim in ds.dims:
        if str(dim) in ds.coords and np.asarray(ds[dim].values).dtype.kind == "M":
            return str(dim)
    return None


def data_context(
    ds: xr.Dataset, schema: DataSchema | None, req: DataExploreRequest | None = None
) -> DataContext:
    """Resolve the time dim, frequency, season length, variables and KPI of a request."""
    req = req or DataExploreRequest()
    given_time = req.time_dim or (req.hints.time_dim if req.hints is not None else None)
    time_dim = _find_time_dim(ds, schema, given_time)
    structure = detect_structure(ds, schema, req.hints, time_dim=given_time)
    if req.group_by is not None and "groups" in structure.not_applicable:
        na = {k: v for k, v in structure.not_applicable.items() if k != "groups"}
        structure = Structure(**{**structure.__dict__, "not_applicable": na})
    roles: dict[str, Role] = {}
    if schema is not None:
        roles = {n: v.role for n, v in schema.vars.items()}
    numeric = [str(n) for n in ds.data_vars if _numeric(ds[n])]
    wanted = list(req.vars) or numeric
    missing = [v for v in wanted if v not in ds.data_vars]
    if missing:
        raise SectionSkipped(f"the data has no variable called {missing[0]!r}")
    non_data = structure.non_data
    chosen = [
        v
        for v in wanted
        if v in numeric and roles.get(v) not in (Role.INDEX, Role.MASK) and v not in non_data
    ][:MAX_EXPLORE_VARS]
    freq: str | None = None
    if time_dim is not None:
        if schema is not None and time_dim in schema.dims:
            freq = schema.dims[time_dim].freq
        values = np.asarray(ds[time_dim].values) if time_dim in ds.coords else np.array([])
        if freq is None and values.dtype.kind == "M":
            freq = infer_freq(values)
    period = req.period or explore_period(freq)
    kpi = req.kpi
    if kpi is not None and kpi not in ds.data_vars:
        raise SectionSkipped(f"the data has no variable called {kpi!r} (the KPI)")
    if kpi is None:
        kpi = next((v for v in chosen if roles.get(v) is Role.TARGET), None)
    if req.facet_by:
        unknown = [d for d in req.facet_by if d not in ds.dims]
        if unknown:
            raise SectionSkipped(f"the data has no dimension called {unknown[0]!r}")
    return DataContext(
        ds, schema, req, time_dim, freq, period, chosen, roles, kpi, structure=structure
    )


def ingest_sections(structure: Structure) -> tuple[str, ...]:
    """The sections an ingest writes: :data:`CHEAP_SECTIONS`, ``structure`` and the
    picture sections that apply (the others are listed as not applicable)."""
    from mapi_spec.views import SCIENCE_SECTIONS

    out = [s for s in CHEAP_SECTIONS if s not in structure.not_applicable]
    out += [s for s in SCIENCE_SECTIONS if s not in structure.not_applicable]
    return tuple(out)


_STAT_CELLS: Final = 5_000_000
"""Cells of one variable that the tabular statistics (overview, distributions, missing values,
outliers, quality) read: above it, an even sample along the first dim. A stack of 20 000
images (v0.14) is never copied whole to floats."""


def _stat_array(da: xr.DataArray) -> xr.DataArray:
    """``da``, or an even sample of it along its first dim when it has more than
    :data:`_STAT_CELLS` cells (a dim without a coord gets its positions as coords first, so
    an outlier still names its place)."""
    if da.size <= _STAT_CELLS or da.ndim == 0:
        return da
    d = da.dims[0]
    n = int(da.sizes[d])
    if d not in da.coords:
        da = da.assign_coords({d: np.arange(n)})
    return da.isel({d: even(n, max(1, _STAT_CELLS // max(1, da.size // n)))})


def _sampled(c: DataContext, var: str) -> bool:
    return int(c.ds[var].size) > _STAT_CELLS


_SAMPLED_NOTE: Final = (
    "Some variables have more than 5,000,000 values. The statistics use an even sample of them."
)


# ------------------------------------------------------------------------- series


def _reduce(c: DataContext, da: xr.DataArray, dims: Sequence[str]) -> xr.DataArray:
    dims = [d for d in dims if d in da.dims]
    if not dims:
        return da
    if c.role(str(da.name)) in _ADDITIVE:
        return da.sum(dims, min_count=1)
    return da.mean(dims)


def _labels(ds: xr.Dataset, dim: str) -> list[Any]:
    if dim in ds.coords:
        return [jsonable(v) for v in np.asarray(ds[dim].values)]
    return list(range(int(ds.sizes[dim])))


def iter_series(
    c: DataContext, var: str, *, facet_dims: Sequence[str] | None = None
) -> Iterator[tuple[dict[str, Any], np.ndarray]]:
    """``(facet, values along time)`` of ``var``: one series per combination of the facet
    dims (default: ``facet_by``, else every other dim), the rest reduced."""
    assert c.time_dim is not None
    da = c.ds[var]
    if c.time_dim not in da.dims:
        return
    others = [str(d) for d in da.dims if d != c.time_dim]
    if facet_dims is None and not c.req.facet_by:
        reduced = c.reduced_dims()
        facet_dims = [d for d in others if d not in reduced]
    keep = list(facet_dims) if facet_dims is not None else (list(c.req.facet_by) or others)
    keep = [d for d in keep if d in others]
    da = _reduce(c, da.rename(var), [d for d in others if d not in keep])
    da = da.transpose(c.time_dim, *keep)
    values = np.asarray(da.values, dtype=float)
    if not keep:
        yield {}, values
        return
    levels = [_labels(c.ds, d) for d in keep]
    for idx in itertools.product(*[range(len(lv)) for lv in levels]):
        facet = {d: lv[i] for d, lv, i in zip(keep, levels, idx, strict=True)}
        sel: tuple[Any, ...] = (slice(None), *idx)
        yield facet, values[sel]


def time_columns(
    c: DataContext, vars_: Sequence[str], *, facet: dict[str, Any] | None = None
) -> dict[str, np.ndarray]:
    """Columns along time: panel dims reduced (or selected by ``facet``), small
    categorical dims split into one column per level."""
    assert c.time_dim is not None
    out: dict[str, np.ndarray] = {}
    for var in vars_:
        da = c.ds[var].rename(var)
        if c.time_dim not in da.dims:
            continue
        if facet:
            sel = {d: _index_of(c.ds, d, v) for d, v in facet.items() if d in da.dims}
            da = da.isel(sel)
        others = [str(d) for d in da.dims if d != c.time_dim]
        panel = c.panel_dims()
        split = [d for d in others if d not in panel and c.ds.sizes[d] <= MAX_SPLIT_LEVELS][:1]
        da = _reduce(c, da, [d for d in others if d not in split])
        if not split:
            out[var] = np.asarray(da.transpose(c.time_dim).values, dtype=float)
            continue
        d = split[0]
        for i, label in enumerate(_labels(c.ds, d)):
            col = da.isel({d: i}).transpose(c.time_dim)
            out[f"{var}[{label}]"] = np.asarray(col.values, dtype=float)
        if len(out) >= MAX_EXPLORE_VARS:
            break
    return dict(itertools.islice(out.items(), MAX_EXPLORE_VARS))


def _index_of(ds: xr.Dataset, dim: str, label: Any) -> int:
    labels = _labels(ds, dim)
    return labels.index(label)


def _downsample(
    coords: list[Any], values: np.ndarray
) -> tuple[list[Any], list[float | None], bool]:
    n = len(coords)
    if n <= MAX_EXPLORE_POINTS:
        return coords, [_fv(v) for v in values], False
    idx = np.linspace(0, n - 1, MAX_EXPLORE_POINTS).round().astype(int)
    return [coords[i] for i in idx], [_fv(values[i]) for i in idx], True


def _need_time(c: DataContext) -> None:
    if c.time_dim is None:
        raise NotApplicable("the data has no time dimension")


def _need_strong_time(c: DataContext) -> None:
    """The lag, stationarity and trend sections: real time with enough periods."""
    _need_time(c)
    assert c.time_dim is not None
    if not c.strong_time:
        raise NotApplicable(f"{c.time_dim} is an ordered sequence, not time")
    n = int(c.ds.sizes[c.time_dim])
    if n < MIN_TIME_PERIODS:
        raise InsufficientData(
            f"there are only {n} periods. This view needs {MIN_TIME_PERIODS} or more"
        )


# ------------------------------------------------------------------------ sections


_PERIOD_WORDS: Final = {
    "B": "business day", "D": "day", "W": "week", "M": "month", "MS": "month", "ME": "month",
    "Q": "quarter", "QS": "quarter", "QE": "quarter", "A": "year", "AS": "year", "Y": "year",
    "YS": "year", "YE": "year", "h": "hour", "H": "hour",
}  # fmt: skip
"""Plain words for the time dimension by its pandas frequency (anchors such as ``-SUN``
dropped); multiples (``2W``) and finer frequencies keep the dimension's name."""


def _dim_phrase(d: DimOverview) -> str:
    """``156 weeks`` for a weekly time dimension whatever it is called (a dimension named
    ``time`` read "156 time"); ``3 geo`` for the others."""
    word = _PERIOD_WORDS.get(d.freq.split("-", 1)[0]) if d.kind == "time" and d.freq else None
    if word is None:
        return f"{d.size} {d.name}"
    return f"{d.size} {word}{'' if d.size == 1 else 's'}"


def overview(c: DataContext) -> DataOverviewView:
    ds = c.ds
    dims: list[DimOverview] = []
    for d in list(ds.dims)[:32]:
        name = str(d)
        kind = "categorical"
        if c.schema is not None and name in c.schema.dims:
            kind = c.schema.dims[name].kind
        elif name in ds.coords and np.asarray(ds[name].values).dtype.kind == "M":
            kind = "time"
        labels = _labels(ds, name)
        dims.append(
            DimOverview(
                name=name[:256],
                kind=kind,
                size=int(ds.sizes[name]),
                start=labels[0] if labels and kind in ("time", "integer", "float") else None,
                end=labels[-1] if labels and kind in ("time", "integer", "float") else None,
                freq=c.freq if name == c.time_dim else None,
            )
        )
    var_rows: list[VarOverview] = []
    for v in list(ds.data_vars)[:MAX_EXPLORE_VARS]:
        name = str(v)
        da = ds[name]
        numeric = _numeric(da)
        arr = np.asarray(_stat_array(da).values)
        miss = 0.0
        if numeric and arr.size and arr.dtype.kind not in "iub":  # integers are never missing
            miss = float(np.mean(~np.isfinite(arr.astype(float))))
        distinct: int | None = None
        constant = False
        if numeric:
            fin = stats.finite(arr)
            if fin.size:
                distinct = int(np.unique(fin).size)
                constant = distinct == 1
        spec = c.schema.vars.get(name) if c.schema is not None else None
        var_rows.append(
            VarOverview(
                name=name[:256],
                dtype=str(da.dtype)[:64],
                dims=[str(x) for x in da.dims],
                role=spec.role.value if spec else None,
                units=(spec.units or None) if spec else None,
                missing_share=min(1.0, max(0.0, miss)),
                distinct=distinct,
                constant=constant,
                used_as=None if c.shapes.plain else c.shapes.var_use.get(name, "data"),
            )
        )
    issues = quality_warnings(c)
    serious = sum(1 for w in issues if w.severity != "info")
    infos = len(issues) - serious
    n_cells = int(sum(int(ds[v].size) for v in ds.data_vars))
    shape = " x ".join(_dim_phrase(d) for d in dims) or "no dimensions"
    period_txt = ""
    if c.time_dim is not None and dims:
        td = next((d for d in dims if d.name == c.time_dim), None)
        if td is not None and td.start is not None:
            period_txt = f", from {td.start} to {td.end}"
    shapes_txt = ""
    shape_views = None
    if not c.shapes.plain:
        from mapi_runner.worker.explore_science import shape_views as _shape_views

        shape_views = _shape_views(c.shapes)
        main = [s for s in c.shapes.shapes if s.kind not in ("timeseries", "sequence", "table")]
        if main:
            shapes_txt = f"It holds {_shapes_phrase(ds, main)}. "
    summary = (
        f"{len(var_rows)} variable{'' if len(var_rows) == 1 else 's'} over {shape}{period_txt}. "
        + shapes_txt
        + (
            f"The result to explain is {c.kpi!r}. "
            if c.kpi
            else "No variable has the role of the result (target). "
        )
        + (
            f"{serious} data problem(s) to look at."
            if serious
            else f"No data problems to correct. There are {infos} thing(s) to know."
            if infos
            else "No data problems found."
        )
    )
    return DataOverviewView(
        n_cells=n_cells,
        dims=dims,
        vars=var_rows,
        summary_plain=summary[:1000],
        quality_score=max(0.0, 1.0 - 0.1 * serious),
        caption_plain="What the dataset contains: its dimensions, variables and roles.",
        shapes=shape_views,
    )


_SHAPE_WORDS: Final = {
    "image": "image", "vector_field": "vector field", "areal_map": "map of areas",
    "points": "set of points", "network": "network", "tree": "tree",
    "circular": "set of angles or times of day", "profile": "profile",
}  # fmt: skip


def _shapes_phrase(ds: xr.Dataset, shapes: Sequence[Any]) -> str:
    """``2 images of 81 x 81 pixels and 1 of 31 x 31 pixels; a network``."""
    parts: list[str] = []
    for kind in dict.fromkeys(s.kind for s in shapes):
        group = [s for s in shapes if s.kind == kind]
        word = _SHAPE_WORDS.get(kind, kind)
        if kind in ("image", "vector_field"):
            sizes: dict[str, int] = {}
            for s in group:
                key = f"{ds.sizes[s.dims['y']]} x {ds.sizes[s.dims['x']]} pixels"
                sizes[key] = sizes.get(key, 0) + len(s.vars)
            items = list(sizes.items())
            total = sum(sizes.values())
            text = f"{items[0][1]} {word}{'s' if total != 1 else ''} of {items[0][0]}"
            text += "".join(f" and {n} of {k}" for k, n in items[1:])
            parts.append(text)
        else:
            parts.append(f"{len(group)} {word}s" if len(group) > 1 else f"a {word}")
    return "; ".join(parts)[:400]


def _distribution_columns(c: DataContext) -> dict[str, np.ndarray]:
    """Values per distribution: each variable as a whole, a table split per column."""
    tables = {s.vars[0]: s for s in c.shapes.of("table")}
    out: dict[str, np.ndarray] = {}
    for var in c.vars:
        shape = tables.get(var)
        if shape is None:
            out[var] = np.asarray(_stat_array(c.ds[var]).values)
            continue
        col = shape.dims["columns"]
        da = c.ds[var].transpose(col, ...)
        for i, label in enumerate(_labels(c.ds, col)):
            out[f"{var}[{label}]"] = np.asarray(da.values[i])
    return out


def distributions(c: DataContext) -> DistributionsView:
    from scipy import stats as sps

    items: list[VarDistribution] = []
    cols = _distribution_columns(c)
    for var in list(cols)[: c.max_series]:
        y = stats.finite(cols[var])
        if y.size == 0:
            items.append(VarDistribution(var=var, n=0))
            continue
        qs = np.quantile(y, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        lo, hi = float(y.min()), float(y.max())
        hist = None
        if hi > lo:
            counts, edges = np.histogram(y, bins=_HIST_BINS)
            hist = Histogram(edges=[float(e) for e in edges], counts=[int(k) for k in counts])
        items.append(
            VarDistribution(
                var=var,
                n=int(y.size),
                mean=_fv(y.mean()),
                sd=_fv(y.std(ddof=1)) if y.size > 1 else None,
                skew=_fv(sps.skew(y)) if y.size > 2 and hi > lo else None,
                kurtosis=_fv(sps.kurtosis(y)) if y.size > 3 and hi > lo else None,
                zero_share=float(np.mean(y == 0)),
                quantiles={
                    k: float(v)
                    for k, v in zip(
                        ("0.01", "0.05", "0.25", "0.5", "0.75", "0.95", "0.99"), qs, strict=True
                    )
                },
                histogram=hist,
            )
        )
    return DistributionsView(
        items=items,
        caption_plain="How the values of each variable are spread out.",
        warnings_plain=[_SAMPLED_NOTE] if any(_sampled(c, v) for v in c.vars) else [],
    )


def timeseries(c: DataContext) -> TimeSeriesView:
    _need_time(c)
    assert c.time_dim is not None
    coords = c.time_coords()
    series: list[SeriesView] = []
    omitted = 0
    for var in c.vars:
        for facet, values in iter_series(c, var):
            if len(series) >= c.max_series:
                omitted += 1
                continue
            cs, vs, down = _downsample(coords, values)
            series.append(SeriesView(var=var, facet=facet, coords=cs, values=vs, downsampled=down))
    return TimeSeriesView(
        dim=c.time_dim,
        facet_by=list(c.req.facet_by),
        series=series,
        omitted_series=omitted,
        caption_plain="Every variable over time"
        + (f", one panel per {', '.join(c.req.facet_by)}" if c.req.facet_by else "")
        + ".",
    )


def _longest_gap(values: np.ndarray) -> int:
    best = run = 0
    for ok in np.isfinite(values):
        run = 0 if ok else run + 1
        best = max(best, run)
    return best


def _missing_along(c: DataContext) -> str | None:
    """The dim of the missingness grid: time, else the frame dim of an image, else the
    unit dim (areas, species, observations)."""
    if c.time_dim is not None:
        return c.time_dim
    image = c.shapes.first("image", "vector_field")
    if image is not None and "frame" in image.dims:
        return image.dims["frame"]
    return c.shapes.unit_dim


def missingness_grid(c: DataContext) -> MissingnessGrid | None:
    along = _missing_along(c)
    if along is None:
        return None
    labels: list[str] = []
    rows: list[list[float]] = []
    coords = [str(x) for x in _labels(c.ds, along)]
    for var in c.vars:
        da = c.ds[var]
        if along not in da.dims:
            continue
        frac = np.isnan(np.asarray(da.transpose(along, ...).values, dtype=float))
        frac = frac.reshape(frac.shape[0], -1).mean(axis=1)
        labels.append(var)
        rows.append([float(f) for f in frac])
    if not labels:
        return None
    from mapi_spec.views import MAX_GRID_COORDS

    if len(coords) > MAX_GRID_COORDS:
        edges = np.linspace(0, len(coords), MAX_GRID_COORDS + 1).round().astype(int)
        coords = [coords[a] for a in edges[:-1]]
        rows = [
            [float(np.mean(r[a:b])) if b > a else 0.0 for a, b in itertools.pairwise(edges)]
            for r in rows
        ]
    return MissingnessGrid(along=along, labels=labels[:1024], coords=coords, fraction=rows[:1024])


def missingness(c: DataContext) -> MissingnessView:
    share: dict[str, float] = {}
    gaps: dict[str, int] = {}
    for var in c.vars:
        a = np.asarray(_stat_array(c.ds[var]).values, dtype=float)
        share[var] = float(np.mean(~np.isfinite(a))) if a.size else 0.0
        if c.time_dim is not None and c.time_dim in c.ds[var].dims:
            gaps[var] = max(
                (
                    _longest_gap(v)
                    for _f_, v in iter_series(
                        c, var, facet_dims=[str(d) for d in c.ds[var].dims if d != c.time_dim]
                    )
                ),
                default=0,
            )
    return MissingnessView(
        grid=missingness_grid(c),
        share_by_var=share,
        longest_gap=gaps,
        caption_plain="Shows where values are missing (a darker cell shows more missing values).",
        warnings_plain=[_SAMPLED_NOTE] if any(_sampled(c, v) for v in c.vars) else [],
    )


def outliers(c: DataContext) -> OutliersView:
    method = c.req.outliers
    thr = c.req.outlier_threshold
    items: list[VarOutliers] = []
    for var in c.vars:
        if var in c.curve_vars:
            continue
        da = _stat_array(c.ds[var])
        a = np.asarray(da.values, dtype=float)
        scores = stats.outlier_scores(a, method)
        flags = np.abs(scores) > thr
        flags &= np.isfinite(scores)
        count = int(flags.sum())
        n = int(np.isfinite(a).sum())
        points: list[OutlierPoint] = []
        if count:
            order = np.argsort(-np.abs(np.where(flags, scores, 0)).reshape(-1))[: min(count, 100)]
            dims = [str(d) for d in da.dims]
            for flat in order:
                idx = np.unravel_index(int(flat), a.shape) if a.ndim else ()
                coords = {
                    d: (jsonable(da[d].values[i]) if d in da.coords else _labels(c.ds, d)[i])
                    for d, i in zip(dims, idx, strict=True)
                }
                points.append(
                    OutlierPoint(
                        coords=coords,
                        value=float(a.reshape(-1)[flat]),
                        score=float(scores.reshape(-1)[flat]),
                    )
                )
        items.append(
            VarOutliers(var=var, count=count, share=count / n if n else 0.0, points=points)
        )
    return OutliersView(
        method=method,
        threshold=thr,
        items=items,
        caption_plain=f"Shows the values that are more than {thr:g} robust standard deviations "
        "from the typical value.",
        warnings_plain=[_SAMPLED_NOTE] if any(_sampled(c, v) for v in c.vars) else [],
    )


def _corr(a: np.ndarray, b: np.ndarray, method: str) -> tuple[float | None, float | None]:
    from scipy import stats as sps

    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3 or np.ptp(a[ok]) == 0 or np.ptp(b[ok]) == 0:
        return None, None
    res = sps.spearmanr(a[ok], b[ok]) if method == "spearman" else sps.pearsonr(a[ok], b[ok])
    return _fv(res.statistic), _fv(res.pvalue)


def data_columns(c: DataContext) -> dict[str, np.ndarray]:
    """Columns for correlation and VIF: along time (panel dims reduced, small categories
    split); without time the columns of a table (``level[raf]``), else the variables on the
    unit dim (areas, species), else the pixels of images that share a grid, else the
    variables that share the first variable's dims."""
    if c.time_dim is not None:
        return time_columns(c, c.vars)
    if not c.vars:
        return {}
    s = c.shapes
    if not s.plain:
        table = s.first("table")
        if table is not None and table.vars[0] in c.vars:
            var, col = table.vars[0], table.dims["columns"]
            da = c.ds[var].transpose(table.dims["item"], col)
            out = {
                f"{var}[{label}]": np.asarray(da.values[:, i], dtype=float)
                for i, label in enumerate(_labels(c.ds, col))
            }
            return dict(itertools.islice(out.items(), MAX_EXPLORE_VARS))
        unit = s.unit_dim
        if unit is not None:
            on_unit = {
                v: np.asarray(c.ds[v].values, dtype=float)
                for v in c.vars
                if c.ds[v].dims == (unit,)
            }
            if len(on_unit) >= 2:
                return on_unit
        for image in s.of("image"):
            same = [v for v in image.vars if v in c.vars]
            if len(same) >= 2:
                dims = c.ds[same[0]].dims
                return {
                    v: np.asarray(c.ds[v].transpose(*dims).values, dtype=float).reshape(-1)
                    for v in same
                    if set(c.ds[v].dims) == set(dims)
                }
    # variables that share the first variable's dims, flattened
    dims = c.ds[c.vars[0]].dims
    return {
        v: np.asarray(c.ds[v].transpose(*dims).values, dtype=float).reshape(-1)
        for v in c.vars
        if set(c.ds[v].dims) == set(dims)
    }


_flat_columns = data_columns


def correlation(c: DataContext) -> CorrelationView:
    cols = _flat_columns(c)
    names = list(cols)[:64]
    if len(names) < 2:
        raise NotApplicable("a correlation needs at least two numeric variables")
    m: list[list[float | None]] = [[None] * len(names) for _ in names]
    p: list[list[float | None]] = [[None] * len(names) for _ in names]
    for i, a in enumerate(names):
        m[i][i] = 1.0
        p[i][i] = 0.0
        for j in range(i + 1, len(names)):
            r, pv = _corr(cols[a], cols[names[j]], c.req.correlation)
            m[i][j] = m[j][i] = r
            p[i][j] = p[j][i] = pv
    return CorrelationView(
        method=c.req.correlation,
        vars=names,
        matrix=m,
        p_values=p,
        caption_plain="Shows how strongly the variables move together (1: together, -1: opposite).",
    )


def _facets(c: DataContext) -> list[dict[str, Any]]:
    if not c.req.facet_by:
        return [{}]
    levels = [_labels(c.ds, d) for d in c.req.facet_by]
    out = [dict(zip(c.req.facet_by, combo, strict=True)) for combo in itertools.product(*levels)]
    return out[: c.max_series]


def _profile(var: str, facet: dict[str, Any], prof: stats.Lagged) -> LagProfile:
    return LagProfile(
        var=var,
        facet=facet,
        lags=prof.lags,
        values=prof.values,
        bound=prof.bound,
        peak_lag=prof.peak_lag,
        significant_lags=prof.significant,
    )


def ccf(c: DataContext) -> CrossCorrelationView:
    _need_strong_time(c)
    if c.kpi is None:
        raise NeedsOption("no KPI. Give a variable the role target, or give kpi")
    items: list[LagProfile] = []
    drivers = c.drivers()
    if not drivers:
        raise NotApplicable("the data has no drivers to compare with the KPI")
    for facet in _facets(c):
        kpi_col = time_columns(c, [c.kpi], facet=facet)
        y = next(iter(kpi_col.values()), None)
        if y is None:
            continue
        for name, x in time_columns(c, drivers, facet=facet).items():
            if len(items) >= c.max_series:
                break
            xs, ys = (x, y)
            if c.req.prewhiten:
                xs, ys, _p = stats.prewhiten(x, y)
            items.append(_profile(name, facet, stats.ccf_profile(xs, ys, c.req.max_lag)))
    return CrossCorrelationView(
        kpi=c.kpi,
        max_lag=c.req.max_lag,
        prewhitened=c.req.prewhiten,
        items=items,
        caption_plain="Shows the correlation of each driver with the KPI some periods later. "
        "A peak at lag 2 shows that the effect of the driver occurs two periods after the driver.",
    )


def acf_pacf(c: DataContext) -> AcfPacfView:
    _need_strong_time(c)
    acfs: list[LagProfile] = []
    pacfs: list[LagProfile] = []
    suggested: dict[str, dict[str, int]] = {}
    targets = [c.kpi] if c.kpi else []
    for var in targets + [v for v in c.vars if v != c.kpi]:
        for facet, values in iter_series(c, var):
            if len(acfs) >= c.max_series:
                break
            y = stats.finite(values)
            a = stats.acf_profile(y, c.req.max_lag)
            p = stats.pacf_profile(y, c.req.max_lag)
            acfs.append(_profile(var, facet, a))
            pacfs.append(_profile(var, facet, p))
            key = var if not facet else f"{var}[{','.join(str(v) for v in facet.values())}]"
            suggested[key[:256]] = stats.suggest_arma_orders(a, p)
    return AcfPacfView(
        acf=acfs,
        pacf=pacfs,
        suggested=suggested,
        caption_plain="Shows how each value relates to its own past. Bars outside the dashed "
        "band are more than chance.",
    )


def stationarity(c: DataContext) -> StationarityView:
    _need_strong_time(c)
    rows: list[StationarityRow] = []
    for var in c.vars:
        for facet, values in iter_series(c, var):
            if len(rows) >= c.max_series:
                break
            y = stats.finite(values)
            adf = stats.adf_test(y, alpha=c.req.alpha, var=var)
            kp = stats.kpss_test(y, alpha=c.req.alpha, var=var)
            verdict, text = stats.stationarity_verdict(adf, kp)
            rows.append(
                StationarityRow(
                    var=var, facet=facet, adf=adf, kpss=kp, verdict=verdict, verdict_plain=text
                )
            )
    return StationarityView(
        alpha=c.req.alpha,
        rows=rows,
        caption_plain="Whether each series returns to a stable level (ADF and KPSS tests).",
    )


def stl(c: DataContext) -> StlView:
    _need_strong_time(c)
    if c.period is None:
        raise NeedsOption("the season length is unknown. Give period")
    coords = c.time_coords()
    items: list[StlComponents] = []
    skipped = 0
    for var in c.vars:
        for facet, values in iter_series(c, var):
            if len(items) >= c.max_series:
                break
            parts = stats.stl_decompose(values, c.period)
            if parts is None:
                skipped += 1
                continue
            fs, ft = stats.season_strengths(parts)
            cs, obs, _d = _downsample(coords, values)
            _c, tr, _d = _downsample(coords, parts["trend"])
            _c, se, _d = _downsample(coords, parts["seasonal"])
            _c, re, _d = _downsample(coords, parts["resid"])
            items.append(
                StlComponents(
                    var=var,
                    facet=facet,
                    period=c.period,
                    coords=cs,
                    observed=obs,
                    trend=tr,
                    seasonal=se,
                    resid=re,
                    seasonal_strength=fs,
                    trend_strength=ft,
                )
            )
    if not items:
        raise InsufficientData(
            f"each series has less than two seasons ({2 * c.period} periods) of complete data"
        )
    warn = (
        [f"The view does not show {skipped} series (gaps or fewer than two seasons)."]
        if skipped
        else []
    )
    return StlView(
        items=items,
        warnings_plain=warn,
        caption_plain=f"Shows each series as a trend, a repeating {c.period}-period season "
        "and the remainder.",
    )


def vif(c: DataContext, *, limit: float = 10.0) -> VifView:
    drivers = c.drivers()
    if c.time_dim is not None:
        cols = time_columns(c, drivers)
    else:
        tshape = c.shapes.first("table")
        cols = {
            k: v
            for k, v in data_columns(c).items()
            if k in drivers or (tshape is not None and k.startswith(f"{tshape.vars[0]}["))
        }
    if len(cols) < 2:
        raise NotApplicable("collinearity needs at least two drivers")
    table = stats.vif_table(cols)
    flagged = [k for k, v in table.items() if v is not None and v > limit]
    return VifView(
        vif=table,
        limit=limit,
        flagged=flagged,
        condition_number=stats.condition_number(cols),
        caption_plain="Shows how much each driver overlaps with the other drivers. Above 10, "
        "it is difficult to separate their effects.",
    )


def _group_comparison(
    c: DataContext, var: str, labels: Sequence[Any], parts: Sequence[np.ndarray]
) -> GroupComparison:
    """One variable across groups (``parts[i]`` = the values of group ``labels[i]``)."""
    from scipy import stats as sps

    total = None
    every = np.concatenate([np.asarray(x, dtype=float).ravel() for x in parts]) if parts else None
    if c.role(var) in _ADDITIVE and every is not None:
        total = float(np.nansum(every))
    rows: list[GroupStats] = []
    samples: list[np.ndarray] = []
    for i, label in enumerate(labels[:1000]):
        y = stats.finite(parts[i])
        samples.append(y)
        share = None
        if total and total > 0 and every is not None and np.all(stats.finite(every) >= 0):
            share = float(min(1.0, max(0.0, float(y.sum()) / total)))
        rows.append(
            GroupStats(
                group=label if label is not None else str(i),
                n=int(y.size),
                mean=_fv(y.mean()) if y.size else None,
                sd=_fv(y.std(ddof=1)) if y.size > 1 else None,
                median=_fv(np.median(y)) if y.size else None,
                share_of_total=share,
            )
        )
    usable = [s for s in samples if s.size >= 2]
    test = p = None
    if len(usable) >= 2 and any(np.ptp(s) > 0 for s in usable):
        normalish = all(s.size >= 8 for s in usable)
        if normalish:
            test, p = "anova", _fv(sps.f_oneway(*usable).pvalue)
        else:
            test, p = "kruskal", _fv(sps.kruskal(*usable).pvalue)
    return GroupComparison(
        var=var,
        groups=rows,
        test=test,
        p_value=min(1.0, max(0.0, p)) if p is not None else None,
        differs=None if p is None else p < c.req.alpha,
    )


def _groups_by_var(c: DataContext, gvar: str) -> GroupComparisonView:
    """Groups given by a grouping variable (an index var, or an integer var with few
    values) on the same dims as the data."""
    g = c.ds[gvar]
    codes = np.asarray(g.values)
    levels = np.unique(codes[np.isfinite(codes.astype(float))]) if codes.size else codes
    labelled = c.shapes.group_vars.get(gvar) or (
        c.schema.vars[gvar].index_of if c.schema is not None and gvar in c.schema.vars else None
    )
    names = _labels(c.ds, labelled) if labelled and labelled in c.ds.dims else None
    labels: list[Any] = []
    for code in levels[:1000]:
        k = int(code)
        labels.append(names[k] if names is not None and 0 <= k < len(names) else jsonable(code))
    items: list[GroupComparison] = []
    for var in c.vars:
        da = c.ds[var]
        if da.dims != g.dims or var == gvar:
            continue
        vals = np.asarray(da.values, dtype=float)
        parts = [vals[codes == code] for code in levels[:1000]]
        items.append(_group_comparison(c, var, labels, parts))
    if not items:
        raise NotApplicable(f"no variable has the dimensions of {gvar}")
    by = labelled or gvar
    return GroupComparisonView(
        by=by[:128],
        items=items,
        caption_plain=f"Each variable compared across the values of {by} (from {gvar}).",
    )


def groups(c: DataContext) -> GroupComparisonView:
    by = c.req.group_by
    if by is not None and by in c.ds.data_vars:
        return _groups_by_var(c, by)
    if by is None:
        s = c.shapes
        if s.plain:
            cats = [
                str(d) for d in c.ds.dims if str(d) != c.time_dim and 2 <= c.ds.sizes[d] <= 1000
            ]
        else:
            cats = list(s.categorical_dims)
            if not cats and s.group_vars:
                return _groups_by_var(c, next(iter(s.group_vars)))
        if not cats:
            raise NotApplicable("the data has no dimension with groups to compare")
        by = cats[0]
    if by not in c.ds.dims:
        raise NotApplicable(f"the data has no dimension called {by!r}")
    labels = _labels(c.ds, by)
    items: list[GroupComparison] = []
    for var in c.vars:
        da = c.ds[var]
        if by not in da.dims:
            continue
        arr = np.asarray(da.transpose(by, ...).values, dtype=float).reshape(len(labels), -1)
        items.append(_group_comparison(c, var, labels, list(arr)))
    if not items:
        raise NotApplicable(f"no variable varies over {by}")
    return GroupComparisonView(
        by=by,
        items=items,
        caption_plain=f"Each variable compared across {by} levels.",
    )


def _w(
    code: str, severity: str, message: str, action: str | None = None, *, var: str | None = None
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


def quality_warnings(
    c: DataContext, *, max_missing: float = 0.05, min_history: int | None = None
) -> list[QualityWarning]:
    """Plain-language data-quality warnings (also the ``data.quality`` governance check)."""
    out: list[QualityWarning] = []
    ds = c.ds
    if c.time_dim is not None and c.time_dim in ds.coords:
        t = np.asarray(ds[c.time_dim].values)
        idx = pd.Index(t)
        if idx.has_duplicates:
            out.append(
                _w(
                    "DUPLICATE_PERIODS",
                    "error",
                    f"Some {c.time_dim} labels appear more than once.",
                    "Remove or combine the duplicated periods before you fit a model.",
                )
            )
        if t.dtype.kind == "M" and t.size >= 3 and c.freq is None:
            out.append(
                _w(
                    "IRREGULAR_PERIODS",
                    "warning",
                    f"The {c.time_dim} labels are not evenly spaced.",
                    "Resample to a regular frequency (for example with core.resample).",
                )
            )
        n = int(t.size)
        need = min_history or (2 * c.period if c.period else 24)
        if n < need and c.strong_time:
            out.append(
                _w(
                    "SHORT_HISTORY",
                    "warning",
                    f"There are only {n} periods of history. We recommend {need} periods or more.",
                    "Add more history, or use a simpler model with fewer effects.",
                )
            )
    for var in c.vars:
        a = np.asarray(_stat_array(ds[var]).values, dtype=float)
        if a.size == 0:
            continue
        miss = float(np.mean(~np.isfinite(a)))
        if miss > max_missing:
            out.append(
                _w(
                    "MISSING_VALUES",
                    "warning",
                    f"{miss:.0%} of {var!r} is missing.",
                    "Fill the gaps (core.impute), or examine the upload mapping.",
                    var=var,
                )
            )
        elif miss > 0:
            out.append(
                _w(
                    "MISSING_VALUES",
                    "info",
                    f"{miss:.1%} of {var!r} is missing.",
                    "Small gaps are usually not a problem. If the model needs complete data, "
                    "impute them.",
                    var=var,
                )
            )
        fin = a[np.isfinite(a)]
        role = c.role(var)
        if fin.size and np.ptp(fin) == 0 and role is not Role.CONSTANT:
            out.append(
                _w(
                    "CONSTANT_VAR",
                    "warning",
                    f"{var!r} never changes.",
                    "Remove it. A variable that never changes cannot explain the result.",
                    var=var,
                )
            )
        if role in (Role.SPEND, Role.MEDIA) and fin.size and float(fin.min()) < 0:
            out.append(
                _w(
                    "NEGATIVE_SPEND",
                    "error",
                    f"{var!r} has negative values, which spend and media cannot have.",
                    "Correct the source data. Put refunds in a separate variable.",
                    var=var,
                )
            )
        spec = c.schema.vars.get(var) if c.schema is not None else None
        if spec is not None and spec.bounds is not None and fin.size:
            lo, hi = spec.bounds
            if (lo is not None and float(fin.min()) < lo) or (
                hi is not None and float(fin.max()) > hi
            ):
                out.append(
                    _w(
                        "OUT_OF_BOUNDS",
                        "error",
                        f"{var!r} has values outside its declared range {lo}..{hi}.",
                        "Correct the values or the declared bounds.",
                        var=var,
                    )
                )
        if role is Role.TARGET and fin.size and float(np.mean(fin == 0)) > 0.5:
            out.append(
                _w(
                    "MOSTLY_ZERO",
                    "info",
                    f"More than half of {var!r} is zero.",
                    "Think about a model for counts or for intermittent demand.",
                    var=var,
                )
            )
        if fin.size >= 10 and var not in c.curve_vars:
            s = stats.outlier_scores(fin, "mad")
            share = float(np.mean(np.abs(s[np.isfinite(s)]) > 5.0)) if np.isfinite(s).any() else 0.0
            if share > 0.01:
                out.append(
                    _w(
                        "OUTLIERS",
                        "info",
                        f"{share:.1%} of {var!r} are extreme values.",
                        "Find if they are errors or real events. For real events, add a control.",
                        var=var,
                    )
                )
        if c.time_dim is not None and c.time_dim in ds[var].dims:
            gap = max(
                (
                    _longest_gap(v)
                    for _f_, v in iter_series(
                        c, var, facet_dims=[str(d) for d in ds[var].dims if d != c.time_dim]
                    )
                ),
                default=0,
            )
            if gap > 4:
                out.append(
                    _w(
                        "LONG_GAP",
                        "warning",
                        f"{var!r} has a gap of {gap} periods in a row.",
                        "Examine the source. You cannot fill long gaps reliably.",
                        var=var,
                    )
                )
    drivers = c.drivers()
    if c.time_dim is not None and len(drivers) >= 2:
        cols = time_columns(c, drivers)
        if 2 <= len(cols) <= 60:
            table = stats.vif_table(cols)
            high = sorted(k for k, v in table.items() if v is not None and v > 10)
            if high:
                out.append(
                    _w(
                        "COLLINEAR_DRIVERS",
                        "warning",
                        f"These drivers move almost together: {', '.join(high[:6])}.",
                        "Combine them, remove one, or use priors to separate them.",
                    )
                )
    if not c.shapes.plain:
        from mapi_runner.worker.explore_science import structure_warnings

        out.extend(structure_warnings(c))
    return out[:500]


def quality(c: DataContext) -> QualityView:
    rank = {"error": 0, "warning": 1, "info": 2}
    items = sorted(quality_warnings(c), key=lambda w: rank[w.severity])
    return QualityView(
        items=items,
        caption_plain="Shows the data problems to look at before you fit a model. The most "
        "serious problems are first.",
    )


DATA_SECTION_FUNCS: Final[dict[str, Callable[[DataContext], BaseModel]]] = {
    "overview": overview,
    "distributions": distributions,
    "timeseries": timeseries,
    "missingness": missingness,
    "outliers": outliers,
    "correlation": correlation,
    "ccf": ccf,
    "acf_pacf": acf_pacf,
    "stationarity": stationarity,
    "stl": stl,
    "vif": vif,
    "groups": groups,
    "quality": quality,
    **SCIENCE_SECTION_FUNCS,
}
"""Every Data Explorer section's implementation (keys = :data:`mapi_spec.views.DATA_SECTIONS`)."""
