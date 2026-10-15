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


def test_every_table_carries_tenant_and_project() -> None:
    for table in m.Base.metadata.sorted_tables:
        cols = set(table.columns.keys())
        if table.name == "tenants":
            continue
        assert {"tenant_id", "project_id"} <= cols, table.name


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
