"""Shared fixtures: a session-scoped embedded Postgres (``pgserver``, or ``DATABASE_URL``),
migrated once; a fresh :class:`ServerContext` per test over a truncated database with an
in-memory object store; and an ``httpx`` client on the ASGI app."""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapi_server.app import build_context, create_app, shutdown, startup
from mapi_server.context import ServerContext
from mapi_server.db.migrate import upgrade
from mapi_server.services.signing import BundleSigner
from mapi_server.settings import ObjectStoreKind, Settings
from mapi_server.storage import MemoryObjectStore
from mapi_server_testkit.builders import CATALOG, EXEC_TOKEN

IMAGE_DIGEST = "sha256:" + "ab" * 32

APP_TABLES = (
    "workflow_events",
    "workflow_node_runs",
    "workflow_runs",
    "workflow_schedules",
    "workflow_triggers",
    "workflow_revisions",
    "workflows",
    "webhook_deliveries",
    "webhook_events",
    "webhooks",
    "endpoint_runs",
    "endpoint_revisions",
    "endpoints",
    "audit_events",
    "cost_ledger",
    "quota_policies",
    "platform_settings",
    "rate_counters",
    "executor_reports",
    "web_sessions",
    "login_transactions",
    "api_tokens",
    "memberships",
    "users",
    "plot_embeds",
    "saved_plots",
    "plots",
    "model_versions",
    "models",
    "run_metrics",
    "run_metric_summary",
    "run_params",
    "run_tags",
    "run_logs",
    "run_events",
    "run_progress",
    "run_attempts",
    "runs",
    "spec_revisions",
    "specs",
    "uploads",
    "dataset_versions",
    "datasets",
    "catalogs",
    "idempotency_keys",
    "signups",
    "tenant_verifications",
    "tenant_deletions",
    "tenant_exports",
    "projects",
    "tenants",
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
)


def _with_db(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


@pytest.fixture(scope="session")
def pg_admin_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """``DATABASE_URL`` if set, else an embedded PostgreSQL 16 for the whole session."""
    env = os.environ.get("DATABASE_URL")
    if env:
        yield env
        return
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    try:
        yield srv.get_uri()
    finally:
        srv.cleanup()


@pytest.fixture(scope="session")
def pg_url(pg_admin_url: str) -> Iterator[str]:
    """A fresh, migrated database for this session (dropped afterwards)."""
    name = f"mapi_test_{secrets.token_hex(4)}"
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = _with_db(pg_admin_url, name)
    upgrade(url)
    try:
        yield url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def settings(pg_url: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_url,
        object_store=ObjectStoreKind.MEMORY,
        data_dir=tmp_path,
        image_digest=IMAGE_DIGEST,
        catalog_path=CATALOG,
        gcs_endpoint="http://store.invalid",
        exec_token=EXEC_TOKEN,
        sse_heartbeat_s=0.3,
        sse_poll_s=0.05,
        reconcile_interval_s=0.05,
        dispatch_poll_s=0.05,
        complete_event_grace_s=0.0,
        lost_execution_grace_s=0.0,
        cancel_grace_s=2.0,
    )


def make_store(settings: Settings) -> MemoryObjectStore:
    return MemoryObjectStore(
        base_url=settings.gcs_endpoint, buckets=settings.buckets.all().values()
    )


async def _truncate(ctx: ServerContext) -> None:
    # as the owner (the runtime roles may not RESTART IDENTITY the owner's sequences)
    async with await psycopg.AsyncConnection.connect(ctx.db.url, autocommit=True) as conn:
        await conn.execute(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[ServerContext]:
    store = make_store(settings) if settings.object_store is ObjectStoreKind.MEMORY else None
    context = build_context(settings, store=store, signer=BundleSigner.generate())
    await _truncate(context)
    await startup(context)
    try:
        yield context
    finally:
        await shutdown(context)


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    from mapi_server.executors import docker_available

    if not any(item.get_closest_marker("docker") for item in items):
        return
    if docker_available():
        return
    skip = pytest.mark.skip(reason="the Docker daemon is not available")
    for item in items:
        if item.get_closest_marker("docker"):
            item.add_marker(skip)
