"""Tenants, projects, members and roles (multi-tenant profile), operator endpoints, tenant
status, invitations, and the audit log."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from mapi_server.auth.identity import VerifiedIdentity, claim_or_create_user
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    Caller,
    grant_role,
    make_tenant,
    make_user,
    session_caller,
)
from mapi_spec.api import AdminUsageReport, Me, Page, new_id
from mapi_spec.audit import AuditEvent
from mapi_spec.tenancy import Membership, Project, Tenant
from sqlalchemy import select

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # operators are whoever the configuration names (decided on every request)
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "operator_emails": ["ops@platform.test"]}
    )


async def _operator(ctx: ServerContext, client: httpx.AsyncClient) -> Caller:
    op = await make_user(ctx, "ops@platform.test", operator=True)
    return await session_caller(ctx, client, op)


async def test_credentials_are_required_in_the_multi_tenant_profile(
    client: httpx.AsyncClient,
) -> None:
    for path in ("/v1/me", "/v1/projects", "/v1/catalog", "/v1/runs", "/v1/admin/tenants"):
        r = await client.get(path)
        assert r.status_code == 401, path
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/v1/schemas/modelspec.json")).status_code == 200


async def test_operator_creates_a_tenant_and_the_invited_admin_claims_it(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ops = await _operator(ctx, client)
    r = await ops.post(
        "/v1/admin/tenants",
        json={"id": "acme", "name": "Acme", "trust_tier": "verified",
              "initial_admin_email": "IT@Acme.test"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    assert Tenant.model_validate(r.json()).trust_tier == "verified"
    assert (
        await ops.post("/v1/admin/tenants", json={"id": "acme", "name": "x"})
    ).status_code == 409
    # the invitation is claimed at first sign-in with that *verified* e-mail only
    async with ctx.db.session() as s:
        unverified = await claim_or_create_user(
            s, VerifiedIdentity("https://idp.test", "sub-1", "it@acme.test", False, "Mallory")
        )
        it = await claim_or_create_user(
            s, VerifiedIdentity("https://idp.test", "sub-2", "it@acme.test", True, "Ida")
        )
        assert unverified.id != it.id
        grants = (
            await s.execute(select(m.Membership).where(m.Membership.principal_id == it.id))
        ).scalars()
        assert [(g.tenant_id, g.project_id, g.role) for g in grants] == [("acme", None, "admin")]
        none = (
            await s.execute(select(m.Membership).where(m.Membership.principal_id == unverified.id))
        ).first()
        assert none is None
    admin = await session_caller(ctx, client, it)
    me = Me.model_validate((await admin.get("/v1/me")).json())
    assert [(t.tenant.id, t.role) for t in me.tenants] == [("acme", "admin")]
    # tenant admin: projects and members
    r = await admin.post("/v1/tenants/acme/projects", json={"id": "growth", "name": "Growth"})
    assert r.status_code == 201
    assert Project.model_validate(r.json()).tenant_id == "acme"
    r = await admin.post("/v1/tenants/acme/projects", json={"id": "growth", "name": "Again"})
    assert r.status_code == 409
    r = await admin.post(
        "/v1/tenants/acme/projects/growth/members",
        json={"email": "ana@acme.test", "role": "runner"},
    )
    assert r.status_code == 201, r.text
    ana_id = Membership.model_validate(r.json()).principal_id
    page = Page[Membership].model_validate(
        (await admin.get("/v1/tenants/acme/projects/growth/members")).json()
    )
    assert {(x.principal_id, x.role, x.project_id) for x in page.items} == {
        (it.id, "admin", None),
        (ana_id, "runner", "growth"),
    }
    r = await admin.patch(
        f"/v1/tenants/acme/projects/growth/members/{ana_id}", json={"role": "viewer"}
    )
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    # the last tenant admin can be neither demoted nor removed
    r = await admin.patch(f"/v1/tenants/acme/members/{it.id}", json={"role": "editor"})
    assert r.status_code == 409
    assert (await admin.delete(f"/v1/tenants/acme/members/{it.id}")).status_code == 409
    # projects holding data cannot be deleted; empty ones can
    r = await admin.post(
        "/v1/datasets", json={"name": "d"}, headers={"X-Mapi-Project": "acme/growth"}
    )
    assert r.status_code == 201
    assert (await admin.delete("/v1/tenants/acme/projects/growth")).status_code == 409
    await admin.post("/v1/tenants/acme/projects", json={"id": "tmp", "name": "Tmp"})
    assert (await admin.delete("/v1/tenants/acme/projects/tmp")).status_code == 204
    # the tenant audit log shows the history (tenant admins only)
    audit = Page[AuditEvent].model_validate((await admin.get("/v1/tenants/acme/audit")).json())
    actions = [e.action for e in audit.items]
    assert {"project.create", "project.delete", "member.add", "member.update"} <= set(actions)
    assert all(e.tenant_id == "acme" for e in audit.items)


async def test_roles_gate_writes_and_compute(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    await make_tenant(ctx, "acme", projects=("growth",))
    users = {}
    for role in ("viewer", "editor", "runner"):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", "growth", role)
        users[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id="growth")
    assert (await users["viewer"].get("/v1/datasets")).status_code == 200
    assert (await users["viewer"].post("/v1/datasets", json={"name": "v"})).status_code == 403
    assert (await users["editor"].post("/v1/datasets", json={"name": "e"})).status_code == 201
    run = {"kind": "fit", "spec_id": "spc_x", "revision": 1}
    assert (await users["editor"].post("/v1/runs", json=run)).status_code == 403
    assert (await users["runner"].post("/v1/runs", json=run)).status_code == 404  # allowed
    assert (await users["runner"].get("/v1/tenants/acme/audit")).status_code == 403
    assert (await users["runner"].get("/v1/tenants/acme/members")).status_code == 403
    # a bare project id is enough when the caller belongs to one tenant
    bare = Caller(client, {**users["viewer"].headers, "X-Mapi-Project": "growth"},
                  users["viewer"].cookies)  # fmt: skip
    assert (await bare.get("/v1/datasets")).status_code == 200
    # unknown or invisible projects are 404, never 403
    other = users["viewer"].in_project("acme", "nope")
    assert (await other.get("/v1/datasets")).status_code == 404
    assert (await users["viewer"].get("/v1/tenants/zzz")).status_code == 404
    # no project selected at all: a problem pointing at the header
    plain = Caller(client, {"X-CSRF-Token": users["viewer"].headers["X-CSRF-Token"]},
                   users["viewer"].cookies)  # fmt: skip
    r = await plain.get("/v1/datasets")
    assert r.status_code == 422 and "X-Mapi-Project" in r.json()["detail"]


async def test_suspended_tenants_are_read_only_and_deleting_ones_closed(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ops = await _operator(ctx, client)
    await make_tenant(ctx, "acme", projects=("growth",))
    u = await make_user(ctx, "ana@acme.test")
    await grant_role(ctx, u, "acme", None, "admin")
    ana = await session_caller(ctx, client, u, tenant_id="acme", project_id="growth")
    r = await ops.patch("/v1/admin/tenants/acme", json={"status": "suspended"})
    assert r.status_code == 422  # a reason is required
    r = await ops.patch(
        "/v1/admin/tenants/acme", json={"status": "suspended", "status_reason": "overdue"}
    )
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert (await ana.get("/v1/datasets")).status_code == 200
    assert (await ana.post("/v1/datasets", json={"name": "x"})).status_code == 403
    await ops.patch("/v1/admin/tenants/acme", json={"status": "deleting", "status_reason": "bye"})
    assert (await ana.get("/v1/datasets")).status_code == 403
    me = Me.model_validate((await ana.get("/v1/me")).json())
    assert me.tenants == []
    await ops.patch("/v1/admin/tenants/acme", json={"status": "active"})
    assert (await ana.post("/v1/datasets", json={"name": "x"})).status_code == 201
    audit = Page[AuditEvent].model_validate(
        (await ops.get("/v1/admin/audit", params={"tenant_id": "acme"})).json()
    )
    assert [e.action for e in audit.items][:3] == [
        "tenant.reactivate", "tenant.delete", "tenant.suspend",
    ]  # fmt: skip


async def test_operator_endpoints_refuse_everyone_else(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await make_tenant(ctx, "acme", projects=("growth",))
    u = await make_user(ctx, "ana@acme.test")
    await grant_role(ctx, u, "acme", None, "admin")
    ana = await session_caller(ctx, client, u)
    for method, path, body in (
        ("GET", "/v1/admin/tenants", None),
        ("POST", "/v1/admin/tenants", {"id": "evil", "name": "Evil"}),
        ("PUT", "/v1/admin/kill-switches", {"untrusted_execution_enabled": False,
                                            "reason": "nope"}),
        ("PUT", "/v1/admin/quotas", {"scope": "tenant", "subject_id": "acme",
                                     "limits": {"concurrent_runs": 1000}}),
        ("GET", "/v1/admin/audit", None),
        ("PUT", "/v1/admin/executors/fake", {"enabled": False, "reason": "nope"}),
    ):  # fmt: skip
        r = await ana.request(method, path, json=body)
        assert r.status_code == 403, (path, r.status_code)
    async with ctx.db.session() as s:
        denied = [
            e.action
            for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.outcome == "denied"
        ]
    assert {"tenant.create", "kill_switch.update", "quota.update", "executor.update"} <= set(denied)


async def test_operators_read_metadata_but_never_tenant_content(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ops = await _operator(ctx, client)
    await make_tenant(ctx, "acme", projects=("growth",))
    acme = ops.in_project("acme", "growth")
    assert (await ops.get("/v1/admin/tenants/acme")).status_code == 200
    assert (await acme.get("/v1/runs")).status_code == 200  # run metadata
    assert (await acme.get("/v1/usage")).status_code == 200
    assert (await acme.get("/v1/datasets")).status_code == 404  # content: invisible
    assert (await acme.get("/v1/specs")).status_code == 404
    assert (await acme.get("/v1/models")).status_code == 404
    assert (await acme.post("/v1/datasets", json={"name": "x"})).status_code == 404
    assert (await ops.get("/v1/tenants/acme/members")).status_code == 200
    assert (await ops.patch("/v1/tenants/acme", json={"name": "x"})).status_code == 403


async def test_expired_sessions_are_deleted(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    """An expired session is deleted when its cookie comes back (in its own transaction, so
    the 401 does not roll the delete back), and the housekeeper sweeps the ones that never
    come back, with abandoned sign-ins."""
    from datetime import timedelta

    from mapi_server.services.housekeeping import Housekeeper
    from mapi_server.services.runs import now
    from mapi_spec.auth import SESSION_IDLE_TTL

    ana = await make_user(ctx, "ana@acme.test")
    back = await session_caller(ctx, client, ana)
    await session_caller(ctx, client, ana)  # never comes back, goes idle
    live = await session_caller(ctx, client, ana)
    async with ctx.db.session() as s:
        rows = list(await s.execute(select(m.WebSession).order_by(m.WebSession.created_at)))
        ids = [r[0].id for r in rows]
        first, idle = (await s.get(m.WebSession, ids[0])), (await s.get(m.WebSession, ids[1]))
        assert first is not None and idle is not None
        first.expires_at = now() - timedelta(seconds=1)
        idle.last_seen_at = now() - SESSION_IDLE_TTL - timedelta(minutes=1)
        s.add(
            m.LoginTransactionRow(
                id="sha256:" + "0" * 64, state="s", nonce="n", code_verifier="v" * 43,
                return_to="/", created_at=now() - timedelta(hours=2),
            )
        )  # fmt: skip
    r = await back.get("/v1/me")
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    async with ctx.db.session() as s:
        assert await s.get(m.WebSession, ids[0]) is None  # deleted despite the 401
        assert await s.get(m.WebSession, ids[1]) is not None
    assert await Housekeeper(ctx).sweep_once() >= 2
    async with ctx.db.session() as s:
        left = [r.id for r in (await s.execute(select(m.WebSession))).scalars()]
        logins = list((await s.execute(select(m.LoginTransactionRow))).scalars())
    assert left == [ids[2]] and logins == []
    assert (await live.get("/v1/me")).status_code == 200


async def test_default_residency_comes_from_the_deployment(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """On an EU deployment the platform tenant and tenants created without a residency are
    EU tenants (a 'us' default could never be placed on an EU executor)."""
    from mapi_server.app import ensure_scope

    ctx.settings = ctx.settings.model_copy(
        update={"default_residency": "eu", "tenant_id": "platform-eu"}
    )
    await ensure_scope(ctx)
    async with ctx.db.session() as s:
        platform = await s.get(m.Tenant, "platform-eu")
        assert platform is not None and platform.data_residency == "eu"
    op = await _operator(ctx, client)
    r = await op.post("/v1/admin/tenants", json={"id": "acme", "name": "Acme"})
    assert r.status_code == 201, r.text
    assert r.json()["data_residency"] == "eu"
    r = await op.post(
        "/v1/admin/tenants", json={"id": "usco", "name": "US Co", "data_residency": "us"}
    )
    assert r.status_code == 201 and r.json()["data_residency"] == "us"


async def _bill(
    ctx: ServerContext, tenant_id: str, amount: int, *, run_id: str, at: datetime | None = None
) -> None:
    async with ctx.db.session() as s:
        s.add(
            m.CostLedgerEntry(
                id=new_id("cle"), tenant_id=tenant_id, project_id="main", run_id=run_id,
                attempt=1, kind="actual", resource_class="S", task_count=1, wall_seconds=60,
                vcpu_seconds=120, gib_seconds=480, price_version="v1", amount_micros=amount,
                currency="USD", recorded_at=at or now(),
            )
        )  # fmt: skip


async def test_operators_see_cost_by_tenant_and_spend_against_the_global_cap(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ops = await _operator(ctx, client)
    await make_tenant(ctx, "acme")
    await make_tenant(ctx, "beta", tier="public")
    await _bill(ctx, "acme", 3_000, run_id="run_a1")
    await _bill(ctx, "acme", 2_000, run_id="run_a2")
    await _bill(ctx, "beta", 9_000, run_id="run_b1")
    await _bill(ctx, "beta", 1_000_000, run_id="run_old", at=now() - timedelta(days=400))
    r = await ops.put(
        "/v1/admin/kill-switches",
        json={"global_spend_cap_micros": 20_000, "spend_cap_period": "month", "reason": "cap"},
    )
    assert r.status_code == 200, r.text

    r = await ops.get("/v1/admin/usage", params={"period": "month"})
    assert r.status_code == 200, r.text
    report = AdminUsageReport.model_validate(r.json())
    # the biggest spender first; the 400-day-old entry is outside the period
    assert [(t.tenant_id, t.totals.amount_micros, t.totals.runs) for t in report.tenants] == [
        ("beta", 9_000, 1),
        ("acme", 5_000, 2),
    ]
    beta = report.tenants[0]
    assert beta.name and beta.trust_tier == "public"
    [total] = report.totals
    assert total.amount_micros == 14_000 and total.runs == 3 and total.currency == "USD"
    assert report.price_versions == ["v1"]
    cap = report.spend_cap
    assert cap.cap_micros == 20_000 and cap.spent_micros == 14_000 and cap.period == "month"
    assert cap.fraction == pytest.approx(0.7) and cap.currency == "USD"

    then = (now() - timedelta(days=400)).isoformat()
    r = await ops.get("/v1/admin/usage", params={"period": "day", "start": then})
    old = AdminUsageReport.model_validate(r.json())
    assert [t.tenant_id for t in old.tenants] == ["beta"]
    assert old.spend_cap.spent_micros == 14_000  # the cap window is always the current one

    # tenant admins read their own usage, never the platform's
    u = await make_user(ctx, "ana@acme.test")
    await grant_role(ctx, u, "acme", None, "admin")
    ana = await session_caller(ctx, client, u)
    assert (await ana.get("/v1/admin/usage")).status_code == 403
