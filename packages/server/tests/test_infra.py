"""Migrations, the procrastinate finalize queue, the CLI and the import contract."""

from __future__ import annotations

import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from mapi_server.__main__ import main
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.db.migrate import alembic_config, downgrade, upgrade
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.queue import ProcrastinateQueue
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import queued_fit, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.run import Actor, RunState, RunTrigger
from sqlalchemy import text


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


def test_migrations_round_trip_and_match_the_models(scratch_db: str) -> None:
    upgrade(scratch_db)
    tables = _tables(scratch_db)
    assert {"runs", "run_attempts", "run_events", "catalogs", "idempotency_keys"} <= tables
    assert "procrastinate_jobs" in tables
    command.check(alembic_config(scratch_db))  # models and migrations agree
    downgrade(scratch_db, "base")
    assert _tables(scratch_db) <= {"alembic_version"}
    upgrade(scratch_db)
    assert "runs" in _tables(scratch_db)


M3_CHAIN = ("0007_guided", "0008_endpoints_webhooks", "0009_plots_embeds", "0010_trust_lifecycle")


def test_m3_migrations_are_linear_and_step_down_and_up(scratch_db: str) -> None:
    """The four M3 revisions chain off 0006 in order, and each steps down and up cleanly."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    (head,) = script.get_heads()  # M3.5 revisions chain on top of the M3 ones
    assert M3_CHAIN[-1] in {r.revision for r in script.iterate_revisions(head, "base")}
    down = "0006_executor_disable"
    for rev in M3_CHAIN:
        assert script.get_revision(rev).down_revision == down
        down = rev
    upgrade(scratch_db)
    m3_tables = {"endpoints", "webhooks", "saved_plots", "plot_embeds", "tenant_deletions"}
    assert m3_tables <= _tables(scratch_db)
    for target in reversed(("0006_executor_disable", *M3_CHAIN[:-1])):
        downgrade(scratch_db, target)
    assert not m3_tables & _tables(scratch_db)
    for rev in M3_CHAIN:
        upgrade(scratch_db, rev)
    assert m3_tables <= _tables(scratch_db)
    upgrade(scratch_db)
    command.check(alembic_config(scratch_db))


def test_governance_migration_steps_down_and_up(scratch_db: str) -> None:
    """0012_governance adds three RLS tables and the memberships' capabilities, and steps
    down to (and up from) the revision before it cleanly."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    before = script.get_revision("0012_governance").down_revision
    assert isinstance(before, str)
    upgrade(scratch_db)
    gov = {"governance_policies", "model_validations", "validation_approvals"}
    assert gov <= _tables(scratch_db)
    downgrade(scratch_db, before)
    assert not gov & _tables(scratch_db)
    upgrade(scratch_db)
    assert gov <= _tables(scratch_db)
    command.check(alembic_config(scratch_db))


def test_tenancy_migration_adopts_v02_rows_and_enables_rls(scratch_db: str) -> None:
    """v0.2 rows carry (tenant, project) ids, possibly without tenant/project rows of their
    own; 0003 creates them, back-fills run priorities and turns on forced RLS."""
    upgrade(scratch_db, "0002")
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        for run_id, kind in (("run_a", "fit"), ("run_b", "score")):
            conn.execute(
                "INSERT INTO runs (id, tenant_id, project_id, kind, state, attempt, "
                "resource_class, timeout_s, labels, payload, inputs, checkpointable, "
                "infra_retries, finalize_attempts, finalize, last_event_id) VALUES "
                "(%s, 'legacy', 'p1', %s, 'succeeded', 1, 'S', 60, '{}', '{}', '{}', false, "
                "0, 0, '{}', 0)",
                (run_id, kind),
            )
    upgrade(scratch_db)
    with psycopg.connect(scratch_db) as conn:
        tenants = conn.execute("SELECT id, trust_tier, status FROM tenants").fetchall()
        assert tenants == [("legacy", "internal", "active")]
        projects = conn.execute("SELECT tenant_id, id FROM projects").fetchall()
        assert projects == [("legacy", "p1")]
        prio = dict(conn.execute("SELECT id, priority FROM runs").fetchall())
        assert prio == {"run_a": "batch", "run_b": "interactive"}
        rls = conn.execute(
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'runs'"
        ).fetchone()
        assert rls == (True,)
    downgrade(scratch_db, "0002")
    assert "memberships" not in _tables(scratch_db)
    with psycopg.connect(scratch_db) as conn:
        off = conn.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'runs'")
        assert off.fetchone() == (False,)
    upgrade(scratch_db)  # the roles already exist: still idempotent
    assert "memberships" in _tables(scratch_db)


def test_every_table_is_classified_for_row_level_security() -> None:
    """A new table is either a tenant table (with ``tenant_id``; RLS in a migration) or a
    declared platform table: nothing is left unclassified."""
    names = {t.name for t in m.Base.metadata.sorted_tables}
    assert names == set(m.TENANT_TABLES) | set(m.GLOBAL_TABLES) | {"tenants"}
    assert not set(m.TENANT_TABLES) & set(m.GLOBAL_TABLES)
    for table in m.Base.metadata.sorted_tables:
        if table.name in m.TENANT_TABLES:
            assert "tenant_id" in table.columns, table.name


def test_cli_migrate(scratch_db: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mapi_server.services.signing import BundleSigner

    monkeypatch.setenv("DATABASE_URL", scratch_db)
    monkeypatch.setenv("MAPI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MAPI_SIGNING_KEY_PATH", raising=False)
    assert main(["migrate"]) == 0
    assert "runs" in _tables(scratch_db)
    # migrate also prepares the bundle-signing key, once, before api and worker start
    key = tmp_path / "keys" / "bundle-signing-p256.pem"
    assert key.is_file()
    key_id = BundleSigner.from_file(key, create=False).key_id
    assert main(["migrate"]) == 0
    assert BundleSigner.from_file(key, create=False).key_id == key_id
    with pytest.raises(SystemExit):
        main(["bogus"])


def test_cli_help_lists_the_commands() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "mapi_server", "--help"], capture_output=True, text=True, check=True
    )
    for cmd in ("api", "worker", "migrate", "dev"):
        assert cmd in out.stdout


def test_server_never_imports_science_or_plugin_code() -> None:
    code = (
        "import sys, mapi_server.app, mapi_server.orchestrator, mapi_server.executors, "
        "mapi_server.services.finalizer\n"
        "bad = sorted(n for n in sys.modules if n.split('.')[0] in "
        "{'numpy','xarray','pandas','mapi_sdk','mapi_runner','mapi_pymc','mapi_sklearn',"
        "'mapi_steps','pymc','altair','plotly'} or n.startswith('mapi_plugin'))\n"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


async def test_procrastinate_queue_finalizes(ctx: ServerContext) -> None:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge_all()
    await simulate_outputs(ctx, run, att)
    queue = ProcrastinateQueue(ctx.db.url, Finalizer(ctx))
    try:
        await queue.defer_finalize(run_id, 1)
        await queue.defer_finalize(run_id, 1)  # deduplicated while pending
        async with ctx.db.session() as s:
            n = (await s.execute(text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one()
        assert n == 1
        await queue.run_worker(wait=False)
    finally:
        await queue.close()
    async with ctx.db.session() as s:
        done = await load_run(s, None, run_id)
    assert done.state == RunState.SUCCEEDED.value and done.output_model_version_id


async def test_finalize_job_of_a_dead_worker_is_recovered(ctx: ServerContext) -> None:
    """A SIGKILLed worker leaves its job in 'doing' (procrastinate only prunes the worker
    row); the next defer must retry it instead of treating the run as being finalized."""
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge_all()
    await simulate_outputs(ctx, run, att)
    queue = ProcrastinateQueue(ctx.db.url, Finalizer(ctx))
    try:
        await queue.defer_finalize(run_id, 1)
        async with ctx.db.session() as s:  # what a worker killed mid-job leaves behind
            await s.execute(text("UPDATE procrastinate_jobs SET status = 'doing'"))
        for _ in range(2):  # reconciler passes over the FINALIZING run
            await queue.defer_finalize(run_id, 1)
            await queue.run_worker(wait=False)
    finally:
        await queue.close()
    async with ctx.db.session() as s:
        done = await load_run(s, None, run_id)
        jobs = (await s.execute(text("SELECT status FROM procrastinate_jobs"))).scalars().all()
    assert done.state == RunState.SUCCEEDED.value, (done.state, jobs)
    assert "doing" not in jobs
