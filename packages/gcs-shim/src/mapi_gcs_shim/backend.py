"""Filesystem backend: buckets, object generations, metadata sidecars, atomic writes.

Storage layout under ``{root}/buckets/{bucket}/``::

    bucket.json                     bucket record (retention, lifecycle, creation time)
    objects/{h[:2]}/{h}.json        live-generation metadata sidecar (the commit point)
    objects/{h[:2]}/{h}.{gen}.bin   object bytes for that generation

``h`` is the SHA-256 of the object name, so **no user-supplied string ever becomes a path
component** (bucket names are validated against the GCS bucket grammar). Writes are staged
in ``{root}/tmp`` while CRC32C and MD5 are computed, renamed into place, and committed by
atomically replacing the sidecar; a crash at any point leaves either the old or the new
generation, and :meth:`FsBackend.__init__` sweeps the debris.

Only the live generation of an object is kept (a bucket without object versioning): reading
an older generation is a 404, exactly like GCS.

Thread-safety: every mutation runs under one lock; the HTTP layer calls these synchronous
methods from worker threads. One server process owns a root (an exclusive ``flock``).
"""

from __future__ import annotations

import base64
import builtins
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from bisect import bisect_left, insort
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, BinaryIO, Final

import google_crc32c
from mapi_spec.storage import ObjectMeta, check_object_name, crc32c_to_base64

from mapi_gcs_shim._errors import ShimError
from mapi_gcs_shim.config import CONFIG_DIR, BucketConfig, atomic_write, check_bucket_name

__all__ = [
    "MAX_LIST_RESULTS",
    "NO_PRECONDITIONS",
    "BucketRecord",
    "FsBackend",
    "Preconditions",
    "StagedUpload",
    "StoredObject",
]

MAX_LIST_RESULTS: Final = 1000
MAX_METADATA_BYTES: Final = 8 * 1024
MAX_METADATA_ENTRIES: Final = 64
_CHUNK: Final = 1024 * 1024
LOCK_FILE: Final = "shim.lock"


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def validate_name(name: str) -> str:
    """Object-name guard (400): GCS limits plus no empty/``.``/``..`` segments."""
    try:
        return check_object_name(name)
    except ValueError as exc:
        raise ShimError.bad_request(
            f"invalid object name: {exc}", xml_code="InvalidArgument"
        ) from exc


def validate_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    """Custom metadata limits: at most 64 entries and 8 KiB in total, string values."""
    out: dict[str, str] = {}
    total = 0
    for k, v in metadata.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k:
            raise ShimError.bad_request("custom metadata must map non-empty strings to strings")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in k + v):
            raise ShimError.bad_request("custom metadata must not contain control characters")
        total += len(k.encode()) + len(v.encode())
        out[k] = v
    if len(out) > MAX_METADATA_ENTRIES or total > MAX_METADATA_BYTES:
        raise ShimError.bad_request("custom metadata exceeds 64 entries or 8 KiB")
    return out


def validate_content_type(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > 256 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ShimError.bad_request("invalid content type")
    return value


# ------------------------------------------------------------------------------ records


@dataclass(frozen=True, slots=True)
class BucketRecord:
    """A bucket and its policies."""

    name: str
    time_created: datetime
    retention_period_seconds: int | None = None
    retention_locked: bool = False
    lifecycle_age_seconds: int | None = None
    metageneration: int = 1

    @classmethod
    def from_config(cls, cfg: BucketConfig, now: datetime) -> BucketRecord:
        return cls(
            name=cfg.name,
            time_created=now,
            retention_period_seconds=cfg.retention_period_seconds,
            retention_locked=cfg.retention_locked,
            lifecycle_age_seconds=cfg.lifecycle_age_seconds,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "time_created": _iso(self.time_created),
            "retention_period_seconds": self.retention_period_seconds,
            "retention_locked": self.retention_locked,
            "lifecycle_age_seconds": self.lifecycle_age_seconds,
            "metageneration": self.metageneration,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> BucketRecord:
        return cls(
            name=check_bucket_name(str(data["name"])),
            time_created=_parse_iso(str(data["time_created"])),
            retention_period_seconds=data.get("retention_period_seconds"),
            retention_locked=bool(data.get("retention_locked", False)),
            lifecycle_age_seconds=data.get("lifecycle_age_seconds"),
            metageneration=int(data.get("metageneration", 1)),
        )

    def resource(self) -> dict[str, Any]:
        """GCS JSON-API bucket resource."""
        out: dict[str, Any] = {
            "kind": "storage#bucket",
            "id": self.name,
            "name": self.name,
            "timeCreated": _iso(self.time_created),
            "updated": _iso(self.time_created),
            "metageneration": str(self.metageneration),
            "location": "LOCAL",
            "storageClass": "STANDARD",
            "etag": base64.b64encode(f"{self.name}:{self.metageneration}".encode()).decode(),
        }
        if self.retention_period_seconds is not None:
            out["retentionPolicy"] = {
                "retentionPeriod": str(self.retention_period_seconds),
                "effectiveTime": _iso(self.time_created),
                "isLocked": self.retention_locked,
            }
        if self.lifecycle_age_seconds is not None:
            out["lifecycle"] = {
                "rule": [
                    {
                        "action": {"type": "Delete"},
                        "condition": {"age": -(-self.lifecycle_age_seconds // 86400)},
                    }
                ]
            }
        return out


@dataclass(frozen=True, slots=True)
class StoredObject:
    """The live generation of one object, as persisted in its metadata sidecar."""

    bucket: str
    name: str
    generation: int
    metageneration: int
    size: int
    crc32c: str
    md5_hash: str
    content_type: str | None
    metadata: dict[str, str]
    time_created: datetime
    updated: datetime
    retention_expires: datetime | None = None

    def to_meta(self) -> ObjectMeta:
        return ObjectMeta(
            bucket=self.bucket,
            name=self.name,
            generation=self.generation,
            metageneration=self.metageneration,
            size=self.size,
            crc32c=self.crc32c,
            md5_hash=self.md5_hash,
            content_type=self.content_type,
            metadata=dict(self.metadata),
            time_created=self.time_created,
        )

    def etag(self) -> str:
        return base64.b64encode(f"{self.generation}:{self.metageneration}".encode()).decode()

    def goog_hash(self) -> str:
        return f"crc32c={self.crc32c},md5={self.md5_hash}"

    def resource(self, base_url: str | None = None) -> dict[str, Any]:
        """GCS JSON-API object resource (numbers as strings, like GCS)."""
        from urllib.parse import quote

        out: dict[str, Any] = {
            "kind": "storage#object",
            "id": f"{self.bucket}/{self.name}/{self.generation}",
            "bucket": self.bucket,
            "name": self.name,
            "generation": str(self.generation),
            "metageneration": str(self.metageneration),
            "size": str(self.size),
            "crc32c": self.crc32c,
            "md5Hash": self.md5_hash,
            "etag": self.etag(),
            "storageClass": "STANDARD",
            "timeCreated": _iso(self.time_created),
            "updated": _iso(self.updated),
        }
        if self.content_type is not None:
            out["contentType"] = self.content_type
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        if self.retention_expires is not None:
            out["retentionExpirationTime"] = _iso(self.retention_expires)
        if base_url is not None:
            b, o = quote(self.bucket, safe=""), quote(self.name, safe="")
            out["selfLink"] = f"{base_url}/storage/v1/b/{b}/o/{o}"
            out["mediaLink"] = (
                f"{base_url}/download/storage/v1/b/{b}/o/{o}?generation={self.generation}&alt=media"
            )
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "name": self.name,
            "generation": self.generation,
            "metageneration": self.metageneration,
            "size": self.size,
            "crc32c": self.crc32c,
            "md5_hash": self.md5_hash,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "time_created": _iso(self.time_created),
            "updated": _iso(self.updated),
            "retention_expires": (
                None if self.retention_expires is None else _iso(self.retention_expires)
            ),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> StoredObject:
        retention = data.get("retention_expires")
        return cls(
            bucket=str(data["bucket"]),
            name=str(data["name"]),
            generation=int(data["generation"]),
            metageneration=int(data["metageneration"]),
            size=int(data["size"]),
            crc32c=str(data["crc32c"]),
            md5_hash=str(data["md5_hash"]),
            content_type=data.get("content_type"),
            metadata=dict(data.get("metadata") or {}),
            time_created=_parse_iso(str(data["time_created"])),
            updated=_parse_iso(str(data["updated"])),
            retention_expires=None if retention is None else _parse_iso(str(retention)),
        )


@dataclass(frozen=True, slots=True)
class Preconditions:
    """GCS generation/metageneration preconditions on the *target* object."""

    if_generation_match: int | None = None
    if_generation_not_match: int | None = None
    if_metageneration_match: int | None = None
    if_metageneration_not_match: int | None = None

    def check(self, live: StoredObject | None) -> None:
        """Raise 412 when a precondition does not hold for ``live`` (``None`` = absent)."""
        gen = live.generation if live is not None else 0
        if self.if_generation_match is not None and gen != self.if_generation_match:
            raise ShimError.precondition(
                f"ifGenerationMatch={self.if_generation_match} but the live generation is {gen}"
            )
        if self.if_generation_not_match is not None and gen == self.if_generation_not_match:
            raise ShimError.precondition(
                f"ifGenerationNotMatch={self.if_generation_not_match} matches the live generation"
            )
        if self.if_metageneration_match is not None and (
            live is None or live.metageneration != self.if_metageneration_match
        ):
            raise ShimError.precondition("ifMetagenerationMatch does not hold")
        if self.if_metageneration_not_match is not None and (
            live is not None and live.metageneration == self.if_metageneration_not_match
        ):
            raise ShimError.precondition("ifMetagenerationNotMatch does not hold")


NO_PRECONDITIONS: Final = Preconditions()


# ------------------------------------------------------------------------------ staging


class StagedUpload:
    """Bytes being received into a temp file while CRC32C and MD5 are computed.

    ``max_bytes`` is enforced on every :meth:`write` (so oversized bodies are rejected while
    streaming, never buffered).
    """

    def __init__(self, directory: Path, *, max_bytes: int) -> None:
        fd, path = tempfile.mkstemp(dir=directory, prefix="upload-", suffix=".part")
        self.path = Path(path)
        self._fh: IO[bytes] | None = os.fdopen(fd, "wb")
        self.max_bytes = max_bytes
        self.size = 0
        self._crc = google_crc32c.Checksum()  # type: ignore[no-untyped-call]
        self._md5 = hashlib.md5(usedforsecurity=False)
        self._finished = False

    def write(self, chunk: bytes) -> None:
        if self._fh is None:
            raise RuntimeError("staged upload is closed")
        if self.size + len(chunk) > self.max_bytes:
            raise ShimError.too_large(
                f"the object exceeds the maximum size of {self.max_bytes} bytes"
            )
        self._fh.write(chunk)
        self._crc.update(chunk)  # type: ignore[no-untyped-call]
        self._md5.update(chunk)
        self.size += len(chunk)

    def finish(self) -> tuple[int, str, str]:
        """Flush and fsync; return ``(size, crc32c_b64, md5_b64)``."""
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
        self._finished = True
        digest: bytes = self._crc.digest()  # type: ignore[no-untyped-call]
        crc = crc32c_to_base64(int.from_bytes(digest, "big"))
        md5 = base64.b64encode(self._md5.digest()).decode()
        return self.size, crc, md5

    def discard(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def __enter__(self) -> StagedUpload:
        return self

    def __exit__(self, *_: object) -> None:
        self.discard()


# ------------------------------------------------------------------------------ backend


@dataclass(slots=True)
class _Bucket:
    record: BucketRecord
    objects: dict[str, StoredObject] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)

    def put(self, obj: StoredObject) -> None:
        if obj.name not in self.objects:
            insort(self.names, obj.name)
        self.objects[obj.name] = obj

    def remove(self, name: str) -> None:
        del self.objects[name]
        idx = bisect_left(self.names, name)
        if idx < len(self.names) and self.names[idx] == name:
            del self.names[idx]


def _object_id(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _encode_token(start: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"s": start}).encode()).decode().rstrip("=")


def _decode_token(token: str) -> str:
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        start = data["s"]
        if not isinstance(start, str):
            raise TypeError
        return start
    except (ValueError, KeyError, TypeError) as exc:
        raise ShimError.bad_request("invalid pageToken") from exc


def _after_prefix(prefix: str) -> str:
    """The smallest string greater than every string starting with ``prefix``."""
    last = prefix[-1]
    if ord(last) >= 0x10FFFF:  # pragma: no cover - not reachable with real delimiters
        return prefix + "\x00"
    return prefix[:-1] + chr(ord(last) + 1)


class FsBackend:
    """Thread-safe filesystem object store with GCS generation semantics."""

    def __init__(self, root: Path, *, lock: bool = True) -> None:
        self.root = Path(root)
        self._buckets_dir = self.root / "buckets"
        self._tmp = self.root / "tmp"
        for d in (self._buckets_dir, self._tmp, self.root / CONFIG_DIR):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._lock_fh: IO[bytes] | None = None
        if lock:
            self._acquire_root_lock()
        self._buckets: dict[str, _Bucket] = {}
        self._last_generation = 0
        self._load()

    # ---------------------------------------------------------------- lifecycle

    def _acquire_root_lock(self) -> None:
        fh = open(self.root / CONFIG_DIR / LOCK_FILE, "a+b")  # noqa: SIM115 - held for life
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise RuntimeError(
                f"another GCS shim already serves {self.root} (could not lock {LOCK_FILE})"
            ) from exc
        self._lock_fh = fh

    def close(self) -> None:
        """Release the root lock (idempotent)."""
        if self._lock_fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    def __enter__(self) -> FsBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _load(self) -> None:
        """Rebuild the in-memory index from disk and remove debris left by a crash."""
        for leftover in self._tmp.iterdir():
            with contextlib.suppress(OSError):
                if leftover.is_dir():
                    shutil.rmtree(leftover)
                else:
                    leftover.unlink()
        for bdir in sorted(self._buckets_dir.iterdir()):
            record_path = bdir / "bucket.json"
            if not bdir.is_dir() or not record_path.exists():
                continue
            record = BucketRecord.from_json(json.loads(record_path.read_bytes()))
            if record.name != bdir.name:
                raise RuntimeError(f"bucket record {record_path} names {record.name!r}")
            bucket = _Bucket(record=record)
            objects_dir = bdir / "objects"
            for meta_path in sorted(objects_dir.glob("*/*.json")):
                obj = StoredObject.from_json(json.loads(meta_path.read_bytes()))
                if _object_id(obj.name) != meta_path.stem or obj.bucket != record.name:
                    raise RuntimeError(f"metadata sidecar {meta_path} is inconsistent")
                if not self._data_path(obj.bucket, obj.name, obj.generation).exists():
                    # data lost (should not happen with the commit order) -> drop the object
                    meta_path.unlink()
                    continue
                bucket.put(obj)
                self._last_generation = max(self._last_generation, obj.generation)
            # orphaned data files: generations that never committed or were superseded
            for data_path in objects_dir.glob("*/*.bin"):
                oid, _, gen = data_path.stem.partition(".")
                meta_path = data_path.parent / f"{oid}.json"
                keep = False
                if meta_path.exists():
                    with contextlib.suppress(ValueError, KeyError, OSError):
                        keep = int(json.loads(meta_path.read_bytes())["generation"]) == int(gen)
                if not keep:
                    data_path.unlink()
            self._buckets[record.name] = bucket

    # ---------------------------------------------------------------- paths

    def _bucket_dir(self, bucket: str) -> Path:
        return self._buckets_dir / check_bucket_name(bucket)

    def _meta_path(self, bucket: str, name: str) -> Path:
        oid = _object_id(name)
        return self._bucket_dir(bucket) / "objects" / oid[:2] / f"{oid}.json"

    def _data_path(self, bucket: str, name: str, generation: int) -> Path:
        oid = _object_id(name)
        return self._bucket_dir(bucket) / "objects" / oid[:2] / f"{oid}.{generation}.bin"

    def _write_meta(self, obj: StoredObject) -> None:
        payload = json.dumps(obj.to_json(), separators=(",", ":")).encode()
        atomic_write(self._meta_path(obj.bucket, obj.name), payload)

    def _next_generation(self) -> int:
        # Microsecond timestamps like GCS, strictly increasing across the whole store.
        self._last_generation = max(time.time_ns() // 1000, self._last_generation + 1)
        return self._last_generation

    # ---------------------------------------------------------------- buckets

    def _get(self, bucket: str) -> _Bucket:
        state = self._buckets.get(bucket)
        if state is None:
            raise ShimError.no_bucket(bucket)
        return state

    def create_bucket(self, cfg: BucketConfig, *, now: datetime | None = None) -> BucketRecord:
        """Create a bucket (409 if it exists)."""
        with self._lock:
            if cfg.name in self._buckets:
                raise ShimError.conflict(f"bucket {cfg.name} already exists")
            record = BucketRecord.from_config(cfg, now or _now())
            bdir = self._bucket_dir(cfg.name)
            (bdir / "objects").mkdir(parents=True, exist_ok=True)
            atomic_write(bdir / "bucket.json", json.dumps(record.to_json()).encode())
            self._buckets[cfg.name] = _Bucket(record=record)
            return record

    def ensure_bucket(self, cfg: BucketConfig) -> BucketRecord:
        """Create the bucket if missing; an existing bucket keeps its persisted policies."""
        with self._lock:
            existing = self._buckets.get(cfg.name)
            if existing is not None:
                return existing.record
            return self.create_bucket(cfg)

    def get_bucket(self, bucket: str) -> BucketRecord:
        with self._lock:
            return self._get(bucket).record

    def list_buckets(self) -> list[BucketRecord]:
        with self._lock:
            return [self._buckets[n].record for n in sorted(self._buckets)]

    def delete_bucket(self, bucket: str) -> None:
        """Delete an empty bucket (409 when it still has objects)."""
        with self._lock:
            state = self._get(bucket)
            if state.objects:
                raise ShimError.conflict(f"the bucket {bucket} is not empty")
            shutil.rmtree(self._bucket_dir(bucket))
            del self._buckets[bucket]

    # ---------------------------------------------------------------- objects

    def stage(self, *, max_bytes: int) -> StagedUpload:
        """A temp file for an incoming upload (same filesystem as the store)."""
        return StagedUpload(self._tmp, max_bytes=max_bytes)

    @staticmethod
    def _retained(obj: StoredObject, now: datetime) -> bool:
        return obj.retention_expires is not None and now < obj.retention_expires

    def commit(
        self,
        bucket: str,
        name: str,
        staged: StagedUpload,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        preconditions: Preconditions = NO_PRECONDITIONS,
        allow_overwrite: bool = True,
        expected_crc32c: str | None = None,
        expected_md5: str | None = None,
        now: datetime | None = None,
    ) -> StoredObject:
        """Atomically publish ``staged`` as the new live generation of ``bucket/name``.

        ``allow_overwrite=False`` (the writer lacks ``storage.objects.delete``) refuses to
        replace an existing object. Retained objects can never be replaced. ``expected_*``
        digests (from ``Content-MD5``/``x-goog-hash``) are checked before anything is
        published.
        """
        validate_name(name)
        content_type = validate_content_type(content_type)
        meta = validate_metadata(metadata or {})
        size, crc, md5 = staged.finish()
        if expected_crc32c is not None and expected_crc32c != crc:
            raise ShimError.bad_digest("the provided CRC32C does not match the data")
        if expected_md5 is not None and expected_md5 != md5:
            raise ShimError.bad_digest("the provided MD5 does not match the data")
        ts = now or _now()
        old: StoredObject | None
        with self._lock:
            state = self._get(bucket)
            old = state.objects.get(name)
            preconditions.check(old)
            if old is not None:
                if not allow_overwrite:
                    raise ShimError.forbidden(
                        "overwriting an object requires storage.objects.delete"
                    )
                if self._retained(old, ts):
                    raise ShimError.retention(
                        f"object {bucket}/{name} is under a retention policy and cannot be "
                        "overwritten"
                    )
            generation = self._next_generation()
            rec = state.record
            retention = (
                ts + timedelta(seconds=rec.retention_period_seconds)
                if rec.retention_period_seconds is not None
                else None
            )
            obj = StoredObject(
                bucket=bucket,
                name=name,
                generation=generation,
                metageneration=1,
                size=size,
                crc32c=crc,
                md5_hash=md5,
                content_type=content_type,
                metadata=meta,
                time_created=ts,
                updated=ts,
                retention_expires=retention,
            )
            data_path = self._data_path(bucket, name, generation)
            data_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged.path, data_path)
            try:
                self._write_meta(obj)  # commit point
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    data_path.unlink()
                raise
            state.put(obj)
        if old is not None:
            with contextlib.suppress(FileNotFoundError):
                self._data_path(bucket, name, old.generation).unlink()
        return obj

    def stat(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
    ) -> StoredObject:
        """Metadata of the live generation (404 if absent or ``generation`` is not live)."""
        validate_name(name)
        with self._lock:
            obj = self._get(bucket).objects.get(name)
            if obj is None or (generation is not None and obj.generation != generation):
                raise ShimError.no_object(bucket, name, generation)
            return obj

    def open(
        self, bucket: str, name: str, *, generation: int | None = None
    ) -> tuple[StoredObject, BinaryIO]:
        """Metadata plus an open handle on that generation's bytes (caller closes it).

        The handle stays readable even if the object is replaced or deleted meanwhile.
        """
        with self._lock:
            obj = self.stat(bucket, name, generation=generation)
            fh = open(self._data_path(bucket, name, obj.generation), "rb")  # noqa: SIM115
            return obj, fh

    def list(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        page_token: str | None = None,
        max_results: int = MAX_LIST_RESULTS,
        start_offset: str | None = None,
        end_offset: str | None = None,
    ) -> tuple[list[StoredObject], list[str], str | None]:
        """Live objects in name order: ``(items, prefixes, next_page_token)``.

        With a ``delimiter``, names containing it after ``prefix`` collapse into
        ``prefixes`` (each counts toward ``max_results``, like GCS).
        """
        if not 1 <= max_results <= MAX_LIST_RESULTS:
            max_results = MAX_LIST_RESULTS if max_results > MAX_LIST_RESULTS else 1
        if delimiter == "":
            delimiter = None
        start = prefix
        if start_offset is not None and start_offset > start:
            start = start_offset
        if page_token is not None:
            token_start = _decode_token(page_token)
            if not token_start.startswith(prefix):
                raise ShimError.bad_request("pageToken does not belong to this listing")
            start = max(start, token_start)
        items: list[StoredObject] = []
        prefixes: list[str] = []
        with self._lock:
            state = self._get(bucket)
            names = state.names
            i = bisect_left(names, start)
            while i < len(names):
                name = names[i]
                if not name.startswith(prefix) or (end_offset is not None and name >= end_offset):
                    break
                if len(items) + len(prefixes) >= max_results:
                    return items, prefixes, _encode_token(name)
                if delimiter is not None:
                    cut = name.find(delimiter, len(prefix))
                    if cut >= 0:
                        sub = name[: cut + len(delimiter)]
                        prefixes.append(sub)
                        i = bisect_left(names, _after_prefix(sub), i)
                        continue
                items.append(state.objects[name])
                i += 1
        return items, prefixes, None

    def delete(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        preconditions: Preconditions = NO_PRECONDITIONS,
        now: datetime | None = None,
    ) -> StoredObject:
        """Delete the live generation (or ``generation`` if it is the live one)."""
        validate_name(name)
        ts = now or _now()
        with self._lock:
            state = self._get(bucket)
            obj = state.objects.get(name)
            if obj is None or (generation is not None and obj.generation != generation):
                raise ShimError.no_object(bucket, name, generation)
            preconditions.check(obj)
            if self._retained(obj, ts):
                raise ShimError.retention(
                    f"object {bucket}/{name} is under a retention policy and cannot be deleted"
                )
            self._meta_path(bucket, name).unlink()  # commit point
            state.remove(name)
        with contextlib.suppress(FileNotFoundError):
            self._data_path(bucket, name, obj.generation).unlink()
        return obj

    def copy(
        self,
        src_bucket: str,
        src_name: str,
        dst_bucket: str,
        dst_name: str,
        *,
        source_generation: int | None = None,
        source_preconditions: Preconditions = NO_PRECONDITIONS,
        preconditions: Preconditions = NO_PRECONDITIONS,
        allow_overwrite: bool = True,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        max_bytes: int,
        now: datetime | None = None,
    ) -> StoredObject:
        """Server-side copy (a rewrite); the destination's hashes are recomputed and must
        equal the source's (corruption check). ``content_type``/``metadata`` default to the
        source's."""
        validate_name(dst_name)
        with self._lock:
            self._get(dst_bucket)  # 404 early for a missing destination bucket
            src = self.stat(src_bucket, src_name, generation=source_generation)
            source_preconditions.check(src)
            preconditions.check(self._get(dst_bucket).objects.get(dst_name))
            src, fh = self.open(src_bucket, src_name, generation=src.generation)
        staged = self.stage(max_bytes=max(max_bytes, src.size))
        try:
            with fh:
                for chunk in iter(lambda: fh.read(_CHUNK), b""):
                    staged.write(chunk)
            return self.commit(
                dst_bucket,
                dst_name,
                staged,
                content_type=src.content_type if content_type is None else content_type,
                metadata=src.metadata if metadata is None else metadata,
                preconditions=preconditions,
                allow_overwrite=allow_overwrite,
                expected_crc32c=src.crc32c,
                expected_md5=src.md5_hash,
                now=now,
            )
        finally:
            staged.discard()

    def patch(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        content_type: str | None = None,
        set_content_type: bool = False,
        metadata: Mapping[str, str | None] | None = None,
        preconditions: Preconditions = NO_PRECONDITIONS,
        now: datetime | None = None,
    ) -> StoredObject:
        """Update mutable metadata; bumps ``metageneration``. ``metadata`` merges (``None``
        values delete keys)."""
        with self._lock:
            obj = self.stat(bucket, name, generation=generation)
            preconditions.check(obj)
            new_meta = dict(obj.metadata)
            for k, v in (metadata or {}).items():
                if v is None:
                    new_meta.pop(k, None)
                else:
                    new_meta[k] = v
            updated = replace(
                obj,
                metageneration=obj.metageneration + 1,
                content_type=(
                    validate_content_type(content_type) if set_content_type else obj.content_type
                ),
                metadata=validate_metadata(new_meta),
                updated=now or _now(),
            )
            self._write_meta(updated)
            self._get(bucket).put(updated)
            return updated

    # ---------------------------------------------------------------- lifecycle

    def sweep(self, *, now: datetime | None = None) -> builtins.list[StoredObject]:
        """Apply age-based lifecycle rules; retained objects are skipped (GCS semantics)."""
        ts = now or _now()
        deleted: builtins.list[StoredObject] = []
        with self._lock:
            targets = [
                (state.record.name, obj)
                for state in self._buckets.values()
                if state.record.lifecycle_age_seconds is not None
                for obj in state.objects.values()
                if ts - obj.time_created >= timedelta(seconds=state.record.lifecycle_age_seconds)
                and not self._retained(obj, ts)
            ]
            for bucket, obj in targets:
                deleted.append(
                    self.delete(
                        bucket,
                        obj.name,
                        preconditions=Preconditions(if_generation_match=obj.generation),
                        now=ts,
                    )
                )
        return deleted

    def iter_objects(self, bucket: str) -> Iterator[StoredObject]:
        """Snapshot of every live object in ``bucket`` (name order)."""
        with self._lock:
            state = self._get(bucket)
            snapshot = [state.objects[n] for n in state.names]
        yield from snapshot
