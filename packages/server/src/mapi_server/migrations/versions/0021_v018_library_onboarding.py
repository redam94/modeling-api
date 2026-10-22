"""library items, onboarding progress, tenant samples, sample projects, preview runs

Revision ID: 0021_v018_library_onboarding
Revises: 0020_any_model_dashboards
Create Date: 2026-10-22 09:00:00

v0.18 "make it yours" (``mapi_spec.library``, ``mapi_spec.onboarding``, ``mapi_spec.samples``):

* ``library_items``: an analyst-authored model recipe, prep preset, chart template or
  workflow template. ``scope`` is ``project`` (``project_id`` set) or ``tenant``
  (``project_id`` NULL, readable from every project of the tenant); names are unique per
  tenant, project scope and kind. Items are archived, never deleted by the request path;
* ``library_item_revisions``: the immutable JSON documents of an item (``revision`` per
  item, ``document`` ≤ 300 000 bytes as text), their hash, validity, issues and compiled
  output. The foreign key carries the tenant, so a revision only ever joins an item of its
  own tenant;
* ``onboarding_progress``: one member's tour and checklist state per tenant (JSON ≤ 8 KiB);
* ``tenant_samples``: the sample projects installed in a tenant and where their set-up is
  (a partial index finds the ones the sample loop must advance);
* ``projects.kind``: ``standard`` (default), ``sample`` or ``sample_retired``;
* ``ix_runs_preview_key``: the recipe playground finds a cached preview render run by its
  ``mapi.preview`` label.

Every new table has forced row-level security with the tenant and platform policies of
``0018``; the request path may select, insert and update (never delete) and the gateway role
gets nothing.

Downgrading refuses, with the counts, while a library item or a sample that is not retired
exists; then it drops the tables (onboarding progress is UX state and goes without asking)
and ``projects.kind``, so a retired sample project stays as a standard project.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_v018_library_onboarding"
down_revision: str | None = "0020_any_model_dashboards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
TABLES: tuple[str, ...] = (
    "library_items",
    "library_item_revisions",
    "onboarding_progress",
    "tenant_samples",
)
APP_PRIVILEGES = "SELECT, INSERT, UPDATE"

PROJECT_KINDS = ("standard", "sample", "sample_retired")
LIBRARY_KINDS = ("model_recipe", "prep_preset", "chart_template", "workflow_template")
SAMPLE_STATES = ("provisioning", "ingesting", "fitting", "ready", "failed", "retired")
REVISION_MAX_BYTES = 300_000
ONBOARDING_MAX_BYTES = 8192


def _role(option: str, default: str) -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option(option) if cfg else None) or default
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column[datetime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if now else None,
        nullable=nullable,
    )


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name=op.f(f"fk_{table}_tenant_id_projects"),
        ondelete="CASCADE",
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.id"],
        name=op.f(f"fk_{table}_tenant_id_tenants"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    _projects()
    _library()
    _onboarding()
    _samples()
    op.create_index(
        "ix_runs_preview_key",
        "runs",
        [sa.literal_column("(labels ->> 'mapi.preview')")],
        postgresql_where=sa.text("kind = 'render'"),
    )
    _security()


def _projects() -> None:
    op.add_column(
        "projects",
        sa.Column("kind", sa.String(length=16), server_default="standard", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_projects_kind"), "projects", f"kind in ({_in(PROJECT_KINDS)})"
    )


def _library() -> None:
    op.create_table(
        "library_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.String(length=300), server_default="", nullable=False),
        sa.Column("tags", _jsonb(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("latest_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _tenant_fk("library_items"),
        _project_fk("library_items"),
        sa.CheckConstraint("scope in ('project','tenant')", name=op.f("ck_library_items_scope")),
        sa.CheckConstraint(
            "(scope = 'tenant') = (project_id IS NULL)",
            name=op.f("ck_library_items_scope_project"),
        ),
        sa.CheckConstraint(f"kind in ({_in(LIBRARY_KINDS)})", name=op.f("ck_library_items_kind")),
        sa.CheckConstraint("latest_revision >= 1", name=op.f("ck_library_items_latest_revision")),
        sa.CheckConstraint(
            "jsonb_typeof(tags) = 'array'", name=op.f("ck_library_items_tags_array")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_items")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_library_items_tenant_id_id")),
    )
    op.create_index(
        "uq_library_items_name",
        "library_items",
        ["tenant_id", sa.literal_column("coalesce(project_id, '')"), "kind", "name"],
        unique=True,
    )
    op.create_index("ix_library_items_scope", "library_items", ["tenant_id", "project_id", "kind"])
    op.create_table(
        "library_item_revisions",
        sa.Column("item_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("document", _jsonb(), nullable=False),
        sa.Column("compiled", _jsonb(), nullable=True),
        sa.Column("doc_hash", sa.String(length=71), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("issues", _jsonb(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("catalog_digest", sa.String(length=71), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["library_items.tenant_id", "library_items.id"],
            name=op.f("fk_library_item_revisions_tenant_id_library_items"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_library_item_revisions_revision")),
        sa.CheckConstraint(
            f"octet_length(document::text) <= {REVISION_MAX_BYTES}",
            name=op.f("ck_library_item_revisions_document_size"),
        ),
        sa.PrimaryKeyConstraint("item_id", "revision", name=op.f("pk_library_item_revisions")),
    )


def _onboarding() -> None:
    op.create_table(
        "onboarding_progress",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("state", _jsonb(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        _ts("updated_at", now=True),
        _tenant_fk("onboarding_progress"),
        sa.CheckConstraint(
            "jsonb_typeof(state) = 'object'", name=op.f("ck_onboarding_progress_state_object")
        ),
        sa.CheckConstraint(
            f"pg_column_size(state) <= {ONBOARDING_MAX_BYTES}",
            name=op.f("ck_onboarding_progress_state_size"),
        ),
        sa.CheckConstraint("version >= 0", name=op.f("ck_onboarding_progress_version")),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", name=op.f("pk_onboarding_progress")),
    )


def _samples() -> None:
    op.create_table(
        "tenant_samples",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("sample_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="provisioning", nullable=False),
        sa.Column("dataset_id", sa.String(length=64), nullable=True),
        sa.Column("dataset_version", sa.Integer(), nullable=True),
        sa.Column("spec_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("model_id", sa.String(length=64), nullable=True),
        sa.Column("model_version", sa.Integer(), nullable=True),
        sa.Column("problem", _jsonb(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _tenant_fk("tenant_samples"),
        _project_fk("tenant_samples"),
        sa.CheckConstraint(
            f"state in ({_in(SAMPLE_STATES)})", name=op.f("ck_tenant_samples_state")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_tenant_samples_attempts")),
        sa.PrimaryKeyConstraint("tenant_id", "sample_id", name=op.f("pk_tenant_samples")),
    )
    op.create_index(
        "ix_tenant_samples_pending",
        "tenant_samples",
        ["state"],
        postgresql_where=sa.text("state in ('ingesting','fitting')"),
    )


def _security() -> None:
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    cond = "tenant_id = current_setting('app.tenant_id', false)"
    for table in TABLES:
        # the platform role has default privileges on new tables (0003); granted explicitly
        # too, so a database whose default privileges were changed still works
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {system}")
        op.execute(f"GRANT {APP_PRIVILEGES} ON {table} TO {app}")
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


_REFUSE = """
DO $$
DECLARE
  items bigint;
  samples bigint;
BEGIN
  SELECT count(*) INTO items FROM library_items;
  SELECT count(*) INTO samples FROM tenant_samples WHERE state <> 'retired';
  IF items + samples > 0 THEN
    RAISE EXCEPTION 'v0.18 library or sample data exists (% library items, % samples not '
      'retired): remove the items and retire the samples before downgrading below '
      '0021_v018_library_onboarding', items, samples;
  END IF;
END $$
"""


def downgrade() -> None:
    op.execute(_REFUSE)
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {_PLATFORM_POLICY} ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM {app}, {system}, {gateway}")
    op.drop_index("ix_runs_preview_key", table_name="runs")
    op.drop_index("ix_tenant_samples_pending", table_name="tenant_samples")
    op.drop_table("tenant_samples")
    op.drop_table("onboarding_progress")
    op.drop_table("library_item_revisions")
    op.drop_index("ix_library_items_scope", table_name="library_items")
    op.drop_index("uq_library_items_name", table_name="library_items")
    op.drop_table("library_items")
    # sample projects stay, as standard projects
    op.drop_constraint(op.f("ck_projects_kind"), "projects", type_="check")
    op.drop_column("projects", "kind")
