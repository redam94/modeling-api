"""when a browser session's person last proved who they are (the ID token's auth_time)

Revision ID: 0023_session_auth_time
Revises: 0022_plugins
Create Date: 2026-10-23 18:00:00

v0.19 (D13, recent sign-in for approving uploaded code):

* ``web_sessions.auth_time``: the ``auth_time`` claim of the ID token that started the
  session (NULL when the identity provider sent none). Approving uploaded code checks this,
  not ``created_at``: a sign-in the identity provider completed silently from its own SSO
  session creates a new row but proves nothing;
* ``login_transactions.reauth``: the sign-in asked the identity provider to authenticate the
  person again (``max_age=0``, ``prompt=login``); the callback then refuses an ID token
  without a fresh ``auth_time``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_session_auth_time"
down_revision: str | None = "0022_plugins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("web_sessions", sa.Column("auth_time", sa.DateTime(timezone=True)))
    op.add_column(
        "login_transactions",
        sa.Column("reauth", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("login_transactions", "reauth")
    op.drop_column("web_sessions", "auth_time")
