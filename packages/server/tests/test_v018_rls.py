"""v0.18 tables (0021_v018_library_onboarding): row-level security and least-privilege grants
on library items, their revisions, onboarding progress and tenant samples; the scope, enum
and size checks that back the contracts; ``projects.kind``; the preview-key index; and a
downgrade that refuses while library or live sample data exists."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any, get_args
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic.script import ScriptDirectory
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.db.migrate import alembic_config, downgrade, upgrade
from mapi_server_testkit.identity import make_tenant
from mapi_spec.api import new_id
from mapi_spec.library import LibraryKind, LibraryScope
from mapi_spec.onboarding import MAX_ONBOARDING_BYTES
from mapi_spec.samples import SampleState
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.integration

V018_TABLES: tuple[type[m.Base], ...] = (
    m.LibraryItem,
    m.LibraryItemRevision,
    m.OnboardingProgress,
    m.TenantSample,
)
TABLE_NAMES = tuple(t.__tablename__ for t in V018_TABLES)
HASH = "sha256:" + "0" * 64


def _rows(tenant: str) -> dict[str, Any]:
    """One row of every v0.18 table for ``tenant`` (a project item in ``main``)."""
    item = new_id("lib")
    return {
        "item": m.LibraryItem(
            id=item, tenant_id=tenant, project_id="main", scope="project", kind="model_recipe",
            name="store-sales", title="Store sales", tags=["retail"], created_by="usr_ana",
        ),
        "revision": m.LibraryItemRevision(
            item_id=item, revision=1, tenant_id=tenant, project_id="main",
            document={"kind": "ModelRecipe"}, doc_hash=HASH, valid=True, created_by="usr_ana",
        ),
        "progress": m.OnboardingProgress(
            tenant_id=tenant, principal_id="usr_ana", state={"tips_off": True}
        ),
        "sample": m.TenantSample(
            tenant_id=tenant, sample_id="geo_mmm", project_id="main", state="fitting",
            created_by="usr_ana",
        ),
    }  # fmt: skip


async def _seed(ctx: ServerContext) -> dict[str, str]:
    """Both tenants' rows; returns each tenant's item id."""
    ids: dict[str, str] = {}
    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant, projects=("main",))
        rows = _rows(tenant)
        async with ctx.db.session() as s:
            for key in ("item", "revision", "progress", "sample"):
                s.add(rows[key])
                await s.flush()  # the item before its revision
        ids[tenant] = rows["item"].id
    return ids


def test_database_enums_match_the_contracts() -> None:
    """The checks in the database name exactly the contract's kinds, scopes and states."""
    assert get_args(LibraryKind) == m.LIBRARY_KINDS
    assert get_args(LibraryScope) == ("project", "tenant")
    assert get_args(SampleState) == m.SAMPLE_STATES
    assert m.ONBOARDING_STATE_MAX_BYTES == MAX_ONBOARDING_BYTES
    assert set(TABLE_NAMES) <= set(m.TENANT_TABLES)


async def test_v018_tables_force_rls(ctx: ServerContext) -> None:
    """Every new table has forced row-level security with the tenant and platform policies,
    select/insert/update for the request path and nothing for the gateway: a tenant session
    sees only its own rows (no tenant filter in the query) and cannot reach, change or add
    the other tenant's."""
    ids = await _seed(ctx)
    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        for table in TABLE_NAMES:
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
            assert policies == {"mapi_tenant_isolation", "mapi_platform_access"}, table
            grants = await (
                await conn.execute(
                    "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = %s",
                    (table,),
                )
            ).fetchall()
            assert {p for g, p in grants if g == "mapi_app"} == {
                "SELECT",
                "INSERT",
                "UPDATE",
            }, table
            assert not {p for g, p in grants if g == "mapi_gateway"}, table
    for tenant, other in (("alpha", "beta"), ("beta", "alpha")):
        async with ctx.db.tenant_session(tenant) as s:
            for model in V018_TABLES:
                found = (await s.execute(select(model.tenant_id))).scalars().all()  # type: ignore[attr-defined]
                assert found == [tenant], model
            assert await s.get(m.LibraryItem, ids[other]) is None
            assert await s.get(m.LibraryItemRevision, (ids[other], 1)) is None
            assert await s.get(m.OnboardingProgress, (other, "usr_ana")) is None
            assert await s.get(m.TenantSample, (other, "geo_mmm")) is None
            for stmt in (
                update(m.LibraryItem).where(m.LibraryItem.id == ids[other]).values(archived=True),
                update(m.LibraryItemRevision)
                .where(m.LibraryItemRevision.item_id == ids[other])
                .values(valid=False),
                update(m.OnboardingProgress)
                .where(m.OnboardingProgress.tenant_id == other)
                .values(version=9),
                update(m.TenantSample)
                .where(m.TenantSample.tenant_id == other)
                .values(state="failed"),
            ):
                res = await s.execute(stmt)
                assert res.rowcount == 0  # type: ignore[attr-defined]
    foreign = _rows("beta")
    for key in ("item", "progress", "sample"):
        with pytest.raises(ProgrammingError, match="row-level security"):
            async with ctx.db.tenant_session("alpha") as s:
                s.add(foreign[key])
                await s.flush()
    async with ctx.db.session() as s:  # the platform role sees both tenants, untouched
        archived = (await s.execute(select(m.LibraryItem.archived))).scalars().all()
        assert archived == [False, False]
        states = (await s.execute(select(m.TenantSample.state))).scalars().all()
        assert states == ["fitting", "fitting"]


async def test_the_request_path_never_deletes_and_rows_go_with_their_scope(
    ctx: ServerContext,
) -> None:
    """Items are archived, samples retired and progress reset, never deleted by the request
    path. Revisions go with their item; project items and samples go with their project;
    everything goes with the tenant."""
    ids = await _seed(ctx)
    for model in V018_TABLES:
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(delete(model))
    async with ctx.db.tenant_session("alpha") as s:
        await s.execute(update(m.LibraryItem).values(archived=True))
        await s.execute(update(m.TenantSample).values(state="retired"))
        await s.execute(update(m.OnboardingProgress).values(state={}, version=1))
        s.add(
            m.LibraryItem(
                id=new_id("lib"), tenant_id="alpha", scope="tenant", kind="prep_preset",
                name="weekly-clean", title="Weekly clean-up", created_by="usr_admin",
            )
        )  # fmt: skip
    async with ctx.db.session() as s:
        await s.execute(delete(m.Project).where(m.Project.tenant_id == "alpha"))
    async with ctx.db.session() as s:
        items = (await s.execute(select(m.LibraryItem.tenant_id, m.LibraryItem.scope))).all()
        assert sorted(items) == [("alpha", "tenant"), ("beta", "project")]
        revisions = (await s.execute(select(m.LibraryItemRevision.item_id))).scalars().all()
        assert revisions == [ids["beta"]]
        samples = (await s.execute(select(m.TenantSample.tenant_id))).scalars().all()
        assert samples == ["beta"]
        progress = (await s.execute(select(m.OnboardingProgress.tenant_id))).scalars().all()
        assert sorted(progress) == ["alpha", "beta"]  # per tenant, not per project
        await s.execute(delete(m.Tenant).where(m.Tenant.id == "alpha"))
    async with ctx.db.session() as s:
        for model in V018_TABLES:
            tenants = (await s.execute(select(model.tenant_id))).scalars().all()  # type: ignore[attr-defined]
            assert tenants == ["beta"], model


async def test_v018_checks_back_the_contracts(ctx: ServerContext) -> None:
    """Scope and project agree; kinds, states and project kinds are closed; revisions,
    counters and attempts start at their floor; documents and onboarding state are capped;
    tags are a list and onboarding state an object."""
    await _seed(ctx)
    for stmt in (
        update(m.LibraryItem).values(scope="tenant"),  # a tenant item has no project
        update(m.LibraryItem).values(scope="global"),
        update(m.LibraryItem).values(kind="python_model"),
        update(m.LibraryItem).values(latest_revision=0),
        update(m.LibraryItem).values(tags={"a": 1}),
        update(m.LibraryItemRevision).values(revision=0),
        update(m.LibraryItemRevision).values(document={"note": "x" * 300_000}),
        update(m.OnboardingProgress).values(state={"note": "x" * 9000}),
        update(m.OnboardingProgress).values(state=["took_tour"]),
        update(m.OnboardingProgress).values(version=-1),
        update(m.TenantSample).values(state="deleted"),
        update(m.TenantSample).values(attempts=-1),
        update(m.Project).values(kind="demo"),
    ):
        with pytest.raises(IntegrityError, match="ck_"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(stmt)
    with pytest.raises(IntegrityError, match="ck_library_items_scope_project"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(
                m.LibraryItem(
                    id=new_id("lib"), tenant_id="alpha", project_id=None, scope="project",
                    kind="chart_template", name="x", title="X", created_by="usr_ana",
                )
            )  # fmt: skip
            await s.flush()
    async with ctx.db.tenant_session("alpha") as s:  # just under the caps is fine
        await s.execute(update(m.LibraryItemRevision).values(document={"note": "x" * 299_000}))
        await s.execute(update(m.OnboardingProgress).values(state={"note": "x" * 7000}))
        await s.execute(update(m.Project).values(kind="sample"))
        await s.execute(update(m.TenantSample).values(state="ready", attempts=3))
    async with ctx.db.session() as s:
        kinds = (
            await s.execute(
                select(m.Project.tenant_id, m.Project.kind).where(
                    m.Project.tenant_id.in_(("alpha", "beta"))
                )
            )
        ).all()
        assert sorted(kinds) == [("alpha", "sample"), ("beta", "standard")]


async def test_item_names_are_unique_per_scope_and_revisions_stay_in_their_tenant(
    ctx: ServerContext,
) -> None:
    """A name is unique per tenant, project (or tenant scope) and kind; the same name may
    exist in another project, the tenant scope, another kind or another tenant. A revision
    cannot point at another tenant's item, even for a role that bypasses row-level security."""
    ids = await _seed(ctx)
    await make_tenant(ctx, "alpha", projects=("main", "other"))

    def item(project_id: str | None, kind: str = "model_recipe") -> m.LibraryItem:
        return m.LibraryItem(
            id=new_id("lib"), tenant_id="alpha", project_id=project_id,
            scope="tenant" if project_id is None else "project", kind=kind, name="store-sales",
            title="Store sales", created_by="usr_ana",
        )  # fmt: skip

    async with ctx.db.tenant_session("alpha") as s:
        for added in (item("other"), item(None), item("main", "chart_template")):
            s.add(added)
            await s.flush()
    for project_id in ("main", None):
        with pytest.raises(IntegrityError, match="uq_library_items_name"):
            async with ctx.db.tenant_session("alpha") as s:
                s.add(item(project_id))
                await s.flush()
    with pytest.raises(IntegrityError, match="fk_library_item_revisions_tenant_id_library_items"):
        async with ctx.db.session() as s:
            s.add(
                m.LibraryItemRevision(
                    item_id=ids["alpha"], revision=2, tenant_id="beta", project_id="main",
                    document={}, doc_hash=HASH, valid=True, created_by="usr_x",
                )
            )  # fmt: skip
            await s.flush()


async def test_preview_runs_are_found_by_their_label_index(ctx: ServerContext) -> None:
    """``ix_runs_preview_key`` indexes the ``mapi.preview`` label of render runs only, and the
    sample loop's partial index covers the states it advances."""
    async with ctx.db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname IN ('ix_runs_preview_key', 'ix_tenant_samples_pending')"
                )
            )
        ).all()
    defs: dict[str, str] = {name: definition for name, definition in rows}
    assert "(labels ->> 'mapi.preview'::text)" in defs["ix_runs_preview_key"]
    assert "WHERE ((kind)::text = 'render'::text)" in defs["ix_runs_preview_key"]
    assert "ingesting" in defs["ix_tenant_samples_pending"]
    assert "fitting" in defs["ix_tenant_samples_pending"]


# ------------------------------------------------------------------ migration up and down


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


def _columns(url: str, table: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {r[0] for r in rows}


def _v017_seed(url: str) -> None:
    """What a v0.17 database holds: a tenant, two projects, a render run and a dashboard."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, trust_tier, status) VALUES ('t1', 't1', "
            "'internal', 'active')"
        )
        for project in ("p1", "sample"):
            conn.execute(
                "INSERT INTO projects (tenant_id, id, project_id, name) "
                f"VALUES ('t1', '{project}', '{project}', '{project}')"
            )
        conn.execute(
            "INSERT INTO runs (id, tenant_id, project_id, kind, state, attempt, "
            "resource_class, timeout_s, labels, payload, inputs, checkpointable, "
            "infra_retries, finalize_attempts, finalize, last_event_id) VALUES "
            "('run_1', 't1', 'p1', 'render', 'succeeded', 1, 'S', 60, "
            "'{\"mapi.preview\": \"k1\"}', '{}', '{}', false, 0, 0, '{}', 0)"
        )
        conn.execute(
            "INSERT INTO dashboards (id, tenant_id, project_id, name, draft_spec, draft_etag, "
            "created_by) VALUES ('dsh_1', 't1', 'p1', 'd', '{}', '\"1\"', 'usr_1')"
        )


V018_NEW = {"library_items", "library_item_revisions", "onboarding_progress", "tenant_samples"}


def test_upgrade_keeps_v017_rows_and_downgrade_refuses_with_library_or_live_samples(
    scratch_db: str,
) -> None:
    """0021 upgrades a database with v0.17 rows (every project becomes ``standard``) and steps
    down only once no library item and no sample that is not retired is left, naming the
    counts; onboarding progress goes without asking and the sample project stays as an
    ordinary project. It steps up again cleanly."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    assert script.get_revision("0021_v018_library_onboarding").down_revision == (
        "0020_any_model_dashboards"
    )
    upgrade(scratch_db, "0020_any_model_dashboards")
    _v017_seed(scratch_db)
    upgrade(scratch_db)
    assert _tables(scratch_db) >= V018_NEW
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        kinds = conn.execute("SELECT id, kind FROM projects ORDER BY id").fetchall()
        assert kinds == [("p1", "standard"), ("sample", "standard")]
        conn.execute("UPDATE projects SET kind = 'sample' WHERE id = 'sample'")
        conn.execute(
            "INSERT INTO library_items (id, tenant_id, project_id, scope, kind, name, title, "
            "created_by) VALUES ('lib_1', 't1', 'sample', 'project', 'prep_preset', 'p', 'P', "
            "'system')"
        )
        conn.execute(
            "INSERT INTO library_item_revisions (item_id, revision, tenant_id, project_id, "
            f"document, doc_hash, valid, created_by) VALUES ('lib_1', 1, 't1', 'sample', '{{}}', "
            f"'{HASH}', true, 'system')"
        )
        conn.execute(
            "INSERT INTO tenant_samples (tenant_id, sample_id, project_id, state, created_by) "
            "VALUES ('t1', 'geo_mmm', 'sample', 'ready', 'usr_1')"
        )
        conn.execute(
            "INSERT INTO onboarding_progress (tenant_id, principal_id, state, version) "
            "VALUES ('t1', 'usr_1', '{\"tips_off\": true}', 2)"
        )
        plan = conn.execute(
            "EXPLAIN SELECT id FROM runs WHERE kind = 'render' AND labels ->> 'mapi.preview' = 'k1'"
        ).fetchall()
        assert plan  # the expression and predicate parse as the index declares them
    with pytest.raises(Exception, match=r"1 library items, 1 samples not retired"):
        downgrade(scratch_db, "0020_any_model_dashboards")
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("UPDATE projects SET kind = 'sample_retired' WHERE id = 'sample'")
        conn.execute("UPDATE tenant_samples SET state = 'retired'")
    with pytest.raises(Exception, match=r"1 library items, 0 samples not retired"):
        downgrade(scratch_db, "0020_any_model_dashboards")
    assert _tables(scratch_db) >= V018_NEW
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("DELETE FROM library_items")  # its revision goes with it
    downgrade(scratch_db, "0020_any_model_dashboards")
    assert not V018_NEW & _tables(scratch_db)
    assert "kind" not in _columns(scratch_db, "projects")
    with psycopg.connect(scratch_db) as conn:
        projects = conn.execute("SELECT id FROM projects ORDER BY id").fetchall()
        assert projects == [("p1",), ("sample",)]
        assert conn.execute("SELECT id FROM dashboards").fetchall() == [("dsh_1",)]
        index = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_runs_preview_key'"
        ).fetchall()
        assert index == []
    upgrade(scratch_db)
    assert _tables(scratch_db) >= V018_NEW
    with psycopg.connect(scratch_db) as conn:
        kinds = conn.execute("SELECT DISTINCT kind FROM projects").fetchall()
        assert kinds == [("standard",)]
