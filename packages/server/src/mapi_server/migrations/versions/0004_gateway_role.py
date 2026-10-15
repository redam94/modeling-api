"""the gateway role: what the internet-facing API and events services may do

Revision ID: 0004_gateway
Revises: 0003_tenancy
Create Date: 2026-10-15 18:00:00

Plan §9 (IDOR row) keeps the role that bypasses row-level security to the dispatcher,
reconciler and finalizer. The API and events services instead get the **gateway** role
(``NOLOGIN``; name from the Alembic option ``mapi.gateway_role``, default ``mapi_gateway``):

* no ``BYPASSRLS``, not a table owner, so ``FORCE ROW LEVEL SECURITY`` binds it;
* ``SELECT`` across tenants only on what sign-in, the operator console and the quota and
  usage counters read (explicit ``FOR SELECT`` policies);
* writes only on identity rows (users, sessions, login transactions, memberships, token
  bookkeeping), operator documents (tenants, the platform project of a tenant, quota
  policies, platform settings, request counters), catalog imports, and **inserts** into the
  audit log (never updates or deletes);
* nothing at all on datasets, specs, runs' writes, model versions, plots or the cost ledger:
  tenant data is written through tenant sessions, pushed run events included.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_gateway"
down_revision: str | None = "0003_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READ = (
    # identity and platform documents
    "users",
    "web_sessions",
    "login_transactions",
    "rate_counters",
    "quota_policies",
    "platform_settings",
    "executor_reports",
    "catalogs",
    # tenant tables read across tenants (sign-in, operator console, quotas, usage)
    "tenants",
    "projects",
    "memberships",
    "api_tokens",
    "runs",
    "run_attempts",
    "cost_ledger",
    "audit_events",
    "uploads",
    "dataset_versions",
)
"""Tables the gateway may read."""

WRITE: dict[str, tuple[str, ...]] = {
    "users": ("INSERT", "UPDATE"),
    "web_sessions": ("INSERT", "UPDATE", "DELETE"),
    "login_transactions": ("INSERT", "UPDATE", "DELETE"),
    "rate_counters": ("INSERT", "UPDATE", "DELETE"),
    "quota_policies": ("INSERT", "UPDATE", "DELETE"),
    "platform_settings": ("INSERT", "UPDATE"),
    "catalogs": ("INSERT",),
    "tenants": ("INSERT", "UPDATE"),
    "projects": ("INSERT",),
    "memberships": ("INSERT", "UPDATE", "DELETE"),
    "api_tokens": ("UPDATE",),
    "audit_events": ("INSERT",),
}
"""Statements the gateway may run, per table."""

RLS_TABLES = (
    "tenants",
    "projects",
    "memberships",
    "api_tokens",
    "runs",
    "run_attempts",
    "cost_ledger",
    "audit_events",
    "uploads",
    "dataset_versions",
)
"""The tables above that have (forced) row-level security (migration 0003)."""

_READ_POLICY = "mapi_gateway_read"


def _role() -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option("mapi.gateway_role") if cfg else None) or "mapi_gateway"
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def _write_policy(command: str) -> str:
    return f"mapi_gateway_{command.lower()}"


def upgrade() -> None:
    gw = _role()
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{gw}') THEN
                CREATE ROLE {gw} NOLOGIN;
            END IF;
            BEGIN
                EXECUTE format('GRANT {gw} TO %I', current_user);
            EXCEPTION WHEN insufficient_privilege OR invalid_grant_operation THEN
                RAISE NOTICE 'cannot grant runtime role membership; use dedicated logins';
            END;
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO {gw}', current_schema());
            EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO {gw}',
                           current_schema());
        END
        $$;
        """
    )
    for table in READ:
        op.execute(f"GRANT SELECT ON {table} TO {gw}")
    for table, commands in WRITE.items():
        op.execute(f"GRANT {', '.join(commands)} ON {table} TO {gw}")
    for table in RLS_TABLES:
        op.execute(f"CREATE POLICY {_READ_POLICY} ON {table} FOR SELECT TO {gw} USING (true)")
        for command in WRITE.get(table, ()):
            clause = "WITH CHECK (true)" if command == "INSERT" else "USING (true)"
            if command == "UPDATE":
                clause = "USING (true) WITH CHECK (true)"
            op.execute(
                f"CREATE POLICY {_write_policy(command)} ON {table} FOR {command} TO {gw} {clause}"
            )


def downgrade() -> None:
    gw = _role()
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_READ_POLICY} ON {table}")
        for command in ("INSERT", "UPDATE", "DELETE"):
            op.execute(f"DROP POLICY IF EXISTS {_write_policy(command)} ON {table}")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{gw}') THEN
                EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM {gw}',
                               current_schema());
                EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM {gw}',
                               current_schema());
                EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {gw}', current_schema());
            END IF;
        END
        $$;
        """
    )
