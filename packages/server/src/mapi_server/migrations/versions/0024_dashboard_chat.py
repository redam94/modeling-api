"""dashboard chat: settings, kept transcripts, usage rows; dashboard read tokens

Revision ID: 0024_dashboard_chat
Revises: 0023_session_auth_time
Create Date: 2026-10-23 20:00:00

v0.20 "ask about your dashboard" (``mapi_spec.chat``):

* ``dashboard_chat_settings``: one row per dashboard whose chat an editor configured (no row
  means the defaults: chat off). Audiences, whether transcripts are kept, the daily token
  budgets (dashboard and per share link) and ``version`` for the ETag. Kept apart from the
  dashboard spec, so a toggle never makes a revision or changes a ``spec_hash``;
* ``dashboard_chat_transcripts``: one row per turn of a kept conversation, only when the
  dashboard keeps transcripts. User or assistant text (at most 4000 characters) and the tool
  names and outcomes of the turn (never arguments or results). ``expires_at`` (30 days) is
  what the housekeeping purge reads;
* ``dashboard_chat_usage``: one row per chat turn with the model, the tokens, the tool calls,
  the estimated cost under a price version and the outcome. Never any text. Chat cost is not
  in ``cost_ledger`` (that ledger is per run attempt);
* ``api_tokens.dashboard_id``: set only on a dashboard read token (scope
  ``dashboard:<dashboard_id>``). It counts active tokens per owner and dashboard, and the
  token goes with its dashboard (``ON DELETE CASCADE``, as every other dashboard child).

Every new table has forced row-level security with the tenant and platform policies of
``0018``/``0021``. The request path may select, insert, update and delete (an editor deletes
a transcript; settings are upserted); the platform role selects and deletes (purge, tenant
and user deletion, GDPR export); the gateway role gets nothing.

Downgrading refuses, with the counts, while a row exists in one of the three tables or a
read token names a dashboard (dropping the column would leave a dashboard token that looks
unscoped to an older server); then it drops the tables, the index, the key and the column.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_dashboard_chat"
down_revision: str | None = "0023_session_auth_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "mapi_tenant_isolation"
_PLATFORM_POLICY = "mapi_platform_access"
TABLES: tuple[str, ...] = (
    "dashboard_chat_settings",
    "dashboard_chat_transcripts",
    "dashboard_chat_usage",
)
APP_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
SYSTEM_PRIVILEGES = "SELECT, DELETE"

# mapi_spec.chat (duplicated: migrations never import the contracts)
PRINCIPAL_KINDS = ("member", "client", "share")
ROLES = ("user", "assistant")
OUTCOMES = ("ok", "budget", "error", "refused")
CONVERSATION_ID_RE = "^cnv_[0-9a-z]{26}$"
TEXT_MAX_CHARS = 4000
MAX_TURN_INDEX = 63
MAX_TOOL_CALLS = 6
MAX_TOKENS_PER_DAY = 5_000_000
MAX_SHARE_TOKENS_PER_DAY = 1_000_000
TOOL_CALLS_MAX_BYTES = 4096


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


def _dashboard_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["dashboard_id"],
        ["dashboards.id"],
        name=op.f(f"fk_{table}_dashboard_id_dashboards"),
        ondelete="CASCADE",
    )


def _principal_kind(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"principal_kind in ({_in(PRINCIPAL_KINDS)})", name=op.f(f"ck_{table}_principal_kind")
    )


def upgrade() -> None:
    _settings()
    _transcripts()
    _usage()
    _read_tokens()
    _security()


def _settings() -> None:
    table = "dashboard_chat_settings"
    op.create_table(
        table,
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("dashboard_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("aud_members", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("aud_clients", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("aud_share", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("keep_transcripts", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("tokens_per_day", sa.Integer(), server_default="200000", nullable=False),
        sa.Column("share_tokens_per_day", sa.Integer(), server_default="50000", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        _ts("updated_at", now=True),
        _project_fk(table),
        _dashboard_fk(table),
        sa.CheckConstraint(
            f"tokens_per_day >= 0 AND tokens_per_day <= {MAX_TOKENS_PER_DAY}",
            name=op.f(f"ck_{table}_tokens_per_day"),
        ),
        sa.CheckConstraint(
            f"share_tokens_per_day >= 0 AND share_tokens_per_day <= {MAX_SHARE_TOKENS_PER_DAY}",
            name=op.f(f"ck_{table}_share_tokens_per_day"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f(f"ck_{table}_version")),
        sa.PrimaryKeyConstraint("tenant_id", "dashboard_id", name=op.f(f"pk_{table}")),
    )


def _transcripts() -> None:
    table = "dashboard_chat_transcripts"
    op.create_table(
        table,
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("dashboard_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=30), nullable=False),
        sa.Column("principal_kind", sa.String(length=8), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=9), nullable=False),
        sa.Column("tool_calls", _jsonb(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("ok", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        _ts("created_at", now=True),
        _ts("expires_at"),
        _project_fk(table),
        _dashboard_fk(table),
        sa.CheckConstraint(
            f"conversation_id ~ '{CONVERSATION_ID_RE}'", name=op.f(f"ck_{table}_conversation_id")
        ),
        _principal_kind(table),
        sa.CheckConstraint(
            f"turn_index >= 0 AND turn_index <= {MAX_TURN_INDEX}",
            name=op.f(f"ck_{table}_turn_index"),
        ),
        sa.CheckConstraint(f"role in ({_in(ROLES)})", name=op.f(f"ck_{table}_role")),
        sa.CheckConstraint(
            f"char_length(text) <= {TEXT_MAX_CHARS}", name=op.f(f"ck_{table}_text_length")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tool_calls) = 'array'", name=op.f(f"ck_{table}_tool_calls_array")
        ),
        sa.CheckConstraint(
            f"octet_length(tool_calls::text) <= {TOOL_CALLS_MAX_BYTES}",
            name=op.f(f"ck_{table}_tool_calls_size"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint(
            "tenant_id", "conversation_id", "turn_index", name=op.f(f"uq_{table}_turn")
        ),
    )
    op.create_index(
        "ix_dashboard_chat_transcripts_dashboard",
        table,
        ["tenant_id", "dashboard_id", "created_at"],
    )
    op.create_index("ix_dashboard_chat_transcripts_expiry", table, ["expires_at"])


def _usage() -> None:
    table = "dashboard_chat_usage"
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("dashboard_id", sa.String(length=64), nullable=False),
        sa.Column("principal_kind", sa.String(length=8), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("share_id", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("thought_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("est_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("price_version", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        _ts("at", now=True),
        _project_fk(table),
        _dashboard_fk(table),
        _principal_kind(table),
        sa.CheckConstraint(
            "(principal_kind = 'share') = (share_id IS NOT NULL)",
            name=op.f(f"ck_{table}_share_id"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND thought_tokens >= 0",
            name=op.f(f"ck_{table}_tokens"),
        ),
        sa.CheckConstraint(
            f"tool_calls >= 0 AND tool_calls <= {MAX_TOOL_CALLS}",
            name=op.f(f"ck_{table}_tool_calls"),
        ),
        sa.CheckConstraint("est_micros >= 0", name=op.f(f"ck_{table}_est_micros")),
        sa.CheckConstraint(f"outcome in ({_in(OUTCOMES)})", name=op.f(f"ck_{table}_outcome")),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index("ix_dashboard_chat_usage_dashboard", table, ["tenant_id", "dashboard_id", "at"])


def _read_tokens() -> None:
    op.add_column("api_tokens", sa.Column("dashboard_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        op.f("fk_api_tokens_dashboard_id_dashboards"),
        "api_tokens",
        "dashboards",
        ["dashboard_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_api_tokens_dashboard", "api_tokens", ["tenant_id", "dashboard_id", "owner_id"]
    )


def _security() -> None:
    app = _role("mapi.app_role", "mapi_app")
    system = _role("mapi.system_role", "mapi_system")
    gateway = _role("mapi.gateway_role", "mapi_gateway")
    cond = "tenant_id = current_setting('app.tenant_id', false)"
    for table in TABLES:
        # the platform role has default privileges on new tables (0003); granted explicitly
        # too, so a database whose default privileges were changed still purges and exports
        op.execute(f"GRANT {SYSTEM_PRIVILEGES} ON {table} TO {system}")
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
  settings_count bigint;
  transcript_count bigint;
  usage_count bigint;
  token_count bigint;
BEGIN
  SELECT count(*) INTO settings_count FROM dashboard_chat_settings;
  SELECT count(*) INTO transcript_count FROM dashboard_chat_transcripts;
  SELECT count(*) INTO usage_count FROM dashboard_chat_usage;
  SELECT count(*) INTO token_count FROM api_tokens WHERE dashboard_id IS NOT NULL;
  IF settings_count + transcript_count + usage_count + token_count > 0 THEN
    RAISE EXCEPTION 'v0.20 dashboard chat data exists (% chat settings, % transcript turns, '
      '% usage rows, % dashboard read tokens): remove them before downgrading below '
      '0024_dashboard_chat', settings_count, transcript_count, usage_count, token_count;
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
    op.drop_index("ix_api_tokens_dashboard", table_name="api_tokens")
    op.drop_constraint(
        op.f("fk_api_tokens_dashboard_id_dashboards"), "api_tokens", type_="foreignkey"
    )
    op.drop_column("api_tokens", "dashboard_id")
    op.drop_index("ix_dashboard_chat_usage_dashboard", table_name="dashboard_chat_usage")
    op.drop_table("dashboard_chat_usage")
    op.drop_index("ix_dashboard_chat_transcripts_expiry", table_name="dashboard_chat_transcripts")
    op.drop_index(
        "ix_dashboard_chat_transcripts_dashboard", table_name="dashboard_chat_transcripts"
    )
    op.drop_table("dashboard_chat_transcripts")
    op.drop_table("dashboard_chat_settings")
