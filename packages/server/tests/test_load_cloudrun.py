"""Load: 100 concurrent fits through the real dispatcher, reconciler and finalizer with the
``cloudrun-jobs`` executor over FakeCloudRun (plan §14 M2 exit criteria: "100 concurrent fits
without loss", "p95 time-to-RUNNING under 90 s").

The synthetic supervisor reads its bootstrap object from — and writes its outputs, manifest
and checkpoints-free bundle to — the real GCS shim (``ShimObjectStore``, signed URLs and
POST policies over HTTP). Its stdout becomes Cloud Logging entries delivered to
``/internal/events`` as OIDC-authenticated Pub/Sub pushes with random lag (so events arrive
out of order) and duplicates. The backend answers the first ``jobs.run`` calls with 429 and
loses two responses (orphans the executor must adopt instead of resubmitting).

Submissions go through the production rate limits — the dispatcher's token bucket and the
executor's (``DEFAULT_SUBMITS_PER_MINUTE``, below the 180/min ``jobs.run`` quota) — so the
measured time-to-RUNNING is dominated by them, not by the fake's (small) provisioning
latency: real Cloud Run cold starts come on top [verify M0].

Knobs: ``MAPI_LOAD_RUNS`` (100), ``MAPI_LOAD_SUBMITS_PER_MINUTE`` (the executor default) and
``MAPI_LOAD_FIT_MS`` (``200-3000``; e.g. ``40000-60000`` keeps all 100 executions live at once).

Asserts: every run SUCCEEDED (no lost runs), exactly one execution and one attempt per run
(no double dispatch), and p95 time-to-RUNNING (``started_at - queued_at``) under 90 s.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_gcs_shim.server import ShimServer
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors.cloudrun import DEFAULT_SUBMITS_PER_MINUTE, CloudRunJobsExecutor
from mapi_server.executors.fake_cloudrun import FakeCloudRun, FakeLatency, FakeTask
from mapi_server.orchestrator import Orchestrator
from mapi_server.services.pubsub import PushVerifier
from mapi_server.settings import ObjectStoreKind, Settings
from mapi_server_testkit.builders import submit_fit, trend_spec
from mapi_server_testkit.conformance import INVENTORY
from mapi_server_testkit.pubsub import AUDIENCE, PUSH_SA, SUBSCRIPTION, LocalGoogleIdP, PubSubPusher
from mapi_server_testkit.synthetic_runner import MODE_LABEL, SLEEP_LABEL, supervise
from mapi_spec.auth import PushAuthExpectation
from mapi_spec.run import RunState
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RUNS = int(os.environ.get("MAPI_LOAD_RUNS", "100"))
SUBMITS_PER_MINUTE = float(
    os.environ.get("MAPI_LOAD_SUBMITS_PER_MINUTE", str(DEFAULT_SUBMITS_PER_MINUTE))
)
FIT_MS = tuple(int(x) for x in os.environ.get("MAPI_LOAD_FIT_MS", "200-3000").split("-"))
"""Synthetic fit duration range; longer fits raise the number of executions live at once."""
P95_BUDGET_S = 90.0


@pytest.fixture
def settings(settings: Settings, gcs_shim: ShimServer) -> Settings:
    """The suite's settings on the real GCS shim, with production dispatch limits."""
    return settings.model_copy(
        update={
            "object_store": ObjectStoreKind.SHIM,
            "gcs_endpoint": gcs_shim.endpoint,
            "gcs_root": gcs_shim.root,
            "exec_token": None,
            "db_pool_size": 20,
            "dispatch_rate_per_minute": 180.0,
            "dispatch_burst": 10,
            "dispatch_batch": 20,
            "dispatch_poll_s": 0.05,
            "reconcile_interval_s": 0.25,
            "complete_event_grace_s": 30.0,
            "lost_execution_grace_s": 30.0,
            "pubsub_push_audience": AUDIENCE,
            "pubsub_push_service_account": PUSH_SA,
            "pubsub_push_subscriptions": [SUBSCRIPTION],
        }
    )


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return ordered[k]


async def test_100_concurrent_fits_without_loss_or_double_dispatch(
    ctx: ServerContext, gcs_shim: ShimServer, tmp_path: Path, record_property: Any
) -> None:
    idp = LocalGoogleIdP()
    ctx.push_verifier = PushVerifier(
        PushAuthExpectation(audience=AUDIENCE, service_account_email=PUSH_SA),
        idp.jwks_cache(),
        subscriptions=[SUBSCRIPTION],
    )
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    rng = random.Random(7)
    deliveries: set[asyncio.Task[None]] = set()
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as pushes,
        httpx.AsyncClient(timeout=60, limits=limits) as http,
    ):
        pusher = PubSubPusher(pushes, idp, duplicate_rate=0.05, seed=11)

        async def lagging_sink(entry: dict[str, Any]) -> None:
            """Pub/Sub: at-least-once, with lag, so events of a run arrive out of order."""

            async def later() -> None:
                await asyncio.sleep(rng.uniform(0.0, 0.25))
                await pusher.push(entry)

            task = asyncio.create_task(later())
            deliveries.add(task)
            task.add_done_callback(deliveries.discard)

        async def workload(task: FakeTask) -> int:
            return await supervise(task.env, emit=task.print, sigterm=task.sigterm, http=http)

        fake = FakeCloudRun.for_inventory(
            INVENTORY,
            job_env={
                "MAPI_BOOTSTRAP_URL": ctx.settings.bootstrap_url_template(),
                "MAPI_EXEC_TOKEN": gcs_shim.token("exec"),  # the job's service account
            },
            workload=workload,
            log_sink=lagging_sink,
            latency=FakeLatency(run_job_s=0.02, provision_s=0.3, provision_jitter_s=1.2),
            seed=3,
        )
        fake.rate_limit(3)
        fake.lose_response(2)
        executor = CloudRunJobsExecutor(
            fake, INVENTORY, submits_per_minute=SUBMITS_PER_MINUTE, burst=10
        )
        spec_id = await trend_spec(ctx)
        run_ids = await asyncio.gather(
            *(
                submit_fit(
                    ctx,
                    spec_id,
                    labels={MODE_LABEL: "sleep", SLEEP_LABEL: str(rng.randint(*FIT_MS))},
                )
                for _ in range(RUNS)
            )
        )
        assert len(set(run_ids)) == RUNS
        orch = Orchestrator(ctx, executor, use_procrastinate=False)
        started = time.monotonic()
        loop_task = asyncio.create_task(orch.run())
        try:
            deadline = time.monotonic() + 600
            while True:
                async with ctx.db.session() as s:
                    states = list(
                        (
                            await s.execute(select(m.Run.state).where(m.Run.id.in_(run_ids)))
                        ).scalars()
                    )
                if all(RunState(x).terminal for x in states):
                    break
                assert time.monotonic() < deadline, f"runs still active: {sorted(set(states))}"
                assert not loop_task.done(), "the orchestrator stopped"
                await asyncio.sleep(0.5)
            wall = time.monotonic() - started
        finally:
            orch.stop.set()
            await asyncio.wait_for(loop_task, 60)
            if deliveries:
                await asyncio.gather(*deliveries, return_exceptions=True)
            await fake.aclose()

    async with ctx.db.session() as s:
        runs = list((await s.execute(select(m.Run).where(m.Run.id.in_(run_ids)))).scalars())
    problems = {r.id: (r.state, r.problem) for r in runs if r.state != RunState.SUCCEEDED.value}
    assert not problems, f"{len(problems)} runs did not succeed: {list(problems.items())[:3]}"
    assert all(r.attempt == 1 and r.infra_retries == 0 for r in runs)
    for run_id in run_ids:  # one execution per run, even with lost responses and 429s
        executions = fake.executions_for_run(run_id)
        assert len(executions) == 1, (run_id, [e.name for e in executions])
    created = [c for c in fake.run_job_calls if c.outcome in ("created", "lost")]
    assert len(created) == RUNS
    assert [c.outcome for c in fake.run_job_calls].count("throttled") == 3
    assert executor.throttled.total == 3

    to_running = [(r.started_at - r.queued_at).total_seconds() for r in runs]
    p50, p95 = statistics.median(to_running), _pct(to_running, 0.95)
    metrics = {
        "runs": RUNS,
        "submits_per_minute": SUBMITS_PER_MINUTE,
        "wall_seconds": round(wall, 2),
        "time_to_running_p50_s": round(p50, 2),
        "time_to_running_p95_s": round(p95, 2),
        "time_to_running_max_s": round(max(to_running), 2),
        "pushes_delivered": pusher.delivered,
        "pushes_duplicate_results": pusher.results.get("duplicate", 0),
        "max_live_executions": fake.max_live,
    }
    for k, v in metrics.items():
        record_property(k, v)
    (tmp_path / "load-metrics.json").write_text(json.dumps(metrics, indent=2))
    print("\nload metrics:", json.dumps(metrics))
    assert p95 < P95_BUDGET_S, metrics
