"""Object storage for the control plane: the store factory and small helpers.

The server talks to storage only through :class:`mapi_spec.storage.ObjectStore`. The
default implementation is ``ShimObjectStore`` from ``mapi_gcs_shim`` (the local GCS
emulator); ``GcsObjectStore`` is real Cloud Storage (``MAPI_OBJECT_STORE=gcs``: URLs signed
keylessly with IAM ``signBlob`` as ``MAPI_URL_SIGNER``); :class:`MemoryObjectStore` is an
in-process implementation for tests.
"""

from __future__ import annotations

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
    """``ShimObjectStore`` as the shim's ``control`` principal (key file under the shim root)."""
    from mapi_gcs_shim import PRINCIPAL_CONTROL, ShimCredentials, ShimObjectStore

    key_file = settings.gcs_key_path(PRINCIPAL_CONTROL)
    if not key_file.is_file():
        raise RuntimeError(
            f"the GCS shim's control key {str(key_file)!r} does not exist: start the shim "
            f"first (mapi-gcs-shim serve --root {settings.resolved_gcs_root()}) or set "
            "MAPI_GCS_ROOT / MAPI_GCS_KEY_FILE"
        )
    credentials = ShimCredentials.load(key_file)
    return ShimObjectStore(
        settings.gcs_endpoint,
        credentials,
        public_endpoint=settings.gcs_public_endpoint,
    )


def _gcs_store(settings: Settings) -> ObjectStore:
    """``GcsObjectStore`` with the Cloud Run wiring: application-default credentials for the
    storage client and the IAM token, URLs signed as ``MAPI_URL_SIGNER``."""
    from mapi_gcs_shim.gcs import GcsObjectStore

    assert settings.url_signer is not None  # enforced by Settings
    store: ObjectStore = GcsObjectStore.for_cloud_run(
        service_account_email=settings.url_signer,
        uploader_service_account_email=settings.url_uploader,
        project=settings.gcp_project,
    )
    return store


def build_object_store(settings: Settings) -> ObjectStore:
    """The configured :class:`ObjectStore` (``MAPI_OBJECT_STORE``)."""
    if settings.object_store is ObjectStoreKind.MEMORY:
        return MemoryObjectStore(
            base_url=settings.gcs_endpoint, buckets=settings.buckets.all().values()
        )
    if settings.object_store is ObjectStoreKind.GCS:
        return _gcs_store(settings)
    return _shim_store(settings)
