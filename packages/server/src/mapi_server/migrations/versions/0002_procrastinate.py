"""procrastinate job queue schema (same Postgres as the control plane)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 06:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from procrastinate.schema import SchemaManager

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _raw_execute(sql: str) -> None:
    # Bypass SQLAlchemy's bind-parameter parsing: the procrastinate schema is plain SQL with
    # PL/pgSQL bodies, executed as one multi-statement simple query by psycopg.
    driver_conn = op.get_bind().connection.driver_connection
    if driver_conn is None:  # pragma: no cover - always a live psycopg connection here
        raise RuntimeError("no DBAPI connection available")
    driver_conn.execute(sql)


def upgrade() -> None:
    _raw_execute(SchemaManager.get_schema())


_DROP = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT tgname, tgrelid::regclass AS tbl FROM pg_trigger
             WHERE tgname LIKE 'procrastinate%' AND NOT tgisinternal LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s', r.tgname, r.tbl);
    END LOOP;
    FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = current_schema() AND c.relkind = 'r'
               AND c.relname LIKE 'procrastinate%' LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', r.relname);
    END LOOP;
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate%' LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', r.sig);
    END LOOP;
    FOR r IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname = current_schema() AND t.typname LIKE 'procrastinate%'
               AND (t.typtype = 'e' OR (t.typtype = 'c' AND EXISTS (
                    SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid AND c.relkind = 'c'))) LOOP
        EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', r.typname);
    END LOOP;
END $$;
"""


def downgrade() -> None:
    _raw_execute(_DROP)
