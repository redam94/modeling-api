"""Image collections on the client side (v0.15): scan a folder, plan deterministic parts.

A collection version is made from a **manifest** (one row per image with the sha256 of its
bytes, its path and its columns) and **parts**: zip files of the images the server does
not have yet. The client hashes every image, asks the server which sha256 are missing
(``datasets.missing_images``) and packs only those. Parts are byte-identical when they are
built again from the same images (entries ``<sha256>.<ext>`` sorted by sha256, STORE, fixed
DOS timestamps, no extra fields), so an interrupted upload restarts from a state file and
skips the parts that already arrived.

Standard library only (``hashlib``, ``zipfile``, ``csv``); a Parquet manifest needs
``pyarrow``.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from mapi_spec.collection import (
    DEFAULT_PART_BYTES,
    IMAGE_FORMATS,
    MANIFEST_RESERVED,
    MAX_COLLECTION_IMAGES,
    MAX_COLLECTION_PARTS,
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    MAX_PART_ENTRIES,
)

__all__ = [
    "CollectionPlan",
    "ImageFile",
    "PartFile",
    "UploadState",
    "client_manifest_csv",
    "file_sha256",
    "parse_part_size",
    "plan_parts",
    "scan_images",
    "write_parts",
]

ZIP_DATE: Final = (1980, 1, 1, 0, 0, 0)
"""The DOS timestamp of every part entry (the earliest a zip can hold)."""
_LOCAL_HEADER: Final = 30
_CENTRAL_HEADER: Final = 46
_END_RECORD: Final = 22
_HASH_CHUNK: Final = 1024 * 1024
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_SIZE: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$", re.IGNORECASE)
_UNITS: Final = {
    "": 1,
    "b": 1,
    **{f"{u}b": 1000 ** (i + 1) for i, u in enumerate("kmgt")},
    **{f"{u}ib": 1024 ** (i + 1) for i, u in enumerate("kmgt")},
    **{u: 1024 ** (i + 1) for i, u in enumerate("kmgt")},
}


def file_sha256(path: Path) -> str:
    """Lowercase hex sha256 of a file's bytes (read in 1 MiB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def parse_part_size(text: str) -> int:
    """``"256MB"``, ``"1GiB"``, ``"64m"`` or a plain byte count as bytes (``KB``/``MB``/``GB``
    are powers of 1000; ``KiB``/``MiB``/``GiB`` and the bare letters ``k``/``m``/``g`` are
    powers of 1024)."""
    m = _SIZE.match(text)
    unit = (m.group(2) or "").lower() if m is not None else ""
    if m is None or unit not in _UNITS:
        raise ValueError(f"{text!r} is not a size (examples: 256MB, 1GiB, 1048576)")
    return int(float(m.group(1)) * _UNITS[unit])


@dataclass(frozen=True, slots=True)
class ImageFile:
    """One image of a plan (a manifest row)."""

    path: Path
    """The file on disk."""
    rel: str
    """Its path relative to the root (POSIX separators): the manifest's path column."""
    sha256: str
    ext: str
    """Lowercase suffix without the dot (``png``, ``jpg``, ...)."""
    size: int


@dataclass(slots=True)
class CollectionPlan:
    """The images of a folder (or of a manifest) with their sha256 and manifest columns."""

    root: Path
    images: list[ImageFile]
    """Manifest order: one per row (the same bytes may be in several rows)."""
    columns: list[str]
    """Columns of the client manifest, in order (``path`` and ``sha256`` first)."""
    rows: list[dict[str, str]]
    """Manifest rows (all values as text), aligned with :attr:`images`."""
    path_column: str = "path"
    label_column: str | None = None

    @property
    def shas(self) -> list[str]:
        """Distinct sha256 of the plan, sorted."""
        return sorted({im.sha256 for im in self.images})

    @property
    def by_sha(self) -> dict[str, ImageFile]:
        """The first image of each distinct sha256."""
        out: dict[str, ImageFile] = {}
        for im in self.images:
            out.setdefault(im.sha256, im)
        return out

    @property
    def total_bytes(self) -> int:
        """Bytes of the distinct images."""
        return sum(im.size for im in self.by_sha.values())


@dataclass(frozen=True, slots=True)
class PartFile:
    """A part zip written by :func:`write_parts`."""

    index: int
    path: Path
    sha256: str
    """sha256 of the part bytes (the state file compares it on resume)."""
    size: int
    shas: tuple[str, ...]
    """The image sha256 in the part, sorted."""


@dataclass(slots=True)
class UploadState:
    """What :meth:`datasets.upload_collection` keeps in its state file between attempts."""

    dataset_id: str
    manifest_sha256: str
    manifest_upload_id: str | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    """``{index, sha256, size, upload_id, received}`` per part."""

    def to_json(self) -> str:
        doc = {
            "dataset_id": self.dataset_id,
            "manifest_sha256": self.manifest_sha256,
            "manifest_upload_id": self.manifest_upload_id,
            "parts": self.parts,
        }
        return json.dumps(doc, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: Path, *, dataset_id: str, manifest_sha256: str) -> UploadState:
        """The state in ``path`` when it belongs to this dataset and manifest, else a new one
        (a missing or unreadable file starts over)."""
        fresh = cls(dataset_id, manifest_sha256)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fresh
        if (
            not isinstance(doc, dict)
            or doc.get("dataset_id") != dataset_id
            or doc.get("manifest_sha256") != manifest_sha256
        ):
            return fresh
        parts = doc.get("parts")
        upload = doc.get("manifest_upload_id")
        return cls(
            dataset_id,
            manifest_sha256,
            upload if isinstance(upload, str) else None,
            [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else [],
        )

    def save(self, path: Path) -> None:
        """Write atomically (a crash never leaves half a state file)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    def part(self, index: int, sha256: str) -> dict[str, Any] | None:
        """The saved entry of part ``index`` when it holds the same bytes."""
        for p in self.parts:
            if p.get("index") == index and p.get("sha256") == sha256:
                return p
        return None

    def set_part(self, entry: dict[str, Any]) -> None:
        self.parts = [p for p in self.parts if p.get("index") != entry["index"]]
        self.parts.append(entry)
        self.parts.sort(key=lambda p: int(p.get("index", 0)))


# =================================================================================== scan


def _ext(name: str) -> str | None:
    suffix = PurePosixPath(name).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_FORMATS else None


def _safe_rel(text: str) -> PurePosixPath:
    rel = PurePosixPath(text.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts):
        raise ValueError(f"manifest path {text!r} must be relative and stay inside the folder")
    return rel


def _walk(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in names:
            if name.startswith(".") or _ext(name) is None:
                continue
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _read_manifest(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"the manifest {path} is larger than {MAX_MANIFEST_BYTES} bytes")
    suffix = path.suffix.lower()
    if suffix in (".parquet", ".pq"):
        if importlib.util.find_spec("pyarrow") is None:
            raise ImportError("a Parquet manifest needs pyarrow (or give a CSV manifest)")
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        columns = [str(c) for c in table.column_names]
        data = table.to_pydict()
        n = table.num_rows
        rows = [{c: _text(data[c][i]) for c in columns} for i in range(n)]
        return columns, rows
    delimiter = "\t" if suffix == ".tsv" else ","
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        columns = [str(c) for c in reader.fieldnames or []]
        rows = [{c: (r.get(c) or "") for c in columns} for r in reader]
    return columns, rows


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def scan_images(
    root: Path,
    manifest: Path | None = None,
    *,
    path_column: str = "path",
    label_from_folder: bool = False,
    label_column: str = "label",
    max_images: int = MAX_COLLECTION_IMAGES,
) -> CollectionPlan:
    """Hash the images under ``root`` into a :class:`CollectionPlan`.

    Without ``manifest`` every PNG/JPEG/TIFF file under ``root`` (hidden files and folders
    skipped, symlinks not followed) is one row, sorted by path. With ``manifest`` (CSV, TSV
    or Parquet) its rows name the images by ``path_column`` (relative to ``root``); its
    other columns are kept. ``label_from_folder`` sets the ``label`` column to the first
    folder of each path. A ``sha256`` column of the manifest must match the bytes.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a folder")
    if manifest is not None:
        columns, given = _read_manifest(Path(manifest).expanduser())
        if path_column not in columns:
            raise ValueError(f"the manifest has no {path_column!r} column (columns: {columns})")
    else:
        columns, given = [path_column], [{path_column: rel} for rel in _walk(root)]
    if not given:
        raise ValueError(f"no images found in {root} (PNG, JPEG or TIFF files)")
    if len(given) > max_images:
        raise ValueError(f"{len(given)} images is more than the maximum of {max_images}")
    images: list[ImageFile] = []
    rows: list[dict[str, str]] = []
    resolved = root.resolve()
    for i, row in enumerate(given):
        rel = _safe_rel(row[path_column])
        ext = _ext(rel.name)
        if ext is None:
            raise ValueError(f"row {i}: {rel} is not a PNG, JPEG or TIFF file name")
        path = root.joinpath(*rel.parts)
        if not path.is_file():
            raise FileNotFoundError(f"row {i}: {path} does not exist")
        if not path.resolve().is_relative_to(resolved):
            raise ValueError(f"row {i}: {rel} points outside {root}")
        sha = file_sha256(path)
        claimed = row.get("sha256", "").strip().lower()
        if claimed and claimed != sha:
            raise ValueError(f"row {i}: the sha256 column of {rel} does not match its bytes")
        out = dict(row)
        out[path_column] = rel.as_posix()
        out["sha256"] = sha
        if label_from_folder:
            if len(rel.parts) < 2:
                raise ValueError(
                    f"{rel} is not in a folder: with label_from_folder every image must be "
                    "in a folder named after its class"
                )
            out[label_column] = rel.parts[0]
        images.append(ImageFile(path, rel.as_posix(), sha, ext, path.stat().st_size))
        rows.append(out)
    order = [path_column, "sha256"]
    if label_from_folder:
        order.append(label_column)
    names = order + [c for c in columns if c not in order]
    reserved = sorted((set(names) - {"sha256"}) & MANIFEST_RESERVED)
    if reserved:
        raise ValueError(f"manifest columns use reserved names: {reserved}")
    return CollectionPlan(
        root,
        images,
        names,
        rows,
        path_column,
        label_column if label_from_folder or label_column in names else None,
    )


def client_manifest_csv(plan: CollectionPlan) -> bytes:
    """The client manifest as CSV (UTF-8, ``\\n`` line ends): the plan's columns and rows."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf, fieldnames=plan.columns, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in plan.rows:
        writer.writerow({c: row.get(c, "") for c in plan.columns})
    data = buf.getvalue().encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError(f"the manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    return data


# ================================================================================== parts


def _entry_cost(name: str, size: int) -> int:
    return _LOCAL_HEADER + _CENTRAL_HEADER + 2 * len(name) + size


def plan_parts(
    plan: CollectionPlan, missing: Iterable[str], part_bytes: int = DEFAULT_PART_BYTES
) -> list[list[str]]:
    """The sha256 of each part: the missing images sorted by sha256, filled in order up to
    ``part_bytes`` (zip headers counted) and :data:`MAX_PART_ENTRIES` entries."""
    if not 1024 <= part_bytes <= MAX_PART_BYTES:
        raise ValueError(f"the part size must be 1024..{MAX_PART_BYTES} bytes")
    by_sha = plan.by_sha
    want = sorted(set(missing))
    unknown = [s for s in want if s not in by_sha]
    if unknown:
        raise ValueError(f"{len(unknown)} missing sha256 are not in the plan: {unknown[:3]}")
    parts: list[list[str]] = []
    current: list[str] = []
    used = _END_RECORD
    for sha in want:
        im = by_sha[sha]
        cost = _entry_cost(f"{sha}.{im.ext}", im.size)
        if _END_RECORD + cost > MAX_PART_BYTES:
            raise ValueError(f"{im.rel} is too large for a part ({im.size} bytes)")
        if current and (used + cost > part_bytes or len(current) >= MAX_PART_ENTRIES):
            parts.append(current)
            current, used = [], _END_RECORD
        current.append(sha)
        used += cost
    if current:
        parts.append(current)
    if len(parts) > MAX_COLLECTION_PARTS:
        raise ValueError(
            f"the images need {len(parts)} parts (the maximum is {MAX_COLLECTION_PARTS}): "
            "use a larger part size"
        )
    return parts


def _write_part(path: Path, plan: CollectionPlan, shas: Sequence[str]) -> None:
    by_sha = plan.by_sha
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as zf:
        for sha in shas:
            im = by_sha[sha]
            info = zipfile.ZipInfo(f"{sha}.{im.ext}", date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 0
            info.external_attr = 0
            data = im.path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError(f"{im.rel} changed since it was hashed")
            zf.writestr(info, data)
    os.replace(tmp, path)


def write_parts(
    plan: CollectionPlan,
    missing: Iterable[str],
    out_dir: Path,
    part_bytes: int = DEFAULT_PART_BYTES,
) -> list[PartFile]:
    """Write the parts of the missing images as ``part-0000.zip`` ... in ``out_dir``.

    Deterministic: the same images and part size give byte-identical files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list[PartFile] = []
    for i, shas in enumerate(plan_parts(plan, missing, part_bytes)):
        path = out_dir / f"part-{i:04d}.zip"
        _write_part(path, plan, shas)
        out.append(PartFile(i, path, file_sha256(path), path.stat().st_size, tuple(shas)))
    return out


def check_sha256(values: Iterable[str]) -> list[str]:
    """``values`` when each is a lowercase hex sha256 (else ``ValueError``)."""
    out = list(values)
    bad = [v for v in out if not _SHA256.match(v)]
    if bad:
        raise ValueError(f"not a lowercase hex sha256: {bad[0]!r}")
    return out
