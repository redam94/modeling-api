"""the gateway role: the API's platform lookups without the row-level-security bypass

Revision ID: 0005_gateway_role
Revises: 0004_append_only_records
Create Date: 2026-10-15 19:00:00

Plan §9 (IDOR row) keeps the ``BYPASSRLS`` platform role for the dispatcher, reconciler and
finalizer only. Until now the API and the events service also used it for everything that
runs before a tenant is known (sign-in lookups, memberships, operator metadata, pushed
events), so an authorization slip in one of those handlers was not contained by row-level
security. This revision creates a third runtime role, the **gateway** (cluster-wide,
``NOLOGIN``; name from the Alembic option ``mapi.gateway_role``), for those lookups. It is
neither the table owner nor ``BYPASSRLS``, and gets only what the request path needs:

* identity and platform tables without row-level security (users, sessions, sign-ins, rate
  counters, quota policies, kill switches, executor reports, the catalog cache);
* on tables with row-level security, explicit policies for the gateway: tenants, projects
  and memberships (read and write: tenant administration), API tokens (read, and touch
  ``last_used_at``), audit events (read and append);
* the few metadata columns of ``runs`` and ``cost_ledger`` that global quotas, the spend cap,
  operator executor health and event routing aggregate across tenants (column privileges:
  no payload, inputs, labels, outputs or problem text);
* nothing at all on datasets, uploads, specs, models, plots, attempts, events, logs or
  idempotency keys: their rows are reached through tenant sessions only.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_gateway_role"
down_revision: str | None = "0004_append_only_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_gateway_access"

GLOBAL_GRANTS: dict[str, str] = {
    "users": "SELECT, INSERT, UPDATE",
    "web_sessions": "SELECT, INSERT, UPDATE, DELETE",
    "login_transactions": "SELECT, INSERT, DELETE",
    "rate_counters": "SELECT, INSERT, UPDATE, DELETE",
    "quota_policies": "SELECT, INSERT, UPDATE, DELETE",
    "platform_settings": "SELECT, INSERT, UPDATE",
    "executor_reports": "SELECT",
    "catalogs": "SELECT, INSERT, UPDATE",
}
"""Tables without row-level security."""

RLS_GRANTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tenants": ("SELECT, INSERT, UPDATE", ("SELECT", "INSERT", "UPDATE")),
    "projects": ("SELECT, INSERT", ("SELECT", "INSERT")),
    "memberships": ("SELECT, INSERT, UPDATE, DELETE", ("SELECT", "INSERT", "UPDATE", "DELETE")),
    "api_tokens": ("SELECT, UPDATE (last_used_at)", ("SELECT", "UPDATE")),
    "audit_events": ("SELECT, INSERT", ("SELECT", "INSERT")),
    "runs": (
        "SELECT (id, tenant_id, project_id, state, attempt, estimate, token_id, task_count, "
        "executor, failure_class, finished_at)",
        ("SELECT",),
    ),
    "cost_ledger": (
        "SELECT (id, tenant_id, project_id, run_id, attempt, kind, token_id, vcpu_seconds, "
        "amount_micros, currency, recorded_at)",
        ("SELECT",),
    ),
}
"""Tables with row-level security: privileges, and the commands the gateway policy covers."""


def _gateway() -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option("mapi.gateway_role") if cfg else None) or "mapi_gateway"
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def upgrade() -> None:
    gw = _gateway()
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
                RAISE NOTICE 'cannot grant runtime role membership; use a dedicated login';
            END;
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO {gw}', current_schema());
        END
        $$;
        """
    )
    for table, privileges in GLOBAL_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {gw}")
    for table, (privileges, commands) in RLS_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {gw}")
        for command in commands:
            using = "" if command == "INSERT" else " USING (true)"
            check = " WITH CHECK (true)" if command in ("INSERT", "UPDATE") else ""
            op.execute(
                f"CREATE POLICY {_POLICY}_{command.lower()} ON {table} AS PERMISSIVE "
                f"FOR {command} TO {gw}{using}{check}"
            )


def downgrade() -> None:
    gw = _gateway()
    for table, (_, commands) in RLS_GRANTS.items():
        for command in commands:
            op.execute(f"DROP POLICY IF EXISTS {_POLICY}_{command.lower()} ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM {gw}")
    for table in GLOBAL_GRANTS:
        op.execute(f"REVOKE ALL ON {table} FROM {gw}")
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {gw}', current_schema());
        END
        $$;
        """
    )
    # the role is cluster-wide (other databases may use it): it is not dropped
