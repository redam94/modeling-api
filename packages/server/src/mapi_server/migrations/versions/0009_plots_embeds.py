"""saved plots and embed links (M3: static rendering and sharing, plan §10.3 items 4-5)

Revision ID: 0009_plots_embeds
Revises: 0006_executor_disable
Create Date: 2026-10-16 09:00:00

Two tenant tables:

* ``saved_plots``: a model version's plot or a user-authored PlotSpec kept under a title
  (``POST /v1/plots``); the sanitized spec is stored and sanitized again on every read;
* ``plot_embeds``: tokenised read-only links to a saved plot. Only the token's HMAC is
  stored (like API tokens), with its expiry, revocation and view counter.

Both get ``FORCE ROW LEVEL SECURITY`` with the fail-closed tenant policy and the platform
policy of ``0003_tenancy``, and the request-path role's privileges.

The public ``GET /v1/embeds/{token}`` must find an embed before it knows the tenant, so the
gateway role (``0005_gateway_role``) may read exactly the columns that lookup needs (id,
tenant, project, token hash, expiry, revocation) of ``plot_embeds``, and nothing of
``saved_plots``: the plot itself is then read in a session pinned to the embed's tenant.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_plots_embeds"
down_revision: str | None = "0006_executor_disable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("saved_plots", "plot_embeds")
_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
_GATEWAY_POLICY = "mapi_gateway_access_select"
GATEWAY_EMBED_COLUMNS = ("id", "tenant_id", "project_id", "token_hash", "expires_at", "revoked_at")


def _role(option: str, default: str) -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option(option) if cfg else None) or default
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def _roles() -> tuple[str, str, str]:
    return (
        _role("mapi.app_role", "mapi_app"),
        _role("mapi.system_role", "mapi_system"),
        _role("mapi.gateway_role", "mapi_gateway"),
    )


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    app, system, gateway = _roles()
    op.create_table(
        "saved_plots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=True),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recipe", sa.String(length=128), nullable=True),
        sa.Column("provenance", sa.String(length=16), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "source_kind in ('model_plot','spec')", name=op.f("ck_saved_plots_source_kind")
        ),
        sa.CheckConstraint(
            "provenance in ('recipe','plugin','author')", name=op.f("ck_saved_plots_provenance")
        ),
        sa.CheckConstraint("kind in ('vega-lite','plotly')", name=op.f("ck_saved_plots_kind")),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_saved_plots_model_version_id_model_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_saved_plots_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_plots")),
    )
    op.create_index("ix_saved_plots_project", "saved_plots", ["tenant_id", "project_id", "id"])
    op.create_table(
        "plot_embeds",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("plot_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("token_hash", sa.String(length=80), nullable=False),
        sa.Column("display", sa.String(length=16), nullable=False),
        sa.Column("allowed_origins", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=False),
        _ts("last_viewed_at", nullable=True),
        sa.Column("render_run_id", sa.String(length=64), nullable=True),
        sa.Column("image", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        _ts("created_at"),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
        sa.ForeignKeyConstraint(
            ["plot_id"],
            ["saved_plots.id"],
            name=op.f("fk_plot_embeds_plot_id_saved_plots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_plot_embeds_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plot_embeds")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_plot_embeds_token_hash")),
    )
    op.create_index("ix_plot_embeds_plot", "plot_embeds", ["tenant_id", "project_id", "plot_id"])

    cond = "tenant_id = current_setting('app.tenant_id', false)"
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {app}")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES ON {table} TO {system}"
        )
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
    # the public embed lookup: find the row by its token hash, then pin a tenant session
    op.execute(f"GRANT SELECT ({', '.join(GATEWAY_EMBED_COLUMNS)}) ON plot_embeds TO {gateway}")
    op.execute(
        f"CREATE POLICY {_GATEWAY_POLICY} ON plot_embeds AS PERMISSIVE FOR SELECT "
        f"TO {gateway} USING (true)"
    )


def downgrade() -> None:
    app, system, gateway = _roles()
    op.execute(f"DROP POLICY IF EXISTS {_GATEWAY_POLICY} ON plot_embeds")
    op.execute(f"REVOKE ALL ON plot_embeds FROM {gateway}")
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {_PLATFORM_POLICY} ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM {app}, {system}")
    op.drop_index("ix_plot_embeds_plot", table_name="plot_embeds")
    op.drop_table("plot_embeds")
    op.drop_index("ix_saved_plots_project", table_name="saved_plots")
    op.drop_table("saved_plots")
