"""FINALIZING refuses a tampered output of the *real* runner (plan §8.6, §9).

The real ``mapi-runner`` supervisor + worker ingest a CSV through the GCS shim; between the
runner's ``complete`` event and verification, the attempt prefix is tampered with the way an
attacker with storage access could: an output is swapped for other bytes (a new generation
with a different size/CRC32C/MD5), or an object the runner never claimed is added. Either
way the run must fail as ``integrity`` with a security alert, nothing may be copied into
the content-addressed data bucket, and the dataset version must not become ready.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server.settings import ObjectStoreKind, Settings
from mapi_server.storage import list_all
from mapi_spec.run import MANIFEST_OBJECT_NAME, RunState
from mapi_spec.storage import run_prefix

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.sandbox]

CSV = b"date,co2\n" + b"".join(f"2020-{i:02d}-01,{400 + i}\n".encode() for i in range(1, 13))
MAPPING = {
    "format": "csv",
    "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
    "vars": {"y": {"column": "co2", "role": "target"}},
}


@pytest.fixture
def settings(settings: Settings, gcs_shim: ShimServer) -> Settings:
    """The suite's settings on a real shim (the fixture catalog: no describe run needed)."""
    return settings.model_copy(
        update={
            "object_store": ObjectStoreKind.SHIM,
            "gcs_endpoint": gcs_shim.endpoint,
            "gcs_root": gcs_shim.root,
            "exec_token": None,
            "complete_event_grace_s": 30.0,
            "lost_execution_grace_s": 30.0,
            "reconcile_interval_s": 0.2,
        }
    )


async def _tamper(ctx: ServerContext, run_id: str, attempt: int, how: str) -> str:
    """Change the attempt prefix behind the runner's back; returns what was changed."""
    bucket = ctx.buckets.runs
    prefix = run_prefix(ctx.scope.tenant_id, run_id, attempt)
    objects = await list_all(ctx.store, bucket, prefix)
    claimed = [o for o in objects if not o.name.endswith(MANIFEST_OBJECT_NAME)]
    assert claimed, f"the runner wrote nothing under {prefix}"
    if how == "none":
        return ""
    if how == "swap":
        target = next((o for o in claimed if o.name.endswith("data.zarr.zip")), claimed[0])
        await ctx.store.put(bucket, target.name, b"not what the runner uploaded")
        return target.name
    name = prefix + "smuggled.bin"
    await ctx.store.put(bucket, name, b"an object the manifest does not claim")
    return name


async def _wait(client: httpx.AsyncClient, run_id: str, timeout: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        run: dict[str, Any] = (await client.get(f"/v1/runs/{run_id}")).json()
        if run["state"] in ("succeeded", "failed", "partial", "cancelled", "rejected"):
            return run
        await asyncio.sleep(0.3)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


@pytest.mark.parametrize("how", ["none", "swap", "extra"])
async def test_finalizer_rejects_a_tampered_real_runner_output(
    client: httpx.AsyncClient, ctx: ServerContext, caplog: pytest.LogCaptureFixture, how: str
) -> None:
    executor = LocalProcessExecutor(
        command=[sys.executable, "-m", "mapi_runner"],
        bootstrap_url_template=ctx.settings.bootstrap_url_template(),
        workdir=ctx.settings.workdir,
        exec_token=ctx.settings.resolved_exec_token(),
    )
    orch = Orchestrator(ctx, executor, use_procrastinate=False)
    real_finalize = orch.finalizer.finalize
    tampered: list[str] = []

    async def tamper_then_finalize(run_id: str, attempt: int) -> RunState | None:
        if not tampered:
            tampered.append(await _tamper(ctx, run_id, attempt, how))
        return await real_finalize(run_id, attempt)

    orch.finalizer.finalize = tamper_then_finalize  # type: ignore[method-assign]
    task = asyncio.create_task(orch.run())
    try:
        ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
        up = (
            await client.post(
                f"/v1/datasets/{ds['id']}/uploads",
                json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": len(CSV)},
            )
        ).json()
        async with httpx.AsyncClient() as http:
            put = await http.put(up["upload"]["url"], content=CSV, headers=up["upload"]["headers"])
        assert put.status_code == 200, put.text
        dv = await client.post(
            f"/v1/datasets/{ds['id']}/versions", json={"upload_id": up["id"], "mapping": MAPPING}
        )
        assert dv.status_code == 202, dv.text
        run = await _wait(client, dv.json()["ingest_run_id"], 300)
    finally:
        orch.stop.set()
        await asyncio.wait_for(task, 60)

    assert tampered, "the finalizer never ran"
    if how == "none":  # the control: the same real run verifies and is registered
        assert run["state"] == "succeeded", run
        assert (await client.get(f"/v1/datasets/{ds['id']}/versions/1")).json()["status"] == (
            "ready"
        )
        assert await list_all(ctx.store, ctx.buckets.data, "")
        return
    assert run["state"] == "failed" and run["failure_class"] == "integrity", run
    assert run["problem"]["code"] == "INTEGRITY_FAILURE"
    assert any("SECURITY ALERT" in r.getMessage() for r in caplog.records)
    events = (await client.get(f"/v1/runs/{run['id']}/events")).json()["items"]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["code"] == "INTEGRITY_FAILURE"
    version = (await client.get(f"/v1/datasets/{ds['id']}/versions/1")).json()
    assert version["status"] == "failed"
    # nothing reached the content-addressed data bucket
    assert not await list_all(ctx.store, ctx.buckets.data, "")
