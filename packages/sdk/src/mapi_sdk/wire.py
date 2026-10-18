"""Wire formats: XrJSON, single-object zarr v3 zips, DataTree zips, stacking and Arrow IPC.

Encoding rules (plan §3.2), applied by every writer here:

* any ``pandas.MultiIndex`` is reset before persisting (and recorded so readers restore it);
* string coords and variables are zarr v3 ``vlen-utf8`` (data type ``string``);
* datetimes are ``int64`` nanoseconds with explicit ``units`` and ``calendar`` attrs;
* only the ``zstd`` and ``blosc`` compressors are written or accepted;
* :func:`stack_rows` drops empty rows and records a row mask so :func:`unstack_rows`
  restores them.

Pickle (and joblib/cloudpickle) is never used for anything. Readers treat every payload as
hostile: zip entries, zarr metadata, codecs, dtypes and sizes are checked *before* any array
is decoded.
"""

from __future__ import annotations

import io
import json
import math
import re
import stat
import zipfile
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, cast

import numpy as np
import pandas as pd
import pyarrow as pa
import xarray as xr
import zarr
from zarr.codecs import BloscCodec, ZstdCodec
from zarr.core.buffer import default_buffer_prototype
from zarr.storage import MemoryStore

from mapi_sdk._util import (
    MULTIINDEX_ATTR,
    is_string_dtype,
    json_safe,
    normalise_dataset,
    reset_multiindex,
    str_names,
)
from mapi_sdk._util import restore_multiindex as _restore_mi
from mapi_sdk.errors import PayloadLimitError, WireFormatError

__all__ = [
    "ALLOWED_COMPRESSORS",
    "ALLOWED_ZARR_DTYPES",
    "DEFAULT_LIMITS",
    "MULTIINDEX_ATTR",
    "ROWMASK_SUFFIX",
    "VAR_ORDER_ATTR",
    "WireLimits",
    "dumps_xrjson",
    "from_xrjson",
    "loads_xrjson",
    "read_arrow_ipc",
    "read_datatree_zip",
    "read_zarr_zip",
    "reset_multiindex",
    "restore_multiindex",
    "stack_rows",
    "to_arrow_table",
    "to_xrjson",
    "unstack_rows",
    "write_arrow_ipc",
    "write_datatree_zip",
    "write_zarr_zip",
    "zip_entries",
]

ROWMASK_SUFFIX = "__rowmask"
VAR_ORDER_ATTR = "_mapi_var_order"
"""Group attribute holding the data-variable order of a written dataset (read back and
removed by the readers; zarr itself does not keep member order)."""
DATETIME_UNITS = "nanoseconds since 1970-01-01"
DATETIME_CALENDAR = "proleptic_gregorian"
_STANDARD_CALENDARS = frozenset({"proleptic_gregorian", "standard", "gregorian"})

ALLOWED_COMPRESSORS: frozenset[str] = frozenset({"zstd", "blosc"})
ALLOWED_SERIALIZERS: frozenset[str] = frozenset({"bytes", "vlen-utf8"})
ALLOWED_ZARR_DTYPES: frozenset[str] = frozenset(
    {
        "bool",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float32",
        "float64",
        "string",
    }
)
_ZARR_ITEMSIZE = {
    "bool": 1,
    "int8": 1,
    "uint8": 1,
    "int16": 2,
    "uint16": 2,
    "int32": 4,
    "uint32": 4,
    "float32": 4,
    "int64": 8,
    "uint64": 8,
    "float64": 8,
    "string": 16,
}
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class WireLimits:
    """Caps applied when *reading* untrusted payloads (and to XrJSON writes)."""

    max_json_bytes: int = 5 * 1024 * 1024
    max_elements: int = 2_000_000
    max_ndim: int = 32
    max_string_chars: int = 4096
    max_variables: int = 4096
    max_name_chars: int = 128
    max_attrs_bytes: int = 64 * 1024
    max_zip_entries: int = 200_000
    max_uncompressed_bytes: int = 4 * 1024**3
    max_array_bytes: int = 2 * 1024**3
    max_store_elements: int = 256 * 1024**2
    """zarr zips: elements every array of the store may declare in total."""
    max_expansion: int = 4096
    """zarr zips: declared (decoded) bytes may exceed the archive's stored bytes by at most
    this factor (beyond a small floor), since missing chunks decode to the fill value."""
    max_decoded_bytes: int = 64 * 1024**2
    """XrJSON: bytes the decoded arrays may take (fixed-width strings cost ``4 * longest``
    per element, so one long string among many short ones cannot blow up memory)."""
    max_tree_nodes: int = 4096
    """DataTree zips: groups (the root included) the store may declare."""
    max_tree_depth: int = 32
    """DataTree zips: levels below the root a group may sit at."""


DEFAULT_LIMITS = WireLimits()


def restore_multiindex(ds: xr.Dataset) -> xr.Dataset:
    """Rebuild MultiIndexes recorded by :func:`reset_multiindex`."""
    try:
        return _restore_mi(ds)
    except (ValueError, KeyError) as exc:
        raise WireFormatError(f"cannot restore MultiIndex: {exc}") from None


# ========================================================================================= XrJSON

_XRJSON_DTYPES = frozenset(
    {
        "bool",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float32",
        "float64",
        "str",
        "datetime64[ns]",
    }
)
_XRJSON_KEYS = frozenset({"dims", "attrs", "coords", "data_vars", "encoding"})
_VAR_KEYS = frozenset({"dims", "attrs", "data", "dtype", "shape", "encoding"})


def _dtype_name(values: np.ndarray) -> str:
    if is_string_dtype(values.dtype):
        return "str"
    if values.dtype.kind == "M":
        return "datetime64[ns]"
    name = values.dtype.name
    if name not in _XRJSON_DTYPES:
        raise WireFormatError(f"dtype {name!r} cannot be sent as XrJSON")
    return name


def _encode_values(values: np.ndarray) -> Any:
    kind = values.dtype.kind
    if kind == "M":
        strs = np.datetime_as_string(values.astype("datetime64[ns]"), unit="auto")
        out = strs.astype(object)
        out[np.isnat(values)] = None
        return out.tolist()
    if kind == "f":
        if np.isinf(values).any():
            raise WireFormatError("XrJSON cannot carry infinite values")
        out = values.astype(object)
        out[np.isnan(values)] = None
        return out.tolist()
    if kind in ("U", "T", "O"):
        return np.asarray(values, dtype=object).tolist()
    return values.tolist()


def _check_attrs(attrs: Mapping[Any, Any], limits: WireLimits, where: str) -> dict[str, Any]:
    try:
        safe = json_safe(dict(attrs))
    except ValueError as exc:
        raise WireFormatError(f"{where}: attributes are not JSON-serialisable: {exc}") from None
    if len(json.dumps(safe)) > limits.max_attrs_bytes:
        raise PayloadLimitError(f"{where}: attributes exceed {limits.max_attrs_bytes} bytes")
    assert isinstance(safe, dict)
    return safe


def to_xrjson(ds: xr.Dataset, *, limits: WireLimits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Encode ``ds`` as XrJSON (the ``xr.Dataset.to_dict()`` shape plus explicit dtypes).

    NaN/NaT become ``null``; datetimes are ISO-8601 strings; MultiIndexes are reset.
    """
    ds = normalise_dataset(reset_multiindex(ds))
    n = sum(int(v.size) for v in ds.variables.values())
    if n > limits.max_elements:
        raise PayloadLimitError(f"dataset has {n} elements; XrJSON allows {limits.max_elements}")

    def enc(var: xr.Variable, name: str) -> dict[str, Any]:
        values = np.asarray(var.values)
        return {
            "dims": str_names(var.dims),
            "attrs": _check_attrs(var.attrs, limits, name),
            "dtype": _dtype_name(values),
            "data": _encode_values(values),
        }

    return {
        "dims": {str(k): int(v) for k, v in ds.sizes.items()},
        "attrs": _check_attrs(ds.attrs, limits, "dataset"),
        "coords": {str(k): enc(ds[k].variable, str(k)) for k in ds.coords},
        "data_vars": {str(k): enc(ds[k].variable, str(k)) for k in ds.data_vars},
    }


def dumps_xrjson(ds: xr.Dataset, *, limits: WireLimits = DEFAULT_LIMITS) -> str:
    """:func:`to_xrjson` serialised to compact strict JSON."""
    text = json.dumps(to_xrjson(ds, limits=limits), separators=(",", ":"), allow_nan=False)
    if len(text.encode()) > limits.max_json_bytes:
        raise PayloadLimitError(f"XrJSON payload exceeds {limits.max_json_bytes} bytes")
    return text


def _reject_constant(name: str) -> Any:
    raise WireFormatError(f"XrJSON may not contain {name}")


def loads_xrjson(text: str | bytes, *, limits: WireLimits = DEFAULT_LIMITS) -> xr.Dataset:
    """Parse strict JSON text (size-capped, no NaN/Infinity) and decode it as XrJSON."""
    size = len(text) if isinstance(text, bytes) else len(text.encode())
    if size > limits.max_json_bytes:
        raise PayloadLimitError(f"XrJSON payload exceeds {limits.max_json_bytes} bytes")
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise PayloadLimitError("XrJSON payload is nested too deeply") from None
    except json.JSONDecodeError as exc:
        raise WireFormatError(f"XrJSON payload is not valid JSON: {exc.msg}") from None
    return from_xrjson(obj, limits=limits)


class _Budget:
    def __init__(self, limits: WireLimits) -> None:
        self.limits = limits
        self.elements = 0
        self.decoded = 0

    def take(self, n: int) -> None:
        self.elements += n
        if self.elements > self.limits.max_elements:
            raise PayloadLimitError(f"payload has more than {self.limits.max_elements} elements")

    def take_bytes(self, n: int, name: str) -> None:
        self.decoded += n
        if self.decoded > self.limits.max_decoded_bytes:
            raise PayloadLimitError(
                f"{name}: the payload would decode to more than "
                f"{self.limits.max_decoded_bytes} bytes"
            )


def _shape_and_leaves(
    data: Any, ndim: int, budget: _Budget, name: str
) -> tuple[list[int | None], list[Any]]:
    """Validate a rectangular nested list of depth ``ndim``; return its shape and leaves.

    Axes below an empty level have unknown size (``None``); callers resolve them from the
    other variables or the declared dims.
    """
    shape: list[int | None] = []
    level: list[Any] = [data]
    for axis in range(ndim):
        if not level:
            shape.append(None)
            continue
        size: int | None = None
        nxt: list[Any] = []
        for item in level:
            if not isinstance(item, list):
                raise WireFormatError(f"{name}: data is not nested {ndim} levels deep")
            if size is None:
                size = len(item)
            elif len(item) != size:
                raise WireFormatError(f"{name}: data is ragged along axis {axis}")
            if len(nxt) + len(item) > budget.limits.max_elements:
                raise PayloadLimitError(f"{name}: too many elements")
            nxt.extend(item)
        shape.append(size)
        level = nxt
    budget.take(len(level))
    for leaf in level:
        if isinstance(leaf, list | dict):
            raise WireFormatError(f"{name}: data is nested deeper than its {ndim} dims")
        if isinstance(leaf, str) and len(leaf) > budget.limits.max_string_chars:
            raise PayloadLimitError(
                f"{name}: a string exceeds {budget.limits.max_string_chars} characters"
            )
    return shape, level


def _infer_dtype(leaves: Sequence[Any], name: str) -> str:
    if not leaves:
        return "float64"
    if all(isinstance(v, bool) for v in leaves):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in leaves):
        return "int64"
    if all(v is None or (isinstance(v, int | float) and not isinstance(v, bool)) for v in leaves):
        return "float64"
    if all(isinstance(v, str) for v in leaves):
        return "str"
    raise WireFormatError(f"{name}: values have mixed or unsupported types; give an explicit dtype")


_ISO_DATE = re.compile(
    r"^[0-9]{4}-[0-9]{2}(-[0-9]{2}([T ][0-9]{2}(:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,9})?)?)?)?)?$"
)


def _decoded_size(entry: _Entry) -> int:
    """Bytes the decoded array takes (``<U{longest}`` for strings: 4 bytes per char)."""
    n = len(entry.leaves)
    if entry.dtype == "str":
        longest = max((len(v) for v in entry.leaves if isinstance(v, str)), default=1)
        return n * max(longest, 1) * 4
    return n * 8


def _decode_values(leaves: list[Any], shape: list[int], dtype: str, name: str) -> np.ndarray:
    try:
        match dtype:
            case "str":
                if not all(isinstance(v, str) for v in leaves):
                    raise WireFormatError(f"{name}: dtype str needs string values (no nulls)")
                arr = np.array(leaves, dtype=str) if leaves else np.array([], dtype="<U1")
            case "datetime64[ns]":
                if not all(
                    v is None or (isinstance(v, str) and _ISO_DATE.match(v)) for v in leaves
                ):
                    # numpy also parses 'now'/'today': payloads must not depend on the clock
                    raise WireFormatError(f"{name}: datetimes must be ISO-8601 strings or null")
                arr = np.array(
                    [np.datetime64("NaT") if v is None else np.datetime64(v, "ns") for v in leaves],
                    dtype="datetime64[ns]",
                )
            case "bool":
                if not all(isinstance(v, bool) for v in leaves):
                    raise WireFormatError(f"{name}: dtype bool needs true/false values")
                arr = np.array(leaves, dtype=bool)
            case "float32" | "float64":
                if not all(
                    v is None or (isinstance(v, int | float) and not isinstance(v, bool))
                    for v in leaves
                ):
                    raise WireFormatError(f"{name}: dtype {dtype} needs numbers or null")
                arr = np.array([math.nan if v is None else v for v in leaves], dtype=dtype)
            case _:
                if not all(isinstance(v, int) and not isinstance(v, bool) for v in leaves):
                    raise WireFormatError(f"{name}: dtype {dtype} needs integers (no nulls)")
                arr = np.array(leaves, dtype=dtype)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, WireFormatError):
            raise
        raise WireFormatError(f"{name}: values do not fit dtype {dtype}") from None
    return arr.reshape(shape)


def _check_name(name: Any, limits: WireLimits, what: str) -> str:
    if not isinstance(name, str) or not name or len(name) > limits.max_name_chars:
        raise WireFormatError(f"invalid {what} name {str(name)[:64]!r}")
    return name


@dataclass(slots=True)
class _Entry:
    dims: tuple[str, ...]
    shape: list[int | None]
    leaves: list[Any]
    dtype: str
    attrs: dict[str, Any]


def _decode_entry(name: str, entry: Any, limits: WireLimits, budget: _Budget) -> _Entry:
    if not isinstance(entry, Mapping):
        raise WireFormatError(f"{name}: variable entry must be an object")
    unknown = set(entry) - _VAR_KEYS
    if unknown:
        raise WireFormatError(f"{name}: unknown keys {sorted(unknown)}")
    dims_raw = entry.get("dims", ())
    if isinstance(dims_raw, str):
        dims_raw = [dims_raw]
    if not isinstance(dims_raw, list | tuple):
        raise WireFormatError(f"{name}: dims must be a list of names")
    if len(dims_raw) > limits.max_ndim:
        raise PayloadLimitError(f"{name}: more than {limits.max_ndim} dims")
    dims = tuple(_check_name(d, limits, "dim") for d in dims_raw)
    if len(set(dims)) != len(dims):
        raise WireFormatError(f"{name}: repeated dims {list(dims)}")
    if "data" not in entry:
        raise WireFormatError(f"{name}: missing data")
    shape, leaves = _shape_and_leaves(entry["data"], len(dims), budget, name)
    dtype = entry.get("dtype")
    if dtype is None or dtype == "object":
        dtype = _infer_dtype(leaves, name)
    elif dtype not in _XRJSON_DTYPES:
        raise WireFormatError(f"{name}: unsupported dtype {str(dtype)[:32]!r}")
    attrs = _check_attrs(entry.get("attrs") or {}, limits, name)
    return _Entry(dims, shape, leaves, str(dtype), attrs)


def from_xrjson(obj: Mapping[str, Any], *, limits: WireLimits = DEFAULT_LIMITS) -> xr.Dataset:
    """Decode an XrJSON object (as produced by :func:`to_xrjson` or ``to_dict()``).

    Every element is counted against ``limits.max_elements`` before any array is built;
    shapes must be rectangular and consistent with ``dims``.
    """
    if not isinstance(obj, Mapping):
        raise WireFormatError("XrJSON payload must be an object")
    unknown = set(obj) - _XRJSON_KEYS
    if unknown:
        raise WireFormatError(f"unknown XrJSON keys {sorted(unknown)}")
    coords_in = obj.get("coords") or {}
    vars_in = obj.get("data_vars") or {}
    if not isinstance(coords_in, Mapping) or not isinstance(vars_in, Mapping):
        raise WireFormatError("coords and data_vars must be objects")
    if len(coords_in) + len(vars_in) > limits.max_variables:
        raise PayloadLimitError(f"more than {limits.max_variables} variables")
    budget = _Budget(limits)
    entries: dict[str, tuple[bool, _Entry]] = {}
    sizes: dict[str, int] = {}
    for is_coord, section in ((True, coords_in), (False, vars_in)):
        for raw_name, raw_entry in section.items():
            name = _check_name(raw_name, limits, "variable")
            if name in entries:
                raise WireFormatError(f"variable {name!r} appears twice")
            entry = _decode_entry(name, raw_entry, limits, budget)
            for d, n in zip(entry.dims, entry.shape, strict=True):
                if n is not None and sizes.setdefault(d, n) != n:
                    raise WireFormatError(f"dim {d!r} has inconsistent sizes ({sizes[d]} vs {n})")
            entries[name] = (is_coord, entry)
    declared = obj.get("dims")
    if declared is not None:
        if not isinstance(declared, Mapping):
            raise WireFormatError("dims must be an object of name -> size")
        for d, n in declared.items():
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise WireFormatError(f"dim {str(d)[:64]!r} has an invalid declared size")
            if n > limits.max_elements:
                raise PayloadLimitError(
                    f"dim {str(d)[:64]!r} declares {n} entries; at most {limits.max_elements}"
                )
            if d in sizes and sizes[d] != n:
                raise WireFormatError(f"dim {d!r} declared with size {n} but data has {sizes[d]}")
            sizes.setdefault(str(d), n)
    coords: dict[str, xr.Variable] = {}
    data_vars: dict[str, xr.Variable] = {}
    for name, (is_coord, entry) in entries.items():
        shape = [
            sizes.get(d, 0) if n is None else n
            for d, n in zip(entry.dims, entry.shape, strict=True)
        ]
        if math.prod(shape) != len(entry.leaves):
            raise WireFormatError(f"{name}: data does not match the sizes of its dims")
        budget.take_bytes(_decoded_size(entry), name)
        values = _decode_values(entry.leaves, shape, entry.dtype, name)
        (coords if is_coord else data_vars)[name] = xr.Variable(entry.dims, values, entry.attrs)
    attrs = _check_attrs(obj.get("attrs") or {}, limits, "dataset")
    try:
        ds = xr.Dataset(data_vars=data_vars, coords=coords, attrs=attrs)
    except (ValueError, TypeError) as exc:
        raise WireFormatError(f"payload does not form a valid dataset: {exc}") from None
    return restore_multiindex(normalise_dataset(ds))


# ===================================================================================== zarr zip


def _compressor(name: str) -> Any:
    if name == "zstd":
        return ZstdCodec(level=3)
    if name == "blosc":
        return BloscCodec(cname="zstd", clevel=5, shuffle="shuffle")
    raise WireFormatError(f"compressor {name!r} is not allowed (use zstd or blosc)")


def _prepare_dataset(
    ds: xr.Dataset, compressor: str
) -> tuple[xr.Dataset, dict[str, dict[str, Any]]]:
    """Apply the encoding rules; return the dataset to write and its zarr encoding."""
    ds = normalise_dataset(reset_multiindex(ds)).drop_encoding()
    enc: dict[str, dict[str, Any]] = {}
    replacements: dict[Hashable, xr.Variable] = {}
    codec = _compressor(compressor)
    for name, var in ds.variables.items():
        values = np.asarray(var.values)
        kind = values.dtype.kind
        e: dict[str, Any] = {"compressors": [codec]}
        attrs = json_safe_attrs(var.attrs, str(name))
        if kind == "M":
            e.update(units=DATETIME_UNITS, calendar=DATETIME_CALENDAR, dtype="int64")
        elif kind == "U":
            values = values.astype(object)
        elif kind in ("O", "c", "m", "V", "S"):
            raise WireFormatError(
                f"variable {str(name)!r} has dtype {values.dtype} which cannot be persisted"
            )
        if values is not var.values or attrs != var.attrs:
            replacements[name] = xr.Variable(var.dims, values, attrs)
        enc[str(name)] = e
    if replacements:
        coord_names = set(ds.coords)
        cr = {k: v for k, v in replacements.items() if k in coord_names}
        vr = {k: v for k, v in replacements.items() if k not in coord_names}
        if cr:
            ds = ds.assign_coords(cr)
        if vr:
            ds = ds.assign(vr)
    ds.attrs = json_safe_attrs(ds.attrs, "dataset")
    if VAR_ORDER_ATTR in ds.attrs:
        raise WireFormatError(f"the attribute {VAR_ORDER_ATTR!r} is reserved")
    # zarr lists group members in no particular order: record the variable order so readers
    # (and "first variable" defaults in plot recipes) see the dataset as it was written
    ds.attrs[VAR_ORDER_ATTR] = [str(n) for n in ds.data_vars]
    return ds, enc


def json_safe_attrs(attrs: Mapping[Any, Any], where: str) -> dict[str, Any]:
    """Attributes as JSON-safe values (raises :class:`WireFormatError` otherwise)."""
    try:
        safe = json_safe(dict(attrs))
    except ValueError as exc:
        raise WireFormatError(f"{where}: attributes are not JSON-serialisable: {exc}") from None
    assert isinstance(safe, dict)
    return safe


def _store_to_zip(store: Mapping[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for key in sorted(store):
            info = zipfile.ZipInfo(key, date_time=_ZIP_DATE)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, store[key].to_bytes())
    return buf.getvalue()


def _emit(data: bytes, dest: str | Path | IO[bytes] | None) -> bytes | None:
    if dest is None:
        return data
    if isinstance(dest, str | Path):
        Path(dest).write_bytes(data)
    else:
        dest.write(data)
    return None


def write_zarr_zip(
    ds: xr.Dataset,
    dest: str | Path | IO[bytes] | None = None,
    *,
    compressor: Literal["zstd", "blosc"] = "zstd",
) -> bytes | None:
    """Write ``ds`` as a single-object zarr v3 zip (deterministic bytes, STORED entries).

    Returns the bytes when ``dest`` is ``None``.
    """
    prepared, enc = _prepare_dataset(ds, compressor)
    store_dict: dict[str, Any] = {}
    prepared.to_zarr(
        MemoryStore(store_dict), zarr_format=3, consolidated=False, encoding=enc, mode="w"
    )
    return _emit(_store_to_zip(store_dict), dest)


def write_datatree_zip(
    tree: xr.DataTree,
    dest: str | Path | IO[bytes] | None = None,
    *,
    compressor: Literal["zstd", "blosc"] = "zstd",
) -> bytes | None:
    """Write a ``DataTree`` (every node encoded as by :func:`write_zarr_zip`)."""
    nodes: dict[str, xr.Dataset] = {}
    encoding: dict[str, dict[str, dict[str, Any]]] = {}
    for node in tree.subtree:
        prepared, enc = _prepare_dataset(node.to_dataset(inherit=False), compressor)
        nodes[node.path] = prepared
        encoding[node.path] = enc
    try:
        rebuilt = xr.DataTree.from_dict(nodes)
    except (ValueError, KeyError) as exc:
        raise WireFormatError(f"tree cannot be persisted: {exc}") from None
    store_dict: dict[str, Any] = {}
    rebuilt.to_zarr(
        MemoryStore(store_dict), zarr_format=3, consolidated=False, encoding=encoding, mode="w"
    )
    return _emit(_store_to_zip(store_dict), dest)


def _bad_name(name: str) -> bool:
    parts = name.split("/")
    return (
        name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or any(p in ("..", ".") for p in parts)
    )


def zip_entries(
    src: str | Path | bytes | IO[bytes], *, limits: WireLimits = DEFAULT_LIMITS
) -> dict[str, bytes]:
    """Read and sanity-check every entry of a zip (no traversal, links, dups or bombs)."""
    source: Any = io.BytesIO(src) if isinstance(src, bytes) else src
    try:
        zf = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WireFormatError(f"not a readable zip archive: {exc}") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_zip_entries:
            raise PayloadLimitError(f"zip has more than {limits.max_zip_entries} entries")
        out: dict[str, bytes] = {}
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if _bad_name(name):
                raise WireFormatError(f"zip entry has an unsafe name: {name[:200]!r}")
            if name in out:
                raise WireFormatError(f"zip entry appears twice: {name[:200]!r}")
            if info.flag_bits & 0x1:
                raise WireFormatError("encrypted zip entries are not allowed")
            ftype = stat.S_IFMT(info.external_attr >> 16)
            if ftype and ftype != stat.S_IFREG:
                raise WireFormatError(f"zip entry is not a regular file: {name[:200]!r}")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise WireFormatError(f"zip entry uses an unsupported compression: {name[:200]!r}")
            total += info.file_size
            if total > limits.max_uncompressed_bytes:
                raise PayloadLimitError("zip expands beyond the allowed size")
            with zf.open(info) as fh:
                data = fh.read(info.file_size + 1)
            if len(data) != info.file_size:
                raise WireFormatError(f"zip entry size mismatch: {name[:200]!r}")
            out[name] = data
    return out


_EXPANSION_FLOOR = 256 * 1024**2


def _check_zarr_metadata(entries: Mapping[str, bytes], limits: WireLimits) -> list[str]:
    """Validate every ``zarr.json`` before any chunk is decoded; return the group paths
    (``/`` for the root, ``/a/b`` for nested groups).

    The declared shapes are what decoding allocates (a missing chunk becomes its fill
    value), so they are budgeted as a whole: element count (``max_store_elements``),
    decoded bytes (``max_uncompressed_bytes``) and expansion over the bytes the archive
    actually stores (``max_expansion``).
    """
    total_elements = 0
    total_bytes = 0
    groups: list[str] = []
    for name, raw in entries.items():
        base = name.rsplit("/", 1)[-1]
        if base in (".zarray", ".zgroup", ".zattrs", ".zmetadata"):
            raise WireFormatError("only zarr v3 stores are accepted")
        if base != "zarr.json":
            continue
        try:
            meta = json.loads(raw, parse_constant=_reject_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise WireFormatError(f"invalid zarr metadata in {name[:200]!r}") from None
        if not isinstance(meta, dict) or meta.get("zarr_format") != 3:
            raise WireFormatError(f"{name[:200]!r} is not zarr v3 metadata")
        if meta.get("node_type") == "group":
            groups.append("/" + name[: -len("zarr.json")].rstrip("/"))
            continue
        if meta.get("node_type") != "array":
            raise WireFormatError(f"{name[:200]!r} has an unknown node type")
        dtype = meta.get("data_type")
        if not isinstance(dtype, str) or dtype not in ALLOWED_ZARR_DTYPES:
            raise WireFormatError(f"{name[:200]!r}: data type {str(dtype)[:40]!r} is not allowed")
        if meta.get("storage_transformers"):
            raise WireFormatError(f"{name[:200]!r}: storage transformers are not allowed")
        grid = meta.get("chunk_grid", {})
        if not isinstance(grid, dict) or grid.get("name") != "regular":
            raise WireFormatError(f"{name[:200]!r}: only regular chunk grids are allowed")
        codecs = meta.get("codecs")
        if not isinstance(codecs, list) or not codecs:
            raise WireFormatError(f"{name[:200]!r}: missing codecs")
        names = [c.get("name") if isinstance(c, dict) else None for c in codecs]
        if names[0] not in ALLOWED_SERIALIZERS:
            raise WireFormatError(f"{name[:200]!r}: serializer {names[0]!r} is not allowed")
        for c in names[1:]:
            if c not in ALLOWED_COMPRESSORS:
                raise WireFormatError(
                    f"{name[:200]!r}: codec {str(c)[:40]!r} is not allowed (zstd/blosc only)"
                )
        shape = meta.get("shape")
        if not isinstance(shape, list) or not all(isinstance(s, int) and s >= 0 for s in shape):
            raise WireFormatError(f"{name[:200]!r}: invalid shape")
        if len(shape) > limits.max_ndim:
            raise PayloadLimitError(f"{name[:200]!r}: too many dims")
        n = math.prod(shape)
        total_elements += n
        total_bytes += n * _ZARR_ITEMSIZE[dtype]
        if n * _ZARR_ITEMSIZE[dtype] > limits.max_array_bytes:
            raise PayloadLimitError(f"{name[:200]!r}: array exceeds the size limit")
        attrs = meta.get("attributes") or {}
        units = attrs.get("units") if isinstance(attrs, dict) else None
        if isinstance(units, str) and " since " in units:
            cal = attrs.get("calendar", "standard")
            if cal not in _STANDARD_CALENDARS:
                raise WireFormatError(f"{name[:200]!r}: calendar {str(cal)[:40]!r} unsupported")
    if total_elements > limits.max_store_elements:
        raise PayloadLimitError(
            f"store declares {total_elements} elements; at most {limits.max_store_elements}"
        )
    if total_bytes > limits.max_uncompressed_bytes:
        raise PayloadLimitError("store declares more data than allowed")
    stored = sum(len(v) for v in entries.values())
    if total_bytes > max(limits.max_expansion * stored, _EXPANSION_FLOOR):
        raise PayloadLimitError(
            f"store declares {total_bytes} bytes of data but the archive holds only {stored}"
        )
    return groups


def _check_tree_groups(groups: Sequence[str], limits: WireLimits, node_pattern: str | None) -> None:
    """Node count, depth and (optionally) path syntax of a DataTree store, before decoding."""
    if len(groups) > limits.max_tree_nodes:
        raise PayloadLimitError(
            f"tree has {len(groups)} nodes; at most {limits.max_tree_nodes} are allowed"
        )
    for path in groups:
        depth = 0 if path == "/" else path.count("/")
        if depth > limits.max_tree_depth:
            raise PayloadLimitError(
                f"tree node {path[:200]!r} is deeper than {limits.max_tree_depth} levels"
            )
        if node_pattern is not None and not re.fullmatch(node_pattern, path):
            raise WireFormatError(f"tree node {path[:200]!r} is not a valid node path")


def _check_strings(ds: xr.Dataset, limits: WireLimits, where: str) -> None:
    """vlen-utf8 lengths are not in the metadata: enforce ``max_string_chars`` after decode."""
    for name, var in ds.variables.items():
        dt = var.dtype
        if dt.kind == "U" and dt.itemsize // 4 > limits.max_string_chars:
            label = (where + str(name))[:200]
            raise PayloadLimitError(
                f"{label}: a string exceeds {limits.max_string_chars} characters"
            )


def _memory_store(entries: Mapping[str, bytes]) -> MemoryStore:
    proto = default_buffer_prototype()
    return MemoryStore({k: proto.buffer.from_bytes(v) for k, v in entries.items()}, read_only=True)


def _restore_order(ds: xr.Dataset) -> xr.Dataset:
    """Data variables in their written order (``VAR_ORDER_ATTR``), else sorted by name."""
    attrs = dict(ds.attrs)
    order = attrs.pop(VAR_ORDER_ATTR, None)
    names = [str(n) for n in ds.data_vars]
    if isinstance(order, list) and sorted(map(str, order)) == sorted(names):
        want = [str(n) for n in order]
    else:
        want = sorted(names)
    out = ds.drop_vars(names).assign({n: ds.variables[n] for n in want})
    out.attrs = attrs
    return out


def _decode(ds: xr.Dataset) -> xr.Dataset:
    return restore_multiindex(normalise_dataset(_restore_order(ds.drop_encoding())))


def read_zarr_zip(
    src: str | Path | bytes | IO[bytes], *, limits: WireLimits = DEFAULT_LIMITS
) -> xr.Dataset:
    """Read a single-dataset zarr v3 zip after validating entries, codecs and dtypes."""
    entries = zip_entries(src, limits=limits)
    _check_zarr_metadata(entries, limits)
    store = _memory_store(entries)
    try:
        ds = xr.open_zarr(
            store, zarr_format=3, consolidated=False, chunks=None, decode_timedelta=False
        ).load()
    except (ValueError, KeyError, TypeError, OSError, zarr.errors.BaseZarrError) as exc:
        raise WireFormatError(f"zarr store could not be decoded: {exc}") from None
    out = _decode(ds)
    _check_strings(out, limits, "")
    return out


def read_datatree_zip(
    src: str | Path | bytes | IO[bytes],
    *,
    limits: WireLimits = DEFAULT_LIMITS,
    node_pattern: str | None = None,
) -> xr.DataTree:
    """Read a ``DataTree`` written by :func:`write_datatree_zip` (same checks).

    Every node gets the checks of :func:`read_zarr_zip`; the store as a whole is capped at
    ``limits.max_tree_nodes`` groups no deeper than ``limits.max_tree_depth``, and when
    ``node_pattern`` is given every group path (``/``, ``/a/b``) must match it - all checked
    before any array is decoded.
    """
    entries = zip_entries(src, limits=limits)
    groups = _check_zarr_metadata(entries, limits)
    _check_tree_groups(groups, limits, node_pattern)
    store = _memory_store(entries)
    try:
        tree = xr.open_datatree(
            cast(Any, store),
            engine="zarr",
            zarr_format=3,
            consolidated=False,
            chunks=None,
            decode_timedelta=False,
        ).load()
    except (ValueError, KeyError, TypeError, OSError, zarr.errors.BaseZarrError) as exc:
        raise WireFormatError(f"zarr tree could not be decoded: {exc}") from None
    nodes = {node.path: _decode(node.to_dataset(inherit=False)) for node in tree.subtree}
    for path, node_ds in nodes.items():
        _check_strings(node_ds, limits, path.rstrip("/") + "/")
    return xr.DataTree.from_dict(nodes)


# ================================================================================ stack/unstack


def stack_rows(
    ds: xr.Dataset,
    dims: Sequence[str],
    new_dim: str = "row",
    *,
    drop: Literal["all", "any", "none"] = "all",
    mask_var: str | None = None,
) -> xr.Dataset:
    """Stack ``dims`` into one ``new_dim`` and drop empty rows, recording a row mask.

    A row is dropped when all (``drop="all"``) or any (``"any"``) of the variables that use
    *every* stacked dim are missing there (lower-rank variables, which are broadcast, only
    decide when no variable is full-rank), or where ``mask_var`` is false. The result has no
    MultiIndex: the component coords are plain coords along ``new_dim`` and the boolean
    ``<new_dim>__rowmask`` variable (over the original dims, with their full coords) says
    which rows were kept, so :func:`unstack_rows` can restore the dropped rows as missing.
    """
    dims = list(dims)
    if len(dims) < 1:
        raise ValueError("stack_rows needs at least one dim")
    missing = [d for d in dims if d not in ds.dims]
    if missing:
        raise ValueError(f"cannot stack unknown dims {missing}")
    if new_dim in ds.dims or new_dim in ds.variables:
        raise ValueError(f"{new_dim!r} already exists")
    users = [v for v in ds.data_vars if set(dims) & set(ds[v].dims) and v != mask_var]
    order = {str(v): str_names(ds[v].dims) for v in users}
    full = xr.DataArray(
        np.ones([ds.sizes[d] for d in dims], dtype=bool), coords={d: ds[d] for d in dims}, dims=dims
    )
    keep = full
    if drop != "none" and users:
        full_rank = [v for v in users if set(dims) <= set(ds[v].dims)]
        bad: xr.DataArray | None = None
        for v in full_rank or users:
            other = [d for d in ds[v].dims if d not in dims]
            isnull = ds[v].isnull()
            n = (isnull.all(other) if other else isnull).broadcast_like(full).transpose(*dims)
            bad = n if bad is None else (bad & n if drop == "all" else bad | n)
        assert bad is not None
        keep = keep & ~bad
    if mask_var is not None:
        mv = ds[mask_var]
        other = [d for d in mv.dims if d not in dims]
        keep = keep & (mv.all(other) if other else mv).astype(bool).broadcast_like(full)
    stacked = ds.drop_vars([mask_var]) if mask_var is not None else ds
    flat_keep = keep.transpose(*dims).values.reshape(-1)
    if len(dims) == 1:
        stacked = stacked.rename_dims({dims[0]: new_dim}).drop_indexes(dims[0])
    else:
        stacked = stacked.stack({new_dim: dims})
        stacked = stacked.reset_index(new_dim).drop_vars(new_dim, errors="ignore")
    stacked = stacked.isel({new_dim: np.flatnonzero(flat_keep)})
    mask_name = f"{new_dim}{ROWMASK_SUFFIX}"
    mask_dims = {d: f"{new_dim}__{d}" for d in dims}
    rowmask = keep.transpose(*dims).rename(mask_dims)
    rowmask.attrs = {
        "stacked_dims": dims,
        "var_dims": order,
        "var_dtypes": {str(v): str(ds[v].dtype) for v in users},
    }
    return stacked.assign({mask_name: rowmask})


def unstack_rows(ds: xr.Dataset, new_dim: str = "row") -> xr.Dataset:
    """Inverse of :func:`stack_rows`: dropped rows come back as missing values."""
    mask_name = f"{new_dim}{ROWMASK_SUFFIX}"
    if mask_name not in ds:
        raise ValueError(f"{mask_name!r} not found; was this dataset produced by stack_rows?")
    mask = ds[mask_name]
    dims = [str(d) for d in mask.attrs.get("stacked_dims", [])]
    if len(dims) != mask.ndim:
        raise ValueError(f"{mask_name!r} is missing its stacked_dims attribute")
    order: Mapping[str, list[str]] = mask.attrs.get("var_dims", {})
    dtypes: Mapping[str, str] = mask.attrs.get("var_dtypes", {})
    levels = {d: mask[f"{new_dim}__{d}"].values for d in dims}
    body = ds.drop_vars([mask_name, *(f"{new_dim}__{d}" for d in dims)])
    if len(dims) == 1:
        body = body.swap_dims({new_dim: dims[0]})
    else:
        body = body.set_index(cast(Any, {new_dim: dims})).unstack(new_dim)
    body = body.reindex(levels)
    out_vars: dict[Hashable, xr.DataArray] = {}
    for v in body.data_vars:
        da = body[v]
        want = order.get(str(v))
        if want:
            extra = [d for d in da.dims if d not in want]
            if extra:
                da = _first_valid(da, [str(d) for d in extra])
            da = da.transpose(*want)
        orig = dtypes.get(str(v))
        if orig is not None and da.dtype != np.dtype(orig) and not bool(da.isnull().any()):
            da = da.astype(orig)
        out_vars[v] = da
    out = body.assign(out_vars)
    return out.drop_vars([d for d in dims if d not in out.dims and d in out.coords])


def _first_valid(da: xr.DataArray, extra: list[str]) -> xr.DataArray:
    """Collapse dims a variable was broadcast over by stacking (first non-missing value)."""
    keep = [d for d in da.dims if d not in extra]
    moved = da.transpose(*keep, *extra)
    values = np.asarray(moved.values)
    flat = values.reshape(*values.shape[: len(keep)], -1)
    valid = ~pd.isna(flat)
    first = np.where(valid.any(axis=-1), valid.argmax(axis=-1), 0)
    picked = np.take_along_axis(flat, first[..., None], axis=-1)[..., 0]
    coords = {d: da[d] for d in keep if d in da.coords}
    return xr.DataArray(picked, dims=keep, coords=coords, attrs=da.attrs, name=da.name)


# ========================================================================================= Arrow


def to_arrow_table(obj: xr.Dataset | xr.DataArray, *, max_rows: int = 2_000_000) -> pa.Table:
    """Tidy (long) Arrow table: one column per dim coordinate plus one per variable.

    Variable attrs (units, ...) are attached as JSON schema metadata under ``mapi.attrs``.
    """
    ds = obj.to_dataset(name=obj.name or "value") if isinstance(obj, xr.DataArray) else obj
    ds = normalise_dataset(reset_multiindex(ds))
    rows = math.prod(int(ds.sizes[d]) for d in ds.dims) if ds.dims else 1
    if rows > max_rows:
        raise PayloadLimitError(f"view would have {rows} rows; at most {max_rows} allowed")
    df = ds.to_dataframe() if ds.dims else pd.DataFrame({k: [ds[k].item()] for k in ds.data_vars})
    df = df.reset_index() if ds.dims else df
    table = pa.Table.from_pandas(df, preserve_index=False)
    meta = {str(k): json_safe_attrs(ds[k].attrs, str(k)) for k in ds.variables if ds[k].attrs}
    if meta or ds.attrs:
        payload = json.dumps({"vars": meta, "dataset": json_safe_attrs(ds.attrs, "dataset")})
        table = table.replace_schema_metadata(
            {**(table.schema.metadata or {}), b"mapi.attrs": payload.encode()}
        )
    return table


def write_arrow_ipc(table: pa.Table, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Serialise ``table`` in the Arrow IPC file format (size-capped)."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    data: bytes = sink.getvalue().to_pybytes()
    if len(data) > max_bytes:
        raise PayloadLimitError(f"Arrow view exceeds {max_bytes} bytes")
    return data


def read_arrow_ipc(data: bytes, *, max_bytes: int = 64 * 1024 * 1024) -> pa.Table:
    """Read an Arrow IPC file (size-capped)."""
    if len(data) > max_bytes:
        raise PayloadLimitError(f"Arrow view exceeds {max_bytes} bytes")
    try:
        table: pa.Table = pa.ipc.open_file(pa.BufferReader(data)).read_all()
    except (pa.ArrowInvalid, OSError) as exc:
        raise WireFormatError(f"not a readable Arrow IPC file: {exc}") from None
    return table
