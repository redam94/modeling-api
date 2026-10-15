"""Small shared helpers: JSON-safe conversion and dtype normalisation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Hashable, Iterable, Mapping
from typing import Any, cast

import numpy as np
import pandas as pd
import xarray as xr

__all__ = [
    "MULTIINDEX_ATTR",
    "as_list",
    "dtype_family",
    "is_string_dtype",
    "json_hash",
    "json_safe",
    "normalise_dataset",
    "normalise_variable_values",
    "py_scalar",
    "reset_multiindex",
    "restore_multiindex",
    "str_names",
]

MULTIINDEX_ATTR = "_mapi_multiindex"


def py_scalar(value: Any) -> Any:
    """Convert a numpy/pandas scalar into the equivalent plain Python value.

    Datetimes become ISO-8601 strings (no timezone), ``NaN``/``NaT`` become ``None``.
    """
    if isinstance(value, np.generic):
        if isinstance(value, np.datetime64):
            if np.isnat(value):
                return None
            return pd.Timestamp(value).isoformat()
        if isinstance(value, np.floating):
            f = float(value)
            return None if math.isnan(f) else f
        return value.item()
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Recursively convert ``value`` to JSON-compatible Python objects.

    Raises ``ValueError`` for non-finite floats and unsupported types, so nothing that
    cannot round-trip through strict JSON is silently accepted.
    """
    if _depth > 64:
        raise ValueError("value is nested too deeply to serialise")
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be serialised to JSON")
        return value
    if isinstance(value, np.ndarray):
        return [json_safe(v, _depth=_depth + 1) for v in value.tolist()]
    if isinstance(value, np.generic | pd.Timestamp):
        return json_safe(py_scalar(value), _depth=_depth + 1)
    if isinstance(value, Mapping):
        return {str(k): json_safe(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v, _depth=_depth + 1) for v in value]
    raise ValueError(f"cannot serialise a value of type {type(value).__name__} to JSON")


def json_hash(value: Any) -> str:
    """``sha256:<hex>`` of the canonical (sorted, compact) JSON form of ``value``."""
    text = json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def is_string_dtype(dtype: Any) -> bool:
    """True for numpy unicode, numpy ``StringDType`` and pandas string dtypes."""
    if isinstance(dtype, np.dtype):
        return dtype.kind in ("U", "T")
    return str(dtype) in ("str", "string")


def dtype_family(dtype: Any) -> str:
    """Coarse dtype family: ``float``, ``int``, ``bool``, ``str``, ``datetime`` or ``other``."""
    if is_string_dtype(dtype):
        return "str"
    dt = np.dtype(dtype)
    match dt.kind:
        case "f":
            return "float"
        case "i" | "u":
            return "int"
        case "b":
            return "bool"
        case "M":
            return "datetime"
        case "O":
            return "object"
    return "other"


def _strings_to_unicode(values: np.ndarray) -> np.ndarray:
    if values.dtype.kind == "U":
        return values
    if values.size == 0:
        return values.astype("<U1")
    return np.asarray(values.astype(object).tolist(), dtype=str).reshape(values.shape)


def normalise_variable_values(values: np.ndarray) -> np.ndarray:
    """Canonical in-memory dtype: datetimes as ``datetime64[ns]``, strings as ``<U``."""
    if values.dtype.kind == "M" and values.dtype != np.dtype("datetime64[ns]"):
        return values.astype("datetime64[ns]")
    if values.dtype.kind == "T":
        return _strings_to_unicode(values)
    if values.dtype.kind == "O" and values.size and all(isinstance(v, str) for v in values.flat):
        return _strings_to_unicode(values)
    return values


def reset_multiindex(ds: xr.Dataset) -> xr.Dataset:
    """Reset every MultiIndex; record the level names in ``attrs[MULTIINDEX_ATTR]``."""
    mi: dict[str, list[str]] = {}
    for dim, idx in ds.indexes.items():
        if isinstance(idx, pd.MultiIndex) and dim in ds.dims:
            mi[str(dim)] = [str(n) for n in idx.names]
    if not mi:
        return ds
    out = ds.reset_index(list(mi))
    out = out.drop_vars([d for d in mi if d in out.variables])
    out.attrs = {**out.attrs, MULTIINDEX_ATTR: mi}
    return out


def restore_multiindex(ds: xr.Dataset) -> xr.Dataset:
    """Inverse of :func:`reset_multiindex` (no-op without the recording attribute)."""
    mi = ds.attrs.get(MULTIINDEX_ATTR)
    if not mi:
        return ds
    if not isinstance(mi, Mapping) or not all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in mi.values()
    ):
        raise ValueError(f"malformed {MULTIINDEX_ATTR} attribute")
    out = ds.set_index(cast(Any, {str(k): list(v) for k, v in mi.items()}))
    out.attrs = {k: v for k, v in out.attrs.items() if k != MULTIINDEX_ATTR}
    return out


def normalise_dataset(ds: xr.Dataset) -> xr.Dataset:
    """Return ``ds`` with canonical dtypes for every variable and coordinate.

    Datetime coords/vars become ``datetime64[ns]`` and string ones become numpy ``<U``
    (whatever the source: object arrays, ``StringDType`` or pandas string arrays).
    MultiIndexes are preserved.
    """
    if any(isinstance(ds.indexes[d], pd.MultiIndex) for d in ds.indexes if d in ds.dims):
        if MULTIINDEX_ATTR in ds.attrs:
            raise ValueError(f"{MULTIINDEX_ATTR} is a reserved attribute")
        return restore_multiindex(normalise_dataset(reset_multiindex(ds)))
    changes: dict[Hashable, Any] = {}
    for name, var in ds.variables.items():
        values = np.asarray(var.values)
        new = normalise_variable_values(values)
        if new is not values:
            changes[name] = (var.dims, new, var.attrs)
    if not changes:
        return ds
    coord_changes = {k: v for k, v in changes.items() if k in ds.coords}
    var_changes = {k: v for k, v in changes.items() if k not in ds.coords}
    out = ds
    if coord_changes:
        out = out.assign_coords(coord_changes)
    if var_changes:
        out = out.assign(var_changes)
    return out


def as_list(value: Any) -> list[Any]:
    """``None`` → ``[]``, scalar → ``[scalar]``, iterable (not str) → list."""
    if value is None:
        return []
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        return [value]
    return list(value)


def str_names(names: Iterable[Hashable]) -> list[str]:
    """Hashable xarray names as strings (xarray allows any hashable; we use str only)."""
    return [str(n) for n in names]
