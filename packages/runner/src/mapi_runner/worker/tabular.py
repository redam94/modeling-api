"""Reading tabular uploads under strict caps, and profiling their columns (sandbox only).

Every byte of an upload is untrusted. Before a parser allocates anything big, the caps of
:class:`IngestLimits` are checked against what the file *declares* (Parquet metadata, the
CSV header, gzip and zip sizes); after parsing, cells and headers are checked again:

==========================  ==================================================================
cap                         checked
==========================  ==================================================================
``max_rows``                CSV: parsing stops one row past it; Parquet: from the metadata
``max_columns``             the CSV header or Parquet schema (MFF: distinct variables)
``max_cell_chars``          every header and text cell after parsing
``max_decompression_ratio`` gzip CSV (streamed), Parquet (declared uncompressed size), zip
                            archives (entry sizes) and netCDF (decoded array bytes)
``max_uncompressed_bytes``  the same, as an absolute size
``max_image_*``             ``image_folder`` uploads: images, pixels (from the header) and
                            bytes of one image file (:mod:`~mapi_runner.worker.image_ingest`)
==========================  ==================================================================

Problems are reported as ``MapiError`` with one :class:`~mapi_spec.errors.Issue` whose
``message`` says what is wrong in plain words and ``suggested_action`` what to do; user
values (cells) only ever travel in ``untrusted_text``.
"""

from __future__ import annotations

import csv
import gzip
import io
import warnings
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
from mapi_sdk.schema import infer_freq
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.guided import ColumnProfile
from mapi_spec.ingest import MAX_IMAGE_FILE_BYTES, MAX_IMAGE_PIXELS, MAX_IMAGES

from mapi_runner.jobs import IngestJob

__all__ = [
    "IngestLimits",
    "check_zip_archive",
    "column_profiles",
    "hdf5_superblock_offset",
    "problem",
    "read_as_hdf5",
    "read_table",
    "sniff",
    "write_profiles",
]

PARQUET_MAGIC = b"PAR1"
HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"
BZIP2_MAGIC = b"BZh"
XZ_MAGIC = b"\xfd7zXZ\x00"
NETCDF3_MAGICS: Final = (b"CDF\x01", b"CDF\x02", b"CDF\x05")
"""Classic, 64-bit offset and 64-bit data NetCDF-3 files start with one of these."""
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_HEADER_NAME = 256
"""Column names are at most this long (``IngestMapping`` column refs are)."""
_SNIFF_BYTES = 64 * 1024
_COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class IngestLimits:
    """The caps one ingest run enforces (from :class:`~mapi_spec.jobs.IngestJob`)."""

    max_rows: int = 50_000_000
    max_columns: int = 4096
    max_cell_chars: int = 4096
    max_decompression_ratio: float = 50.0
    max_uncompressed_bytes: int = 8 * 1024**3
    ratio_floor_bytes: int = 64 * 1024**2
    """Expansion below this size is never refused for its ratio (a small file of zeros is
    harmless); the absolute cap still applies."""
    max_cells: int = 250_000_000
    """Values of the dense dataset a table pivots to (every combination of the dim labels
    times the variables), checked before it is built; its bytes must also fit
    ``max_uncompressed_bytes`` (the class's scratch budget)."""
    # M4: tree datasets, HDF5 and multi-file versions
    max_nodes: int = 256
    """Nodes of a tree dataset (the root included)."""
    max_sources: int = 64
    """Uploads of one multi-file version."""
    max_hdf5_objects: int = 10_000
    """Links (groups, datasets) and attributes walked in one HDF5 file (each)."""
    max_ndim: int = 32
    """Dims of one array."""
    max_attr_bytes: int = 65_536
    """Bytes of one attribute value."""
    # v0.14: image_folder uploads
    max_images: int = MAX_IMAGES
    """Images (manifest rows) of one upload; ``ImageFolderOptions.max_images`` is lower."""
    max_image_pixels: int = MAX_IMAGE_PIXELS
    """Pixels of one image, checked from its header before it is decoded."""
    max_image_file_bytes: int = MAX_IMAGE_FILE_BYTES
    """Bytes of one image file in the zip."""

    @classmethod
    def from_job(cls, job: IngestJob) -> IngestLimits:
        return cls(
            max_rows=job.max_rows,
            max_columns=job.max_columns,
            max_cell_chars=job.max_cell_chars,
            max_decompression_ratio=job.max_decompression_ratio,
            max_uncompressed_bytes=job.max_uncompressed_bytes,
            max_nodes=job.max_nodes,
            max_sources=job.max_sources,
            max_hdf5_objects=job.max_hdf5_objects,
            max_ndim=job.max_ndim,
            max_attr_bytes=job.max_attr_bytes,
        )

    def expansion_budget(self, stored: int) -> int:
        """Bytes a compressed object of ``stored`` bytes may expand to."""
        by_ratio = max(max(stored, 1) * self.max_decompression_ratio, self.ratio_floor_bytes)
        return int(min(self.max_uncompressed_bytes, by_ratio))


def problem(
    code: ErrorCode,
    message: str,
    *,
    pointer: str = "",
    action: str | None = None,
    untrusted: str | None = None,
) -> MapiError:
    """A ``MapiError`` carrying one issue with a plain message and a suggested action."""
    issue = Issue(
        code=code,
        pointer=pointer,
        message=message[:2000],
        suggested_action=action[:2000] if action else None,
        untrusted_text=untrusted[:8000] if untrusted else None,
    )
    return MapiError(
        message[:2000],
        code=code,
        pointer=pointer,
        untrusted_text=untrusted[:8000] if untrusted else None,
        issues=[issue],
    )


def _too_large(message: str, action: str, pointer: str = "") -> MapiError:
    return problem(ErrorCode.PAYLOAD_TOO_LARGE, message, pointer=pointer, action=action)


def hdf5_superblock_offset(path: Path) -> int | None:
    """Where libhdf5 would find the file's superblock, or ``None`` when it is not HDF5.

    libhdf5 looks for the signature at byte 0 and then at every power of two from 512 up
    (a *user block* of arbitrary bytes may precede it), so a file whose first bytes look
    like something else (``CDF``...) can still open as HDF5: every one of those offsets is
    checked, never just the first eight bytes.
    """
    size = path.stat().st_size
    with open(path, "rb") as fh:
        offset = 0
        while offset + len(HDF5_MAGIC) <= size:
            fh.seek(offset)
            if fh.read(len(HDF5_MAGIC)) == HDF5_MAGIC:
                return offset
            offset = 512 if offset == 0 else offset * 2
    return None


def read_as_hdf5(path: Path) -> bool:
    """True when the runner reads ``path`` as HDF5 (with the guard first, then an HDF5
    engine): libhdf5 would find a superblock in it (:func:`hdf5_superblock_offset`) and it is
    not a NetCDF-3 file. A file that starts with the NetCDF-3 magic is read only by the
    pure-Python NetCDF-3 reader (scipy), which never hands a byte to libhdf5, so HDF5
    signature bytes that happen to sit in its data at a power-of-two offset do not make it
    unreadable.
    """
    with open(path, "rb") as fh:
        if fh.read(4) in NETCDF3_MAGICS:
            return False
    return hdf5_superblock_offset(path) is not None


def sniff(path: Path) -> str:
    """The container of an upload from its magic bytes: ``parquet``, ``zarr_zip``,
    ``netcdf``, ``gzip``, ``bzip2``, ``xz``, ``zstd`` or ``csv`` (anything else)."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    for magic, kind in (
        (PARQUET_MAGIC, "parquet"),
        (ZIP_MAGIC, "zarr_zip"),
        (HDF5_MAGIC, "netcdf"),
        (b"CDF", "netcdf"),
        (GZIP_MAGIC, "gzip"),
        (BZIP2_MAGIC, "bzip2"),
        (XZ_MAGIC, "xz"),
        (ZSTD_MAGIC, "zstd"),
    ):
        if head.startswith(magic):
            return kind
    return "csv"


# ================================================================================== archives


def check_zip_archive(path: Path, limits: IngestLimits, *, action: str | None = None) -> None:
    """Refuse zip bombs before any entry is decompressed (sizes are declared per entry).
    ``action`` replaces the suggested action for an unreadable archive (default: a zarr
    zip)."""
    stored = path.stat().st_size
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload is not a readable zip archive",
            action=action
            or "Upload the .zarr.zip that mapi_sdk.wire.write_zarr_zip (or the xarray "
            "to_zarr into a ZipStore) wrote. Do not change the file.",
            untrusted=str(exc),
        ) from None
    declared = sum(i.file_size for i in infos)
    if declared > limits.expansion_budget(stored):
        raise _too_large(
            f"the archive expands to {declared:,} bytes from {stored:,} "
            f"(the maximum is {limits.max_decompression_ratio:g} times its size and "
            f"{limits.max_uncompressed_bytes:,} bytes)",
            "Upload the data with less compression, or split it into several datasets.",
        )


def _gunzip(path: Path, limits: IngestLimits) -> Path:
    """Stream-decompress a gzip upload next to it, stopping at the expansion budget."""
    budget = limits.expansion_budget(path.stat().st_size)
    out = path.with_name(path.name + ".csv")
    written = 0
    try:
        with gzip.open(path, "rb") as src, open(out, "wb") as dst:
            while chunk := src.read(_COPY_CHUNK):
                written += len(chunk)
                if written > budget:
                    raise _too_large(
                        f"the gzip file expands to more than {budget:,} bytes (the maximum "
                        f"is {limits.max_decompression_ratio:g} times its size)",
                        "Upload the CSV uncompressed, or split it into several files.",
                    )
                dst.write(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        out.unlink(missing_ok=True)
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload looks gzip-compressed, but the server cannot decompress it",
            action="Upload the CSV file itself, or a complete .csv.gz file.",
            untrusted=str(exc),
        ) from None
    except MapiError:
        out.unlink(missing_ok=True)
        raise
    return out


# ======================================================================================= CSV


def _check_header(names: Sequence[Any], limits: IngestLimits) -> list[str]:
    cols = [str(c).strip() for c in names]
    if len(cols) > limits.max_columns:
        raise _too_large(
            f"the upload has {len(cols):,} columns. The maximum is {limits.max_columns:,}",
            "Remove the columns that the model does not need, or split the table.",
        )
    long = next((c for c in cols if len(c) > MAX_HEADER_NAME), None)
    if long is not None:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            f"a column name has {len(long):,} characters. The maximum is {MAX_HEADER_NAME}",
            action="Make the column names in the first row shorter.",
            untrusted=long[:200],
        )
    if not all(cols):
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            f"column {cols.index('') + 1} has no name",
            action="Give each column a name in the first row (or delete empty columns).",
        )
    seen: set[str] = set()
    for c in cols:
        if c in seen:
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                f"the column name {c[:64]!r} appears twice",
                action="Give each column a different name in the first row.",
            )
        seen.add(c)
    return cols


def _delimiter(path: Path) -> str:
    """``,``, ``;``, tab or ``|``: whichever splits the first line into the most fields
    (spreadsheets in many locales write ``;``)."""
    with open(path, "rb") as fh:
        head = fh.read(_SNIFF_BYTES)
    text = head.decode("utf-8", errors="replace").lstrip("﻿")
    first = text.splitlines()[0] if text.splitlines() else ""
    best, count = ",", 0
    for sep in (",", ";", "\t", "|"):
        try:
            n = len(next(csv.reader([first], delimiter=sep)))
        except (csv.Error, StopIteration):
            continue
        if n > count:
            best, count = sep, n
    return best


def _raw_header(path: Path, sep: str) -> list[str]:
    """The first row as written (pandas renames a repeated name to ``name.1``)."""
    with open(path, "rb") as fh:
        head = fh.read(_SNIFF_BYTES * 16)
    text = head.decode("utf-8", errors="replace").lstrip("\ufeff")
    try:
        return next(csv.reader(io.StringIO(text), delimiter=sep))
    except (csv.Error, StopIteration):
        return []


def _read_csv(path: Path, limits: IngestLimits) -> pd.DataFrame:
    if path.stat().st_size == 0:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload is empty",
            action="Upload the file again. The server got the file with no content.",
        )
    sep = _delimiter(path)
    _check_header(_raw_header(path, sep), limits)
    try:
        df = pd.read_csv(
            path,
            nrows=limits.max_rows + 1,
            sep=sep,
            low_memory=False,
            encoding="utf-8-sig",
        )
    except UnicodeDecodeError as exc:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload is not UTF-8 text",
            action="Save the file as 'CSV UTF-8' (in Excel: File > Save As > CSV UTF-8).",
            untrusted=str(exc),
        ) from None
    except pd.errors.EmptyDataError:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload has no columns",
            action="The first row must name the columns.",
        ) from None
    except (ValueError, OSError, pd.errors.ParserError) as exc:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload is not a readable CSV file",
            action="Make sure that each row has the same number of fields, and that each "
            "quoted text is closed.",
            untrusted=str(exc),
        ) from None
    return df


# =================================================================================== Parquet


def _read_parquet(path: Path, limits: IngestLimits) -> pd.DataFrame:
    import pyarrow.parquet as pq

    try:
        meta = pq.ParquetFile(path).metadata
    except Exception as exc:  # pyarrow raises its own error types for corrupt footers
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload is not a readable Parquet file",
            action="Write the file again with pandas.DataFrame.to_parquet or pyarrow.",
            untrusted=f"{type(exc).__name__}: {exc}",
        ) from None
    if meta.num_rows > limits.max_rows:
        raise _too_large(
            f"the upload has {meta.num_rows:,} rows. The maximum is {limits.max_rows:,}",
            "Aggregate the data (for example to weeks) or split it into several datasets.",
        )
    _check_header(meta.schema.names, limits)
    # what decoding allocates: at least 8 bytes per cell (dictionary and run-length encoded
    # pages declare far less than they decode to)
    declared = 0
    for i in range(meta.num_row_groups):
        rg = meta.row_group(i)
        for j in range(rg.num_columns):
            declared += max(rg.column(j).total_uncompressed_size, rg.num_rows * 8)
    if declared > limits.expansion_budget(path.stat().st_size):
        raise _too_large(
            f"the Parquet file expands to {declared:,} bytes (the maximum is "
            f"{limits.max_decompression_ratio:g} times its size and "
            f"{limits.max_uncompressed_bytes:,} bytes)",
            "Write it with less compression, or split it into several files.",
        )
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the server cannot decode the Parquet file",
            action="Write the file again with pandas.DataFrame.to_parquet or pyarrow.",
            untrusted=f"{type(exc).__name__}: {exc}",
        ) from None


# ===================================================================================== table


def read_table(
    path: Path, fmt: Literal["csv", "parquet"] | str, limits: IngestLimits
) -> pd.DataFrame:
    """Parse a tabular upload under ``limits`` (``fmt`` is what the mapping declares; the
    container is sniffed: gzip CSV is decompressed with a budget, other compressions and
    mismatched containers are refused with a plain explanation)."""
    kind = sniff(path)
    if kind in ("bzip2", "xz", "zstd"):
        raise problem(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            f"{kind} compressed uploads are not supported (the server cannot read them)",
            action="Upload the file uncompressed or as gzip (.csv.gz).",
        )
    if kind in ("zarr_zip", "netcdf"):
        what = "a zip archive" if kind == "zarr_zip" else "a netCDF/HDF5 file"
        raise problem(
            ErrorCode.SCHEMA_INVALID,
            f"the upload is {what}, not a {fmt} table",
            pointer="/mapping/format",
            action="Unzip a zipped CSV before you upload it. For labelled datasets, set the "
            "format of the mapping to zarr_zip or netcdf.",
        )
    if kind == "parquet":
        df = _read_parquet(path, limits)
    elif kind == "gzip":
        plain = _gunzip(path, limits)
        try:
            df = _read_csv(plain, limits)
        finally:
            plain.unlink(missing_ok=True)
    else:
        if fmt == "parquet":
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                "the upload is not a Parquet file",
                pointer="/mapping/format",
                action="Set the format of the mapping to csv, or upload the .parquet file.",
            )
        df = _read_csv(path, limits)
    if len(df) > limits.max_rows:
        raise _too_large(
            f"the upload has more than {limits.max_rows:,} rows",
            "Aggregate the data (for example to weeks) or split it into several datasets.",
        )
    df.columns = _check_header(df.columns, limits)
    _check_cells(df, limits)
    return df


def _check_cells(df: pd.DataFrame, limits: IngestLimits) -> None:
    for col in df.columns:
        s = df[col]
        if s.dtype != object and not pd.api.types.is_string_dtype(s.dtype):
            continue
        lengths = s.dropna().astype(str).str.len()
        if len(lengths) and int(lengths.max()) > limits.max_cell_chars:
            row = int(lengths.idxmax()) if lengths.index.dtype.kind in "iu" else 0
            raise problem(
                ErrorCode.SCHEMA_INVALID,
                f"column {str(col)[:64]!r} has a cell of {int(lengths.max()):,} characters "
                f"(row {row + 2}). The maximum is {limits.max_cell_chars:,}",
                action="Remove long free text (notes, descriptions) from the table.",
            )


# ================================================================================== profiles


def _as_dates(s: pd.Series) -> pd.Series | None:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    if s.dtype != object and not pd.api.types.is_string_dtype(s.dtype):
        return None
    nonnull = s.dropna()
    if nonnull.empty:
        return None
    sample = nonnull.astype(str).head(200)
    if not sample.str.contains(r"\d{1,4}[-/.]\d{1,2}", regex=True).all():
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(nonnull.astype(str), errors="coerce")
    if parsed.isna().any():
        return None
    return pd.to_datetime(s.astype("string"), errors="coerce")


def _scalar(v: Any) -> str | int | float | bool | None:
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, bool | int):
        return v
    if isinstance(v, float):
        return v if np.isfinite(v) else None
    if v is None:
        return None
    return str(v)[:200]


def _profile(
    name: str, s: pd.Series, origin: Literal["column", "mff_variable"]
) -> ColumnProfile | None:
    if len(name) > MAX_HEADER_NAME or not name:
        return None
    n = len(s)
    missing = int(s.isna().sum())
    nonnull = s.dropna()
    distinct = int(nonnull.nunique())
    sample = [_scalar(v) for v in nonnull.drop_duplicates().head(5).tolist()]
    base: dict[str, Any] = {
        "name": name,
        "n_rows": n,
        "n_missing": missing,
        "n_distinct": distinct,
        "sample": sample,
        "origin": origin,
    }
    if pd.api.types.is_bool_dtype(s):
        return ColumnProfile(dtype="boolean", **base)
    numeric = s if pd.api.types.is_numeric_dtype(s) else None
    if numeric is None and (s.dtype == object or pd.api.types.is_string_dtype(s.dtype)):
        conv = pd.to_numeric(s, errors="coerce")
        if conv.notna().sum() == len(nonnull) and len(nonnull):
            numeric = conv
        else:
            lowered = nonnull.astype(str).str.strip().str.lower()
            if len(lowered) and lowered.isin(["true", "false"]).all():
                return ColumnProfile(dtype="boolean", **base)
    if numeric is not None:
        vals = numeric.dropna().astype(float)
        integral = bool(len(vals)) and bool(np.all(np.isclose(vals % 1, 0)))
        finite = vals[np.isfinite(vals)]
        return ColumnProfile(
            dtype="integer" if integral else "number",
            min=float(finite.min()) if len(finite) else None,
            max=float(finite.max()) if len(finite) else None,
            monotonic=bool(numeric.dropna().is_monotonic_increasing) if len(vals) > 1 else None,
            **base,
        )
    dates = _as_dates(s)
    if dates is not None:
        valid = dates.dropna()
        uniq = pd.DatetimeIndex(valid.unique()).sort_values()
        return ColumnProfile(
            dtype="date",
            min_date=uniq[0].isoformat()[:40] if len(uniq) else None,
            max_date=uniq[-1].isoformat()[:40] if len(uniq) else None,
            detected_freq=infer_freq(uniq) if len(uniq) >= 3 else None,
            monotonic=bool(valid.is_monotonic_increasing) if len(valid) > 1 else None,
            **base,
        )
    return ColumnProfile(dtype="string", **base)


def column_profiles(df: pd.DataFrame, *, variables: Iterable[str] = ()) -> list[ColumnProfile]:
    """One :class:`ColumnProfile` per column (``origin: mff_variable`` for ``variables``,
    the pivoted ``VariableName`` values of an MFF table)."""
    as_var = set(variables)
    out: list[ColumnProfile] = []
    for col in df.columns:
        prof = _profile(str(col), df[col], "mff_variable" if col in as_var else "column")
        if prof is not None:
            out.append(prof)
    return out


def write_profiles(profiles: Sequence[ColumnProfile]) -> bytes:
    """``columns.json``: the profiles as a JSON list."""
    buf = io.StringIO()
    buf.write("[")
    buf.write(",".join(p.model_dump_json() for p in profiles))
    buf.write("]")
    return buf.getvalue().encode()
