"""``mapi-server dev`` starts an embedded GCS shim when none is running."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_server.__main__ import start_embedded_shim
from mapi_server.settings import ObjectStoreKind, Settings
from mapi_server.storage import build_object_store

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _settings(tmp_path: Path, endpoint: str, **kw: object) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        data_dir=tmp_path,
        gcs_endpoint=endpoint,
        image_digest="sha256:" + "ab" * 32,
        **kw,  # type: ignore[arg-type]
    )


def test_starts_a_shim_whose_keys_the_server_uses(tmp_path: Path) -> None:
    settings = _settings(tmp_path, f"http://127.0.0.1:{_free_port()}")
    shim = start_embedded_shim(settings)
    assert shim is not None
    try:
        assert settings.gcs_key_path("control").is_file()
        assert settings.resolved_exec_token()

        async def roundtrip() -> bytes:
            store = build_object_store(settings)
            try:
                await store.put(settings.buckets.runs, "t/default/x", b"hello")
                return await store.get(settings.buckets.runs, "t/default/x")
            finally:
                await store.aclose()  # type: ignore[attr-defined]

        assert asyncio.run(roundtrip()) == b"hello"
    finally:
        shim.stop()


def test_reuses_a_running_shim_and_skips_other_stores(tmp_path: Path) -> None:
    with ShimServer(tmp_path / "gcs") as running:
        assert start_embedded_shim(_settings(tmp_path, running.endpoint)) is None
    memory = _settings(tmp_path, "http://127.0.0.1:9", object_store=ObjectStoreKind.MEMORY)
    assert start_embedded_shim(memory) is None


def test_refuses_to_embed_for_a_remote_endpoint(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not a loopback"):
        start_embedded_shim(_settings(tmp_path, "http://10.255.255.1:4443"))
