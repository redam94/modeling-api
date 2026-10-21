"""client dashboards: the ``client`` role, dashboards, revisions, grants, readouts, share links

Revision ID: 0018_client_dashboards
Revises: 0017_collections
Create Date: 2026-10-20 22:00:00

v0.16 client dashboards (``mapi_spec.dashboards``):

* ``memberships.role`` gains ``client`` (rank 0: every pre-v0.16 permission denies it). A
  client grant is always project-scoped (``ck_memberships_client_project``): a tenant-wide
  client would be overridden by, or override, the other grants of the tenant;
* ``dashboards``: an analyst-authored dashboard of a project (name unique per project). The
  editable ``draft_spec`` (a :class:`~mapi_spec.dashboards.DashboardSpec`, capped at 64 KiB)
  is separate from the published revision; ``run_as`` is the publisher whose rights the
  dashboard's sandboxed runs use, ``live_inputs = 'paused'`` once those rights are gone;
* ``dashboard_revisions``: immutable published specs (``seq`` per dashboard) with the pinned
  model version and endpoint revision. The request path may insert and read them, never
  change or delete them;
* ``dashboard_grants``: which client users see a published dashboard;
* ``dashboard_readouts``: the readout history (``seq`` per dashboard): capped JSON snapshots
  (``snapshot`` ≤ 300 000 bytes as text) built after their runs finish. An uploaded input is
  kept in object storage; ``input_ref`` points at it, rows never hold it;
* ``dashboard_shares``: tokenised share links. Only the token's HMAC is stored (as embeds),
  with the passcode's scrypt hash, the lockout counters and ``session_version`` (bumped to
  end every session of the link);
* ``dashboard_refit_requests``: an actuals upload's request that the analyst refits the model
  with those numbers. At most one ``open`` request per dashboard (a partial unique index).

Every new table has forced row-level security with the fail-closed tenant policy of
``0003_tenancy`` plus the platform role's explicit policy, and a composite foreign key to
its project (or to its dashboard), so a project's or tenant's deletion takes it along.

The public share-session exchange must find a link before it knows the tenant, so the
gateway role (``0005_gateway_role``) may read exactly the columns that lookup needs (id,
tenant, project, dashboard, token hash, expiry, revocation, session version) of
``dashboard_shares``: never the passcode hash or the counters, and nothing of the other
tables. The rest is read in a session pinned to the link's tenant.

Downgrading refuses while a ``client`` membership or a dashboard exists (an older server
would treat neither correctly).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

revision: str = "0018_client_dashboards"
down_revision: str | None = "0017_collections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
_GATEWAY_POLICY = "mapi_gateway_access_select"
_ROLE_CHECK = "ck_memberships_role"
_CLIENT_CHECK = "ck_memberships_client_project"

APP_GRANTS: dict[str, str] = {
    "dashboards": "SELECT, INSERT, UPDATE, DELETE",
    "dashboard_revisions": "SELECT, INSERT",
    "dashboard_grants": "SELECT, INSERT, UPDATE, DELETE",
    "dashboard_readouts": "SELECT, INSERT, UPDATE, DELETE",
    "dashboard_shares": "SELECT, INSERT, UPDATE, DELETE",
    "dashboard_refit_requests": "SELECT, INSERT, UPDATE",
}
"""What the request-path role may do on each new table (row-level security on top).
Revisions are immutable and refit requests are decided, never erased; both go with their
dashboard through the foreign-key cascade."""

TABLES: tuple[str, ...] = tuple(APP_GRANTS)
GATEWAY_SHARE_COLUMNS = (
    "id",
    "tenant_id",
    "project_id",
    "dashboard_id",
    "token_hash",
    "expires_at",
    "revoked_at",
    "session_version",
)
SPEC_MAX_BYTES = 65_536
SNAPSHOT_MAX_BYTES = 300_000


def _role(option: str, default: str) -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option(option) if cfg else None) or default
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column[datetime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if now else None,
        nullable=nullable,
    )


def _scope(table: str) -> list[SchemaItem]:
    return [
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f(f"fk_{table}_tenant_id_projects"),
            ondelete="CASCADE",
        ),
    ]


def _dashboard_fk(table: str) -> list[SchemaItem]:
    return [
        sa.Column("dashboard_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name=op.f(f"fk_{table}_dashboard_id_dashboards"),
            ondelete="CASCADE",
        ),
    ]


def upgrade() -> None:
    _memberships()
    _schema()
    _security()


def _memberships() -> None:
    op.drop_constraint(op.f(_ROLE_CHECK), "memberships", type_="check")
    op.create_check_constraint(
        op.f(_ROLE_CHECK), "memberships", "role in ('client','viewer','editor','runner','admin')"
    )
    op.create_check_constraint(
        op.f(_CLIENT_CHECK), "memberships", "role <> 'client' OR project_id IS NOT NULL"
    )


def _schema() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("draft_spec", _jsonb(), nullable=False),
        sa.Column("draft_etag", sa.String(length=128), nullable=False),
        sa.Column("published_revision_id", sa.String(length=64), nullable=True),
        sa.Column("run_as", sa.String(length=64), nullable=True),
        sa.Column("live_inputs", sa.String(length=8), server_default="ok", nullable=False),
        sa.Column("problem", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _ts("archived_at", nullable=True),
        *_scope("dashboards"),
        sa.CheckConstraint(
            "status in ('draft','published','archived')", name=op.f("ck_dashboards_status")
        ),
        sa.CheckConstraint(
            "live_inputs in ('ok','paused')", name=op.f("ck_dashboards_live_inputs")
        ),
        sa.CheckConstraint(
            f"octet_length(draft_spec::text) <= {SPEC_MAX_BYTES}",
            name=op.f("ck_dashboards_draft_spec_size"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboards")),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "name", name=op.f("uq_dashboards_tenant_id_project_id_name")
        ),
    )
    op.create_index("ix_dashboards_status", "dashboards", ["tenant_id", "project_id", "status"])
    op.create_table(
        "dashboard_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        *_dashboard_fk("dashboard_revisions"),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("spec", _jsonb(), nullable=False),
        sa.Column("spec_hash", sa.String(length=71), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("model_ref", sa.String(length=512), nullable=False),
        sa.Column("endpoint_alias", sa.String(length=128), nullable=True),
        sa.Column("endpoint_revision", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        *_scope("dashboard_revisions"),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_dashboard_revisions_seq")),
        sa.CheckConstraint(
            f"octet_length(spec::text) <= {SPEC_MAX_BYTES}",
            name=op.f("ck_dashboard_revisions_spec_size"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_revisions")),
        sa.UniqueConstraint(
            "dashboard_id", "seq", name=op.f("uq_dashboard_revisions_dashboard_id_seq")
        ),
    )
    op.create_table(
        "dashboard_grants",
        *_dashboard_fk("dashboard_grants"),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("granted_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        *_scope("dashboard_grants"),
        sa.PrimaryKeyConstraint("dashboard_id", "user_id", name=op.f("pk_dashboard_grants")),
    )
    op.create_index(
        "ix_dashboard_grants_user", "dashboard_grants", ["tenant_id", "project_id", "user_id"]
    )
    op.create_table(
        "dashboard_readouts",
        *_dashboard_fk("dashboard_readouts"),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("actor_kind", sa.String(length=8), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("share_id", sa.String(length=64), nullable=True),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("model_ref", sa.String(length=512), nullable=False),
        sa.Column("endpoint_revision", sa.Integer(), nullable=True),
        sa.Column("spec_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "run_ids",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("workflow_run_id", sa.String(length=64), nullable=True),
        sa.Column("input_ref", _jsonb(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=71), nullable=True),
        sa.Column("scenario", _jsonb(), nullable=True),
        sa.Column("cache_key", sa.String(length=71), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("snapshot", _jsonb(), nullable=True),
        sa.Column("problem", _jsonb(), nullable=True),
        _ts("expires_at", nullable=True),
        _ts("created_at", now=True),
        _ts("ready_at", nullable=True),
        *_scope("dashboard_readouts"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["dashboard_revisions.id"],
            name=op.f("fk_dashboard_readouts_revision_id_dashboard_revisions"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_dashboard_readouts_seq")),
        sa.CheckConstraint(
            "kind in ('baseline','refresh','scenario','actuals')",
            name=op.f("ck_dashboard_readouts_kind"),
        ),
        sa.CheckConstraint(
            "trigger in ('publish','manual','whatif','actuals','workflow','endpoint')",
            name=op.f("ck_dashboard_readouts_trigger"),
        ),
        sa.CheckConstraint(
            "visibility in ('published','private','pending_review','hidden')",
            name=op.f("ck_dashboard_readouts_visibility"),
        ),
        sa.CheckConstraint(
            "status in ('queued','running','materialising','ready','failed')",
            name=op.f("ck_dashboard_readouts_status"),
        ),
        sa.CheckConstraint(
            "actor_kind in ('member','client','share','system')",
            name=op.f("ck_dashboard_readouts_actor_kind"),
        ),
        sa.CheckConstraint(
            f"octet_length(snapshot::text) <= {SNAPSHOT_MAX_BYTES}",
            name=op.f("ck_dashboard_readouts_snapshot_size"),
        ),
        sa.PrimaryKeyConstraint("dashboard_id", "seq", name=op.f("pk_dashboard_readouts")),
    )
    op.create_index(
        "ix_dashboard_readouts_history",
        "dashboard_readouts",
        ["dashboard_id", "visibility", sa.text("seq DESC")],
    )
    op.create_index(
        "ix_dashboard_readouts_pending",
        "dashboard_readouts",
        ["status"],
        postgresql_where=sa.text("status in ('queued','materialising')"),
    )
    op.create_index(
        "ix_dashboard_readouts_cache",
        "dashboard_readouts",
        ["dashboard_id", "cache_key"],
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_index(
        "ix_dashboard_readouts_expiry",
        "dashboard_readouts",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_table(
        "dashboard_shares",
        sa.Column("id", sa.String(length=64), nullable=False),
        *_dashboard_fk("dashboard_shares"),
        sa.Column("token_hash", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=True),
        sa.Column("display", sa.String(length=16), nullable=False),
        sa.Column("passcode_hash", sa.String(length=200), nullable=True),
        sa.Column("session_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "allowed_inputs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("views", sa.BigInteger(), server_default="0", nullable=False),
        _ts("last_viewed_at", nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        _ts("locked_until", nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        _ts("created_at", now=True),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
        *_scope("dashboard_shares"),
        sa.CheckConstraint(
            "session_version >= 1", name=op.f("ck_dashboard_shares_session_version")
        ),
        sa.CheckConstraint(
            "allowed_inputs <@ ARRAY['whatif','actuals']::text[]",
            name=op.f("ck_dashboard_shares_allowed_inputs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_shares")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_dashboard_shares_token_hash")),
    )
    op.create_index(
        "ix_dashboard_shares_dashboard",
        "dashboard_shares",
        ["tenant_id", "project_id", "dashboard_id"],
    )
    op.create_table(
        "dashboard_refit_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        *_dashboard_fk("dashboard_refit_requests"),
        sa.Column("readout_seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("requested_by_kind", sa.String(length=8), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        _ts("created_at", now=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        _ts("decided_at", nullable=True),
        sa.Column("workflow_run_id", sa.String(length=64), nullable=True),
        sa.Column("dataset_version_id", sa.String(length=128), nullable=True),
        sa.Column("problem", _jsonb(), nullable=True),
        *_scope("dashboard_refit_requests"),
        sa.ForeignKeyConstraint(
            ["dashboard_id", "readout_seq"],
            ["dashboard_readouts.dashboard_id", "dashboard_readouts.seq"],
            name=op.f("fk_dashboard_refit_requests_dashboard_id_dashboard_readouts"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status in ('open','approved','rejected','superseded','failed')",
            name=op.f("ck_dashboard_refit_requests_status"),
        ),
        sa.CheckConstraint(
            "requested_by_kind in ('member','client','share')",
            name=op.f("ck_dashboard_refit_requests_requested_by_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_refit_requests")),
    )
    op.create_index(
        "ix_dashboard_refit_requests_dashboard",
        "dashboard_refit_requests",
        ["dashboard_id", "created_at"],
    )
    op.create_index(
        "uq_dashboard_refit_requests_open",
        "dashboard_refit_requests",
        ["dashboard_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
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
    # the public share-session exchange: find the link by its token hash, then pin a tenant
    # session (the passcode hash and the lockout counters stay tenant-side)
    op.execute(
        f"GRANT SELECT ({', '.join(GATEWAY_SHARE_COLUMNS)}) ON dashboard_shares TO {gateway}"
    )
    op.execute(
        f"CREATE POLICY {_GATEWAY_POLICY} ON dashboard_shares AS PERMISSIVE FOR SELECT "
        f"TO {gateway} USING (true)"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM memberships WHERE role = 'client') "
        "OR EXISTS (SELECT 1 FROM dashboards) "
        "THEN RAISE EXCEPTION 'client memberships or dashboards exist: remove them before "
        "downgrading below 0018_client_dashboards'; END IF; END $$"
    )
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    op.execute(f"DROP POLICY IF EXISTS {_GATEWAY_POLICY} ON dashboard_shares")
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {_PLATFORM_POLICY} ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM {app}, {system}, {gateway}")
    op.drop_index("uq_dashboard_refit_requests_open", table_name="dashboard_refit_requests")
    op.drop_index("ix_dashboard_refit_requests_dashboard", table_name="dashboard_refit_requests")
    op.drop_table("dashboard_refit_requests")
    op.drop_index("ix_dashboard_shares_dashboard", table_name="dashboard_shares")
    op.drop_table("dashboard_shares")
    op.drop_index("ix_dashboard_readouts_expiry", table_name="dashboard_readouts")
    op.drop_index("ix_dashboard_readouts_cache", table_name="dashboard_readouts")
    op.drop_index("ix_dashboard_readouts_pending", table_name="dashboard_readouts")
    op.drop_index("ix_dashboard_readouts_history", table_name="dashboard_readouts")
    op.drop_table("dashboard_readouts")
    op.drop_index("ix_dashboard_grants_user", table_name="dashboard_grants")
    op.drop_table("dashboard_grants")
    op.drop_table("dashboard_revisions")
    op.drop_index("ix_dashboards_status", table_name="dashboards")
    op.drop_table("dashboards")
    op.drop_constraint(op.f(_CLIENT_CHECK), "memberships", type_="check")
    op.drop_constraint(op.f(_ROLE_CHECK), "memberships", type_="check")
    op.create_check_constraint(
        op.f(_ROLE_CHECK), "memberships", "role in ('viewer','editor','runner','admin')"
    )
