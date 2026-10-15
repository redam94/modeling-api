"""Database URLs, the two runtime roles, sessions with row-level security, and the
embedded Postgres.

``DATABASE_URL`` may be any ``postgresql://`` URL (unix-socket ``?host=/path`` URLs from
``pgserver`` included). SQLAlchemy gets the ``postgresql+psycopg://`` form, while
procrastinate and the LISTEN connection get the plain libpq form.

**Two roles, two pools** (plan §9, IDOR row):

* :meth:`Database.tenant_session` runs as the request-path role (``mapi_app``), which owns
  no table and is subject to the ``FORCE ROW LEVEL SECURITY`` policies of migration
  ``0003_tenancy``. Every transaction starts with
  ``SELECT set_config('app.tenant_id', <tenant>, true)`` (transaction-local, so a pooled
  connection never carries a previous request's tenant). A transaction without the setting
  fails closed: ``current_setting('app.tenant_id', false)`` raises on a fresh connection
  and is the empty string (matching no row) on a reused one.
* :meth:`Database.session` / :meth:`Database.system_session` run as the platform role
  (``mapi_system``, ``BYPASSRLS``): the dispatcher, reconciler, finalizer, events service,
  authentication lookups and operator endpoints.

Without dedicated logins (``MAPI_DATABASE_APP_URL``/``MAPI_DATABASE_SYSTEM_URL``) both pools
connect with ``DATABASE_URL`` and select their role with the libpq start-up option
``-c role=<role>`` (``RESET ROLE`` then returns to that role, not to the login).
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from mapi_server.settings import Settings

__all__ = [
    "TENANT_SETTING",
    "Database",
    "TenantScopedSession",
    "libpq_url",
    "resolve_database_url",
    "sqlalchemy_url",
    "start_embedded_postgres",
]

TENANT_SETTING: Final = "app.tenant_id"
"""The transaction-local setting every row-level-security policy compares ``tenant_id`` to."""


class TenantScopedSession(Session):
    """Sessions of the request-path role: each transaction is pinned to ``info["tenant_id"]``."""


@event.listens_for(TenantScopedSession, "after_begin")
def _pin_tenant(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    tenant = session.info.get("tenant_id")
    if not tenant:
        return  # fail closed: every policy errors or matches nothing
    connection.execute(
        text("SELECT set_config(:name, :tenant, true)"),
        {"name": TENANT_SETTING, "tenant": str(tenant)},
    )


_EMBEDDED: dict[Path, Any] = {}
_EMBEDDED_LOCK = threading.Lock()


def sqlalchemy_url(url: str) -> str:
    """``postgresql+psycopg://...`` for SQLAlchemy (async psycopg 3 driver)."""
    for prefix in (
        "postgresql+psycopg://",
        "postgresql+asyncpg://",
        "postgresql://",
        "postgres://",
    ):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    raise ValueError("DATABASE_URL must be a postgresql:// URL")


def libpq_url(url: str) -> str:
    """Plain ``postgresql://...`` (psycopg / procrastinate conninfo)."""
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    if not url.startswith("postgresql://"):
        raise ValueError("DATABASE_URL must be a postgresql:// URL")
    return url


def start_embedded_postgres(pg_dir: Path) -> str:
    """Start (or reuse) an embedded PostgreSQL 16 under ``pg_dir`` and return its URL.

    Uses ``pgserver`` (pip-installed binaries; no Docker). The server is shared per data
    directory within this process and stopped when the last user exits.
    """
    try:
        import pgserver  # type: ignore[import-untyped,unused-ignore]
    except ImportError as exc:  # pragma: no cover - dev dependency
        raise RuntimeError(
            "DATABASE_URL is not set and the embedded Postgres (pgserver) is not installed"
        ) from exc
    pg_dir = pg_dir.resolve()
    with _EMBEDDED_LOCK:
        srv = _EMBEDDED.get(pg_dir)
        if srv is None:
            pg_dir.mkdir(parents=True, exist_ok=True)
            srv = pgserver.get_server(  # type: ignore[attr-defined,unused-ignore]
                str(pg_dir), cleanup_mode="stop"
            )
            _EMBEDDED[pg_dir] = srv
        uri: str = srv.get_uri()
        return uri


def resolve_database_url(settings: Settings) -> str:
    """``settings.database_url`` or the embedded server's URL (libpq form)."""
    if settings.database_url:
        return libpq_url(settings.database_url)
    return libpq_url(start_embedded_postgres(settings.pg_dir))


def _engine(url: str, *, role: str | None, pool_size: int, echo: bool) -> AsyncEngine:
    connect_args: dict[str, Any] = {}
    if role is not None:
        connect_args["options"] = f"-c role={role}"
    return create_async_engine(
        sqlalchemy_url(url),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        echo=echo,
        connect_args=connect_args,
    )


class Database:
    """Owns both engines and hands out sessions.

    ``async with db.tenant_session(tenant_id) as s`` for request handlers (row-level
    security), ``async with db.session() as s`` for platform components (bypasses it).
    """

    def __init__(
        self,
        url: str,
        *,
        app_url: str | None = None,
        system_url: str | None = None,
        app_role: str = "mapi_app",
        system_role: str = "mapi_system",
        pool_size: int = 10,
        system_pool_size: int | None = None,
        echo: bool = False,
        session_info: Mapping[str, Any] | None = None,
    ) -> None:
        self.url = libpq_url(url)
        self.app_role = app_role
        self.system_role = system_role
        self.system_engine = _engine(
            libpq_url(system_url) if system_url else self.url,
            role=None if system_url else system_role,
            pool_size=system_pool_size or max(2, pool_size // 2),
            echo=echo,
        )
        self.app_engine = _engine(
            libpq_url(app_url) if app_url else self.url,
            role=None if app_url else app_role,
            pool_size=pool_size,
            echo=echo,
        )
        self.engine: AsyncEngine = self.system_engine
        """The platform-role engine (kept under its v0.2 name)."""
        info = dict(session_info or {})
        self.sessionmaker = async_sessionmaker(
            self.system_engine, expire_on_commit=False, info=dict(info)
        )
        self.tenant_sessionmaker = async_sessionmaker(
            self.app_engine,
            expire_on_commit=False,
            sync_session_class=TenantScopedSession,
            info=dict(info),
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, url: str, *, session_info: Mapping[str, Any] | None = None
    ) -> Database:
        return cls(
            url,
            app_url=settings.database_app_url,
            system_url=settings.database_system_url,
            app_role=settings.db_app_role,
            system_role=settings.db_system_role,
            pool_size=settings.db_pool_size,
            system_pool_size=settings.db_system_pool_size,
            echo=settings.db_echo,
            session_info=session_info,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A **platform-role** session inside one transaction (committed on success, rolled
        back on error). It bypasses row-level security: request handlers use
        :meth:`tenant_session` instead."""
        async with self.sessionmaker() as s, s.begin():
            yield s

    system_session = session

    @asynccontextmanager
    async def tenant_session(self, tenant_id: str) -> AsyncIterator[AsyncSession]:
        """A request-path session: every query sees and writes only ``tenant_id``'s rows."""
        if not tenant_id:
            raise ValueError("a tenant session needs a tenant id")
        async with self.tenant_sessionmaker(info={"tenant_id": tenant_id}) as s, s.begin():
            yield s

    async def dispose(self) -> None:
        await self.app_engine.dispose()
        await self.system_engine.dispose()
