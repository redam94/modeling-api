"""Uploads to signed PUT URLs and verified downloads from signed GET URLs.

The API never sees dataset or artifact bytes: uploads go straight to the quarantine bucket
through a signed PUT, and artifacts come from generation-pinned signed GETs. These requests
use a separate HTTP client that **never carries the API's Authorization header**.

Every transfer is checked against the CRC32C the storage service computed itself
(``ArtifactLink.crc32c``; the object resource returned by the PUT). A mismatch raises
:class:`~mapi_spec.errors.IntegrityFailure` and nothing is kept.

``xr.Dataset`` / ``xr.DataTree`` inputs and ``xr.DataTree`` outputs need the ``xarray``
extra (``pip install 'mapi-client[xarray]'``), which brings ``mapi-sdk`` and its validated
zarr-zip reader/writer.

Raw HDF5 files (``.h5``, ``.hdf5``, ``.he5``; format ``hdf5``, M4) are not self-describing:
they need an :class:`~mapi_spec.ingest.Hdf5Mapping` naming each array's dims. A NetCDF-4 or
xarray-written ``.h5`` file may instead be ingested with ``IngestMapping(format="netcdf")``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx
from mapi_spec.api import ArtifactLink, IngestMapping
from mapi_spec.errors import IntegrityFailure, PayloadTooLarge
from mapi_spec.storage import SignedPut, crc32c, crc32c_from_base64, crc32c_to_base64

from mapi_client.errors import raise_for_response

if TYPE_CHECKING:
    import xarray as xr

__all__ = [
    "CHUNK_SIZE",
    "UploadFormat",
    "UploadSource",
    "check_put_response",
    "datatree_from_bytes",
    "datatree_to_bytes",
    "default_mapping",
    "detect_format",
    "mapping_reads",
    "require_xarray",
    "upload_source",
    "verify_bytes",
    "xarray_available",
]

CHUNK_SIZE: Final = 1024 * 1024
UploadFormat = Literal["csv", "parquet", "zarr_zip", "netcdf", "hdf5"]

_SUFFIXES: Final[tuple[tuple[str, UploadFormat, str], ...]] = (
    (".zarr.zip", "zarr_zip", "application/zip"),
    (".zip", "zarr_zip", "application/zip"),
    (".csv", "csv", "text/csv"),
    (".parquet", "parquet", "application/vnd.apache.parquet"),
    (".pq", "parquet", "application/vnd.apache.parquet"),
    (".nc", "netcdf", "application/x-netcdf"),
    (".nc4", "netcdf", "application/x-netcdf"),
    (".h5", "hdf5", "application/x-hdf5"),
    (".hdf5", "hdf5", "application/x-hdf5"),
    (".he5", "hdf5", "application/x-hdf5"),
)
_CONTENT_TYPES: Final[dict[UploadFormat, str]] = {f: ct for _, f, ct in reversed(_SUFFIXES)}

_XARRAY_HINT: Final = (
    "this feature needs xarray and mapi-sdk: install the extra with "
    "\"pip install 'mapi-client[xarray]'\""
)


# --------------------------------------------------------------------------------- xarray


def xarray_available() -> bool:
    """True when the ``xarray`` extra (xarray + mapi-sdk) is installed."""
    return all(importlib.util.find_spec(m) is not None for m in ("xarray", "mapi_sdk"))


def require_xarray() -> None:
    if not xarray_available():
        raise ImportError(_XARRAY_HINT)


def _is_xr_dataset(obj: Any) -> bool:
    xr_mod = sys.modules.get("xarray")
    return xr_mod is not None and isinstance(obj, xr_mod.Dataset)


def _is_xr_datatree(obj: Any) -> bool:
    xr_mod = sys.modules.get("xarray")
    tree_type = getattr(xr_mod, "DataTree", None) if xr_mod is not None else None
    return tree_type is not None and isinstance(obj, tree_type)


def datatree_from_bytes(data: bytes) -> xr.DataTree:
    """Decode a ``*.zarr.zip`` DataTree with the SDK's validating reader."""
    require_xarray()
    from mapi_sdk.wire import read_datatree_zip

    return read_datatree_zip(data)


def _dataset_bytes(ds: xr.Dataset) -> bytes:
    require_xarray()
    from mapi_sdk.wire import write_zarr_zip

    out = write_zarr_zip(ds)
    assert out is not None
    return out


def datatree_to_bytes(tree: xr.DataTree) -> bytes:
    """Encode a ``DataTree`` with the SDK's writer (the ``data.tree.zip`` wire format)."""
    require_xarray()
    from mapi_sdk.wire import write_datatree_zip

    out = write_datatree_zip(tree)
    assert out is not None
    return out


# -------------------------------------------------------------------------------- uploads


def detect_format(filename: str) -> tuple[UploadFormat, str]:
    """``(format, content_type)`` from a file name's extension."""
    lower = filename.lower()
    for suffix, fmt, ctype in _SUFFIXES:
        if lower.endswith(suffix):
            return fmt, ctype
    known = ", ".join(s for s, _, _ in _SUFFIXES)
    raise ValueError(f"cannot tell the format of {filename!r} (known extensions: {known})")


def mapping_reads(mapping_format: str, file_format: UploadFormat) -> bool:
    """Whether an ingest mapping of ``mapping_format`` can read a file of ``file_format``:
    the same format, an ``mff`` long table (a CSV file), or - since NetCDF-4 is HDF5 - a
    ``netcdf`` mapping for a self-describing ``.h5`` file and an ``hdf5`` mapping for a
    NetCDF-4 file read as raw HDF5."""
    if mapping_format == file_format:
        return True
    if mapping_format == "mff":
        return file_format == "csv"
    return {mapping_format, file_format} == {"netcdf", "hdf5"}


def default_mapping(fmt: UploadFormat) -> IngestMapping:
    """Mapping for self-describing formats; csv/parquet need an explicit mapping."""
    if fmt in ("csv", "parquet"):
        raise ValueError(
            f"{fmt} uploads need an IngestMapping that maps columns to dims and variables"
        )
    if fmt == "hdf5":
        raise ValueError(
            "raw HDF5 uploads need an IngestMapping with an 'hdf5' mapping (Hdf5Mapping) that "
            "names each array's dims and coords - inspect the file first (datasets.inspect / "
            "'mapi datasets inspect'); a NetCDF-4 or xarray-written .h5 file can use "
            "IngestMapping(format='netcdf') instead"
        )
    return IngestMapping(format=fmt)


@dataclass(frozen=True, slots=True)
class UploadSource:
    """Bytes to upload: a file on disk (streamed) or an in-memory payload."""

    filename: str
    size: int
    format: UploadFormat
    content_type: str
    path: Path | None = None
    data: bytes | None = None

    def chunks(self, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        if self.data is not None:
            for i in range(0, len(self.data), chunk_size):
                yield self.data[i : i + chunk_size]
            return
        assert self.path is not None
        with self.path.open("rb") as fh:
            while chunk := fh.read(chunk_size):
                yield chunk

    async def achunks(self, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        for chunk in self.chunks(chunk_size):
            yield chunk


def upload_source(
    source: str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes,
    *,
    filename: str | None = None,
) -> UploadSource:
    """Describe what to upload. An ``xr.Dataset`` is written as a zarr zip in memory, an
    ``xr.DataTree`` as a tree zarr zip (``data.tree.zip``, format ``zarr_zip``)."""
    if _is_xr_datatree(source):
        tree_payload = datatree_to_bytes(source)  # type: ignore[arg-type]
        name = filename or "data.tree.zip"
        return UploadSource(
            name, len(tree_payload), "zarr_zip", "application/zip", data=tree_payload
        )
    if _is_xr_dataset(source):
        payload = _dataset_bytes(source)  # type: ignore[arg-type]
        name = filename or "data.zarr.zip"
        return UploadSource(name, len(payload), "zarr_zip", "application/zip", data=payload)
    if isinstance(source, bytes):
        if filename is None:
            raise ValueError("uploading raw bytes needs a filename (for the format)")
        fmt, ctype = detect_format(filename)
        return UploadSource(filename, len(source), fmt, ctype, data=source)
    if isinstance(source, str | os.PathLike):
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{path} is not a file")
        name = filename or path.name
        fmt, ctype = detect_format(name)
        return UploadSource(name, path.stat().st_size, fmt, ctype, path=path)
    raise TypeError(
        "upload a file path, raw bytes with a filename, an xr.Dataset or an xr.DataTree"
    )


def check_upload_size(src: UploadSource, put: SignedPut) -> None:
    if src.size == 0:
        raise ValueError("refusing to upload an empty file")
    if put.max_bytes is not None and src.size > put.max_bytes:
        raise PayloadTooLarge(
            f"{src.filename} is {src.size} bytes; the upload URL accepts at most {put.max_bytes}"
        )


def put_headers(src: UploadSource, put: SignedPut) -> dict[str, str]:
    headers = dict(put.headers)  # must be sent verbatim (they are part of the signature)
    headers["content-length"] = str(src.size)
    return headers


class Crc32cAccumulator:
    """Running CRC32C over streamed chunks."""

    def __init__(self) -> None:
        self.value = 0
        self.size = 0

    def update(self, chunk: bytes) -> bytes:
        self.value = crc32c(chunk, self.value)
        self.size += len(chunk)
        return chunk

    @property
    def base64(self) -> str:
        return crc32c_to_base64(self.value)


def check_put_response(response: httpx.Response, acc: Crc32cAccumulator, name: str) -> None:
    """Raise for an error status; compare the storage-computed CRC32C when it is returned."""
    if response.status_code >= 400:
        response.read()
        raise_for_response(response)
    try:
        body = response.json()
    except ValueError:
        return  # storage services may answer an upload with an empty body
    remote = body.get("crc32c") if isinstance(body, dict) else None
    if isinstance(remote, str) and remote != acc.base64:
        raise IntegrityFailure(
            f"upload of {name} was corrupted in transit: storage computed CRC32C {remote}, "
            f"the client sent {acc.base64}"
        )


# ------------------------------------------------------------------------------ downloads


def verify_bytes(link: ArtifactLink, size: int, value: int) -> None:
    if size != link.size:
        raise IntegrityFailure(
            f"artifact {link.name} is {size} bytes, the signed link promised {link.size}"
        )
    expected = crc32c_from_base64(link.crc32c)
    if value != expected:
        raise IntegrityFailure(
            f"artifact {link.name} failed its CRC32C check "
            f"(got {crc32c_to_base64(value)}, expected {link.crc32c})"
        )


class DownloadSink:
    """Collects a download in memory or in a temp file renamed into place on success."""

    def __init__(self, link: ArtifactLink, dest: Path | None) -> None:
        self.link = link
        self.dest = dest
        self.acc = Crc32cAccumulator()
        self._buf: list[bytes] = []
        self._fh: Any = None
        self._tmp: Path | None = None
        if dest is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
            self._fh = os.fdopen(fd, "wb")
            self._tmp = Path(tmp)

    def write(self, chunk: bytes) -> None:
        self.acc.update(chunk)
        if self.acc.size > self.link.size:
            raise IntegrityFailure(
                f"artifact {self.link.name} is larger than the {self.link.size} bytes promised"
            )
        if self._fh is not None:
            self._fh.write(chunk)
        else:
            self._buf.append(chunk)

    def finish(self) -> bytes | Path:
        verify_bytes(self.link, self.acc.size, self.acc.value)
        if self._fh is not None:
            assert self._tmp is not None and self.dest is not None
            self._fh.close()
            os.replace(self._tmp, self.dest)
            return self.dest
        return b"".join(self._buf)

    def abort(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            if self._tmp is not None:
                with contextlib.suppress(OSError):
                    self._tmp.unlink()
