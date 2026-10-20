"""Image collections (v0.15): large image sets as a manifest plus content-addressed packs.

A collection dataset version holds ``manifest.parquet`` (one row per image: id, sha256,
where its bytes are, size, label, split, user columns), ``collection.json``
(:class:`CollectionInfo`), ``members.bin`` (the sorted raw sha256 digests) and the usual
``schema.json``/``summary.json``/``profile.json``/``preview.json``/explorer views. Image bytes
live in **packs**: STORE/DEFLATE zip files whose entries are named ``<sha256>.<ext>``, kept
once per tenant at :func:`pack_object_name` and shared by every version that references
them.

Packs are addressed by :func:`pack_digest` over the *server-computed* size, CRC32C and MD5
of the uploaded part (the same basis as a dataset version's content address); no client
hash is trusted. Every image is still identified by its sha256, which the ingest run and
every loader read verify.

Pure Pydantic: the control plane imports this module; parsing happens in sandboxed runs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Annotated, Final, Literal

from pydantic import Field, field_validator, model_validator

from mapi_spec._dto import Id, RequestModel
from mapi_spec._types import SHA256_HEX_RE, Name, Sha256Hex, StrictModel, unique
from mapi_spec.ingest import MAX_IMAGES, RESERVED_IMAGE_DIMS, ColumnRef
from mapi_spec.media import MAX_IMAGE_SIDE
from mapi_spec.storage import CRC32C_B64_RE, MD5_B64_RE, Crc32c, Md5, check_segment

__all__ = [
    "BASE_INFO_INPUT",
    "BASE_MANIFEST_INPUT",
    "COLLECTION_FORMAT",
    "COLLECTION_INFO",
    "COLLECTION_MANIFEST",
    "COLLECTION_MEMBERS",
    "DEFAULT_PART_BYTES",
    "IMAGE_FORMATS",
    "INGEST_MANIFEST_INPUT",
    "MANIFEST_FIXED",
    "MANIFEST_RESERVED",
    "MAX_BLOB_QUERY",
    "MAX_COLLECTION_CLASSES",
    "MAX_COLLECTION_IMAGES",
    "MAX_COLLECTION_PARTS",
    "MAX_MANIFEST_BYTES",
    "MAX_PART_BYTES",
    "MAX_PART_ENTRIES",
    "MAX_VERSION_PACKS",
    "PACK_OBJECT_PREFIX",
    "PART_ENTRY_RE",
    "ClassCount",
    "CollectionInfo",
    "CollectionIngest",
    "CollectionOptions",
    "CollectionRead",
    "CollectionSource",
    "PackInfo",
    "info_input",
    "pack_digest",
    "pack_input",
    "pack_object_name",
    "part_input",
]

COLLECTION_FORMAT: Final = "mapi.collection/v1"
COLLECTION_MANIFEST: Final = "manifest.parquet"
"""Version object: one row per image (:data:`MANIFEST_FIXED` columns first)."""
COLLECTION_INFO: Final = "collection.json"
"""Version object: the :class:`CollectionInfo` JSON."""
COLLECTION_MEMBERS: Final = "members.bin"
"""Version object: sorted unique raw 32-byte sha256 digests (answers ``blobs/missing``)."""
PACK_OBJECT_PREFIX: Final = "pack/"
"""``DatasetVersion.objects`` keys ``pack/<digest>`` name the packs of a version."""

MAX_COLLECTION_IMAGES: Final = 1_000_000
MAX_COLLECTION_PARTS: Final = 64
"""New part uploads per version."""
MAX_PART_BYTES: Final = 2 * 1024**3 - 64 * 1024**2
"""Parts stay below the ZIP64 limits (ZIP64 records are refused)."""
MAX_PART_ENTRIES: Final = 60_000
DEFAULT_PART_BYTES: Final = 256 * 1024**2
MAX_VERSION_PACKS: Final = 128
MAX_BLOB_QUERY: Final = 5000
MAX_COLLECTION_CLASSES: Final = 1000
MAX_MANIFEST_BYTES: Final = 256 * 1024**2
"""Size cap of the client manifest upload."""

PART_ENTRY_RE: Final = r"^[0-9a-f]{64}\.(png|jpg|jpeg|tif|tiff)$"
"""Entry names of a part (and of a pack): ``<sha256 of the bytes>.<ext>``."""
IMAGE_FORMATS: Final = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "tif": "tiff", "tiff": "tiff"}
"""Entry suffix → image format."""

MANIFEST_FIXED: Final[tuple[tuple[str, str], ...]] = (
    ("image_id", "str"),
    ("sha256", "str"),
    ("pack", "str"),
    ("offset", "int64"),
    ("stored", "int64"),
    ("method", "int8"),
    ("crc32", "int64"),
    ("nbytes", "int64"),
    ("format", "str"),
    ("width", "int32"),
    ("height", "int32"),
    ("mode", "str"),
)
"""``(column, parquet dtype)`` every ``manifest.parquet`` starts with, in order."""
MANIFEST_RESERVED: Final = frozenset(name for name, _ in MANIFEST_FIXED)
"""Column names a client manifest may not use for its own columns."""

INGEST_MANIFEST_INPUT: Final = "manifest"
BASE_MANIFEST_INPUT: Final = "base.manifest"
BASE_INFO_INPUT: Final = "base.info"

_IO_NAME: Final = r"^[a-z][a-z0-9_.\-]{0,63}$"  # = mapi_spec.run.IoName (not imported: cycle)

_IoName = Annotated[str, Field(pattern=_IO_NAME)]
_Size = tuple[int, int]


def part_input(i: int) -> str:
    """Ingest input name of part ``i`` (``part.0000``)."""
    if not 0 <= i < MAX_COLLECTION_PARTS:
        raise ValueError(f"part index must be 0..{MAX_COLLECTION_PARTS - 1}")
    return f"part.{i:04d}"


def pack_input(i: int, prefix: str = "dataset") -> str:
    """Run input name of pack ``i`` of a collection (``dataset.pack.0000``, ``base.pack.0000``).

    ``i`` is the pack's index in :attr:`CollectionInfo.packs`.
    """
    if not 0 <= i < MAX_VERSION_PACKS:
        raise ValueError(f"pack index must be 0..{MAX_VERSION_PACKS - 1}")
    return f"{prefix}.pack.{i:04d}"


def info_input(prefix: str = "dataset") -> str:
    """Run input name of the ``collection.json`` of a collection input (``dataset.info``)."""
    return f"{prefix}.info"


def pack_digest(size: int, crc32c: str, md5: str) -> str:
    """Content address of a pack: sha256 hex of ``canonical_json({size, crc32c, md5})``.

    The same basis as the server's ``dataset_sha`` (the store's own digests of the pinned
    object generation). ``ValueError`` when ``crc32c``/``md5`` are not the base64 forms GCS
    reports or ``size`` is negative.
    """
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("size must be a non-negative int")
    if not isinstance(crc32c, str) or not re.match(CRC32C_B64_RE, crc32c):
        raise ValueError("crc32c must be the base64 of a 4-byte checksum")
    if not isinstance(md5, str) or not re.match(MD5_B64_RE, md5):
        raise ValueError("md5 must be the base64 of a 16-byte digest")
    try:
        base64.b64decode(crc32c, validate=True)
        base64.b64decode(md5, validate=True)
    except binascii.Error as exc:  # pragma: no cover - the patterns already exclude it
        raise ValueError(str(exc)) from None
    # canonical_json of a flat {int, str, str} mapping (mapi_spec.loader.canonical_json gives
    # the same text; not imported here to keep this module free of the loader's imports)
    basis = json.dumps(
        {"size": size, "crc32c": crc32c, "md5": md5},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def pack_object_name(tenant_id: str, digest: str) -> str:
    """``t/{tenant}/blobs/packs/{digest}.zip`` in the data bucket."""
    if not re.match(SHA256_HEX_RE, digest):
        raise ValueError("pack digest must be 64 lowercase hex characters")
    return f"t/{check_segment(tenant_id, 'tenant_id')}/blobs/packs/{digest}.zip"


def _check_size(v: _Size | None) -> _Size | None:
    if v is not None and not all(1 <= n <= MAX_IMAGE_SIDE for n in v):
        raise ValueError(f"size (height, width) must be 1..{MAX_IMAGE_SIDE} each")
    return v


# ===================================================================== client → server


class CollectionOptions(RequestModel):
    """How a client manifest and image parts become a collection version.

    The manifest is a CSV, TSV or Parquet table with one row per image and at least the
    sha256 of each image's bytes. Its other columns become the id, label, split and path of
    each image, or are kept as coords.
    """

    sha256_column: ColumnRef = Field("sha256", description="Lowercase hex sha256 of each image")
    id_column: ColumnRef | None = Field(
        None, description="Unique ids of the image dim (default: the path, else the sha256)"
    )
    path_column: ColumnRef | None = Field(
        "path", description="Kept as the coord 'path' (ignored when the default is missing)"
    )
    label_column: ColumnRef | None = Field(
        None, description="Class labels: the text variable 'label(image)', role target"
    )
    split_column: ColumnRef | None = Field(
        None, description="Train/test assignment: the text coord 'split(image)'"
    )
    columns: list[ColumnRef] | None = Field(
        None, max_length=64, description="Other columns kept as coords (default: all others)"
    )
    var: Name = "pixels"
    dim: Name = "image"
    mode: Literal["auto", "gray", "rgb"] = Field(
        "auto", description="auto: rgb when an image has colour, else gray"
    )
    size: _Size | None = Field(
        None, description="Canonical (height, width) for samples and model defaults; each 1..4096"
    )
    resize: Literal["stretch", "crop", "pad"] = "stretch"
    max_images: int = Field(100_000, ge=1, le=MAX_COLLECTION_IMAGES)

    _size = field_validator("size")(_check_size)

    @field_validator("dim")
    @classmethod
    def _dim(cls, v: str) -> str:
        if v in RESERVED_IMAGE_DIMS or v in ("y", "x", "channel"):
            raise ValueError(
                f"dim {v!r} is reserved; use another name for the image dim (such as 'image')"
            )
        return v

    @model_validator(mode="after")
    def _columns(self) -> CollectionOptions:
        named = [
            c
            for c in (
                self.sha256_column,
                self.id_column,
                self.path_column,
                self.label_column,
                self.split_column,
            )
            if c is not None
        ]
        if dups := unique(named):
            raise ValueError(f"a manifest column has two roles: {dups}")
        if self.var == self.dim:
            raise ValueError("var and dim need different names")
        if reserved := sorted(set(self.columns or ()) & MANIFEST_RESERVED):
            raise ValueError(f"columns use reserved manifest names: {reserved}")
        return self


class CollectionSource(RequestModel):
    """``DatasetVersionCreate.collection``: a manifest upload plus the parts it needs.

    ``base_version``: ``None`` uses the latest READY collection version of the dataset
    (when there is one) as the source of images the parts do not hold; ``0`` uses no base.
    """

    manifest_upload_id: Id
    part_upload_ids: list[Id] = Field(default_factory=list, max_length=MAX_COLLECTION_PARTS)
    base_version: int | None = Field(None, ge=0)
    options: CollectionOptions = Field(default_factory=CollectionOptions)

    @model_validator(mode="after")
    def _uploads(self) -> CollectionSource:
        if dups := unique(self.part_upload_ids):
            raise ValueError(f"a part upload may be used once: {dups}")
        if self.manifest_upload_id in self.part_upload_ids:
            raise ValueError("the manifest upload cannot also be a part")
        return self

    @property
    def upload_ids(self) -> list[str]:
        """The manifest upload, then the parts in order."""
        return [self.manifest_upload_id, *self.part_upload_ids]


# ============================================================================ runner side


class CollectionIngest(StrictModel):
    """``IngestJob.collection``: what the sandboxed collection ingest run gets.

    Inputs: ``manifest``, ``part.0000`` .. ``part.{parts-1}`` and, with a base version,
    ``base.manifest``, ``base.info`` and ``base.pack.0000`` .. (index = base info order).
    """

    options: CollectionOptions
    parts: int = Field(0, ge=0, le=MAX_COLLECTION_PARTS)
    base_version: int | None = None
    base_packs: int = Field(0, ge=0, le=MAX_VERSION_PACKS)
    explore_samples: int = Field(2000, ge=0, le=20_000)

    @model_validator(mode="after")
    def _base(self) -> CollectionIngest:
        if self.base_version is None and self.base_packs:
            raise ValueError("base_packs needs a base_version")
        return self

    @property
    def input_names(self) -> list[str]:
        """Every input name this job reads (the manifest first)."""
        names = [INGEST_MANIFEST_INPUT, *(part_input(i) for i in range(self.parts))]
        if self.base_version is not None:
            names += [BASE_MANIFEST_INPUT, BASE_INFO_INPUT]
            names += [pack_input(i, "base") for i in range(self.base_packs)]
        return names


class PackInfo(StrictModel):
    """One pack a collection version reads."""

    digest: Sha256Hex
    size: int = Field(..., ge=22)
    crc32c: Crc32c
    md5: Md5
    entries: int = Field(..., ge=0, description="Zip entries in the pack")
    images: int = Field(..., ge=0, description="Distinct images of this version stored in it")
    source: Literal["upload", "base"]
    input: _IoName | None = Field(
        None, description="Uploads: the ingest input it came from ('part.0003'); base: none"
    )


class ClassCount(StrictModel):
    label: str = Field(..., max_length=256)
    count: int = Field(..., ge=0)


class CollectionInfo(StrictModel):
    """``collection.json``: what a collection version holds."""

    format: Literal["mapi.collection/v1"] = COLLECTION_FORMAT
    n_images: int = Field(..., ge=1, le=MAX_COLLECTION_IMAGES)
    dim: Name
    var: Name
    label: bool = Field(..., description="The version has label(image)")
    split: bool = Field(..., description="The version has split(image)")
    classes: list[ClassCount] = Field(
        default_factory=list,
        max_length=MAX_COLLECTION_CLASSES,
        description="Image count per class, by count (descending) then label",
    )
    other_classes: int = Field(0, ge=0, description="Classes not listed in classes")
    splits: dict[str, int] = Field(default_factory=dict, max_length=64)
    mode: Literal["gray", "rgb"] = Field(..., description="Channels the images decode to")
    size: _Size | None = Field(
        ..., description="options.size, else the common size of all images, else none"
    )
    min_size: _Size
    max_size: _Size
    formats: dict[str, int] = Field(default_factory=dict, max_length=8)
    total_bytes: int = Field(..., ge=0, description="Stored bytes of the distinct images")
    packs: list[PackInfo] = Field(..., min_length=1, max_length=MAX_VERSION_PACKS)
    new_images: int = Field(..., ge=0)
    reused_images: int = Field(..., ge=0)
    base_version: int | None = None
    columns: list[str] = Field(default_factory=list, max_length=64)
    manifest_sha256: Sha256Hex

    _size = field_validator("size")(_check_size)

    @model_validator(mode="after")
    def _consistent(self) -> CollectionInfo:
        if dups := unique(p.digest for p in self.packs):
            raise ValueError(f"packs listed twice: {dups}")
        # Manifest rows with the same bytes share one stored image: the packs hold the
        # distinct images (new + reused), which can be fewer than n_images.
        distinct = self.new_images + self.reused_images
        if distinct > self.n_images:
            raise ValueError("more distinct images than n_images")
        if sum(p.images for p in self.packs) < distinct:
            raise ValueError("the packs hold fewer images than the distinct images")
        for p in self.packs:
            if (p.source == "upload") != (p.input is not None):
                raise ValueError("upload packs name their input; base packs do not")
        if self.base_version is None and any(p.source == "base" for p in self.packs):
            raise ValueError("base packs need a base_version")
        if dups := unique(c.label for c in self.classes):
            raise ValueError(f"classes listed twice: {dups}")
        return self


# ============================================================================ model side


class CollectionRead(StrictModel):
    """How a model that reads datasets sees a collection: a seeded sample as
    ``pixels(image, y, x, channel)`` (``ModelSpec`` ``data.collection``)."""

    max_samples: int = Field(5000, ge=1, le=MAX_IMAGES)
    size: _Size | None = Field(
        None,
        description="(height, width); default: the collection's size (needed when it has none)",
    )
    resize: Literal["stretch", "crop", "pad"] = "stretch"
    mode: Literal["auto", "gray", "rgb"] = "auto"
    stratify: Name | None = Field(
        "label", description="Keep the share of each level of this var or coord (none: uniform)"
    )
    seed: int | None = Field(None, ge=0, lt=2**32, description="Default: the fit seed")

    _size = field_validator("size")(_check_size)
