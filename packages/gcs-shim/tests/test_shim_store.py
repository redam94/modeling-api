"""``ShimObjectStore`` satisfies ``mapi_spec.storage.ObjectStore`` against the running shim."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from mapi_gcs_shim import PRINCIPAL_EXEC, InvalidRange, ShimObjectStore
from mapi_gcs_shim.client import raise_for_storage_error
from mapi_gcs_shim.server import ShimServer
from mapi_gcs_shim.transfer import download_signed, upload_post_policy, upload_signed_put
from mapi_spec.errors import ErrorCode
from mapi_spec.localdev import LocalDev
from mapi_spec.storage import (
    AccessDenied,
    NotFound,
    ObjectStore,
    PreconditionFailed,
    StorageError,
    bundle_prefix,
    crc32c,
    crc32c_to_base64,
    run_prefix,
)

pytestmark = pytest.mark.integration

MIN5 = timedelta(minutes=5)


async def test_is_an_object_store(gcs_shim: ShimServer) -> None:
    async with gcs_shim.store() as store:
        assert isinstance(store, ObjectStore)
    assert builtins.list is list


async def test_protocol_semantics_end_to_end(gcs_shim: ShimServer) -> None:
    b = gcs_shim.buckets
    prefix = run_prefix("default", "run_01jabc", 1)
    async with gcs_shim.store() as store:
        m1 = await store.put(
            b.runs,
            prefix + "x",
            b"hello",
            if_generation_match=0,
            content_type="text/plain",
            metadata={"k": "v"},
        )
        assert (m1.size, m1.content_type, m1.metadata) == (5, "text/plain", {"k": "v"})
        with pytest.raises(PreconditionFailed) as pf:
            await store.put(b.runs, prefix + "x", b"again", if_generation_match=0)
        assert pf.value.code is ErrorCode.PRECONDITION_FAILED and pf.value.bucket == b.runs
        m2 = await store.put(b.runs, prefix + "x", b"v2", if_generation_match=m1.generation)
        assert m2.generation > m1.generation
        assert await store.get(b.runs, prefix + "x", byte_range=(0, 0)) == b"v"
        assert await store.get(b.runs, prefix + "x", byte_range=(1, None)) == b"2"
        assert await store.get(b.runs, prefix + "x", generation=m2.generation) == b"v2"
        with pytest.raises(NotFound):
            await store.stat(b.runs, prefix + "x", generation=m1.generation)
        with pytest.raises(NotFound):
            await store.get(b.runs, prefix + "missing")
        with pytest.raises(InvalidRange):
            await store.get(b.runs, prefix + "x", byte_range=(10, None))
        with pytest.raises(ValueError):
            await store.get(b.runs, prefix + "x", byte_range=(3, 1))

        dst = bundle_prefix("default", "model_01", 1) + "x"
        copied = await store.copy(
            b.runs,
            prefix + "x",
            b.bundles,
            dst,
            source_generation=m2.generation,
            if_generation_match=0,
        )
        assert copied.same_content(m2) and copied.bucket == b.bundles
        with pytest.raises(PreconditionFailed):
            await store.copy(b.runs, prefix + "x", b.bundles, dst, if_generation_match=0)
        with pytest.raises(AccessDenied):  # retention-locked
            await store.delete(b.bundles, dst)

        for i in range(5):
            await store.put(b.runs, f"{prefix}many/{i}", b"m")
        page = await store.list(b.runs, prefix=prefix, delimiter="/")
        assert [m.name for m in page.items] == [prefix + "x"] and page.prefixes == [
            prefix + "many/"
        ]
        names: list[str] = []
        token = None
        while True:
            page = await store.list(
                b.runs, prefix=prefix + "many/", max_results=2, page_token=token
            )
            names += [m.name for m in page.items]
            token = page.next_page_token
            if token is None:
                break
        assert names == [f"{prefix}many/{i}" for i in range(5)]

        with pytest.raises(PreconditionFailed):
            await store.delete(b.runs, prefix + "x", if_generation_match=m1.generation)
        await store.delete(b.runs, prefix + "x", generation=m2.generation)
        with pytest.raises(NotFound):
            await store.delete(b.runs, prefix + "x")
        with pytest.raises(NotFound):
            await store.list("mapi-local-missing")
        with pytest.raises(ValueError):
            await store.put(b.runs, "../escape", b"x")


async def test_get_max_bytes_cap(gcs_shim: ShimServer) -> None:
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        await store.put(runs, "t/default/big.json", b"x" * 1000)
        with pytest.raises(StorageError) as err:
            await store.get(runs, "t/default/big.json", max_bytes=999)
        assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE
        assert len(await store.get(runs, "t/default/big.json", max_bytes=1000)) == 1000
        assert (
            await store.get(runs, "t/default/big.json", byte_range=(0, 9), max_bytes=10)
            == b"x" * 10
        )


async def test_signing_methods_and_transfer_helpers(gcs_shim: ShimServer) -> None:
    runs = gcs_shim.buckets.runs
    prefix = run_prefix("default", "run_01jabc", 2)
    async with gcs_shim.store() as store, httpx.AsyncClient() as http:
        with pytest.raises(ValueError):
            await store.sign_put(runs, prefix + "x", expires_in=timedelta(days=8))
        with pytest.raises(ValueError):
            await store.post_policy(runs, "no-slash", expires_in=MIN5, max_bytes=1)
        unconditional = await store.sign_put(
            runs, prefix + "u", expires_in=MIN5, if_generation_match=None
        )
        assert unconditional.headers == {}
        await upload_signed_put(http, unconditional, b"1", send_md5=False)
        await upload_signed_put(http, unconditional, b"2")  # overwrite allowed (admin signer)
        assert await store.get(runs, prefix + "u") == b"2"

        policy = await store.post_policy(runs, prefix, expires_in=MIN5, max_bytes=1024)
        assert policy.allows(prefix + "artifacts/a.json", 10)
        await upload_post_policy(http, policy, prefix + "artifacts/a.json", b"{}")
        signed = await store.sign_get(runs, prefix + "artifacts/a.json", expires_in=MIN5)
        left = signed.expires_at - datetime.now(UTC)
        assert timedelta(minutes=4) < left <= MIN5
        assert await download_signed(http, signed) == b"{}"


async def test_bootstrap_handoff_flow(gcs_shim: ShimServer) -> None:
    """Control writes the RunSpec object; exec reads and deletes it, but can do nothing else."""
    b = gcs_shim.buckets
    nonce = "a" * 64
    async with gcs_shim.store() as control, gcs_shim.store(PRINCIPAL_EXEC) as exec_store:
        await control.put(
            b.bootstrap,
            nonce,
            b'{"schema_version": "mapi.runspec/v1"}',
            content_type="application/json",
            if_generation_match=0,
        )
        assert (await exec_store.get(b.bootstrap, nonce)).startswith(b"{")
        await exec_store.delete(b.bootstrap, nonce)
        with pytest.raises(NotFound):
            await exec_store.get(b.bootstrap, nonce)
        with pytest.raises(AccessDenied):
            await exec_store.put(b.bootstrap, nonce, b"replay")
        with pytest.raises(AccessDenied):
            await exec_store.stat(b.bundles, "anything")


async def test_bucket_extras_and_sweep(gcs_shim: ShimServer) -> None:
    async with gcs_shim.store() as store:
        res = await store.create_bucket(
            "mapi-local-extra",
            retention_period_seconds=60,
            retention_locked=False,
            lifecycle_age_days=3,
        )
        assert res["retentionPolicy"]["retentionPeriod"] == "60"
        assert (await store.get_bucket("mapi-local-extra"))["name"] == "mapi-local-extra"
        with pytest.raises(StorageError) as err:
            await store.create_bucket("mapi-local-extra")
        assert err.value.code is ErrorCode.CONFLICT
        assert await store.sweep_lifecycle() == []
    async with gcs_shim.store(PRINCIPAL_EXEC) as exec_store:
        with pytest.raises(AccessDenied):
            await exec_store.sweep_lifecycle()


async def test_from_root_and_local_dev(gcs_shim: ShimServer, tmp_path: Path) -> None:
    store = ShimObjectStore.from_root(gcs_shim.root, "control")
    assert store.endpoint == gcs_shim.endpoint
    async with store:
        await store.put(gcs_shim.buckets.runs, "t/default/k", b"k")
    local = LocalDev(data_dir=str(gcs_shim.root.parent), gcs_endpoint=gcs_shim.endpoint)
    assert (gcs_shim.root.parent / "gcs") != gcs_shim.root  # fixture roots are not '<data>/gcs'
    with pytest.raises(FileNotFoundError):
        ShimObjectStore.from_local_dev(local)


async def test_transport_failures_become_storage_errors() -> None:
    from mapi_gcs_shim.config import ShimCredentials, default_config

    creds = ShimCredentials.from_principal(default_config().principal("control"))
    async with ShimObjectStore("http://127.0.0.1:9", creds, timeout=2) as store:
        with pytest.raises(StorageError) as err:
            await store.stat("mapi-local-runs", "x")
        assert err.value.code is ErrorCode.INFRA_ERROR
        with pytest.raises(StorageError):
            await store.get("mapi-local-runs", "x")


def test_error_mapping_of_raw_responses() -> None:
    def resp(status: int, body: str, ctype: str = "application/json") -> httpx.Response:
        return httpx.Response(status, content=body.encode(), headers={"content-type": ctype})

    cases: dict[int, type[StorageError]] = {
        404: NotFound,
        412: PreconditionFailed,
        401: AccessDenied,
        403: AccessDenied,
        416: InvalidRange,
        500: StorageError,
    }
    for status, cls in cases.items():
        with pytest.raises(cls):
            raise_for_storage_error(resp(status, '{"error": {"message": "m"}}'))
    with pytest.raises(StorageError, match="SignatureDoesNotMatch: bad &"):
        raise_for_storage_error(
            resp(
                403,
                "<Error><Code>SignatureDoesNotMatch</Code><Message>bad &amp;</Message></Error>",
                "application/xml",
            )
        )
    with pytest.raises(StorageError) as err:
        raise_for_storage_error(resp(413, "too big", "text/plain"))
    assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    with pytest.raises(StorageError) as conflict:
        raise_for_storage_error(resp(409, "{}"))
    assert conflict.value.code is ErrorCode.CONFLICT


async def test_full_read_is_crc_checked(gcs_shim: ShimServer) -> None:
    """A corrupted body (simulated by rewriting the stored bytes) fails the CRC32C check."""
    runs = gcs_shim.buckets.runs
    async with gcs_shim.store() as store:
        meta = await store.put(runs, "t/default/c", b"original")
        data_files = list(
            (gcs_shim.root / "buckets" / runs / "objects").rglob(f"*.{meta.generation}.bin")
        )
        assert len(data_files) == 1
        data_files[0].write_bytes(b"tampered")
        with pytest.raises(StorageError) as err:
            await store.get(runs, "t/default/c")
        assert err.value.code is ErrorCode.INTEGRITY_FAILURE
        assert crc32c_to_base64(crc32c(b"original")) == meta.crc32c
