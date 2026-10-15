"""The JSON API over real HTTP: auth, IAM-lite, objects, preconditions, listing, traversal."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_spec.run import RunnerEnv, new_nonce
from mapi_spec.storage import ObjectMeta, bootstrap_key, crc32c, crc32c_to_base64

pytestmark = pytest.mark.integration


def obj_url(shim: ShimServer, bucket: str, name: str | None = None) -> str:
    """JSON-API URL of a bucket's object collection or of one object."""
    base = f"{shim.endpoint}/storage/v1/b/{quote(bucket, safe='')}/o"
    return base if name is None else f"{base}/{quote(name, safe='')}"


def upload_url(shim: ShimServer, bucket: str) -> str:
    return f"{shim.endpoint}/upload/storage/v1/b/{quote(bucket, safe='')}/o"


async def _media_put(
    http: httpx.AsyncClient,
    shim: ShimServer,
    bucket: str,
    name: str,
    data: bytes,
    *,
    principal: str = "control",
    **params: str,
) -> httpx.Response:
    return await http.post(
        upload_url(shim, bucket),
        params={"uploadType": "media", "name": name, **params},
        content=data,
        headers={**shim.auth_headers(principal), "Content-Type": "application/octet-stream"},
    )


# ------------------------------------------------------------------------ auth + IAM


async def test_healthz_needs_no_auth(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    r = await http.get(f"{gcs_shim.endpoint}/_shim/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "env": "local"}


async def test_anonymous_and_bad_tokens_are_401(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    url = obj_url(gcs_shim, gcs_shim.buckets.runs)
    r = await http.get(url)
    assert r.status_code == 401 and r.json()["error"]["errors"][0]["reason"] == "required"
    for header in ("Bearer nope", "Basic abc", f"Token {gcs_shim.token()}"):
        r = await http.get(url, headers={"Authorization": header})
        assert r.status_code == 401, header


async def test_exec_principal_is_limited_to_bootstrap_get_and_delete(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    b = gcs_shim.buckets
    nonce = new_nonce()
    key = bootstrap_key(nonce)
    assert (
        await _media_put(http, gcs_shim, b.bootstrap, key, b'{"runspec": 1}')
    ).status_code == 200
    assert (
        await _media_put(http, gcs_shim, b.bundles, "t/d/models/m/1/x", b"b")
    ).status_code == 200
    assert (await _media_put(http, gcs_shim, b.runs, "t/d/runs/r/a1/x", b"r")).status_code == 200
    ex = gcs_shim.auth_headers("exec")

    # denied: list bootstrap, anything on bundles/runs, writes anywhere, bucket admin
    denied = [
        ("GET", obj_url(gcs_shim, b.bootstrap), None),
        ("GET", obj_url(gcs_shim, b.bundles, "t/d/models/m/1/x"), None),
        ("GET", obj_url(gcs_shim, b.bundles), None),
        ("DELETE", obj_url(gcs_shim, b.bundles, "t/d/models/m/1/x"), None),
        ("GET", obj_url(gcs_shim, b.runs, "t/d/runs/r/a1/x"), None),
        ("GET", f"{gcs_shim.endpoint}/storage/v1/b", None),
        ("POST", f"{gcs_shim.endpoint}/storage/v1/b", {"name": "mapi-local-evil"}),
        ("PATCH", obj_url(gcs_shim, b.bootstrap, key), {"metadata": {"a": "b"}}),
        ("POST", f"{gcs_shim.endpoint}/_shim/v1/lifecycle:sweep", None),
    ]
    for method, url, body in denied:
        r = await http.request(method, url, headers=ex, json=body)
        assert r.status_code == 403, (method, url, r.text)
        assert "does not have" in r.json()["error"]["message"]
    r = await _media_put(http, gcs_shim, b.bootstrap, "other", b"x", principal="exec")
    assert r.status_code == 403
    r = await http.post(
        f"{obj_url(gcs_shim, b.bootstrap, key)}/rewriteTo/b/{b.runs}/o/stolen", headers=ex
    )
    assert r.status_code == 403

    # allowed: the supervisor's bootstrap flow, via the RunnerEnv template URL
    env = RunnerEnv(
        run_id="run_01jabc",
        nonce=nonce,
        bootstrap_url=f"{gcs_shim.endpoint}/storage/v1/b/{b.bootstrap}/o/{{nonce}}",
        exec_token=gcs_shim.token("exec"),
    )
    url = env.resolved_bootstrap_url()
    auth = {"Authorization": f"Bearer {env.exec_token}"}
    r = await http.get(url, params={"alt": "media"}, headers=auth)
    assert r.status_code == 200 and r.content == b'{"runspec": 1}'
    assert (await http.delete(url, headers=auth)).status_code == 204
    assert (await http.get(url, params={"alt": "media"}, headers=auth)).status_code == 404


# ------------------------------------------------------------------------ objects


async def test_media_upload_get_metadata_and_media(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    data = bytes(range(256)) * 40
    r = await _media_put(http, gcs_shim, runs, "t/d/runs/r/a1/blob.bin", data)
    assert r.status_code == 200
    res = r.json()
    meta = ObjectMeta.from_gcs_resource(res)
    assert meta.size == len(data) and meta.crc32c == crc32c_to_base64(crc32c(data))
    assert isinstance(res["generation"], str) and res["kind"] == "storage#object"
    assert res["mediaLink"].startswith(f"{gcs_shim.endpoint}/download/storage/v1/b/{runs}/o/")

    h = gcs_shim.auth_headers()
    url = obj_url(gcs_shim, runs, "t/d/runs/r/a1/blob.bin")
    got = (await http.get(url, headers=h)).json()
    assert got["generation"] == res["generation"]
    media = await http.get(url, params={"alt": "media"}, headers=h)
    assert media.content == data
    assert media.headers["x-goog-generation"] == res["generation"]
    assert media.headers["x-goog-hash"] == f"crc32c={meta.crc32c},md5={meta.md5_hash}"
    ranged = await http.get(url, params={"alt": "media"}, headers={**h, "Range": "bytes=10-19"})
    assert ranged.status_code == 206 and ranged.content == data[10:20]
    assert ranged.headers["content-range"] == f"bytes 10-19/{len(data)}"
    dl = await http.get(res["mediaLink"], headers=h)
    assert dl.status_code == 200 and dl.content == data
    head = await http.head(res["mediaLink"], headers=h)
    assert head.status_code == 200 and head.headers["content-length"] == str(len(data))
    bad = await http.get(url, params={"alt": "xml"}, headers=h)
    assert bad.status_code == 400


async def test_multipart_upload_with_metadata_and_checksums(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs

    def body(resource: dict[str, object], data: bytes) -> bytes:
        return (
            b"--bnd\r\nContent-Type: application/json\r\n\r\n"
            + json.dumps(resource).encode()
            + b"\r\n--bnd\r\nContent-Type: text/csv\r\n\r\n"
            + data
            + b"\r\n--bnd--"
        )

    hdr = {**gcs_shim.auth_headers(), "Content-Type": "multipart/related; boundary=bnd"}
    good = {
        "name": "t/d/x.csv",
        "metadata": {"src": "test"},
        "crc32c": crc32c_to_base64(crc32c(b"a,b")),
    }
    r = await http.post(
        upload_url(gcs_shim, runs),
        params={"uploadType": "multipart"},
        content=body(good, b"a,b"),
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["contentType"] == "text/csv" and r.json()["metadata"] == {"src": "test"}
    wrong = {"name": "t/d/y.csv", "crc32c": "AAAAAA=="}
    r = await http.post(
        upload_url(gcs_shim, runs),
        params={"uploadType": "multipart"},
        content=body(wrong, b"a,b"),
        headers=hdr,
    )
    assert r.status_code == 400 and "CRC32C" in r.json()["error"]["message"]
    r = await http.post(
        upload_url(gcs_shim, runs),
        params={"uploadType": "multipart"},
        content=b"--bnd\r\n\r\n{}\r\n--bnd--",
        headers=hdr,
    )
    assert r.status_code == 400
    r = await http.post(
        upload_url(gcs_shim, runs),
        params={"uploadType": "resumable"},
        headers=gcs_shim.auth_headers(),
    )
    assert r.status_code == 501


async def test_generation_preconditions(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    runs = gcs_shim.buckets.runs
    h = gcs_shim.auth_headers()
    first = await _media_put(http, gcs_shim, runs, "o", b"1", ifGenerationMatch="0")
    gen = first.json()["generation"]
    again = await _media_put(http, gcs_shim, runs, "o", b"2", ifGenerationMatch="0")
    assert again.status_code == 412
    assert again.json()["error"]["errors"][0]["reason"] == "conditionNotMet"
    assert (
        await _media_put(http, gcs_shim, runs, "o", b"2", ifGenerationNotMatch=gen)
    ).status_code == 412
    second = await _media_put(http, gcs_shim, runs, "o", b"2", ifGenerationMatch=gen)
    assert second.status_code == 200
    gen2 = second.json()["generation"]
    url = obj_url(gcs_shim, runs, "o")
    assert (await http.get(url, params={"ifGenerationMatch": gen}, headers=h)).status_code == 412
    assert (
        await http.get(url, params={"ifGenerationNotMatch": gen2}, headers=h)
    ).status_code == 304
    assert (await http.get(url, params={"generation": gen}, headers=h)).status_code == 404
    assert (await http.delete(url, params={"ifGenerationMatch": gen}, headers=h)).status_code == 412
    assert (await http.get(url, params={"ifGenerationMatch": "x"}, headers=h)).status_code == 400
    assert (
        await http.delete(url, params={"ifGenerationMatch": gen2}, headers=h)
    ).status_code == 204
    assert (await http.delete(url, headers=h)).status_code == 404


async def test_rewrite_copy_and_write_once_bundles(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    b = gcs_shim.buckets
    h = gcs_shim.auth_headers()
    src = (await _media_put(http, gcs_shim, b.runs, "t/d/runs/r/a1/bundle.zarr.zip", b"zip")).json()
    dst = "t/d/models/m/1/bundle.zarr.zip"
    rewrite = (
        f"{obj_url(gcs_shim, b.runs, src['name'])}/rewriteTo/b/{b.bundles}/o/{quote(dst, safe='')}"
    )
    r = await http.post(
        rewrite, params={"sourceGeneration": src["generation"], "ifGenerationMatch": "0"}, headers=h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["done"] is True and body["objectSize"] == "3"
    assert body["resource"]["crc32c"] == src["crc32c"] and body["resource"]["bucket"] == b.bundles
    r = await http.post(rewrite, params={"ifGenerationMatch": "0"}, headers=h)
    assert r.status_code == 412
    # write-once: even the admin cannot overwrite or delete a bundle object
    r = await http.post(rewrite, headers=h)
    assert (
        r.status_code == 403 and r.json()["error"]["errors"][0]["reason"] == "retentionPolicyNotMet"
    )
    r = await http.delete(obj_url(gcs_shim, b.bundles, dst), headers=h)
    assert r.status_code == 403
    copy = f"{obj_url(gcs_shim, b.runs, src['name'])}/copyTo/b/{b.runs}/o/copied"
    r = await http.post(
        copy, headers=h, json={"contentType": "application/zip", "metadata": {"k": "v"}}
    )
    assert r.status_code == 200 and r.json()["contentType"] == "application/zip"
    assert r.json()["metadata"] == {"k": "v"}


async def test_patch_metadata(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    runs = gcs_shim.buckets.runs
    await _media_put(http, gcs_shim, runs, "p", b"x")
    url = obj_url(gcs_shim, runs, "p")
    h = gcs_shim.auth_headers()
    r = await http.patch(
        url,
        json={"metadata": {"a": "1"}, "contentType": "text/plain"},
        headers=h,
        params={"ifMetagenerationMatch": "1"},
    )
    assert r.status_code == 200 and r.json()["metageneration"] == "2"
    r = await http.patch(
        url, json={"metadata": {"a": None}}, headers=h, params={"ifMetagenerationMatch": "1"}
    )
    assert r.status_code == 412
    r = await http.patch(url, json=["not", "an", "object"], headers=h)
    assert r.status_code == 400


async def test_list_with_prefix_delimiter_and_pages(
    gcs_shim: ShimServer, http: httpx.AsyncClient
) -> None:
    runs = gcs_shim.buckets.runs
    for n in ["t/a/1", "t/a/2", "t/b/1", "t/c", "u/1"]:
        await _media_put(http, gcs_shim, runs, n, b"x")
    h = gcs_shim.auth_headers()
    r = await http.get(
        obj_url(gcs_shim, runs), params={"prefix": "t/", "delimiter": "/"}, headers=h
    )
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["t/c"]
    assert body["prefixes"] == ["t/a/", "t/b/"] and "nextPageToken" not in body
    names: list[str] = []
    token = None
    while True:
        params = {"prefix": "t/", "maxResults": "2", **({"pageToken": token} if token else {})}
        page = (await http.get(obj_url(gcs_shim, runs), params=params, headers=h)).json()
        names += [i["name"] for i in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    assert names == ["t/a/1", "t/a/2", "t/b/1", "t/c"]
    empty = (await http.get(obj_url(gcs_shim, runs), params={"prefix": "zzz"}, headers=h)).json()
    assert empty == {"kind": "storage#objects"}
    r = await http.get(obj_url(gcs_shim, runs), params={"versions": "true"}, headers=h)
    assert r.status_code == 400


async def test_buckets_api(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    h = gcs_shim.auth_headers()
    base = f"{gcs_shim.endpoint}/storage/v1/b"
    names = [b["name"] for b in (await http.get(base, headers=h)).json()["items"]]
    assert sorted(names) == sorted(gcs_shim.buckets.all().values())
    bundles = (await http.get(f"{base}/{gcs_shim.buckets.bundles}", headers=h)).json()
    assert bundles["retentionPolicy"]["isLocked"] is True
    boot = (await http.get(f"{base}/{gcs_shim.buckets.bootstrap}", headers=h)).json()
    assert boot["lifecycle"]["rule"][0]["condition"] == {"age": 1}
    r = await http.post(
        base,
        headers=h,
        json={
            "name": "mapi-local-scratch",
            "lifecycle": {"rule": [{"action": {"type": "Delete"}, "condition": {"age": 2}}]},
        },
    )
    assert r.status_code == 200 and r.json()["lifecycle"]["rule"][0]["condition"]["age"] == 2
    assert (
        await http.post(base, headers=h, json={"name": "mapi-local-scratch"})
    ).status_code == 409
    for bad in ({"name": "UPPER"}, {"name": "storage"}, {"name": "a..b"}, {"nope": 1}):
        assert (await http.post(base, headers=h, json=bad)).status_code == 400, bad
    assert (await http.delete(f"{base}/mapi-local-scratch", headers=h)).status_code == 204
    assert (await http.get(f"{base}/mapi-local-scratch", headers=h)).status_code == 404


async def test_lifecycle_sweep_endpoint(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    q = gcs_shim.buckets.quarantine
    await _media_put(http, gcs_shim, q, "t/d/uploads/u1", b"x")
    await _media_put(http, gcs_shim, gcs_shim.buckets.runs, "t/d/runs/r/a1/x", b"x")
    sweep = f"{gcs_shim.endpoint}/_shim/v1/lifecycle:sweep"
    h = gcs_shim.auth_headers()
    assert (await http.post(sweep, headers=h)).json() == {"deleted": []}
    later = (datetime.now(UTC) + timedelta(days=8)).isoformat()
    deleted = (await http.post(sweep, headers=h, params={"now": later})).json()["deleted"]
    assert [d["name"] for d in deleted] == ["t/d/uploads/u1"]
    assert (await http.post(sweep, headers=h, params={"now": "yesterday"})).status_code == 400


# ------------------------------------------------------------------------ traversal


@pytest.mark.parametrize(
    "name", ["../etc/passwd", "a/../../b", "a//b", "/abs", "./x", "a/./b", "..", "%00"]
)
async def test_traversal_names_are_rejected(
    gcs_shim: ShimServer, http: httpx.AsyncClient, name: str
) -> None:
    runs = gcs_shim.buckets.runs
    name = name.replace("%00", "\x00")
    r = await _media_put(http, gcs_shim, runs, name, b"x")
    assert r.status_code == 400, r.text
    # percent-encode dots too, so the client cannot normalise the path before sending it
    encoded = quote(name, safe="").replace(".", "%2E")
    r = await http.get(f"{obj_url(gcs_shim, runs)}/{encoded}", headers=gcs_shim.auth_headers())
    assert r.status_code == 400, r.text
    assert {p.name for p in gcs_shim.root.iterdir()} == {"_shim", "buckets", "tmp"}
    assert not list(gcs_shim.root.parent.rglob("passwd"))


async def test_raw_dot_segments_on_the_wire(gcs_shim: ShimServer) -> None:
    """Send literal ``..`` path segments (httpx would normalise them away)."""
    host, port = gcs_shim.host, gcs_shim.port
    for path in (
        f"/storage/v1/b/{gcs_shim.buckets.runs}/o/../../../_shim/config.json?alt=media",
        f"/{gcs_shim.buckets.runs}/../_shim/keys/control.json",
        "/../../etc/passwd",
        "/storage/v1/b/../o/x",
    ):
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Authorization: Bearer {gcs_shim.token()}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        status_line = (await reader.readline()).decode()
        rest = await reader.read()
        writer.close()
        status = int(status_line.split()[1])
        assert status in (400, 403, 404), (path, status_line)
        assert b"secret" not in rest and b"token" not in rest.lower(), path


async def test_unknown_routes_and_methods(gcs_shim: ShimServer, http: httpx.AsyncClient) -> None:
    h = gcs_shim.auth_headers()
    assert (await http.get(f"{gcs_shim.endpoint}/storage/v1/nope", headers=h)).status_code == 404
    assert (await http.put(f"{gcs_shim.endpoint}/storage/v1/b", headers=h)).status_code == 405
    assert (await http.get(f"{gcs_shim.endpoint}/_shim/v2", headers=h)).status_code == 404
    assert (await http.post(obj_url(gcs_shim, gcs_shim.buckets.runs), headers=h)).status_code == 405
