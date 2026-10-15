"""Run Alembic migrations programmatically (``mapi-server migrate``)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from mapi_server.db.engine import libpq_url

__all__ = ["alembic_config", "downgrade", "upgrade"]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # '%' must be escaped for ConfigParser interpolation
    cfg.set_main_option("sqlalchemy.url", libpq_url(url).replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (idempotent)."""
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)
