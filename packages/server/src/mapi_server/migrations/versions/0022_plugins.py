"""uploaded code: plugins, uploads, versions, describes, approvals, attestations, run overlays

Revision ID: 0022_plugins
Revises: 0021_v018_library_onboarding
Create Date: 2026-10-23 09:00:00

v0.19 "upload your own code" (``mapi_spec.plugins``):

* ``tenant_plugin_settings``: one row per tenant that has claimed a plugin. ``tenant_code``
  is the platform-assigned 6-character code in every uploaded namespace (``t_<code>_<short>``),
  unique across tenants; ``private_use`` lets an uploader use their own kit-passed upload
  before a second person approves it (default on);
* ``user_verifications``: a tenant admin's attestation that an editor may upload
  (``admin_attested``; ``provider`` is reserved). Nobody attests themselves; a revocation
  keeps the row;
* ``plugins``: a claimed namespace (``t_<code>_<short>``, unique on the platform) and its
  short name (unique per tenant);
* ``plugin_uploads``: an upload ticket for one wheel in the quarantine bucket;
* ``plugin_versions``: one uploaded wheel and where it is in the pipeline (``state``), who
  may mount it (``scope``), the bytes' sha256 and object keys, the inspection facts, the
  review and yank facts and the purge schedule. ``(id, upload_sha256)`` is unique so an
  approval can pin the exact bytes it approved;
* ``plugin_describes``: the re-checked describe result of a version for one base image
  digest (exports, kit report, examples, issues). This is the only place describe output
  goes; it never reaches the platform-wide ``catalogs`` table;
* ``plugin_approvals``: append-only approve/reject decisions. The decision names the
  overlay sha256 (composite foreign key into the version), the describe digest and the
  base image digest it saw; the approver is never the uploader;
* ``runs.overlays``: the overlay pins a run mounts (the control plane's truth for lineage).
  A run with pins is always an untrusted-code run (``ck_runs_overlays_untrusted``); a GIN
  index finds the runs pinned to one version for usage counts and security yanks.

Every new table has forced row-level security with the tenant and platform policies of
``0018``/``0021``; the request path may select, insert and update (never delete: a purge
removes the object and keeps the row) and the gateway role gets nothing.

Downgrading refuses, with the counts, while a plugin, a plugin version or a run with
overlays exists; then it drops the tables (settings and attestations go without asking:
nothing can use them without a plugin) and ``runs.overlays``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_plugins"
down_revision: str | None = "0021_v018_library_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
TABLES: tuple[str, ...] = (
    "tenant_plugin_settings",
    "user_verifications",
    "plugins",
    "plugin_uploads",
    "plugin_versions",
    "plugin_describes",
    "plugin_approvals",
)
APP_PRIVILEGES = "SELECT, INSERT, UPDATE"

# mapi_spec.plugins (duplicated: migrations never import the contracts)
TENANT_CODE_RE = "^[a-z2-7]{6}$"
PLUGIN_SHORT_RE = "^[a-z][a-z0-9]{1,15}$"
UPLOAD_NAMESPACE_RE = "^t_[a-z2-7]{6}_[a-z][a-z0-9]{1,15}$"
SHA256_RE = "^sha256:[0-9a-f]{64}$"
WHEEL_FILENAME_RE = "^[A-Za-z0-9._-]+\\.whl$"
VERIFICATION_METHODS = ("admin_attested", "provider")
UPLOAD_STATES = ("pending", "completed", "expired")
VERSION_STATES = (
    "uploaded",
    "inspecting",
    "inspect_failed",
    "describing",
    "kit_failed",
    "kit_passed",
    "awaiting_approval",
    "approved",
    "rejected",
    "yanked",
    "purged",
)
VERSION_SCOPES = ("private", "tenant", "none")
YANK_REASONS = ("withdrawn", "security", "superseded", "rekit_failed")
DECISIONS = ("approve", "reject")
UPLOAD_MAX_BYTES = 20_971_520
INSPECTION_MAX_TEXT = 1_100_000
"""The database's cap on ``plugin_versions.inspection`` as text (the contract's own cap,
``INSPECTION_MAX_BYTES``, is 1 MiB of compact JSON)."""
EXPORTS_MAX_TEXT = 2_200_000
"""The database's cap on ``plugin_describes.exports`` as text (``DESCRIBE_MAX_BYTES`` is
2 MiB for the whole describe result)."""


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


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.id"],
        name=op.f(f"fk_{table}_tenant_id_tenants"),
        ondelete="CASCADE",
    )


def _check(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    _settings()
    _plugins()
    _versions()
    _runs()
    _security()


def _settings() -> None:
    op.create_table(
        "tenant_plugin_settings",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_code", sa.String(length=6), nullable=False),
        sa.Column("private_use", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        _ts("updated_at", now=True),
        _tenant_fk("tenant_plugin_settings"),
        _check("tenant_plugin_settings", "tenant_code", f"tenant_code ~ '{TENANT_CODE_RE}'"),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_plugin_settings")),
        sa.UniqueConstraint("tenant_code", name=op.f("uq_tenant_plugin_settings_tenant_code")),
    )
    op.create_table(
        "user_verifications",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("verified_by", sa.String(length=64), nullable=False),
        _ts("verified_at", now=True),
        _ts("revoked_at", nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=True),
        _tenant_fk("user_verifications"),
        _check("user_verifications", "method", f"method in ({_in(VERIFICATION_METHODS)})"),
        _check("user_verifications", "not_self", "verified_by <> user_id"),
        _check("user_verifications", "revoked", "(revoked_at IS NULL) = (revoked_by IS NULL)"),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name=op.f("pk_user_verifications")),
    )


def _plugins() -> None:
    op.create_table(
        "plugins",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=40), nullable=False),
        sa.Column("short_name", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _tenant_fk("plugins"),
        _check("plugins", "namespace", f"namespace ~ '{UPLOAD_NAMESPACE_RE}'"),
        _check("plugins", "short_name", f"short_name ~ '{PLUGIN_SHORT_RE}'"),
        # the namespace ends with the short name: t_<code>_<short>
        _check("plugins", "namespace_short", "split_part(namespace, '_', 3) = short_name"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugins")),
        sa.UniqueConstraint("namespace", name=op.f("uq_plugins_namespace")),
        sa.UniqueConstraint(
            "tenant_id", "short_name", name=op.f("uq_plugins_tenant_id_short_name")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_plugins_tenant_id_id")),
    )
    op.create_table(
        "plugin_uploads",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        _ts("expires_at"),
        _ts("created_at", now=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plugin_id"],
            ["plugins.tenant_id", "plugins.id"],
            name=op.f("fk_plugin_uploads_tenant_id_plugins"),
            ondelete="CASCADE",
        ),
        _check("plugin_uploads", "state", f"state in ({_in(UPLOAD_STATES)})"),
        _check("plugin_uploads", "filename", f"filename ~ '{WHEEL_FILENAME_RE}'"),
        _check(
            "plugin_uploads",
            "declared_size",
            f"declared_size >= 1 AND declared_size <= {UPLOAD_MAX_BYTES}",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_uploads")),
    )
    op.create_index("ix_plugin_uploads_expiry", "plugin_uploads", ["state", "expires_at"])


def _versions() -> None:
    op.create_table(
        "plugin_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="uploaded", nullable=False),
        sa.Column("scope", sa.String(length=8), server_default="none", nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False),
        _ts("uploaded_at", now=True),
        sa.Column("upload_sha256", sa.String(length=71), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("quarantine_key", sa.String(length=512), nullable=False),
        sa.Column("quarantine_generation", sa.BigInteger(), nullable=False),
        sa.Column("overlay_key", sa.String(length=512), nullable=True),
        sa.Column("overlay_generation", sa.BigInteger(), nullable=True),
        sa.Column("inspect_run_id", sa.String(length=64), nullable=True),
        sa.Column("inspection", _jsonb(), nullable=True),
        sa.Column("sbom", _jsonb(), nullable=True),
        sa.Column("problem", _jsonb(), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
        _ts("requested_at", nullable=True),
        _ts("decided_at", nullable=True),
        _ts("yanked_at", nullable=True),
        sa.Column("yanked_by", sa.String(length=64), nullable=True),
        sa.Column("yank_reason", sa.String(length=16), nullable=True),
        sa.Column("yank_note", sa.Text(), nullable=True),
        _ts("purge_after", nullable=True),
        _ts("purged_at", nullable=True),
        _ts("updated_at", now=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plugin_id"],
            ["plugins.tenant_id", "plugins.id"],
            name=op.f("fk_plugin_versions_tenant_id_plugins"),
            ondelete="CASCADE",
        ),
        _check("plugin_versions", "state", f"state in ({_in(VERSION_STATES)})"),
        _check("plugin_versions", "scope", f"scope in ({_in(VERSION_SCOPES)})"),
        _check("plugin_versions", "tenant_scope", "scope <> 'tenant' OR state = 'approved'"),
        _check("plugin_versions", "upload_sha256", f"upload_sha256 ~ '{SHA256_RE}'"),
        _check(
            "plugin_versions",
            "size_bytes",
            f"size_bytes >= 1 AND size_bytes <= {UPLOAD_MAX_BYTES}",
        ),
        _check(
            "plugin_versions",
            "overlay_object",
            "(overlay_key IS NULL) = (overlay_generation IS NULL)",
        ),
        _check(
            "plugin_versions",
            "inspection_size",
            f"octet_length(inspection::text) <= {INSPECTION_MAX_TEXT}",
        ),
        _check(
            "plugin_versions",
            "yank_reason",
            f"yank_reason IS NULL OR yank_reason in ({_in(YANK_REASONS)})",
        ),
        _check("plugin_versions", "yanked", "(yanked_at IS NULL) = (yank_reason IS NULL)"),
        _check("plugin_versions", "purged", "(state = 'purged') = (purged_at IS NOT NULL)"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_versions")),
        sa.UniqueConstraint(
            "plugin_id", "version", name=op.f("uq_plugin_versions_plugin_id_version")
        ),
        # the targets of the approvals' sha-pinned and the describes' tenant-pinned keys
        sa.UniqueConstraint(
            "id", "upload_sha256", name=op.f("uq_plugin_versions_id_upload_sha256")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_plugin_versions_tenant_id_id")),
    )
    op.create_index("ix_plugin_versions_state", "plugin_versions", ["tenant_id", "state"])
    op.create_index(
        "ix_plugin_versions_purge",
        "plugin_versions",
        ["purge_after"],
        postgresql_where=sa.text("purged_at IS NULL"),
    )
    op.create_table(
        "plugin_describes",
        sa.Column("version_id", sa.String(length=32), nullable=False),
        sa.Column("base_image_digest", sa.String(length=71), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("describe_digest", sa.String(length=71), nullable=False),
        sa.Column("exports", _jsonb(), nullable=False),
        sa.Column("plot_examples", _jsonb(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("kit", _jsonb(), nullable=False),
        sa.Column("issues", _jsonb(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        _ts("created_at", now=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["plugin_versions.tenant_id", "plugin_versions.id"],
            name=op.f("fk_plugin_describes_tenant_id_plugin_versions"),
            ondelete="CASCADE",
        ),
        _check(
            "plugin_describes",
            "exports_size",
            f"octet_length(exports::text) <= {EXPORTS_MAX_TEXT}",
        ),
        _check("plugin_describes", "base_image_digest", f"base_image_digest ~ '{SHA256_RE}'"),
        _check("plugin_describes", "describe_digest", f"describe_digest ~ '{SHA256_RE}'"),
        sa.PrimaryKeyConstraint(
            "version_id", "base_image_digest", name=op.f("pk_plugin_describes")
        ),
    )
    op.create_table(
        "plugin_approvals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=32), nullable=False),
        sa.Column("overlay_sha256", sa.String(length=71), nullable=False),
        sa.Column("describe_digest", sa.String(length=71), nullable=False),
        sa.Column("base_image_digest", sa.String(length=71), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("approver_id", sa.String(length=64), nullable=False),
        sa.Column("uploader_id", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _ts("created_at", now=True),
        _tenant_fk("plugin_approvals"),
        sa.ForeignKeyConstraint(
            ["version_id", "overlay_sha256"],
            ["plugin_versions.id", "plugin_versions.upload_sha256"],
            name=op.f("fk_plugin_approvals_version_id_plugin_versions"),
            ondelete="CASCADE",
        ),
        _check("plugin_approvals", "decision", f"decision in ({_in(DECISIONS)})"),
        _check("plugin_approvals", "not_self", "approver_id <> uploader_id"),
        _check("plugin_approvals", "reject_note", "decision <> 'reject' OR note IS NOT NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_approvals")),
    )
    op.create_index("ix_plugin_approvals_version", "plugin_approvals", ["version_id", "created_at"])


def _runs() -> None:
    op.add_column(
        "runs",
        sa.Column("overlays", _jsonb(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_runs_overlays_array"), "runs", "jsonb_typeof(overlays) = 'array'"
    )
    op.create_check_constraint(
        op.f("ck_runs_overlays_untrusted"), "runs", "overlays = '[]'::jsonb OR untrusted_code"
    )
    op.create_index(
        "ix_runs_overlays",
        "runs",
        ["overlays"],
        postgresql_using="gin",
        postgresql_ops={"overlays": "jsonb_path_ops"},
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
  plugin_count bigint;
  version_count bigint;
  run_count bigint;
BEGIN
  SELECT count(*) INTO plugin_count FROM plugins;
  SELECT count(*) INTO version_count FROM plugin_versions;
  SELECT count(*) INTO run_count FROM runs WHERE overlays <> '[]'::jsonb;
  IF plugin_count + version_count + run_count > 0 THEN
    RAISE EXCEPTION 'v0.19 uploaded code exists (% plugins, % plugin versions, % runs with '
      'overlays): remove the plugins and the runs that used uploaded code before downgrading '
      'below 0022_plugins', plugin_count, version_count, run_count;
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
    op.drop_index("ix_runs_overlays", table_name="runs")
    op.drop_constraint(op.f("ck_runs_overlays_untrusted"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_overlays_array"), "runs", type_="check")
    op.drop_column("runs", "overlays")
    op.drop_index("ix_plugin_approvals_version", table_name="plugin_approvals")
    op.drop_table("plugin_approvals")
    op.drop_table("plugin_describes")
    op.drop_index("ix_plugin_versions_purge", table_name="plugin_versions")
    op.drop_index("ix_plugin_versions_state", table_name="plugin_versions")
    op.drop_table("plugin_versions")
    op.drop_index("ix_plugin_uploads_expiry", table_name="plugin_uploads")
    op.drop_table("plugin_uploads")
    op.drop_table("plugins")
    op.drop_table("user_verifications")
    op.drop_table("tenant_plugin_settings")
