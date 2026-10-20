"""Collection ingest (v0.15): a client manifest and image parts become an image collection
version.

Inputs (:class:`~mapi_spec.collection.CollectionIngest`): ``manifest`` (the client's table,
one row per image with the sha256 of its bytes), ``part.0000`` .. (zips of images named
``<sha256>.<ext>``) and, with a base version, ``base.manifest``, ``base.info`` and
``base.pack.0000`` ... The run:

1. reads the manifest (CSV, TSV or Parquet; at most ``MAX_MANIFEST_BYTES``) and checks its
   columns (lowercase hex sha256, unique ids, no reserved names, at most ``max_images`` rows);
2. indexes each part with ranged reads (never downloaded whole) and reads every entry in
   spans of at most 64 MiB: inflated, CRC-32 and sha256 (= the entry name) checked, the
   image header read with the pixel cap (no pixel is decoded);
3. reads the base version's manifest and info (the images it already holds);
4. finds every manifest image in a part, else in the base; a missing image is an error;
5. lists the packs the version reads (the parts it uses, then the base packs it uses; at most
   ``MAX_VERSION_PACKS``); a part no row uses is a note;
6. writes the outputs (I-2): ``manifest.parquet``, ``members.bin``, ``collection.json``,
   ``schema.json``, ``summary.json``/``profile.json``/``preview.json`` of the manifest,
   ``columns.json`` and the Data Explorer views of a seeded sample decoded to
   ``pixels(image, y, x, channel)``.

A part's pack address is :func:`~mapi_spec.collection.pack_digest` of the signed GET's
server-computed size, CRC32C and MD5 (pinned generation); the finalizer recomputes it before
it copies the part to the tenant's pack store. Nothing here writes outside the run's outputs.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd
from mapi_sdk.collection import (
    LABEL_VAR,
    BlobLocation,
    Collection,
    CollectionError,
    PartEntry,
    index_part,
    members_bin,
    read_blob,
    write_manifest,
)
from mapi_sdk.images import ImageDecodeError, ImageHeader, image_header
from mapi_sdk.schema import data_summary
from mapi_spec.collection import (
    BASE_MANIFEST_INPUT,
    COLLECTION_INFO,
    COLLECTION_MANIFEST,
    COLLECTION_MEMBERS,
    INGEST_MANIFEST_INPUT,
    MANIFEST_RESERVED,
    MAX_COLLECTION_CLASSES,
    MAX_MANIFEST_BYTES,
    MAX_VERSION_PACKS,
    ClassCount,
    CollectionInfo,
    CollectionIngest,
    CollectionOptions,
    PackInfo,
    pack_digest,
    pack_input,
    part_input,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.run import RunnerPhase
from mapi_spec.schema import DataSchema
from mapi_spec.storage import SignedGet

from mapi_runner.jobs import INGEST_SCHEMA, IngestJob
from mapi_runner.worker.blobs import SignedPackSource, check_pack
from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import load_collection
from mapi_runner.worker.tabular import (
    IngestLimits,
    column_profiles,
    problem,
    read_table,
    sniff,
    write_profiles,
)

__all__ = ["run_collection_ingest"]

_POINTER: Final = "/collection"
_OPTIONS: Final = "/collection/options"
_MAX_LISTED: Final = 20
_SPAN_BYTES: Final = 64 * 1024**2
"""Largest range read of a part while its entries are checked."""
_EXPLORE_SIDE: Final = 64
"""The explorer sample is decoded at the collection's size, else at most 64 x 64."""
_MAX_SPLITS: Final = 64
_COORDS: Final = ("label", "split", "path")
"""Manifest columns the version keeps under these names (from the options' columns)."""


def _err(
    message: str,
    pointer: str = _OPTIONS,
    *,
    action: str | None = None,
    untrusted: str | None = None,
) -> MapiError:
    return problem(
        ErrorCode.SCHEMA_INVALID, message, pointer=pointer, action=action, untrusted=untrusted
    )


@dataclass(frozen=True, slots=True)
class _Image:
    """Where one distinct image is and what its header says."""

    location: BlobLocation
    header: ImageHeader
    source: str
    """``upload`` (a part of this ingest) or ``base``."""


# ================================================================================ manifest


def _text(value: Any) -> str:
    if value is None or value is pd.NA or value is pd.NaT:
        return ""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not np.isfinite(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def _texts(df: pd.DataFrame, column: str, pointer: str) -> list[str]:
    if column not in df.columns:
        present = [str(c) for c in df.columns]
        raise _err(
            "a column of the options is not in the manifest",
            pointer,
            action="Use the name of a column of the manifest. The manifest has these columns: "
            + ", ".join(present[:_MAX_LISTED])
            + (" ..." if len(present) > _MAX_LISTED else "")
            + ".",
            untrusted=column[:256],
        )
    return [_text(v) for v in df[column].tolist()]


def _read_client_manifest(ctx: WorkerContext, limits: IngestLimits) -> pd.DataFrame:
    path = ctx.download(INGEST_MANIFEST_INPUT)
    assert path is not None
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the manifest has more than {MAX_MANIFEST_BYTES:,} bytes",
            pointer=f"{_POINTER}/manifest_upload_id",
            action="Split the images into several datasets.",
        )
    try:
        return read_table(path, "parquet" if sniff(path) == "parquet" else "csv", limits)
    except MapiError as exc:
        issue = exc.issues[0] if exc.issues else None
        raise problem(
            exc.code,
            f"the manifest cannot be read: {exc.detail}",
            pointer=f"{_POINTER}/manifest_upload_id",
            action=issue.suggested_action if issue is not None else None,
            untrusted=exc.untrusted_text,
        ) from None


def _unique(values: list[str], what: str, pointer: str, action: str) -> None:
    seen: set[str] = set()
    for i, v in enumerate(values):
        if not v:
            raise _err(f"the {what} is empty in row {i + 2}", pointer, action=action)
        if v in seen:
            raise _err(
                f"the {what} has the same value two times (row {i + 2})",
                pointer,
                action=action,
                untrusted=v[:256],
            )
        seen.add(v)


@dataclass(slots=True)
class _Rows:
    """The client manifest, checked: one entry per image (row order kept)."""

    ids: list[str]
    shas: list[str]
    coords: dict[str, list[str]]
    """``label``, ``split`` and ``path`` (when the options give them)."""
    columns: dict[str, pd.Series]
    """User columns kept (their names are not reserved)."""


def _rows(df: pd.DataFrame, opts: CollectionOptions) -> _Rows:
    n = len(df)
    if n == 0:
        raise _err("the manifest has no rows", f"{_POINTER}/manifest_upload_id")
    if n > opts.max_images:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the manifest has {n:,} rows. The maximum of this version is {opts.max_images:,}",
            pointer=f"{_OPTIONS}/max_images",
            action="Increase max_images, or split the images into several datasets.",
        )
    shas = [s.lower() for s in _texts(df, opts.sha256_column, f"{_OPTIONS}/sha256_column")]
    for i, s in enumerate(shas):
        if len(s) != 64 or any(ch not in "0123456789abcdef" for ch in s):
            raise _err(
                f"the sha256 in row {i + 2} is not 64 hex characters",
                f"{_OPTIONS}/sha256_column",
                action="Write the sha256 of each image file as 64 hex characters. "
                "'mapi datasets upload-images' writes the manifest for you.",
                untrusted=s[:100],
            )
    coords: dict[str, list[str]] = {}
    path_default = opts.path_column == "path" and "path" not in df.columns
    if opts.path_column is not None and not path_default:
        coords["path"] = _texts(df, opts.path_column, f"{_OPTIONS}/path_column")
    if opts.label_column is not None:
        coords["label"] = _texts(df, opts.label_column, f"{_OPTIONS}/label_column")
    if opts.split_column is not None:
        coords["split"] = _texts(df, opts.split_column, f"{_OPTIONS}/split_column")
    if opts.id_column is not None:
        ids = _texts(df, opts.id_column, f"{_OPTIONS}/id_column")
        _unique(ids, "id column", f"{_OPTIONS}/id_column", "Give each image a different id.")
    elif "path" in coords:
        ids = coords["path"]
        _unique(
            ids,
            "path column",
            f"{_OPTIONS}/path_column",
            "Give each image a different path, or set id_column.",
        )
    else:
        ids = shas
        _unique(
            ids,
            "sha256 column",
            f"{_OPTIONS}/sha256_column",
            "An image occurs in two rows: set id_column (or path_column) to give each row "
            "its own id.",
        )
    used = {
        c
        for c in (
            opts.sha256_column,
            opts.id_column,
            opts.path_column,
            opts.label_column,
            opts.split_column,
        )
        if c is not None
    }
    if opts.columns is not None:
        kept = list(opts.columns)
        for c in kept:
            _texts(df, c, f"{_OPTIONS}/columns")  # present
    else:
        kept = [str(c) for c in df.columns if str(c) not in used]
    reserved = sorted({c for c in kept if c in MANIFEST_RESERVED or c in coords})
    if reserved:
        raise _err(
            f"the manifest has columns with reserved names: {', '.join(reserved[:_MAX_LISTED])}",
            f"{_OPTIONS}/columns",
            action="Rename these columns, or list the columns to keep in the columns option. "
            "Reserved names: " + ", ".join(sorted(MANIFEST_RESERVED | set(_COORDS))) + ".",
        )
    if len(kept) > 64:
        raise _err(
            f"the manifest has {len(kept)} other columns. The maximum is 64",
            f"{_OPTIONS}/columns",
            action="List the columns to keep in the columns option.",
        )
    return _Rows(ids, shas, coords, {c: df[c].reset_index(drop=True) for c in kept})


# =================================================================================== parts


class _SpanSource:
    """A :class:`~mapi_sdk.collection.BlobSource` over one span of one part in memory."""

    def __init__(self, pack: str, start: int, data: bytes) -> None:
        self.pack, self.start, self.data = pack, start, data

    def read(self, pack: str, offset: int, size: int) -> bytes:
        lo = offset - self.start
        if pack != self.pack or lo < 0 or lo + size > len(self.data):
            raise MapiError("an entry is outside its span", code=ErrorCode.INTEGRITY_FAILURE)
        return self.data[lo : lo + size]


def _spans(entries: list[PartEntry]) -> list[list[PartEntry]]:
    """Entries (by offset) grouped into reads of at most :data:`_SPAN_BYTES`."""
    out: list[list[PartEntry]] = []
    current: list[PartEntry] = []
    start = 0
    for e in sorted(entries, key=lambda e: e.location.offset):
        end = e.location.offset + e.location.stored
        if current and end - start > _SPAN_BYTES:
            out.append(current)
            current = []
        if not current:
            start = e.location.offset
        current.append(e)
    if current:
        out.append(current)
    return out


def _header(data: bytes, sha: str, limits: IngestLimits, pointer: str) -> ImageHeader:
    try:
        return image_header(data, max_pixels=limits.max_image_pixels)
    except ImageDecodeError as exc:
        raise problem(
            exc.code,
            f"image {sha} cannot be read: {exc.detail}",
            pointer=pointer,
            action="Remove the image, or save it again as PNG or JPEG with 8 bits per channel.",
            untrusted=exc.untrusted_text,
        ) from None


def _index_part(
    ctx: WorkerContext,
    i: int,
    limits: IngestLimits,
    found: dict[str, _Image],
) -> tuple[str, SignedGet, int, set[str]]:
    """Index and check part ``i``; adds its images to ``found`` (first part wins). Returns
    ``(digest, signed GET, entries, image sha256)``."""
    name = part_input(i)
    pointer = f"{_POINTER}/part_upload_ids/{i}"
    get = ctx.start.inputs.get(name)
    if get is None:
        raise MapiError(
            f"the run needs an input named {name!r}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/inputs/{name}",
        )
    if get.md5_hash is None:
        raise MapiError(
            f"part {i} has no MD5, so it cannot be stored", code=ErrorCode.INTEGRITY_FAILURE
        )
    digest = pack_digest(get.size, get.crc32c, get.md5_hash)
    check_pack(digest, get)
    transfers = ctx.transfers

    def read(offset: int, n: int) -> bytes:
        return transfers.read_range(get, offset, offset + n)

    try:
        entries = index_part(read, get.size, pack=digest)
    except CollectionError as exc:
        raise problem(
            exc.code,
            f"part {i}: {exc.detail}",
            pointer=pointer,
            action="Make the parts again. 'mapi datasets upload-images' makes valid parts.",
            untrusted=exc.untrusted_text,
        ) from None
    shas: set[str] = set()
    for span in _spans(entries):
        ctx.cancel.check()
        start = span[0].location.offset
        end = max(e.location.offset + e.location.stored for e in span)
        source = _SpanSource(
            digest, start, transfers.read_range(get, start, end) if end > start else b""
        )
        for e in span:
            try:
                data = read_blob(source, e.location, sha256=e.sha256)
            except MapiError as exc:
                raise problem(
                    exc.code,
                    f"part {i}: {exc.detail}",
                    pointer=pointer,
                    action="Make the part again from the image files.",
                    untrusted=exc.untrusted_text,
                ) from None
            header = _header(data, e.sha256, limits, pointer)
            if e.sha256 not in found:
                found[e.sha256] = _Image(e.location, header, "upload")
            shas.add(e.sha256)
    return digest, get, len(entries), shas


# ==================================================================================== base


def _base_images(collection: Collection, found: dict[str, _Image]) -> None:
    """Add the base version's images that the parts do not hold."""
    df = collection.manifest
    for row in df.itertuples(index=False):
        sha = str(row.sha256)
        if sha in found:
            continue
        loc = BlobLocation(
            str(row.pack),
            int(row.offset),
            int(row.stored),
            int(row.method),
            int(row.crc32),
            int(row.nbytes),
        )
        fmt = str(row.format)
        header = ImageHeader(
            format=fmt if fmt in ("png", "jpeg", "tiff") else "png",  # type: ignore[arg-type]
            width=int(row.width),
            height=int(row.height),
            mode=str(row.mode),
            colour=str(row.mode) not in ("1", "L", "LA", "I", "I;16", "F"),
        )
        found[sha] = _Image(loc, header, "base")


# ================================================================================== output


def _missing_error(missing: list[str]) -> MapiError:
    listed = ", ".join(missing[:_MAX_LISTED]) + (" ..." if len(missing) > _MAX_LISTED else "")
    return problem(
        ErrorCode.SCHEMA_INVALID,
        f"{len(missing):,} images of the manifest are not in the parts or in the base version",
        pointer=f"{_POINTER}/part_upload_ids",
        action="Upload the missing images. 'mapi datasets upload-images' uploads only what is "
        "missing.",
        untrusted=listed,
    )


def _classes(labels: list[str]) -> tuple[list[ClassCount], int]:
    counts = Counter(v for v in labels if v)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    listed = [ClassCount(label=k[:256], count=v) for k, v in ordered[:MAX_COLLECTION_CLASSES]]
    return listed, max(0, len(ordered) - MAX_COLLECTION_CLASSES)


def _modal_size(sizes: list[tuple[int, int]]) -> tuple[int, int]:
    (h, w), _ = Counter(sizes).most_common(1)[0]
    return min(h, _EXPLORE_SIDE), min(w, _EXPLORE_SIDE)


def _manifest_frame(rows: _Rows, found: Mapping[str, _Image]) -> pd.DataFrame:
    images = [found[s] for s in rows.shas]
    data: dict[str, Any] = {
        "image_id": rows.ids,
        "sha256": rows.shas,
        "pack": [im.location.pack for im in images],
        "offset": [im.location.offset for im in images],
        "stored": [im.location.stored for im in images],
        "method": [im.location.method for im in images],
        "crc32": [im.location.crc32 for im in images],
        "nbytes": [im.location.nbytes for im in images],
        "format": [im.header.format for im in images],
        "width": [im.header.width for im in images],
        "height": [im.header.height for im in images],
        "mode": [im.header.mode for im in images],
    }
    if "label" in rows.coords:
        data[LABEL_VAR] = rows.coords["label"]
    for name in ("split", "path"):
        if name in rows.coords:
            data[name] = rows.coords[name]
    df = pd.DataFrame(data)
    for name, series in rows.columns.items():
        df[name] = series.to_numpy()
    return df


def _view_schema(schema: DataSchema, info: CollectionInfo) -> DataSchema:
    """``schema`` without the virtual pixel variable and the dims only it uses (what the
    manifest dataset has)."""
    vars_ = {k: v for k, v in schema.vars.items() if k != info.var}
    used = {d for v in vars_.values() for d in v.dims} | {info.dim}
    dims = {k: v for k, v in schema.dims.items() if k in used}
    return schema.model_copy(update={"vars": vars_, "dims": dims})


def run_collection_ingest(ctx: WorkerContext, job: IngestJob) -> DataSchema:
    """Ingest a collection (see the module docstring); returns the version's schema."""
    from mapi_runner.worker.explore import write_data_explorer
    from mapi_runner.worker.ingest import preview, profile

    coll = job.collection
    assert isinstance(coll, CollectionIngest)
    opts = coll.options
    limits = IngestLimits.from_job(job)
    # (1) the client manifest
    ctx.state(RunnerPhase.LOAD_INPUTS, "reading the manifest")
    client = _read_client_manifest(ctx, limits)
    rows = _rows(client, opts)
    # (2) the parts
    ctx.state(RunnerPhase.VALIDATE, "checking the image parts")
    found: dict[str, _Image] = {}
    parts: list[tuple[str, SignedGet, int, set[str]]] = []
    for i in range(coll.parts):
        ctx.progress(0.8 * i / max(1, coll.parts), f"checking part {i + 1} of {coll.parts}")
        parts.append(_index_part(ctx, i, limits, found))
    # (3) the base version
    base: Collection | None = None
    if coll.base_version is not None:
        ctx.progress(0.8, "reading the base version")
        base = load_collection(ctx, "base", manifest=BASE_MANIFEST_INPUT)
        _base_images(base, found)
    # (4) every row has its image
    missing = sorted({s for s in rows.shas if s not in found})
    if missing:
        raise _missing_error(missing)
    # (5) the packs this version reads
    distinct = sorted(set(rows.shas))
    per_pack = Counter(found[s].location.pack for s in distinct)
    notes: list[str] = []
    packs: list[PackInfo] = []
    gets: dict[str, SignedGet] = {}
    for i, (digest, get, entries, _shas) in enumerate(parts):
        if not per_pack.get(digest):
            notes.append(f"Part {i} holds no image of the manifest. The version does not use it.")
            continue
        if digest in gets:
            continue  # the same bytes uploaded two times
        assert get.md5_hash is not None
        gets[digest] = get
        packs.append(
            PackInfo(
                digest=digest,
                size=get.size,
                crc32c=get.crc32c,
                md5=get.md5_hash,
                entries=entries,
                images=per_pack[digest],
                source="upload",
                input=part_input(i),
            )
        )
    if base is not None:
        for i, p in enumerate(base.info.packs):
            if per_pack.get(p.digest) and p.digest not in gets:
                gets[p.digest] = ctx.start.inputs[pack_input(i, "base")]
                packs.append(
                    p.model_copy(
                        update={"images": per_pack[p.digest], "source": "base", "input": None}
                    )
                )
    if len(packs) > MAX_VERSION_PACKS:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the version reads {len(packs)} packs. The maximum is {MAX_VERSION_PACKS}",
            pointer=f"{_POINTER}/base_version",
            action="Upload all images again without a base version (--no-base).",
        )
    # (6) the outputs
    ctx.progress(0.85, "writing the manifest")
    df = _manifest_frame(rows, found)
    raw = write_manifest(df)
    headers = [found[s].header for s in distinct]
    sizes = [(h.height, h.width) for h in headers]
    mode = (
        opts.mode if opts.mode != "auto" else ("rgb" if any(h.colour for h in headers) else "gray")
    )
    labels = rows.coords.get("label")
    classes, other = _classes(labels) if labels is not None else ([], 0)
    splits = Counter(rows.coords["split"]) if "split" in rows.coords else Counter()
    if len(splits) > _MAX_SPLITS:
        raise _err(
            f"the split column has {len(splits)} different values. The maximum is {_MAX_SPLITS}",
            f"{_OPTIONS}/split_column",
            action="Use a split column with values such as train, val and test.",
        )
    info = CollectionInfo(
        n_images=len(rows.ids),
        dim=opts.dim,
        var=opts.var,
        label=labels is not None,
        split="split" in rows.coords,
        classes=classes,
        other_classes=other,
        splits={k: splits[k] for k in sorted(splits)},
        mode=mode,
        size=opts.size or (sizes[0] if len(set(sizes)) == 1 else None),
        min_size=(min(h for h, _ in sizes), min(w for _, w in sizes)),
        max_size=(max(h for h, _ in sizes), max(w for _, w in sizes)),
        formats=dict(sorted(Counter(h.format for h in headers).items())),
        total_bytes=sum(found[s].location.nbytes for s in distinct),
        packs=packs,
        new_images=sum(1 for s in distinct if found[s].source == "upload"),
        reused_images=sum(1 for s in distinct if found[s].source == "base"),
        base_version=coll.base_version if any(p.source == "base" for p in packs) else None,
        columns=list(rows.columns),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )
    collection = Collection(info, df, SignedPackSource(ctx, gets))
    schema = collection.schema
    assert schema is not None
    ctx.write_output(COLLECTION_MANIFEST, raw)
    ctx.write_output(COLLECTION_MEMBERS, members_bin(distinct))
    ctx.write_output(COLLECTION_INFO, info.model_dump_json(indent=2).encode())
    ctx.write_output(INGEST_SCHEMA, schema.model_dump_json(indent=2).encode())
    mds = collection.manifest_dataset()
    view_schema = _view_schema(schema, info)
    issues = [
        Issue(code=ErrorCode.SCHEMA_INVALID, severity=Severity.INFO, pointer=_POINTER, message=m)
        for m in notes
    ]
    for m in notes:
        ctx.log(m)
    ctx.write_output(
        "summary.json", data_summary(mds, view_schema).model_dump_json(indent=2).encode()
    )
    ctx.write_json("profile.json", {**profile(mds, view_schema, issues, raw), "collection": True})
    ctx.write_json("preview.json", preview(mds, job.preview_rows))
    ctx.write_output("columns.json", write_profiles(column_profiles(client)))
    if coll.explore_samples:
        ctx.progress(0.9, "decoding a sample for the Data Explorer")
        try:
            size = info.size or _modal_size(sizes)
            sample = collection.loader().to_xarray(coll.explore_samples, size, seed=0)
        except MapiError as exc:
            ctx.warning(
                "EXPLORE_FAILED", "the Data Explorer sample was not decoded", exc.detail[:8000]
            )
        else:
            write_data_explorer(
                ctx, sample, schema, ref=job.dataset or "dataset://runner/upload@latest"
            )
    ctx.progress(1.0, "collection ready", force=True)
    return schema
