"""audit events and cost ledger entries are append-only

Revision ID: 0004_append_only_records
Revises: 0003_tenancy
Create Date: 2026-10-15 18:00:00

0003 granted the request-path role ``SELECT, INSERT, UPDATE, DELETE`` on every tenant table,
``audit_events`` and ``cost_ledger`` included, and the platform role everything. A bug or an
injection on either path could then rewrite or erase a tenant's audit trail and billing
history. This revision

* revokes ``UPDATE``/``DELETE`` (and the platform role's ``TRUNCATE``) on both tables from
  both runtime roles: they may only ``INSERT`` and ``SELECT``;
* adds a ``BEFORE UPDATE OR DELETE`` trigger that refuses any change of an existing row,
  whoever the caller is (the table owner must drop the trigger on purpose, for example for
  a tenant deletion or a retention purge).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_append_only_records"
down_revision: str | None = "0003_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("audit_events", "cost_ledger")
_FUNCTION = "mapi_append_only"


def _roles() -> tuple[str, str]:
    cfg = op.get_context().config
    app = (cfg.get_main_option("mapi.app_role") if cfg else None) or "mapi_app"
    system = (cfg.get_main_option("mapi.system_role") if cfg else None) or "mapi_system"
    for name in (app, system):
        if not name.replace("_", "").isalnum() or not name[0].isalpha():
            raise ValueError(f"invalid role name {name!r}")
    return app, system


def upgrade() -> None:
    app, system = _roles()
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: rows cannot be %', TG_TABLE_NAME, lower(TG_OP)
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    for table in TABLES:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {app}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {system}")
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
        )


def downgrade() -> None:
    app, system = _roles()
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {app}")
        op.execute(f"GRANT UPDATE, DELETE, TRUNCATE ON {table} TO {system}")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
