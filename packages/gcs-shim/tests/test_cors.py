"""CORS for the web app's origin: browsers upload and download through signed URLs."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mapi_gcs_shim.server import ShimServer

WEB = "http://127.0.0.1:5173"


@pytest.fixture
def shim(tmp_path: Path) -> Iterator[ShimServer]:
    with ShimServer(tmp_path / "gcs", cors_origins=[WEB + "/"]) as server:
        yield server


def _signed_put(shim: ShimServer) -> tuple[str, dict[str, str]]:
    async def sign() -> tuple[str, dict[str, str]]:
        async with shim.store() as store:
            put = await store.sign_put(
                shim.buckets.quarantine,
                "t/default/uploads/upl_1",
                expires_in=timedelta(minutes=5),
                content_type="text/csv",
                max_bytes=1024,
            )
            return put.url, put.headers

    return asyncio.run(sign())


def test_preflight_for_a_signed_put_from_the_web_origin(shim: ShimServer) -> None:
    url, headers = _signed_put(shim)
    r = httpx.options(
        url,
        headers={
            "Origin": WEB,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": ",".join(sorted(headers)),
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == WEB
    assert "PUT" in r.headers["access-control-allow-methods"]


def test_cross_origin_put_and_get_expose_the_gcs_headers(shim: ShimServer) -> None:
    url, headers = _signed_put(shim)
    r = httpx.put(url, content=b"a,b\n1,2\n", headers={**headers, "Origin": WEB})
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == WEB
    exposed = r.headers["access-control-expose-headers"].lower()
    assert "x-goog-hash" in exposed and "x-goog-generation" in exposed


def test_other_origins_get_no_cors_grant(shim: ShimServer) -> None:
    url, _ = _signed_put(shim)
    r = httpx.options(
        url,
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_cors_is_off_by_default(tmp_path: Path) -> None:
    with ShimServer(tmp_path / "gcs") as server:
        r = httpx.get(f"{server.endpoint}/_shim/healthz", headers={"Origin": WEB})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers


def test_allowed_hosts_refuse_other_host_headers(tmp_path: Path) -> None:
    with ShimServer(tmp_path / "gcs2", allowed_hosts=["127.0.0.1", "localhost"]) as shim:
        r = httpx.get(f"{shim.endpoint}/_shim/healthz", headers={"Host": "rebound.example"})
        assert r.status_code == 400
        assert httpx.get(f"{shim.endpoint}/_shim/healthz").status_code == 200
    with ShimServer(tmp_path / "gcs3") as shim:  # library default: no check (tests, embedding)
        r = httpx.get(f"{shim.endpoint}/_shim/healthz", headers={"Host": "rebound.example"})
        assert r.status_code == 200
