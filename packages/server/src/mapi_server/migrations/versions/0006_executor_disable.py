"""executor_reports.disabled_reason: why an operator disabled an executor

Revision ID: 0006_executor_disable
Revises: 0005_gateway_role
Create Date: 2026-10-15 18:00:00

``PUT /v1/admin/executors/{name}`` disables an executor (incident containment). The flag
lives in ``executor_reports.enabled``; orchestrator reports no longer overwrite it, and this
column keeps the operator's reason for the admin console (the audit log has who and when).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_executor_disable"
down_revision: str | None = "0005_gateway_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executor_reports", sa.Column("disabled_reason", sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("executor_reports", "disabled_reason")
