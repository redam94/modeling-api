"""Tenant deletion (M3): request and cancel, the start (status, credentials, runs, hooks),
the purge of every store with another tenant left untouched, retention locks, waiting for
runs, failures with retry, and the procrastinate job."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.deletion import TenantPurger
from mapi_server.services.lifecycle import (
    InlineLifecycleJobs,
    LifecycleScheduler,
    ProcrastinateLifecycleJobs,
)
from mapi_server.services.queue import ProcrastinateQueue
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server.storage import MemoryObjectStore
from mapi_server_testkit.identity import (
    grant_role,
    make_user,
    session_caller,
    token_caller,
)
from mapi_server_testkit.trust import Seeded, seed_tenant
from mapi_spec.storage import NotFound
from mapi_spec.trust import TenantDeletion
from pydantic import JsonValue
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

OPS = "ops@platform.test"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "profile": Profile.MULTI_TENANT,
            "operator_emails": [OPS],
            "tenant_deletion_grace_s": 0.0,
        }
    )


async def _owner(ctx: ServerContext, client: httpx.AsyncClient, seeded: Seeded) -> Any:
    return await session_caller(ctx, client, seeded.user)


async def _counts(ctx: ServerContext, tenant_id: str) -> dict[str, int]:
    out = {}
    async with ctx.db.session() as s:
        for table in (*m.TENANT_TABLES,):
            n = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
                )
            ).scalar_one()
            out[table] = int(n)
    return out


async def _objects(ctx: ServerContext, tenant_id: str) -> list[str]:
    names: list[str] = []
    for bucket in ctx.buckets.all().values():
        page = await ctx.store.list(bucket, prefix=f"t/{tenant_id}/")
        names.extend(f"{bucket}/{o.name}" for o in page.items)
    return names


# ================================================================================ request


async def test_request_and_cancel(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    ctx.settings = ctx.settings.model_copy(update={"tenant_deletion_grace_s": 7 * 86400.0})
    alpha = await seed_tenant(ctx, "alpha")
    owner = await _owner(ctx, client, alpha)
    async with ctx.db.session() as s:  # an unfinished run, cancelled by the request
        await s.execute(update(m.Run).values(state="queued", queued_at=now()))
    r = await owner.post("/v1/tenants/alpha/deletion", json={"confirm": "beta"})
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/confirm"
    viewer = await make_user(ctx, "viewer@alpha.test")
    await grant_role(ctx, viewer, "alpha", None, "viewer")
    v = await session_caller(ctx, client, viewer)
    r = await v.post("/v1/tenants/alpha/deletion", json={"confirm": "alpha"})
    assert r.status_code == 403
    r = await token_caller(client, alpha.token).post(
        "/v1/tenants/alpha/deletion", json={"confirm": "alpha"}
    )
    assert r.status_code == 403  # tokens never administer a tenant
    r = await owner.post("/v1/tenants/alpha/deletion",
                         json={"confirm": "alpha", "reason": "closing"})  # fmt: skip
    assert r.status_code == 202, r.text
    d = TenantDeletion.model_validate(r.json())
    assert d.state == "scheduled" and d.purge_after - d.requested_at == timedelta(days=7)
    assert [p.store for p in d.stores] == ["queue", "database", "object_store", "logs",
                                           "webhooks"]  # fmt: skip
    assert set(d.retained) >= {"audit", "cost_ledger"}
    assert (await owner.post("/v1/tenants/alpha/deletion",
                             json={"confirm": "alpha"})).status_code == 409  # fmt: skip
    async with ctx.db.session() as s:
        tenant = await s.get(m.Tenant, "alpha")
        run = await s.get(m.Run, alpha.run_id)
    assert tenant is not None and tenant.status == "suspended"
    assert run is not None and run.state == "cancelling"
    # suspended: reads work, writes do not, but the owner may still export and cancel
    assert (await owner.get("/v1/tenants/alpha/deletion")).json()["state"] == "scheduled"
    r = await owner.post("/v1/tenants/alpha/projects", json={"id": "new", "name": "New"})
    assert r.status_code == 403
    assert (await owner.post("/v1/tenants/alpha/exports", json={})).status_code == 202
    # the grace period is not over: the orchestrator does nothing yet
    assert await TenantPurger(ctx).due() == []
    r = await owner.delete("/v1/tenants/alpha/deletion")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    async with ctx.db.session() as s:
        tenant = await s.get(m.Tenant, "alpha")
        assert tenant is not None and (tenant.status, tenant.status_reason) == ("active", None)
        actions = set(
            (await s.execute(select(m.AuditEvent.action).where(
                m.AuditEvent.tenant_id == "alpha"))).scalars()
        )  # fmt: skip
    assert {"tenant.deletion_request", "tenant.deletion_cancel", "tenant.export"} <= actions
    assert (await owner.delete("/v1/tenants/alpha/deletion")).status_code == 404


async def test_the_platform_tenant_cannot_be_deleted(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    sc = ctx.platform_scope
    admin = await make_user(ctx, "boss@platform.test")
    await grant_role(ctx, admin, sc.tenant_id, None, "admin")
    caller = await session_caller(ctx, client, admin)
    r = await caller.post(f"/v1/tenants/{sc.tenant_id}/deletion", json={"confirm": sc.tenant_id})
    assert r.status_code == 409


async def test_recent_sign_in_can_be_required(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"deletion_reauth_max_age_s": 60.0})
    alpha = await seed_tenant(ctx, "alpha")
    owner = await _owner(ctx, client, alpha)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.WebSession).values(
                created_at=now() - timedelta(minutes=10), last_seen_at=now()
            )
        )
    r = await owner.post("/v1/tenants/alpha/deletion", json={"confirm": "alpha"})
    assert r.status_code == 401 and "sign in again" in r.json()["detail"]
    fresh = await _owner(ctx, client, alpha)
    assert (await fresh.post("/v1/tenants/alpha/deletion",
                             json={"confirm": "alpha"})).status_code == 202  # fmt: skip


# ================================================================================== purge


class _Revoker:
    name = "probe"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def revoke(self, session: AsyncSession, tenant_id: str, *, now: Any) -> int:
        self.calls.append(tenant_id)
        return 3


class _Notifier:
    name = "webhooks"

    def __init__(self) -> None:
        self.delivered: list[tuple[str, list[JsonValue]]] = []

    async def capture(self, session: AsyncSession, tenant_id: str) -> list[JsonValue]:
        return [{"url": "https://hooks.example/x", "project_id": "main"}]

    async def deliver(self, tenant_id: str, deletion_id: str, captured: Any, *, now: Any) -> int:
        self.delivered.append((tenant_id, list(captured)))
        return len(captured)


async def test_full_purge_leaves_other_tenants_untouched(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    beta = await seed_tenant(ctx, "beta")
    shared = await make_user(ctx, "shared@both.test")
    for t in ("alpha", "beta"):
        await grant_role(ctx, shared, t, "main", "viewer")
    beta_before = await _counts(ctx, "beta")
    beta_objects = await _objects(ctx, "beta")
    assert ctx.trust is not None
    revoker, notifier = _Revoker(), _Notifier()
    ctx.trust.add_revoker(revoker)
    ctx.trust.notifier = notifier
    owner = await _owner(ctx, client, alpha)
    r = await owner.post("/v1/tenants/alpha/deletion", json={"confirm": "alpha"})
    assert r.status_code == 202
    deletion_id = r.json()["id"]
    assert (await token_caller(client, alpha.token).get("/v1/runs")).status_code == 200

    jobs = InlineLifecycleJobs(ctx)
    ticked = await LifecycleScheduler(ctx, jobs).tick()
    assert ticked["purges"] == 1
    await jobs.drain()

    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
        tenant = await s.get(m.Tenant, "alpha")
        assert row is not None and row.state == "purged", row and row.stores
        assert tenant is not None and (tenant.status, tenant.name) == ("deleting", "alpha")
        # people who belonged only to alpha are gone; the shared viewer stays
        assert await s.get(m.User, alpha.user.id) is None
        assert await s.get(m.User, shared.id) is not None
        assert await s.get(m.User, beta.user.id) is not None
        audit = (
            await s.execute(select(m.AuditEvent.action).where(m.AuditEvent.tenant_id == "alpha"))
        ).scalars()
        actions = list(audit)
        ledger = (
            await s.execute(select(func.count()).where(m.CostLedgerEntry.tenant_id == "alpha"))
        ).scalar_one()
    assert {"tenant.delete", "tenant.purged", "run.create"} <= set(actions)  # audit kept
    assert ledger == 1  # cost ledger kept
    counts = await _counts(ctx, "alpha")
    assert {t: n for t, n in counts.items() if n} == {
        "audit_events": len(actions), "cost_ledger": 1, "tenant_deletions": 1,
    }  # fmt: skip
    assert await _objects(ctx, "alpha") == []
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.bootstrap, alpha.nonce)
    # beta: every row and object untouched (cross-tenant isolation of the purge)
    assert await _counts(ctx, "beta") == beta_before
    assert await _objects(ctx, "beta") == beta_objects
    await ctx.store.stat(ctx.buckets.bootstrap, beta.nonce)
    # credentials are dead, the tenant is invisible to its old members
    assert (await token_caller(client, alpha.token).get("/v1/runs")).status_code == 401
    assert (await token_caller(client, beta.token).get("/v1/runs")).status_code == 200
    viewer = await session_caller(ctx, client, shared)
    assert (await viewer.in_project("alpha", "main").get("/v1/runs")).status_code == 404
    assert [t["tenant"]["id"] for t in (await viewer.get("/v1/me")).json()["tenants"]] == ["beta"]
    # operators still see the deletion record and its progress
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    r = await ops.get("/v1/tenants/alpha/deletion")
    assert r.status_code == 200, r.text
    d = TenantDeletion.model_validate(r.json())
    assert d.state == "purged" and d.fraction_done == 1.0 and d.completed_at is not None
    progress = {p.store: p for p in d.stores}
    assert progress["database"].items_done > 10
    assert progress["object_store"].items_done == len(alpha.objects)
    assert progress["logs"].detail and "not purged" in progress["logs"].detail.lower()
    assert progress["webhooks"].items_done == 1
    assert revoker.calls == ["alpha"]
    assert notifier.delivered == [("alpha", [{"url": "https://hooks.example/x",
                                              "project_id": "main"}])]  # fmt: skip
    async with ctx.db.session() as s:
        [start] = (
            await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "tenant.delete"))
        ).scalars()
    assert start.details["revoked"] == {
        "tokens": 1, "sessions": 1, "runs": 0, "embeds": 1, "webhooks": 1, "probe": 3,
    }  # fmt: skip
    # a second pass is a no-op
    assert (await TenantPurger(ctx).run(deletion_id)).value == "purged"
    assert await TenantPurger(ctx).due() == []


async def test_retention_locks_are_recorded_and_retried(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    alpha = await seed_tenant(ctx, "alpha")
    store._locked.add(ctx.buckets.bundles)
    owner = await _owner(ctx, client, alpha)
    deletion_id = (await owner.post("/v1/tenants/alpha/deletion",
                                    json={"confirm": "alpha"})).json()["id"]  # fmt: skip
    purger = TenantPurger(ctx)
    assert (await purger.run(deletion_id)).value == "deleting"
    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
    assert row is not None and row.next_attempt_at is not None
    assert row.next_attempt_at - now() > timedelta(days=29)
    [kept] = row.retained_objects
    assert kept["bucket"] == ctx.buckets.bundles and kept["name"].startswith("t/alpha/models/")
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    d = TenantDeletion.model_validate((await ops.get("/v1/tenants/alpha/deletion")).json())
    obj = next(p for p in d.stores if p.store == "object_store")
    assert obj.state == "running" and "retention lock" in (obj.detail or "")
    assert [p.state for p in d.stores if p.store in ("queue", "database")] == ["done", "done"]
    assert await purger.due() == []  # nothing to do before the lock expires
    store._locked.clear()  # the retention period passes
    later = now() + timedelta(days=31)
    assert await purger.due(at=later) == [deletion_id]
    assert (await purger.run(deletion_id, at=later)).value == "purged"
    assert await _objects(ctx, "alpha") == []


async def test_the_purge_waits_for_runs_to_stop(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).values(state="running", started_at=now()))
    owner = await _owner(ctx, client, alpha)
    deletion_id = (await owner.post("/v1/tenants/alpha/deletion",
                                    json={"confirm": "alpha"})).json()["id"]  # fmt: skip
    purger = TenantPurger(ctx)
    assert (await purger.run(deletion_id)).value == "deleting"
    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
        run = await s.get(m.Run, alpha.run_id)
    assert run is not None and run.state == "cancelling"
    assert row is not None and "waiting for 1" in str(row.stores[0]["detail"])
    assert (await _counts(ctx, "alpha"))["runs"] == 1  # nothing purged yet
    # the reconciler confirms the cancel: the next pass purges
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).values(state="cancelled", finished_at=now()))
    assert (await purger.run(deletion_id, at=now() + timedelta(minutes=1))).value == "purged"


async def test_runs_that_never_stop_do_not_block_the_purge(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"deletion_run_stop_timeout_s": 60.0})
    alpha = await seed_tenant(ctx, "alpha")
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).values(state="finalizing"))
    owner = await _owner(ctx, client, alpha)
    deletion_id = (await owner.post("/v1/tenants/alpha/deletion",
                                    json={"confirm": "alpha"})).json()["id"]  # fmt: skip
    purger = TenantPurger(ctx)
    assert (await purger.run(deletion_id)).value == "deleting"
    assert (await purger.run(deletion_id, at=now() + timedelta(minutes=5))).value == "purged"
    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
    assert row is not None and "had not stopped" in str(row.stores[0]["detail"])


async def test_a_failing_store_is_retried_then_operators_retry(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"deletion_max_attempts": 1})
    alpha = await seed_tenant(ctx, "alpha")
    owner = await _owner(ctx, client, alpha)
    deletion_id = (await owner.post("/v1/tenants/alpha/deletion",
                                    json={"confirm": "alpha"})).json()["id"]  # fmt: skip
    store = ctx.store

    async def broken_list(*args: Any, **kw: Any) -> Any:
        raise ConnectionError("storage is down")

    original = store.list
    monkeypatch.setattr(store, "list", broken_list)
    purger = TenantPurger(ctx)
    assert (await purger.run(deletion_id)).value == "failed"
    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
    assert (
        row is not None and row.attempts == 1 and row.last_error == "object_store: ConnectionError"
    )
    assert await purger.due(at=now() + timedelta(days=1)) == []  # out of automatic retries
    monkeypatch.setattr(store, "list", original)
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    # the owner belonged only to alpha: the database purge erased the account
    assert (await owner.post("/v1/admin/tenants/alpha/deletion/retry")).status_code == 401
    r = await ops.post("/v1/admin/tenants/alpha/deletion/retry")
    assert r.status_code == 202 and r.json()["state"] == "deleting"
    assert (await ops.post("/v1/admin/tenants/beta/deletion/retry")).status_code == 404
    assert await purger.due() == [deletion_id]
    assert (await purger.run(deletion_id)).value == "purged"
    r = await ops.post("/v1/admin/tenants/alpha/deletion/retry")
    assert r.status_code == 404  # nothing open any more
    async with ctx.db.session() as s:
        retried = (
            await s.execute(
                select(func.count()).where(m.AuditEvent.action == "tenant.deletion_retry")
            )
        ).scalar_one()
    assert retried == 1


async def test_cancelling_after_the_start_is_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    store._locked.add(ctx.buckets.bundles)  # keeps the deletion in "deleting"
    owner = await _owner(ctx, client, alpha)
    deletion_id = (await owner.post("/v1/tenants/alpha/deletion",
                                    json={"confirm": "alpha"})).json()["id"]  # fmt: skip
    await TenantPurger(ctx).run(deletion_id)
    # the owner's account went with the tenant; operators may not cancel a started purge
    assert (await owner.delete("/v1/tenants/alpha/deletion")).status_code == 401
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    assert (await ops.delete("/v1/tenants/alpha/deletion")).status_code == 403


async def test_purge_runs_as_a_procrastinate_job(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    owner = await _owner(ctx, client, alpha)
    deletion_id = (await owner.post("/v1/tenants/alpha/deletion",
                                    json={"confirm": "alpha"})).json()["id"]  # fmt: skip
    from mapi_server.services.finalizer import Finalizer

    queue = ProcrastinateQueue(ctx.db.url, Finalizer(ctx))
    jobs = ProcrastinateLifecycleJobs(queue, ctx)
    assert "lifecycle" in queue.queues
    try:
        scheduler = LifecycleScheduler(ctx, jobs)
        await scheduler.tick()
        await scheduler.tick()  # deduplicated while queued
        async with ctx.db.session() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM procrastinate_jobs WHERE queue_name = 'lifecycle'")
                )
            ).scalar_one()
        assert n == 1
        await queue.run_worker(wait=False)
    finally:
        await queue.close()
    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
    assert row is not None and row.state == "purged"
