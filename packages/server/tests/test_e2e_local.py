"""End to end through real subprocesses: API → dispatcher → LocalProcessExecutor → a
protocol-following stand-in supervisor (signed URLs over HTTP, HMAC events on stdout) →
reconciler → finalizer → model registry → scoring.

The stand-in (``mapi_server_testkit/fake_runner.py``) replaces ``mapi-runner`` so the whole
control-plane path is exercised without PyMC; ``test_real_runner.py`` runs the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server_testkit.builders import CATALOG, EXEC_TOKEN, TREND_SPEC, ready_dataset
from mapi_server_testkit.store_http import serve_store

pytestmark = pytest.mark.integration

FAKE_RUNNER = Path(__file__).resolve().parent / "mapi_server_testkit" / "fake_runner.py"


@contextlib.asynccontextmanager
async def running(ctx: ServerContext, mode: str = "ok") -> AsyncIterator[str]:
    """Serve the store over HTTP and run an orchestrator with the stand-in supervisor."""
    # events reach the database asynchronously after the pipe drains: give them time
    ctx.settings.complete_event_grace_s = 3
    store = ctx.store
    async with serve_store(
        store,  # type: ignore[arg-type]
        exec_token=EXEC_TOKEN,
        bootstrap_bucket=ctx.buckets.bootstrap,
    ) as url:
        executor = LocalProcessExecutor(
            command=[sys.executable, str(FAKE_RUNNER), mode],
            bootstrap_url_template=f"{url}/storage/v1/b/{ctx.buckets.bootstrap}/o/{{nonce}}",
            workdir=ctx.settings.workdir,
            exec_token=EXEC_TOKEN,
            extra_env={"FAKE_RUNNER_CATALOG": str(CATALOG)},
            cancel_grace_s=2,
        )
        orch = Orchestrator(ctx, executor, use_procrastinate=False)
        task = asyncio.create_task(orch.run())
        try:
            yield url
        finally:
            orch.stop.set()
            await asyncio.wait_for(task, 20)


async def until(check: Callable[[], Awaitable[bool]], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


async def wait_run(
    client: httpx.AsyncClient, run_id: str, timeout: float = 30
) -> dict[str, object]:
    out: dict[str, object] = {}

    async def done() -> bool:
        out.update((await client.get(f"/v1/runs/{run_id}")).json())
        return out["state"] in ("succeeded", "failed", "partial", "cancelled", "rejected")

    await until(done, timeout)
    return out


async def test_ingest_fit_score_end_to_end(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    async with running(ctx):
        # dataset: upload session → PUT the file over HTTP → ingest run
        ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
        content = b"date,co2\n" + b"".join(
            f"2020-{i:02d}-01,{400 + i}\n".encode() for i in range(1, 13)
        )
        up = (
            await client.post(
                f"/v1/datasets/{ds['id']}/uploads",
                json={
                    "filename": "co2.csv",
                    "content_type": "text/csv",
                    "size_bytes": len(content),
                },
            )
        ).json()
        async with httpx.AsyncClient() as http:
            r = await http.put(
                up["upload"]["url"], content=content, headers=up["upload"]["headers"]
            )
            assert r.status_code == 200, r.text
            again = await http.put(
                up["upload"]["url"], content=content, headers=up["upload"]["headers"]
            )
            assert again.status_code == 412  # write-once
        mapping = {
            "format": "csv",
            "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
            "vars": {"y": {"column": "co2", "role": "target"}},
        }
        dv = (
            await client.post(
                f"/v1/datasets/{ds['id']}/versions",
                json={"upload_id": up["id"], "mapping": mapping},
            )
        ).json()
        ingest = await wait_run(client, dv["ingest_run_id"])
        assert ingest["state"] == "succeeded", ingest
        version = (await client.get(f"/v1/datasets/{ds['id']}/versions/1")).json()
        assert version["status"] == "ready"
        # spec → fit
        spec = (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).json()
        assert spec["latest"]["valid"], spec["latest"]["issues"]
        run = (
            await client.post(
                "/v1/runs",
                json={"kind": "fit", "spec_id": spec["id"], "revision": 1},
                headers={"Idempotency-Key": "fit-1"},
            )
        ).json()
        fit = await wait_run(client, str(run["id"]))
        assert fit["state"] == "succeeded", fit
        assert fit["executor"] == "local-process" and fit["output_model_version_id"]
        # bootstrap objects are gone (the supervisor deleted them)
        assert not [k for k in ctx.store.snapshot() if k[0] == ctx.buckets.bootstrap]  # type: ignore[attr-defined]
        # events and logs were ingested from the stdout pipe
        events = (await client.get(f"/v1/runs/{run['id']}/events")).json()["items"]
        types = [e["type"] for e in events]
        assert types[:1] == ["state"] and "metric" in types and types[-1] == "warning"
        assert "complete" in types
        logs = (await client.get(f"/v1/runs/{run['id']}/logs")).json()["items"]
        assert [x["line"] for x in logs] == ["sampling chain 0"]
        # model registry + artifact download through the signed URL (with a range read)
        models = (await client.get("/v1/models")).json()["items"]
        model_id = models[0]["id"]
        mv = (await client.get(f"/v1/models/{model_id}/versions/1")).json()
        assert mv["status"] == "ready" and mv["lineage"]["executor"] == "local-process"
        link = (
            await client.get(f"/v1/models/{model_id}/versions/1/artifacts/bundle.zarr.zip")
        ).json()
        async with httpx.AsyncClient() as http:
            full = await http.get(link["url"])
            part = await http.get(link["url"], headers={"Range": "bytes=0-9"})
        assert full.status_code == 200 and len(full.content) == link["size"] == 2048
        assert part.status_code == 206 and part.content == full.content[:10]
        # score with Prefer: wait → 200 with outputs
        body = {
            "request": {
                "apiVersion": "mapi/v1",
                "kind": "ScoreRequest",
                "model": "model://default/trend@v1",
                "mode": "forecast",
                "horizon": {"dim": "t", "periods": 6},
            }
        }
        r = await client.post(
            f"/v1/models/{model_id}/versions/1:score", json=body, headers={"Prefer": "wait=30"}
        )
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["done"] and result["run"]["state"] == "succeeded"
        assert [o["name"] for o in result["outputs"]] == ["result.json"]


@pytest.mark.parametrize(
    ("mode", "state", "failure", "code"),
    [
        ("fail_user", "failed", "user", "USER_CODE_ERROR"),
        ("oom", "failed", "resource_exhausted", "RESOURCE_EXHAUSTED"),
        ("tamper", "failed", "integrity", "INTEGRITY_FAILURE"),
        ("bad_hmac", "failed", "infra", "INFRA_ERROR"),
        ("partial", "partial", None, None),
    ],
)
async def test_failure_modes(
    client: httpx.AsyncClient,
    ctx: ServerContext,
    mode: str,
    state: str,
    failure: str | None,
    code: str | None,
) -> None:
    ctx.settings.max_infra_retries = 0
    await ready_dataset(ctx)
    spec = (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).json()
    async with running(ctx, mode):
        run = (
            await client.post(
                "/v1/runs", json={"kind": "fit", "spec_id": spec["id"], "revision": 1}
            )
        ).json()
        final = await wait_run(client, str(run["id"]))
    assert final["state"] == state, final["problem"]
    assert final["failure_class"] == failure
    if code is not None:
        assert final["problem"]["code"] == code  # type: ignore[index]
    if mode == "fail_user":
        assert "Traceback" in final["problem"]["untrusted_text"]  # type: ignore[index]
        assert "Traceback" not in final["problem"]["detail"]  # type: ignore[index]


async def test_cancel_a_running_execution(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    await ready_dataset(ctx)
    spec = (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).json()
    async with running(ctx, "hang"):
        run = (
            await client.post(
                "/v1/runs", json={"kind": "fit", "spec_id": spec["id"], "revision": 1}
            )
        ).json()

        async def is_running() -> bool:
            return (await client.get(f"/v1/runs/{run['id']}")).json()["state"] == "running"

        await until(is_running)
        r = await client.post(f"/v1/runs/{run['id']}:cancel")
        assert r.json()["state"] == "cancelling"
        final = await wait_run(client, str(run["id"]))
    assert final["state"] == "cancelled"
    assert os.environ.get("MAPI_RUN_ID") is None  # nothing leaked into the server process
