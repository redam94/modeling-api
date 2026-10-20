"""image collections: ``dataset_versions.layout = 'collection'`` and ``dataset_versions.collection``

Revision ID: 0017_collections
Revises: 0016_experiments_media
Create Date: 2026-10-20 20:00:00

v0.15 image collections (``mapi_spec.collection``):

* ``dataset_versions.layout`` gains ``collection`` (``manifest.parquet`` + ``collection.json``
  + ``members.bin``; the image bytes live in packs shared per tenant under
  ``t/{tenant}/blobs/packs/``). The column widens to 16 characters and its check constraint
  is recreated;
* ``dataset_versions.collection``: the :class:`~mapi_spec.collection.CollectionInfo` of a
  collection version (``NULL`` for the other layouts).

Only a column is added to a table of ``0001``/``0003``: its forced row-level security policy
and grants cover it, so no policy changes. Downgrading refuses while collection versions
exist (an older server cannot read them).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_collections"
down_revision: str | None = "0016_experiments_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_dataset_versions_layout"


def upgrade() -> None:
    op.drop_constraint(op.f(_CHECK), "dataset_versions", type_="check")
    op.alter_column(
        "dataset_versions",
        "layout",
        type_=sa.String(length=16),
        existing_type=sa.String(length=8),
        existing_nullable=False,
        existing_server_default=sa.text("'dataset'"),
    )
    op.create_check_constraint(
        op.f(_CHECK), "dataset_versions", "layout in ('dataset','tree','collection')"
    )
    op.add_column(
        "dataset_versions",
        sa.Column("collection", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM dataset_versions WHERE layout = 'collection') "
        "THEN RAISE EXCEPTION 'collection dataset versions exist: delete them before "
        "downgrading below 0017_collections'; END IF; END $$"
    )
    op.drop_column("dataset_versions", "collection")
    op.drop_constraint(op.f(_CHECK), "dataset_versions", type_="check")
    op.alter_column(
        "dataset_versions",
        "layout",
        type_=sa.String(length=8),
        existing_type=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'dataset'"),
    )
    op.create_check_constraint(op.f(_CHECK), "dataset_versions", "layout in ('dataset','tree')")
