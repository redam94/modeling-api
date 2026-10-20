"""run tracking: metrics, metric summaries, params, tags, run names/notes, structured logs

Revision ID: 0015_run_tracking
Revises: 0014_tree_datasets
Create Date: 2026-10-20 12:00:00

v0.11 run tracking (``mapi_spec.tracking``):

* ``runs.name`` / ``runs.note``: a display name and a free-text note (``PATCH /v1/runs/{id}``);
* ``run_logs.level`` / ``run_logs.fields``: the level and structured fields of ``user`` log
  lines (``NULL`` for captured stdout/stderr/system lines);
* ``run_metrics``: every metric point, one row per ``(run, attempt, key, labels, step)``. A
  point without a step is stored with ``step = -1`` (:data:`~mapi_spec.tracking.SCALAR_STEP`)
  so the primary key can hold it;
* ``run_metric_summary``: one row per series ``(run, key, labels)`` of the newest attempt
  that wrote it (count, first/last time, last value and step, min, max), recomputed from
  ``run_metrics`` on every write. Run search filters and sorts on it;
* ``run_params``: write-once run parameters with their text and (when numeric) number;
* ``run_tags``: mutable key/value tags (the last value wins).

The three tables a live stream polls (summaries, params, tags) carry ``seq`` from the shared
sequence ``run_tracking_seq``; every insert or update takes a new value, so "rows with
``seq`` above the stream's cursor" is what changed.

Each new table has forced row-level security with the fail-closed tenant policy of
``0003_tenancy`` plus the platform role's explicit policy. The request path may insert and
update (never delete) metrics, summaries and params, and delete tags; the gateway role
(``0005``) gets nothing. Rows go with their run through the foreign-key cascade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_run_tracking"
down_revision: str | None = "0014_tree_datasets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
SEQUENCE = "run_tracking_seq"

APP_GRANTS: dict[str, str] = {
    "run_metrics": "SELECT, INSERT, UPDATE",
    "run_metric_summary": "SELECT, INSERT, UPDATE",
    "run_params": "SELECT, INSERT, UPDATE",
    "run_tags": "SELECT, INSERT, UPDATE, DELETE",
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


def _run_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["run_id"], ["runs.id"], name=op.f(f"fk_{table}_run_id_runs"), ondelete="CASCADE"
    )


def _seq() -> sa.Column[int]:
    return sa.Column(
        "seq", sa.BigInteger(), server_default=sa.text(f"nextval('{SEQUENCE}')"), nullable=False
    )


def _scope() -> list[sa.Column[str]]:
    return [
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
    ]


def upgrade() -> None:
    _schema()
    _security()


def _schema() -> None:
    op.add_column("runs", sa.Column("name", sa.String(length=200), nullable=True))
    op.add_column("runs", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("run_logs", sa.Column("level", sa.String(length=8), nullable=True))
    op.add_column("run_logs", sa.Column("fields", _jsonb(), nullable=True))
    op.execute(f"CREATE SEQUENCE {SEQUENCE} AS bigint")
    op.create_table(
        "run_metrics",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("labels_key", sa.String(length=1024), server_default="", nullable=False),
        sa.Column("step", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        *_scope(),
        sa.CheckConstraint("source in ('runner','api')", name=op.f("ck_run_metrics_source")),
        _run_fk("run_metrics"),
        sa.PrimaryKeyConstraint(
            "run_id", "attempt", "key", "labels_key", "step", name=op.f("pk_run_metrics")
        ),
    )
    op.create_table(
        "run_metric_summary",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("labels_key", sa.String(length=1024), server_default="", nullable=False),
        sa.Column("labels", _jsonb(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("first_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last", sa.Float(), nullable=False),
        sa.Column("last_step", sa.BigInteger(), nullable=True),
        sa.Column("min", sa.Float(), nullable=False),
        sa.Column("max", sa.Float(), nullable=False),
        _seq(),
        *_scope(),
        _run_fk("run_metric_summary"),
        sa.PrimaryKeyConstraint("run_id", "key", "labels_key", name=op.f("pk_run_metric_summary")),
    )
    op.create_index("ix_run_metric_summary_seq", "run_metric_summary", ["run_id", "seq"])
    op.create_index(
        "ix_run_metric_summary_search",
        "run_metric_summary",
        ["tenant_id", "project_id", "key", "labels_key"],
    )
    op.create_table(
        "run_params",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=250), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        _seq(),
        *_scope(),
        sa.CheckConstraint("source in ('runner','api')", name=op.f("ck_run_params_source")),
        _run_fk("run_params"),
        sa.PrimaryKeyConstraint("run_id", "key", name=op.f("pk_run_params")),
    )
    op.create_index("ix_run_params_seq", "run_params", ["run_id", "seq"])
    op.create_index(
        "ix_run_params_search", "run_params", ["tenant_id", "project_id", "key", "value_num"]
    )
    op.create_table(
        "run_tags",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=1000), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        _seq(),
        *_scope(),
        _run_fk("run_tags"),
        sa.PrimaryKeyConstraint("run_id", "key", name=op.f("pk_run_tags")),
    )
    op.create_index("ix_run_tags_seq", "run_tags", ["run_id", "seq"])
    op.create_index("ix_run_tags_search", "run_tags", ["tenant_id", "project_id", "key", "value"])


def _security() -> None:
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    cond = "tenant_id = current_setting('app.tenant_id', false)"
    # every write takes a new ``seq`` (column default or an explicit nextval in upserts)
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {SEQUENCE} TO {app}, {system}")
    op.execute(f"REVOKE ALL ON SEQUENCE {SEQUENCE} FROM {gateway}")
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
    op.drop_index("ix_run_tags_search", table_name="run_tags")
    op.drop_index("ix_run_tags_seq", table_name="run_tags")
    op.drop_table("run_tags")
    op.drop_index("ix_run_params_search", table_name="run_params")
    op.drop_index("ix_run_params_seq", table_name="run_params")
    op.drop_table("run_params")
    op.drop_index("ix_run_metric_summary_search", table_name="run_metric_summary")
    op.drop_index("ix_run_metric_summary_seq", table_name="run_metric_summary")
    op.drop_table("run_metric_summary")
    op.drop_table("run_metrics")
    op.execute(f"DROP SEQUENCE IF EXISTS {SEQUENCE}")
    op.drop_column("run_logs", "fields")
    op.drop_column("run_logs", "level")
    op.drop_column("runs", "note")
    op.drop_column("runs", "name")
