"""``inspect`` (M4): list the groups and arrays of an upload from its metadata only.

The output, ``inspection.json`` (:class:`~mapi_spec.ingest.UploadInspection`), is what a
user maps a raw HDF5 file from (:class:`~mapi_spec.ingest.Hdf5Mapping`) and how a NetCDF or
zarr file's groups are previewed before ingest. No array data is read:

* HDF5 (raw or NetCDF-4): :func:`~mapi_runner.worker.hdf5_guard.walk_hdf5` first (an unsafe
  file is refused with ``INGEST_UNSAFE_FILE``), then dim hints from the dimension scales
  (``DIMENSION_LIST``) or a ``_ARRAY_DIMENSIONS`` attribute;
* NetCDF-3: the file header (variables, dims, attributes);
* zarr zip: the ``zarr.json`` documents (no chunk is decoded);
* Parquet: the footer schema; CSV: nothing (its columns are profiled at ingest).

Every cap of :class:`~mapi_spec.ingest.UploadInspection` is enforced here: arrays past
``max_arrays`` set ``truncated``; long attribute values are cut, unusual ones dropped; the
whole document is trimmed until it fits :data:`~mapi_spec.ingest.MAX_INSPECTION_BYTES`.
"""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path
from typing import Any, Final

from mapi_spec.ingest import (
    MAX_INSPECT_ARRAYS,
    MAX_INSPECT_ATTR_CHARS,
    MAX_INSPECT_ATTRS,
    MAX_INSPECTION_BYTES,
    UploadInspection,
)
from mapi_spec.jobs import INPUT_UPLOAD, INSPECT_OUTPUT, InspectJob
from mapi_spec.run import INPUT_DATASET, RunnerPhase

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.hdf5_guard import Hdf5Walk, walk_hdf5
from mapi_runner.worker.tabular import HDF5_MAGIC, IngestLimits, check_zip_archive, sniff

__all__ = ["inspect_upload", "run_inspect"]

MAX_GROUPS: Final = 10_000
MAX_WARNINGS: Final = 1000
MAX_FILTERS: Final = 32
MAX_DIMS: Final = 64
MAX_PATH: Final = 1025
MAX_SHORT: Final = 256
_ZARR_META_BYTES: Final = 1024 * 1024
_INTERNAL_ATTRS: Final = frozenset(
    {"CLASS", "NAME", "DIMENSION_LIST", "REFERENCE_LIST", "DIMENSION_LABELS", "_Netcdf4Dimid"}
)


# ---------------------------------------------------------------------------- capping


def _scalar(value: Any) -> tuple[bool, Any]:
    """``(keep, value)`` for one attribute value: small JSON scalars only."""
    if hasattr(value, "item") and getattr(value, "ndim", 0) == 0:
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if value is None or isinstance(value, bool | int):
        return True, value
    if isinstance(value, float):
        return True, value if math.isfinite(value) else None
    if isinstance(value, str):
        return True, value[:MAX_INSPECT_ATTR_CHARS]
    return False, None


def _attrs(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= MAX_INSPECT_ATTRS:
            break
        name = str(key)
        if len(name) > MAX_SHORT or name in _INTERNAL_ATTRS or name.startswith("_Netcdf4"):
            continue
        keep, v = _scalar(value)
        if keep:
            out[name] = v
    return out


def _short(items: Any, limit: int) -> list[str]:
    return [str(x)[:MAX_SHORT] for x in list(items)[:limit]]


class _Doc:
    """The inspection being built, with its caps."""

    def __init__(self, fmt: str, size: int, max_arrays: int) -> None:
        self.format = fmt
        self.size = size
        self.max_arrays = min(max_arrays, MAX_INSPECT_ARRAYS)
        self.groups: list[str] = []
        self.arrays: list[dict[str, Any]] = []
        self.truncated = False
        self.warnings: list[str] = []
        self._skipped_paths = 0

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message[:2000])

    def group(self, path: str) -> None:
        if len(path) > MAX_PATH:
            self._skipped_paths += 1
            return
        if len(self.groups) >= MAX_GROUPS:
            self.truncated = True
            return
        self.groups.append(path)

    def full(self) -> bool:
        return len(self.arrays) >= self.max_arrays

    def array(
        self,
        path: str,
        shape: Any,
        dtype: str,
        *,
        chunks: Any = None,
        filters: Any = (),
        dims_hint: Any = None,
        attrs: dict[str, Any] | None = None,
    ) -> None:
        if self.full():
            self.truncated = True
            return
        if len(path) > MAX_PATH:
            self._skipped_paths += 1
            return
        shape_l = [int(s) for s in shape][:MAX_DIMS]
        chunk_l = None if chunks is None else [max(1, int(c)) for c in chunks][:MAX_DIMS]
        dims = None if dims_hint is None else _short(dims_hint, MAX_DIMS)
        self.arrays.append(
            {
                "path": path,
                "shape": shape_l,
                "dtype": str(dtype)[:64],
                "chunks": chunk_l,
                "filters": _short(filters, MAX_FILTERS),
                "dims_hint": dims,
                "attrs": _attrs(attrs or {}),
            }
        )

    def build(self) -> UploadInspection:
        if self._skipped_paths:
            self.warn(f"{self._skipped_paths} object(s) with paths over {MAX_PATH} characters")
        if self.truncated:
            self.warn(f"the listing stops after {self.max_arrays} arrays or {MAX_GROUPS} groups")
        doc: dict[str, Any] = {
            "format": self.format,
            "size_bytes": self.size,
            "groups": self.groups,
            "arrays": self.arrays,
            "truncated": self.truncated,
            "warnings": self.warnings,
        }
        # trim to the byte cap: first the attributes, then arrays (each halving)
        while len(json.dumps(doc).encode()) > MAX_INSPECTION_BYTES - 4096:
            if any(a["attrs"] for a in doc["arrays"]):
                for a in doc["arrays"]:
                    a["attrs"] = {}
                doc["warnings"] = [*doc["warnings"], "attributes were left out (size)"][
                    :MAX_WARNINGS
                ]
            elif len(doc["groups"]) > 64:
                doc["groups"] = doc["groups"][: len(doc["groups"]) // 2]
                doc["truncated"] = True
            else:
                doc["arrays"] = doc["arrays"][: len(doc["arrays"]) // 2]
                doc["truncated"] = True
        return UploadInspection.model_validate(doc)


# ---------------------------------------------------------------------------- formats


def _dims_from_attr(attrs: dict[str, Any]) -> list[str] | None:
    raw = attrs.get("_ARRAY_DIMENSIONS")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list | tuple) and all(isinstance(x, str) for x in raw):
        return [str(x) for x in raw]
    return None


def _hdf5(path: Path, doc: _Doc, walk: Hdf5Walk) -> None:
    import h5py

    for g in walk.groups:
        doc.group(g)
    with h5py.File(path, "r", locking=False) as f:  # safe: the guard walked it
        if doc.format == "hdf5" and "_NCProperties" in f.attrs:
            doc.warn("the file looks like NetCDF-4: ingest it with format 'netcdf'")
        for arr in walk.arrays:
            if doc.full():
                doc.truncated = True
                break
            hint = _dims_from_attr(arr.attrs)
            if hint is None and len(arr.shape) > 0:
                hint = _scale_names(f, arr.path)
            doc.array(
                arr.path,
                arr.shape,
                arr.dtype,
                chunks=arr.chunks,
                filters=arr.filters,
                dims_hint=hint,
                attrs=arr.attrs,
            )


def _scale_names(f: Any, path: str) -> list[str] | None:
    """Dim names from the dimension scales attached to ``path`` (NetCDF-4 dims)."""
    try:
        dset = f[path]
        names: list[str] = []
        for dim in dset.dims:
            label = dim.label
            if len(dim) > 0:
                scale = dim[0].name or ""
                name = scale.rsplit("/", 1)[-1]
            else:
                name = label or ""
            if not name:
                return None
            names.append(name)
        return names
    except (KeyError, ValueError, OSError, RuntimeError, TypeError):
        return None


def _netcdf3(path: Path, doc: _Doc) -> None:
    import xarray as xr

    doc.group("/")
    with xr.open_dataset(path, engine="scipy", decode_cf=False, mask_and_scale=False) as ds:
        for name, var in ds.variables.items():
            doc.array(
                "/" + str(name),
                var.shape,
                str(var.dtype),
                dims_hint=[str(d) for d in var.dims],
                attrs=dict(var.attrs),
            )


def _zarr(path: Path, doc: _Doc, limits: IngestLimits) -> None:
    check_zip_archive(path, limits)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename
            base = name.rsplit("/", 1)[-1]
            if base != "zarr.json":
                continue
            if info.file_size > _ZARR_META_BYTES:
                doc.warn("a zarr metadata document is too large to list")
                continue
            try:
                meta = json.loads(zf.read(info))
            except (ValueError, UnicodeDecodeError, RecursionError):
                doc.warn("a zarr metadata document is not valid JSON")
                continue
            if not isinstance(meta, dict):
                continue
            node = "/" + name[: -len("zarr.json")].rstrip("/")
            attrs = meta.get("attributes") if isinstance(meta.get("attributes"), dict) else {}
            if meta.get("node_type") == "group":
                doc.group(node)
                continue
            shape = meta.get("shape")
            if not isinstance(shape, list) or not all(isinstance(s, int) for s in shape):
                continue
            grid = meta.get("chunk_grid")
            chunks = None
            if isinstance(grid, dict) and isinstance(grid.get("configuration"), dict):
                cs = grid["configuration"].get("chunk_shape")
                if isinstance(cs, list) and all(isinstance(c, int) for c in cs):
                    chunks = cs
            codecs = meta.get("codecs")
            filters = (
                [str(c.get("name")) for c in codecs if isinstance(c, dict)]
                if isinstance(codecs, list)
                else []
            )
            dims = meta.get("dimension_names")
            hint = (
                [str(d) for d in dims]
                if isinstance(dims, list) and all(isinstance(d, str) for d in dims)
                else _dims_from_attr(attrs or {})
            )
            doc.array(
                node,
                shape,
                str(meta.get("data_type")),
                chunks=chunks,
                filters=filters,
                dims_hint=hint,
                attrs=attrs or {},
            )
            if doc.full():
                doc.truncated = True
                break


def _parquet(path: Path, doc: _Doc) -> None:
    import pyarrow.parquet as pq

    doc.group("/")
    meta = pq.read_metadata(path)
    schema = meta.schema.to_arrow_schema()
    for field in schema:
        doc.array("/" + str(field.name), [meta.num_rows], str(field.type))


def _sniff_format(path: Path, walk: Hdf5Walk | None) -> str:
    if walk is not None:
        import h5py

        with h5py.File(path, "r", locking=False) as f:
            return "netcdf" if "_NCProperties" in f.attrs else "hdf5"
    kind = sniff(path)
    return kind if kind in ("parquet", "zarr_zip", "netcdf") else "csv"


def inspect_upload(path: Path, job: InspectJob) -> UploadInspection:
    """The inspection of ``path`` (see the module docstring)."""
    limits = IngestLimits(
        max_hdf5_objects=job.max_hdf5_objects,
        max_ndim=job.max_ndim,
        max_attr_bytes=job.max_attr_bytes,
        max_uncompressed_bytes=job.max_uncompressed_bytes,
    )
    size = path.stat().st_size
    with open(path, "rb") as fh:
        hdf5 = fh.read(8).startswith(HDF5_MAGIC)
    walk = walk_hdf5(path, limits, collect_attrs=True) if hdf5 else None
    fmt = job.format or _sniff_format(path, walk)
    doc = _Doc(fmt, size, job.max_arrays)
    if walk is not None:
        _hdf5(path, doc, walk)
        for w in walk.warnings:
            doc.warn(w)
    elif sniff(path) == "netcdf":
        _netcdf3(path, doc)
    elif sniff(path) == "zarr_zip":
        _zarr(path, doc, limits)
    elif sniff(path) == "parquet":
        _parquet(path, doc)
    else:
        doc.warn("CSV uploads are listed by their columns when they are ingested")
    return doc.build()


def run_inspect(ctx: WorkerContext, job: InspectJob) -> UploadInspection:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the upload")
    name = INPUT_UPLOAD if ctx.has_input(INPUT_UPLOAD) else INPUT_DATASET
    path = ctx.download(name)
    assert path is not None
    ctx.state(RunnerPhase.VALIDATE, "listing the groups and arrays of the upload")
    inspection = inspect_upload(path, job)
    ctx.write_output(INSPECT_OUTPUT, inspection.model_dump_json(indent=2).encode())
    ctx.progress(1.0, "upload inspected", force=True)
    return inspection
