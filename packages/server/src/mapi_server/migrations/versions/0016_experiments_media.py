"""experiments, saved run views and run media

Revision ID: 0016_experiments_media
Revises: 0015_run_tracking
Create Date: 2026-10-20 18:00:00

v0.14 run comparison and media:

* ``experiments``: named groups of runs of a project (name unique per project, compared on
  ``name_key = lower(btrim(name))``, computed by the server); ``archived_at`` set = archived;
* ``runs.experiment_id``: the run's experiment (one at most). Deleting an experiment keeps
  its runs (``ON DELETE SET NULL``);
* ``run_views``: saved run-table queries (the ``list_runs`` parameters as JSON), shared in
  the project and optionally scoped to an experiment (deleted with it). Names are unique per
  (project, experiment) case-insensitively: a unique expression index on
  ``coalesce(experiment_id, '')``;
* ``run_media``: images and tables a run logged, one row per ``(run, attempt, key, labels,
  step)`` (a media item without a step has ``step = -1``). The verified bytes live in the
  bundles bucket at ``object_name``; ``seq`` comes from ``run_tracking_seq`` (the live
  stream's cursor, like the other tracking tables).

Each new table has forced row-level security with the fail-closed tenant policy of
``0003_tenancy`` plus the platform role's explicit policy. The request path may do anything
on experiments and saved views, and insert/update (never delete) media rows (they go with
their run through the foreign-key cascade); the gateway role (``0005``) gets nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_experiments_media"
down_revision: str | None = "0015_run_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
SEQUENCE = "run_tracking_seq"

APP_GRANTS: dict[str, str] = {
    "experiments": "SELECT, INSERT, UPDATE, DELETE",
    "run_views": "SELECT, INSERT, UPDATE, DELETE",
    "run_media": "SELECT, INSERT, UPDATE",
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


def _scope() -> list[sa.Column[str]]:
    return [
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
    ]


def _stamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    _schema()
    _security()


def _schema() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_stamps(),
        *_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiments")),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "name_key",
            name=op.f("uq_experiments_tenant_id_project_id_name_key"),
        ),
    )
    op.create_index("ix_experiments_scope_id", "experiments", ["tenant_id", "project_id", "id"])
    op.add_column("runs", sa.Column("experiment_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_experiment_id_experiments"),
        "runs",
        "experiments",
        ["experiment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_runs_experiment", "runs", ["tenant_id", "project_id", "experiment_id", "id"]
    )
    op.create_table(
        "run_views",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=200), nullable=False),
        sa.Column("query", _jsonb(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_stamps(),
        *_scope(),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_run_views_experiment_id_experiments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_views")),
    )
    op.create_index(
        "uq_run_views_name",
        "run_views",
        ["tenant_id", "project_id", sa.text("coalesce(experiment_id, '')"), "name_key"],
        unique=True,
    )
    op.create_table(
        "run_media",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("labels_key", sa.String(length=1024), server_default="", nullable=False),
        sa.Column("labels", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("step", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("crc32c", sa.String(length=16), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("caption", sa.String(length=500), nullable=True),
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("object_name", sa.String(length=1024), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column(
            "seq",
            sa.BigInteger(),
            server_default=sa.text(f"nextval('{SEQUENCE}')"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *_scope(),
        sa.CheckConstraint("kind in ('image','table')", name=op.f("ck_run_media_kind")),
        sa.CheckConstraint("source in ('runner','api')", name=op.f("ck_run_media_source")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_media_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_media")),
        sa.UniqueConstraint(
            "run_id",
            "attempt",
            "key",
            "labels_key",
            "step",
            name=op.f("uq_run_media_run_id_attempt_key_labels_key_step"),
        ),
    )
    op.create_index("ix_run_media_series", "run_media", ["run_id", "key", "labels_key", "step"])
    op.create_index("ix_run_media_seq", "run_media", ["run_id", "seq"])


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
    op.drop_index("ix_run_media_seq", table_name="run_media")
    op.drop_index("ix_run_media_series", table_name="run_media")
    op.drop_table("run_media")
    op.drop_index("uq_run_views_name", table_name="run_views")
    op.drop_table("run_views")
    op.drop_index("ix_runs_experiment", table_name="runs")
    op.drop_constraint(op.f("fk_runs_experiment_id_experiments"), "runs", type_="foreignkey")
    op.drop_column("runs", "experiment_id")
    op.drop_index("ix_experiments_scope_id", table_name="experiments")
    op.drop_table("experiments")
