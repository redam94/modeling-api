"""Run Alembic migrations programmatically (``mapi-server migrate``)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from mapi_server.db.engine import libpq_url

__all__ = [
    "DEFAULT_APP_ROLE",
    "DEFAULT_GATEWAY_ROLE",
    "DEFAULT_SYSTEM_ROLE",
    "alembic_config",
    "downgrade",
    "upgrade",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
DEFAULT_APP_ROLE = "mapi_app"
DEFAULT_SYSTEM_ROLE = "mapi_system"
DEFAULT_GATEWAY_ROLE = "mapi_gateway"


def alembic_config(
    url: str,
    *,
    app_role: str = DEFAULT_APP_ROLE,
    system_role: str = DEFAULT_SYSTEM_ROLE,
    gateway_role: str = DEFAULT_GATEWAY_ROLE,
) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # '%' must be escaped for ConfigParser interpolation
    cfg.set_main_option("sqlalchemy.url", libpq_url(url).replace("%", "%%"))
    # the runtime database roles the tenancy migration creates and grants (see 0003)
    cfg.set_main_option("mapi.app_role", app_role)
    cfg.set_main_option("mapi.system_role", system_role)
    cfg.set_main_option("mapi.gateway_role", gateway_role)
    return cfg


def upgrade(
    url: str,
    revision: str = "head",
    *,
    app_role: str = DEFAULT_APP_ROLE,
    system_role: str = DEFAULT_SYSTEM_ROLE,
    gateway_role: str = DEFAULT_GATEWAY_ROLE,
) -> None:
    """Apply migrations up to ``revision`` (idempotent)."""
    cfg = alembic_config(url, app_role=app_role, system_role=system_role, gateway_role=gateway_role)
    command.upgrade(cfg, revision)


def downgrade(
    url: str,
    revision: str,
    *,
    app_role: str = DEFAULT_APP_ROLE,
    system_role: str = DEFAULT_SYSTEM_ROLE,
    gateway_role: str = DEFAULT_GATEWAY_ROLE,
) -> None:
    cfg = alembic_config(url, app_role=app_role, system_role=system_role, gateway_role=gateway_role)
    command.downgrade(cfg, revision)
