"""plot examples: ``catalogs.plot_examples``

Revision ID: 0013_plot_examples
Revises: 0012_governance
Create Date: 2026-10-18 00:30:00

A describe run renders every plot recipe on its synthetic example data
(``plot_examples.json``, :class:`mapi_spec.plots.PlotExamples`). The finalizer keeps it
next to the catalog of the same image digest, so the catalog can show what each plot looks
like. The column is nullable: catalogs imported from a file, and catalogs stored before this
revision, have no examples.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_plot_examples"
down_revision: str | None = "0012_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("catalogs", sa.Column("plot_examples", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("catalogs", "plot_examples")
