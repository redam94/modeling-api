"""Settings that select cloud services (object store, signer)."""

from __future__ import annotations

import pytest
from mapi_server.settings import Settings


def test_cloud_storage_store_is_keyless_and_needs_a_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAPI_OBJECT_STORE=gcs (what Terraform sets): IAM signBlob as MAPI_URL_SIGNER, uploads
    signed as MAPI_URL_UPLOADER, the client in MAPI_GCP_PROJECT."""
    from mapi_gcs_shim import gcs
    from mapi_server.settings import ObjectStoreKind
    from mapi_server.storage import build_object_store

    with pytest.raises(ValueError, match="MAPI_URL_SIGNER"):
        Settings(database_url="postgresql://u/u", object_store=ObjectStoreKind.GCS)
    seen: dict[str, object] = {}

    def fake(cls: object, client: object = None, **kw: object) -> str:
        seen.update(kw)
        return "store"

    monkeypatch.setattr(gcs.GcsObjectStore, "for_cloud_run", classmethod(fake))
    settings = Settings(
        database_url="postgresql://u/u",
        object_store=ObjectStoreKind.GCS,
        url_signer="signer@p.iam.gserviceaccount.com",
        url_uploader="uploader@p.iam.gserviceaccount.com",
        gcp_project="mapi-prod-data",
    )
    assert build_object_store(settings) == "store"
    assert seen == {
        "service_account_email": "signer@p.iam.gserviceaccount.com",
        "uploader_service_account_email": "uploader@p.iam.gserviceaccount.com",
        "project": "mapi-prod-data",
    }


def test_default_residency_follows_the_cloud_run_region() -> None:
    """Tenants created without a residency (the platform tenant among them) must be able to
    run on the deployment's executor: the default comes from its region."""
    assert Settings().effective_default_residency() == "us"  # local executors: no region
    cloud = Settings(executor="cloudrun-jobs", cloudrun_region="europe-west4")
    assert cloud.effective_default_residency() == "eu"
    assert Settings(executor="cloudrun-jobs").effective_default_residency() == "eu"
    pinned = Settings(executor="cloudrun-jobs", default_residency="europe-west4")
    assert pinned.effective_default_residency() == "europe-west4"
    with pytest.raises(ValueError, match="default_residency"):
        Settings(default_residency="Not A Region")
def test_cloud_storage_store_needs_a_separate_uploader() -> None:
    """Every run asks for a create_only output policy, which GCS can only sign as an account
    without delete: a gcs deployment without MAPI_URL_UPLOADER would fail every run at
    dispatch, so it is refused at start-up (and so is reusing the signer, which may delete
    checkpoints)."""
    from mapi_server.settings import ObjectStoreKind

    signer = "signer@p.iam.gserviceaccount.com"
    with pytest.raises(ValueError, match="MAPI_URL_UPLOADER"):
        Settings(
            database_url="postgresql://u/u", object_store=ObjectStoreKind.GCS, url_signer=signer
        )
    with pytest.raises(ValueError, match="MAPI_URL_UPLOADER"):
        Settings(
            database_url="postgresql://u/u",
            object_store=ObjectStoreKind.GCS,
            url_signer=signer,
            url_uploader=signer,
        )
    ok = Settings(
        database_url="postgresql://u/u",
        object_store=ObjectStoreKind.GCS,
        url_signer=signer,
        url_uploader="uploader@p.iam.gserviceaccount.com",
    )
    assert ok.url_uploader == "uploader@p.iam.gserviceaccount.com"
