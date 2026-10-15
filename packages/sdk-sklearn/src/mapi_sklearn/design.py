"""From a labelled ``xr.Dataset`` to a design matrix and back.

``rows`` dims are stacked into samples with :func:`mapi_sdk.wire.stack_rows`; the kept
rows (complete targets, finite features, ``mask`` true, real rather than padded labels)
are recorded in the SDK row mask, so :meth:`Design.unstack` restores every prediction to
its labelled cell and leaves dropped rows missing.

Feature columns come from two sources, fixed at fit time in a JSON :class:`Layout`:

* **variables**: one column per variable; dims beyond ``rows`` are flattened into one
  column per fitted label (``spend[tv]``, ``promo_lag[2]``); lower-rank variables are
  broadcast over the missing row dims;
* **coordinate features** (``coord_features``): a row dim's labels encoded as
  ``onehot`` columns, integer codes (``category``: declared natively to xgboost or
  histogram boosting, one-hot for every other estimator; ``ordinal``: plain numeric codes)
  or ``numeric`` values (numbers, or days since the first fitted label for datetimes).

Levels unseen at fit get no one-hot column and a missing code; :mod:`mapi_sklearn.levels`
decides what they become (pooled or zero).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_sdk.wire import stack_rows, unstack_rows
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mapi_sklearn.estimators import EstimatorEntry
from mapi_sklearn.settings import SklearnSettings

__all__ = [
    "MAX_COLUMNS",
    "ROW",
    "ColumnDecl",
    "Design",
    "DimEncoding",
    "Layout",
    "build_design",
    "plan_layout",
]

ROW = "__row__"
KEEP = "__keep__"
MAX_COLUMNS = 4096
MAX_HIST_CATEGORIES = 254
_DAY_NS = 86_400 * 10**9


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnDecl(_Frozen):
    """One feature column. ``source`` says how it is computed from a dataset."""

    name: str
    source: Literal["var", "onehot", "code", "numeric"]
    var: str | None = None
    sel: dict[str, JsonValue] = Field(default_factory=dict)
    dim: str | None = None
    level: JsonValue = None


class DimEncoding(_Frozen):
    """How a row dim's labels become features (``encoding`` is the effective one)."""

    dim: str
    requested: Literal["category", "onehot", "ordinal", "numeric"]
    encoding: Literal["category", "onehot", "ordinal", "numeric"]
    levels: list[JsonValue] = Field(default_factory=list)
    counts: list[int] = Field(default_factory=list)
    origin_ns: int | None = None

    @property
    def leveled(self) -> bool:
        """Encodings that only know the fitted levels (new levels need a strategy)."""
        return self.encoding != "numeric"


class Layout(_Frozen):
    """The fitted feature layout, stored as JSON in the bundle."""

    target: str
    rows: list[str]
    columns: list[ColumnDecl]
    encodings: dict[str, DimEncoding] = Field(default_factory=dict)
    extra_levels: dict[str, list[JsonValue]] = Field(default_factory=dict)
    weight: str | None = None
    mask: str | None = None

    @property
    def feature_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def categorical(self) -> list[int]:
        """Indices of natively categorical (integer-coded) columns."""
        return [
            j
            for j, c in enumerate(self.columns)
            if c.source == "code" and self.encodings[str(c.dim)].encoding == "category"
        ]

    def columns_of(self, dim: str) -> list[int]:
        return [j for j, c in enumerate(self.columns) if c.dim == dim and c.source != "var"]

    def groups(self) -> list[tuple[str, list[int]]]:
        """Contribution groups: each variable column alone, coordinate features per dim."""
        out: list[tuple[str, list[int]]] = []
        seen: dict[str, int] = {}
        for j, c in enumerate(self.columns):
            if c.source == "var":
                out.append((c.name, [j]))
                continue
            key = str(c.dim)
            if key not in seen:
                seen[key] = len(out)
                out.append((key, []))
            out[seen[key]][1].append(j)
        return out


def _json_label(v: Any) -> JsonValue:
    item = v.item() if hasattr(v, "item") else v
    if isinstance(item, bytes):
        return item.decode()
    if isinstance(item, (str, int, float, bool)) or item is None:
        return item
    raise MapiError(
        f"coordinate label {item!r} of type {type(item).__name__} cannot be used as a feature",
        code=ErrorCode.SCHEMA_INVALID,
    )


def _labels(ds: xr.Dataset, dim: str) -> np.ndarray:
    return np.asarray(ds[dim].values)


def _settings_error(message: str, pointer: str = "/model/settings") -> MapiError:
    return MapiError(message, code=ErrorCode.SETTINGS_INVALID, pointer=pointer)


def _is_numeric(da: xr.DataArray) -> bool:
    return da.dtype.kind in "fiub"


def _candidate_vars(
    ds: xr.Dataset, s: SklearnSettings, stage_schema: DataSchema | None
) -> tuple[list[str], bool]:
    """Feature variable names and whether they were listed explicitly."""
    reserved = {s.target, s.weight, s.mask} - {None}
    excluded = set(s.columns.exclude) | reserved
    if s.columns.vars is not None:
        missing = [v for v in s.columns.vars if v not in ds.data_vars]
        if missing:
            raise MapiError(
                f"feature variables {missing} are not in the model's data",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings/columns/vars",
            )
        return [v for v in s.columns.vars if v not in excluded], True
    if stage_schema is not None:
        roles = set(s.columns.roles)
        names = [n for n, v in stage_schema.vars.items() if v.role in roles and n in ds.data_vars]
    else:
        names = [str(v) for v in ds.data_vars]
    return [n for n in names if n not in excluded], False


def plan_layout(
    ds: xr.Dataset,
    s: SklearnSettings,
    *,
    entry: EstimatorEntry,
    stage_schema: DataSchema | None = None,
    log: Callable[[str], None] | None = None,
) -> Layout:
    """Decide the feature columns for ``ds`` (the model-stage training data)."""
    rows = list(s.rows)
    missing_rows = [d for d in rows if d not in ds.dims]
    if missing_rows:
        raise MapiError(
            f"row dims {missing_rows} are not dims of the model's data",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/model/settings/rows",
        )
    if s.target not in ds:
        raise MapiError(
            f"the target {s.target!r} is not in the model's data", code=ErrorCode.SCHEMA_INVALID
        )
    names, explicit = _candidate_vars(ds, s, stage_schema)
    columns: list[ColumnDecl] = []
    extra_levels: dict[str, list[JsonValue]] = {}
    for name in names:
        da = ds[name]
        if not _is_numeric(da):
            if explicit:
                raise _settings_error(
                    f"feature variable {name!r} has dtype {da.dtype}; only numeric and bool "
                    "variables can be columns",
                    "/model/settings/columns/vars",
                )
            if log is not None:
                log(f"skipping non-numeric variable {name!r}")
            continue
        dims = [str(d) for d in da.dims]
        if not set(dims) & set(rows):
            if explicit:
                raise _settings_error(
                    f"feature variable {name!r} has none of the row dims {rows}",
                    "/model/settings/columns/vars",
                )
            if log is not None:
                log(f"skipping {name!r}: it does not vary along any row dim")
            continue
        extra = [d for d in dims if d not in rows]
        if not extra:
            columns.append(ColumnDecl(name=name, source="var", var=name))
            continue
        grids = []
        for d in extra:
            labels = [_json_label(v) for v in _labels(ds, d)]
            extra_levels.setdefault(d, labels)
            grids.append(labels)
        for combo in _product(grids):
            sel = dict(zip(extra, combo, strict=True))
            label = ",".join(str(v) for v in combo)
            columns.append(ColumnDecl(name=f"{name}[{label}]", source="var", var=name, sel=sel))
    encodings: dict[str, DimEncoding] = {}
    for dim, requested in s.coord_features.items():
        labels_arr = _labels(ds, dim)
        if requested == "numeric":
            origin = None
            if labels_arr.dtype.kind == "M":
                origin = int(labels_arr.astype("datetime64[ns]").astype(np.int64).min())
            elif labels_arr.dtype.kind not in "iuf":
                raise _settings_error(
                    f"coord feature {dim!r}: 'numeric' needs numeric or datetime labels",
                    f"/model/settings/coord_features/{dim}",
                )
            encodings[dim] = DimEncoding(
                dim=dim, requested=requested, encoding="numeric", origin_ns=origin
            )
            columns.append(ColumnDecl(name=dim, source="numeric", dim=dim))
            continue
        if labels_arr.dtype.kind == "M":
            raise _settings_error(
                f"coord feature {dim!r}: datetime labels can only be encoded as 'numeric'",
                f"/model/settings/coord_features/{dim}",
            )
        levels = [_json_label(v) for v in labels_arr]
        encoding: Literal["category", "onehot", "ordinal"]
        if requested == "category":
            native = entry.native_categorical
            too_many = native == "sklearn_hist" and len(levels) > MAX_HIST_CATEGORIES
            encoding = "category" if native is not None and not too_many else "onehot"
            if too_many and log is not None:
                log(f"{dim!r} has {len(levels)} levels; histogram boosting one-hot encodes it")
        else:
            encoding = requested
        encodings[dim] = DimEncoding(dim=dim, requested=requested, encoding=encoding, levels=levels)
        if encoding == "onehot":
            columns += [
                ColumnDecl(name=f"{dim}={lvl}", source="onehot", dim=dim, level=lvl)
                for lvl in levels
            ]
        else:
            columns.append(ColumnDecl(name=dim, source="code", dim=dim))
    if not columns:
        raise _settings_error(
            "no feature columns: list columns.vars, widen columns.roles or add coord_features"
        )
    if len(columns) > MAX_COLUMNS:
        raise _settings_error(
            f"the design has {len(columns)} columns; at most {MAX_COLUMNS} are allowed"
        )
    names_seen = [c.name for c in columns]
    if len(set(names_seen)) != len(names_seen):
        raise _settings_error("feature column names collide; rename a variable or a dim")
    return Layout(
        target=s.target,
        rows=rows,
        columns=columns,
        encodings=encodings,
        extra_levels=extra_levels,
        weight=s.weight,
        mask=s.mask,
    )


def _product(grids: Sequence[Sequence[JsonValue]]) -> list[tuple[JsonValue, ...]]:
    out: list[tuple[JsonValue, ...]] = [()]
    for g in grids:
        out = [(*prefix, v) for prefix in out for v in g]
    if len(out) > MAX_COLUMNS:
        raise _settings_error(f"flattening extra dims gives more than {MAX_COLUMNS} columns")
    return out


@dataclass(slots=True)
class Design:
    """A stacked design matrix for the kept rows of a dataset."""

    X: np.ndarray
    frame: xr.Dataset
    y: np.ndarray | None = None
    w: np.ndarray | None = None
    new_rows: dict[str, np.ndarray] = field(default_factory=dict)
    row_labels: dict[str, np.ndarray] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    n_cells: int = 0

    @property
    def n(self) -> int:
        return int(self.X.shape[0])

    def unstack(
        self, outputs: Mapping[str, tuple[Sequence[str], Mapping[str, Any], np.ndarray]]
    ) -> xr.Dataset:
        """Labelled outputs from ``{name: (lead_dims, lead_coords, values[..., n])}``.

        Each output gets dims ``(*lead_dims, *rows)``; rows not kept are missing.
        """
        data: dict[str, Any] = {}
        coords: dict[str, Any] = {}
        for name, (lead, lead_coords, values) in outputs.items():
            arr = np.asarray(values, dtype=float)
            if arr.shape[-1] != self.n:
                raise ValueError(f"{name}: expected {self.n} rows, got {arr.shape[-1]}")
            data[name] = ((*lead, ROW), arr)
            coords.update(lead_coords)
        stacked = self.frame.assign(data).assign_coords(coords)
        return unstack_rows(stacked, ROW)


def _template(ds: xr.Dataset, rows: Sequence[str]) -> xr.DataArray:
    return xr.DataArray(
        np.zeros([ds.sizes[d] for d in rows], dtype=bool),
        dims=list(rows),
        coords={d: ds[d].values for d in rows},
    )


def _flat(da: xr.DataArray, tmpl: xr.DataArray) -> np.ndarray:
    out = da.broadcast_like(tmpl).transpose(*tmpl.dims).values
    return np.asarray(out).reshape(-1)


def _var_column(ds: xr.Dataset, col: ColumnDecl, layout: Layout, tmpl: xr.DataArray) -> np.ndarray:
    var = str(col.var)
    if var not in ds:
        raise MapiError(
            f"feature variable {var!r} is missing from the score data; direct forecasting "
            "needs every feature over the horizon",
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            untrusted_text=var,
        )
    da = ds[var]
    if col.sel:
        for d, lab in col.sel.items():
            if d not in da.dims or lab not in set(_labels(ds, d).tolist()):
                raise MapiError(
                    f"feature column {col.name!r} is missing from the score data",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    untrusted_text=col.name,
                )
        da = da.sel(col.sel)
    stray = [str(d) for d in da.dims if d not in layout.rows]
    if stray:
        raise MapiError(
            f"variable {var!r} has dims {stray} that were not flattened at fit",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return _flat(da.astype(float), tmpl)


def _codes(ds: xr.Dataset, enc: DimEncoding) -> np.ndarray:
    lookup = {lvl: float(i) for i, lvl in enumerate(enc.levels)}
    return np.array(
        [lookup.get(_json_label(v), math.nan) for v in _labels(ds, enc.dim)], dtype=float
    )


def _coord_column(
    ds: xr.Dataset, col: ColumnDecl, enc: DimEncoding, tmpl: xr.DataArray
) -> np.ndarray:
    dim = enc.dim
    labels = _labels(ds, dim)
    if col.source == "numeric":
        if labels.dtype.kind == "M":
            ns = labels.astype("datetime64[ns]").astype(np.int64)
            vals = (ns - int(enc.origin_ns or 0)) / _DAY_NS
        else:
            vals = labels.astype(float)
    elif col.source == "onehot":
        vals = np.array([_json_label(v) == col.level for v in labels], dtype=float)
    else:
        vals = _codes(ds, enc)
    return _flat(xr.DataArray(vals, dims=[dim], coords={dim: labels}), tmpl)


def _check_extra_levels(ds: xr.Dataset, layout: Layout) -> None:
    for d, fitted in layout.extra_levels.items():
        if d not in ds.dims:
            continue
        known = set(fitted)
        unseen = [_json_label(v) for v in _labels(ds, d) if _json_label(v) not in known]
        if unseen:
            raise MapiError(
                f"{len(unseen)} label(s) of {d!r} have no fitted feature column",
                code=ErrorCode.UNSEEN_LEVELS,
                untrusted_text=", ".join(str(v) for v in unseen[:10]),
            )


def build_design(
    ds: xr.Dataset,
    layout: Layout,
    *,
    phase: Literal["fit", "score"],
    allow_nan: bool,
) -> Design:
    """Stack ``ds`` into a design matrix following ``layout``.

    At fit, rows with a missing target, a missing or non-positive weight, ``mask`` false or
    (unless the estimator handles missing values) a missing feature are dropped. At score,
    padded labels (``<dim>__requested`` false) and rows with missing features are not
    predicted; new levels are flagged in :attr:`Design.new_rows`.
    """
    missing = [d for d in layout.rows if d not in ds.dims]
    if missing:
        raise MapiError(
            f"the data has no {missing} dim(s); the model stacks rows over {layout.rows}",
            code=ErrorCode.SCHEMA_INVALID,
        )
    if phase == "score":
        _check_extra_levels(ds, layout)
    tmpl = _template(ds, layout.rows)
    n_cells = int(tmpl.size)
    cols = np.empty((n_cells, len(layout.columns)), dtype=float)
    leveled = np.zeros(len(layout.columns), dtype=bool)
    for j, col in enumerate(layout.columns):
        if col.source == "var":
            cols[:, j] = _var_column(ds, col, layout, tmpl)
        else:
            enc = layout.encodings[str(col.dim)]
            cols[:, j] = _coord_column(ds, col, enc, tmpl)
            leveled[j] = enc.leveled
    keep = np.ones(n_cells, dtype=bool)
    dropped: dict[str, int] = {}

    def drop(reason: str, bad: np.ndarray) -> None:
        nonlocal keep
        bad = bad & keep
        if bad.any():
            dropped[reason] = int(bad.sum())
            keep = keep & ~bad

    if layout.mask is not None:
        if layout.mask not in ds:
            raise MapiError(
                f"mask variable {layout.mask!r} is missing", code=ErrorCode.SCHEMA_INVALID
            )
        mask = _flat(ds[layout.mask].fillna(False).astype(bool), tmpl)
        drop("masked", ~mask)
    if phase == "score":
        for d in layout.rows:
            flag = f"{d}__requested"
            if flag in ds.coords:
                drop("padding", ~_flat(ds[flag].astype(bool), tmpl))
    y = w = None
    y_all: np.ndarray | None = None
    w_all: np.ndarray | None = None
    if phase == "fit":
        y_all = _flat(ds[layout.target].astype(float), tmpl)
        drop("missing_target", ~np.isfinite(y_all))
        if layout.weight is not None:
            if layout.weight not in ds:
                raise MapiError(
                    f"weight variable {layout.weight!r} is missing", code=ErrorCode.SCHEMA_INVALID
                )
            w_all = _flat(ds[layout.weight].astype(float), tmpl)
            drop("bad_weight", ~(np.isfinite(w_all) & (w_all > 0)))
    plain = cols[:, ~leveled]
    if not allow_nan:
        drop("missing_feature", ~np.isfinite(plain).all(axis=1))
    else:
        drop("infinite_feature", np.isinf(plain).any(axis=1))
    frame = stack_rows(
        xr.Dataset(
            {KEEP: xr.DataArray(keep.reshape(tmpl.shape), dims=tmpl.dims, coords=tmpl.coords)}
        ),
        layout.rows,
        ROW,
        drop="none",
        mask_var=KEEP,
    )
    X = cols[keep]
    if y_all is not None:
        y = y_all[keep]
    if w_all is not None:
        w = w_all[keep]
    new_rows: dict[str, np.ndarray] = {}
    for dim, enc in layout.encodings.items():
        if not enc.leveled:
            continue
        known = set(enc.levels)
        flags = np.array([_json_label(v) not in known for v in _labels(ds, dim)], dtype=bool)
        if flags.any():
            if phase == "fit":  # pragma: no cover - fit levels come from the same data
                raise AssertionError("new levels at fit")
            da = xr.DataArray(flags, dims=[dim], coords={dim: _labels(ds, dim)})
            new_rows[dim] = _flat(da, tmpl)[keep]
    row_labels = {d: np.asarray(frame[d].values) for d in layout.rows if d in frame.coords}
    return Design(
        X=X,
        frame=frame,
        y=y,
        w=w,
        new_rows=new_rows,
        row_labels=row_labels,
        dropped=dropped,
        n_cells=n_cells,
    )
