"""Signed PUT header handling and POST-policy response modes, signed by hand."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta

import httpx
import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_gcs_shim.signing import SigningIdentity, build_post_policy, hmac_policy_signer, sign_url
from mapi_spec.storage import md5_base64

pytestmark = pytest.mark.integration

PREFIX = "t/default/runs/run_02/a1/"


def _identity(shim: ShimServer) -> SigningIdentity:
    creds = shim.credentials()
    return SigningIdentity(creds.client_email, creds.secret.get_secret_value())


def _put_url(shim: ShimServer, name: str, headers: dict[str, str]) -> str:
    return sign_url(
        _identity(shim),
        method="PUT",
        endpoint=shim.endpoint,
        bucket=shim.buckets.runs,
        name=name,
        expires_in=timedelta(minutes=5),
        headers=headers,
    )


def _code(r: httpx.Response) -> str:
    return r.text.split("<Code>")[1].split("</Code>")[0]


async def test_signed_put_metadata_and_size_range(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    headers = {"x-goog-meta-run": "run_02", "x-goog-content-length-range": "3,10"}
    url = _put_url(gcs_shim, PREFIX + "m", headers)
    r = await http.put(url, content=b"ab", headers=headers)
    assert r.status_code == 400 and _code(r) == "EntityTooSmall"
    r = await http.put(url, content=b"abcd", headers=headers)
    assert r.status_code == 200
    async with gcs_shim.store() as store:
        meta = await store.stat(gcs_shim.buckets.runs, PREFIX + "m")
    assert meta.metadata == {"run": "run_02"} and meta.size == 4
    assert r.headers["etag"] == f'"{hashlib.md5(b"abcd").hexdigest()}"'


@pytest.mark.parametrize("value", ["abc", "5,1", "-1,4", "1,2,3"])
async def test_malformed_content_length_range(
    gcs_shim: ShimServer, http: httpx.AsyncClient, value: str
) -> None:
    headers = {"x-goog-content-length-range": value}
    r = await http.put(_put_url(gcs_shim, PREFIX + "r", headers), content=b"x", headers=headers)
    assert r.status_code == 400 and "content-length-range" in r.text


async def test_content_md5_and_x_goog_hash_must_agree(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    url = _put_url(gcs_shim, PREFIX + "d", {})
    md5 = md5_base64(b"data")
    other = base64.b64encode(hashlib.md5(b"else").digest()).decode()
    r = await http.put(
        url, content=b"data", headers={"Content-MD5": md5, "x-goog-hash": f"md5={other}"}
    )
    assert r.status_code == 400 and _code(r) == "BadDigest"
    r = await http.put(url, content=b"data", headers={"x-goog-hash": "garbage"})
    assert r.status_code == 400
    r = await http.put(url, content=b"data", headers={"x-goog-hash": "crc32c=notbase64"})
    assert r.status_code == 400
    r = await http.put(
        url, content=b"data", headers={"Content-MD5": md5, "x-goog-hash": f"md5={md5}"}
    )
    assert r.status_code == 200


async def test_signed_put_overwrite_and_declared_length(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    headers = {"x-goog-content-length-range": "0,4"}
    url = _put_url(gcs_shim, PREFIX + "o", headers)
    assert (await http.put(url, content=b"1", headers=headers)).status_code == 200
    assert (await http.put(url, content=b"22", headers=headers)).status_code == 200  # no IGM
    r = await http.put(url, content=b"x" * 50, headers=headers)
    assert r.status_code == 400 and _code(r) == "EntityTooLarge"


def _policy_fields(shim: ShimServer, *extra: object) -> dict[str, str]:
    ident = _identity(shim)
    fields, _ = build_post_policy(
        access_id=ident.access_id,
        bucket=shim.buckets.runs,
        prefix=PREFIX,
        min_bytes=0,
        max_bytes=100,
        expires_in=timedelta(minutes=5),
        sign=hmac_policy_signer(ident.secret),
        extra_conditions=list(extra),
    )
    return fields


async def test_post_policy_response_modes_metadata_and_filename(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    url = f"{gcs_shim.endpoint}/{gcs_shim.buckets.runs}"
    fields = _policy_fields(
        gcs_shim,
        ["eq", "$success_action_status", "201"],
        {"x-goog-meta-source": "runner"},
    )
    form = {
        "key": PREFIX + "${filename}",
        **fields,
        "success_action_status": "201",
        "x-goog-meta-source": "runner",
    }
    r = await http.post(url, data=form, files={"file": ("plots.json", b"[]", "application/json")})
    assert r.status_code == 201, r.text
    assert f"<Key>{PREFIX}plots.json</Key>" in r.text and "<ETag>" in r.text
    async with gcs_shim.store() as store:
        meta = await store.stat(gcs_shim.buckets.runs, PREFIX + "plots.json")
    assert meta.metadata == {"source": "runner"}

    fields200 = _policy_fields(gcs_shim, {"success_action_status": "200"})
    r = await http.post(
        url,
        data={"key": PREFIX + "b", **fields200, "success_action_status": "200"},
        files={"file": ("b", b"b")},
    )
    assert r.status_code == 200 and r.headers["x-goog-generation"]

    # malformed forms
    body = (
        b'--x\r\nContent-Disposition: form-data; name="key"\r\n\r\nk\r\n'
        b'--x\r\nContent-Disposition: form-data; name="key"\r\n\r\nk\r\n--x--'
    )
    r = await http.post(
        url, content=body, headers={"Content-Type": "multipart/form-data; boundary=x"}
    )
    assert r.status_code == 400 and "duplicate" in r.text
    body = b"--x\r\nContent-Disposition: form-data\r\n\r\nk\r\n--x--"
    r = await http.post(
        url, content=body, headers={"Content-Type": "multipart/form-data; boundary=x"}
    )
    assert r.status_code == 400 and "needs a name" in r.text
    body = b'--x\r\nContent-Disposition: form-data; name="a"\r\n\r\n\xff\xfe\r\n--x--'
    r = await http.post(
        url, content=body, headers={"Content-Type": "multipart/form-data; boundary=x"}
    )
    assert r.status_code == 400 and "UTF-8" in r.text
