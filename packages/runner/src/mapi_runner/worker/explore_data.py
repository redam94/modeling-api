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

``CHEAP_SECTIONS`` are written by every ingest run (``ui/explore/``); the others run on
demand in an ``explore`` run. Columns are built at the time level: panel dims (geo, store)
are summed for additive roles (target, media, spend) and averaged otherwise, and a small
categorical dim (up to :data:`MAX_SPLIT_LEVELS` levels, e.g. ``channel``) splits a variable
into one column per level (``spend[tv]``).
"""

from __future__ import annotations

import datetime
import itertools
import math
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

__all__ = [
    "CHEAP_SECTIONS",
    "DATA_SECTION_FUNCS",
    "MAX_SPLIT_LEVELS",
    "DataContext",
    "SectionSkipped",
    "data_context",
    "explore_period",
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
"""Sections every ingest run writes (seconds even for large panels)."""
MAX_SPLIT_LEVELS: Final = 12
"""A categorical dim with at most this many levels splits a variable into columns."""
_ADDITIVE: Final = frozenset({Role.TARGET, Role.MEDIA, Role.SPEND, Role.EXPOSURE, Role.WEIGHT})
_DRIVERS: Final = frozenset({Role.MEDIA, Role.SPEND, Role.COVARIATE, Role.CONTROL, Role.EXPOSURE})
_HIST_BINS: Final = 30


class SectionSkipped(Exception):
    """A section that does not apply to this data (the message is shown to the user)."""


def jsonable(v: Any) -> str | int | float | bool | None:
    """A JSON scalar for a coordinate label or value (dates as ISO text, NaN as ``None``)."""
    if isinstance(v, np.datetime64 | pd.Timestamp | datetime.datetime | datetime.date):
        ts = pd.Timestamp(v)
        if pd.isna(ts):
            return None
        text: str = ts.date().isoformat() if ts == ts.normalize() else ts.isoformat()
        return text
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, bool | int | str) or v is None:
        return v[:256] if isinstance(v, str) else v
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")[:256]
    return str(v)[:256]


def _fv(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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
            if name in ds.dims and d.policy.value == "extend":
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
    time_dim = _find_time_dim(ds, schema, req.time_dim)
    roles: dict[str, Role] = {}
    if schema is not None:
        roles = {n: v.role for n, v in schema.vars.items()}
    numeric = [str(n) for n in ds.data_vars if _numeric(ds[n])]
    wanted = list(req.vars) or numeric
    missing = [v for v in wanted if v not in ds.data_vars]
    if missing:
        raise SectionSkipped(f"the data has no variable called {missing[0]!r}")
    chosen = [v for v in wanted if v in numeric and roles.get(v) not in (Role.INDEX, Role.MASK)][
        :MAX_EXPLORE_VARS
    ]
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
    return DataContext(ds, schema, req, time_dim, freq, period, chosen, roles, kpi)


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
        raise SectionSkipped("the data has no time dimension")


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
        arr = np.asarray(da.values)
        miss = float(np.mean(~np.isfinite(arr.astype(float)))) if numeric and arr.size else 0.0
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
            )
        )
    issues = quality_warnings(c)
    serious = sum(1 for w in issues if w.severity != "info")
    infos = len(issues) - serious
    n_cells = int(sum(int(np.asarray(ds[v].values).size) for v in ds.data_vars))
    shape = " x ".join(_dim_phrase(d) for d in dims) or "no dimensions"
    period_txt = ""
    if c.time_dim is not None and dims:
        td = next((d for d in dims if d.name == c.time_dim), None)
        if td is not None and td.start is not None:
            period_txt = f", from {td.start} to {td.end}"
    summary = (
        f"{len(var_rows)} variables over {shape}{period_txt}. "
        + (
            f"The result to explain is {c.kpi!r}. "
            if c.kpi
            else "No variable is marked as the result (target). "
        )
        + (
            f"{serious} data problem(s) to look at."
            if serious
            else f"No data problems to fix; {infos} thing(s) to know."
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
    )


def distributions(c: DataContext) -> DistributionsView:
    from scipy import stats as sps

    items: list[VarDistribution] = []
    for var in c.vars[: c.max_series]:
        y = stats.finite(c.ds[var].values)
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
        items=items, caption_plain="How the values of each variable are spread out."
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


def missingness_grid(c: DataContext) -> MissingnessGrid | None:
    if c.time_dim is None:
        return None
    labels: list[str] = []
    rows: list[list[float]] = []
    coords = [str(x) for x in c.time_coords()]
    for var in c.vars:
        da = c.ds[var]
        if c.time_dim not in da.dims:
            continue
        frac = np.isnan(np.asarray(da.transpose(c.time_dim, ...).values, dtype=float))
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
    return MissingnessGrid(
        along=c.time_dim, labels=labels[:1024], coords=coords, fraction=rows[:1024]
    )


def missingness(c: DataContext) -> MissingnessView:
    share: dict[str, float] = {}
    gaps: dict[str, int] = {}
    for var in c.vars:
        a = np.asarray(c.ds[var].values, dtype=float)
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
        caption_plain="Where values are missing (darker = more missing).",
    )


def outliers(c: DataContext) -> OutliersView:
    method = c.req.outliers
    thr = c.req.outlier_threshold
    items: list[VarOutliers] = []
    for var in c.vars:
        da = c.ds[var]
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
                coords = {d: _labels(c.ds, d)[i] for d, i in zip(dims, idx, strict=True)}
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
        caption_plain=f"Values more than {thr:g} robust standard deviations from the typical "
        "value.",
    )


def _corr(a: np.ndarray, b: np.ndarray, method: str) -> tuple[float | None, float | None]:
    from scipy import stats as sps

    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3 or np.ptp(a[ok]) == 0 or np.ptp(b[ok]) == 0:
        return None, None
    res = sps.spearmanr(a[ok], b[ok]) if method == "spearman" else sps.pearsonr(a[ok], b[ok])
    return _fv(res.statistic), _fv(res.pvalue)


def _flat_columns(c: DataContext) -> dict[str, np.ndarray]:
    if c.time_dim is not None:
        return time_columns(c, c.vars)
    # no time: variables that share the first variable's dims, flattened
    if not c.vars:
        return {}
    dims = c.ds[c.vars[0]].dims
    return {
        v: np.asarray(c.ds[v].transpose(*dims).values, dtype=float).reshape(-1)
        for v in c.vars
        if set(c.ds[v].dims) == set(dims)
    }


def correlation(c: DataContext) -> CorrelationView:
    cols = _flat_columns(c)
    names = list(cols)[:64]
    if len(names) < 2:
        raise SectionSkipped("a correlation needs at least two numeric variables")
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
        caption_plain="How strongly variables move together (1: together, -1: opposite).",
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
    _need_time(c)
    if c.kpi is None:
        raise SectionSkipped("no KPI: mark a variable as the target or pass kpi")
    items: list[LagProfile] = []
    drivers = c.drivers()
    if not drivers:
        raise SectionSkipped("the data has no drivers to compare with the KPI")
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
        caption_plain="Correlation of each driver with the KPI some periods later (a peak "
        "at lag 2 means the driver's effect shows up two periods after it happens).",
    )


def acf_pacf(c: DataContext) -> AcfPacfView:
    _need_time(c)
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
        caption_plain="How each value relates to its own past; bars beyond the dashed band "
        "are more than chance.",
    )


def stationarity(c: DataContext) -> StationarityView:
    _need_time(c)
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
    _need_time(c)
    if c.period is None:
        raise SectionSkipped("the season length is unknown: pass period")
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
        raise SectionSkipped(
            f"less than two seasons ({2 * c.period} periods) of complete data in every series"
        )
    warn = [f"{skipped} series were skipped (gaps or fewer than two seasons)."] if skipped else []
    return StlView(
        items=items,
        warnings_plain=warn,
        caption_plain=f"Each series split into trend, a repeating {c.period}-period season "
        "and the rest.",
    )


def vif(c: DataContext, *, limit: float = 10.0) -> VifView:
    drivers = c.drivers()
    cols = (
        time_columns(c, drivers)
        if c.time_dim is not None
        else {k: v for k, v in _flat_columns(c).items() if k in drivers}
    )
    if len(cols) < 2:
        raise SectionSkipped("collinearity needs at least two drivers")
    table = stats.vif_table(cols)
    flagged = [k for k, v in table.items() if v is not None and v > limit]
    return VifView(
        vif=table,
        limit=limit,
        flagged=flagged,
        condition_number=stats.condition_number(cols),
        caption_plain="How much each driver overlaps with the others (above 10: their "
        "separate effects are hard to tell apart).",
    )


def groups(c: DataContext) -> GroupComparisonView:
    from scipy import stats as sps

    by = c.req.group_by
    if by is None:
        cats = [str(d) for d in c.ds.dims if str(d) != c.time_dim and 2 <= c.ds.sizes[d] <= 1000]
        if not cats:
            raise SectionSkipped("the data has no dimension with groups to compare")
        by = cats[0]
    if by not in c.ds.dims:
        raise SectionSkipped(f"the data has no dimension called {by!r}")
    labels = _labels(c.ds, by)
    items: list[GroupComparison] = []
    for var in c.vars:
        da = c.ds[var]
        if by not in da.dims:
            continue
        arr = np.asarray(da.transpose(by, ...).values, dtype=float).reshape(len(labels), -1)
        total = float(np.nansum(arr)) if c.role(var) in _ADDITIVE else None
        rows: list[GroupStats] = []
        samples: list[np.ndarray] = []
        for i, label in enumerate(labels[:1000]):
            y = stats.finite(arr[i])
            samples.append(y)
            share = None
            if total and total > 0 and np.all(stats.finite(arr) >= 0):
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
        items.append(
            GroupComparison(
                var=var,
                groups=rows,
                test=test,
                p_value=min(1.0, max(0.0, p)) if p is not None else None,
                differs=None if p is None else p < c.req.alpha,
            )
        )
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
                    "Remove or combine the duplicated periods before modelling.",
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
        if n < need:
            out.append(
                _w(
                    "SHORT_HISTORY",
                    "warning",
                    f"Only {n} periods of history (at least {need} are recommended).",
                    "Add more history or use a simpler model with fewer effects.",
                )
            )
    for var in c.vars:
        a = np.asarray(ds[var].values, dtype=float)
        if a.size == 0:
            continue
        miss = float(np.mean(~np.isfinite(a)))
        if miss > max_missing:
            out.append(
                _w(
                    "MISSING_VALUES",
                    "warning",
                    f"{miss:.0%} of {var!r} is missing.",
                    "Fill gaps (core.impute) or check the upload mapping.",
                    var=var,
                )
            )
        elif miss > 0:
            out.append(
                _w(
                    "MISSING_VALUES",
                    "info",
                    f"{miss:.1%} of {var!r} is missing.",
                    "Small gaps are usually fine; impute them if the model needs complete data.",
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
                    "Drop it: a variable that never changes cannot explain anything.",
                    var=var,
                )
            )
        if role in (Role.SPEND, Role.MEDIA) and fin.size and float(fin.min()) < 0:
            out.append(
                _w(
                    "NEGATIVE_SPEND",
                    "error",
                    f"{var!r} has negative values, which spend and media cannot have.",
                    "Correct the source data (refunds belong in a separate variable).",
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
                    "Consider a model for counts or intermittent demand.",
                    var=var,
                )
            )
        if fin.size >= 10:
            s = stats.outlier_scores(fin, "mad")
            share = float(np.mean(np.abs(s[np.isfinite(s)]) > 5.0)) if np.isfinite(s).any() else 0.0
            if share > 0.01:
                out.append(
                    _w(
                        "OUTLIERS",
                        "info",
                        f"{share:.1%} of {var!r} are extreme values.",
                        "Check whether they are errors or real events (then add a "
                        "control for them).",
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
                        "Check the source; long gaps cannot be filled reliably.",
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
                        "Combine them, drop one, or use priors to separate them.",
                    )
                )
    return out[:500]


def quality(c: DataContext) -> QualityView:
    rank = {"error": 0, "warning": 1, "info": 2}
    items = sorted(quality_warnings(c), key=lambda w: rank[w.severity])
    return QualityView(
        items=items,
        caption_plain="Data problems to look at before modelling, most serious first.",
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
}
"""Every Data Explorer section's implementation (keys = :data:`mapi_spec.views.DATA_SECTIONS`)."""
