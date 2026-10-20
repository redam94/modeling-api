"""A safetensors codec in numpy (v0.15), and the weights dataset of pretrained networks.

The `safetensors <https://huggingface.co/docs/safetensors>`_ format is an 8-byte
little-endian header length, a JSON header (``{name: {dtype, shape, data_offsets}}`` plus an
optional ``__metadata__`` mapping of strings) and the raw little-endian tensor bytes. It holds
no code, so network weights in it are a safe native artifact (``FitOutput.native``).

* :func:`read_safetensors` parses untrusted bytes with strict caps (header size, tensor
  count, parameter count) and checks that the offsets cover the data exactly;
* :func:`write_safetensors` writes deterministic bytes (names sorted, the header padded with
  spaces). The header length never starts with byte ``0x80``, so
  :func:`mapi_sdk.bundle.assert_no_pickle` never sees a pickle-like prefix;
* :func:`weights_dataset` / :func:`tensors_from_dataset` convert tensors to and from the
  weights dataset a ``safetensors`` ingest writes: one flat ``weights(param)`` float32 variable
  and the tensor table in the ``mapi_weights`` attribute (a dataset allows few dims, so one
  dim per tensor axis is not possible).

``BF16`` tensors are read as float32 (exact: the 16 bits are the high half of a float32);
they are written from arrays whose dtype is named ``bfloat16`` (``ml_dtypes``, as jax uses).
The ``safetensors`` package is not needed (its files are read and written the same way).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import (
    MAX_SAFETENSORS_HEADER,
    MAX_WEIGHT_PARAMETERS,
    MAX_WEIGHT_TENSORS,
    WEIGHTS_ATTR,
    WEIGHTS_DIM,
    WEIGHTS_VAR,
)
from pydantic import JsonValue

__all__ = [
    "DTYPES",
    "file_sha256",
    "read_safetensors",
    "tensor_dtypes",
    "tensors_from_dataset",
    "weights_dataset",
    "write_safetensors",
]

DTYPES: Final[dict[str, str]] = {
    "F64": "<f8",
    "F32": "<f4",
    "F16": "<f2",
    "BF16": "<u2",
    "I64": "<i8",
    "I32": "<i4",
    "I16": "<i2",
    "I8": "i1",
    "U8": "u1",
    "BOOL": "?",
}
"""The safetensors dtypes this codec reads and writes, with their stored numpy dtype."""
_FROM_NUMPY: Final[dict[str, str]] = {
    "float64": "F64",
    "float32": "F32",
    "float16": "F16",
    "bfloat16": "BF16",
    "int64": "I64",
    "int32": "I32",
    "int16": "I16",
    "int8": "I8",
    "uint8": "U8",
    "bool": "BOOL",
}
_METADATA: Final = "__metadata__"
_MAX_NAME: Final = 1024
_MAX_RANK: Final = 16
_EXACT_F32: Final = 2**24
"""Integers up to this magnitude are exact in float32."""


def _bad(message: str, untrusted: str | None = None) -> MapiError:
    return MapiError(
        f"the safetensors file is not valid: {message}",
        code=ErrorCode.SCHEMA_INVALID,
        untrusted_text=None if untrusted is None else untrusted[:500],
    )


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _bad("the header has the same key two times", k)
        out[k] = v
    return out


def _parse(
    data: bytes | memoryview, max_tensors: int, max_header: int, max_parameters: int
) -> tuple[list[tuple[str, str, tuple[int, ...], int, int]], dict[str, str], int]:
    """``([(name, dtype, shape, begin, end)], metadata, data_start)``, sorted by offset."""
    view = memoryview(data).cast("B")
    if len(view) < 8:
        raise _bad("it is shorter than its 8-byte header length")
    n = int.from_bytes(view[:8], "little")
    if n > max_header:
        raise _bad(f"the header has {n:,} bytes. The maximum is {max_header:,}")
    if n < 2 or 8 + n > len(view):
        raise _bad("the header length is not in the file")
    try:
        header = json.loads(
            bytes(view[8 : 8 + n]).decode("utf-8"), object_pairs_hook=_no_duplicates
        )
    except MapiError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _bad("the header is not JSON", str(exc)) from None
    if not isinstance(header, dict):
        raise _bad("the header is not a JSON object")
    raw_meta = header.pop(_METADATA, None) or {}
    if not isinstance(raw_meta, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_meta.items()
    ):
        raise _bad("__metadata__ must map strings to strings")
    if len(header) > max_tensors:
        raise _bad(f"it has {len(header):,} tensors. The maximum is {max_tensors:,}")
    entries: list[tuple[str, str, tuple[int, ...], int, int]] = []
    parameters = 0
    for name, entry in header.items():
        if not name or len(name) > _MAX_NAME:
            raise _bad("a tensor name is empty or too long", name)
        if not isinstance(entry, dict) or set(entry) != {"dtype", "shape", "data_offsets"}:
            raise _bad("a tensor entry must have dtype, shape and data_offsets only", name)
        dtype, shape, offsets = entry["dtype"], entry["shape"], entry["data_offsets"]
        if dtype not in DTYPES:
            raise _bad(
                f"a tensor has the dtype {str(dtype)[:20]!r}; the supported dtypes are "
                + ", ".join(DTYPES),
                name,
            )
        if (
            not isinstance(shape, list)
            or len(shape) > _MAX_RANK
            or not all(isinstance(d, int) and not isinstance(d, bool) and d >= 0 for d in shape)
        ):
            raise _bad("a tensor shape must be a list of non-negative integers", name)
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(isinstance(o, int) and not isinstance(o, bool) for o in offsets)
            or not 0 <= offsets[0] <= offsets[1]
        ):
            raise _bad("a tensor's data_offsets must be [begin, end] with begin <= end", name)
        size = math.prod(shape)
        parameters += size
        if parameters > max_parameters:
            raise _bad(f"it has more than {max_parameters:,} parameters")
        if offsets[1] - offsets[0] != size * np.dtype(DTYPES[dtype]).itemsize:
            raise _bad("a tensor's data_offsets do not match its shape and dtype", name)
        entries.append((name, dtype, tuple(shape), offsets[0], offsets[1]))
    entries.sort(key=lambda e: (e[3], e[4]))
    at = 0
    for name, _, _, begin, end in entries:
        if begin != at:
            raise _bad("the tensor data overlaps or has gaps (data_offsets)", name)
        at = end
    if 8 + n + at != len(view):
        raise _bad("the tensor data does not end at the end of the file")
    return entries, dict(raw_meta), 8 + n


def read_safetensors(
    data: bytes | memoryview,
    *,
    max_tensors: int = MAX_WEIGHT_TENSORS,
    max_header: int = MAX_SAFETENSORS_HEADER,
    max_parameters: int = MAX_WEIGHT_PARAMETERS,
) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    """The tensors (native-endian copies, ``BF16`` as float32) and the ``__metadata__``.

    Raises:
        MapiError: ``SCHEMA_INVALID`` when the bytes are not a valid file within the caps.
    """
    entries, metadata, start = _parse(data, max_tensors, max_header, max_parameters)
    view = memoryview(data).cast("B")
    out: dict[str, np.ndarray] = {}
    for name, dtype, shape, begin, end in sorted(entries):
        raw = np.frombuffer(view[start + begin : start + end], dtype=DTYPES[dtype])
        if dtype == "BF16":
            arr = (raw.astype(np.uint32) << 16).view(np.float32)
        elif dtype == "BOOL":
            if raw.view(np.uint8).max(initial=0) > 1:
                raise _bad("a BOOL tensor holds values other than 0 and 1", name)
            arr = raw.copy()
        else:
            arr = raw.astype(raw.dtype.newbyteorder("="))
        out[name] = arr.reshape(shape)
    return out, metadata


def tensor_dtypes(
    data: bytes | memoryview,
    *,
    max_tensors: int = MAX_WEIGHT_TENSORS,
    max_header: int = MAX_SAFETENSORS_HEADER,
    max_parameters: int = MAX_WEIGHT_PARAMETERS,
) -> dict[str, str]:
    """The stored safetensors dtype of each tensor (``read_safetensors`` returns ``BF16`` as
    float32; pass these to :func:`weights_dataset` to keep the source dtypes)."""
    entries, _, _ = _parse(data, max_tensors, max_header, max_parameters)
    return {name: dtype for name, dtype, *_ in entries}


def _dtype_code(arr: np.ndarray, name: str) -> str:
    code = _FROM_NUMPY.get(arr.dtype.name)
    if code is None:
        raise MapiError(
            f"tensor {name!r} has the dtype {arr.dtype.name}; safetensors takes "
            + ", ".join(sorted(_FROM_NUMPY)),
            code=ErrorCode.SCHEMA_INVALID,
        )
    return code


def write_safetensors(
    tensors: Mapping[str, np.ndarray], metadata: Mapping[str, str] | None = None
) -> bytes:
    """Deterministic safetensors bytes: names sorted, little-endian, the JSON header padded
    with spaces to a multiple of 8 (8 more when its length's first byte would be ``0x80``)."""
    header: dict[str, Any] = {}
    if metadata:
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
            raise MapiError(
                "safetensors metadata must map strings to strings", code=ErrorCode.SCHEMA_INVALID
            )
        header[_METADATA] = {k: metadata[k] for k in sorted(metadata)}
    chunks: list[bytes] = []
    at = 0
    for name in sorted(tensors):
        if not isinstance(name, str) or not name or name == _METADATA or len(name) > _MAX_NAME:
            raise MapiError(
                "a tensor name must be a non-empty string other than __metadata__",
                code=ErrorCode.SCHEMA_INVALID,
                untrusted_text=str(name)[:200],
            )
        arr = np.asarray(tensors[name])
        code = _dtype_code(arr, name)
        if code == "BF16":
            raw = np.ascontiguousarray(arr).view(np.uint16).astype("<u2").tobytes()
        else:
            raw = np.ascontiguousarray(arr, dtype=np.dtype(DTYPES[code])).tobytes()
        header[name] = {
            "dtype": code,
            "shape": [int(d) for d in arr.shape],
            "data_offsets": [at, at + len(raw)],
        }
        chunks.append(raw)
        at += len(raw)
    text = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    n = -(-len(text) // 8) * 8
    if n & 0xFF == 0x80:
        n += 8  # a leading 0x80 byte is a pickle PROTO opcode
    text += b" " * (n - len(text))
    return n.to_bytes(8, "little") + text + b"".join(chunks)


# =============================================================================== the dataset


def _weights_error(message: str) -> MapiError:
    return MapiError(
        f"the dataset is not a weights dataset: {message}",
        code=ErrorCode.SCHEMA_INVALID,
        pointer="/data/side_tables",
    )


def _as_float32(arr: np.ndarray, name: str) -> np.ndarray:
    """``arr`` as float32: floats always, integers and bools only when exact."""
    if arr.dtype.kind == "f" or arr.dtype.name == "bfloat16":
        return np.asarray(arr, dtype=np.float32)
    if arr.dtype.kind in "biu":
        if arr.size and arr.dtype.kind != "b" and np.abs(arr.astype(np.int64)).max() > _EXACT_F32:
            raise MapiError(
                f"tensor {name!r} has integers that float32 cannot hold exactly",
                code=ErrorCode.SCHEMA_INVALID,
            )
        return arr.astype(np.float32)
    raise MapiError(
        f"tensor {name!r} has the dtype {arr.dtype.name}, which is not a number",
        code=ErrorCode.SCHEMA_INVALID,
    )


def weights_dataset(
    tensors: Mapping[str, np.ndarray],
    metadata: Mapping[str, str] | None = None,
    *,
    arch: Mapping[str, JsonValue] | None = None,
    sha256: str | None = None,
    dtypes: Mapping[str, str] | None = None,
) -> xr.Dataset:
    """The weights dataset of ``tensors``: ``weights(param)`` float32 (tensors flattened in
    name order) with the ``mapi_weights`` attribute (JSON: the tensor table, metadata, arch,
    sha256 and parameter count).

    ``arch`` defaults to the JSON of ``metadata["mapi_arch"]`` when present. ``dtypes`` gives
    the source dtype of each tensor (default: from the array dtype).
    """
    meta = {str(k): str(v) for k, v in (metadata or {}).items()}
    if arch is None and "mapi_arch" in meta:
        try:
            parsed = json.loads(meta["mapi_arch"])
        except ValueError:
            raise MapiError(
                "the metadata entry mapi_arch is not JSON", code=ErrorCode.SCHEMA_INVALID
            ) from None
        if not isinstance(parsed, dict):
            raise MapiError(
                "the metadata entry mapi_arch is not a JSON object", code=ErrorCode.SCHEMA_INVALID
            )
        arch = parsed
    table: list[dict[str, JsonValue]] = []
    flat: list[np.ndarray] = []
    offset = 0
    for name in sorted(tensors):
        arr = np.asarray(tensors[name])
        source = (dtypes or {}).get(name) or _dtype_code(arr, name)
        if source not in DTYPES:
            raise MapiError(f"tensor {name!r} has an unknown dtype", code=ErrorCode.SCHEMA_INVALID)
        values = _as_float32(arr, name).reshape(-1)
        table.append(
            {"name": name, "shape": [int(d) for d in arr.shape], "dtype": source, "offset": offset}
        )
        flat.append(values)
        offset += values.size
    doc: dict[str, JsonValue] = {
        "tensors": table,  # type: ignore[dict-item]
        "metadata": meta,  # type: ignore[dict-item]
        "arch": dict(arch) if arch is not None else None,
        "sha256": sha256,
        "parameters": offset,
    }
    weights = np.concatenate(flat) if flat else np.zeros(0, dtype=np.float32)
    attrs = {WEIGHTS_ATTR: json.dumps(doc, sort_keys=True, separators=(",", ":"))}
    return xr.Dataset(
        {WEIGHTS_VAR: ((WEIGHTS_DIM,), weights.astype(np.float32, copy=False), attrs)}
    )


def tensors_from_dataset(ds: xr.Dataset) -> tuple[dict[str, np.ndarray], dict[str, JsonValue]]:
    """The float32 tensors (in their shapes) of a weights dataset and its ``mapi_weights``
    document.

    Raises:
        MapiError: ``SCHEMA_INVALID`` when ``ds`` is not a weights dataset.
    """
    if WEIGHTS_VAR not in ds.data_vars or ds[WEIGHTS_VAR].dims != (WEIGHTS_DIM,):
        raise _weights_error(f"it has no {WEIGHTS_VAR}({WEIGHTS_DIM}) variable")
    raw = ds[WEIGHTS_VAR].attrs.get(WEIGHTS_ATTR, ds.attrs.get(WEIGHTS_ATTR))
    if not isinstance(raw, str):
        raise _weights_error(f"it has no {WEIGHTS_ATTR} attribute")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise _weights_error(f"the {WEIGHTS_ATTR} attribute is not JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("tensors"), list):
        raise _weights_error(f"the {WEIGHTS_ATTR} attribute has no tensor table")
    values = np.asarray(ds[WEIGHTS_VAR].values, dtype=np.float32)
    out: dict[str, np.ndarray] = {}
    for entry in doc["tensors"]:
        try:
            name, shape, offset = (
                str(entry["name"]),
                [int(d) for d in entry["shape"]],
                entry["offset"],
            )
        except (KeyError, TypeError, ValueError):
            raise _weights_error("a tensor entry has no name, shape or offset") from None
        size = math.prod(shape)
        if (
            not isinstance(offset, int)
            or offset < 0
            or offset + size > values.size
            or min(shape, default=0) < 0
        ):
            raise _weights_error(f"tensor {name[:200]!r} is not inside the weights")
        if name in out:
            raise _weights_error("two tensors have the same name")
        out[name] = values[offset : offset + size].reshape(shape).copy()
    return out, doc


def file_sha256(data: bytes | memoryview) -> str:
    """Hex SHA-256 of a file's bytes (the ``sha256`` of :func:`weights_dataset`)."""
    return hashlib.sha256(data).hexdigest()
