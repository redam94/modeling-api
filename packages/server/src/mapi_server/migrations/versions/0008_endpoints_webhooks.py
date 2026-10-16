"""endpoints and aliases, webhooks with a transactional outbox (M3)

Revision ID: 0008_endpoints_webhooks
Revises: 0006_executor_disable
Create Date: 2026-10-15 21:00:00

Six tenant tables, each with forced row-level security and the same fail-closed policy as
``0003_tenancy`` (``tenant_id = current_setting('app.tenant_id', false)``) plus the platform
role's explicit policy:

* ``endpoints``: one row per alias and the model version it serves now;
* ``endpoint_revisions``: every promotion and rollback (append-only for the request path:
  ``SELECT, INSERT`` only; rows go with their endpoint through the foreign-key cascade);
* ``endpoint_runs``: which endpoint and revision a score run was submitted through;
* ``webhooks``: subscriptions, with their signing secrets sealed (never stored in clear);
* ``webhook_events``: the outbox - events are written in the transaction that causes them
  (a run finishing, a promotion), so an event exists if and only if its cause committed;
* ``webhook_deliveries``: one delivery chain per event and webhook, with its log.

Least privilege: the request-path role (``mapi.app_role``) gets only what the handlers do on
each table; the gateway role (``mapi.gateway_role``, ``0005``) gets **nothing** on any of
them - every route that reads them is project-scoped and runs in a tenant session, and
deliveries run in the orchestrator (platform role).

The integrator re-chains this revision after the other M3 migrations; it only depends on
``0006_executor_disable``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_endpoints_webhooks"
down_revision: str | None = "0006_executor_disable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"

APP_GRANTS: dict[str, str] = {
    "endpoints": "SELECT, INSERT, UPDATE, DELETE",
    "endpoint_revisions": "SELECT, INSERT",
    "endpoint_runs": "SELECT, INSERT",
    "webhooks": "SELECT, INSERT, UPDATE, DELETE",
    "webhook_events": "SELECT, INSERT",
    "webhook_deliveries": "SELECT, INSERT, UPDATE",
}
"""What the request-path role may do on each new table (row-level security on top)."""

TABLES: tuple[str, ...] = tuple(APP_GRANTS)


def _role(option: str, default: str) -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option(option) if cfg else None) or default
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _scope_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name=op.f(f"fk_{table}_tenant_id_projects"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    _schema()
    _security()


def _schema() -> None:
    op.create_table(
        "endpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("evaluation_policy", _jsonb(), nullable=False),
        sa.Column("schema_snapshot", _jsonb(), nullable=False),
        sa.Column("gate", _jsonb(), nullable=False),
        sa.Column("overridden", sa.Boolean(), nullable=False),
        sa.Column("promoted_by", sa.String(length=128), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_endpoints_revision")),
        _scope_fk("endpoints"),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_endpoints_model_version_id_model_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_endpoints")),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "alias", name=op.f("uq_endpoints_tenant_id_project_id_alias")
        ),
    )
    op.create_table(
        "endpoint_revisions",
        sa.Column("endpoint_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("model_ref", sa.String(length=300), nullable=False),
        sa.Column("previous_model_version_id", sa.String(length=64), nullable=True),
        sa.Column("gate", _jsonb(), nullable=False),
        sa.Column("overridden", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("token_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action in ('promote','rollback')", name=op.f("ck_endpoint_revisions_action")
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name=op.f("fk_endpoint_revisions_endpoint_id_endpoints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("endpoint_id", "revision", name=op.f("pk_endpoint_revisions")),
    )
    op.create_table(
        "endpoint_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("endpoint_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_endpoint_runs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_endpoint_runs")),
    )
    op.create_index(
        "ix_endpoint_runs_endpoint", "endpoint_runs", ["endpoint_id", "created_at"], unique=False
    )
    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("events", _jsonb(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("secret_sealed", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.String(length=16), nullable=False),
        sa.Column("previous_secret_sealed", sa.Text(), nullable=True),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("disabled_reason", sa.String(length=500), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("failure_count >= 0", name=op.f("ck_webhooks_failure_count")),
        _scope_fk("webhooks"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhooks")),
    )
    op.create_index(
        "ix_webhooks_scope", "webhooks", ["tenant_id", "project_id", "active"], unique=False
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("envelope", _jsonb(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_webhook_events_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
    )
    op.create_index(
        "ix_webhook_events_tenant_time", "webhook_events", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("webhook_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("response_excerpt", sa.String(length=1024), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_of", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_webhook_deliveries_attempt")),
        sa.CheckConstraint(
            "status in ('pending','succeeded','retrying','failed','blocked')",
            name=op.f("ck_webhook_deliveries_status"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["webhook_events.id"],
            name=op.f("fk_webhook_deliveries_event_id_webhook_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_id"],
            ["webhooks.id"],
            name=op.f("fk_webhook_deliveries_webhook_id_webhooks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_webhook_deliveries_webhook",
        "webhook_deliveries",
        ["webhook_id", "created_at"],
        unique=False,
    )


def _security() -> None:
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    cond = "tenant_id = current_setting('app.tenant_id', false)"
    for table, privileges in APP_GRANTS.items():
        # the platform role has default privileges on new tables (0003); granted explicitly
        # too, so a database whose default privileges were changed still works
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {system}")
        op.execute(f"GRANT {privileges} ON {table} TO {app}")
        op.execute(f"REVOKE ALL ON {table} FROM {gateway}")
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
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {_PLATFORM_POLICY} ON {table}")
    op.drop_index("ix_webhook_deliveries_webhook", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_events_tenant_time", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index("ix_webhooks_scope", table_name="webhooks")
    op.drop_table("webhooks")
    op.drop_index("ix_endpoint_runs_endpoint", table_name="endpoint_runs")
    op.drop_table("endpoint_runs")
    op.drop_table("endpoint_revisions")
    op.drop_table("endpoints")
