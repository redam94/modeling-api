"""Client dashboards' tables (0018_client_dashboards, 0020_any_model_dashboards): row-level
security, least-privilege grants, the gateway's share-lookup columns, the project-scoped
client role, the layout 2 readout kinds, inputs and assets, and downgrades that refuse while
data they cannot keep exists."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic.script import ScriptDirectory
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.db.migrate import alembic_config, downgrade, upgrade
from mapi_server.services.runs import now
from mapi_server_testkit.identity import make_tenant
from mapi_spec.api import new_id
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.integration

DASHBOARD_TABLES: tuple[type[m.Base], ...] = (
    m.Dashboard,
    m.DashboardRevision,
    m.DashboardGrant,
    m.DashboardReadout,
    m.DashboardShare,
    m.DashboardRefitRequest,
    m.DashboardAsset,
)
APP_PRIVILEGES = {
    "dashboards": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "dashboard_revisions": {"SELECT", "INSERT"},
    "dashboard_grants": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "dashboard_readouts": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "dashboard_shares": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "dashboard_refit_requests": {"SELECT", "INSERT", "UPDATE"},
    "dashboard_assets": {"SELECT", "INSERT", "UPDATE"},
}
LOOKUP = (
    "id",
    "tenant_id",
    "project_id",
    "dashboard_id",
    "token_hash",
    "expires_at",
    "revoked_at",
    "session_version",
)


def _rows(tenant: str) -> dict[str, Any]:
    """One row of every dashboard table for ``tenant``'s ``main`` project."""
    ts = now()
    scope = {"tenant_id": tenant, "project_id": "main"}
    dsh, dsr, dss = new_id("dsh"), new_id("dsr"), new_id("dss")
    return {
        "dashboard": m.Dashboard(
            id=dsh, name=f"{tenant}-weekly", status="published", draft_spec={"title": "W"},
            draft_etag='"1"', published_revision_id=dsr, run_as="usr_ana", created_by="usr_ana",
            **scope,
        ),
        "revision": m.DashboardRevision(
            id=dsr, dashboard_id=dsh, seq=1, spec={"title": "W"}, spec_hash="sha256:" + "0" * 64,
            model_version_id="mv_1", model_ref="model://main/mmm@v1", created_by="usr_ana",
            **scope,
        ),
        "grant": m.DashboardGrant(
            dashboard_id=dsh, user_id=f"usr_{tenant}_client", granted_by="usr_ana", **scope
        ),
        "readout": m.DashboardReadout(
            dashboard_id=dsh, seq=1, revision_id=dsr, kind="baseline", trigger="publish",
            visibility="published", status="ready", title="Baseline", actor_kind="system",
            model_version_id="mv_1", model_ref="model://main/mmm@v1",
            spec_hash="sha256:" + "0" * 64, run_ids=["run_1"], snapshot={"lead": "Fine."},
            period_start=date(2026, 10, 12), period_end=date(2026, 10, 18), ready_at=ts,
            **scope,
        ),
        "share": m.DashboardShare(
            id=dss, dashboard_id=dsh, token_hash=f"h-{tenant}-{secrets.token_hex(8)}",
            display="mpd_…x7k2", passcode_hash="scrypt$14$8$1$salt$hash",
            allowed_inputs=["whatif"], created_by="usr_ana", expires_at=ts + timedelta(days=30),
            **scope,
        ),
        "refit": m.DashboardRefitRequest(
            id=new_id("drr"), dashboard_id=dsh, readout_seq=1, requested_by_kind="share",
            requested_by=dss, **scope,
        ),
        "asset": m.DashboardAsset(
            id=new_id("dsa"), dashboard_id=dsh, revision_id=dsr, plot_id="ppc",
            recipe="core.ppc_fan", status="ready", run_id="run_2", bucket="bundles",
            name=f"t/{tenant}/dsa.png", generation=1, bytes=2048, width=800, height=480,
            ready_at=ts, **scope,
        ),
    }  # fmt: skip


async def _seed(ctx: ServerContext) -> dict[str, dict[str, Any]]:
    ids: dict[str, dict[str, Any]] = {}
    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant, projects=("main",))
        rows = _rows(tenant)
        async with ctx.db.session() as s:
            for key in ("dashboard", "revision", "grant", "readout", "share", "refit", "asset"):
                s.add(rows[key])
                await s.flush()  # the cyclic-free insert order the foreign keys need
        ids[tenant] = {
            "dashboard": rows["dashboard"].id,
            "share": rows["share"].id,
            "asset": rows["asset"].id,
        }
    return ids


async def test_dashboard_tables_force_rls(ctx: ServerContext) -> None:
    """Every dashboard table has forced row-level security with the tenant and platform
    policies and least-privilege grants: a tenant session sees only its own rows, even with
    no tenant filter in the query, and cannot reach the other tenant's dashboard by id."""
    ids = await _seed(ctx)
    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        for table, expected in APP_PRIVILEGES.items():
            rls = await (
                await conn.execute(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s",
                    (table,),
                )
            ).fetchone()
            assert rls == (True,), table
            policies = {
                r[0]
                for r in await (
                    await conn.execute(
                        "SELECT policyname FROM pg_policies WHERE tablename = %s", (table,)
                    )
                ).fetchall()
            }
            wanted = {"mapi_tenant_isolation", "mapi_platform_access"}
            if table == "dashboard_shares":
                wanted.add("mapi_gateway_access_select")
            assert policies == wanted, table
            grants = await (
                await conn.execute(
                    "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = %s",
                    (table,),
                )
            ).fetchall()
            assert {p for g, p in grants if g == "mapi_app"} == expected, table
            assert not {p for g, p in grants if g == "mapi_gateway"}, table  # columns only
    for tenant, other in (("alpha", "beta"), ("beta", "alpha")):
        async with ctx.db.tenant_session(tenant) as s:
            for model in DASHBOARD_TABLES:
                rows = list((await s.execute(select(model))).scalars())
                assert len(rows) == 1 and {r.tenant_id for r in rows} == {tenant}, model
            assert await s.get(m.Dashboard, ids[other]["dashboard"]) is None
            res = await s.execute(
                update(m.Dashboard)
                .where(m.Dashboard.id == ids[other]["dashboard"])
                .values(status="archived")
            )
            assert res.rowcount == 0  # type: ignore[attr-defined]
            res = await s.execute(
                delete(m.DashboardShare).where(m.DashboardShare.id == ids[other]["share"])
            )
            assert res.rowcount == 0  # type: ignore[attr-defined]
            assert await s.get(m.DashboardAsset, ids[other]["asset"]) is None
            res = await s.execute(
                update(m.DashboardAsset)
                .where(m.DashboardAsset.id == ids[other]["asset"])
                .values(status="failed")
            )
            assert res.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(
                m.DashboardGrant(
                    dashboard_id=ids["beta"]["dashboard"], user_id="usr_x", granted_by="usr_ana",
                    tenant_id="beta", project_id="main",
                )
            )  # fmt: skip
            await s.flush()
    async with ctx.db.session() as s:  # the platform role sees both tenants, untouched
        statuses = (await s.execute(select(m.Dashboard.status))).scalars().all()
        assert sorted(statuses) == ["published", "published"]


async def test_revisions_and_refit_requests_are_never_erased_by_the_request_path(
    ctx: ServerContext,
) -> None:
    """Published revisions are immutable for the request path (insert and read only); refit
    requests can be decided and assets finished, but neither deleted. All go with their
    dashboard (cascade)."""
    ids = await _seed(ctx)
    statements = [
        update(m.DashboardRevision).values(spec_hash="sha256:" + "1" * 64),
        delete(m.DashboardRevision),
        delete(m.DashboardRefitRequest),
        delete(m.DashboardAsset),
    ]
    for stmt in statements:
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(stmt)
    async with ctx.db.tenant_session("alpha") as s:
        await s.execute(update(m.DashboardRefitRequest).values(status="rejected"))
        await s.execute(update(m.DashboardAsset).values(status="failed", problem="timed out"))
        await s.execute(delete(m.Dashboard).where(m.Dashboard.id == ids["alpha"]["dashboard"]))
    async with ctx.db.session() as s:
        for model in DASHBOARD_TABLES:
            tenants = (await s.execute(select(model.tenant_id))).scalars().all()  # type: ignore[attr-defined]
            assert tenants == ["beta"], model


async def test_gateway_sees_only_share_lookup_columns(ctx: ServerContext) -> None:
    """The public share-session exchange (gateway role) reads the lookup columns of every
    tenant's links, never the passcode hash or the counters, nothing of the other dashboard
    tables, and writes nothing."""
    ids = await _seed(ctx)
    cols = [getattr(m.DashboardShare, c) for c in LOOKUP]
    async with ctx.db.gateway_session() as s:
        rows = (await s.execute(select(*cols))).all()
    assert {r.id for r in rows} == {ids["alpha"]["share"], ids["beta"]["share"]}
    assert {r.session_version for r in rows} == {1}
    forbidden = [
        select(m.DashboardShare.passcode_hash),
        select(m.DashboardShare.failed_attempts),
        select(m.DashboardShare),
        select(m.Dashboard),
        select(m.DashboardRevision.id),
        select(m.DashboardGrant.user_id),
        select(m.DashboardReadout.seq),
        select(m.DashboardRefitRequest.id),
        select(m.DashboardAsset.id),
        update(m.DashboardShare).values(views=99),
        delete(m.DashboardShare),
    ]
    for stmt in forbidden:
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with ctx.db.gateway_session() as s:
                # pinned to a tenant, so row-level security alone would let rows through
                await s.execute(text("SELECT set_config('app.tenant_id', 'alpha', true)"))
                await s.execute(stmt)


async def test_client_membership_must_be_project_scoped(ctx: ServerContext) -> None:
    """``client`` is a valid role only with a project: a tenant-wide client grant would be
    overridden by (or override) the tenant's other grants."""
    await make_tenant(ctx, "alpha", projects=("main",))

    def grant(project_id: str | None, role: str) -> m.Membership:
        return m.Membership(
            id=new_id("mem"), tenant_id="alpha", project_id=project_id,
            principal_id=new_id("usr"), role=role,
        )  # fmt: skip

    async with ctx.db.session() as s:
        s.add(grant("main", "client"))
        s.add(grant(None, "viewer"))
    for project_id, role in ((None, "client"), ("main", "owner")):
        with pytest.raises(IntegrityError, match="ck_memberships_"):
            async with ctx.db.session() as s:
                s.add(grant(project_id, role))
                await s.flush()
    async with ctx.db.session() as s:
        roles = await s.execute(
            select(m.Membership.role)
            .where(m.Membership.tenant_id == "alpha")
            .order_by(m.Membership.role)
        )
    assert list(roles.scalars()) == ["client", "viewer"]


async def test_dashboard_size_caps_and_one_open_refit_request(ctx: ServerContext) -> None:
    """The database backs the contract's caps (spec 64 KiB, snapshot 400 000 bytes as text,
    asset 512 KiB of PNG) and allows at most one open refit request per dashboard."""
    ids = await _seed(ctx)
    dsh = ids["alpha"]["dashboard"]
    for stmt in (
        update(m.Dashboard).values(draft_spec={"note": "x" * 70_000}),
        update(m.DashboardReadout).values(snapshot={"lead": "x" * 410_000}),
        update(m.DashboardShare).values(allowed_inputs=["refit"]),
        update(m.DashboardShare).values(allowed_inputs=["batch", "chat"]),
        update(m.DashboardReadout).values(visibility="public"),
        update(m.DashboardReadout).values(kind="chat"),
        update(m.DashboardReadout).values(trigger="upload"),
        update(m.DashboardAsset).values(bytes=524_289),
        update(m.DashboardAsset).values(bytes=0),
        update(m.DashboardAsset).values(content_type="image/svg+xml"),
        update(m.DashboardAsset).values(status="rendering"),
        update(m.DashboardAsset).values(generation=None),
    ):
        with pytest.raises(IntegrityError, match="ck_dashboard"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(stmt)
    with pytest.raises(IntegrityError, match="uq_dashboard_refit_requests_open"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(
                m.DashboardRefitRequest(
                    id=new_id("drr"), dashboard_id=dsh, readout_seq=1, requested_by_kind="client",
                    requested_by="usr_c", tenant_id="alpha", project_id="main",
                )
            )  # fmt: skip
            await s.flush()
    async with ctx.db.tenant_session("alpha") as s:  # superseding the open one frees the slot
        await s.execute(update(m.DashboardRefitRequest).values(status="superseded"))
        s.add(
            m.DashboardRefitRequest(
                id=new_id("drr"), dashboard_id=dsh, readout_seq=1, requested_by_kind="client",
                requested_by="usr_c", tenant_id="alpha", project_id="main",
            )
        )  # fmt: skip
    async with ctx.db.tenant_session("alpha") as s:
        n = await s.execute(select(func.count()).select_from(m.DashboardRefitRequest))
        assert n.scalar_one() == 2


async def test_layout_2_readout_kinds_inputs_and_snapshots_are_accepted(
    ctx: ServerContext,
) -> None:
    """0020 widens the checks: scenario-row and batch readouts, share links that allow them,
    a layout 2 snapshot above the old 300 000-byte cap, and a queued asset with no object
    yet (one per revision and plot)."""
    ids = await _seed(ctx)
    dsh = ids["alpha"]["dashboard"]
    async with ctx.db.tenant_session("alpha") as s:
        await s.execute(
            update(m.DashboardReadout).values(
                kind="prediction", trigger="scenario_rows", snapshot={"lead": "x" * 350_000}
            )
        )
        await s.execute(
            update(m.DashboardShare).values(
                allowed_inputs=["whatif", "actuals", "scenario_rows", "batch"]
            )
        )
        await s.execute(update(m.DashboardReadout).values(kind="batch", trigger="batch"))
        rev = (await s.execute(select(m.DashboardRevision.id))).scalar_one()
        s.add(
            m.DashboardAsset(
                id=new_id("dsa"), dashboard_id=dsh, revision_id=rev, plot_id="fit_vs_actual",
                tenant_id="alpha", project_id="main",
            )
        )  # fmt: skip
    async with ctx.db.tenant_session("alpha") as s:
        queued = (
            await s.execute(select(m.DashboardAsset).where(m.DashboardAsset.status == "queued"))
        ).scalar_one()
        assert queued.content_type == "image/png" and queued.bucket is None
        kinds = (await s.execute(select(m.DashboardReadout.kind))).scalars().all()
        assert kinds == ["batch"]
    with pytest.raises(IntegrityError, match="uq_dashboard_assets_revision_id_plot_id"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(
                m.DashboardAsset(
                    id=new_id("dsa"), dashboard_id=dsh, revision_id=rev, plot_id="ppc",
                    tenant_id="alpha", project_id="main",
                )
            )  # fmt: skip
            await s.flush()


# ------------------------------------------------------------------ migration downgrade


@pytest.fixture
def scratch_db(pg_admin_url: str) -> Iterator[str]:
    name = f"mapi_mig_{secrets.token_hex(4)}"
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(pg_admin_url)
    url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    try:
        yield url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def test_downgrade_refuses_with_client_members_or_dashboards(scratch_db: str) -> None:
    """Stepping below 0018 refuses while a client membership or a dashboard exists (an older
    server would treat neither correctly); once both are gone it drops the tables and the
    old role check is back."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    before = script.get_revision("0018_client_dashboards").down_revision
    assert before == "0017_collections"
    upgrade(scratch_db)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, trust_tier, status) VALUES ('t1', 't1', "
            "'internal', 'active')"
        )
        conn.execute(
            "INSERT INTO projects (tenant_id, id, project_id, name) VALUES ('t1', 'p1', 'p1', 'p1')"
        )
        conn.execute(
            "INSERT INTO memberships (id, tenant_id, project_id, principal_id, principal_kind, "
            "role, capabilities) VALUES ('mem_1', 't1', 'p1', 'usr_1', 'user', 'client', '[]')"
        )
    with pytest.raises(Exception, match="client memberships or dashboards exist"):
        downgrade(scratch_db, before)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("UPDATE memberships SET role = 'viewer' WHERE id = 'mem_1'")
        conn.execute(
            "INSERT INTO dashboards (id, tenant_id, project_id, name, draft_spec, draft_etag, "
            "created_by) VALUES ('dsh_1', 't1', 'p1', 'd', '{}', '\"1\"', 'usr_1')"
        )
    with pytest.raises(Exception, match="client memberships or dashboards exist"):
        downgrade(scratch_db, before)
    assert "dashboards" in _tables(scratch_db)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("DELETE FROM dashboards")
    downgrade(scratch_db, before)
    tables = {"dashboards", "dashboard_shares", "dashboard_refit_requests"}
    assert not tables & _tables(scratch_db)
    with (
        psycopg.connect(scratch_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute("UPDATE memberships SET role = 'client' WHERE id = 'mem_1'")
    upgrade(scratch_db)
    assert tables <= _tables(scratch_db)


def _layout_2_seed(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, trust_tier, status) VALUES ('t1', 't1', "
            "'internal', 'active')"
        )
        conn.execute(
            "INSERT INTO projects (tenant_id, id, project_id, name) VALUES ('t1', 'p1', 'p1', 'p1')"
        )
        conn.execute(
            "INSERT INTO dashboards (id, tenant_id, project_id, name, draft_spec, draft_etag, "
            "created_by) VALUES ('dsh_1', 't1', 'p1', 'd', '{}', '\"1\"', 'usr_1')"
        )
        conn.execute(
            "INSERT INTO dashboard_revisions (id, tenant_id, project_id, dashboard_id, seq, "
            "spec, spec_hash, model_version_id, model_ref, created_by) VALUES ('dsr_1', 't1', "
            "'p1', 'dsh_1', 1, '{}', 'sha256:0', 'mv_1', 'model://p1/m@v1', 'usr_1')"
        )
        conn.execute(
            "INSERT INTO dashboard_readouts (tenant_id, project_id, dashboard_id, seq, "
            "revision_id, kind, trigger, visibility, status, title, actor_kind, "
            "model_version_id, model_ref, spec_hash) VALUES ('t1', 'p1', 'dsh_1', 1, 'dsr_1', "
            "'baseline', 'publish', 'published', 'ready', 'B', 'system', 'mv_1', "
            "'model://p1/m@v1', 'sha256:0')"
        )
        conn.execute(
            "INSERT INTO dashboard_shares (id, tenant_id, project_id, dashboard_id, token_hash, "
            "display, created_by, expires_at, allowed_inputs) VALUES ('dss_1', 't1', 'p1', "
            "'dsh_1', 'h1', 'mpd_x', 'usr_1', now() + interval '1 day', ARRAY['whatif'])"
        )


def test_downgrade_below_0020_refuses_with_layout_2_data(scratch_db: str) -> None:
    """Stepping below 0020 keeps v0.16 rows (a baseline readout, a what-if share link) and
    refuses, naming the counts, while a readout has a new kind or trigger, a link allows a
    new input, a snapshot is above the old cap or an asset exists; once those are gone it
    drops ``dashboard_assets`` and the 0018 checks are back."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    assert script.get_revision("0020_any_model_dashboards").down_revision == (
        "0019_dashboard_hardening"
    )
    upgrade(scratch_db)
    _layout_2_seed(scratch_db)
    fixes = [
        (
            "UPDATE dashboard_readouts SET kind = 'prediction', trigger = 'scenario_rows'",
            "UPDATE dashboard_readouts SET kind = 'baseline', trigger = 'publish'",
            "1 readouts with a new kind or trigger",
        ),
        (
            "UPDATE dashboard_shares SET allowed_inputs = ARRAY['whatif','batch']",
            "UPDATE dashboard_shares SET allowed_inputs = ARRAY['whatif']",
            "1 share links with a new input",
        ),
        (
            "UPDATE dashboard_readouts SET snapshot = jsonb_build_object('lead', "
            "repeat('x', 350000))",
            'UPDATE dashboard_readouts SET snapshot = \'{"lead": "Fine."}\'',
            "1 snapshots above 300000 bytes",
        ),
        (
            "INSERT INTO dashboard_assets (id, tenant_id, project_id, dashboard_id, "
            "revision_id, plot_id) VALUES ('dsa_1', 't1', 'p1', 'dsh_1', 'dsr_1', 'ppc')",
            "DELETE FROM dashboard_assets",
            "1 assets",
        ),
        (  # the automatic upgrade: layout 2 specs in rows of the old kinds
            "UPDATE dashboard_revisions SET spec = spec || '{\"layout_version\": 2}'",
            "UPDATE dashboard_revisions SET spec = spec - 'layout_version'",
            "1 layout 2 specs or snapshots",
        ),
        (
            "UPDATE dashboards SET draft_spec = draft_spec || '{\"layout_version\": 2}'",
            "UPDATE dashboards SET draft_spec = draft_spec - 'layout_version'",
            "1 layout 2 specs or snapshots",
        ),
        (
            "UPDATE dashboard_readouts SET snapshot = '{\"schema_version\": 2}'",
            'UPDATE dashboard_readouts SET snapshot = \'{"lead": "Fine."}\'',
            "1 layout 2 specs or snapshots",
        ),
    ]
    for make, undo, message in fixes:
        with psycopg.connect(scratch_db, autocommit=True) as conn:
            conn.execute(make)
        with pytest.raises(Exception, match=message):
            downgrade(scratch_db, "0019_dashboard_hardening")
        assert "dashboard_assets" in _tables(scratch_db)
        with psycopg.connect(scratch_db, autocommit=True) as conn:
            conn.execute(undo)
    downgrade(scratch_db, "0019_dashboard_hardening")
    assert "dashboard_assets" not in _tables(scratch_db)
    for stmt in (
        "UPDATE dashboard_readouts SET kind = 'batch'",
        "UPDATE dashboard_readouts SET trigger = 'scenario_rows'",
        "UPDATE dashboard_shares SET allowed_inputs = ARRAY['scenario_rows']",
        "UPDATE dashboard_readouts SET snapshot = jsonb_build_object('lead', repeat('x', 310000))",
    ):
        with (
            psycopg.connect(scratch_db, autocommit=True) as conn,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            conn.execute(stmt)
    with psycopg.connect(scratch_db) as conn:  # the v0.16 rows survived the round trip
        row = conn.execute("SELECT kind, trigger FROM dashboard_readouts").fetchone()
        assert row == ("baseline", "publish")
    upgrade(scratch_db)
    assert "dashboard_assets" in _tables(scratch_db)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("UPDATE dashboard_readouts SET kind = 'batch', trigger = 'batch'")
