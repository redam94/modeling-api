"""``GcsObjectStore`` against a fake google-cloud-storage client (no network, no GCP).

The fake mirrors the library surface the driver uses (Client.bucket/list_blobs,
Bucket.blob/get_blob, Blob.upload_from_string/download_as_bytes/delete/rewrite/reload/
generate_signed_url) and raises errors that carry ``.code`` like ``google.api_core``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mapi_gcs_shim import InvalidRange
from mapi_gcs_shim.gcs import GcsObjectStore, map_gcs_error
from mapi_spec.errors import ErrorCode
from mapi_spec.storage import (
    AccessDenied,
    NotFound,
    ObjectStore,
    PreconditionFailed,
    StorageError,
    crc32c,
    crc32c_to_base64,
    md5_base64,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


class ApiError(Exception):
    def __init__(self, code: int, message: str = "api error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Stored:
    data: bytes
    generation: int
    content_type: str | None
    metadata: dict[str, str] | None


@dataclass
class World:
    objects: dict[tuple[str, str], Stored] = field(default_factory=dict)
    gen: int = 100
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    rewrite_calls: int = 0


class FakeBucket:
    def __init__(self, world: World, name: str) -> None:
        self.world = world
        self.name = name

    def blob(self, name: str, generation: int | None = None) -> FakeBlob:
        return FakeBlob(self, name, generation)

    def get_blob(self, name: str, generation: int | None = None) -> FakeBlob | None:
        hit = self.world.objects.get((self.name, name))
        if hit is None or (generation is not None and hit.generation != generation):
            return None
        blob = FakeBlob(self, name, generation)
        blob._load(hit)
        return blob


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str, generation: int | None) -> None:
        self.bucket = bucket
        self.name = name
        self._pin = generation
        self.generation: int | None = None
        self.metageneration: int | None = None
        self.size: int | None = None
        self.crc32c: str | None = None
        self.md5_hash: str | None = None
        self.content_type: str | None = None
        self.metadata: dict[str, str] | None = None
        self.time_created: datetime | None = None

    @property
    def _world(self) -> World:
        return self.bucket.world

    def _load(self, s: Stored) -> None:
        self.generation = s.generation
        self.metageneration = 1
        self.size = len(s.data)
        self.crc32c = crc32c_to_base64(crc32c(s.data))
        self.md5_hash = md5_base64(s.data)
        self.content_type = s.content_type
        self.metadata = s.metadata
        self.time_created = NOW

    def _live(self) -> Stored:
        hit = self._world.objects.get((self.bucket.name, self.name))
        if hit is None or (self._pin is not None and hit.generation != self._pin):
            raise ApiError(404, "No such object")
        return hit

    def _check(self, if_generation_match: int | None) -> None:
        if if_generation_match is None:
            return
        hit = self._world.objects.get((self.bucket.name, self.name))
        if (hit.generation if hit else 0) != if_generation_match:
            raise ApiError(412, "conditionNotMet")

    def upload_from_string(self, data: bytes, **kw: Any) -> None:
        self._world.calls.append(("upload_from_string", kw))
        self._check(kw.get("if_generation_match"))
        self._world.gen += 1
        s = Stored(data, self._world.gen, kw.get("content_type"), self.metadata)
        self._world.objects[(self.bucket.name, self.name)] = s
        self._load(s)

    def download_as_bytes(
        self, start: int | None = None, end: int | None = None, **kw: Any
    ) -> bytes:
        self._world.calls.append(("download_as_bytes", {"start": start, "end": end, **kw}))
        data = self._live().data
        if start is not None and start >= len(data):
            raise ApiError(416, "range not satisfiable")
        return data[start or 0 : None if end is None else end + 1]

    def delete(self, if_generation_match: int | None = None) -> None:
        self._live()
        self._check(if_generation_match)
        del self._world.objects[(self.bucket.name, self.name)]

    def rewrite(
        self, source: FakeBlob, token: str | None = None, **kw: Any
    ) -> tuple[str | None, int, int]:
        self._world.rewrite_calls += 1
        src = source._live()
        if token is None:
            return "tok-1", 0, len(src.data)  # first call: not done yet (large object)
        self._check(kw.get("if_generation_match"))
        self._world.gen += 1
        s = Stored(src.data, self._world.gen, src.content_type, src.metadata)
        self._world.objects[(self.bucket.name, self.name)] = s
        return None, len(src.data), len(src.data)

    def reload(self) -> None:
        self._load(self._live())

    def generate_signed_url(self, **kw: Any) -> str:
        self._world.calls.append(("generate_signed_url", kw))
        return f"https://storage.googleapis.com/{self.bucket.name}/{self.name}?X-Goog-Signature=ab"


class FakePage(list[FakeBlob]):
    prefixes: set[str]


class FakeIterator:
    def __init__(self, page: FakePage | None, token: str | None) -> None:
        self.pages = iter([page] if page is not None else [])
        self.next_page_token = token


class FakeCredentials:
    signer_email = "control@mapi-prod.iam.gserviceaccount.com"

    def sign_bytes(self, data: bytes) -> bytes:
        return b"\x01\x02" + data[:2]


class FakeClient:
    def __init__(self) -> None:
        self.world = World()
        self._credentials = FakeCredentials()

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.world, name)

    def list_blobs(self, bucket: str, **kw: Any) -> FakeIterator:
        self.world.calls.append(("list_blobs", kw))
        names = sorted(
            n
            for (b, n) in self.world.objects
            if b == bucket and n.startswith(kw.get("prefix") or "")
        )
        page = FakePage()
        page.prefixes = set()
        size = kw["page_size"]
        for n in names[:size]:
            blob = FakeBlob(self.bucket(bucket), n, None)
            blob._load(self.world.objects[(bucket, n)])
            page.append(blob)
        return FakeIterator(page, "next-token" if len(names) > size else None)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def store(client: FakeClient) -> GcsObjectStore:
    return GcsObjectStore(client, clock=lambda: NOW)


async def test_satisfies_the_protocol(store: GcsObjectStore) -> None:
    assert isinstance(store, ObjectStore)


async def test_put_get_stat_list_delete(store: GcsObjectStore, client: FakeClient) -> None:
    meta = await store.put(
        "mapi-prod-runs",
        "t/x/a",
        b"hello",
        content_type="text/plain",
        metadata={"k": "v"},
        if_generation_match=0,
    )
    assert meta.generation == 101 and meta.size == 5 and meta.metadata == {"k": "v"}
    assert meta.crc32c == crc32c_to_base64(crc32c(b"hello"))
    upload_kw = client.world.calls[0][1]
    assert upload_kw == {
        "content_type": "text/plain",
        "if_generation_match": 0,
        "checksum": "crc32c",
    }
    with pytest.raises(PreconditionFailed):
        await store.put("mapi-prod-runs", "t/x/a", b"again", if_generation_match=0)
    assert await store.get("mapi-prod-runs", "t/x/a") == b"hello"
    assert await store.get("mapi-prod-runs", "t/x/a", byte_range=(1, 2)) == b"el"
    ranged = client.world.calls[-1][1]
    assert ranged == {"start": 1, "end": 2, "checksum": None}
    with pytest.raises(InvalidRange):
        await store.get("mapi-prod-runs", "t/x/a", byte_range=(99, None))
    with pytest.raises(StorageError) as big:
        await store.get("mapi-prod-runs", "t/x/a", max_bytes=4)
    assert big.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert await store.get("mapi-prod-runs", "t/x/a", byte_range=(0, 3), max_bytes=4) == b"hell"
    assert (await store.stat("mapi-prod-runs", "t/x/a")).generation == 101
    with pytest.raises(NotFound):
        await store.stat("mapi-prod-runs", "t/x/a", generation=1)
    with pytest.raises(NotFound):
        await store.get("mapi-prod-runs", "t/x/missing", max_bytes=10)
    await store.put("mapi-prod-runs", "t/x/b", b"b")
    page = await store.list("mapi-prod-runs", prefix="t/x/", max_results=1)
    assert [m.name for m in page.items] == ["t/x/a"] and page.next_page_token == "next-token"
    assert client.world.calls[-1] == (
        "list_blobs",
        {"prefix": "t/x/", "delimiter": None, "page_token": None, "page_size": 1},
    )
    await store.delete("mapi-prod-runs", "t/x/a", if_generation_match=101)
    with pytest.raises(NotFound):
        await store.delete("mapi-prod-runs", "t/x/a")
    with pytest.raises(ValueError):
        await store.put("mapi-prod-runs", "../x", b"")


async def test_copy_loops_until_the_rewrite_is_done(
    store: GcsObjectStore, client: FakeClient
) -> None:
    src = await store.put("mapi-prod-runs", "t/x/bundle", b"zip")
    dst = await store.copy(
        "mapi-prod-runs",
        "t/x/bundle",
        "mapi-prod-bundles",
        "t/x/m/1/bundle",
        source_generation=src.generation,
        if_generation_match=0,
    )
    assert client.world.rewrite_calls == 2
    assert dst.same_content(src) and dst.bucket == "mapi-prod-bundles"
    with pytest.raises(PreconditionFailed):
        await store.copy(
            "mapi-prod-runs",
            "t/x/bundle",
            "mapi-prod-bundles",
            "t/x/m/1/bundle",
            if_generation_match=0,
        )


async def test_signed_urls(store: GcsObjectStore, client: FakeClient) -> None:
    meta = await store.put("mapi-prod-data", "t/x/data.zarr.zip", b"data")
    signed = await store.sign_get(
        "mapi-prod-data", "t/x/data.zarr.zip", expires_in=timedelta(minutes=15)
    )
    assert signed.generation == meta.generation and signed.expires_at == NOW + timedelta(minutes=15)
    kw = client.world.calls[-1][1]
    assert kw == {
        "version": "v4",
        "expiration": timedelta(minutes=15),
        "method": "GET",
        "generation": meta.generation,
    }
    put = await store.sign_put(
        "mapi-prod-runs",
        "t/x/manifest.json",
        expires_in=timedelta(minutes=5),
        content_type="application/json",
        max_bytes=1024,
    )
    kw = client.world.calls[-1][1]
    assert kw["method"] == "PUT" and kw["content_type"] == "application/json"
    assert kw["headers"] == {
        "x-goog-if-generation-match": "0",
        "x-goog-content-length-range": "0,1024",
    }
    assert put.headers == {**kw["headers"], "content-type": "application/json"}
    with pytest.raises(ValueError):
        await store.sign_put("mapi-prod-runs", "x", expires_in=timedelta(days=30))


async def test_iam_signblob_options_are_forwarded(client: FakeClient) -> None:
    store = GcsObjectStore(
        client,
        service_account_email="sa@p.iam.gserviceaccount.com",
        access_token="tok",
        signing_credentials="creds",
    )
    await store.put("b-1", "o", b"x")
    await store.sign_get("b-1", "o", expires_in=timedelta(minutes=1))
    kw = client.world.calls[-1][1]
    assert kw["service_account_email"] == "sa@p.iam.gserviceaccount.com"
    assert kw["access_token"] == "tok" and kw["credentials"] == "creds"


async def test_post_policy_prefix_conditions(store: GcsObjectStore) -> None:
    policy = await store.post_policy(
        "mapi-prod-runs",
        "t/x/runs/r/a1/",
        expires_in=timedelta(hours=1),
        max_bytes=100,
        min_bytes=1,
    )
    assert policy.url == "https://storage.googleapis.com/mapi-prod-runs/"
    fields = policy.fields
    assert fields["x-goog-algorithm"] == "GOOG4-RSA-SHA256"
    assert fields["x-goog-credential"].startswith(
        "control@mapi-prod.iam.gserviceaccount.com/20261015/"
    )
    doc = json.loads(base64.b64decode(fields["policy"]))
    assert ["starts-with", "$key", "t/x/runs/r/a1/"] in doc["conditions"]
    assert ["content-length-range", 1, 100] in doc["conditions"]
    assert fields["x-goog-signature"] == (b"\x01\x02" + fields["policy"][:2].encode()).hex()
    assert policy.allows("t/x/runs/r/a1/out", 50) and not policy.allows("t/x/other", 50)
    with pytest.raises(ValueError):
        await store.post_policy(
            "mapi-prod-runs", "no-slash", expires_in=timedelta(hours=1), max_bytes=1
        )


async def test_post_policy_without_signer_is_not_implemented(client: FakeClient) -> None:
    client._credentials = object()  # type: ignore[assignment]
    store = GcsObjectStore(client)
    with pytest.raises(NotImplementedError, match="signing credentials"):
        await store.post_policy("b-1", "p/", expires_in=timedelta(hours=1), max_bytes=1)


@pytest.mark.parametrize(
    ("code", "cls", "error_code"),
    [
        (404, NotFound, ErrorCode.NOT_FOUND),
        (412, PreconditionFailed, ErrorCode.PRECONDITION_FAILED),
        (304, PreconditionFailed, ErrorCode.PRECONDITION_FAILED),
        (401, AccessDenied, ErrorCode.FORBIDDEN),
        (403, AccessDenied, ErrorCode.FORBIDDEN),
        (409, StorageError, ErrorCode.CONFLICT),
        (503, StorageError, ErrorCode.INFRA_ERROR),
    ],
)
def test_error_mapping(code: int, cls: type[StorageError], error_code: ErrorCode) -> None:
    with pytest.raises(cls) as err:
        map_gcs_error(ApiError(code), bucket="b", name="n")
    assert err.value.code is error_code and (err.value.bucket, err.value.name) == ("b", "n")


def test_error_mapping_uses_response_status_and_passes_bugs_through() -> None:
    class InvalidResponse(Exception):
        def __init__(self) -> None:
            super().__init__("bad")
            self.response = type("R", (), {"status_code": 404})()

    with pytest.raises(NotFound):
        map_gcs_error(InvalidResponse(), bucket=None, name=None)
    with pytest.raises(KeyError):
        map_gcs_error(KeyError("bug"), bucket=None, name=None)


def test_missing_library_is_a_clear_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("google.cloud"):
            raise ImportError("no google")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"mapi-gcs-shim\[gcp\]"):
        GcsObjectStore()
