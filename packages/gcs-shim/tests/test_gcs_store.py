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
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mapi_gcs_shim import InvalidRange
from mapi_gcs_shim.gcs import GCS_API_ENDPOINT, GcsObjectStore, map_gcs_error
from mapi_gcs_shim.gcs_signing import (
    BlobSigner,
    IamSignBlobError,
    IamSignBlobSigner,
    rsa_signed_url,
    rsa_string_to_sign,
)
from mapi_gcs_shim.signing import canonical_request, credential_scope, resource_path
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
    corrupt_copies: bool = False
    corrupt_uploads: bool = False


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
        if self._world.corrupt_uploads:
            data = data[:-1] + bytes([data[-1] ^ 1]) if data else b"x"
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
        self._world.calls.append(("rewrite", dict(kw)))
        src = source._live()
        pin = kw.get("if_source_generation_match")
        if pin is not None and src.generation != pin:
            raise ApiError(412, "source conditionNotMet")
        if token is None:
            return "tok-1", 0, len(src.data)  # first call: not done yet (large object)
        self._check(kw.get("if_generation_match"))
        self._world.gen += 1
        data = src.data + (b"!" if self._world.corrupt_copies else b"")
        s = Stored(data, self._world.gen, src.content_type, src.metadata)
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


class UploaderCredentials(FakeCredentials):
    signer_email = "uploader@mapi-prod.iam.gserviceaccount.com"


async def test_create_only_post_policy_is_signed_by_the_uploader(client: FakeClient) -> None:
    """Real GCS has no POST-form generation precondition: a create-only policy is signed by
    a principal without ``storage.objects.delete`` (which GCS needs to overwrite)."""
    plain = GcsObjectStore(client)
    with pytest.raises(NotImplementedError, match="uploader_credentials"):
        await plain.post_policy(
            "mapi-prod-runs", "t/x/", expires_in=timedelta(hours=1), max_bytes=1, create_only=True
        )
    store = GcsObjectStore(client, uploader_credentials=UploaderCredentials())
    policy = await store.post_policy(
        "mapi-prod-runs", "t/x/", expires_in=timedelta(hours=1), max_bytes=1, create_only=True
    )
    assert policy.create_only
    assert policy.fields["x-goog-credential"].startswith("uploader@")
    assert "x-goog-if-generation-match" not in policy.fields
    other = await store.post_policy(
        "mapi-prod-runs", "t/x/", expires_in=timedelta(hours=1), max_bytes=1
    )
    assert other.fields["x-goog-credential"].startswith("control@") and not other.create_only


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


# ------------------------------------------------------------------ hardening (M2)


class RsaSigner:
    """A service-account key: RSA-SHA256 PKCS #1 v1.5, like IAM signBlob."""

    def __init__(self, email: str = "control@mapi-prod.iam.gserviceaccount.com") -> None:
        self.signer_email = email
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def sign_bytes(self, message: bytes) -> bytes:
        return self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())

    def verify(self, signature: bytes, message: bytes) -> bool:
        try:
            self.key.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return False
        return True


TS = "20261015T093000Z"


@pytest.fixture(scope="module")
def rsa_signer() -> RsaSigner:
    return RsaSigner()


def _split(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, dict(parse_qsl(parts.query, keep_blank_values=True))


def test_rsa_signed_url_verifies_with_the_public_key(rsa_signer: RsaSigner) -> None:
    assert isinstance(rsa_signer, BlobSigner)
    headers = {"content-type": "application/json", "x-goog-if-generation-match": "0"}
    url = rsa_signed_url(
        rsa_signer,
        method="PUT",
        bucket="mapi-prod-runs",
        name="t/x/runs/r 1/a1/manifest.json",
        expires_in=timedelta(minutes=10),
        now=NOW,
        headers=headers,
    )
    _, q = _split(url)
    assert url.startswith(f"{GCS_API_ENDPOINT}/mapi-prod-runs/t/x/runs/r%201/a1/manifest.json?")
    assert q["X-Goog-Algorithm"] == "GOOG4-RSA-SHA256" and q["X-Goog-Date"] == TS
    assert q["X-Goog-SignedHeaders"] == "content-type;host;x-goog-if-generation-match"
    scope = "20261015/auto/storage/goog4_request"
    assert q["X-Goog-Credential"] == f"{rsa_signer.signer_email}/{scope}"
    signature = bytes.fromhex(q.pop("X-Goog-Signature"))
    all_headers = {**headers, "host": "storage.googleapis.com"}
    creq = canonical_request(
        "PUT",
        resource_path("mapi-prod-runs", "t/x/runs/r 1/a1/manifest.json"),
        q.items(),
        all_headers,
        sorted(all_headers),
    )
    sts = rsa_string_to_sign(TS, credential_scope("20261015"), creq)
    assert rsa_signer.verify(signature, sts.encode())
    tampered = rsa_string_to_sign(TS, credential_scope("20261015"), creq.replace("PUT", "GET", 1))
    assert not rsa_signer.verify(signature, tampered.encode())
    with pytest.raises(ValueError):
        rsa_signed_url(rsa_signer, method="GET", bucket="b", name="n", expires_in=timedelta(8))


@pytest.mark.parametrize(
    ("method", "kwargs", "lib"),
    [
        ("GET", {"query": {"generation": "1760520000000001"}}, {"generation": 1760520000000001}),
        (
            "PUT",
            {
                "headers": {
                    "content-type": "application/json",
                    "x-goog-if-generation-match": "0",
                    "x-goog-content-length-range": "0,4194304",
                }
            },
            {
                "content_type": "application/json",
                "headers": {
                    "x-goog-if-generation-match": "0",
                    "x-goog-content-length-range": "0,4194304",
                },
            },
        ),
    ],
)
def test_rsa_signed_url_is_byte_identical_to_google_cloud_storage(
    rsa_signer: RsaSigner, method: str, kwargs: dict[str, Any], lib: dict[str, Any]
) -> None:
    signing = pytest.importorskip("google.cloud.storage._signing")
    from google.auth import crypt
    from google.oauth2 import service_account

    pem = rsa_signer.key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    creds = service_account.Credentials(  # what the library signs with: a real key
        crypt.RSASigner.from_string(pem),
        rsa_signer.signer_email,
        "https://oauth2.googleapis.com/token",
    )
    name = "t/acme/models/mdl_1/3/bundle.zarr.zip"
    ours = rsa_signed_url(
        creds,
        method=method,
        bucket="mapi-prod-bundles",
        name=name,
        expires_in=timedelta(minutes=15),
        now=NOW,
        **kwargs,
    )
    theirs = signing.generate_signed_url_v4(
        creds,
        resource=resource_path("mapi-prod-bundles", name),
        expiration=timedelta(minutes=15),
        api_access_endpoint=GCS_API_ENDPOINT,
        method=method,
        _request_timestamp=TS,
        **lib,
    )
    assert ours == theirs


async def test_blob_signer_signs_urls_and_policies_without_the_library(
    client: FakeClient, rsa_signer: RsaSigner
) -> None:
    store = GcsObjectStore(client, blob_signer=rsa_signer, clock=lambda: NOW)
    meta = await store.put("mapi-prod-data", "t/x/data.zarr.zip", b"data")
    get = await store.sign_get("mapi-prod-data", "t/x/data.zarr.zip", expires_in=timedelta(hours=1))
    _, q = _split(get.url)
    assert q["generation"] == str(meta.generation) and get.generation == meta.generation
    put = await store.sign_put(
        "mapi-prod-runs",
        "t/x/manifest.json",
        expires_in=timedelta(minutes=5),
        content_type="application/json",
        max_bytes=10,
    )
    _, q = _split(put.url)
    assert q["X-Goog-SignedHeaders"] == (
        "content-type;host;x-goog-content-length-range;x-goog-if-generation-match"
    )
    assert put.headers["x-goog-if-generation-match"] == "0"  # write-once, and signed
    assert not [c for c in client.world.calls if c[0] == "generate_signed_url"]
    policy = await store.post_policy(
        "mapi-prod-runs", "t/x/runs/r/a1/", expires_in=timedelta(hours=1), max_bytes=100
    )
    assert policy.fields["x-goog-algorithm"] == "GOOG4-RSA-SHA256"
    sig = bytes.fromhex(policy.fields["x-goog-signature"])
    assert rsa_signer.verify(sig, policy.fields["policy"].encode())


async def test_create_only_policy_uses_the_uploader_signer(
    client: FakeClient, rsa_signer: RsaSigner
) -> None:
    uploader = RsaSigner("uploader@mapi-prod.iam.gserviceaccount.com")
    store = GcsObjectStore(client, blob_signer=rsa_signer, uploader_signer=uploader)
    policy = await store.post_policy(
        "mapi-prod-runs", "t/x/", expires_in=timedelta(hours=1), max_bytes=1, create_only=True
    )
    assert policy.create_only and policy.fields["x-goog-credential"].startswith("uploader@")
    sig = bytes.fromhex(policy.fields["x-goog-signature"])
    assert uploader.verify(sig, policy.fields["policy"].encode())


def _iam_client(responses: list[Any], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response)
        return item

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_iam_signblob_signer_retries_and_refreshes_tokens() -> None:
    seen: list[httpx.Request] = []
    signed = base64.b64encode(b"\x01\x02").decode()
    responses: list[Any] = [
        httpx.Response(429),
        httpx.ConnectError("reset"),
        httpx.Response(503),
        httpx.Response(200, json={"keyId": "k1", "signedBlob": signed}),
    ]
    tokens = iter(["t1", "t2", "t3", "t4"])
    sleeps: list[float] = []
    signer = IamSignBlobSigner(
        "control@mapi-prod.iam.gserviceaccount.com",
        lambda: next(tokens),
        client=_iam_client(responses, seen),
        backoff_s=0.1,
        sleep=sleeps.append,
    )
    assert signer.sign_bytes(b"string-to-sign") == b"\x01\x02"
    assert signer.last_key_id == "k1" and signer.calls == 4
    assert sleeps == [0.1, 0.2, 0.4]
    req = seen[-1]
    assert str(req.url) == (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        "control@mapi-prod.iam.gserviceaccount.com:signBlob"
    )
    assert req.headers["authorization"] == "Bearer t4"  # a fresh token on every attempt
    assert json.loads(req.content) == {"payload": base64.b64encode(b"string-to-sign").decode()}


@pytest.mark.parametrize(
    ("responses", "status"),
    [
        ([httpx.Response(403)], 403),
        ([httpx.Response(503), httpx.Response(503)], 503),
        ([httpx.Response(200, json={"keyId": "k"})], None),
    ],
)
def test_iam_signblob_signer_failures(responses: list[Any], status: int | None) -> None:
    signer = IamSignBlobSigner(
        "sa@p.iam.gserviceaccount.com",
        "static-token",
        client=_iam_client(list(responses), []),
        max_attempts=2,
        sleep=lambda s: None,
    )
    with pytest.raises(IamSignBlobError) as err:
        signer.sign_bytes(b"x")
    assert err.value.status == status
    with pytest.raises(ValueError):
        IamSignBlobSigner("not-an-email", "t")


async def test_for_cloud_run_wires_iam_signers(client: FakeClient) -> None:
    store = GcsObjectStore.for_cloud_run(
        client,
        service_account_email="control@mapi-prod.iam.gserviceaccount.com",
        uploader_service_account_email="uploader@mapi-prod.iam.gserviceaccount.com",
        token=lambda: "tok",
    )
    assert isinstance(store._blob_signer, IamSignBlobSigner)
    assert isinstance(store._uploader_signer, IamSignBlobSigner)
    assert store._uploader_signer.signer_email.startswith("uploader@")


async def test_corrupt_upload_is_deleted_and_reported(client: FakeClient) -> None:
    store = GcsObjectStore(client)
    client.world.corrupt_uploads = True
    with pytest.raises(StorageError) as err:
        await store.put("mapi-prod-runs", "t/x/obj", b"payload", if_generation_match=0)
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE
    assert ("mapi-prod-runs", "t/x/obj") not in client.world.objects  # never left behind


async def test_copy_pins_the_source_generation_and_checks_the_result(client: FakeClient) -> None:
    store = GcsObjectStore(client)
    first = await store.put("mapi-prod-runs", "t/x/a", b"v1")
    second = await store.put("mapi-prod-runs", "t/x/a", b"v2")  # a newer generation exists
    with pytest.raises(NotFound):  # the pinned generation is gone: nothing is copied
        await store.copy(
            "mapi-prod-runs",
            "t/x/a",
            "mapi-prod-bundles",
            "t/x/m/1/a",
            source_generation=first.generation,
        )
    assert client.world.rewrite_calls == 0
    copied = await store.copy(
        "mapi-prod-runs", "t/x/a", "mapi-prod-bundles", "t/x/m/1/a", if_generation_match=0
    )
    assert copied.same_content(second)
    pins = [kw for name, kw in client.world.calls if name == "rewrite"]
    assert pins and all(kw["if_source_generation_match"] == second.generation for kw in pins)
    client.world.corrupt_copies = True
    with pytest.raises(StorageError) as err:
        await store.copy("mapi-prod-runs", "t/x/a", "mapi-prod-bundles", "t/x/m/1/b")
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE
    assert ("mapi-prod-bundles", "t/x/m/1/b") not in client.world.objects
