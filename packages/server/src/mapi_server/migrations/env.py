"""Alembic environment (driven programmatically by :mod:`mapi_server.db.migrate`)."""

from __future__ import annotations

from alembic import context
from mapi_server.db.engine import sqlalchemy_url
from mapi_server.db.models import Base
from sqlalchemy import create_engine, pool

config = context.config
target_metadata = Base.metadata


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Ignore procrastinate's own tables (its schema is applied verbatim in 0002)."""
    return not (name or "").startswith(("procrastinate", "idx_procrastinate"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not configured")
    # psycopg 3 in synchronous mode: migrations are a one-off CLI step
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
