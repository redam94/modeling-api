"""Quotas at run creation, request-rate limits, kill switches, weighted fair-share leasing in
the dispatcher, and the cost ledger (metered attempts → ``Run.cost`` and ``/v1/usage``)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.observability import metrics as obs_metrics
from mapi_server.observability.metrics import Metrics
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.queue import InlineFinalizeQueue
from mapi_server.services.reconciler import Reconciler
from mapi_server.services.runs import load_run, now
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.api import Run, UsageReport, new_id
from mapi_spec.cost import DEFAULT_PRICE_TABLE, estimate_cost
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

pytestmark = pytest.mark.integration


async def _spec(client: httpx.AsyncClient) -> str:
    await_ = await client.post("/v1/specs", json={"yaml": TREND_SPEC})
    assert await_.status_code == 201, await_.text
    return str(await_.json()["id"])


async def _policy(ctx: ServerContext, scope: str, subject: str, **limits: object) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            insert(m.QuotaPolicy)
            .values(scope=scope, subject_id=subject, limits=limits, updated_at=now())
            .on_conflict_do_update(index_elements=["scope", "subject_id"], set_={"limits": limits})
        )


async def _runs(ctx: ServerContext) -> list[m.Run]:
    async with ctx.db.session() as s:
        return list((await s.execute(select(m.Run).order_by(m.Run.created_at))).scalars())


# ============================================================================= admission


@pytest.fixture
def metrics() -> Iterator[Metrics]:
    """A throwaway Prometheus registry so assertions see only this test's observations."""
    previous = obs_metrics.METRICS
    fresh = Metrics()
    obs_metrics.set_metrics(fresh)
    try:
        yield fresh
    finally:
        obs_metrics.set_metrics(previous)


def _sample(name: str, **labels: str) -> float:
    for family in text_string_to_metric_families(obs_metrics.render().decode()):
        for sample in family.samples:
            if sample.name == name and labels.items() <= sample.labels.items():
                return float(sample.value)
    return 0.0


async def test_run_creation_is_admitted_against_tenant_quotas(
    ctx: ServerContext, client: httpx.AsyncClient, metrics: Metrics
) -> None:
    await ready_dataset(ctx)
    spec_id = await _spec(client)
    fit = {"kind": "fit", "spec_id": spec_id, "revision": 1}
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 201, r.text
    first = Run.model_validate(r.json())
    assert first.cost.estimated and first.cost.estimate_micros
    expected = estimate_cost(ResourceClass.S, 300.0, table=DEFAULT_PRICE_TABLE)
    assert first.cost.estimate_micros == expected.amount_micros
    [row] = await _runs(ctx)
    assert row.priority == "batch" and row.created_by is not None

    await _policy(ctx, "tenant", "default", concurrent_runs=1)
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXCEEDED"
    assert "concurrent_runs" in r.json()["detail"] and "retry-after" not in r.headers
    assert len(await _runs(ctx)) == 1  # nothing was created

    await _policy(ctx, "tenant", "default", vcpu_seconds_per_day=700)  # 600 in flight + 600
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 429 and "vcpu_seconds_per_day" in r.json()["detail"]
    assert int(r.headers["retry-after"]) > 0

    await _policy(ctx, "tenant", "default", max_resource_class="S")
    r = await client.post("/v1/runs", json={**fit, "resource_class": "M"})
    assert r.status_code == 429 and "max_resource_class" in r.json()["detail"]
    assert (await client.post("/v1/runs", json=fit)).status_code == 201

    async with ctx.db.session() as s:
        denied = [
            e for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.action == "quota.denied"
        ]  # fmt: skip
    assert len(denied) == 3 and all(e.outcome == "denied" for e in denied)
    assert {e.details["limit_name"] for e in denied} == {
        "concurrent_runs", "vcpu_seconds_per_day", "max_resource_class",
    }  # fmt: skip
    # the MapiQuota* alerts key on this counter
    assert _sample("mapi_quota_rejections_total", scope="tenant", code="QUOTA_EXCEEDED") == 3


async def test_token_vcpu_budget_and_global_concurrency(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await ready_dataset(ctx)
    spec_id = await _spec(client)
    fit = {"kind": "fit", "spec_id": spec_id, "revision": 1}
    owner = await make_user(ctx, "ci@default.test")
    await grant_role(ctx, owner, "default", None, "runner")
    secret = await make_token(ctx, owner, "default", "default", ["runs:create"],
                              vcpu_seconds_budget=1000, budget_period="day")  # fmt: skip
    tok = token_caller(client, secret)
    assert (await tok.post("/v1/runs", json=fit)).status_code == 201  # 600 vCPU-s estimate
    r = await tok.post("/v1/runs", json=fit)
    assert r.status_code == 429 and r.json()["detail"].startswith("token tok_")
    # the local admin has no token budget, but the global level still applies
    await _policy(ctx, "global", "*", concurrent_runs=1)
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 429 and r.json()["detail"].startswith("global")


async def test_kill_switches_refuse_new_compute(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, metrics: Metrics
) -> None:
    import mapi_server.services.submit as submit

    await ready_dataset(ctx)
    spec_id = await _spec(client)
    fit = {"kind": "fit", "spec_id": spec_id, "revision": 1}
    r = await client.put(
        "/v1/admin/kill-switches",
        json={"untrusted_execution_enabled": False, "reason": "drill"},
    )
    assert r.status_code == 200 and r.json()["untrusted_execution_enabled"] is False
    assert (await client.post("/v1/runs", json=fit)).status_code == 201  # built-in: fine
    monkeypatch.setattr(submit, "_overlay", lambda *a: True)  # pretend it ships tenant code
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 429 and "uploaded plugin code is paused" in r.json()["detail"]
    monkeypatch.undo()
    # the spend cap: internal tenants are exempt, others stop once it is reached
    await client.put(
        "/v1/admin/kill-switches", json={"global_spend_cap_micros": 1000, "reason": "beta cap"}
    )
    async with ctx.db.session() as s:
        s.add(
            m.CostLedgerEntry(
                id=new_id("cle"), tenant_id="other", project_id="p", run_id="run_x", attempt=1,
                kind="actual", resource_class="S", task_count=1, wall_seconds=1,
                vcpu_seconds=2, gib_seconds=8, price_version="v", amount_micros=5000,
                currency="USD", recorded_at=now(),
            )
        )  # fmt: skip
    assert (await client.post("/v1/runs", json=fit)).status_code == 201  # default: internal
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).values(trust_tier="self-hosted-trusted"))
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 429 and "spend cap" in r.json()["detail"]
    assert int(r.headers["retry-after"]) > 0
    # the spend-cap dashboard panel and its 70 %/90 % alerts read these gauges
    assert _sample("mapi_global_spend_micros") == 5000
    assert _sample("mapi_global_spend_cap_micros") == 1000
    switches = (await client.get("/v1/admin/kill-switches")).json()
    assert switches["global_spend_cap_micros"] == 1000 and switches["reason"] == "beta cap"


async def test_tenant_request_rate_limit(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    await make_tenant(ctx, "acme", projects=("growth",))
    u = await make_user(ctx, "ana@acme.test")
    await grant_role(ctx, u, "acme", None, "viewer")
    ana = await session_caller(ctx, client, u, tenant_id="acme", project_id="growth")
    await _policy(ctx, "tenant", "acme", requests_per_minute=2)
    assert [(await ana.get("/v1/runs")).status_code for _ in range(2)] == [200, 200]
    r = await ana.get("/v1/runs")
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    assert "Retry-After" in r.headers
    # another tenant is unaffected
    assert (await client.get("/v1/runs")).status_code == 200


async def test_storage_quota_blocks_uploads_only(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ds = (await client.post("/v1/datasets", json={"name": "big"})).json()
    await _policy(ctx, "tenant", "default", storage_bytes=1000)
    body = {"filename": "x.csv", "content_type": "text/csv", "size_bytes": 2000}
    r = await client.post(f"/v1/datasets/{ds['id']}/uploads", json=body)
    assert r.status_code == 429 and "storage_bytes" in r.json()["detail"]
    assert (await client.get(f"/v1/datasets/{ds['id']}")).status_code == 200


async def test_concurrent_uploads_cannot_overrun_the_storage_quota(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """Storage admission is serialized per tenant (the tenant row is locked until the upload
    row commits), so concurrent intents cannot each pass against the same usage."""
    ds = (await client.post("/v1/datasets", json={"name": "race"})).json()
    await _policy(ctx, "tenant", "default", storage_bytes=1000)
    body = {"filename": "a.csv", "content_type": "text/csv", "size_bytes": 800}
    url = f"/v1/datasets/{ds['id']}/uploads"
    for _ in range(3):  # the race is timing-dependent: try a few rounds
        rs = await asyncio.gather(*[client.post(url, json=body) for _ in range(6)])
        assert all(r.status_code in (201, 429) for r in rs), [r.text for r in rs]
        async with ctx.db.session() as s:
            total = (await s.execute(select(func.sum(m.Upload.size_bytes)))).scalar_one()
        assert total == 800, total  # exactly one intent fits


async def test_abandoned_upload_intents_stop_counting_once_expired(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """An upload intent reserves its declared size only until its signed PUT expires; the
    housekeeper then marks it expired and deletes whatever reached quarantine."""
    from mapi_server.services.housekeeping import Housekeeper
    from mapi_spec.storage import NotFound

    ds = (await client.post("/v1/datasets", json={"name": "abandoned"})).json()
    await _policy(ctx, "tenant", "default", storage_bytes=1000)
    body = {"filename": "a.csv", "content_type": "text/csv", "size_bytes": 800}
    url = f"/v1/datasets/{ds['id']}/uploads"
    first = await client.post(url, json=body)
    assert first.status_code == 201
    assert (await client.post(url, json=body)).status_code == 429  # 800 + 800 > 1000
    async with ctx.db.session() as s:
        row = await s.get(m.Upload, first.json()["id"])
        assert row is not None
        row.expires_at = now() - timedelta(minutes=1)  # the browser was closed
        bucket, name = row.bucket, row.object_name
    await ctx.store.put(bucket, name, b"half a file")
    second = await client.post(url, json=body)
    assert second.status_code == 201, second.text  # the expired intent no longer counts
    assert await Housekeeper(ctx).sweep_once() >= 1
    async with ctx.db.session() as s:
        statuses = {
            u.id: u.status for u in (await s.execute(select(m.Upload))).scalars()
        }  # fmt: skip
    assert statuses == {first.json()["id"]: "expired", second.json()["id"]: "pending"}
    with pytest.raises(NotFound):
        await ctx.store.stat(bucket, name)


# ============================================================================ fair share


async def _queue(
    ctx: ServerContext,
    tenant: str,
    *,
    kind: str = "fit",
    state: str = "queued",
    minutes_ago: float = 0,
    untrusted: bool = False,
) -> str:
    ts = now() - timedelta(minutes=minutes_ago)
    run_id = new_id("run")
    async with ctx.db.session() as s:
        s.add(
            m.Run(
                id=run_id, tenant_id=tenant, project_id="main", kind=kind, state=state,
                attempt=1, resource_class="S", timeout_s=600, labels={}, payload={},
                inputs={}, checkpointable=False, infra_retries=0, finalize_attempts=0,
                finalize={}, last_event_id=0, created_at=ts, updated_at=ts, queued_at=ts,
                priority="interactive" if kind == "score" else "batch",
                task_count=1, untrusted_code=untrusted,
            )
        )  # fmt: skip
    return run_id


async def test_a_finalizing_run_is_not_counted_twice(ctx: ServerContext, tenants: None) -> None:
    """Entering FINALIZING meters the attempt into the ledger: from then on the run's
    estimate no longer counts, so it is not counted as estimate + actual."""
    from mapi_server.services.quotas import quota_usage
    from mapi_spec.quotas import QuotaScope

    run_id = await _queue(ctx, "big", state="finalizing")
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        run.estimate = {"vcpu_seconds": 100.0}
        s.add(
            m.CostLedgerEntry(
                id=new_id("cst"), tenant_id="big", project_id="main", run_id=run_id,
                attempt=1, kind="actual", resource_class="S", task_count=1,
                wall_seconds=50.0, vcpu_seconds=100.0, gib_seconds=400.0,
                price_version="test", amount_micros=1, currency="USD", recorded_at=now(),
            )
        )  # fmt: skip
        await _queue(ctx, "big", state="running")  # an unmetered run still counts its estimate
    async with ctx.db.session() as s:
        for other in (await s.execute(select(m.Run).where(m.Run.id != run_id))).scalars():
            other.estimate = {"vcpu_seconds": 30.0}
    async with ctx.db.tenant_session("big") as s:
        usage = await quota_usage(s, QuotaScope.TENANT, "big", now=now())
    assert usage.vcpu_seconds_today == 130.0 and usage.concurrent_runs == 2
    # a retried run (attempt 2 queued after a metered attempt 1) counts its new estimate
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        run.state, run.attempt = "queued", 2
    async with ctx.db.tenant_session("big") as s:
        usage = await quota_usage(s, QuotaScope.TENANT, "big", now=now())
    assert usage.vcpu_seconds_today == 230.0


async def test_concurrent_submissions_respect_the_global_concurrency_cap(
    ctx: ServerContext, tenants: None
) -> None:
    """Admission against the global level is serialized across tenants (each tenant's row
    lock only serializes that tenant), so two tenants cannot both take the last slot."""
    from mapi_server.services.quotas import admit_run
    from mapi_spec.quotas import QuotaDemand

    await _policy(ctx, "global", "*", concurrent_runs=1)

    async def submit(tenant: str) -> bool:
        async with ctx.db.tenant_session(tenant) as sess:
            async with ctx.db.session() as system:
                decision = await admit_run(
                    sess, system, tenant_id=tenant, token=None,
                    demand=QuotaDemand(new_runs=1), untrusted_code=False, now=now(),
                )  # fmt: skip
            if decision.allowed:
                ts = now()
                sess.add(
                    m.Run(
                        id=new_id("run"), tenant_id=tenant, project_id="main", kind="fit",
                        state="queued", attempt=1, resource_class="S", timeout_s=600,
                        labels={}, payload={}, inputs={}, checkpointable=False,
                        infra_retries=0, finalize_attempts=0, finalize={}, last_event_id=0,
                        created_at=ts, updated_at=ts, queued_at=ts, priority="batch",
                        task_count=1, untrusted_code=False,
                    )
                )  # fmt: skip
                await asyncio.sleep(0.2)  # the rest of a real submission
            return decision.allowed

    allowed = await asyncio.gather(submit("big"), submit("small"))
    assert sorted(allowed) == [False, True]
    assert len(await _runs(ctx)) == 1


async def _leased(ctx: ServerContext, dispatcher: Dispatcher) -> list[str]:
    return [run_id for run_id, _ in await dispatcher.lease()]


@pytest.fixture
async def tenants(ctx: ServerContext) -> None:
    for t in ("big", "small"):
        await make_tenant(ctx, t, tier="internal", projects=("main",))


async def test_interactive_work_and_idle_tenants_go_first(
    ctx: ServerContext, tenants: None
) -> None:
    ctx.settings.dispatch_batch = 2
    dispatcher = Dispatcher(ctx, FakeExecutor())
    for i in range(3):
        await _queue(ctx, "big", state="running", minutes_ago=30 + i)
    big = [await _queue(ctx, "big", minutes_ago=20 - i) for i in range(4)]
    score = await _queue(ctx, "small", kind="score", minutes_ago=1)
    fit = await _queue(ctx, "small", minutes_ago=0.5)
    # small has nothing running: its score (weight 4) then its fit beat big's older fits
    assert await _leased(ctx, dispatcher) == [score, fit]
    # now small runs 2 tasks and big 3: big's oldest fit is next, then small vs big again
    nxt = await _leased(ctx, dispatcher)
    assert nxt[0] == big[0] and len(nxt) == 2


async def test_tenant_and_global_concurrency_caps(ctx: ServerContext, tenants: None) -> None:
    ctx.settings.dispatch_batch = 10
    dispatcher = Dispatcher(ctx, FakeExecutor())
    await _queue(ctx, "big", state="running", minutes_ago=5)
    queued_big = [await _queue(ctx, "big", minutes_ago=3 - i) for i in range(3)]
    queued_small = [await _queue(ctx, "small", minutes_ago=2 - i) for i in range(2)]
    await _policy(ctx, "tenant", "big", concurrent_runs=2)
    await _policy(ctx, "global", "*", concurrent_runs=3)
    leased = await _leased(ctx, dispatcher)
    # global: 3 - 1 running = 2 slots; big may add only 1 more; fair share alternates
    assert len(leased) == 2 and set(leased) <= set(queued_big + queued_small)
    assert sum(r in queued_big for r in leased) <= 1
    assert await _leased(ctx, dispatcher) == []  # global capacity exhausted


async def test_dispatcher_holds_runs_the_kill_switches_or_tenant_status_stop(
    ctx: ServerContext, client: httpx.AsyncClient, tenants: None
) -> None:
    dispatcher = Dispatcher(ctx, FakeExecutor())
    untrusted = await _queue(ctx, "big", untrusted=True, minutes_ago=2)
    trusted = await _queue(ctx, "small", minutes_ago=1)
    await client.put(
        "/v1/admin/kill-switches", json={"untrusted_execution_enabled": False, "reason": "drill"}
    )
    assert await _leased(ctx, dispatcher) == [trusted]
    async with ctx.db.session() as s:
        assert (await load_run(s, None, untrusted)).state == RunState.QUEUED.value
        await s.execute(update(m.Tenant).where(m.Tenant.id == "big").values(status="suspended"))
    await client.put(
        "/v1/admin/kill-switches", json={"untrusted_execution_enabled": True, "reason": "drill"}
    )
    assert await _leased(ctx, dispatcher) == []  # suspended tenants keep their queue
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).where(m.Tenant.id == "big").values(status="active"))
    assert await _leased(ctx, dispatcher) == [untrusted]


async def test_the_spend_cap_counts_work_in_flight(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """The ledger only grows when runs finish: the spend cap also counts the estimates of
    work already admitted (at admission) or started (at dispatch, run by run)."""
    from mapi_server.services.quotas import kill_switch_check

    await make_tenant(ctx, "pay", tier="self-hosted-trusted", projects=("main",))
    r = await client.put(
        "/v1/admin/kill-switches", json={"global_spend_cap_micros": 1000, "reason": "beta cap"}
    )
    assert r.status_code == 200
    runs = [await _queue(ctx, "pay", minutes_ago=3 - i) for i in range(3)]
    async with ctx.db.session() as s:
        for row in (await s.execute(select(m.Run))).scalars():
            row.estimate = {"vcpu_seconds": 1.0, "amount_micros": 600, "currency": "USD"}
    # nothing is metered yet, but 1800 micros of work is queued: no more admissions
    async with ctx.db.session() as s:
        decision = await kill_switch_check(
            s, untrusted_code=False, tier="self-hosted-trusted", now=now()
        )
    assert not decision.allowed and "spend cap" in (decision.message or "")
    # dispatch starts runs while the committed spend is below the cap: 0, then 600, not 1200
    dispatcher = Dispatcher(ctx, FakeExecutor())
    assert await _leased(ctx, dispatcher) == runs[:2]
    assert await _leased(ctx, dispatcher) == []  # 1200 in flight
    async with ctx.db.session() as s:
        assert (await load_run(s, None, runs[2])).state == RunState.QUEUED.value


async def test_held_runs_cannot_fill_the_lease_window(
    ctx: ServerContext, client: httpx.AsyncClient, tenants: None
) -> None:
    """Runs that would be held anyway (suspended tenant, paused untrusted code, a tenant
    over budget) never fill the window of queued runs the planner looks at, so other
    tenants' runs behind them still start."""
    ctx.settings = ctx.settings.model_copy(
        update={"lease_candidates": 5, "lease_candidates_per_tenant": 2}
    )
    dispatcher = Dispatcher(ctx, FakeExecutor())
    old = [await _queue(ctx, "big", minutes_ago=60 - i) for i in range(6)]
    fresh = await _queue(ctx, "small", minutes_ago=1)
    # 1. a suspended tenant keeps its queue, out of the window
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).where(m.Tenant.id == "big").values(status="suspended"))
    assert await _leased(ctx, dispatcher) == [fresh]
    # 2. untrusted code paused platform-wide
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).where(m.Tenant.id == "big").values(status="active"))
        await s.execute(update(m.Run).where(m.Run.id.in_(old)).values(untrusted_code=True))
    await client.put(
        "/v1/admin/kill-switches", json={"untrusted_execution_enabled": False, "reason": "drill"}
    )
    fresh = await _queue(ctx, "small", minutes_ago=1)
    assert await _leased(ctx, dispatcher) == [fresh]
    # 3. a tenant over its budget holds at most lease_candidates_per_tenant slots
    await client.put(
        "/v1/admin/kill-switches", json={"untrusted_execution_enabled": True, "reason": "drill"}
    )
    await _policy(ctx, "tenant", "big", vcpu_seconds_per_day=0)
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).where(m.Run.id.in_(old)).values(untrusted_code=False))
        for row in (await s.execute(select(m.Run).where(m.Run.id.in_(old)))).scalars():
            row.estimate = {"vcpu_seconds": 10.0}
    fresh = await _queue(ctx, "small", minutes_ago=1)
    assert await _leased(ctx, dispatcher) == [fresh]
    async with ctx.db.session() as s:
        states = {(await load_run(s, None, r)).state for r in old}
    assert states == {RunState.QUEUED.value}


async def test_shared_tiers_need_a_sandboxing_executor(ctx: ServerContext) -> None:
    await make_tenant(ctx, "pub", tier="public", projects=("main",))
    run_id = await _queue(ctx, "pub")
    dispatcher = Dispatcher(ctx, FakeExecutor())  # PROCESS isolation
    assert await _leased(ctx, dispatcher) == []
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
    assert run.state == RunState.FAILED.value and "gvisor" in (run.problem or {})["detail"]


async def test_executor_health_is_reported(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    dispatcher = Dispatcher(ctx, FakeExecutor())
    await dispatcher.report()
    page = (await client.get("/v1/admin/executors")).json()
    [health] = page["items"]
    assert health["name"] == "fake" and health["status"] == "ok"
    assert health["allows_public_tenants"] is False
    assert (await client.get("/v1/admin/executors/fake")).status_code == 200
    assert (await client.get("/v1/admin/executors/nope")).status_code == 404


async def test_cloud_run_throttling_reaches_the_admin_console(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """The cloudrun-jobs executor's own 429 alarm and submit rate feed the dispatcher's
    report, so operators see jobs.run throttling in /v1/admin/executors."""
    from mapi_server.executors.cloudrun import CloudRunJobsExecutor
    from mapi_server.executors.fake_cloudrun import FakeCloudRun
    from mapi_server_testkit.conformance import INVENTORY

    executor = CloudRunJobsExecutor(
        FakeCloudRun.for_inventory(INVENTORY), INVENTORY, submits_per_minute=120, burst=5
    )
    for _ in range(2):
        executor.throttled.record()
    await Dispatcher(ctx, executor).report()
    health = (await client.get("/v1/admin/executors/cloudrun-jobs")).json()
    assert health["status"] == "degraded" and health["throttled_last_hour"] == 2
    assert health["submit_rate_limit_per_minute"] == 120
    assert "429" in health["message"]


def test_dispatcher_backs_off_on_a_cloud_run_429(ctx: ServerContext) -> None:
    """A jobs.run 429 the executor could not absorb (``CloudRunRateLimited``) puts the
    dispatcher into back-off, honouring Retry-After, and counts toward its health."""
    from mapi_server.executors.cloudrun import CloudRunError, CloudRunRateLimited

    dispatcher = Dispatcher(ctx, FakeExecutor())
    assert dispatcher._throttled(CloudRunError("boom", status=500)) is None
    assert dispatcher._throttled(CloudRunRateLimited("429", retry_after=7.0)) == 7.0
    first = dispatcher._throttled(CloudRunRateLimited("429"))
    second = dispatcher._throttled(CloudRunRateLimited("429"))
    assert first is not None and second is not None and second >= first


async def test_runs_admitted_under_looser_limits_wait_at_dispatch(
    ctx: ServerContext, tenants: None
) -> None:
    """The dispatcher's own quota decision: limits tightened after admission hold the queue."""
    dispatcher = Dispatcher(ctx, FakeExecutor())
    run_id = await _queue(ctx, "big")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run).where(m.Run.id == run_id).values(estimate={"vcpu_seconds": 600})
        )
    await _policy(ctx, "tenant", "big", vcpu_seconds_per_day=100)
    assert await _leased(ctx, dispatcher) == []
    await _policy(ctx, "tenant", "big", vcpu_seconds_per_day=600, max_resource_class="S")
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).where(m.Run.id == run_id).values(resource_class="M"))
    assert await _leased(ctx, dispatcher) == []
    # exactly at the vCPU limit (its own estimate) and within the class: it starts
    await _policy(ctx, "tenant", "big", vcpu_seconds_per_day=600, max_resource_class="M")
    assert await _leased(ctx, dispatcher) == [run_id]


# =============================================================================== cost


async def test_finished_attempts_are_metered_into_the_ledger(
    ctx: ServerContext, client: httpx.AsyncClient, metrics: Metrics
) -> None:
    executor = FakeExecutor()
    ctx.queue = InlineFinalizeQueue(Finalizer(ctx))
    dispatcher, reconciler = Dispatcher(ctx, executor), Reconciler(ctx, executor)
    await ready_dataset(ctx)
    spec_id = await _spec(client)
    run_id = (
        await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    ).json()["id"]
    await dispatcher.dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        # pretend the execution started 5 minutes ago
        att.submitted_at = now() - timedelta(minutes=5)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
    executor.finish(att.nonce, 0)
    await simulate_outputs(ctx, run, att)
    await reconciler.reconcile_run(run_id)
    assert isinstance(ctx.queue, InlineFinalizeQueue)
    await ctx.queue.drain()
    view = Run.model_validate((await client.get(f"/v1/runs/{run_id}")).json())
    assert view.state == RunState.SUCCEEDED
    cost = view.cost
    assert not cost.estimated and cost.attempts_billed == 1
    assert cost.currency == "USD" and cost.price_version == DEFAULT_PRICE_TABLE.version
    assert cost.vcpu_seconds is not None and 590 <= cost.vcpu_seconds <= 640  # 2 vCPU × ~300 s
    assert cost.amount_micros is not None and cost.amount_micros > 0
    assert cost.usd == pytest.approx(cost.amount_micros / 1e6)
    async with ctx.db.session() as s:
        [entry] = list((await s.execute(select(m.CostLedgerEntry))).scalars())
        # billing is idempotent: another terminal pass writes nothing new
        from mapi_server.services.cost import bill_attempts

        run = await load_run(s, None, run_id)
        assert await bill_attempts(s, run) == []
    assert entry.executor == "fake" and entry.token_id is None and entry.kind == "actual"
    assert _sample("mapi_cost_micros_total", tenant_id="default") == entry.amount_micros
    usage = UsageReport.model_validate(
        (await client.get("/v1/usage", params={"group_by": "resource_class"})).json()
    )
    assert usage.totals.runs == 1 and usage.totals.amount_micros == cost.amount_micros
    assert set(usage.groups) == {"S"} and usage.price_versions == [DEFAULT_PRICE_TABLE.version]
    assert any(q.limit_name == "concurrent_runs" for q in usage.quotas)
    day = UsageReport.model_validate(
        (await client.get("/v1/usage", params={"period": "day"})).json()
    )
    assert day.totals.runs == 1 and (day.end - day.start) == timedelta(days=1)
    tenant = UsageReport.model_validate(
        (await client.get("/v1/tenants/default/usage", params={"group_by": "run"})).json()
    )
    assert set(tenant.groups) == {run_id}
