"""Object storage contracts (plan §8.6): metadata, the ``ObjectStore`` protocol, signed URLs,
integrity hashes and the bucket/key naming scheme.

The control plane talks to object storage only through :class:`ObjectStore`. Two
implementations exist outside this package: ``ShimObjectStore`` (the local GCS emulator in
``mapi_gcs_shim``) and ``GcsObjectStore`` (google-cloud-storage). Both expose GCS semantics:

* every write creates a new **generation**; ``if_generation_match=0`` means "only if the
  object does not exist" (write-once);
* the *server* computes **CRC32C** (base64 of the big-endian 4-byte value) and **MD5**
  (base64 of the 16-byte digest) — uploader-supplied hashes are never evidence;
* signed **GET** URLs are pinned to a generation and honour HTTP ``Range``; signed **PUT**
  URLs and V4 **POST policies** (key prefix + ``content-length-range``) are how sandboxes
  write, so the executor never holds storage credentials.

Object keys are always built server-side from validated IDs with the helpers at the bottom
of this module (never from user-supplied paths).
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from mapi_spec._types import StrictModel
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "BUCKET_RE",
    "BUNDLE_OBJECTS",
    "DATASET_OBJECTS",
    "MAX_SIGNED_URL_TTL",
    "SEGMENT_RE",
    "AccessDenied",
    "BucketName",
    "BucketPurpose",
    "Buckets",
    "Crc32c",
    "Md5",
    "NotFound",
    "ObjectMeta",
    "ObjectName",
    "ObjectPage",
    "ObjectStore",
    "PostPolicy",
    "PreconditionFailed",
    "SignedGet",
    "SignedPut",
    "StorageError",
    "bootstrap_key",
    "bucket_name",
    "bundle_prefix",
    "check_object_name",
    "check_segment",
    "check_ttl",
    "crc32c",
    "crc32c_from_base64",
    "crc32c_to_base64",
    "dataset_prefix",
    "md5_base64",
    "quarantine_key",
    "run_checkpoint_prefix",
    "run_prefix",
]

# ------------------------------------------------------------------------------- hash types

CRC32C_B64_RE = r"^[A-Za-z0-9+/]{6}==$"
MD5_B64_RE = r"^[A-Za-z0-9+/]{22}==$"

Crc32c = Annotated[
    str,
    StringConstraints(pattern=CRC32C_B64_RE),
    Field(
        description="CRC32C as GCS reports it: base64 of the big-endian 4-byte checksum",
        examples=["4waSgw=="],
    ),
]
Md5 = Annotated[
    str,
    StringConstraints(pattern=MD5_B64_RE),
    Field(description="MD5 as GCS reports it: base64 of the 16-byte digest"),
]

_CRC32C_POLY = 0x82F63B78  # Castagnoli, reflected


def _make_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ _CRC32C_POLY if c & 1 else c >> 1
        table.append(c)
    return tuple(table)


_CRC32C_TABLE = _make_table()
_ACCEL_THRESHOLD = 64 * 1024


def _accelerated() -> Callable[[int, bytes], int] | None:
    """``google_crc32c.extend`` when the optional C extension is installed."""
    try:
        import google_crc32c  # type: ignore[import-not-found,unused-ignore]
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    extend: Callable[[int, bytes], int] = google_crc32c.extend
    return extend


def crc32c(data: bytes | bytearray | memoryview, value: int = 0) -> int:
    """CRC32C (Castagnoli) of ``data``, continuing from ``value`` (for chunked hashing).

    Pure Python; payloads above 64 KiB use ``google_crc32c`` when it is installed.
    """
    buf = bytes(data)
    if len(buf) >= _ACCEL_THRESHOLD:
        ext = _accelerated()
        if ext is not None:
            return ext(value, buf)
    crc = value ^ 0xFFFFFFFF
    table = _CRC32C_TABLE
    for b in buf:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def crc32c_to_base64(value: int) -> str:
    """GCS wire form of a CRC32C value (base64 of 4 big-endian bytes)."""
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("crc32c must be an unsigned 32-bit integer")
    return base64.b64encode(value.to_bytes(4, "big")).decode("ascii")


def crc32c_from_base64(text: str) -> int:
    """Inverse of :func:`crc32c_to_base64`."""
    if not re.match(CRC32C_B64_RE, text):
        raise ValueError("not a base64 CRC32C (expected 8 characters ending in '==')")
    return int.from_bytes(base64.b64decode(text), "big")


def md5_base64(data: bytes | bytearray | memoryview) -> str:
    """GCS wire form of the MD5 of ``data``."""
    import hashlib

    return base64.b64encode(hashlib.md5(bytes(data), usedforsecurity=False).digest()).decode()


# ------------------------------------------------------------------------- names and keys

BUCKET_RE = r"^[a-z0-9][a-z0-9_.\-]{1,61}[a-z0-9]$"
SEGMENT_RE = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$"
_SEGMENT = re.compile(SEGMENT_RE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

BucketName = Annotated[str, StringConstraints(pattern=BUCKET_RE)]


def check_object_name(name: str) -> str:
    """Validate an object name: 1–1024 UTF-8 bytes, no control characters, no leading ``/``,
    no empty, ``.`` or ``..`` segments (so keys can never escape a prefix)."""
    if not name or len(name.encode("utf-8")) > 1024:
        raise ValueError("object names must be 1-1024 bytes of UTF-8")
    if _CONTROL.search(name):
        raise ValueError("object names must not contain control characters")
    if name.startswith("/"):
        raise ValueError("object names must not start with '/'")
    parts = name.split("/")
    # a trailing '/' (prefix-like names) is allowed; interior empty segments are not
    for part in parts[:-1] if name.endswith("/") else parts:
        if part in ("", ".", ".."):
            raise ValueError("object names must not contain empty, '.' or '..' segments")
    return name


ObjectName = Annotated[str, Field(min_length=1, max_length=1024)]


def check_segment(value: str, what: str = "id") -> str:
    """Validate one key segment built from an ID (``[A-Za-z0-9][A-Za-z0-9_.-]{0,127}``)."""
    if not _SEGMENT.match(value) or ".." in value:
        raise ValueError(f"{what} {value!r} is not a safe storage key segment")
    return value


# ------------------------------------------------------------------------------ metadata


class ObjectMeta(StrictModel):
    """Metadata-only view of one object generation (what ``objects.get`` returns).

    ``crc32c`` and ``md5_hash`` are computed by the storage service, never by the uploader.
    ``md5_hash`` is absent for composite objects (GCS semantics).
    """

    bucket: BucketName
    name: ObjectName
    generation: int = Field(..., ge=1)
    metageneration: int = Field(1, ge=1)
    size: int = Field(..., ge=0)
    crc32c: Crc32c
    md5_hash: Md5 | None = None
    content_type: str | None = Field(None, max_length=256)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=64)
    time_created: AwareDatetime

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "bucket": "mapi-local-runs",
                    "name": "t/default/runs/run_01jabc/a1/bundle.zarr.zip",
                    "generation": 1760500000123456,
                    "metageneration": 1,
                    "size": 1048576,
                    "crc32c": "4waSgw==",
                    "md5_hash": "XrY7u+Ae7tCTyyK7j1rNww==",
                    "content_type": "application/zip",
                    "metadata": {},
                    "time_created": "2026-10-15T09:30:00Z",
                }
            ]
        }
    )

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_object_name(v)

    @classmethod
    def from_gcs_resource(cls, resource: Mapping[str, Any]) -> ObjectMeta:
        """Build from a GCS JSON-API object resource (``generation``/``size`` are strings)."""
        return cls(
            bucket=resource["bucket"],
            name=resource["name"],
            generation=int(resource["generation"]),
            metageneration=int(resource.get("metageneration", 1)),
            size=int(resource["size"]),
            crc32c=resource["crc32c"],
            md5_hash=resource.get("md5Hash"),
            content_type=resource.get("contentType"),
            metadata=dict(resource.get("metadata") or {}),
            time_created=resource["timeCreated"],
        )

    def to_gcs_resource(self) -> dict[str, Any]:
        """The GCS JSON-API shape (inverse of :meth:`from_gcs_resource`)."""
        out: dict[str, Any] = {
            "kind": "storage#object",
            "bucket": self.bucket,
            "name": self.name,
            "generation": str(self.generation),
            "metageneration": str(self.metageneration),
            "size": str(self.size),
            "crc32c": self.crc32c,
            "timeCreated": self.time_created.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        if self.md5_hash is not None:
            out["md5Hash"] = self.md5_hash
        if self.content_type is not None:
            out["contentType"] = self.content_type
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    def same_content(self, other: ObjectMeta) -> bool:
        """Size and server-computed hashes agree (generation and location may differ)."""
        return (
            self.size == other.size
            and self.crc32c == other.crc32c
            and (self.md5_hash is None or other.md5_hash is None or self.md5_hash == other.md5_hash)
        )


class ObjectPage(StrictModel):
    """One page of :meth:`ObjectStore.list`."""

    items: list[ObjectMeta] = Field(default_factory=list)
    prefixes: list[str] = Field(default_factory=list, description="With a delimiter only")
    next_page_token: str | None = None


# ----------------------------------------------------------------------------- signed URLs

MAX_SIGNED_URL_TTL = timedelta(days=7)
"""V4 signatures cannot outlive 7 days."""


def check_ttl(expires_in: timedelta) -> timedelta:
    """Reject non-positive TTLs and TTLs above the V4 maximum."""
    if expires_in <= timedelta(0) or expires_in > MAX_SIGNED_URL_TTL:
        raise ValueError("signed URL TTL must be > 0 and <= 7 days")
    return expires_in


class SignedGet(StrictModel):
    """A generation-pinned signed GET URL plus the server-computed facts about its object.

    The URL honours HTTP ``Range``, so zarr zip readers can range-read one object. A reader
    verifies ``size``/``crc32c`` after a full download (corruption check only — integrity is
    enforced by the control plane).
    """

    method: Literal["GET"] = "GET"
    url: str = Field(..., min_length=8, max_length=8192)
    expires_at: AwareDatetime
    generation: int = Field(..., ge=1)
    size: int = Field(..., ge=0)
    crc32c: Crc32c
    md5_hash: Md5 | None = None
    content_type: str | None = Field(None, max_length=256)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "method": "GET",
                    "url": "http://127.0.0.1:4443/download/storage/v1/b/mapi-local-data/o/"
                    "t%2Fdefault%2Fdatasets%2Fab12%2Fdata.zarr.zip?generation=17&"
                    "X-Goog-Signature=...",
                    "expires_at": "2026-10-15T10:30:00Z",
                    "generation": 17,
                    "size": 52428800,
                    "crc32c": "4waSgw==",
                    "content_type": "application/zip",
                }
            ]
        }
    )


class SignedPut(StrictModel):
    """A signed PUT URL for exactly one object name.

    ``headers`` must be sent verbatim (they are part of the signature): typically
    ``content-type``, ``x-goog-if-generation-match: 0`` (write-once) and
    ``x-goog-content-length-range: 0,<max_bytes>``.
    """

    method: Literal["PUT"] = "PUT"
    url: str = Field(..., min_length=8, max_length=8192)
    name: ObjectName
    expires_at: AwareDatetime
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    max_bytes: int | None = Field(None, ge=0)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "method": "PUT",
                    "url": "http://127.0.0.1:4443/upload/storage/v1/b/mapi-local-runs/o/"
                    "t%2Fdefault%2Fruns%2Frun_01jabc%2Fa1%2Fmanifest.json?X-Goog-Signature=...",
                    "name": "t/default/runs/run_01jabc/a1/manifest.json",
                    "expires_at": "2026-10-15T10:30:00Z",
                    "headers": {
                        "content-type": "application/json",
                        "x-goog-if-generation-match": "0",
                        "x-goog-content-length-range": "0,1048576",
                    },
                    "max_bytes": 1048576,
                }
            ]
        }
    )

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_object_name(v)


class PostPolicy(StrictModel):
    """A V4 POST policy: multipart form uploads of any key under ``prefix``.

    The uploader posts ``multipart/form-data`` to ``url`` with every entry of ``fields``,
    then ``key`` (which must start with ``prefix``), then the ``file`` part last. The storage
    service rejects bodies outside ``content_length_range`` and keys outside the prefix.

    A ``create_only`` policy cannot replace an existing object (its signed conditions carry
    ``x-goog-if-generation-match: 0``), so the holder can neither overwrite the write-once
    manifest nor swap an output after it was written.
    """

    method: Literal["POST"] = "POST"
    url: str = Field(..., min_length=8, max_length=8192)
    prefix: ObjectName
    fields: dict[str, str] = Field(default_factory=dict, max_length=32)
    content_length_range: tuple[int, int]
    expires_at: AwareDatetime
    create_only: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "method": "POST",
                    "url": "http://127.0.0.1:4443/mapi-local-runs",
                    "prefix": "t/default/runs/run_01jabc/a1/",
                    "fields": {
                        "policy": "eyJjb25kaXRpb25zIjpbXX0=",
                        "x-goog-algorithm": "GOOG4-HMAC-SHA256",
                        "x-goog-credential": "local/20261015/auto/storage/goog4_request",
                        "x-goog-date": "20261015T093000Z",
                        "x-goog-signature": "abcdef0123",
                    },
                    "content_length_range": [0, 1073741824],
                    "expires_at": "2026-10-15T10:30:00Z",
                }
            ]
        }
    )

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        check_object_name(v)
        if not v.endswith("/"):
            raise ValueError("a POST policy prefix must end with '/'")
        return v

    @model_validator(mode="after")
    def _range(self) -> PostPolicy:
        lo, hi = self.content_length_range
        if lo < 0 or hi < lo:
            raise ValueError("content_length_range must be (min, max) with 0 <= min <= max")
        return self

    def allows(self, key: str, size: int) -> bool:
        """Whether an upload of ``size`` bytes to ``key`` satisfies this policy."""
        lo, hi = self.content_length_range
        try:
            check_object_name(key)
        except ValueError:
            return False
        return key.startswith(self.prefix) and key != self.prefix and lo <= size <= hi


# --------------------------------------------------------------------------------- errors


class StorageError(MapiError):
    """Base class for object-store failures (carries ``bucket``/``name`` when known)."""

    default_code = ErrorCode.INFRA_ERROR

    def __init__(
        self, detail: str, *, bucket: str | None = None, name: str | None = None, **kw: Any
    ) -> None:
        super().__init__(detail, **kw)
        self.bucket = bucket
        self.name = name


class NotFound(StorageError):
    """The bucket, object or generation does not exist."""

    default_code = ErrorCode.NOT_FOUND


class PreconditionFailed(StorageError):
    """An ``if_generation_match`` (or ``if_metageneration_match``) precondition failed."""

    default_code = ErrorCode.PRECONDITION_FAILED


class AccessDenied(StorageError):
    """The principal may not perform this operation (e.g. listing the bootstrap bucket)."""

    default_code = ErrorCode.FORBIDDEN


# -------------------------------------------------------------------------------- protocol


@runtime_checkable
class ObjectStore(Protocol):
    """Async object-store driver used by the control plane (GCS semantics).

    Conventions shared by every method:

    * ``generation`` pins the *source* generation to read (``None`` = live generation);
    * ``if_generation_match`` is a precondition on the *target*: ``0`` means "must not
      exist", ``n`` means "live generation must be ``n``"; a failed precondition raises
      :class:`PreconditionFailed`;
    * missing buckets/objects raise :class:`NotFound`; denied operations raise
      :class:`AccessDenied`.
    """

    async def put(
        self,
        bucket: str,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        if_generation_match: int | None = None,
    ) -> ObjectMeta:
        """Upload ``data`` as a new generation and return its server-computed metadata."""
        ...

    async def get(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        byte_range: tuple[int, int | None] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """Download (a byte range of) an object. ``byte_range=(start, end_inclusive|None)``.

        ``max_bytes`` caps the body: larger objects raise ``PAYLOAD_TOO_LARGE`` before any
        byte is read (the control plane only ever reads small JSON sidecars).
        """
        ...

    async def stat(self, bucket: str, name: str, *, generation: int | None = None) -> ObjectMeta:
        """Metadata-only ``objects.get``; never downloads content."""
        ...

    async def list(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        page_token: str | None = None,
        max_results: int = 1000,
    ) -> ObjectPage:
        """List live objects under ``prefix`` in lexicographic name order."""
        ...

    async def delete(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        """Delete an object (or one generation). Retention-locked objects raise AccessDenied."""
        ...

    async def copy(
        self,
        src_bucket: str,
        src_name: str,
        dst_bucket: str,
        dst_name: str,
        *,
        source_generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> ObjectMeta:
        """Server-side copy; hashes are recomputed by the service, bytes never leave it.

        Finalisation pins ``source_generation`` to the verified generation and uses
        ``if_generation_match=0`` so bundle objects are write-once.
        """
        ...

    async def sign_get(
        self, bucket: str, name: str, *, expires_in: timedelta, generation: int | None = None
    ) -> SignedGet:
        """Signed GET pinned to ``generation`` (default: the live one); Range requests work."""
        ...

    async def sign_put(
        self,
        bucket: str,
        name: str,
        *,
        expires_in: timedelta,
        content_type: str | None = None,
        max_bytes: int | None = None,
        if_generation_match: int | None = 0,
    ) -> SignedPut:
        """Signed PUT for exactly ``name`` (write-once unless ``if_generation_match=None``)."""
        ...

    async def post_policy(
        self,
        bucket: str,
        prefix: str,
        *,
        expires_in: timedelta,
        max_bytes: int,
        min_bytes: int = 0,
        create_only: bool = False,
    ) -> PostPolicy:
        """V4 POST policy allowing uploads of any key under ``prefix`` within the size range.

        ``create_only`` uploads may not replace an existing object (whatever the signer's
        own permissions)."""
        ...


# ------------------------------------------------------------------------- bucket scheme


class BucketPurpose(StrEnum):
    """The five buckets of the data project (plan §8.6)."""

    QUARANTINE = "quarantine"  # raw uploads; 7-day lifecycle
    BOOTSTRAP = "bootstrap"  # RunSpec objects keyed by nonce; exec SA get+delete, no list
    RUNS = "runs"  # per-attempt scratch, written only via signed URLs
    BUNDLES = "bundles"  # verified, write-once bundles; exec SA has no access
    DATA = "data"  # content-addressed dataset versions


_ENV_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")


def bucket_name(env: str, purpose: BucketPurpose | str) -> str:
    """``mapi-{env}-{purpose}``, e.g. ``mapi-local-runs``."""
    if not _ENV_RE.match(env):
        raise ValueError(f"environment name {env!r} must match {_ENV_RE.pattern}")
    return f"mapi-{env}-{BucketPurpose(purpose).value}"


@dataclass(frozen=True, slots=True)
class Buckets:
    """All bucket names for one environment (``Buckets('local').runs == 'mapi-local-runs'``)."""

    env: str

    def __post_init__(self) -> None:
        bucket_name(self.env, BucketPurpose.RUNS)  # validates env

    @property
    def quarantine(self) -> str:
        return bucket_name(self.env, BucketPurpose.QUARANTINE)

    @property
    def bootstrap(self) -> str:
        return bucket_name(self.env, BucketPurpose.BOOTSTRAP)

    @property
    def runs(self) -> str:
        return bucket_name(self.env, BucketPurpose.RUNS)

    @property
    def bundles(self) -> str:
        return bucket_name(self.env, BucketPurpose.BUNDLES)

    @property
    def data(self) -> str:
        return bucket_name(self.env, BucketPurpose.DATA)

    def all(self) -> dict[BucketPurpose, str]:
        return {p: bucket_name(self.env, p) for p in BucketPurpose}


_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def quarantine_key(tenant_id: str, upload_id: str) -> str:
    """``t/{tenant}/uploads/{upload_id}`` in the quarantine bucket."""
    return f"t/{check_segment(tenant_id, 'tenant_id')}/uploads/{check_segment(upload_id)}"


def bootstrap_key(nonce: str) -> str:
    """``{nonce}`` in the bootstrap bucket (the 256-bit nonce as 64 lowercase hex chars)."""
    if not _NONCE_RE.match(nonce):
        raise ValueError("nonce must be 64 lowercase hex characters (256 bits)")
    return nonce


def run_prefix(tenant_id: str, run_id: str, attempt: int, task_index: int | None = None) -> str:
    """``t/{tenant}/runs/{run}/a{n}/[tasks/{i}/]`` in the runs bucket (always ends in ``/``)."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    out = f"t/{check_segment(tenant_id, 'tenant_id')}/runs/{check_segment(run_id, 'run_id')}/"
    out += f"a{attempt}/"
    if task_index is not None:
        if task_index < 0:
            raise ValueError("task_index must be >= 0")
        out += f"tasks/{task_index}/"
    return out


def run_checkpoint_prefix(
    tenant_id: str, run_id: str, attempt: int, task_index: int | None = None
) -> str:
    """``{run_prefix}checkpoints/``: where segmented sampling writes resumable state."""
    return run_prefix(tenant_id, run_id, attempt, task_index) + "checkpoints/"


def bundle_prefix(tenant_id: str, model_id: str, version: int) -> str:
    """``t/{tenant}/models/{model}/{version}/`` in the bundles bucket."""
    if version < 1:
        raise ValueError("model versions start at 1")
    return (
        f"t/{check_segment(tenant_id, 'tenant_id')}/models/"
        f"{check_segment(model_id, 'model_id')}/{version}/"
    )


def dataset_prefix(tenant_id: str, sha256_hex: str) -> str:
    """``t/{tenant}/datasets/{sha}/`` in the data bucket (content-addressed)."""
    if not _SHA_RE.match(sha256_hex):
        raise ValueError("dataset sha must be 64 lowercase hex characters")
    return f"t/{check_segment(tenant_id, 'tenant_id')}/datasets/{sha256_hex}/"


DATASET_OBJECTS: tuple[str, ...] = ("data.zarr.zip", "schema.json", "profile.json", "preview.json")
"""Objects of one dataset version under :func:`dataset_prefix`."""

BUNDLE_OBJECTS: tuple[str, ...] = (
    "bundle.zarr.zip",
    "manifest.json",
    "manifest.sig",
    "diagnostics.json",
    "plots.json",
    "metrics.json",
)
"""Well-known top-level objects of a bundle (plus ``ui/*`` views) under :func:`bundle_prefix`."""
