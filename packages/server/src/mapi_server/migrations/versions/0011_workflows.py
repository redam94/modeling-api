"""workflows: documents, revisions, runs, node runs, events, schedules and triggers (M3.5)

Revision ID: 0011_workflows
Revises: 0010_trust_lifecycle
Create Date: 2026-10-16 18:00:00

Seven tenant tables, each with forced row-level security and the fail-closed policy of
``0003_tenancy`` (``tenant_id = current_setting('app.tenant_id', false)``) plus the platform
role's explicit policy:

* ``workflows``: one row per saved workflow (``workflow://project/name``);
* ``workflow_revisions``: immutable documents with their static validation (append-only
  for the request path: ``SELECT, INSERT``);
* ``workflow_runs``: the orchestration state of a ``workflow`` parent run (its ``runs`` row);
* ``workflow_node_runs``: one row per node of a run (status, result, outputs, child runs,
  cache key - the orchestrator reuses rows with equal keys);
* ``workflow_events``: the run's events (SSE ids), written by the orchestrator;
* ``workflow_schedules``: cron schedules (fired by the orchestrator's workflow loop);
* ``workflow_triggers``: "run when this dataset gets a new version", debounced.

Least privilege: the request-path role gets what the handlers do (runs, node runs and
events are written by the orchestrator, the platform role: the request path only reads them
and flags a run for cancellation); the gateway role gets nothing on any of them.

The integrator re-chains this revision after the other M3.5 migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_workflows"
down_revision: str | None = "0010_trust_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"

APP_GRANTS: dict[str, str] = {
    "workflows": "SELECT, INSERT, UPDATE, DELETE",
    "workflow_revisions": "SELECT, INSERT",
    "workflow_runs": "SELECT, INSERT, UPDATE",
    "workflow_node_runs": "SELECT, INSERT",
    "workflow_events": "SELECT, INSERT",
    "workflow_schedules": "SELECT, INSERT, UPDATE, DELETE",
    "workflow_triggers": "SELECT, INSERT, UPDATE, DELETE",
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


def _ts(name: str, *, nullable: bool = True, default: bool = False) -> sa.Column:  # type: ignore[type-arg]
    if default:
        return sa.Column(
            name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        )
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _scope() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
    ]


def upgrade() -> None:
    _schema()
    _security()


def _schema() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=64), nullable=False),
        *_scope(),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.CheckConstraint("latest_revision >= 1", name=op.f("ck_workflows_latest_revision")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_workflows_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "name", name=op.f("uq_workflows_tenant_id_project_id_name")
        ),
    )
    op.create_table(
        "workflow_revisions",
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        *_scope(),
        sa.Column("document", _jsonb(), nullable=False),
        sa.Column("spec_hash", sa.String(length=71), nullable=False),
        sa.Column("validation", _jsonb(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at", default=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_revisions_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workflow_id", "revision", name=op.f("pk_workflow_revisions")),
    )
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        *_scope(),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("trigger_id", sa.String(length=64), nullable=True),
        sa.Column("parent_run_id", sa.String(length=64), nullable=True),
        sa.Column("parent_node_id", sa.String(length=64), nullable=True),
        sa.Column("params", _jsonb(), nullable=False),
        sa.Column("inputs", _jsonb(), nullable=False),
        sa.Column("input_refs", _jsonb(), nullable=False),
        sa.Column("input_identities", _jsonb(), nullable=False),
        sa.Column("decisions", _jsonb(), nullable=False),
        sa.Column("outputs", _jsonb(), nullable=False),
        sa.Column("error", _jsonb(), nullable=True),
        sa.Column("use_cache", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("resource_class", sa.String(length=4), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False),
        _ts("deadline"),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at", default=True),
        _ts("started_at"),
        _ts("finished_at"),
        _ts("updated_at", default=True),
        sa.CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelling','cancelled')",
            name=op.f("ck_workflow_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_workflow_runs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_runs_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_workflow_runs")),
    )
    op.create_index(
        "ix_workflow_runs_workflow", "workflow_runs", ["workflow_id", "created_at"], unique=False
    )
    op.create_index("ix_workflow_runs_active", "workflow_runs", ["status"], unique=False)
    op.create_table(
        "workflow_node_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        *_scope(),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("use", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("child_run_id", sa.String(length=64), nullable=True),
        sa.Column("child_runs", _jsonb(), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=True),
        sa.Column("cache_key", sa.String(length=71), nullable=True),
        sa.Column("cached_from", sa.String(length=64), nullable=True),
        sa.Column("params", _jsonb(), nullable=False),
        sa.Column("result", _jsonb(), nullable=True),
        sa.Column("outputs", _jsonb(), nullable=False),
        sa.Column("skipped_reason", sa.String(length=500), nullable=True),
        sa.Column("error", _jsonb(), nullable=True),
        _ts("started_at"),
        _ts("finished_at"),
        _ts("updated_at", default=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.run_id"],
            name=op.f("fk_workflow_node_runs_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "node_id", name=op.f("pk_workflow_node_runs")),
    )
    op.create_index(
        "ix_workflow_node_runs_cache",
        "workflow_node_runs",
        ["tenant_id", "project_id", "cache_key"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_node_runs_child", "workflow_node_runs", ["child_run_id"], unique=False
    )
    op.create_table(
        "workflow_events",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        *_scope(),
        sa.Column("type", sa.String(length=48), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        _ts("ts", nullable=False),
        sa.Column("data", _jsonb(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.run_id"],
            name=op.f("fk_workflow_events_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "id", name=op.f("pk_workflow_events")),
    )
    op.create_table(
        "workflow_schedules",
        sa.Column("id", sa.String(length=64), nullable=False),
        *_scope(),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("cron", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("params", _jsonb(), nullable=False),
        sa.Column("inputs", _jsonb(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        _ts("next_run_at"),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        _ts("last_run_at"),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_schedules_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_schedules")),
    )
    op.create_index(
        "ix_workflow_schedules_due", "workflow_schedules", ["active", "next_run_at"], unique=False
    )
    op.create_table(
        "workflow_triggers",
        sa.Column("id", sa.String(length=64), nullable=False),
        *_scope(),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=48), nullable=False),
        sa.Column("dataset", sa.String(length=300), nullable=False),
        sa.Column("dataset_name", sa.String(length=128), nullable=False),
        sa.Column("input", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("params", _jsonb(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("debounce_seconds", sa.Integer(), nullable=False),
        sa.Column("pending_version", sa.Integer(), nullable=True),
        _ts("pending_since"),
        _ts("last_fired_at"),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at", default=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_triggers_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_triggers")),
    )
    op.create_index(
        "ix_workflow_triggers_dataset",
        "workflow_triggers",
        ["tenant_id", "project_id", "dataset_name"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_triggers_pending", "workflow_triggers", ["pending_since"], unique=False
    )


def _security() -> None:
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    cond = "tenant_id = current_setting('app.tenant_id', false)"
    for table, privileges in APP_GRANTS.items():
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
    op.drop_index("ix_workflow_triggers_pending", table_name="workflow_triggers")
    op.drop_index("ix_workflow_triggers_dataset", table_name="workflow_triggers")
    op.drop_table("workflow_triggers")
    op.drop_index("ix_workflow_schedules_due", table_name="workflow_schedules")
    op.drop_table("workflow_schedules")
    op.drop_table("workflow_events")
    op.drop_index("ix_workflow_node_runs_child", table_name="workflow_node_runs")
    op.drop_index("ix_workflow_node_runs_cache", table_name="workflow_node_runs")
    op.drop_table("workflow_node_runs")
    op.drop_index("ix_workflow_runs_active", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workflow", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_revisions")
    op.drop_table("workflows")
