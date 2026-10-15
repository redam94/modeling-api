"""The real ``mapi-runner`` supervisor under ``LocalProcessExecutor`` (tiny fit, end to end).

Skipped until the runner package ships its supervisor entry point (``mapi_runner.__main__``)
— it is built in parallel with the server. The test uses the same HTTP object-store front as
``test_e2e_local.py``: signed URLs are opaque to the runner, so any GCS-like front works.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.executors import LocalProcessExecutor
from mapi_server.orchestrator import Orchestrator
from mapi_server_testkit.builders import EXEC_TOKEN, TREND_SPEC
from mapi_server_testkit.store_http import serve_store


def _runner_missing() -> str | None:
    try:
        spec = importlib.util.find_spec("mapi_runner.__main__")
    except ModuleNotFoundError:
        return "mapi-runner is not installed"
    if spec is None:
        return "mapi-runner has no supervisor entry point yet (mapi_runner.__main__)"
    return None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(_runner_missing() is not None, reason=_runner_missing() or ""),
]

CSV = b"date,co2\n" + b"".join(
    f"{2020 + i // 12}-{i % 12 + 1:02d}-01,{400 + 0.1 * i + (i % 12) * 0.3:.3f}\n".encode()
    for i in range(48)
)
SIZE = len(CSV)
MONTHLY = {"kind": "time", "policy": "extend", "freq": "MS"}


async def _wait(client: httpx.AsyncClient, run_id: str, timeout: float) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        run: dict[str, object] = (await client.get(f"/v1/runs/{run_id}")).json()
        if run["state"] in ("succeeded", "failed", "partial", "cancelled"):
            return run
        await asyncio.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


async def test_real_runner_ingest_fit_score(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    ctx.settings.complete_event_grace_s = 30
    async with serve_store(
        ctx.store,  # type: ignore[arg-type]
        exec_token=EXEC_TOKEN,
        bootstrap_bucket=ctx.buckets.bootstrap,
    ) as url:
        executor = LocalProcessExecutor(
            command=[sys.executable, "-m", "mapi_runner"],
            bootstrap_url_template=f"{url}/storage/v1/b/{ctx.buckets.bootstrap}/o/{{nonce}}",
            workdir=ctx.settings.workdir,
            exec_token=EXEC_TOKEN,
        )
        orch = Orchestrator(ctx, executor, use_procrastinate=False)
        task = asyncio.create_task(orch.run())
        try:
            ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
            up = (
                await client.post(
                    f"/v1/datasets/{ds['id']}/uploads",
                    json={
                        "filename": "co2.csv",
                        "content_type": "text/csv",
                        "size_bytes": len(CSV),
                    },
                )
            ).json()
            async with httpx.AsyncClient() as http:
                r = await http.put(
                    up["upload"]["url"], content=CSV, headers=up["upload"]["headers"]
                )
                r.raise_for_status()
            mapping = {
                "format": "csv",
                "dims": {
                    "t": {
                        "column": "date",
                        "spec": {"kind": "time", "policy": "extend", "freq": "MS"},
                    }
                },
                "vars": {"y": {"column": "co2", "role": "target"}},
            }
            dv = (
                await client.post(
                    f"/v1/datasets/{ds['id']}/versions",
                    json={"upload_id": up["id"], "mapping": mapping},
                )
            ).json()
            ingest = await _wait(client, dv["ingest_run_id"], 300)
            assert ingest["state"] == "succeeded", ingest
            spec = (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).json()
            assert spec["latest"]["valid"], spec["latest"]["issues"]
            run = (
                await client.post(
                    "/v1/runs", json={"kind": "fit", "spec_id": spec["id"], "revision": 1}
                )
            ).json()
            fit = await _wait(client, str(run["id"]), 900)
            assert fit["state"] == "succeeded", fit
            model_id = (await client.get("/v1/models")).json()["items"][0]["id"]
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
                f"/v1/models/{model_id}/versions/1:score", json=body, headers={"Prefer": "wait=60"}
            )
            assert r.status_code in (200, 202), r.text
            score = await _wait(client, r.json()["run"]["id"], 600)
            assert score["state"] == "succeeded", score
        finally:
            orch.stop.set()
            await asyncio.wait_for(task, 60)
