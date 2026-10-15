"""Filesystem backend: generations, preconditions, atomicity, listing, retention, lifecycle."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mapi_gcs_shim._errors import ShimError
from mapi_gcs_shim.backend import NO_PRECONDITIONS, FsBackend, Preconditions, StoredObject
from mapi_gcs_shim.config import WRITE_ONCE_RETENTION_SECONDS, BucketConfig
from mapi_spec.storage import crc32c, crc32c_to_base64, md5_base64

pytestmark = pytest.mark.unit

RUNS = "mapi-local-runs"
BUNDLES = "mapi-local-bundles"
TMPB = "mapi-local-quarantine"
T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FsBackend]:
    with FsBackend(tmp_path / "gcs") as b:
        b.create_bucket(BucketConfig(name=RUNS))
        b.create_bucket(
            BucketConfig(
                name=BUNDLES,
                retention_period_seconds=WRITE_ONCE_RETENTION_SECONDS,
                retention_locked=True,
            )
        )
        b.create_bucket(BucketConfig(name=TMPB, lifecycle_age_seconds=3600))
        yield b


def put(
    b: FsBackend,
    bucket: str,
    name: str,
    data: bytes,
    *,
    pre: Preconditions = NO_PRECONDITIONS,
    now: datetime | None = None,
    **kw: object,
) -> StoredObject:
    staged = b.stage(max_bytes=1 << 20)
    try:
        staged.write(data)
        return b.commit(bucket, name, staged, preconditions=pre, now=now, **kw)  # type: ignore[arg-type]
    finally:
        staged.discard()


def read(b: FsBackend, bucket: str, name: str, generation: int | None = None) -> bytes:
    _, fh = b.open(bucket, name, generation=generation)
    with fh:
        return fh.read()


def test_put_computes_hashes_and_generations(backend: FsBackend) -> None:
    data = b"hello world" * 1000
    a = put(backend, RUNS, "t/x/a", data, content_type="text/plain", metadata={"k": "v"})
    assert a.crc32c == crc32c_to_base64(crc32c(data))
    assert a.md5_hash == md5_base64(data)
    assert a.md5_hash == base64.b64encode(hashlib.md5(data).digest()).decode()
    assert (a.size, a.metageneration, a.content_type, a.metadata) == (
        len(data),
        1,
        "text/plain",
        {"k": "v"},
    )
    b = put(backend, RUNS, "t/x/a", b"v2")
    c = put(backend, RUNS, "t/x/other", b"v3")
    assert a.generation < b.generation < c.generation
    assert read(backend, RUNS, "t/x/a") == b"v2"
    with pytest.raises(ShimError) as err:
        read(backend, RUNS, "t/x/a", generation=a.generation)  # superseded generation
    assert err.value.status == 404
    meta = a.to_meta()
    assert meta.generation == a.generation and meta.crc32c == a.crc32c


def test_preconditions(backend: FsBackend) -> None:
    a = put(backend, RUNS, "o", b"1", pre=Preconditions(if_generation_match=0))
    with pytest.raises(ShimError) as err:
        put(backend, RUNS, "o", b"2", pre=Preconditions(if_generation_match=0))
    assert err.value.status == 412
    with pytest.raises(ShimError):
        put(backend, RUNS, "o", b"2", pre=Preconditions(if_generation_match=a.generation + 1))
    with pytest.raises(ShimError):
        put(backend, RUNS, "o", b"2", pre=Preconditions(if_generation_not_match=a.generation))
    with pytest.raises(ShimError):
        put(backend, RUNS, "new", b"2", pre=Preconditions(if_generation_not_match=0))
    with pytest.raises(ShimError):
        put(backend, RUNS, "o", b"2", pre=Preconditions(if_metageneration_match=2))
    b = put(backend, RUNS, "o", b"2", pre=Preconditions(if_generation_match=a.generation))
    with pytest.raises(ShimError):
        backend.delete(RUNS, "o", preconditions=Preconditions(if_generation_match=a.generation))
    with pytest.raises(ShimError) as nf:
        backend.delete(RUNS, "o", generation=a.generation)
    assert nf.value.status == 404
    backend.delete(RUNS, "o", generation=b.generation)
    with pytest.raises(ShimError):
        backend.stat(RUNS, "o")


def test_overwrite_needs_permission(backend: FsBackend) -> None:
    put(backend, RUNS, "o", b"1", allow_overwrite=False)
    with pytest.raises(ShimError) as err:
        put(backend, RUNS, "o", b"2", allow_overwrite=False)
    assert err.value.status == 403


def test_expected_digests(backend: FsBackend) -> None:
    with pytest.raises(ShimError, match="CRC32C") as err:
        put(backend, RUNS, "o", b"abc", expected_crc32c="AAAAAA==")
    assert err.value.xml_code == "BadDigest"
    with pytest.raises(ShimError, match="MD5"):
        put(backend, RUNS, "o", b"abc", expected_md5=md5_base64(b"abd"))
    with pytest.raises(ShimError):
        backend.stat(RUNS, "o")  # nothing was published
    put(
        backend,
        RUNS,
        "o",
        b"abc",
        expected_md5=md5_base64(b"abc"),
        expected_crc32c=crc32c_to_base64(crc32c(b"abc")),
    )


def test_size_cap_while_streaming(backend: FsBackend) -> None:
    staged = backend.stage(max_bytes=4)
    staged.write(b"1234")
    with pytest.raises(ShimError, match="maximum size") as err:
        staged.write(b"5")
    assert err.value.xml_code == "EntityTooLarge"
    staged.discard()
    assert not staged.path.exists()


@pytest.mark.parametrize(
    "name", ["../escape", "a/../b", "a//b", "/abs", "a/./b", ".", "..", "a\x00b", "x" * 1025, ""]
)
def test_unsafe_names_are_rejected(backend: FsBackend, name: str) -> None:
    with pytest.raises(ShimError) as err:
        put(backend, RUNS, name, b"x")
    assert err.value.status == 400


def test_names_never_become_paths(backend: FsBackend) -> None:
    put(backend, RUNS, "t/default/runs/r1/a1/deep/name with spaces.zip", b"x")
    put(backend, RUNS, "trailing/", b"x")
    files = [p for p in backend.root.rglob("*") if p.is_file()]
    for f in files:
        rel = f.relative_to(backend.root)
        assert rel.parts[0] in ("buckets", "_shim")
        assert "spaces" not in f.name and "deep" not in rel.parts


def test_unknown_bucket_and_bucket_lifecycle(backend: FsBackend) -> None:
    with pytest.raises(ShimError) as err:
        put(backend, "mapi-local-nope", "x", b"x")
    assert err.value.status == 404 and err.value.xml_code == "NoSuchBucket"
    with pytest.raises(ShimError) as conflict:
        backend.create_bucket(BucketConfig(name=RUNS))
    assert conflict.value.status == 409
    put(backend, RUNS, "x", b"x")
    with pytest.raises(ShimError, match="not empty"):
        backend.delete_bucket(RUNS)
    backend.delete(RUNS, "x")
    backend.delete_bucket(RUNS)
    assert RUNS not in [b.name for b in backend.list_buckets()]
    assert backend.ensure_bucket(BucketConfig(name=RUNS)).name == RUNS


def test_list_prefix_delimiter_and_pagination(backend: FsBackend) -> None:
    names = ["a/1", "a/2", "a/b/1", "a/b/2", "a/c/1", "b/1", "a0", "a/z"]
    for n in names:
        put(backend, RUNS, n, b"x")
    items, prefixes, token = backend.list(RUNS, prefix="a/")
    assert [o.name for o in items] == ["a/1", "a/2", "a/b/1", "a/b/2", "a/c/1", "a/z"]
    assert token is None and prefixes == []
    items, prefixes, token = backend.list(RUNS, prefix="a/", delimiter="/")
    assert [o.name for o in items] == ["a/1", "a/2", "a/z"]
    assert prefixes == ["a/b/", "a/c/"]

    # paging with a delimiter: prefixes count toward maxResults, nothing is repeated or lost
    seen: list[str] = []
    token = None
    pages = 0
    while True:
        items, prefixes, token = backend.list(
            RUNS, prefix="a/", delimiter="/", max_results=2, page_token=token
        )
        seen += [o.name for o in items] + prefixes
        pages += 1
        if token is None:
            break
    assert sorted(seen) == ["a/1", "a/2", "a/b/", "a/c/", "a/z"] and pages == 3

    _, _, tok = backend.list(RUNS, prefix="a/", max_results=1)
    assert tok is not None
    with pytest.raises(ShimError, match="pageToken"):
        backend.list(RUNS, prefix="b/", page_token=tok)
    with pytest.raises(ShimError, match="pageToken"):
        backend.list(RUNS, page_token="!!garbage!!")
    items, _, _ = backend.list(RUNS, start_offset="a/b", end_offset="a/c")
    assert [o.name for o in items] == ["a/b/1", "a/b/2"]


def test_retention_lock_makes_bundles_write_once(backend: FsBackend) -> None:
    obj = put(backend, BUNDLES, "t/d/models/m/1/bundle.zarr.zip", b"bundle", now=T0)
    assert obj.retention_expires is not None and obj.retention_expires > T0 + timedelta(days=3650)
    with pytest.raises(ShimError) as err:
        backend.delete(BUNDLES, obj.name, now=T0 + timedelta(days=400))
    assert err.value.status == 403 and err.value.reason == "retentionPolicyNotMet"
    with pytest.raises(ShimError, match="retention"):
        put(backend, BUNDLES, obj.name, b"swap", now=T0 + timedelta(days=1))
    assert read(backend, BUNDLES, obj.name) == b"bundle"
    assert backend.sweep(now=T0 + timedelta(days=36500)) == []


def test_lifecycle_sweep(backend: FsBackend) -> None:
    old = put(backend, TMPB, "t/d/uploads/old", b"x", now=T0)
    put(backend, TMPB, "t/d/uploads/new", b"x", now=T0 + timedelta(minutes=50))
    put(backend, RUNS, "t/d/runs/r/a1/x", b"x", now=T0)  # no lifecycle on runs
    deleted = backend.sweep(now=T0 + timedelta(hours=1))
    assert [o.name for o in deleted] == [old.name]
    assert [o.name for o in backend.iter_objects(TMPB)] == ["t/d/uploads/new"]
    assert [o.name for o in backend.iter_objects(RUNS)] == ["t/d/runs/r/a1/x"]


def test_copy_recomputes_and_preserves(backend: FsBackend) -> None:
    src = put(backend, RUNS, "src", b"payload", content_type="application/zip", metadata={"a": "1"})
    dst = backend.copy(
        RUNS,
        "src",
        BUNDLES,
        "t/d/models/m/1/x",
        source_generation=src.generation,
        preconditions=Preconditions(if_generation_match=0),
        max_bytes=1 << 20,
    )
    assert (dst.crc32c, dst.md5_hash, dst.size) == (src.crc32c, src.md5_hash, src.size)
    assert dst.generation > src.generation and dst.content_type == "application/zip"
    assert dst.metadata == {"a": "1"}
    with pytest.raises(ShimError) as err:
        backend.copy(
            RUNS,
            "src",
            BUNDLES,
            "t/d/models/m/1/x",
            preconditions=Preconditions(if_generation_match=0),
            max_bytes=1 << 20,
        )
    assert err.value.status == 412
    with pytest.raises(ShimError) as nf:
        backend.copy(
            RUNS, "src", BUNDLES, "t/d/y", source_generation=src.generation + 99, max_bytes=1 << 20
        )
    assert nf.value.status == 404
    with pytest.raises(ShimError):
        backend.copy(
            RUNS,
            "src",
            RUNS,
            "dst",
            source_preconditions=Preconditions(if_generation_match=src.generation + 1),
            max_bytes=1 << 20,
        )


def test_patch_bumps_metageneration(backend: FsBackend) -> None:
    a = put(backend, RUNS, "o", b"x", metadata={"keep": "1", "drop": "2"})
    p = backend.patch(
        RUNS,
        "o",
        content_type="text/csv",
        set_content_type=True,
        metadata={"drop": None, "new": "3"},
        preconditions=Preconditions(if_metageneration_match=1),
    )
    assert (p.generation, p.metageneration) == (a.generation, 2)
    assert p.metadata == {"keep": "1", "new": "3"} and p.content_type == "text/csv"
    with pytest.raises(ShimError):
        backend.patch(RUNS, "o", preconditions=Preconditions(if_metageneration_match=1))


def test_metadata_limits(backend: FsBackend) -> None:
    with pytest.raises(ShimError, match="64 entries"):
        put(backend, RUNS, "o", b"x", metadata={f"k{i}": "v" for i in range(65)})
    with pytest.raises(ShimError, match="8 KiB"):
        put(backend, RUNS, "o", b"x", metadata={"k": "v" * 9000})
    with pytest.raises(ShimError, match="control"):
        put(backend, RUNS, "o", b"x", metadata={"k": "a\nb"})
    with pytest.raises(ShimError, match="content type"):
        put(backend, RUNS, "o", b"x", content_type="text/plain\r\nX-Evil: 1")


def test_state_survives_restart_and_crash_debris_is_swept(tmp_path: Path) -> None:
    root = tmp_path / "gcs"
    with FsBackend(root) as b:
        b.create_bucket(BucketConfig(name=RUNS))
        a = put(b, RUNS, "keep", b"data")
        stale = put(b, RUNS, "stale", b"x")
        objects = root / "buckets" / RUNS / "objects"
        # simulate crashes: an upload half-staged, a data file whose sidecar never committed,
        # and a sidecar whose data vanished
        (root / "tmp" / "upload-crash.part").write_bytes(b"partial")
        oid = hashlib.sha256(b"keep").hexdigest()
        orphan = objects / oid[:2] / f"{oid}.{a.generation + 1000}.bin"
        orphan.write_bytes(b"never committed")
        sid = hashlib.sha256(b"stale").hexdigest()
        (objects / sid[:2] / f"{sid}.{stale.generation}.bin").unlink()
    with FsBackend(root) as b2:
        assert read(b2, RUNS, "keep") == b"data"
        assert b2.stat(RUNS, "keep").generation == a.generation
        with pytest.raises(ShimError):
            b2.stat(RUNS, "stale")
        assert not orphan.exists()
        assert list((root / "tmp").iterdir()) == []
        newer = put(b2, RUNS, "keep", b"v2")
        assert newer.generation > a.generation


def test_sidecar_is_consistent_json(backend: FsBackend) -> None:
    a = put(backend, RUNS, "x/y", b"z")
    oid = hashlib.sha256(b"x/y").hexdigest()
    data = json.loads(
        (backend.root / "buckets" / RUNS / "objects" / oid[:2] / f"{oid}.json").read_text()
    )
    assert StoredObject.from_json(data) == a
    assert os.path.exists(backend.root / "buckets" / RUNS / "bucket.json")


def test_root_is_locked_to_one_server(tmp_path: Path) -> None:
    with FsBackend(tmp_path / "gcs"), pytest.raises(RuntimeError, match="another GCS shim"):
        FsBackend(tmp_path / "gcs")
    with FsBackend(tmp_path / "gcs"):  # released on close
        pass


def test_open_handle_survives_overwrite(backend: FsBackend) -> None:
    put(backend, RUNS, "o", b"old")
    obj, fh = backend.open(RUNS, "o")
    put(backend, RUNS, "o", b"new")
    with fh:
        assert fh.read() == b"old"
    assert obj.size == 3
