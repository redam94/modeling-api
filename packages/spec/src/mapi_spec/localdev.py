"""Local development topology: ports, origins and environment variable names.

One place for the defaults every M1 component (API, GCS shim, runner, local executor, web
app, plot sandbox) agrees on, so they can be started independently without drift. Every value
can be overridden through the environment variable named next to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from mapi_spec.storage import Buckets

__all__ = [
    "API_PORT",
    "DEFAULT_ENV",
    "ENV_API_URL",
    "ENV_DATABASE_URL",
    "ENV_DATA_DIR",
    "ENV_ENV",
    "ENV_GCS_ENDPOINT",
    "ENV_PLOTS_ORIGIN",
    "ENV_WEB_ORIGIN",
    "GCS_SHIM_PORT",
    "LOCALHOST",
    "PLOT_SANDBOX_PORT",
    "WEB_PORT",
    "LocalDev",
]

LOCALHOST: Final = "127.0.0.1"
API_PORT: Final = 8000
GCS_SHIM_PORT: Final = 4443
WEB_PORT: Final = 5173
PLOT_SANDBOX_PORT: Final = 5174
"""The plot sandbox must be a *different origin* from the web app (plan §10.3)."""

DEFAULT_ENV: Final = "local"

ENV_ENV: Final = "MAPI_ENV"
ENV_DATABASE_URL: Final = "DATABASE_URL"
ENV_GCS_ENDPOINT: Final = "MAPI_GCS_ENDPOINT"
ENV_API_URL: Final = "MAPI_API_URL"
ENV_WEB_ORIGIN: Final = "MAPI_WEB_ORIGIN"
ENV_PLOTS_ORIGIN: Final = "MAPI_PLOTS_ORIGIN"
ENV_DATA_DIR: Final = "MAPI_DATA_DIR"


@dataclass(frozen=True, slots=True)
class LocalDev:
    """Resolved local configuration. ``database_url=None`` means "start embedded pgserver"."""

    env: str = DEFAULT_ENV
    database_url: str | None = None
    gcs_endpoint: str = f"http://{LOCALHOST}:{GCS_SHIM_PORT}"
    api_url: str = f"http://{LOCALHOST}:{API_PORT}"
    web_origin: str = f"http://{LOCALHOST}:{WEB_PORT}"
    plots_origin: str = f"http://{LOCALHOST}:{PLOT_SANDBOX_PORT}"
    data_dir: str = ".local"

    def __post_init__(self) -> None:
        Buckets(self.env)  # validates the environment name
        if self.web_origin.rstrip("/") == self.plots_origin.rstrip("/"):
            raise ValueError("the plot sandbox must be served from a different origin")

    @property
    def buckets(self) -> Buckets:
        return Buckets(self.env)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> LocalDev:
        """Defaults overridden by any of the ``ENV_*`` variables that are set and non-empty."""
        base = cls()

        def pick(name: str, default: str) -> str:
            value = environ.get(name, "").strip()
            return value or default

        return cls(
            env=pick(ENV_ENV, base.env),
            database_url=environ.get(ENV_DATABASE_URL, "").strip() or None,
            gcs_endpoint=pick(ENV_GCS_ENDPOINT, base.gcs_endpoint),
            api_url=pick(ENV_API_URL, base.api_url),
            web_origin=pick(ENV_WEB_ORIGIN, base.web_origin),
            plots_origin=pick(ENV_PLOTS_ORIGIN, base.plots_origin),
            data_dir=pick(ENV_DATA_DIR, base.data_dir),
        )

    def bootstrap_url_template(self) -> str:
        """``MAPI_BOOTSTRAP_URL`` for executions talking to the local GCS shim."""
        return f"{self.gcs_endpoint}/storage/v1/b/{self.buckets.bootstrap}/o/{{nonce}}"
