"""Signed-URL transfers and the bootstrap read-then-delete (the GCS shim + mocked transports)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import httpx
import pytest
from mapi_runner.bootstrap import (
    METADATA_TOKEN_URL,
    BootstrapError,
    fetch_and_delete,
    object_url,
)
from mapi_runner.transfer import TransferError, Transfers, hashes_of, new_client
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import RunnerEnv, new_nonce
from mapi_spec.storage import crc32c, crc32c_to_base64, md5_base64
from shimgcs import ShimGcs

pytestmark = pytest.mark.unit


@pytest.fixture
def transfers() -> Transfers:
    return Transfers(new_client(timeout=10), sleep=lambda _s: None)


# ======================================================================= download


def test_ranged_download_verifies_hashes(
    gcs: ShimGcs, transfers: Transfers, tmp_path: Path
) -> None:
    data = secrets.token_bytes(10_000)
    gcs.put("mapi-test-data", "obj", data)
    get = gcs.sign_get("mapi-test-data", "obj")
    digest = transfers.download(get, tmp_path / "x", chunk=4096)
    assert (tmp_path / "x").read_bytes() == data
    assert digest.crc32c == crc32c_to_base64(crc32c(data)) and digest.md5 == md5_base64(data)
    ranges = [r for r in gcs.requests if r.path.startswith("/mapi-test-data/")]
    assert [r.status for r in ranges] == [206, 206, 206]


def test_empty_object_downloads(gcs: ShimGcs, transfers: Transfers, tmp_path: Path) -> None:
    gcs.put("mapi-test-data", "empty", b"")
    transfers.download(gcs.sign_get("mapi-test-data", "empty"), tmp_path / "e")
    assert (tmp_path / "e").read_bytes() == b""


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"crc32c": "AAAAAA=="}, ErrorCode.INTEGRITY_FAILURE),
        ({"md5_hash": "1B2M2Y8AsgTpgAmY7PhCfg=="}, ErrorCode.INTEGRITY_FAILURE),
        ({"size": 5}, ErrorCode.INTEGRITY_FAILURE),
    ],
)
def test_download_mismatch_is_an_integrity_failure(
    gcs: ShimGcs, transfers: Transfers, tmp_path: Path, override: dict[str, object], code: ErrorCode
) -> None:
    gcs.put("mapi-test-data", "obj", b"0123456789")
    get = gcs.sign_get("mapi-test-data", "obj").model_copy(update=override)
    with pytest.raises(MapiError) as exc:
        transfers.download(get, tmp_path / "x")
    assert exc.value.code is code


def test_download_respects_the_scratch_budget(
    gcs: ShimGcs, transfers: Transfers, tmp_path: Path
) -> None:
    gcs.put("mapi-test-data", "obj", b"x" * 100)
    with pytest.raises(MapiError) as exc:
        transfers.download(gcs.sign_get("mapi-test-data", "obj"), tmp_path / "x", budget=99)
    assert exc.value.code is ErrorCode.RESOURCE_EXHAUSTED
    assert not (tmp_path / "x").exists()


def test_bad_signature_is_refused(gcs: ShimGcs, transfers: Transfers, tmp_path: Path) -> None:
    gcs.put("mapi-test-data", "obj", b"abc")
    get = gcs.sign_get("mapi-test-data", "obj")
    forged = get.model_copy(
        update={"url": get.url.replace("X-Goog-Signature=", "X-Goog-Signature=0")}
    )
    with pytest.raises(TransferError):
        transfers.download(forged, tmp_path / "x")


# ========================================================================= upload


def test_post_policy_upload_streams_and_hashes(
    gcs: ShimGcs, transfers: Transfers, tmp_path: Path
) -> None:
    data = secrets.token_bytes(50_000)
    src = tmp_path / "f"
    src.write_bytes(data)
    policy = gcs.post_policy("mapi-test-runs", "t/x/a1/")
    with open(src, "rb") as fh:
        digest = transfers.post_file(policy, "t/x/a1/ui/f.arrow", fh, len(data), chunk=4096)
    assert gcs.get("mapi-test-runs", "t/x/a1/ui/f.arrow") == data
    meta = gcs.list("mapi-test-runs", "t/x/a1/")[0]
    assert (meta.size, meta.crc32c, meta.md5_hash) == (digest.size, digest.crc32c, digest.md5)


def test_post_outside_the_policy_is_refused_locally(
    gcs: ShimGcs, transfers: Transfers, tmp_path: Path
) -> None:
    policy = gcs.post_policy("mapi-test-runs", "t/x/a1/", max_bytes=10)
    src = tmp_path / "f"
    src.write_bytes(b"x" * 11)
    fd = os.open(src, os.O_RDONLY)
    try:
        for key, size in (("t/x/a1/f", 11), ("t/other/f", 1), ("t/x/a1/../a2/f", 1)):
            with pytest.raises(MapiError) as exc:
                transfers.post_file(policy, key, fd, size)
            assert exc.value.code is ErrorCode.SANDBOX_VIOLATION
    finally:
        os.close(fd)
    assert gcs.list("mapi-test-runs") == []


def test_signed_put_is_write_once(gcs: ShimGcs, transfers: Transfers) -> None:
    put = gcs.sign_put("mapi-test-runs", "t/x/a1/manifest.json", max_bytes=100)
    d = transfers.put_bytes(put, b"{}", content_type="application/json")
    assert d == hashes_of(b"{}")
    with pytest.raises(TransferError) as exc:
        transfers.put_bytes(put, b"{}")
    assert "412" in exc.value.detail
    with pytest.raises(MapiError):
        transfers.put_bytes(put, b"x" * 101)


def test_transient_errors_are_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, content=b"ok")

    t = Transfers(new_client(transport=httpx.MockTransport(handler)), sleep=lambda _s: None)
    from mapi_spec.storage import SignedGet

    get = SignedGet(
        url="http://store/x",
        expires_at="2030-01-01T00:00:00Z",  # type: ignore[arg-type]
        generation=1,
        size=2,
        crc32c=crc32c_to_base64(crc32c(b"ok")),
    )
    assert t.fetch(get, max_bytes=10) == b"ok"
    assert len(calls) == 3
    calls.clear()
    t2 = Transfers(
        new_client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        attempts=2,
        sleep=lambda _s: None,
    )
    with pytest.raises(TransferError):
        t2.fetch(get, max_bytes=10)


# ====================================================================== bootstrap


def _env(gcs: ShimGcs, nonce: str, token: str | None = None) -> RunnerEnv:
    return RunnerEnv(
        run_id="run_1",
        nonce=nonce,
        bootstrap_url=gcs.bootstrap_template(),
        exec_token=gcs.exec_token if token is None else token,
    )


def test_bootstrap_is_fetched_then_deleted(gcs: ShimGcs) -> None:
    nonce = new_nonce()
    meta = gcs.put(gcs.buckets.bootstrap, nonce, b"{}")
    with new_client() as client:
        obj = fetch_and_delete(_env(gcs, nonce), client)
    assert obj.data == b"{}" and obj.generation == meta.generation
    assert not gcs.exists(gcs.buckets.bootstrap, nonce)
    with new_client() as client, pytest.raises(BootstrapError, match="does not exist"):
        fetch_and_delete(_env(gcs, nonce), client)


def test_bootstrap_needs_the_exec_token(gcs: ShimGcs) -> None:
    nonce = new_nonce()
    gcs.put(gcs.buckets.bootstrap, nonce, b"{}")
    with new_client() as client, pytest.raises(BootstrapError, match="401"):
        fetch_and_delete(_env(gcs, nonce, token="wrong"), client)
    assert gcs.exists(gcs.buckets.bootstrap, nonce)


def test_a_failed_delete_is_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"{}", headers={"x-goog-generation": "7"})
        assert request.url.params["generation"] == "7"
        return httpx.Response(403)

    env = RunnerEnv(
        run_id="r", nonce="a" * 64, bootstrap_url="http://shim/storage/v1/b/b/o/{nonce}"
    )
    with (
        new_client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BootstrapError, match="refusing to run"),
    ):
        fetch_and_delete(env, client)


def test_gs_bootstrap_uses_the_metadata_token() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        if str(request.url) == METADATA_TOKEN_URL:
            assert request.headers["metadata-flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 60})
        if request.method == "GET":
            return httpx.Response(200, content=b'{"x": 1}', headers={"x-goog-generation": "3"})
        return httpx.Response(204)

    env = RunnerEnv(run_id="r", nonce="b" * 64, bootstrap_url="gs://mapi-prod-bootstrap/{nonce}")
    assert object_url(env) == (
        "https://storage.googleapis.com/storage/v1/b/mapi-prod-bootstrap/o/" + "b" * 64
    )
    with new_client(transport=httpx.MockTransport(handler)) as client:
        obj = fetch_and_delete(env, client)
    assert obj.data == b'{"x": 1}'
    methods = [(m, auth) for m, _u, auth in seen]
    assert methods == [("GET", None), ("GET", "Bearer tok"), ("DELETE", "Bearer tok")]
    assert seen[2][1].endswith("?generation=3")
