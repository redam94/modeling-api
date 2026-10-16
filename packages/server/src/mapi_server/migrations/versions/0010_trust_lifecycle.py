"""trust lifecycle: sign-ups, tenant verification, tenant deletion and GDPR exports

Revision ID: 0010_trust_lifecycle
Revises: 0006_executor_disable
Create Date: 2026-10-15 21:00:00

Plan §9 ("Compute abuse", "Data lifecycle") and §14 M3 (signup verification, tenant
deletion). Four tables:

* ``signups`` (platform table, no tenant yet): one row per self-service sign-up with the
  keyed digest of its single-use e-mail token, the keyed digests of the client address and
  device cookie (rate limits and Sybil heuristics) and the operators' review. Only the gateway
  role (the API) and the platform role touch it;
* ``tenant_verifications``: e-mail and identity verification per tenant. The request-path
  role may only **read** it, so no tenant-session code path (or injection) can mark its own
  tenant verified; operators change it through the gateway role;
* ``tenant_deletions``: a deletion with its grace period and purge progress per store. It
  is the deletion's own, content-free record and survives the purge;
* ``tenant_exports``: GDPR export jobs and the archive each produced.

The three tenant tables get forced row-level security with the fail-closed policy of
``0003_tenancy`` (and the platform role's explicit policy), and least-privilege grants for
the gateway role like ``0005_gateway_role``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_trust_lifecycle"
down_revision: str | None = "0006_executor_disable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("tenant_verifications", "tenant_deletions", "tenant_exports")
_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
_GATEWAY_POLICY = "mapi_gateway_access"

APP_GRANTS: dict[str, str] = {
    "tenant_verifications": "SELECT",
    "tenant_deletions": "SELECT, INSERT, UPDATE",
    "tenant_exports": "SELECT, INSERT",
}
"""The request-path role: tenant admins read their verification, request and cancel their
deletion and request exports; it never deletes these rows or changes verification."""

GATEWAY_GRANTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "signups": ("SELECT, INSERT, UPDATE", ()),
    "tenant_verifications": ("SELECT, INSERT, UPDATE", ("SELECT", "INSERT", "UPDATE")),
    "tenant_deletions": ("SELECT, UPDATE", ("SELECT", "UPDATE")),
}
"""The gateway role (sign-up and verification are public routes; operators record identity
verification, review sign-ups and retry failed purges). No access to exports."""


def _roles() -> tuple[str, str, str]:
    cfg = op.get_context().config
    app = (cfg.get_main_option("mapi.app_role") if cfg else None) or "mapi_app"
    system = (cfg.get_main_option("mapi.system_role") if cfg else None) or "mapi_system"
    gateway = (cfg.get_main_option("mapi.gateway_role") if cfg else None) or "mapi_gateway"
    for name in (app, system, gateway):
        if not name.replace("_", "").isalnum() or not name[0].isalpha():
            raise ValueError(f"invalid role name {name!r}")
    return app, system, gateway


def upgrade() -> None:
    _schema()
    _security()


def _schema() -> None:
    op.create_table(
        "signups",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_domain", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("organisation", sa.String(length=200), nullable=True),
        sa.Column("terms_version", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("review", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("device_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("created_tenant_id", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "review in ('none','flagged','approved','rejected')", name=op.f("ck_signups_review")
        ),
        sa.CheckConstraint(
            "state in ('pending','verified','expired')", name=op.f("ck_signups_state")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signups")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_signups_token_hash")),
    )
    op.create_index("ix_signups_device", "signups", ["device_hash", "created_at"], unique=False)
    op.create_index("ix_signups_domain", "signups", ["email_domain", "created_at"], unique=False)
    op.create_index(
        "ix_signups_email", "signups", [sa.literal_column("lower(email)")], unique=False
    )
    op.create_index("ix_signups_ip", "signups", ["ip_hash", "created_at"], unique=False)
    op.create_index("ix_signups_review", "signups", ["review", "created_at"], unique=False)
    op.create_index("ix_signups_tenant", "signups", ["created_tenant_id"], unique=False)
    op.create_table(
        "tenant_deletions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("previous_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retained_objects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notify", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "state in ('scheduled','cancelled','deleting','purged','failed')",
            name=op.f("ck_tenant_deletions_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tenant_deletions_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_deletions")),
    )
    op.create_index(
        "ix_tenant_deletions_due", "tenant_deletions", ["state", "next_attempt_at"], unique=False
    )
    op.create_index(
        "uq_tenant_deletions_open",
        "tenant_deletions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("state in ('scheduled','deleting','failed')"),
    )
    op.create_table(
        "tenant_exports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("include", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=71), nullable=True),
        sa.Column("bucket", sa.String(length=63), nullable=True),
        sa.Column("object_name", sa.String(length=1024), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "state in ('queued','running','ready','expired','failed')",
            name=op.f("ck_tenant_exports_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_exports_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_exports")),
    )
    op.create_index("ix_tenant_exports_state", "tenant_exports", ["state"], unique=False)
    op.create_index(
        "ix_tenant_exports_tenant", "tenant_exports", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "tenant_verifications",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_verified", sa.Boolean(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "method in ('none','email','card','document','manual')",
            name=op.f("ck_tenant_verifications_method"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_verifications_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_verifications")),
    )


def _security() -> None:
    app, system, gateway = _roles()
    for table in ("signups", *TENANT_TABLES):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {system}")
    for table, privileges in APP_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {app}")
    for table in TENANT_TABLES:
        cond = "tenant_id = current_setting('app.tenant_id', false)"
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
    for table, (privileges, commands) in GATEWAY_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {gateway}")
        for command in commands:
            using = "" if command == "INSERT" else " USING (true)"
            check = " WITH CHECK (true)" if command in ("INSERT", "UPDATE") else ""
            op.execute(
                f"CREATE POLICY {_GATEWAY_POLICY}_{command.lower()} ON {table} AS PERMISSIVE "
                f"FOR {command} TO {gateway}{using}{check}"
            )


def downgrade() -> None:
    # policies and grants go with the tables
    op.drop_table("tenant_verifications")
    op.drop_index("ix_tenant_exports_tenant", table_name="tenant_exports")
    op.drop_index("ix_tenant_exports_state", table_name="tenant_exports")
    op.drop_table("tenant_exports")
    op.drop_index(
        "uq_tenant_deletions_open",
        table_name="tenant_deletions",
        postgresql_where=sa.text("state in ('scheduled','deleting','failed')"),
    )
    op.drop_index("ix_tenant_deletions_due", table_name="tenant_deletions")
    op.drop_table("tenant_deletions")
    op.drop_index("ix_signups_tenant", table_name="signups")
    op.drop_index("ix_signups_review", table_name="signups")
    op.drop_index("ix_signups_ip", table_name="signups")
    op.drop_index("ix_signups_email", table_name="signups")
    op.drop_index("ix_signups_domain", table_name="signups")
    op.drop_index("ix_signups_device", table_name="signups")
    op.drop_table("signups")
