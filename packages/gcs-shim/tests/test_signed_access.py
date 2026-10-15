"""Signed GET/PUT URLs and V4 POST policies against the running shim."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
import pytest
from mapi_gcs_shim.client import ShimObjectStore
from mapi_gcs_shim.server import ShimServer
from mapi_gcs_shim.transfer import download_signed, upload_post_policy, upload_signed_put
from mapi_spec.storage import (
    AccessDenied,
    PreconditionFailed,
    StorageError,
    crc32c,
    crc32c_to_base64,
    md5_base64,
)

pytestmark = pytest.mark.integration

PREFIX = "t/default/runs/run_01/a1/"
MIN5 = timedelta(minutes=5)


def _xml_code(r: httpx.Response) -> str:
    assert r.headers["content-type"].startswith("application/xml"), r.text
    return r.text.split("<Code>")[1].split("</Code>")[0]


def _with_query(url: str, **changes: str | None) -> str:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in changes.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(params, quote_via=quote)}"


# ------------------------------------------------------------------------- signed GET


async def test_signed_get_full_range_suffix_and_head(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    data = bytes(range(256)) * 20
    async with gcs_shim.store() as store:
        await store.put(
            gcs_shim.buckets.data,
            "t/default/datasets/x/data.zarr.zip",
            data,
            content_type="application/zip",
        )
        signed = await store.sign_get(
            gcs_shim.buckets.data, "t/default/datasets/x/data.zarr.zip", expires_in=MIN5
        )
    assert signed.size == len(data) and signed.crc32c == crc32c_to_base64(crc32c(data))
    assert await download_signed(http, signed) == data

    cases = {
        "bytes=0-0": (206, data[:1], f"bytes 0-0/{len(data)}"),
        "bytes=100-199": (206, data[100:200], f"bytes 100-199/{len(data)}"),
        "bytes=5000-": (206, data[5000:], f"bytes 5000-{len(data) - 1}/{len(data)}"),
        "bytes=-10": (206, data[-10:], f"bytes {len(data) - 10}-{len(data) - 1}/{len(data)}"),
        "bytes=5000-999999": (206, data[5000:], f"bytes 5000-{len(data) - 1}/{len(data)}"),
        "bytes=0-1,5-6": (200, data, None),  # multi-range: served whole
        "items=0-1": (200, data, None),  # unknown unit: ignored
        "bytes=9-3": (200, data, None),  # invalid: ignored
    }
    for header, (status, body, content_range) in cases.items():
        r = await http.get(signed.url, headers={"Range": header})
        assert r.status_code == status, header
        assert r.content == body, header
        assert r.headers.get("content-range") == content_range, header
        assert r.headers["accept-ranges"] == "bytes"
    for header in (f"bytes={len(data)}-", "bytes=-0", f"bytes={len(data) + 5}-{len(data) + 9}"):
        r = await http.get(signed.url, headers={"Range": header})
        assert r.status_code == 416, header
        assert r.headers["content-range"] == f"bytes */{len(data)}"
        assert _xml_code(r) == "InvalidRange"
    assert await download_signed(http, signed, byte_range=(10, 11)) == data[10:12]

    # HEAD needs its own signature (the method is part of the canonical request)
    r = await http.head(signed.url)
    assert r.status_code == 403


async def test_signed_get_is_pinned_to_its_generation(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        v1 = await store.put(runs, PREFIX + "x", b"v1")
        signed = await store.sign_get(runs, PREFIX + "x", expires_in=MIN5)
        assert signed.generation == v1.generation
        await store.put(runs, PREFIX + "x", b"v2")  # overwrite: v1 no longer exists
        r = await http.get(signed.url)
        assert r.status_code == 404 and _xml_code(r) == "NoSuchKey"
        # the generation is signed: it cannot be edited to point at v2
        v2 = await store.stat(runs, PREFIX + "x")
        r = await http.get(_with_query(signed.url, generation=str(v2.generation)))
        assert r.status_code == 403 and _xml_code(r) == "SignatureDoesNotMatch"


async def test_signed_get_tamper_and_expiry(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        await store.put(runs, PREFIX + "a", b"A")
        await store.put(runs, PREFIX + "b", b"B")
        signed = await store.sign_get(runs, PREFIX + "a", expires_in=MIN5)
    assert (await http.get(signed.url)).status_code == 200
    swapped = signed.url.replace(f"{PREFIX}a?", f"{PREFIX}b?")
    assert _xml_code(await http.get(swapped)) == "SignatureDoesNotMatch"
    r = await http.get(_with_query(signed.url, **{"X-Goog-Expires": "604800"}))
    assert _xml_code(r) == "SignatureDoesNotMatch"
    r = await http.get(_with_query(signed.url, **{"X-Goog-Signature": None}))
    assert r.status_code == 403 and "signed URLs" in r.text
    r = await http.get(_with_query(signed.url, **{"X-Goog-Date": None}))
    assert r.status_code == 400 and _xml_code(r) == "AuthorizationQueryParametersError"

    past = datetime.now(UTC) - timedelta(hours=2)
    async with gcs_shim.store(clock=lambda: past) as old_store:
        expired = await old_store.sign_get(runs, PREFIX + "a", expires_in=timedelta(hours=1))
    r = await http.get(expired.url)
    assert r.status_code == 400 and _xml_code(r) == "ExpiredToken"
    future = datetime.now(UTC) + timedelta(hours=1)
    async with gcs_shim.store(clock=lambda: future) as early_store:
        early = await early_store.sign_get(runs, PREFIX + "a", expires_in=MIN5)
    assert _xml_code(await http.get(early.url)) == "RequestNotValidYet"


async def test_unsigned_xml_access_is_denied(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    runs = gcs_shim.buckets.runs
    for method in ("GET", "PUT", "HEAD"):
        r = await http.request(
            method, f"{gcs_shim.endpoint}/{runs}/{PREFIX}x", headers=gcs_shim.auth_headers()
        )
        assert r.status_code == 403, method
    assert (await http.get(f"{gcs_shim.endpoint}/")).status_code == 403
    assert (await http.get(f"{gcs_shim.endpoint}/{runs}")).status_code == 405
    assert (await http.delete(f"{gcs_shim.endpoint}/{runs}/x")).status_code == 405
    r = await http.get(f"{gcs_shim.endpoint}/Bad_Bucket/x")
    assert r.status_code == 400 and _xml_code(r) == "InvalidBucketName"


# ------------------------------------------------------------------------- signed PUT


async def test_signed_put_write_once_and_hash_checks(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        put = await store.sign_put(
            runs,
            PREFIX + "manifest.json",
            expires_in=MIN5,
            content_type="application/json",
            max_bytes=64,
        )
        assert put.headers == {
            "content-type": "application/json",
            "x-goog-if-generation-match": "0",
            "x-goog-content-length-range": "0,64",
        }
        body = b'{"ok": true}'
        result = await upload_signed_put(http, put, body)
        meta = await store.stat(runs, PREFIX + "manifest.json")
        assert result.generation == meta.generation and result.status == 200
        assert (meta.crc32c, meta.md5_hash) == (crc32c_to_base64(crc32c(body)), md5_base64(body))
        assert meta.content_type == "application/json"
        assert result.crc32c == meta.crc32c and result.md5_hash == meta.md5_hash

        # write-once: the same URL cannot overwrite
        with pytest.raises(PreconditionFailed):
            await upload_signed_put(http, put, b'{"ok": false}')

        # too large for the signed content-length-range
        big = await store.sign_put(runs, PREFIX + "big", expires_in=MIN5, max_bytes=8)
        with pytest.raises(StorageError, match="EntityTooLarge"):
            await upload_signed_put(http, big, b"x" * 9)
        with pytest.raises(StorageError):
            await store.stat(runs, PREFIX + "big")

        # digests: Content-MD5 and x-goog-hash must match the body
        dig = await store.sign_put(runs, PREFIX + "dig", expires_in=MIN5)
        bad_md5 = base64.b64encode(hashlib.md5(b"other").digest()).decode()
        r = await http.put(
            dig.url, content=b"data", headers={**dig.headers, "Content-MD5": bad_md5}
        )
        assert r.status_code == 400 and _xml_code(r) == "BadDigest"
        r = await http.put(
            dig.url, content=b"data", headers={**dig.headers, "x-goog-hash": "crc32c=AAAAAA=="}
        )
        assert r.status_code == 400 and _xml_code(r) == "BadDigest"
        r = await http.put(dig.url, content=b"data", headers={**dig.headers, "Content-MD5": "zz"})
        assert r.status_code == 400 and _xml_code(r) == "InvalidDigest"
        good = f"crc32c={crc32c_to_base64(crc32c(b'data'))},md5={md5_base64(b'data')}"
        r = await http.put(dig.url, content=b"data", headers={**dig.headers, "x-goog-hash": good})
        assert r.status_code == 200


async def test_signed_put_headers_are_part_of_the_signature(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        put = await store.sign_put(
            runs, PREFIX + "h", expires_in=MIN5, content_type="application/json", max_bytes=10
        )
    # dropping the write-once header or widening the size range breaks the signature
    hdrs = dict(put.headers)
    hdrs.pop("x-goog-if-generation-match")
    r = await http.put(put.url, content=b"{}", headers=hdrs)
    assert r.status_code == 403 and _xml_code(r) == "SignatureDoesNotMatch"
    r = await http.put(
        put.url, content=b"{}", headers={**put.headers, "x-goog-content-length-range": "0,999999"}
    )
    assert _xml_code(r) == "SignatureDoesNotMatch"
    r = await http.put(put.url, content=b"{}", headers={**put.headers, "content-type": "text/html"})
    assert _xml_code(r) == "SignatureDoesNotMatch"
    r = await http.put(put.url, content=b"{}", headers={**put.headers, "x-goog-meta-evil": "1"})
    assert r.status_code == 400 and _xml_code(r) == "MissingSecurityHeader"
    # a PUT URL is not a GET URL
    r = await http.get(put.url)
    assert _xml_code(r) == "SignatureDoesNotMatch"
    r = await http.put(put.url, content=b"{}", headers=put.headers)
    assert r.status_code == 200


async def test_signed_urls_carry_the_signers_authority(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    b = gcs_shim.buckets
    async with gcs_shim.store() as admin:
        await admin.put(b.bundles, "t/default/models/m/1/bundle.zarr.zip", b"bundle")
        await admin.put(b.bootstrap, "f" * 64, b"spec")
    async with gcs_shim.store("exec") as exec_store:
        # exec can sign, but its URLs only work where exec itself has access
        put = await exec_store.sign_put(b.runs, PREFIX + "x", expires_in=MIN5)
        r = await http.put(put.url, content=b"x", headers=put.headers)
        assert r.status_code == 403 and _xml_code(r) == "AccessDenied"
        boot_put = await exec_store.sign_put(b.bootstrap, "e" * 64, expires_in=MIN5)
        r = await http.put(boot_put.url, content=b"x", headers=boot_put.headers)
        assert r.status_code == 403  # get+delete only, no create
        pol = await exec_store.post_policy(b.runs, PREFIX, expires_in=MIN5, max_bytes=10)
        with pytest.raises(AccessDenied):
            await upload_post_policy(http, pol, PREFIX + "y", b"y")
        with pytest.raises(AccessDenied):  # sign_get needs a stat, which exec may not do
            await exec_store.sign_get(
                b.bundles, "t/default/models/m/1/bundle.zarr.zip", expires_in=MIN5
            )
        with pytest.raises(AccessDenied):  # listing the bootstrap bucket is forbidden
            await exec_store.list(b.bootstrap)
        assert await exec_store.get(b.bootstrap, "f" * 64) == b"spec"


async def test_signed_put_streams_a_large_body(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    data = bytes(range(251)) * 12000  # ~3 MB, several server-side chunks
    async with gcs_shim.store() as store:
        put = await store.sign_put(runs, PREFIX + "large.bin", expires_in=MIN5, max_bytes=len(data))
        await upload_signed_put(http, put, data)
        meta = await store.stat(runs, PREFIX + "large.bin")
        assert meta.size == len(data) and meta.crc32c == crc32c_to_base64(crc32c(data))
        assert (
            await store.get(runs, PREFIX + "large.bin", byte_range=(2_000_000, 2_000_009))
            == (data[2_000_000:2_000_010])
        )


# ------------------------------------------------------------------------- POST policy


async def test_post_policy_upload_and_violations(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        policy = await store.post_policy(runs, PREFIX, expires_in=MIN5, max_bytes=100, min_bytes=2)
        assert policy.url == f"{gcs_shim.endpoint}/{runs}"
        res = await upload_post_policy(
            http,
            policy,
            PREFIX + "artifacts/plots.json",
            b'{"p":1}',
            content_type="application/json",
        )
        assert res.status == 204 and res.generation is not None
        meta = await store.stat(runs, PREFIX + "artifacts/plots.json")
        assert meta.content_type == "application/json" and meta.size == 7

        # key outside the prefix, traversal, too large, too small
        with pytest.raises(AccessDenied, match="InvalidPolicyDocument"):
            await upload_post_policy(http, policy, "t/default/runs/OTHER/a1/x", b"xx")
        with pytest.raises(StorageError, match="invalid object name"):
            await upload_post_policy(http, policy, PREFIX + "../../escape", b"xx")
        with pytest.raises(StorageError, match="EntityTooLarge"):
            await upload_post_policy(http, policy, PREFIX + "big", b"x" * 101)
        with pytest.raises(StorageError, match="EntityTooSmall"):
            await upload_post_policy(http, policy, PREFIX + "small", b"x")
        for name in ("big", "small"):
            with pytest.raises(StorageError):
                await store.stat(runs, PREFIX + name)

        # a field that is not in the policy, a tampered policy, the wrong bucket
        r = await http.post(
            policy.url,
            data={"key": PREFIX + "z", **policy.fields, "acl": "public"},
            files={"file": ("z", b"zz")},
        )
        assert r.status_code == 403 and _xml_code(r) == "InvalidPolicyDocument"
        fields = {**policy.fields, "policy": policy.fields["policy"][:-4] + "AAAA"}
        r = await http.post(
            policy.url, data={"key": PREFIX + "z", **fields}, files={"file": ("z", b"zz")}
        )
        assert _xml_code(r) == "SignatureDoesNotMatch"
        r = await http.post(
            f"{gcs_shim.endpoint}/{gcs_shim.buckets.data}",
            data={"key": PREFIX + "z", **policy.fields},
            files={"file": ("z", b"zz")},
        )
        assert r.status_code == 403 and _xml_code(r) == "InvalidPolicyDocument"
        r = await http.post(policy.url, data={"key": PREFIX + "z", **policy.fields})
        assert r.status_code == 400  # no file part
        r = await http.post(
            policy.url, content=b"not multipart", headers={"Content-Type": "text/plain"}
        )
        assert r.status_code == 400

        # success_action_status=201 returns the XML PostResponse (must be in the policy: it isn't)
        r = await http.post(
            policy.url,
            data={"key": PREFIX + "s", **policy.fields, "success_action_status": "201"},
            files={"file": ("s", b"ss")},
        )
        assert r.status_code == 403
        r = await http.post(
            policy.url,
            data={"key": PREFIX + "s", **policy.fields, "x-ignore-note": "fine"},
            files={"file": ("s", b"ss")},
        )
        assert r.status_code == 204 and r.headers["location"].endswith(f"/{runs}/{PREFIX}s")


async def test_create_only_post_policy_refuses_overwrites(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    """A create-only policy cannot replace an object, even when its signer could delete one
    (the control principal), so it can't overwrite a write-once manifest or a verified
    output. A plain policy from the same signer still overwrites (GCS semantics)."""
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        once = await store.post_policy(
            runs, PREFIX, expires_in=MIN5, max_bytes=100, create_only=True
        )
        assert once.create_only and once.fields["x-goog-if-generation-match"] == "0"
        res = await upload_post_policy(http, once, PREFIX + "out.json", b"first")
        assert res.generation is not None
        with pytest.raises(PreconditionFailed):
            await upload_post_policy(http, once, PREFIX + "out.json", b"second")
        assert await store.get(runs, PREFIX + "out.json") == b"first"

        put = await store.sign_put(runs, PREFIX + "manifest.json", expires_in=MIN5)
        await upload_signed_put(http, put, b"{}")
        with pytest.raises(PreconditionFailed):
            await upload_post_policy(http, once, PREFIX + "manifest.json", b"forged")
        assert await store.get(runs, PREFIX + "manifest.json") == b"{}"

        # the precondition is part of the signed policy: dropping it from the form fails
        fields = {k: v for k, v in once.fields.items() if k != "x-goog-if-generation-match"}
        r = await http.post(
            once.url, data={"key": PREFIX + "z", **fields}, files={"file": ("z", b"zz")}
        )
        assert r.status_code == 403 and _xml_code(r) == "InvalidPolicyDocument"

        plain = await store.post_policy(runs, PREFIX, expires_in=MIN5, max_bytes=100)
        assert not plain.create_only and "x-goog-if-generation-match" not in plain.fields
        await upload_post_policy(http, plain, PREFIX + "out.json", b"third")
        assert await store.get(runs, PREFIX + "out.json") == b"third"


async def test_post_policy_expiry(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    async with gcs_shim.store(clock=lambda: past) as store:
        policy = await store.post_policy(
            gcs_shim.buckets.runs, PREFIX, expires_in=timedelta(hours=1), max_bytes=10
        )
    with pytest.raises(StorageError, match="ExpiredToken"):
        await upload_post_policy(http, policy, PREFIX + "late", b"x")


async def test_public_endpoint_is_used_in_signed_urls(gcs_shim: ShimServer) -> None:
    public = f"http://localhost:{gcs_shim.port}"
    async with (
        ShimObjectStore(gcs_shim.endpoint, gcs_shim.credentials(), public_endpoint=public) as store,
        httpx.AsyncClient() as http,
    ):
        await store.put(gcs_shim.buckets.runs, PREFIX + "p", b"pub")
        signed = await store.sign_get(gcs_shim.buckets.runs, PREFIX + "p", expires_in=MIN5)
        assert signed.url.startswith(public + "/")
        assert (await http.get(signed.url)).content == b"pub"
        # the host is signed: the same URL through another host name does not verify
        other = signed.url.replace(public, gcs_shim.endpoint)
        assert (await http.get(other)).status_code == 403
