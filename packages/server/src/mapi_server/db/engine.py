"""Database URLs, the async engine and session factory, and the embedded Postgres.

``DATABASE_URL`` may be any ``postgresql://`` URL (unix-socket ``?host=/path`` URLs from
``pgserver`` included). SQLAlchemy gets the ``postgresql+psycopg://`` form, while
procrastinate and the LISTEN connection get the plain libpq form.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mapi_server.settings import Settings

__all__ = [
    "Database",
    "libpq_url",
    "resolve_database_url",
    "sqlalchemy_url",
    "start_embedded_postgres",
]

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
            srv = pgserver.get_server(str(pg_dir), cleanup_mode="stop")
            _EMBEDDED[pg_dir] = srv
        uri: str = srv.get_uri()
        return uri


def resolve_database_url(settings: Settings) -> str:
    """``settings.database_url`` or the embedded server's URL (libpq form)."""
    if settings.database_url:
        return libpq_url(settings.database_url)
    return libpq_url(start_embedded_postgres(settings.pg_dir))


class Database:
    """Owns the async engine and hands out sessions (``async with db.session() as s``)."""

    def __init__(self, url: str, *, pool_size: int = 10, echo: bool = False) -> None:
        self.url = libpq_url(url)
        self.engine: AsyncEngine = create_async_engine(
            sqlalchemy_url(self.url),
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_pre_ping=True,
            echo=echo,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session inside one transaction: committed on success, rolled back on error."""
        async with self.sessionmaker() as s, s.begin():
            yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
