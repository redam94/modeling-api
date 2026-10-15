"""executor_reports.disabled_reason: why an operator disabled an executor

Revision ID: 0006_executor_disable
Revises: 0005_gateway_role
Create Date: 2026-10-15 18:00:00

``PUT /v1/admin/executors/{name}`` disables an executor (incident containment). The flag
lives in ``executor_reports.enabled``; orchestrator reports no longer overwrite it, and this
column keeps the operator's reason for the admin console (the audit log has who and when).
The API changes both columns as the gateway role (``0005_gateway_role``), which may update
just these two.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_executor_disable"
down_revision: str | None = "0005_gateway_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _gateway() -> str:
    cfg = op.get_context().config
    name = (cfg.get_main_option("mapi.gateway_role") if cfg else None) or "mapi_gateway"
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"invalid role name {name!r}")
    return name


def upgrade() -> None:
    op.add_column(
        "executor_reports", sa.Column("disabled_reason", sa.String(length=500), nullable=True)
    )
    op.execute(f"GRANT UPDATE (enabled, disabled_reason) ON executor_reports TO {_gateway()}")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE (enabled, disabled_reason) ON executor_reports FROM {_gateway()}")
    op.drop_column("executor_reports", "disabled_reason")
