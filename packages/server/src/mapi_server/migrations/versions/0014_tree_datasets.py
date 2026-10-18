"""tree datasets: ``dataset_versions.layout/tree_schema/sources``, ``uploads.inspection``

Revision ID: 0014_tree_datasets
Revises: 0013_plot_examples
Create Date: 2026-10-18 12:00:00

M4 tree datasets (``mapi_spec.tree``):

* ``dataset_versions.layout``: ``dataset`` (one ``xr.Dataset``, every version before this
  revision) or ``tree`` (an ``xr.DataTree`` in ``data.tree.zip``), checked;
* ``dataset_versions.tree_schema``: the :class:`~mapi_spec.tree.TreeSchema` of a tree
  version (``NULL`` for dataset-layout versions). It is ``json``, not ``jsonb``: ``jsonb``
  reorders object keys, and the node order is meaningful (the first node with variables
  is the one viewers open);
* ``dataset_versions.sources``: the upload ids a version was built from (a JSONB list;
  back-filled with ``[upload_id]`` for every existing version that has one);
* ``uploads.inspection``: the :class:`~mapi_spec.ingest.UploadInspection` an ``inspect`` run
  found, and ``uploads.inspect_run_id`` (the run that computes it).

Only columns are added to tables of ``0001``/``0003``: their forced row-level security
policies and grants cover the new columns, so no policy changes. ``runs.kind`` has no check
constraint (the new ``inspect`` kind needs none). Downgrading drops the columns (tree versions
lose their layout and must not be read by an older server).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_tree_datasets"
down_revision: str | None = "0013_plot_examples"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "dataset_versions",
        sa.Column(
            "layout", sa.String(length=8), server_default=sa.text("'dataset'"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_dataset_versions_layout"), "dataset_versions", "layout in ('dataset','tree')"
    )
    # json, not jsonb: the node order is the tree's declaration order (default_node())
    op.add_column("dataset_versions", sa.Column("tree_schema", postgresql.JSON(), nullable=True))
    op.add_column(
        "dataset_versions",
        sa.Column("sources", _jsonb(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.execute(
        "UPDATE dataset_versions SET sources = jsonb_build_array(upload_id) "
        "WHERE upload_id IS NOT NULL"
    )
    op.add_column("uploads", sa.Column("inspection", _jsonb(), nullable=True))
    op.add_column("uploads", sa.Column("inspect_run_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("uploads", "inspect_run_id")
    op.drop_column("uploads", "inspection")
    op.drop_column("dataset_versions", "sources")
    op.drop_column("dataset_versions", "tree_schema")
    op.drop_constraint(op.f("ck_dataset_versions_layout"), "dataset_versions", type_="check")
    op.drop_column("dataset_versions", "layout")
