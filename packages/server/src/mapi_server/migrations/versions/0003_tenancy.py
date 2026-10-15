"""multi-tenancy: identity, tokens, sessions, audit, cost ledger, quotas; row-level security

Revision ID: 0003_tenancy
Revises: 0002
Create Date: 2026-10-15 12:15:52

Besides the new tables this revision

* moves existing (M1, single-tenant) rows under a tenant: every ``(tenant_id, project_id)``
  that data rows carry gets a ``tenants``/``projects`` row (``default``/``default`` in v0.2);
* creates the two runtime roles (cluster-wide, ``NOLOGIN``; names from the Alembic option
  ``mapi.app_role``/``mapi.system_role``) and grants the migrating login membership so a
  single ``DATABASE_URL`` can ``SET ROLE`` into them;
* enables **and forces** row-level security on every tenant table with a fail-closed policy
  on ``current_setting('app.tenant_id', false)`` (an unset setting is an error, an empty one
  matches nothing), plus an explicit policy for the platform role. The platform role also
  gets ``BYPASSRLS`` where the migrating login is a superuser; the explicit policy keeps it
  working where that attribute cannot be granted (managed Postgres) and for login roles
  that are members of it (``BYPASSRLS`` is not inherited).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_tenancy"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "projects",
    "memberships",
    "api_tokens",
    "datasets",
    "uploads",
    "dataset_versions",
    "specs",
    "spec_revisions",
    "runs",
    "run_attempts",
    "run_progress",
    "run_events",
    "run_logs",
    "models",
    "model_versions",
    "plots",
    "idempotency_keys",
    "audit_events",
    "cost_ledger",
)
APP_READ_ONLY = ("catalogs", "quota_policies", "platform_settings")
_DATA_TABLES = ("datasets", "specs", "runs", "models", "catalogs", "idempotency_keys")
_INTERACTIVE = ("score", "render", "derive", "validate", "describe")
_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"


def _roles() -> tuple[str, str]:
    cfg = op.get_context().config
    app = (cfg.get_main_option("mapi.app_role") if cfg else None) or "mapi_app"
    system = (cfg.get_main_option("mapi.system_role") if cfg else None) or "mapi_system"
    for name in (app, system):
        if not name.replace("_", "").isalnum() or not name[0].isalpha():
            raise ValueError(f"invalid role name {name!r}")
    return app, system


def upgrade() -> None:
    _schema()
    _adopt_existing_rows()
    _security()


def _schema() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("actor_kind", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("token_id", sa.String(length=64), nullable=True),
        sa.Column("operator", sa.Boolean(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=256), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index("ix_audit_events_tenant_ts", "audit_events", ["tenant_id", "ts"], unique=False)
    op.create_index("ix_audit_events_ts", "audit_events", ["ts"], unique=False)
    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("resource_class", sa.String(length=4), nullable=False),
        sa.Column("executor", sa.String(length=64), nullable=True),
        sa.Column("token_id", sa.String(length=64), nullable=True),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("wall_seconds", sa.Float(), nullable=False),
        sa.Column("vcpu_seconds", sa.Float(), nullable=False),
        sa.Column("gib_seconds", sa.Float(), nullable=False),
        sa.Column("price_version", sa.String(length=64), nullable=False),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.CheckConstraint("kind in ('actual','adjustment')", name=op.f("ck_cost_ledger_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_ledger")),
    )
    op.create_index(
        "ix_cost_ledger_project_time",
        "cost_ledger",
        ["tenant_id", "project_id", "recorded_at"],
        unique=False,
    )
    op.create_index(
        "ix_cost_ledger_tenant_time", "cost_ledger", ["tenant_id", "recorded_at"], unique=False
    )
    op.create_index("ix_cost_ledger_time", "cost_ledger", ["recorded_at"], unique=False)
    op.create_index(
        "ix_cost_ledger_token_time", "cost_ledger", ["token_id", "recorded_at"], unique=False
    )
    op.create_index(
        "uq_cost_ledger_actual",
        "cost_ledger",
        ["run_id", "attempt"],
        unique=True,
        postgresql_where=sa.text("kind = 'actual'"),
    )
    op.create_table(
        "executor_reports",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("caps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("submits_last_minute", sa.Integer(), nullable=False),
        sa.Column("submit_rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("throttled_last_hour", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_by", sa.String(length=128), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_executor_reports")),
    )
    op.create_table(
        "login_transactions",
        sa.Column("id", sa.String(length=71), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("return_to", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_transactions")),
    )
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_platform_settings")),
    )
    op.create_table(
        "quota_policies",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "scope in ('token','tenant','global')", name=op.f("ck_quota_policies_scope")
        ),
        sa.PrimaryKeyConstraint("scope", "subject_id", name=op.f("pk_quota_policies")),
    )
    op.create_table(
        "rate_counters",
        sa.Column("subject", sa.String(length=96), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("subject", "window_start", name=op.f("pk_rate_counters")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("operator", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('active','invited','disabled')", name=op.f("ck_users_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("issuer", "subject", name=op.f("uq_users_issuer_subject")),
    )
    op.create_index(
        "uq_users_invited_email",
        "users",
        [sa.literal_column("lower(email)")],
        unique=True,
        postgresql_where=sa.text("issuer IS NULL"),
    )
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.String(length=71), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_web_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_sessions")),
    )
    op.create_index("ix_web_sessions_user", "web_sessions", ["user_id"], unique=False)
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("secret_hash", sa.String(length=80), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("vcpu_seconds_budget", sa.Float(), nullable=True),
        sa.Column("budget_period", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "budget_period in ('day','month')", name=op.f("ck_api_tokens_budget_period")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_api_tokens_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_tokens_prefix")),
    )
    op.create_index(
        "ix_api_tokens_owner", "api_tokens", ["tenant_id", "project_id", "owner_id"], unique=False
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("principal_kind", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "principal_kind in ('user','service_account')",
            name=op.f("ck_memberships_principal_kind"),
        ),
        sa.CheckConstraint(
            "role in ('viewer','editor','runner','admin')", name=op.f("ck_memberships_role")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_memberships_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_memberships_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
    )
    op.create_index("ix_memberships_principal", "memberships", ["principal_id"], unique=False)
    op.create_index(
        "uq_memberships_grant",
        "memberships",
        ["tenant_id", sa.literal_column("coalesce(project_id, '')"), "principal_id"],
        unique=True,
    )
    op.add_column(
        "runs", sa.Column("priority", sa.String(length=16), server_default="batch", nullable=False)
    )
    op.add_column("runs", sa.Column("task_count", sa.Integer(), server_default="1", nullable=False))
    op.add_column("runs", sa.Column("token_id", sa.String(length=64), nullable=True))
    op.add_column("runs", sa.Column("created_by", sa.String(length=64), nullable=True))
    op.add_column(
        "runs",
        sa.Column("untrusted_code", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "runs", sa.Column("estimate", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_index("ix_runs_tenant_state", "runs", ["tenant_id", "state"], unique=False)
    op.add_column(
        "tenants",
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
    )
    op.add_column("tenants", sa.Column("status_reason", sa.String(length=500), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("data_residency", sa.String(length=32), server_default="us", nullable=False),
    )
    op.add_column("tenants", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_tenants_status"), "tenants", "status in ('active','suspended','deleting')"
    )
    op.create_check_constraint(
        op.f("ck_runs_priority"), "runs", "priority in ('interactive','batch')"
    )
    op.create_check_constraint(op.f("ck_runs_task_count"), "runs", "task_count >= 1")


def _residency() -> str:
    """The deployment's default residency (``mapi.default_residency``, set by
    ``mapi-server migrate`` from ``MAPI_DEFAULT_RESIDENCY`` or the Cloud Run region)."""
    cfg = op.get_context().config
    value = (cfg.get_main_option("mapi.default_residency") if cfg else None) or "us"
    if not re.fullmatch(r"(?:[a-z]{2,6}|[a-z]+-[a-z]+[0-9]{1,2})", value):
        raise ValueError(f"invalid residency {value!r}")
    return value


def _adopt_existing_rows() -> None:
    """Give every (tenant, project) pair that v0.2 rows carry a tenant and a project row."""
    pairs = " UNION ".join(f"SELECT tenant_id, project_id FROM {t}" for t in _DATA_TABLES)
    op.execute(
        f"""
        INSERT INTO tenants (id, name, trust_tier, status, data_residency)
        SELECT DISTINCT p.tenant_id, initcap(p.tenant_id), 'internal', 'active',
               '{_residency()}'
        FROM ({pairs}) AS p
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO projects (tenant_id, id, project_id, name)
        SELECT DISTINCT p.tenant_id, p.project_id, p.project_id,
               initcap(p.project_id) || ' project'
        FROM ({pairs}) AS p
        ON CONFLICT (tenant_id, id) DO NOTHING
        """
    )
    kinds = ", ".join(f"'{k}'" for k in _INTERACTIVE)
    op.execute(f"UPDATE runs SET priority = 'interactive' WHERE kind IN ({kinds})")


def _security() -> None:
    app, system = _roles()
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app}') THEN
                CREATE ROLE {app} NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{system}') THEN
                CREATE ROLE {system} NOLOGIN;
            END IF;
            IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
                EXECUTE 'ALTER ROLE {system} BYPASSRLS';
            END IF;
            BEGIN
                EXECUTE format('GRANT {app} TO %I', current_user);
                EXECUTE format('GRANT {system} TO %I', current_user);
            EXCEPTION WHEN insufficient_privilege OR invalid_grant_operation THEN
                RAISE NOTICE 'cannot grant runtime role membership; use dedicated logins';
            END;
        END
        $$;
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO {app}, {system}', current_schema());
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES '
                'ON ALL TABLES IN SCHEMA %I TO {system}', current_schema());
            EXECUTE format(
                'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO {system}',
                current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE, '
                'TRUNCATE, REFERENCES ON TABLES TO {system}', current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT, UPDATE '
                'ON SEQUENCES TO {system}', current_schema());
        END
        $$;
        """
    )
    for table in ("tenants", *TENANT_TABLES):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {app}")
    for table in APP_READ_ONLY:
        op.execute(f"GRANT SELECT ON {table} TO {app}")
    rules = {"tenants": "id"} | {t: "tenant_id" for t in TENANT_TABLES}
    for table, column in rules.items():
        cond = f"{column} = current_setting('app.tenant_id', false)"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {_POLICY} ON {table} AS PERMISSIVE FOR ALL TO PUBLIC "
            f"USING ({cond}) WITH CHECK ({cond})"
        )
        op.execute(
            f"CREATE POLICY {_PLATFORM_POLICY} ON {table} AS PERMISSIVE FOR ALL TO {system} "
            "USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    app, system = _roles()
    for table in ("tenants", *TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {_PLATFORM_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM {app}, {system}',
                           current_schema());
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM {app}, {system}',
                           current_schema());
            EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {app}, {system}', current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON TABLES FROM {system}',
                current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM {system}',
                current_schema());
        END
        $$;
        """
    )
    # the roles are cluster-wide (other databases may use them): they are not dropped
    op.drop_constraint(op.f("ck_runs_task_count"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_priority"), "runs", type_="check")
    op.drop_constraint(op.f("ck_tenants_status"), "tenants", type_="check")
    op.drop_column("tenants", "updated_at")
    op.drop_column("tenants", "data_residency")
    op.drop_column("tenants", "status_reason")
    op.drop_column("tenants", "status")
    op.drop_index("ix_runs_tenant_state", table_name="runs")
    op.drop_column("runs", "estimate")
    op.drop_column("runs", "untrusted_code")
    op.drop_column("runs", "created_by")
    op.drop_column("runs", "token_id")
    op.drop_column("runs", "task_count")
    op.drop_column("runs", "priority")
    op.drop_index("uq_memberships_grant", table_name="memberships")
    op.drop_index("ix_memberships_principal", table_name="memberships")
    op.drop_table("memberships")
    op.drop_index("ix_api_tokens_owner", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_index("ix_web_sessions_user", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_index(
        "uq_users_invited_email", table_name="users", postgresql_where=sa.text("issuer IS NULL")
    )
    op.drop_table("users")
    op.drop_table("rate_counters")
    op.drop_table("quota_policies")
    op.drop_table("platform_settings")
    op.drop_table("login_transactions")
    op.drop_table("executor_reports")
    op.drop_index(
        "uq_cost_ledger_actual",
        table_name="cost_ledger",
        postgresql_where=sa.text("kind = 'actual'"),
    )
    op.drop_index("ix_cost_ledger_token_time", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_time", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_tenant_time", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_project_time", table_name="cost_ledger")
    op.drop_table("cost_ledger")
    op.drop_index("ix_audit_events_ts", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_ts", table_name="audit_events")
    op.drop_table("audit_events")
    # ### end Alembic commands ###
