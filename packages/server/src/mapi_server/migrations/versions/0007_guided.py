"""guided mode data: the project persona lock and column profiles of dataset versions

Revision ID: 0007_guided
Revises: 0006_executor_disable
Create Date: 2026-10-16 09:00:00

* ``projects.guided_locked`` (M3 persona lock, ``Project.guided_locked``): members below
  project admin see Guided mode only, and their spec writes must stay Guided specs
  (``mapi_spec.guided.guided_lock_issues``). Existing projects are unlocked.
* ``dataset_versions.columns``: the ingest run's ``columns.json`` (one ``ColumnProfile`` per
  uploaded column; for MFF uploads per pivoted variable), validated by the finalizer and
  read by Guided role detection (``POST /v1/guided/detect-roles`` with a stored dataset).
  ``NULL`` for versions ingested before v0.4 and for zarr/netCDF uploads.

Both are columns of existing tenant tables, so their row-level-security policies
(``0003_tenancy``) and grants apply unchanged: the app role reaches them through tenant
sessions, and the gateway role keeps exactly the ``projects`` privileges of
``0005_gateway_role`` (it reads and creates project rows; the new column has a default) and
nothing on ``dataset_versions``. No new table is created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_guided"
down_revision: str | None = "0006_executor_disable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("guided_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "dataset_versions",
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dataset_versions", "columns")
    op.drop_column("projects", "guided_locked")
