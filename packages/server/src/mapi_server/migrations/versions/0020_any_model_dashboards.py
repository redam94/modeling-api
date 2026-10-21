"""dashboards for any model type: new readout kinds and inputs, larger snapshots, assets

Revision ID: 0020_any_model_dashboards
Revises: 0019_dashboard_hardening
Create Date: 2026-10-21 12:00:00

v0.17 layout 2 dashboards (``mapi_spec.dashboard_v2``):

* ``dashboard_readouts.kind`` gains ``prediction`` (scenario rows) and ``batch`` (a CSV of
  new cases); ``trigger`` gains ``scenario_rows`` and ``batch``;
* ``dashboard_shares.allowed_inputs`` may also hold ``scenario_rows`` and ``batch``;
* the readout snapshot cap goes from 300 000 to 400 000 bytes as text (a layout 2 snapshot
  is at most 384 KiB; layout 1 snapshots keep their lower contract cap);
* ``dashboard_assets``: the pictures of a published revision (a model-version plot that a
  render run turned into a PNG of at most 512 KiB). The row points at the object in storage
  (``bucket``, ``name``, ``generation``), rows never hold the bytes. Forced row-level
  security, the tenant and platform policies of ``0018`` and a cascade from its dashboard
  and revision. The request path may insert, read and update an asset (the render finishes
  it), never delete one; the gateway role gets nothing (share reads use a tenant-pinned
  session, as readouts do).

``dashboards.problem`` is a free string (no check), so ``upgrade_needs_review`` (the
automatic layout 1 → 2 upgrade) needs no schema change.

Downgrading refuses, with the counts, while a readout uses a new kind or trigger, a share
link allows a new input, a snapshot is above the old cap or an asset exists (an older
server would treat none of them correctly); then it restores the ``0018`` checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0020_any_model_dashboards"
down_revision: str | None = "0019_dashboard_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
TABLE = "dashboard_assets"
APP_PRIVILEGES = "SELECT, INSERT, UPDATE"
ASSET_MAX_BYTES = 524_288
SNAPSHOT_MAX_BYTES = 400_000
OLD_SNAPSHOT_MAX_BYTES = 300_000

KINDS = ("baseline", "refresh", "scenario", "actuals", "prediction", "batch")
OLD_KINDS = ("baseline", "refresh", "scenario", "actuals")
TRIGGERS = (
    "publish", "manual", "whatif", "actuals", "workflow", "endpoint", "scenario_rows", "batch"
)  # fmt: skip
OLD_TRIGGERS = ("publish", "manual", "whatif", "actuals", "workflow", "endpoint")
SHARE_INPUTS = ("whatif", "actuals", "scenario_rows", "batch")
OLD_SHARE_INPUTS = ("whatif", "actuals")


def _role(option: str, default: str) -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option(option) if cfg else None) or default
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def _ts(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column[datetime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if now else None,
        nullable=nullable,
    )


def _checks(kinds: Sequence[str], triggers: Sequence[str], inputs: Sequence[str], cap: int) -> None:
    """Replace the readout kind/trigger/snapshot and share input checks (same names)."""
    for name, table, sql in (
        ("ck_dashboard_readouts_kind", "dashboard_readouts", f"kind in ({_in(kinds)})"),
        ("ck_dashboard_readouts_trigger", "dashboard_readouts", f"trigger in ({_in(triggers)})"),
        (
            "ck_dashboard_readouts_snapshot_size",
            "dashboard_readouts",
            f"octet_length(snapshot::text) <= {cap}",
        ),
        (
            "ck_dashboard_shares_allowed_inputs",
            "dashboard_shares",
            f"allowed_inputs <@ ARRAY[{_in(inputs)}]::text[]",
        ),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, sql)


def upgrade() -> None:
    _checks(KINDS, TRIGGERS, SHARE_INPUTS, SNAPSHOT_MAX_BYTES)
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("dashboard_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("plot_id", sa.String(length=128), nullable=False),
        sa.Column("recipe", sa.String(length=128), nullable=True),
        sa.Column("content_type", sa.String(length=32), server_default="image/png", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("bucket", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=1024), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("problem", sa.String(length=200), nullable=True),
        _ts("created_at", now=True),
        _ts("ready_at", nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_dashboard_assets_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name=op.f("fk_dashboard_assets_dashboard_id_dashboards"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["dashboard_revisions.id"],
            name=op.f("fk_dashboard_assets_revision_id_dashboard_revisions"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "content_type = 'image/png'", name=op.f("ck_dashboard_assets_content_type")
        ),
        sa.CheckConstraint(
            "status in ('queued','ready','failed')", name=op.f("ck_dashboard_assets_status")
        ),
        sa.CheckConstraint(
            f"bytes IS NULL OR (bytes >= 1 AND bytes <= {ASSET_MAX_BYTES})",
            name=op.f("ck_dashboard_assets_bytes"),
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR (bucket IS NOT NULL AND name IS NOT NULL "
            "AND generation IS NOT NULL AND bytes IS NOT NULL)",
            name=op.f("ck_dashboard_assets_ready_object"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_assets")),
        sa.UniqueConstraint(
            "revision_id", "plot_id", name=op.f("uq_dashboard_assets_revision_id_plot_id")
        ),
    )
    op.create_index("ix_dashboard_assets_dashboard", TABLE, ["tenant_id", "dashboard_id"])
    op.create_index(
        "ix_dashboard_assets_queued",
        TABLE,
        ["run_id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    cond = "tenant_id = current_setting('app.tenant_id', false)"
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {system}")
    op.execute(f"GRANT {APP_PRIVILEGES} ON {TABLE} TO {app}")
    op.execute(f"REVOKE ALL ON {TABLE} FROM {gateway}")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {TABLE} AS PERMISSIVE FOR ALL TO PUBLIC "
        f"USING ({cond}) WITH CHECK ({cond})"
    )
    op.execute(
        f"CREATE POLICY {_PLATFORM_POLICY} ON {TABLE} AS PERMISSIVE FOR ALL TO {system} "
        "USING (true) WITH CHECK (true)"
    )


_REFUSE = f"""
DO $$
DECLARE
  readouts bigint;
  shares bigint;
  snapshots bigint;
  assets bigint;
BEGIN
  SELECT count(*) INTO readouts FROM dashboard_readouts
    WHERE kind NOT IN ({_in(OLD_KINDS)}) OR trigger NOT IN ({_in(OLD_TRIGGERS)});
  SELECT count(*) INTO shares FROM dashboard_shares
    WHERE NOT (allowed_inputs <@ ARRAY[{_in(OLD_SHARE_INPUTS)}]::text[]);
  SELECT count(*) INTO snapshots FROM dashboard_readouts
    WHERE octet_length(snapshot::text) > {OLD_SNAPSHOT_MAX_BYTES};
  SELECT count(*) INTO assets FROM {TABLE};
  IF readouts + shares + snapshots + assets > 0 THEN
    RAISE EXCEPTION 'layout 2 dashboard data exists (% readouts with a new kind or trigger, '
      '% share links with a new input, % snapshots above % bytes, % assets): remove it '
      'before downgrading below 0020_any_model_dashboards',
      readouts, shares, snapshots, {OLD_SNAPSHOT_MAX_BYTES}, assets;
  END IF;
END $$
"""


def downgrade() -> None:
    op.execute(_REFUSE)
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {TABLE}")
    op.execute(f"DROP POLICY IF EXISTS {_PLATFORM_POLICY} ON {TABLE}")
    op.execute(f"REVOKE ALL ON {TABLE} FROM {app}, {system}, {gateway}")
    op.drop_index("ix_dashboard_assets_queued", table_name=TABLE)
    op.drop_index("ix_dashboard_assets_dashboard", table_name=TABLE)
    op.drop_table(TABLE)
    _checks(OLD_KINDS, OLD_TRIGGERS, OLD_SHARE_INPUTS, OLD_SNAPSHOT_MAX_BYTES)
