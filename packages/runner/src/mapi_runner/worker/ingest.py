"""``ingest``: an uploaded file becomes a validated, content-addressed dataset version.

Inputs: ``upload`` (CSV - optionally gzip-compressed -, Parquet, ``*.zarr.zip`` or netCDF).
Outputs::

    data.zarr.zip   the dataset (zarr v3 zip; root attr ``mapi_schema`` holds the schema)
    schema.json     its DataSchema
    profile.json    sha256 + size of data.zarr.zip, coordinate summary, per-variable stats,
                    plain-language validation issues
    preview.json    the first rows as a tidy table
    columns.json    one ColumnProfile per column of the uploaded table (tabular uploads;
                    MFF: per pivoted VariableName) - what Guided role detection reads

Tabular uploads map columns to dims and variables (:class:`~mapi_spec.ingest.IngestMapping`,
Meridian ``CoordToColumns`` style), with **wide** variables gathered from one column per
label of a new dim, or use a preset:

* ``mff`` - the long ``Period`` / key columns / ``VariableName`` / ``VariableValue`` table
  is pivoted to one column per variable; key columns with several values become dims, and
  ``MffOptions.combine`` joins key columns into one dim. An explicit mapping then picks and
  renames variables (and may gather them into wide variables) exactly as for a CSV;
* ``meridian`` - Google Meridian's column contract on a wide CSV/Parquet table.

M4: an upload with groups (NetCDF-4/HDF5, a zarr zip with nested groups), a raw HDF5 file
whose mapping places variables below the root, or an :class:`~mapi_spec.ingest.IngestAssembly`
of several uploads (inputs ``upload.<i>``) becomes a **tree** version
(:mod:`mapi_runner.worker.tree_ingest`)::

    data.tree.zip     the xr.DataTree (root attr ``mapi_tree_schema`` holds its TreeSchema)
    tree_schema.json  its TreeSchema
    schema.json       the root node's DataSchema (possibly empty), for readers before M4
    summary.json      the root's DataSummary plus ``nodes``: one DataSummary per node
    profile.json / preview.json   of the default node (``node`` names it)
    ui/explore/...    the default node's Data Explorer views; every node's under
                      ``ui/explore/nodes/<slug>/`` and ``ui/explore/nodes/index.json``

Every HDF5 file is checked by :func:`~mapi_runner.worker.hdf5_guard.check_hdf5` before
anything else opens it.

v0.14: an ``image_folder`` upload (a zip of images and a manifest table) becomes one plain
dataset ``pixels(image, y, x, channel)`` uint8 (:mod:`~mapi_runner.worker.image_ingest`); it
must be the only source of a version.

Every byte of user data is parsed here, in the sandbox, under the caps of
:class:`~mapi_runner.worker.tabular.IngestLimits`.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import math
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.errors import PayloadLimitError, WireFormatError
from mapi_sdk.schema import data_summary, infer_freq, infer_schema, validate_dataset
from mapi_sdk.tree import node_dataset, validate_tree, write_tree_zip
from mapi_sdk.wire import WireLimits, read_zarr_zip, write_zarr_zip
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.guided import ColumnProfile, safe_name
from mapi_spec.ingest import (
    COMBINE_SEPARATOR,
    MFF_COLUMNS,
    MFF_PANEL_KEY_COLUMNS,
    MFF_TOTAL_LABELS,
    DimMapping,
    ImageFolderOptions,
    IngestMapping,
    IngestPreset,
    VarMapping,
    WideVarMapping,
)
from mapi_spec.jobs import DATATREE_OUTPUT, TREE_SCHEMA_OUTPUT, upload_input
from mapi_spec.run import INPUT_DATASET, RunnerPhase
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role
from mapi_spec.tree import ROOT, TreeSchema
from mapi_spec.views import (
    MAX_GRID_COORDS,
    MAX_PREVIEW_ROWS,
    MissingnessGrid,
    PreviewColumn,
    PreviewView,
    ProfileView,
    VarProfile,
)

from mapi_runner.jobs import INGEST_DATA, INGEST_SCHEMA, INPUT_UPLOAD, IngestJob, MffOptions
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import SCHEMA_ATTR
from mapi_runner.worker.hdf5_guard import check_hdf5, walk_hdf5
from mapi_runner.worker.hdf5_read import read_hdf5_mapped
from mapi_runner.worker.image_ingest import IMAGE_ATTR, read_image_folder
from mapi_runner.worker.tabular import (
    IngestLimits,
    check_zip_archive,
    column_profiles,
    problem,
    read_as_hdf5,
    read_table,
    sniff,
    write_profiles,
)
from mapi_runner.worker.tree_ingest import (
    TREE_SCHEMA_ATTR,
    Upload,
    assemble,
    check_tree_limits,
    explore_slug,
    map_tree,
    prefix_error,
    read_netcdf_tree,
    read_zarr_tree,
    schema_for_nodes,
    tree_from_nodes,
    zarr_has_groups,
)

__all__ = [
    "EXPLORE_NODES_INDEX",
    "INGEST_COLUMNS",
    "INGEST_NOTES_ATTR",
    "build_upload",
    "dataset_from_meridian",
    "dataset_from_mff",
    "dataset_from_table",
    "mff_table",
    "run_ingest",
]

INGEST_COLUMNS = "columns.json"
EXPLORE_NODES_INDEX = "ui/explore/nodes/index.json"
"""M4: the node -> explorer views index of a tree version."""
INGEST_NOTES_ATTR = "mapi.ingest_notes"
"""``Dataset.attrs`` key of the notes a build leaves for the ingest run (``[{pointer,
message}]``): reported as ``info`` issues, never stored with the data."""
_OFFSET = r"(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(?:Z|UTC|[+-]\d{2}:?\d{2})$"
"""A time of day followed by a UTC offset (``...T00:00:00+01:00``, ``...Z``)."""
Notes = list[dict[str, str]]
_COUNT_CHUNK = 4_000_000
_UNLIMITED = IngestLimits()


def _schema_error(message: str, pointer: str = "", **kw: Any) -> MapiError:
    action = kw.pop("action", None)
    untrusted = kw.pop("untrusted_text", None)
    return problem(
        ErrorCode.SCHEMA_INVALID, message, pointer=pointer, action=action, untrusted=untrusted
    )


def _missing_column(column: str, what: str, pointer: str, columns: list[str]) -> MapiError:
    close = difflib.get_close_matches(column, columns, n=3, cutoff=0.6)
    hint = (
        f"Did you mean {', '.join(repr(c[:64]) for c in close)}?"
        if close
        else ("Make sure that the spelling is correct, or select one of the columns of the upload.")
    )
    return _schema_error(
        f"column {column[:64]!r} ({what}) is not in the upload", pointer, action=hint
    )


# ======================================================================== tabular readers


def _read_table(
    path: Path, fmt: str, *, max_rows: int, limits: IngestLimits | None = None
) -> pd.DataFrame:
    """Parse a CSV or Parquet upload (kept for callers of v0.2; see :func:`read_table`)."""
    lim = limits or IngestLimits(max_rows=max_rows)
    if lim.max_rows != max_rows:
        lim = dataclasses.replace(lim, max_rows=max_rows)
    return read_table(path, fmt, lim)


def _sniff(path: Path) -> str:
    kind = sniff(path)
    return kind if kind in ("parquet", "zarr_zip", "netcdf") else "csv"


def _empty_labels(series: pd.Series, n: int, pointer: str) -> MapiError:
    return _schema_error(
        f"dim column {str(series.name)[:64]!r} has empty labels ({n} rows)",
        pointer,
        action="Fill in the label in every row, or remove rows without one.",
    )


def _local_times(series: pd.Series) -> tuple[pd.Series, bool]:
    """Dates and times, with any UTC offset dropped (``True`` when one was): the local
    date-time each value names, so midnight labels stay on their day and a series whose offset
    changes with daylight saving time stays regular."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        warnings.simplefilter("ignore", FutureWarning)
        try:
            out = pd.to_datetime(series, errors="raise")
        except (ValueError, TypeError):
            if not pd.api.types.is_string_dtype(series) and series.dtype != object:
                raise
            text = series.astype("string").str.strip()
            stripped = text.str.replace(_OFFSET, r"\1", regex=True)
            if stripped.equals(text):
                raise
            return pd.to_datetime(stripped, errors="raise"), True
    if isinstance(out.dtype, pd.DatetimeTZDtype):
        return out.dt.tz_localize(None), True
    if out.dtype == object:  # mixed offsets parsed to Timestamp objects
        return pd.Series(
            [None if pd.isna(v) else pd.Timestamp(v).tz_localize(None) for v in out],
            index=out.index,
            dtype="datetime64[ns]",
        ), True
    return out, False


def _coerce_dim(
    series: pd.Series, spec: DimSpec, pointer: str, notes: Notes | None = None
) -> pd.Series:
    try:
        if spec.kind == "time":
            out, dropped = _local_times(series)
            if out.isna().any():
                raise _empty_labels(series, int(out.isna().sum()), pointer)
            if dropped and notes is not None:
                notes.append(
                    {
                        "pointer": pointer,
                        "message": f"the dates in column {str(series.name)[:64]!r} have UTC "
                        "offsets or a time zone. The server read them as local dates and "
                        "times and removed the offsets",
                    }
                )
            return out
        if spec.kind in ("integer", "index", "float"):
            num = pd.to_numeric(series, errors="raise")
            if num.isna().any():
                raise _empty_labels(series, int(num.isna().sum()), pointer)
            if spec.kind == "float":
                return num.astype("float64")
            if not np.all(np.isclose(num % 1, 0)):
                raise ValueError("not whole numbers")
            return num.astype("int64")
    except (ValueError, TypeError, OverflowError) as exc:
        hint = {
            "time": "Use one date format for the whole column, such as 2026-01-31.",
            "integer": "Use whole numbers (1, 2, 3) in this column.",
            "index": "Use whole numbers (1, 2, 3) in this column.",
            "float": "Use numbers in this column.",
        }[spec.kind]
        raise _schema_error(
            f"the server cannot read column {str(series.name)[:64]!r} as a {spec.kind} dim",
            pointer,
            action=hint,
            untrusted_text=str(exc)[:2000],
        ) from None
    blank = series.isna() | (series.astype(str).str.strip() == "")
    if blank.any():
        raise _empty_labels(series, int(blank.sum()), pointer)
    return series.astype(str).str.strip()


def _coerce_var(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64") if series.isna().any() else series
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == series.notna().sum() and series.notna().any():
        return numeric.astype("float64")
    return series.fillna("").astype(str)


def _coerce_wide(series: pd.Series, var: str, label: str, pointer: str) -> pd.Series:
    """A cell column of a wide variable: numbers (or booleans) only."""
    if pd.api.types.is_bool_dtype(series):
        return series.astype("float64")
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64")
    text = series.astype("string").str.strip().str.lower()
    if text.dropna().isin(["true", "false"]).all() and text.notna().any():
        return text.map({"true": 1.0, "false": 0.0}).astype("float64")
    numeric = pd.to_numeric(series, errors="coerce")
    bad = numeric.isna() & series.notna() & (series.astype(str).str.strip() != "")
    if bad.any():
        sample = ", ".join(repr(str(v)[:40]) for v in series[bad].head(3))
        raise _schema_error(
            f"column {str(series.name)[:64]!r} ({var!r} for {label!r}) has {int(bad.sum())} "
            "cells that are not numbers",
            pointer,
            action="Use plain numbers without currency signs or thousands separators.",
            untrusted_text=sample,
        )
    return numeric.astype("float64")


def _labels(values: Any) -> np.ndarray:
    """Categorical coordinate labels as a fixed-width unicode array (pandas 3 string
    arrays and numpy ``StringDType`` do not cast to it directly)."""
    return np.array([str(v) for v in np.asarray(values, dtype=object).ravel()], dtype=str)


def _text_vars(ds: xr.Dataset) -> xr.Dataset:
    """Text variables as fixed-width strings; cells a dense cube had to invent become ''."""
    for name in list(ds.data_vars):
        if ds[name].dtype == object:
            values = np.asarray(ds[name].values, dtype=object)
            text = np.where([isinstance(v, str) for v in values.ravel()], values.ravel(), "")
            ds[name] = (ds[name].dims, text.astype(str).reshape(values.shape))
    return ds


def _check_columns(df: pd.DataFrame, mapping: IngestMapping) -> None:
    columns = [str(c) for c in df.columns]
    have = set(columns)
    for name, dm in mapping.dims.items():
        if dm.column not in have:
            raise _missing_column(
                dm.column, f"dim {name!r}", f"/mapping/dims/{name}/column", columns
            )
    for name, vm in mapping.vars.items():
        if vm.column not in have:
            raise _missing_column(
                vm.column, f"variable {name!r}", f"/mapping/vars/{name}/column", columns
            )
    for name, w in mapping.wide.items():
        for label, col in w.columns.items():
            if col not in have:
                raise _missing_column(
                    col,
                    f"{name!r} for {label!r}",
                    f"/mapping/wide/{name}/columns/{label}",
                    columns,
                )


def _wide_labels(mapping: IngestMapping) -> dict[str, list[str]]:
    """Labels per wide dim (variables sharing a dim must agree; their order is the first's)."""
    labels: dict[str, list[str]] = {}
    owner: dict[str, str] = {}
    for name, w in mapping.wide.items():
        if w.dim in mapping.dims or w.dim in mapping.vars or w.dim in mapping.wide:
            raise _schema_error(
                f"wide variable {name!r} creates dim {w.dim!r}, which is already a dim or variable",
                f"/mapping/wide/{name}/dim",
                action="Give the new dim a different name.",
            )
        mine = list(w.columns)
        if w.dim in labels:
            if set(labels[w.dim]) != set(mine):
                diff = sorted(set(labels[w.dim]) ^ set(mine))[:5]
                raise _schema_error(
                    f"wide variables {owner[w.dim]!r} and {name!r} label {w.dim!r} "
                    f"differently ({diff})",
                    f"/mapping/wide/{name}/columns",
                    action="For each variable along the same dim, map one column for each label.",
                )
        else:
            labels[w.dim] = mine
            owner[w.dim] = name
    return labels


def _check_cube(frame: pd.DataFrame, dims: list[str], limits: IngestLimits, pointer: str) -> None:
    """Refuse (``PAYLOAD_TOO_LARGE``) a pivot whose dense cube - every combination of the
    dim labels, times the variables - exceeds the limits, before it is allocated: a small
    table whose keys rarely repeat would otherwise ask for n^2 (or n^k) cells."""
    sizes = [int(frame[d].nunique(dropna=False)) for d in dims]
    combos = math.prod(sizes)
    n_vars = max(1, len(frame.columns) - len(dims))
    cells = combos * n_vars
    nbytes = cells * 8
    if cells <= limits.max_cells and nbytes <= limits.max_uncompressed_bytes:
        return
    shape = " x ".join(f"{n:,} {d}" for d, n in zip(dims, sizes, strict=True))
    raise problem(
        ErrorCode.PAYLOAD_TOO_LARGE,
        f"the mapped dims make {shape} = {combos:,} combinations. Thus the dataset would "
        f"hold {cells:,} values (~{nbytes / 1024**3:.1f} GiB) for {len(frame):,} rows. The "
        f"limit is {min(limits.max_cells, limits.max_uncompressed_bytes // 8):,} values",
        pointer=pointer,
        action="Most combinations have no row. Map fewer columns as dims (keep the other "
        "columns as variables), use a long layout with one id dim, or split the upload.",
    )


def dataset_from_table(
    df: pd.DataFrame, mapping: IngestMapping, *, limits: IngestLimits = _UNLIMITED
) -> tuple[xr.Dataset, DataSchema]:
    """Build a dataset + schema from a parsed table and a column mapping (``dims``,
    ``vars`` and ``wide`` variables: one column per label of a new dim). A dense cube
    larger than ``limits`` allows is refused before it is built."""
    _check_columns(df, mapping)
    if mapping.layout == "long" and len(mapping.dims) != 1:
        raise _schema_error(
            "a long-layout upload maps exactly one column to the observation dim",
            "/mapping/dims",
            action="Map only the row number (or an id column) as the dim of a long table.",
        )
    if mapping.layout == "long" and mapping.wide:
        raise _schema_error(
            "wide variables need a dense layout", "/mapping/wide",
            action="Set layout to dense, or map each column as its own variable.",
        )  # fmt: skip
    wide_labels = _wide_labels(mapping)
    frame = pd.DataFrame(index=df.index)
    notes: Notes = []
    for name, dm in mapping.dims.items():
        frame[name] = _coerce_dim(df[dm.column], dm.spec, f"/mapping/dims/{name}", notes)
    for name, vm in mapping.vars.items():
        frame[name] = _coerce_var(df[vm.column])
    tmp: dict[str, list[str]] = {}
    for name, w in mapping.wide.items():
        cols: list[str] = []
        for i, label in enumerate(wide_labels[w.dim]):
            key = f"__wide__{name}__{i}"
            frame[key] = _coerce_wide(
                df[w.columns[label]], name, label, f"/mapping/wide/{name}/columns/{label}"
            )
            cols.append(key)
        tmp[name] = cols
    dims = list(mapping.dims)
    dup = frame.duplicated(subset=dims)
    if dup.any():
        first = {k: str(v)[:40] for k, v in frame.loc[dup, dims].iloc[0].to_dict().items()}
        unmapped = [
            str(c)
            for c in df.columns
            if c not in {dm.column for dm in mapping.dims.values()}
            and not pd.api.types.is_numeric_dtype(df[c])
            and 1 < df[c].nunique() <= 1000
        ][:3]
        hint = (
            f"Columns such as {', '.join(repr(c[:40]) for c in unmapped)} change in one "
            "combination. Map them as dims too, or aggregate the rows first."
            if unmapped
            else "Remove duplicate rows, or map another column as a dim."
        )
        raise _schema_error(
            f"{int(dup.sum())} rows repeat a ({', '.join(dims)}) combination",
            "/mapping/dims",
            action=hint,
            untrusted_text=str(first),
        )
    _check_cube(frame, dims, limits, "/mapping/dims")
    ds = _text_vars(frame.set_index(dims).sort_index().to_xarray())
    if notes:
        ds.attrs[INGEST_NOTES_ATTR] = notes
    for name, cols in tmp.items():
        w = mapping.wide[name]
        stacked = xr.concat(
            [ds[c] for c in cols], dim=pd.Index(wide_labels[w.dim], name=w.dim)
        ).transpose(*dims, w.dim)
        ds = ds.drop_vars(cols).assign({name: stacked})
    for name in dims:  # categorical labels as plain str coords
        if mapping.dims[name].spec.kind not in ("time", "integer", "index", "float"):
            ds = ds.assign_coords({name: _labels(ds[name].values)})
    for dim in wide_labels:
        ds = ds.assign_coords({dim: _labels(ds[dim].values)})
    roles = {n: vm.role for n, vm in mapping.vars.items()} | {
        n: w.role for n, w in mapping.wide.items()
    }
    units = {n: vm.units for n, vm in mapping.vars.items() if vm.units} | {
        n: w.units for n, w in mapping.wide.items() if w.units
    }
    draft = infer_schema(ds, roles=roles, units=units)
    dim_specs: dict[str, DimSpec] = {}
    for name, dm in mapping.dims.items():
        spec = dm.spec
        if spec.kind == "time" and spec.freq is None:
            freq = infer_freq(ds[name].values)
            if freq is not None:
                spec = spec.model_copy(update={"freq": freq})
        dim_specs[name] = spec
    for w in mapping.wide.values():
        dim_specs.setdefault(w.dim, w.dim_spec)
    var_specs = {
        name: draft.vars[name].model_copy(
            update={"missing": vm.missing, "description": vm.description}
        )
        for name, vm in mapping.vars.items()
    } | {
        name: draft.vars[name].model_copy(
            update={"missing": w.missing, "description": w.description}
        )
        for name, w in mapping.wide.items()
    }
    schema = DataSchema(layout=mapping.layout, dims=dim_specs, vars=var_specs)
    return ds, schema


# ======================================================================================= MFF


def _mff_error(message: str, pointer: str = "/mapping/mff", **kw: Any) -> MapiError:
    kw.setdefault(
        "action",
        f"An MFF table has the columns {', '.join(MFF_COLUMNS)}. It has one row for each "
        "period, key combination and variable.",
    )
    return _schema_error(message, pointer, **kw)


def mff_table(
    df: pd.DataFrame,
    opts: MffOptions,
    limits: IngestLimits = _UNLIMITED,
    *,
    notes: Notes | None = None,
) -> tuple[pd.DataFrame, dict[str, str], list[str]]:
    """Pivot an MFF long table to one column per variable.

    Returns ``(table, dims, variables)``: the wide table (``Period``, the key columns that
    vary, combined dims, then one column per ``VariableName``), the dims it is indexed by
    (``column -> dim name``) and the variable columns.
    """
    for col in (opts.period_column, opts.variable_column, opts.value_column):
        if col not in df.columns:
            raise _mff_error(f"MFF upload has no {col!r} column")
    names = df[opts.variable_column].astype(str).str.strip()
    variables = sorted(set(names) - {""})
    if (names == "").any():
        raise _mff_error(f"{int((names == '').sum())} MFF rows have no {opts.variable_column}")
    if len(variables) > limits.max_columns:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the MFF table has {len(variables):,} variables. The maximum is "
            f"{limits.max_columns:,}",
            pointer="/mapping/mff",
            action="Remove the variables that the model does not need.",
        )
    values = pd.to_numeric(df[opts.value_column], errors="coerce")
    bad = values.isna() & df[opts.value_column].notna()
    if bad.any():
        raise _mff_error(
            f"{int(bad.sum())} MFF values are not numbers",
            f"/mapping/mff/{opts.value_column}",
            action="Use plain numbers in VariableValue (no currency signs or separators).",
            untrusted_text=", ".join(
                repr(str(v)[:40]) for v in df.loc[bad, opts.value_column].head(3)
            ),
        )
    period = _coerce_dim(
        df[opts.period_column],
        DimSpec(kind="time"),
        f"/mapping/mff/{opts.period_column}",
        notes,
    )
    table = pd.DataFrame({opts.period_column: period, "__var__": names, "__value__": values})
    combined: set[str] = set()
    index: dict[str, str] = {opts.period_column: opts.time_dim}
    for dim, cols in opts.combine.items():
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise _mff_error(f"MFF upload has no {missing[0]!r} column to combine into {dim!r}")
        parts = [df[c].astype("string").fillna("").str.strip() for c in cols]
        label = parts[0]
        for p in parts[1:]:
            label = label + COMBINE_SEPARATOR + p
        table[dim] = label.astype(str)
        index[dim] = dim
        combined |= set(cols)
    # every key column, for the duplicate check on the full key (as mmm-framework's)
    full_key = [opts.period_column, *index.keys() - {opts.period_column}]
    spread: list[tuple[str, list[str]]] = []
    for col, dim in opts.key_columns.items():
        if col in combined or col not in df.columns:
            continue
        labels = df[col].astype("string").fillna("").str.strip().astype(str)
        table[col] = labels
        full_key.append(col)
        if col not in MFF_PANEL_KEY_COLUMNS and col not in opts.split:
            continue  # a breakdown (Campaign, Outlet, Creative): summed below
        levels = sorted({v for v in labels.unique() if v.lower() not in MFF_TOTAL_LABELS})
        if len(levels) > 1 or (col in opts.split and levels):
            index[col] = dim
            spread.append((col, levels))
    clash = sorted(set(variables) & (set(index) | set(index.values())))
    if clash:
        raise _mff_error(
            f"the variable {clash[0][:64]!r} has the name of an MFF key column or dim",
            action="Rename the variable in VariableName.",
        )
    keys = [*full_key, "__var__"]
    dup = table.duplicated(subset=keys)
    if dup.any():
        first = {k: str(v)[:40] for k, v in table.loc[dup, keys].iloc[0].to_dict().items()}
        raise _mff_error(
            f"{int(dup.sum())} MFF rows repeat a key and variable",
            action="Each (Period, key columns, VariableName) combination can occur only one "
            "time. Aggregate the duplicates first.",
            untrusted_text=str(first),
        )
    for col, levels in spread:
        table = _spread_totals(table, col, levels, opts, notes)
    # breakdowns not kept as dims are summed up to the modelled dims (a cell given only
    # as missing values stays missing)
    table = table.groupby([*index, "__var__"], as_index=False, sort=False)["__value__"].sum(
        min_count=1
    )
    wide = table.pivot_table(
        index=list(index), columns="__var__", values="__value__", aggfunc="first", dropna=False
    )
    wide.columns = [str(c) for c in wide.columns]
    out = wide.reset_index()
    return out, index, variables


def _spread_totals(
    table: pd.DataFrame,
    col: str,
    levels: list[str],
    opts: MffOptions,
    notes: Notes | None,
) -> pd.DataFrame:
    """Rows of ``col``'s coarser level (a blank, ``National`` or ``Total`` label) spread
    across ``levels``: a variable given only in total (national TV) gets each level's share
    - equal, or the KPI's share of the level (``opts.allocation``), as mmm-framework's
    ``MFFLoader`` disaggregates national media. A variable may not mix the two levels."""
    total = table[col].str.lower().isin(MFF_TOTAL_LABELS)
    if not total.any():
        return table
    mixed = sorted(set(table.loc[total, "__var__"]) & set(table.loc[~total, "__var__"]))
    if mixed:
        raise _mff_error(
            f"the variable {mixed[0][:64]!r} has rows for single {col} labels and rows for "
            f"all of them (a blank or 'National' {col})",
            f"/mapping/mff/{col}",
            action=f"Give each variable at one level: per {col}, or in total only.",
        )
    method = opts.allocation.get(col, "equal")
    weights = pd.Series(1.0 / len(levels), index=levels)
    kpi = opts.kpi or next((v for v, r in opts.roles.items() if r == "target"), None)
    if method == "kpi":
        rows = table[(table["__var__"] == kpi) & ~total] if kpi is not None else table[:0]
        sums = rows.groupby(col)["__value__"].sum().reindex(levels)
        if sums.notna().all() and float(sums.sum()) > 0 and (sums >= 0).all():
            weights = sums / float(sums.sum())
        elif notes is not None:
            notes.append(
                {
                    "pointer": f"/mapping/mff/allocation/{col}",
                    "message": "the server divided the variables given in total equally across "
                    f"{col}: "
                    + (
                        f"the KPI {kpi!r} has no positive total per {col}"
                        if kpi
                        else "no KPI is known (give a variable the role 'target' or set 'kpi')"
                    ),
                }
            )
    parts = [table[~total]]
    coarse = table[total]
    for level in levels:
        part = coarse.copy()
        part[col] = level
        part["__value__"] = part["__value__"] * float(weights[level])
        parts.append(part)
    if notes is not None:
        names = sorted(set(coarse["__var__"]))
        notes.append(
            {
                "pointer": f"/mapping/mff/{col}",
                "message": f"the server divided {len(names)} variable(s) given in total "
                f"({', '.join(names[:5])}) across the {len(levels)} {col} levels "
                + ("equally" if method == "equal" or weights.nunique() == 1 else "by the KPI"),
            }
        )
    return pd.concat(parts, ignore_index=True)


def dataset_from_mff(
    df: pd.DataFrame,
    opts: MffOptions,
    *,
    limits: IngestLimits = _UNLIMITED,
) -> tuple[xr.Dataset, DataSchema]:
    """The MFF preset: long ``(period, keys..., variable) -> value`` rows, every variable
    kept under its own name."""
    notes: Notes = []
    table, index, variables = mff_table(df, opts, limits, notes=notes)
    frame = table.rename(columns=index)
    _check_cube(frame, list(index.values()), limits, "/mapping/mff")
    ds = frame.set_index(list(index.values())).sort_index().to_xarray()
    for dim in list(index.values())[1:]:
        ds = ds.assign_coords({dim: _labels(ds[dim].values)})
    for var, role in opts.roles.items():
        if var not in ds.data_vars:
            raise _schema_error(
                f"MFF has no variable {var!r} to assign a role to",
                "/mapping/mff/roles",
                action=f"Use one of the VariableName values ({len(variables)} found).",
            )
        Role(role)
    draft = infer_schema(ds, roles=dict(opts.roles), units=dict(opts.units))
    freq = _mff_freq(opts, ds[opts.time_dim].values)
    dims = {
        opts.time_dim: DimSpec(kind="time", policy=DimPolicy.EXTEND, freq=freq),
        **{dim: DimSpec(kind="categorical") for dim in list(index.values())[1:]},
    }
    vars_ = {
        name: spec.model_copy(update={"missing": opts.missing}) for name, spec in draft.vars.items()
    }
    if notes:
        ds.attrs[INGEST_NOTES_ATTR] = notes
    return ds, DataSchema(layout="dense", dims=dims, vars=vars_)


def _mff_freq(opts: MffOptions, dates: Any) -> str | None:
    """The time dim's frequency: the declared one, unless the data has the same base
    frequency with another anchor (a declared weekly ``W`` means Sundays, while MFF periods
    are often Mondays)."""
    inferred = infer_freq(dates)
    if opts.freq is None:
        return inferred
    if inferred is not None and inferred.split("-")[0] == opts.freq.split("-")[0]:
        return inferred
    return opts.freq


def _mff_mapping(
    mapping: IngestMapping, index: Mapping[str, str], opts: MffOptions, table: pd.DataFrame
) -> IngestMapping:
    """An explicit mapping over a pivoted MFF table: default dims are the preset's."""
    if mapping.dims:
        dims = dict(mapping.dims)
    else:
        dims = {
            dim: DimMapping(
                column=col,
                spec=DimSpec(
                    kind="time",
                    policy=DimPolicy.EXTEND,
                    freq=_mff_freq(opts, pd.DatetimeIndex(table[col].unique()).sort_values()),
                )
                if col == opts.period_column
                else DimSpec(kind="categorical"),
            )
            for col, dim in index.items()
        }
    vars_ = {
        name: vm.model_copy(update={"units": vm.units or opts.units.get(vm.column)})
        for name, vm in mapping.vars.items()
    }
    return IngestMapping(format="csv", layout="dense", dims=dims, vars=vars_, wide=mapping.wide)


# ================================================================================== Meridian


def meridian_mapping(mapping: IngestMapping) -> IngestMapping:
    """The column mapping Meridian's ``CoordToColumns`` contract stands for (explicit
    ``dims``/``vars``/``wide`` entries of the mapping win)."""
    m = mapping.meridian
    assert m is not None
    dims: dict[str, DimMapping] = {
        "time": DimMapping(
            column=m.time, spec=DimSpec(kind="time", policy=DimPolicy.EXTEND, freq=None)
        )
    }
    if m.geo:
        dims["geo"] = DimMapping(
            column=m.geo,
            spec=DimSpec(kind="categorical", policy="new_levels", new_level_strategy="pooled"),
        )
    vars_: dict[str, VarMapping] = {"kpi": VarMapping(column=m.kpi, role=Role.TARGET)}
    if m.revenue_per_kpi:
        vars_["revenue_per_kpi"] = VarMapping(column=m.revenue_per_kpi, role=Role.COVARIATE)
    if m.population:
        vars_["population"] = VarMapping(column=m.population, role=Role.COVARIATE)

    def add(cols: list[str], role: Role, pointer: str) -> None:
        for col in cols:
            name = safe_name(col)
            if name in vars_ or name in dims:
                raise _schema_error(
                    f"two Meridian columns become the variable {name!r}",
                    f"/mapping/meridian/{pointer}",
                    action="Rename one of the columns.",
                )
            vars_[name] = VarMapping(column=col, role=role)

    add(m.controls, Role.CONTROL, "controls")
    add(m.organic_media, Role.MEDIA, "organic_media")
    add(m.non_media_treatments, Role.COVARIATE, "non_media_treatments")
    wide: dict[str, WideVarMapping] = dict(m.wide_vars())
    clash = sorted(set(wide) & set(vars_))
    if clash:
        raise _schema_error(
            f"a control column becomes {clash[0]!r}, a Meridian channel variable",
            "/mapping/meridian/controls",
            action="Rename the column.",
        )
    return IngestMapping(
        format="csv",
        layout="dense",
        dims={**dims, **mapping.dims},
        vars={**vars_, **mapping.vars},
        wide={**wide, **mapping.wide},
    )


def dataset_from_meridian(
    df: pd.DataFrame, mapping: IngestMapping, *, limits: IngestLimits = _UNLIMITED
) -> tuple[xr.Dataset, DataSchema]:
    """The ``meridian`` preset: a wide table under Meridian's column contract."""
    m = mapping.meridian
    assert m is not None
    ds, schema = dataset_from_table(df, meridian_mapping(mapping), limits=limits)
    if m.freq and schema.dims["time"].freq is None:
        schema.dims["time"] = schema.dims["time"].model_copy(update={"freq": m.freq})
    if m.kpi_type == "revenue":
        schema.vars["kpi"] = schema.vars["kpi"].model_copy(
            update={"description": "Meridian KPI (revenue)"}
        )
    return ds, schema


# ========================================================================= self-describing


def _self_describing(
    path: Path,
    fmt: str,
    mapping: IngestMapping | None,
    limits: IngestLimits = _UNLIMITED,
    *,
    checked: bool = False,
) -> tuple[xr.Dataset, DataSchema]:
    if fmt == "zarr_zip":
        check_zip_archive(path, limits)
        wire = WireLimits(
            max_uncompressed_bytes=limits.max_uncompressed_bytes,
            max_string_chars=limits.max_cell_chars,
            max_variables=limits.max_columns,
        )
        try:
            ds = read_zarr_zip(path, limits=wire)
        except PayloadLimitError as exc:
            raise problem(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "after decoding, the zarr zip is larger than the upload limit",
                action="Split the dataset, or use a larger resource class.",
                untrusted=exc.detail,
            ) from None
        except WireFormatError as exc:
            raise _schema_error(
                "the upload is not a readable zarr zip",
                action="Write it with mapi_sdk.wire.write_zarr_zip (zarr v3, blosc or zstd "
                "codecs).",
                untrusted_text=exc.detail,
            ) from None
    else:
        ds = _read_netcdf(path, limits, checked=checked)
    stored = ds.attrs.get(SCHEMA_ATTR)
    base: DataSchema | None = None
    if isinstance(stored, str):
        try:
            base = DataSchema.model_validate_json(stored)
        except ValueError:
            base = None
    if mapping is not None and (mapping.dims or mapping.vars):
        rename = {dm.column: n for n, dm in mapping.dims.items() if dm.column != n}
        rename |= {vm.column: n for n, vm in mapping.vars.items() if vm.column != n}
        missing = [c for c in rename if c not in ds.variables and c not in ds.dims]
        if missing:
            raise _schema_error(
                f"the upload has no variable or dim {missing[0]!r}",
                "/mapping",
                action=f"It has {', '.join(repr(str(v)[:40]) for v in list(ds.variables)[:8])}.",
            )
        keep = list(mapping.vars) or [str(v) for v in ds.data_vars]
        renamed = ds.rename(rename)
        ds = renamed[[v for v in keep if v in renamed.data_vars]]
        draft = infer_schema(
            ds,
            roles={n: vm.role for n, vm in mapping.vars.items()},
            units={n: vm.units for n, vm in mapping.vars.items() if vm.units},
        )
        dims = {**draft.dims, **{n: dm.spec for n, dm in mapping.dims.items()}}
        vars_ = {
            n: s.model_copy(
                update={
                    "missing": mapping.vars[n].missing,
                    "description": mapping.vars[n].description,
                }
            )
            if n in mapping.vars
            else s
            for n, s in draft.vars.items()
        }
        return ds, DataSchema(layout=mapping.layout, dims=dims, vars=vars_)
    return ds, base or infer_schema(ds)


def _is_hdf5(path: Path) -> bool:
    """True when ``path`` is read as HDF5 (guard first): libhdf5 would find a superblock in
    it - after a user block too, so every offset libhdf5 tries is looked at - and it does not
    start with the NetCDF-3 magic (such a file only ever goes to the scipy reader)."""
    return read_as_hdf5(path)


def _read_netcdf(
    path: Path, limits: IngestLimits = _UNLIMITED, *, checked: bool = False
) -> xr.Dataset:
    hdf5 = _is_hdf5(path)
    if hdf5 and not checked:
        # before any xarray/h5py open of the upload
        check_hdf5(path, limits, lenient_attrs=True)
    # a file that is not HDF5 (and so skipped the guard) is only ever read as NetCDF-3:
    # an HDF5-capable engine must never see a file the guard did not check
    engines = ("h5netcdf", "netcdf4") if hdf5 else ("scipy",)
    budget = limits.expansion_budget(path.stat().st_size)
    last: Exception | None = None
    for engine in engines:
        try:
            with xr.open_dataset(path, engine=engine) as opened:
                if opened.nbytes > budget:
                    raise problem(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        f"the netCDF file decodes to {opened.nbytes:,} bytes (the maximum is "
                        f"{limits.max_decompression_ratio:g} times its size and "
                        f"{limits.max_uncompressed_bytes:,} bytes)",
                        action="Split the dataset, or write it with less compression.",
                    )
                if len(opened.data_vars) > limits.max_columns:
                    raise problem(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        f"the netCDF file has {len(opened.data_vars):,} variables",
                        action="Drop the variables the model does not need.",
                    )
                return opened.load()
        except (ValueError, ImportError, ModuleNotFoundError) as exc:
            last = exc
            continue
        except OSError as exc:
            raise _schema_error(
                "the upload is not a readable netCDF file",
                action="Write it with xarray's Dataset.to_netcdf.",
                untrusted_text=str(exc)[:2000],
            ) from None
    if hdf5:
        raise MapiError(
            "netCDF-4 (HDF5) uploads need h5netcdf in the runner image",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            untrusted_text=str(last)[:2000] if last else None,
        )
    raise _schema_error(
        "the upload is not a readable netCDF file",
        action="Write it with xarray's Dataset.to_netcdf.",
        untrusted_text=str(last)[:2000] if last else None,
    )


# ================================================================================ profile


def _json_scalar(v: Any) -> Any:
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, pd.Timestamp | np.datetime64):
        return pd.Timestamp(v).isoformat()
    return v


def _uint8_profile(values: np.ndarray) -> dict[str, Any]:
    """The statistics of 8-bit pixels from a 256-bin count: exact, and without a float copy
    of the whole array (an image stack can hold 256 Mi values)."""
    flat = values.reshape(-1)
    counts = np.zeros(256, dtype=np.float64)
    for start in range(0, flat.size, _COUNT_CHUNK):  # bincount copies to int64: a part at a time
        counts += np.bincount(flat[start : start + _COUNT_CHUNK], minlength=256)
    n = int(counts.sum())
    out: dict[str, Any] = {"missing": 0, "count": n}
    if not n:
        return out
    levels = np.arange(256, dtype=np.float64)
    mean = float((counts * levels).sum() / n)
    cum = np.cumsum(counts)
    q = [float(np.searchsorted(cum, p * (n - 1) + 1)) for p in (0.05, 0.5, 0.95)]
    present = np.flatnonzero(counts)
    out.update(
        mean=mean,
        std=float(np.sqrt((counts * (levels - mean) ** 2).sum() / n)),
        min=float(present[0]),
        max=float(present[-1]),
        p05=q[0],
        p50=q[1],
        p95=q[2],
        zeros=int(counts[0]),
    )
    return out


def _var_profile(da: xr.DataArray) -> dict[str, Any]:
    values = np.asarray(da.values)
    out: dict[str, Any] = {
        "dims": [str(d) for d in da.dims],
        "dtype": str(da.dtype),
        "size": int(values.size),
    }
    if values.dtype == np.uint8:
        out.update(_uint8_profile(values))
    elif values.dtype.kind in "fiub":
        x = values.astype(float).reshape(-1)
        ok = x[np.isfinite(x)]
        out["missing"] = int(x.size - ok.size)
        out["count"] = int(ok.size)
        if ok.size:
            q = np.quantile(ok, [0.05, 0.5, 0.95])
            out.update(
                mean=float(ok.mean()),
                std=float(ok.std()),
                min=float(ok.min()),
                max=float(ok.max()),
                p05=float(q[0]),
                p50=float(q[1]),
                p95=float(q[2]),
                zeros=int((ok == 0).sum()),
            )
    else:
        flat = pd.Series(values.reshape(-1))
        out["missing"] = int(flat.isna().sum())
        out["count"] = int(flat.size - out["missing"])
        out["distinct"] = int(flat.nunique())
    return out


def _time_dim(ds: xr.Dataset, schema: DataSchema) -> str | None:
    for name, spec in schema.dims.items():
        if spec.kind == "time" and name in ds.dims:
            return str(name)
    for dim in ds.dims:
        if dim in ds.coords and np.issubdtype(ds[dim].dtype, np.datetime64):
            return str(dim)
    return None


def missingness_grid(ds: xr.Dataset, schema: DataSchema) -> MissingnessGrid | None:
    """Fraction missing per variable along the time-like dim, binned to at most
    :data:`~mapi_spec.views.MAX_GRID_COORDS` columns (other dims are averaged)."""
    along = _time_dim(ds, schema)
    if along is None:
        return None
    names = [str(v) for v in ds.data_vars if along in ds[v].dims][:1024]
    if not names:
        return None
    n = int(ds.sizes[along])
    bins = np.array_split(np.arange(n), min(n, MAX_GRID_COORDS)) if n else []
    coords = [str(_json_scalar(ds[along].values[b[0]])) for b in bins]
    fraction: list[list[float]] = []
    for name in names:
        da = ds[name]
        other = [d for d in da.dims if d != along]
        per = da.isnull().mean(dim=other) if other else da.isnull().astype(float)
        vals = np.asarray(per.transpose(along).values, dtype=float)
        fraction.append([round(float(vals[b].mean()), 6) for b in bins])
    return MissingnessGrid(along=along, labels=names, coords=coords, fraction=fraction)


def profile(ds: xr.Dataset, schema: DataSchema, issues: list[Any], raw: bytes) -> dict[str, Any]:
    """``profile.json`` (:class:`~mapi_spec.views.ProfileView`) plus provenance fields."""
    view = ProfileView(
        vars={str(v): VarProfile.model_validate(_var_profile(ds[v])) for v in ds.data_vars},
        missingness=missingness_grid(ds, schema),
    )
    return {
        **view.model_dump(mode="json"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "summary": data_summary(ds, schema).model_dump(mode="json"),
        "issues": [i.model_dump(mode="json") for i in issues],
    }


def _preview_dataset(ds: xr.Dataset) -> xr.Dataset:
    """``ds`` without its image stacks (``image_folder``: the pixels): one row per image with
    its label and manifest columns, never one row per pixel."""
    images = [v for v in ds.data_vars if ds[v].attrs.get(IMAGE_ATTR) == "1"]
    if not images:
        return ds
    rows = ds[images[0]].dims[0]
    other = {d for v in images for d in ds[v].dims if d != rows}
    return ds.drop_vars(images).drop_dims(sorted(other, key=str), errors="ignore")


def preview(ds: xr.Dataset, rows: int) -> dict[str, Any]:
    """``preview.json`` (:class:`~mapi_spec.views.PreviewView`): dim columns, then vars."""
    ds = _preview_dataset(ds)
    df = ds.to_dataframe().reset_index() if ds.dims else pd.DataFrame()
    head = df.head(min(rows, MAX_PREVIEW_ROWS))
    dims = {str(d) for d in ds.dims}
    columns = [
        PreviewColumn(
            name=str(c), dtype=str(head[c].dtype), role="dim" if str(c) in dims else "var"
        )
        for c in head.columns
    ]
    view = PreviewView(
        columns=columns,
        rows=[[_json_scalar(v) for v in row] for row in head.itertuples(index=False)],
        total_rows=len(df),
    )
    return view.model_dump(mode="json")


# ==================================================================================== run


def _job_mapping(job: IngestJob) -> IngestMapping:
    if job.mapping is not None:
        return job.mapping
    return IngestMapping(format="mff", preset=IngestPreset.MFF, mff=job.mff)


def _check_format(path: Path, fmt: str) -> None:
    kind = sniff(path)
    if kind == fmt or (fmt in ("hdf5", "netcdf") and _is_hdf5(path)):
        return
    if fmt == "image_folder" and kind == "zarr_zip":  # every zip has the same magic
        return
    raise _schema_error(
        f"the upload is not a {'zip' if fmt == 'image_folder' else fmt} file",
        "/mapping/format",
        action="Set the format of the mapping to the format of the file (csv, parquet, "
        "mff, zarr_zip, netcdf, hdf5 or image_folder).",
    )


def _tree_upload(
    tree: xr.DataTree, schema: TreeSchema, size: int, warnings: list[tuple[str, str | None]]
) -> Upload:
    """A tree, or its root as a plain dataset when the tree has no other node."""
    if len(schema.nodes) == 1 and ROOT in schema.nodes:
        ds = tree.to_dataset(inherit=False)
        return Upload(ds, schema.nodes[ROOT], None, warnings, [], size)
    return Upload(tree, schema, None, warnings, [], size)


def build_upload(path: Path, mapping: IngestMapping, limits: IngestLimits) -> Upload:
    """Parse one upload as ``mapping`` says: a plain dataset or (M4) a tree."""
    size = path.stat().st_size
    if mapping.format == "image_folder":
        _check_format(path, mapping.format)
        images, image_schema, image_notes = read_image_folder(
            path, mapping.images or ImageFolderOptions(), limits
        )
        return Upload(images, image_schema, None, [], image_notes, size)
    if mapping.format in ("zarr_zip", "netcdf", "hdf5"):
        _check_format(path, mapping.format)
        if mapping.format == "hdf5":
            assert mapping.hdf5 is not None
            walk = walk_hdf5(path, limits)
            read = read_hdf5_mapped(path, mapping.hdf5, limits, walk)
            tree = tree_from_nodes(read.nodes, "/mapping/hdf5")
            check_tree_limits(tree, limits, limits.expansion_budget(size))
            tschema = schema_for_nodes(
                tree,
                var_meta=lambda n, v: read.var_specs.get((n, v)),
                dim_meta=lambda n, d: read.dim_specs.get((n, d)),
                layout=mapping.layout,
                use_stored=False,
            )
            warnings = [(w, None) for w in walk.warnings] + read.warnings
            return _tree_upload(tree, tschema, size, warnings)
        if mapping.format == "zarr_zip":
            check_zip_archive(path, limits)
            if zarr_has_groups(path):
                tree, var_meta, dim_meta = map_tree(read_zarr_tree(path, limits), mapping)
                tschema = schema_for_nodes(
                    tree,
                    var_meta=var_meta,
                    dim_meta=dim_meta,
                    layout=mapping.layout,
                    use_stored=not (mapping.dims or mapping.vars),
                )
                return Upload(tree, tschema, None, [], [], size)
            ds, flat = _self_describing(path, mapping.format, mapping, limits)
            return Upload(ds, flat, None, [], [], size)
        if _is_hdf5(path):
            # self-describing NetCDF/xarray HDF5 ingested before M4 without attribute caps
            walk = walk_hdf5(path, limits, lenient_attrs=True)
            warnings = [(w, None) for w in walk.warnings]
            if len(walk.groups) > 1:
                tree, var_meta, dim_meta = map_tree(read_netcdf_tree(path, limits), mapping)
                tschema = schema_for_nodes(
                    tree,
                    var_meta=var_meta,
                    dim_meta=dim_meta,
                    layout=mapping.layout,
                    use_stored=not (mapping.dims or mapping.vars),
                )
                return Upload(tree, tschema, None, warnings, [], size)
            ds, flat = _self_describing(path, mapping.format, mapping, limits, checked=True)
            return Upload(ds, flat, None, warnings, [], size)
        ds, flat = _self_describing(path, mapping.format, mapping, limits)
        return Upload(ds, flat, None, [], [], size)
    preset = mapping.preset
    fmt = "parquet" if mapping.format == "parquet" else "csv"
    df = read_table(path, fmt, limits)
    if preset is IngestPreset.MFF:
        opts = mapping.effective_mff
        assert opts is not None
        notes: Notes = []
        table, index, variables = mff_table(df, opts, limits, notes=notes)
        profiles = column_profiles(table, variables=variables)
        if mapping.vars or mapping.wide:
            ds, schema = dataset_from_table(
                table, _mff_mapping(mapping, index, opts, table), limits=limits
            )
        else:
            ds, schema = dataset_from_mff(df, opts, limits=limits)
        notes = [*notes, *(ds.attrs.pop(INGEST_NOTES_ATTR, None) or [])]
        return Upload(ds, schema, profiles, [], notes, size)
    profiles = column_profiles(df)
    if preset is IngestPreset.MERIDIAN:
        ds, schema = dataset_from_meridian(df, mapping, limits=limits)
    else:
        ds, schema = dataset_from_table(df, mapping, limits=limits)
    notes = list(ds.attrs.pop(INGEST_NOTES_ATTR, None) or [])
    return Upload(ds, schema, profiles, [], notes, size)


def build_dataset(
    path: Path, job: IngestJob
) -> tuple[xr.Dataset, DataSchema, list[ColumnProfile] | None]:
    """Parse ``path`` as ``job`` says: ``(dataset, schema, column profiles)``.

    A plain-dataset upload only (a tree upload raises ``SCHEMA_INVALID``; use
    :func:`build_upload`). Notes of the build stay in ``ds.attrs[INGEST_NOTES_ATTR]``.
    """
    up = build_upload(path, _job_mapping(job), IngestLimits.from_job(job))
    if not isinstance(up.data, xr.Dataset) or not isinstance(up.schema, DataSchema):
        raise _schema_error("the upload has groups: it is a tree dataset", "/mapping")
    if up.notes:
        up.data.attrs[INGEST_NOTES_ATTR] = up.notes
    return up.data, up.schema, up.columns


def _assembly_upload(ctx: WorkerContext, job: IngestJob, limits: IngestLimits) -> Upload:
    """Every source of the job's assembly parsed, then combined per node."""
    assert job.assembly is not None
    sources = job.assembly.sources
    if len(sources) > limits.max_sources:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the version combines {len(sources)} uploads (the maximum is {limits.max_sources})",
            pointer="/assembly/sources",
        )
    for i, src in enumerate(sources):
        if src.mapping.format == "image_folder" and len(sources) > 1:
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                "an image folder must be the only upload of a version",
                pointer=f"/assembly/sources/{i}/mapping/format",
                action="Upload the zip of images as a version of its own, without other files.",
            )
    uploads: list[Upload] = []
    decoded = 0
    for i, src in enumerate(sources):
        ctx.cancel.check()
        ctx.progress(i / max(1, len(sources)) * 0.5, f"parsing upload {i + 1} of {len(sources)}")
        path = ctx.download(upload_input(i))
        assert path is not None
        prefix = f"/assembly/sources/{i}"
        try:
            up = build_upload(path, src.mapping, limits)
        except MapiError as exc:
            raise prefix_error(exc, prefix) from None
        decoded += (
            int(up.data.nbytes)
            if isinstance(up.data, xr.Dataset)
            else sum(int(n.to_dataset(inherit=False).nbytes) for n in up.data.subtree)
        )
        if decoded > limits.max_uncompressed_bytes:
            raise problem(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"the uploads decode to more than {limits.max_uncompressed_bytes:,} bytes",
                pointer=prefix,
                action="Combine fewer files, or split the dataset.",
            )
        up.notes = [{**n, "pointer": prefix + n.get("pointer", "")} for n in up.notes]
        uploads.append(up)
    ctx.state(RunnerPhase.VALIDATE, "combining the uploads")
    return assemble(job.assembly, uploads, limits)


def _note_issues(ctx: WorkerContext, notes: list[dict[str, str]]) -> list[Issue]:
    issues: list[Issue] = []
    for note in notes:
        issues.append(
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                severity=Severity.INFO,
                pointer=str(note["pointer"]),
                message=str(note["message"])[:2000],
            )
        )
        ctx.log(str(note["message"]))
    return issues


def _raise_errors(issues: list[Issue]) -> None:
    errors = [i for i in issues if i.severity is Severity.ERROR]
    if errors:
        raise MapiError(
            f"the data does not match its schema: {errors[0].message}",
            code=ErrorCode.SCHEMA_INVALID,
            issues=issues[:1000],
        )


def run_ingest(ctx: WorkerContext, job: IngestJob) -> DataSchema:
    limits = IngestLimits.from_job(job)
    if job.assembly is not None:
        ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the uploads")
        up = _assembly_upload(ctx, job, limits)
    else:
        ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the upload")
        name = INPUT_UPLOAD if ctx.has_input(INPUT_UPLOAD) else INPUT_DATASET
        path = ctx.download(name)
        assert path is not None
        ctx.state(RunnerPhase.VALIDATE, "parsing and validating the upload")
        up = build_upload(path, _job_mapping(job), limits)
    ctx.cancel.check()
    for message, untrusted in up.warnings[:100]:
        ctx.warning("INGEST_WARNING", message, untrusted)
    if isinstance(up.data, xr.DataTree):
        assert isinstance(up.schema, TreeSchema)
        return _write_tree(ctx, job, up.data, up.schema, up.columns, up.notes)
    ds, schema, columns = up.data, up.schema, up.columns
    assert isinstance(schema, DataSchema)
    found = validate_dataset(schema, ds)
    issues = [i.to_issue() for i in found]
    issues.extend(_note_issues(ctx, up.notes))
    _raise_errors(issues)
    for issue in issues:
        if issue.severity is Severity.WARNING:
            ctx.warning("DATA_WARNING", issue.message)
    ds.attrs[SCHEMA_ATTR] = schema.model_dump_json()
    raw = write_zarr_zip(ds)
    assert raw is not None
    ctx.write_output(INGEST_DATA, raw)
    ctx.write_output(INGEST_SCHEMA, schema.model_dump_json(indent=2).encode())
    # metadata the control plane uses for CoordPlan checks of score payloads
    ctx.write_output("summary.json", data_summary(ds, schema).model_dump_json(indent=2).encode())
    ctx.write_json("profile.json", profile(ds, schema, issues, raw))
    ctx.write_json("preview.json", preview(ds, job.preview_rows))
    if columns is not None:
        ctx.write_output(INGEST_COLUMNS, write_profiles(columns))
    # M3.5: the cheap Data Explorer sections (ui/explore/); never fails the ingest
    from mapi_runner.worker.explore import write_data_explorer

    ctx.progress(0.95, "computing the Data Explorer overview")
    ds.attrs.pop(SCHEMA_ATTR, None)
    write_data_explorer(ctx, ds, schema, ref=job.dataset or "dataset://runner/upload@latest")
    ctx.progress(1.0, "dataset ready", force=True)
    return schema


def _write_tree(
    ctx: WorkerContext,
    job: IngestJob,
    tree: xr.DataTree,
    schema: TreeSchema,
    columns: list[ColumnProfile] | None,
    notes: list[dict[str, str]],
) -> DataSchema:
    """The outputs of a tree version (see the module docstring)."""
    issues = [i.to_issue() for i in validate_tree(tree, schema)]
    issues.extend(_note_issues(ctx, notes))
    _raise_errors(issues)
    for issue in issues:
        if issue.severity is Severity.WARNING:
            ctx.warning("DATA_WARNING", issue.message)
    root = schema.root
    tree.attrs = {
        **{k: v for k, v in tree.attrs.items() if k not in (SCHEMA_ATTR, TREE_SCHEMA_ATTR)},
        TREE_SCHEMA_ATTR: schema.model_dump_json(),
        SCHEMA_ATTR: root.model_dump_json(),
    }
    try:
        raw = write_tree_zip(tree)
    except WireFormatError as exc:
        raise _schema_error(
            "the tree dataset cannot be stored", untrusted_text=exc.detail
        ) from None
    assert raw is not None
    ctx.write_output(DATATREE_OUTPUT, raw)
    ctx.write_output(TREE_SCHEMA_OUTPUT, schema.model_dump_json(indent=2).encode())
    ctx.write_output(INGEST_SCHEMA, root.model_dump_json(indent=2).encode())
    datasets = {path: node_dataset(tree, path) for path in schema.nodes}
    summaries = {
        path: data_summary(ds, schema.nodes[path]).model_dump(mode="json")
        for path, ds in datasets.items()
    }
    ctx.write_json("summary.json", {**summaries[ROOT], "nodes": summaries})
    default = schema.default_node()
    dds, dschema = datasets[default], schema.nodes[default]
    ctx.write_json("profile.json", {**profile(dds, dschema, issues, raw), "node": default})
    ctx.write_json("preview.json", {**preview(dds, job.preview_rows), "node": default})
    if columns is not None:
        ctx.write_output(INGEST_COLUMNS, write_profiles(columns))
    from mapi_runner.worker.explore import write_data_explorer

    ref = job.dataset or "dataset://runner/upload@latest"
    ctx.progress(0.9, "computing the Data Explorer overview")
    write_data_explorer(ctx, dds, dschema, ref=ref, node=default)
    index: dict[str, Any] = {"default": default, "nodes": {}}
    with_vars = [p for p, s in schema.nodes.items() if s.vars]
    for k, path in enumerate(with_vars):
        ctx.cancel.check()
        ctx.progress(0.9 + 0.1 * k / max(1, len(with_vars)), f"exploring node {path}")
        prefix = f"ui/explore/nodes/{explore_slug(path)}/"
        written = write_data_explorer(
            ctx, datasets[path], schema.nodes[path], ref=ref, prefix=prefix, node=path
        )
        if written is not None:
            index["nodes"][path] = {"slug": explore_slug(path), "index": f"{prefix}index.json"}
    ctx.write_json(EXPLORE_NODES_INDEX, index)
    ctx.progress(1.0, "dataset ready", force=True)
    return root
