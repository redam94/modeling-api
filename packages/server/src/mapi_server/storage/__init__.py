"""Object storage for the control plane: the store factory and small helpers.

The server talks to storage only through :class:`mapi_spec.storage.ObjectStore`. The
default implementation is ``ShimObjectStore`` from ``mapi_gcs_shim`` (the local GCS
emulator); :class:`MemoryObjectStore` is an in-process implementation for tests.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from mapi_spec.storage import ObjectMeta, ObjectStore

from mapi_server.settings import ObjectStoreKind, Settings
from mapi_server.storage.memory import MemoryObjectStore, PolicyDoc

__all__ = ["MemoryObjectStore", "PolicyDoc", "build_object_store", "list_all"]


async def list_all(store: ObjectStore, bucket: str, prefix: str) -> list[ObjectMeta]:
    """Every live object under ``prefix`` (follows page tokens)."""
    out: list[ObjectMeta] = []
    token: str | None = None
    while True:
        page = await store.list(bucket, prefix=prefix, page_token=token, max_results=1000)
        out.extend(page.items)
        token = page.next_page_token
        if not token:
            return out


def _shim_store(settings: Settings) -> ObjectStore:
    try:
        mod = importlib.import_module("mapi_gcs_shim")
    except ImportError as exc:  # pragma: no cover - workspace always installs it
        raise RuntimeError("MAPI_OBJECT_STORE=shim needs the mapi-gcs-shim package") from exc
    cls: Any = getattr(mod, "ShimObjectStore", None)
    if cls is None:
        raise NotImplementedError(
            "mapi_gcs_shim does not provide ShimObjectStore yet; run with "
            "MAPI_OBJECT_STORE=memory (tests only) or install a shim that implements it"
        )
    params = inspect.signature(cls).parameters
    kwargs: dict[str, Any] = {}
    for name, value in (
        ("endpoint", settings.gcs_endpoint),
        ("base_url", settings.gcs_endpoint),
        ("token", settings.control_token),
        ("env", settings.env),
    ):
        if name in params and value is not None:
            kwargs[name] = value
    store = cls(**kwargs)
    if not isinstance(store, ObjectStore):
        raise TypeError("mapi_gcs_shim.ShimObjectStore does not satisfy the ObjectStore protocol")
    return store


def build_object_store(settings: Settings) -> ObjectStore:
    """The configured :class:`ObjectStore` (``MAPI_OBJECT_STORE``)."""
    if settings.object_store is ObjectStoreKind.MEMORY:
        return MemoryObjectStore(
            base_url=settings.gcs_endpoint, buckets=settings.buckets.all().values()
        )
    return _shim_store(settings)
