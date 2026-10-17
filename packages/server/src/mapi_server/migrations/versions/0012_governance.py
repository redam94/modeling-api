"""governance: versioned policies, model validations, approvals, the reviewer capability

Revision ID: 0012_governance
Revises: 0011_workflows
Create Date: 2026-10-16 12:00:00

M3.5 model governance (``mapi_spec.governance``). Three tenant tables, each with forced
row-level security and the fail-closed policy of ``0003_tenancy`` (``tenant_id =
current_setting('app.tenant_id', false)``) plus the platform role's explicit policy:

* ``governance_policies``: every version of every policy, ``(id, version)``; tenant-wide
  policies have no project. Requests may insert versions and retire the latest one
  (``UPDATE`` of ``retired_at``/``active``), never delete;
* ``model_validations``: a model version's validation: status, the policies and checks it
  was held to, the check results with their evidence, the transition history and the
  governance run. Requests create and advance them; the finalizer records results;
* ``validation_approvals``: a reviewer's decision per submission round (append-only for
  requests: ``SELECT, INSERT``).

``memberships`` gains ``capabilities`` (JSONB list, default ``[]``): the ``reviewer`` grant
on top of a role. The gateway role (``0005``) gets nothing on the new tables: every route
that reads them runs in a tenant session.

It chains after ``0011_workflows`` (the M3.5 migrations are linear) but depends only on
tables of ``0010_trust_lifecycle`` and earlier.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_governance"
down_revision: str | None = "0011_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"

APP_GRANTS: dict[str, str] = {
    "governance_policies": "SELECT, INSERT, UPDATE",
    "model_validations": "SELECT, INSERT, UPDATE",
    "validation_approvals": "SELECT, INSERT",
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
    op.add_column(
        "memberships",
        sa.Column("capabilities", _jsonb(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.create_table(
        "governance_policies",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("applies_to", _jsonb(), nullable=False),
        sa.Column("checks", _jsonb(), nullable=False),
        sa.Column("require_approval", sa.Boolean(), nullable=False),
        sa.Column("approvals_required", sa.Integer(), nullable=False),
        sa.Column("require_validated_for_promotion", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "scope in ('tenant','project')", name=op.f("ck_governance_policies_scope")
        ),
        sa.CheckConstraint(
            "(scope = 'tenant') = (project_id IS NULL)",
            name=op.f("ck_governance_policies_scope_project"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_governance_policies_version")),
        sa.CheckConstraint(
            "approvals_required between 1 and 3",
            name=op.f("ck_governance_policies_approvals_required"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_governance_policies_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        _scope_fk("governance_policies"),
        sa.PrimaryKeyConstraint("id", "version", name=op.f("pk_governance_policies")),
    )
    op.create_index(
        "ix_governance_policies_scope",
        "governance_policies",
        ["tenant_id", "project_id"],
        unique=False,
    )
    op.create_table(
        "model_validations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("policies", _jsonb(), nullable=False),
        sa.Column("required_checks", _jsonb(), nullable=False),
        sa.Column("results", _jsonb(), nullable=False),
        sa.Column("failing_checks", _jsonb(), nullable=False),
        sa.Column("require_approval", sa.Boolean(), nullable=False),
        sa.Column("approvals_required", sa.Integer(), nullable=False),
        sa.Column("history", _jsonb(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("dataset", sa.String(length=512), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("submitted_by", sa.String(length=128), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status in ('draft','checks_running','checks_passed','checks_failed',"
            "'awaiting_approval','validated','rejected','deprecated')",
            name=op.f("ck_model_validations_status"),
        ),
        _scope_fk("model_validations"),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_model_validations_model_version_id_model_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_validations")),
    )
    op.create_index(
        "ix_model_validations_version",
        "model_validations",
        ["model_version_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_model_validations_status",
        "model_validations",
        ["tenant_id", "project_id", "status"],
        unique=False,
    )
    op.create_index("ix_model_validations_run", "model_validations", ["run_id"], unique=False)
    op.create_table(
        "validation_approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("validation_id", sa.String(length=64), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("approver_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision in ('approve','reject')", name=op.f("ck_validation_approvals_decision")
        ),
        sa.ForeignKeyConstraint(
            ["validation_id"],
            ["model_validations.id"],
            name=op.f("fk_validation_approvals_validation_id_model_validations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_approvals")),
        sa.UniqueConstraint(
            "validation_id",
            "round",
            "approver_id",
            name=op.f("uq_validation_approvals_validation_id_round_approver_id"),
        ),
    )
    op.create_index(
        "ix_validation_approvals_validation",
        "validation_approvals",
        ["validation_id", "round"],
        unique=False,
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
    op.drop_index("ix_validation_approvals_validation", table_name="validation_approvals")
    op.drop_table("validation_approvals")
    op.drop_index("ix_model_validations_run", table_name="model_validations")
    op.drop_index("ix_model_validations_status", table_name="model_validations")
    op.drop_index("ix_model_validations_version", table_name="model_validations")
    op.drop_table("model_validations")
    op.drop_index("ix_governance_policies_scope", table_name="governance_policies")
    op.drop_table("governance_policies")
    op.drop_column("memberships", "capabilities")
