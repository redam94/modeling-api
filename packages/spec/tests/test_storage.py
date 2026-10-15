"""Storage contracts: GCS-style hashes, names, signed-URL shapes, the ObjectStore protocol."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode
from mapi_spec.storage import (
    AccessDenied,
    BucketPurpose,
    Buckets,
    NotFound,
    ObjectMeta,
    ObjectPage,
    ObjectStore,
    PostPolicy,
    PreconditionFailed,
    SignedGet,
    SignedPut,
    StorageError,
    bootstrap_key,
    bucket_name,
    bundle_prefix,
    check_object_name,
    check_segment,
    check_ttl,
    crc32c,
    crc32c_from_base64,
    crc32c_to_base64,
    dataset_prefix,
    md5_base64,
    quarantine_key,
    run_checkpoint_prefix,
    run_prefix,
)
from pydantic import ValidationError

TS = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)

# --------------------------------------------------------------------------------- hashes


def test_crc32c_known_vectors_match_gcs() -> None:
    assert crc32c(b"") == 0
    assert crc32c(b"123456789") == 0xE3069283  # RFC 3720 check value
    assert crc32c_to_base64(crc32c(b"")) == "AAAAAA=="
    assert crc32c_to_base64(0xE3069283) == "4waSgw=="
    assert crc32c_from_base64("4waSgw==") == 0xE3069283
    assert md5_base64(b"") == "1B2M2Y8AsgTpgAmY7PhCfg=="


def test_crc32c_is_chainable_and_consistent_above_the_acceleration_threshold() -> None:
    data = bytes(range(256)) * 600  # 150 KiB: the accelerated path if google_crc32c exists
    chunked = 0
    for i in range(0, len(data), 4096):  # small chunks always use the pure implementation
        chunked = crc32c(data[i : i + 4096], chunked)
    assert crc32c(data) == chunked
    assert crc32c(memoryview(data[:10])) == crc32c(bytearray(data[:10]))


def test_crc32c_base64_validation() -> None:
    with pytest.raises(ValueError):
        crc32c_to_base64(2**32)
    with pytest.raises(ValueError):
        crc32c_from_base64("AAAA")


# ---------------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    "name",
    ["", "/abs", "a//b", "a/./b", "a/../b", "..", "a\nb", "a\x00b", "x" * 1025, "é" * 513],
)
def test_bad_object_names(name: str) -> None:
    with pytest.raises(ValueError):
        check_object_name(name)


@pytest.mark.parametrize("name", ["a", "t/default/runs/r/a1/", "ui/plotdata/x.arrow", "é" * 512])
def test_good_object_names(name: str) -> None:
    assert check_object_name(name) == name


@pytest.mark.parametrize("seg", ["", "../x", "a/b", "-x", "a..b", "x" * 129, "a b"])
def test_bad_segments(seg: str) -> None:
    with pytest.raises(ValueError):
        check_segment(seg)


def test_bucket_scheme() -> None:
    b = Buckets("local")
    assert b.runs == "mapi-local-runs" and b.bundles == "mapi-local-bundles"
    assert b.quarantine == "mapi-local-quarantine" and b.bootstrap == "mapi-local-bootstrap"
    assert b.data == "mapi-local-data"
    assert set(b.all()) == set(BucketPurpose)
    assert bucket_name("prod", "runs") == "mapi-prod-runs"
    for bad in ("Prod", "", "a" * 17, "1x"):
        with pytest.raises(ValueError):
            Buckets(bad)
    with pytest.raises(ValueError):
        bucket_name("dev", "scratch")


def test_key_helpers_follow_the_plan_layout() -> None:
    assert quarantine_key("default", "upl_1") == "t/default/uploads/upl_1"
    nonce = "ab" * 32
    assert bootstrap_key(nonce) == nonce
    assert run_prefix("default", "run_1", 2) == "t/default/runs/run_1/a2/"
    assert run_prefix("default", "run_1", 1, task_index=3) == "t/default/runs/run_1/a1/tasks/3/"
    assert run_checkpoint_prefix("default", "run_1", 1) == "t/default/runs/run_1/a1/checkpoints/"
    assert bundle_prefix("default", "mdl_1", 4) == "t/default/models/mdl_1/4/"
    sha = "0f" * 32
    assert dataset_prefix("default", sha) == f"t/default/datasets/{sha}/"


@pytest.mark.parametrize(
    "call",
    [
        lambda: bootstrap_key("short"),
        lambda: bootstrap_key("AB" * 32),
        lambda: run_prefix("default", "run_1", 0),
        lambda: run_prefix("default", "run_1", 1, task_index=-1),
        lambda: run_prefix("../x", "run_1", 1),
        lambda: run_prefix("default", "a/b", 1),
        lambda: bundle_prefix("default", "m", 0),
        lambda: dataset_prefix("default", "sha256:" + "0" * 64),
        lambda: quarantine_key("default", ".."),
    ],
)
def test_key_helpers_reject_unsafe_ids(call: Any) -> None:
    with pytest.raises(ValueError):
        call()


# ------------------------------------------------------------------------- models / examples


@pytest.mark.parametrize("model", [ObjectMeta, SignedGet, SignedPut, PostPolicy])
def test_examples_validate_and_round_trip(model: type[Any]) -> None:
    examples = model.model_json_schema()["examples"]
    assert examples
    for ex in examples:
        obj = model.model_validate(ex)
        assert model.model_validate_json(obj.model_dump_json()) == obj


def test_object_meta_gcs_resource_round_trip() -> None:
    meta = ObjectMeta.model_validate(ObjectMeta.model_json_schema()["examples"][0])
    res = meta.to_gcs_resource()
    assert res["generation"] == str(meta.generation) and res["timeCreated"].endswith("Z")
    assert ObjectMeta.from_gcs_resource(res) == meta
    bare = {k: v for k, v in res.items() if k not in ("md5Hash", "contentType", "metadata")}
    assert ObjectMeta.from_gcs_resource(bare).md5_hash is None


def test_object_meta_validation_and_content_comparison() -> None:
    base = dict(bucket="mapi-local-runs", name="a/b", generation=1, size=1, time_created=TS)
    a = ObjectMeta(**base, crc32c="AAAAAA==", md5_hash="1B2M2Y8AsgTpgAmY7PhCfg==")
    b = a.model_copy(update={"generation": 9, "bucket": "mapi-local-bundles"})
    assert a.same_content(b)
    assert a.same_content(a.model_copy(update={"md5_hash": None}))
    assert not a.same_content(a.model_copy(update={"crc32c": "4waSgw=="}))
    assert not a.same_content(a.model_copy(update={"size": 2}))
    for bad in ({"crc32c": "nothex"}, {"name": "../x"}, {"generation": 0}, {"bucket": "UP"}):
        with pytest.raises(ValidationError):
            ObjectMeta(**{**base, "crc32c": "AAAAAA==", **bad})
    with pytest.raises(ValidationError):
        ObjectMeta(**{**base, "time_created": datetime(2026, 1, 1)}, crc32c="AAAAAA==")


def test_post_policy_rules() -> None:
    pol = PostPolicy.model_validate(PostPolicy.model_json_schema()["examples"][0])
    assert pol.allows(pol.prefix + "bundle.zarr.zip", 10)
    assert not pol.allows(pol.prefix, 10)
    assert not pol.allows("t/default/runs/other/a1/x", 10)
    assert not pol.allows(pol.prefix + "../escape", 10)
    assert not pol.allows(pol.prefix + "x", pol.content_length_range[1] + 1)
    with pytest.raises(ValidationError, match="end with"):
        PostPolicy.model_validate({**pol.model_dump(), "prefix": "no/slash"})
    with pytest.raises(ValidationError, match="content_length_range"):
        PostPolicy.model_validate({**pol.model_dump(), "content_length_range": (5, 1)})


def test_ttl_bounds() -> None:
    assert check_ttl(timedelta(minutes=15)) == timedelta(minutes=15)
    for bad in (timedelta(0), timedelta(days=7, seconds=1)):
        with pytest.raises(ValueError):
            check_ttl(bad)


def test_storage_errors_carry_codes_and_location() -> None:
    assert NotFound("x", bucket="b", name="n").code is ErrorCode.NOT_FOUND
    assert PreconditionFailed("x").code is ErrorCode.PRECONDITION_FAILED
    assert AccessDenied("x").code is ErrorCode.FORBIDDEN
    err = StorageError("boom", bucket="b", name="n")
    assert (err.code, err.bucket, err.name) == (ErrorCode.INFRA_ERROR, "b", "n")
    assert err.to_problem().status == 503


# ---------------------------------------------------------------------- protocol conformance


class _MemoryStore:
    """Minimal reference implementation proving the protocol is implementable as written."""

    def __init__(self) -> None:
        self._objs: dict[tuple[str, str], tuple[ObjectMeta, bytes]] = {}
        self._gen = 0

    def _meta(self, bucket: str, name: str, generation: int | None) -> tuple[ObjectMeta, bytes]:
        hit = self._objs.get((bucket, name))
        if hit is None or (generation is not None and hit[0].generation != generation):
            raise NotFound("no such object", bucket=bucket, name=name)
        return hit

    def _check(self, bucket: str, name: str, if_generation_match: int | None) -> None:
        if if_generation_match is None:
            return
        live = self._objs.get((bucket, name))
        if (live[0].generation if live else 0) != if_generation_match:
            raise PreconditionFailed("generation mismatch", bucket=bucket, name=name)

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
        self._check(bucket, name, if_generation_match)
        self._gen += 1
        meta = ObjectMeta(
            bucket=bucket,
            name=name,
            generation=self._gen,
            size=len(data),
            crc32c=crc32c_to_base64(crc32c(data)),
            md5_hash=md5_base64(data),
            content_type=content_type,
            metadata=dict(metadata or {}),
            time_created=TS,
        )
        self._objs[(bucket, name)] = (meta, data)
        return meta

    async def get(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        byte_range: tuple[int, int | None] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        meta, data = self._meta(bucket, name, generation)
        if max_bytes is not None and meta.size > max_bytes:
            raise StorageError("too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
        if byte_range is not None:
            start, end = byte_range
            return data[start : None if end is None else end + 1]
        return data

    async def stat(self, bucket: str, name: str, *, generation: int | None = None) -> ObjectMeta:
        return self._meta(bucket, name, generation)[0]

    async def list(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        page_token: str | None = None,
        max_results: int = 1000,
    ) -> ObjectPage:
        items = sorted(
            (m for (b, n), (m, _) in self._objs.items() if b == bucket and n.startswith(prefix)),
            key=lambda m: m.name,
        )
        return ObjectPage(items=items[:max_results])

    async def delete(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        self._meta(bucket, name, generation)
        self._check(bucket, name, if_generation_match)
        del self._objs[(bucket, name)]

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
        meta, data = self._meta(src_bucket, src_name, source_generation)
        return await self.put(
            dst_bucket,
            dst_name,
            data,
            content_type=meta.content_type,
            if_generation_match=if_generation_match,
        )

    async def sign_get(
        self, bucket: str, name: str, *, expires_in: timedelta, generation: int | None = None
    ) -> SignedGet:
        meta = await self.stat(bucket, name, generation=generation)
        return SignedGet(
            url=f"mem://{bucket}/{name}?generation={meta.generation}",
            expires_at=TS + check_ttl(expires_in),
            generation=meta.generation,
            size=meta.size,
            crc32c=meta.crc32c,
        )

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
        headers = (
            {}
            if if_generation_match is None
            else {"x-goog-if-generation-match": str(if_generation_match)}
        )
        return SignedPut(
            url=f"mem://{bucket}/{name}",
            name=name,
            expires_at=TS + check_ttl(expires_in),
            headers=headers,
            max_bytes=max_bytes,
        )

    async def post_policy(
        self, bucket: str, prefix: str, *, expires_in: timedelta, max_bytes: int, min_bytes: int = 0
    ) -> PostPolicy:
        return PostPolicy(
            url=f"mem://{bucket}",
            prefix=prefix,
            content_length_range=(min_bytes, max_bytes),
            expires_at=TS + check_ttl(expires_in),
        )


async def test_reference_store_satisfies_the_protocol() -> None:
    store: ObjectStore = _MemoryStore()
    assert isinstance(store, ObjectStore)
    m1 = await store.put("mapi-local-runs", "t/d/runs/r/a1/x", b"hello", if_generation_match=0)
    with pytest.raises(PreconditionFailed):
        await store.put("mapi-local-runs", "t/d/runs/r/a1/x", b"again", if_generation_match=0)
    m2 = await store.put("mapi-local-runs", "t/d/runs/r/a1/x", b"v2", if_generation_match=1)
    assert m2.generation > m1.generation
    assert await store.get("mapi-local-runs", "t/d/runs/r/a1/x", byte_range=(0, 0)) == b"v"
    with pytest.raises(NotFound):
        await store.stat("mapi-local-runs", "t/d/runs/r/a1/x", generation=m1.generation)
    copied = await store.copy(
        "mapi-local-runs",
        "t/d/runs/r/a1/x",
        "mapi-local-bundles",
        "t/d/models/m/1/x",
        source_generation=m2.generation,
        if_generation_match=0,
    )
    assert copied.same_content(m2) and copied.bucket == "mapi-local-bundles"
    page = await store.list("mapi-local-runs", prefix="t/d/runs/r/a1/")
    assert [m.name for m in page.items] == ["t/d/runs/r/a1/x"]
    signed = await store.sign_get("mapi-local-runs", "t/d/runs/r/a1/x", expires_in=timedelta(1))
    assert signed.generation == m2.generation and signed.crc32c == m2.crc32c
    put = await store.sign_put("mapi-local-runs", "t/d/x", expires_in=timedelta(minutes=5))
    assert put.headers == {"x-goog-if-generation-match": "0"}
    pol = await store.post_policy(
        "mapi-local-runs", "t/d/runs/r/a1/", expires_in=timedelta(hours=1), max_bytes=100
    )
    assert pol.allows("t/d/runs/r/a1/bundle", 100)
    await store.delete("mapi-local-runs", "t/d/runs/r/a1/x")
    assert (await store.list("mapi-local-runs")).items == []
    assert builtins.list is list  # the protocol's ``list`` method does not shadow the builtin
