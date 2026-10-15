"""Row-level security (plan §9, IDOR row): two tenants on pooled connections.

The request-path role sees and writes only the tenant its transaction is pinned to, even
when application code forgets every ``WHERE tenant_id = …`` (the "buggy code" queries below
are deliberately unscoped), and even when the very same pooled connection served the other
tenant a moment earlier. A transaction that was never pinned fails closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import psycopg
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import Database
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server_testkit.identity import make_tenant
from mapi_spec.api import new_id
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, ProgrammingError

pytestmark = pytest.mark.integration


@pytest.fixture
async def one_conn_db(ctx: ServerContext) -> AsyncIterator[Database]:
    """A database whose request-path pool holds exactly one connection (forced reuse)."""
    db = Database(ctx.db.url, pool_size=1, system_pool_size=1)
    db.app_engine.pool._max_overflow = 0  # type: ignore[attr-defined]
    try:
        yield db
    finally:
        await db.dispose()


async def _seed(ctx: ServerContext) -> dict[str, dict[str, str]]:
    """Two tenants, each with a dataset, a run and an audit row."""
    ids: dict[str, dict[str, str]] = {}
    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant, projects=("main",))
        ts = now()
        ds_id, run_id = new_id("ds"), new_id("run")
        async with ctx.db.session() as s:
            s.add(
                m.Dataset(
                    id=ds_id, tenant_id=tenant, project_id="main", name=f"{tenant}-sales",
                    labels={}, next_version=1, created_at=ts, updated_at=ts,
                )
            )  # fmt: skip
            s.add(
                m.Run(
                    id=run_id, tenant_id=tenant, project_id="main", kind="fit", state="queued",
                    attempt=1, resource_class="S", timeout_s=60, labels={}, payload={},
                    inputs={}, checkpointable=False, infra_retries=0, finalize_attempts=0,
                    finalize={}, last_event_id=0, created_at=ts, updated_at=ts,
                )
            )  # fmt: skip
        ids[tenant] = {"dataset": ds_id, "run": run_id}
    return ids


async def test_unscoped_queries_only_see_the_pinned_tenant_on_a_reused_connection(
    ctx: ServerContext, one_conn_db: Database
) -> None:
    ids = await _seed(ctx)
    db = one_conn_db
    pids: list[int] = []
    for tenant, other in (("alpha", "beta"), ("beta", "alpha"), ("alpha", "beta")):
        async with db.tenant_session(tenant) as s:
            pids.append(int((await s.execute(text("SELECT pg_backend_pid()"))).scalar_one()))
            # buggy code: no tenant filter anywhere
            datasets = list((await s.execute(select(m.Dataset))).scalars())
            runs = list((await s.execute(select(m.Run))).scalars())
            assert {d.tenant_id for d in datasets} == {tenant}
            assert {r.id for r in runs} == {ids[tenant]["run"]}
            assert (await s.execute(select(func.count()).select_from(m.Tenant))).scalar_one() == 1
            # direct lookups of the other tenant's ids find nothing
            assert await s.get(m.Dataset, ids[other]["dataset"]) is None
            # writes to the other tenant's rows silently affect nothing
            res = await s.execute(
                update(m.Run).where(m.Run.id == ids[other]["run"]).values(state="cancelled")
            )
            assert res.rowcount == 0  # type: ignore[attr-defined]
            res = await s.execute(delete(m.Dataset).where(m.Dataset.id == ids[other]["dataset"]))
            assert res.rowcount == 0  # type: ignore[attr-defined]
    assert len(set(pids)) == 1, "the test must reuse one pooled connection"
    async with ctx.db.session() as s:  # the platform role sees both, untouched
        runs = {r.id: r.state for r in (await s.execute(select(m.Run))).scalars()}
        assert runs == {ids["alpha"]["run"]: "queued", ids["beta"]["run"]: "queued"}
        assert (await s.execute(select(func.count()).select_from(m.Dataset))).scalar_one() == 2


async def test_inserting_or_moving_rows_into_another_tenant_is_refused(
    ctx: ServerContext, one_conn_db: Database
) -> None:
    ids = await _seed(ctx)
    ts = now()
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with one_conn_db.tenant_session("alpha") as s:
            s.add(
                m.Dataset(
                    id=new_id("ds"), tenant_id="beta", project_id="main", name="smuggled",
                    labels={}, next_version=1, created_at=ts, updated_at=ts,
                )
            )  # fmt: skip
            await s.flush()
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with one_conn_db.tenant_session("alpha") as s:
            await s.execute(
                update(m.Dataset)
                .where(m.Dataset.id == ids["alpha"]["dataset"])
                .values(tenant_id="beta")
            )
    async with ctx.db.session() as s:
        ds = await s.get(m.Dataset, ids["alpha"]["dataset"])
        assert ds is not None and ds.tenant_id == "alpha"


async def test_a_transaction_without_a_tenant_fails_closed(
    ctx: ServerContext, one_conn_db: Database
) -> None:
    await _seed(ctx)
    # a fresh connection: the setting does not exist, so every policy errors
    fresh = Database(ctx.db.url, pool_size=1)
    try:
        async with fresh.app_engine.connect() as conn:
            with pytest.raises(DBAPIError, match=r"app\.tenant_id"):
                await conn.execute(select(func.count()).select_from(m.Run))
    finally:
        await fresh.dispose()
    # a pooled connection that served a tenant before: the setting is reset to '' at the
    # end of that transaction (set_config(..., true)), which matches no row
    async with one_conn_db.tenant_session("alpha") as s:
        assert (await s.execute(select(func.count()).select_from(m.Run))).scalar_one() == 1
    async with one_conn_db.app_engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(m.Run))).scalar_one() == 0
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "mapi_app"


async def test_the_app_role_cannot_turn_row_security_off(
    ctx: ServerContext, one_conn_db: Database
) -> None:
    await _seed(ctx)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with one_conn_db.tenant_session("alpha") as s:
            await s.execute(text("SET LOCAL row_security = off"))
            await s.execute(select(func.count()).select_from(m.Run))


async def test_audit_and_cost_records_are_append_only(ctx: ServerContext) -> None:
    """Audit events and cost ledger entries can be added but never edited or erased: not by
    the request-path role (a bug or an injection there cannot rewrite a tenant's audit trail
    or billing history), and not by the platform role either."""
    await _seed(ctx)
    ts = now()
    async with ctx.db.tenant_session("alpha") as s:
        s.add(
            m.AuditEvent(
                id=new_id("aud"), ts=ts, tenant_id="alpha", operator=False, action="run.create",
                resource_kind="run", outcome="success", details={},
            )
        )  # fmt: skip
    async with ctx.db.session() as s:
        s.add(
            m.CostLedgerEntry(
                id=new_id("cle"), tenant_id="alpha", project_id="main", run_id="run_x",
                attempt=1, kind="actual", resource_class="S", task_count=1, wall_seconds=1,
                vcpu_seconds=2, gib_seconds=8, price_version="v", amount_micros=10,
                currency="USD", recorded_at=ts,
            )
        )  # fmt: skip
    statements = [
        update(m.AuditEvent).values(outcome="denied"),
        delete(m.AuditEvent),
        update(m.CostLedgerEntry).values(amount_micros=0),
        delete(m.CostLedgerEntry),
    ]
    for stmt in statements:
        with pytest.raises(DBAPIError, match=r"permission denied|append-only"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(stmt)
        with pytest.raises(DBAPIError, match=r"permission denied|append-only"):
            async with ctx.db.session() as s:
                await s.execute(stmt)
    async with ctx.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(m.AuditEvent))).scalar_one() == 1
        ledger = (await s.execute(select(m.CostLedgerEntry))).scalar_one()
        assert ledger.amount_micros == 10


async def test_the_gateway_role_reads_identity_but_never_tenant_content(
    ctx: ServerContext,
) -> None:
    """Plan §9 (IDOR row): only the orchestrator holds the row-level-security bypass. The
    API's platform lookups (sign-in, memberships, operator metadata, pushed events) run as
    the gateway role: it sees identity rows of every tenant and a few metadata columns of
    runs and the ledger, but no tenant content, so an authorization slip in a handler is
    still contained by row-level security."""
    from mapi_server.db import acting_as_gateway

    ids = await _seed(ctx)
    with acting_as_gateway():
        async with ctx.db.session() as s:
            assert (await s.execute(text("SELECT current_user"))).scalar_one() == "mapi_gateway"
            tenants = {t.id for t in (await s.execute(select(m.Tenant))).scalars()}
            assert {"alpha", "beta"} <= tenants
            states = dict((await s.execute(select(m.Run.id, m.Run.state))).all())
            assert set(states) == {ids["alpha"]["run"], ids["beta"]["run"]}
    forbidden = [
        select(m.Dataset),
        select(m.Run),  # payload, inputs, labels: not readable
        select(m.Spec),
        select(m.Model),
        select(m.RunEvent),
        update(m.Run).values(state="cancelled"),
        delete(m.Dataset),
    ]
    for stmt in forbidden:
        with pytest.raises(ProgrammingError, match="permission denied"), acting_as_gateway():
            async with ctx.db.session() as s:
                # even pinned to a tenant (so row-level security alone would let rows through)
                await s.execute(text("SELECT set_config('app.tenant_id', 'alpha', true)"))
                await s.execute(stmt)
    async with ctx.db.session() as s:  # outside a request: the orchestrator's role
        assert (await s.execute(text("SELECT current_user"))).scalar_one() == "mapi_system"
    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        row = await (
            await conn.execute(
                "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'mapi_gateway'"
            )
        ).fetchone()
    assert row == (False, False)


async def test_the_api_process_starts_up_as_the_gateway(ctx: ServerContext) -> None:
    """``mapi-server api`` provisions the platform tenant, the dev accounts, invited admins
    and the catalog cache at start-up with the gateway role alone (it has no login of the
    bypass role in a cloud deployment)."""
    from mapi_server.app import startup
    from mapi_server.db import acting_as_gateway
    from mapi_server.settings import Profile

    ctx.settings = ctx.settings.model_copy(
        update={
            "profile": Profile.DEV,
            "tenant_id": "gw-platform",
            "default_tenant_admins": ["boss@gw.test"],
        }
    )
    with acting_as_gateway():
        await startup(ctx, listen=False)
    async with ctx.db.session() as s:
        tenant = await s.get(m.Tenant, "gw-platform")
        emails = {u.email for u in (await s.execute(select(m.User))).scalars()}
    assert tenant is not None and {"boss@gw.test", "admin@dev.local"} <= emails


async def test_api_requests_cannot_use_the_bypass_role(
    ctx: ServerContext, client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler that (by mistake) reads tenant content through a platform session gets a
    permission error instead of every tenant's rows."""
    import httpx

    assert isinstance(client, httpx.AsyncClient)
    await _seed(ctx)

    async def leaky_catalog(self: object, session: object) -> object:
        async with ctx.db.session() as s:  # a platform session, inside a request
            await s.execute(text("SELECT set_config('app.tenant_id', 'alpha', true)"))
            return (await s.execute(select(m.Dataset))).scalars().all()

    monkeypatch.setattr(type(ctx.catalogs), "require", leaky_catalog)
    with pytest.raises(ProgrammingError, match="permission denied"):
        await client.get("/v1/catalog")


async def test_every_tenant_table_forces_rls_and_the_app_role_owns_nothing(
    ctx: ServerContext,
) -> None:
    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        rows = await (
            await conn.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       pg_get_userbyid(c.relowner)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND c.relkind = 'r'
                """
            )
        ).fetchall()
        tables = {r[0]: r for r in rows}
        for name in ("tenants", *m.TENANT_TABLES):
            _, enabled, forced, owner = tables[name]
            assert enabled and forced, name
            assert owner not in ("mapi_app", "mapi_system"), name
        policies = await (
            await conn.execute(
                "SELECT tablename, policyname, qual FROM pg_policies "
                "WHERE schemaname = current_schema()"
            )
        ).fetchall()
        tenant_policies = {t: q for t, p, q in policies if p == "mapi_tenant_isolation"}
        assert set(tenant_policies) == {"tenants", *m.TENANT_TABLES}
        assert all(
            "current_setting('app.tenant_id'::text, false)" in q for q in tenant_policies.values()
        )
        bypass = await (
            await conn.execute(
                "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles "
                "WHERE rolname IN ('mapi_app', 'mapi_system')"
            )
        ).fetchall()
        assert dict((r[0], (r[1], r[2])) for r in bypass) == {
            "mapi_app": (False, False),
            "mapi_system": (True, False),
        }


async def test_requests_cannot_reach_another_tenants_rows(
    ctx: ServerContext, client: object
) -> None:
    """End to end: a member of alpha naming beta's run through the API gets 404."""
    import httpx
    from mapi_server_testkit.identity import grant_role, make_user, session_caller

    assert isinstance(client, httpx.AsyncClient)
    ids = await _seed(ctx)
    ana = await make_user(ctx, "ana@alpha.test")
    await grant_role(ctx, ana, "alpha", "main", "runner")
    alpha = await session_caller(ctx, client, ana, tenant_id="alpha", project_id="main")
    assert (await alpha.get(f"/v1/runs/{ids['alpha']['run']}")).status_code == 200
    assert (await alpha.get(f"/v1/runs/{ids['beta']['run']}")).status_code == 404
    assert (await alpha.get(f"/v1/datasets/{ids['beta']['dataset']}")).status_code == 404
    r = await alpha.post(f"/v1/runs/{ids['beta']['run']}:cancel")
    assert r.status_code == 404
    # naming beta's project is refused before any query (no membership: 404, not 403)
    beta = alpha.in_project("beta", "main")
    r = await beta.get(f"/v1/runs/{ids['beta']['run']}")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    page = (await alpha.get("/v1/runs")).json()
    assert [r["id"] for r in page["items"]] == [ids["alpha"]["run"]]


async def test_the_gateway_role_never_writes_tenant_data(ctx: ServerContext) -> None:
    """The internet-facing API and events services run platform sessions as the gateway role
    (migration 0005), not the role that passes row-level security (plan §9:
    that one is for the dispatcher, reconciler and finalizer only). It reads what sign-in,
    the operator console and the quota counters need, and cannot change tenant data."""
    ids = await _seed(ctx)
    db = ctx.db
    async with db.gateway_session() as s:
        assert (await s.execute(text("SELECT current_user"))).scalar_one() == "mapi_gateway"
        tenants = (await s.execute(select(func.count()).select_from(m.Tenant))).scalar_one()
        runs = (await s.execute(select(func.count()).select_from(m.Run))).scalar_one()
        assert tenants >= 2 and runs >= 2  # cross-tenant reads: sign-in, operators, quotas
        role = (
            await s.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).scalar_one()
        assert role is False
    forbidden = [
        update(m.Run).where(m.Run.id == ids["alpha"]["run"]).values(state="failed"),
        delete(m.Run),
        delete(m.AuditEvent),
        update(m.AuditEvent).values(outcome="success"),
        select(m.Dataset),
        update(m.Dataset).values(name="x"),
        delete(m.CostLedgerEntry),
    ]
    for stmt in forbidden:
        with pytest.raises((ProgrammingError, DBAPIError)):
            async with db.gateway_session() as s:
                await s.execute(stmt)
    async with ctx.db.session() as s:  # the orchestrator's role is unaffected
        run = await s.get(m.Run, ids["alpha"]["run"])
        assert run is not None and run.state == "queued"
