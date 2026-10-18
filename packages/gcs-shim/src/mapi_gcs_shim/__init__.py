"""Local Google Cloud Storage emulator for modeling-api dev and tests.

* :mod:`mapi_gcs_shim.app` / :mod:`~mapi_gcs_shim.server` — the HTTP server (GCS JSON-API
  subset, V4 signed URLs, V4 POST policies, IAM-lite, retention, lifecycle);
* :class:`ShimObjectStore` — :class:`mapi_spec.storage.ObjectStore` over the shim;
* :class:`GcsObjectStore` — the same protocol on real Cloud Storage (``[gcp]`` extra);
* :mod:`mapi_gcs_shim.transfer` — using signed URLs (shim or GCS);
* :mod:`mapi_gcs_shim.pytest_plugin` — the ``gcs_shim`` fixture.

Importing this package does not import the server stack (Starlette/uvicorn).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mapi_gcs_shim.client import InvalidRange, ShimObjectStore
from mapi_gcs_shim.config import (
    PRINCIPAL_CONTROL,
    PRINCIPAL_EXEC,
    ShimConfig,
    ShimCredentials,
    default_root,
)

if TYPE_CHECKING:
    from mapi_gcs_shim.gcs import GcsObjectStore
    from mapi_gcs_shim.server import ShimServer

__version__ = "0.7.0"

__all__ = [
    "PRINCIPAL_CONTROL",
    "PRINCIPAL_EXEC",
    "GcsObjectStore",
    "InvalidRange",
    "ShimConfig",
    "ShimCredentials",
    "ShimObjectStore",
    "ShimServer",
    "__version__",
    "default_root",
]


def __getattr__(name: str) -> Any:
    if name == "GcsObjectStore":
        from mapi_gcs_shim.gcs import GcsObjectStore

        return GcsObjectStore
    if name == "ShimServer":
        from mapi_gcs_shim.server import ShimServer

        return ShimServer
    raise AttributeError(f"module 'mapi_gcs_shim' has no attribute {name!r}")
