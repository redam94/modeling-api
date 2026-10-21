"""dashboard hardening: readout seq counter, materialisation attempts, wider dataset refs

Revision ID: 0019_dashboard_hardening
Revises: 0018_client_dashboards
Create Date: 2026-10-21 02:00:00

v0.16 review fixes:

* ``dashboards.next_readout_seq``: the next readout ``seq``, raised under the dashboard row
  lock and never lowered, so pruning the newest private readout never makes its ``seq``
  come back (the history is append-only). ``1`` on existing rows: the next readout then
  takes ``max(seq) + 1`` as before and the counter from there on;
* ``dashboard_readouts.attempts``: materialisation passes that hit a transient error (a
  storage timeout); past the cap the readout fails instead of being retried for ever;
* ``dashboard_refit_requests.dataset_version_id`` widens to 256 characters, so a
  ``dataset://project/name@vN`` ref is never truncated (a 128-character dataset name fits).

Only columns of ``0018`` tables change: their forced row-level security policies and grants
cover them, so no policy changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_dashboard_hardening"
down_revision: str | None = "0018_client_dashboards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column("next_readout_seq", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "dashboard_readouts",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column(
        "dashboard_refit_requests",
        "dataset_version_id",
        type_=sa.String(length=256),
        existing_type=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM dashboard_refit_requests "
        "WHERE length(dataset_version_id) > 128) THEN RAISE EXCEPTION 'refit requests with "
        "long dataset refs exist: remove them before downgrading below "
        "0019_dashboard_hardening'; END IF; END $$"
    )
    op.alter_column(
        "dashboard_refit_requests",
        "dataset_version_id",
        type_=sa.String(length=128),
        existing_type=sa.String(length=256),
        existing_nullable=True,
    )
    op.drop_column("dashboard_readouts", "attempts")
    op.drop_column("dashboards", "next_readout_seq")
