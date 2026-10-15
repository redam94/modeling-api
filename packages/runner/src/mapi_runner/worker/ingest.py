"""``ingest``: an uploaded file becomes a validated, content-addressed dataset version.

Inputs: ``upload`` (CSV, Parquet, ``*.zarr.zip`` or netCDF). Outputs::

    data.zarr.zip   the dataset (zarr v3 zip; root attr ``mapi_schema`` holds the schema)
    schema.json     its DataSchema
    profile.json    sha256 + size of data.zarr.zip, coordinate summary, per-variable stats,
                    plain-language validation issues
    preview.json    the first rows as a tidy table

Tabular uploads map columns to dims and variables (:class:`~mapi_spec.api.IngestMapping`,
Meridian ``CoordToColumns`` style) or use the MFF preset (long ``VariableName`` /
``VariableValue`` rows). Every byte of user data is parsed here, in the sandbox.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.errors import WireFormatError
from mapi_sdk.schema import data_summary, infer_freq, infer_schema, validate_dataset
from mapi_sdk.wire import read_zarr_zip, write_zarr_zip
from mapi_spec.api import IngestMapping
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.run import INPUT_DATASET, RunnerPhase
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role

from mapi_runner.jobs import INPUT_UPLOAD, IngestJob, MffOptions
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import SCHEMA_ATTR

__all__ = ["dataset_from_mff", "dataset_from_table", "run_ingest"]

_PARQUET_MAGIC = b"PAR1"
_HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
_ZIP_MAGIC = b"PK\x03\x04"


def _schema_error(message: str, pointer: str = "", **kw: Any) -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer, **kw)


# ======================================================================== tabular readers


def _read_table(path: Path, fmt: str, *, max_rows: int) -> pd.DataFrame:
    try:
        if fmt == "parquet":
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path, nrows=max_rows + 1, low_memory=False)
    except (ValueError, OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise _schema_error(
            f"the upload is not a readable {fmt} file", untrusted_text=str(exc)[:4000]
        ) from None
    if len(df) > max_rows:
        raise MapiError(
            f"the upload has more than {max_rows} rows", code=ErrorCode.PAYLOAD_TOO_LARGE
        )
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _sniff(path: Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(_PARQUET_MAGIC):
        return "parquet"
    if head.startswith(_ZIP_MAGIC):
        return "zarr_zip"
    if head.startswith(_HDF5_MAGIC) or head.startswith(b"CDF"):
        return "netcdf"
    return "csv"


def _coerce_dim(series: pd.Series, spec: DimSpec, pointer: str) -> pd.Series:
    try:
        if spec.kind == "time":
            return pd.to_datetime(series, errors="raise")
        if spec.kind in ("integer", "index"):
            return pd.to_numeric(series, errors="raise").astype("int64")
        if spec.kind == "float":
            return pd.to_numeric(series, errors="raise").astype("float64")
    except (ValueError, TypeError) as exc:
        raise _schema_error(
            f"column {series.name!r} cannot be read as a {spec.kind} dim",
            pointer,
            untrusted_text=str(exc)[:2000],
        ) from None
    if series.isna().any():
        raise _schema_error(f"dim column {series.name!r} has empty labels", pointer)
    return series.astype(str)


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
    return series.astype("string").astype(object).where(series.notna(), None)


def dataset_from_table(df: pd.DataFrame, mapping: IngestMapping) -> tuple[xr.Dataset, DataSchema]:
    """Build a dataset + schema from a parsed table and a column mapping."""
    columns = set(df.columns)
    for name, dm in mapping.dims.items():
        if dm.column not in columns:
            raise _schema_error(
                f"column {dm.column!r} (dim {name!r}) is not in the upload",
                f"/mapping/dims/{name}/column",
            )
    for name, vm in mapping.vars.items():
        if vm.column not in columns:
            raise _schema_error(
                f"column {vm.column!r} (variable {name!r}) is not in the upload",
                f"/mapping/vars/{name}/column",
            )
    if mapping.layout == "long" and len(mapping.dims) != 1:
        raise _schema_error(
            "a long-layout upload maps exactly one column to the observation dim", "/mapping/dims"
        )
    frame = pd.DataFrame(index=df.index)
    for name, dm in mapping.dims.items():
        frame[name] = _coerce_dim(df[dm.column], dm.spec, f"/mapping/dims/{name}")
    for name, vm in mapping.vars.items():
        frame[name] = _coerce_var(df[vm.column])
    dims = list(mapping.dims)
    dup = frame.duplicated(subset=dims)
    if dup.any():
        first = frame.loc[dup, dims].iloc[0].to_dict()
        raise _schema_error(
            f"{int(dup.sum())} rows repeat a ({', '.join(dims)}) combination, e.g. {first}",
            "/mapping/dims",
        )
    ds = frame.set_index(dims).sort_index().to_xarray()
    for name in dims:  # categorical labels as plain str coords
        if mapping.dims[name].spec.kind not in ("time", "integer", "index", "float"):
            ds = ds.assign_coords({name: np.asarray(ds[name].values, dtype=str)})
    draft = infer_schema(
        ds,
        roles={n: vm.role for n, vm in mapping.vars.items()},
        units={n: vm.units for n, vm in mapping.vars.items() if vm.units},
    )
    dim_specs: dict[str, DimSpec] = {}
    for name, dm in mapping.dims.items():
        spec = dm.spec
        if spec.kind == "time" and spec.freq is None:
            freq = infer_freq(ds[name].values)
            if freq is not None:
                spec = spec.model_copy(update={"freq": freq})
        dim_specs[name] = spec
    var_specs = {
        name: draft.vars[name].model_copy(
            update={"missing": vm.missing, "description": vm.description}
        )
        for name, vm in mapping.vars.items()
    }
    schema = DataSchema(layout=mapping.layout, dims=dim_specs, vars=var_specs)
    return ds, schema


def dataset_from_mff(df: pd.DataFrame, opts: MffOptions) -> tuple[xr.Dataset, DataSchema]:
    """The MFF preset: long ``(period, keys..., variable) -> value`` rows."""
    for col in (opts.period_column, opts.variable_column, opts.value_column):
        if col not in df.columns:
            raise _schema_error(f"MFF upload has no {col!r} column", "/mff")
    keys = {
        col: dim
        for col, dim in opts.key_columns.items()
        if col in df.columns and df[col].dropna().astype(str).nunique() > 1
    }
    frame = pd.DataFrame(
        {
            opts.time_dim: _coerce_dim(
                df[opts.period_column], DimSpec(kind="time"), f"/mff/{opts.period_column}"
            ),
            "__var__": df[opts.variable_column].astype(str).str.strip(),
            "__value__": pd.to_numeric(df[opts.value_column], errors="coerce"),
        }
    )
    for col, dim in keys.items():
        frame[dim] = df[col].astype(str).str.strip()
    bad = frame["__value__"].isna() & df[opts.value_column].notna()
    if bad.any():
        raise _schema_error(
            f"{int(bad.sum())} MFF values are not numbers", f"/mff/{opts.value_column}"
        )
    index = [opts.time_dim, *keys.values()]
    dup = frame.duplicated(subset=[*index, "__var__"])
    if dup.any():
        raise _schema_error(f"{int(dup.sum())} MFF rows repeat a key and variable", "/mff")
    wide = frame.pivot_table(
        index=index, columns="__var__", values="__value__", aggfunc="first", dropna=False
    )
    wide.columns = [str(c) for c in wide.columns]
    ds = wide.sort_index().to_xarray()
    for dim in keys.values():
        ds = ds.assign_coords({dim: np.asarray(ds[dim].values, dtype=str)})
    for var, role in opts.roles.items():
        if var not in ds.data_vars:
            raise _schema_error(f"MFF has no variable {var!r} to assign a role to", "/mff/roles")
        Role(role)
    draft = infer_schema(ds, roles=dict(opts.roles), units=dict(opts.units))
    freq = opts.freq or infer_freq(ds[opts.time_dim].values)
    dims = {
        opts.time_dim: DimSpec(kind="time", policy=DimPolicy.EXTEND, freq=freq),
        **{dim: DimSpec(kind="categorical") for dim in keys.values()},
    }
    vars_ = {
        name: spec.model_copy(update={"missing": opts.missing}) for name, spec in draft.vars.items()
    }
    return ds, DataSchema(layout="dense", dims=dims, vars=vars_)


# ========================================================================= self-describing


def _self_describing(
    path: Path, fmt: str, mapping: IngestMapping | None
) -> tuple[xr.Dataset, DataSchema]:
    if fmt == "zarr_zip":
        try:
            ds = read_zarr_zip(path)
        except WireFormatError as exc:
            raise _schema_error(
                "the upload is not a readable zarr zip", untrusted_text=exc.detail
            ) from None
    else:
        ds = _read_netcdf(path)
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
            raise _schema_error(f"the upload has no variable or dim {missing[0]!r}", "/mapping")
        keep = list(mapping.vars) or [str(v) for v in ds.data_vars]
        ds = ds.rename(rename)[[v for v in keep if v in ds.data_vars]]
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


def _read_netcdf(path: Path) -> xr.Dataset:
    with open(path, "rb") as fh:
        hdf5 = fh.read(8).startswith(_HDF5_MAGIC)
    engines = ("h5netcdf", "netcdf4") if hdf5 else ("scipy", "h5netcdf", "netcdf4")
    last: Exception | None = None
    for engine in engines:
        try:
            with xr.open_dataset(path, engine=engine) as opened:
                return opened.load()
        except (ValueError, ImportError, ModuleNotFoundError) as exc:
            last = exc
            continue
        except OSError as exc:
            raise _schema_error(
                "the upload is not a readable netCDF file", untrusted_text=str(exc)[:2000]
            ) from None
    if hdf5:
        raise MapiError(
            "netCDF-4 (HDF5) uploads need h5netcdf in the runner image",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            untrusted_text=str(last)[:2000] if last else None,
        )
    raise _schema_error(
        "the upload is not a readable netCDF file",
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


def _var_profile(da: xr.DataArray) -> dict[str, Any]:
    values = np.asarray(da.values)
    out: dict[str, Any] = {
        "dims": [str(d) for d in da.dims],
        "dtype": str(da.dtype),
        "size": int(values.size),
    }
    if values.dtype.kind in "fiub":
        x = values.astype(float).reshape(-1)
        ok = x[np.isfinite(x)]
        out["missing"] = int(x.size - ok.size)
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
        out["distinct"] = int(flat.nunique())
    return out


def profile(ds: xr.Dataset, schema: DataSchema, issues: list[Any], raw: bytes) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "summary": data_summary(ds, schema).model_dump(mode="json"),
        "vars": {str(v): _var_profile(ds[v]) for v in ds.data_vars},
        "issues": [i.model_dump(mode="json") for i in issues],
    }


def preview(ds: xr.Dataset, rows: int) -> dict[str, Any]:
    df = ds.to_dataframe().reset_index() if ds.dims else pd.DataFrame()
    head = df.head(rows)
    return {
        "columns": [str(c) for c in head.columns],
        "rows": [[_json_scalar(v) for v in row] for row in head.itertuples(index=False)],
        "total_rows": len(df),
    }


# ==================================================================================== run


def run_ingest(ctx: WorkerContext, job: IngestJob) -> DataSchema:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the upload")
    name = INPUT_UPLOAD if ctx.has_input(INPUT_UPLOAD) else INPUT_DATASET
    path = ctx.download(name)
    assert path is not None
    ctx.state(RunnerPhase.VALIDATE, "parsing and validating the upload")
    if job.preset == "mff":
        fmt = "parquet" if _sniff(path) == "parquet" else "csv"
        ds, schema = dataset_from_mff(_read_table(path, fmt, max_rows=job.max_rows), job.mff)
    else:
        mapping = job.mapping
        assert mapping is not None
        if mapping.format in ("csv", "parquet"):
            df = _read_table(path, mapping.format, max_rows=job.max_rows)
            ds, schema = dataset_from_table(df, mapping)
        else:
            ds, schema = _self_describing(path, mapping.format, mapping)
    ctx.cancel.check()
    found = validate_dataset(schema, ds)
    issues = [i.to_issue() for i in found]
    errors = [i for i in issues if i.severity is Severity.ERROR]
    if errors:
        raise MapiError(
            f"the data does not match its schema: {errors[0].message}",
            code=ErrorCode.SCHEMA_INVALID,
            issues=issues,
        )
    for issue in issues:
        if issue.severity is Severity.WARNING:
            ctx.warning("DATA_WARNING", issue.message)
    ds.attrs[SCHEMA_ATTR] = schema.model_dump_json()
    raw = write_zarr_zip(ds)
    assert raw is not None
    ctx.write_output("data.zarr.zip", raw)
    ctx.write_output("schema.json", schema.model_dump_json(indent=2).encode())
    ctx.write_json("profile.json", profile(ds, schema, issues, raw))
    ctx.write_json("preview.json", preview(ds, job.preview_rows))
    ctx.progress(1.0, "dataset ready", force=True)
    return schema
