"""URL minting for bundles and stored objects (plan §8.6).

Before any URL to a bundle object is handed out — to a client or to a score run — the
model version's signed :class:`~mapi_spec.run.BundleManifest` must verify, its identity must
match the registry row, and the object's live metadata must match the signed generation,
size and CRC32C. Links are generation-pinned and short-lived (``ARTIFACT_LINK_TTL``).
:func:`verify_bundle_inputs` re-runs that check for a run's pinned bundle inputs at every
point URLs are minted for them (dispatch and retry, not only submission).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

from mapi_spec.api import ARTIFACT_LINK_TTL, ArtifactLink
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import (
    BUNDLE_MANIFEST_OUTPUT,
    FIT_BUNDLE,
    INPUT_BUNDLE,
    INPUT_BUNDLE_MANIFEST,
    INPUT_PLOTS,
)
from mapi_spec.run import BundleManifest, BundleObject
from mapi_spec.storage import NotFound, ObjectStore
from pydantic import ValidationError

from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.signing import BundleSigner

__all__ = [
    "BUNDLE_INPUT_OBJECTS",
    "BundleIntegrityError",
    "artifact_link",
    "verified_bundle_object",
    "verified_manifest",
    "verify_bundle_inputs",
]

BUNDLE_INPUT_OBJECTS: Final[Mapping[str, str]] = {
    INPUT_BUNDLE: FIT_BUNDLE,
    INPUT_BUNDLE_MANIFEST: BUNDLE_MANIFEST_OUTPUT,
    INPUT_PLOTS: "plots.json",
}
"""Run input name → the bundle object it must be (as recorded in the signed manifest)."""


class BundleIntegrityError(MapiError):
    default_code = ErrorCode.INTEGRITY_FAILURE


def verified_manifest(signer: BundleSigner, mv: m.ModelVersion) -> BundleManifest:
    """The model version's manifest, only if its signature and identity check out."""
    try:
        manifest = BundleManifest.model_validate(mv.manifest)
    except ValidationError as exc:
        raise BundleIntegrityError("the stored bundle manifest is malformed") from exc
    if not signer.verify(manifest):
        raise BundleIntegrityError("the bundle manifest signature does not verify")
    if (
        manifest.tenant_id != mv.tenant_id
        or manifest.project_id != mv.project_id
        or manifest.model_id != mv.model_id
        or manifest.model_version != mv.version
        or manifest.run_id != mv.run_id
    ):
        raise BundleIntegrityError("the bundle manifest does not belong to this model version")
    return manifest


async def verified_bundle_object(
    store: ObjectStore, signer: BundleSigner, mv: m.ModelVersion, name: str
) -> BundleObject:
    manifest = verified_manifest(signer, mv)
    obj = manifest.objects.get(name)
    if obj is None:
        raise NotFoundError("artifact", name)
    try:
        meta = await store.stat(obj.bucket, obj.key, generation=obj.generation)
    except NotFound as exc:
        raise BundleIntegrityError(f"bundle object {name!r} is missing from storage") from exc
    if meta.size != obj.size or meta.crc32c != obj.crc32c or meta.generation != obj.generation:
        raise BundleIntegrityError(f"bundle object {name!r} does not match its signed manifest")
    return obj


async def verify_bundle_inputs(
    store: ObjectStore, signer: BundleSigner, mv: m.ModelVersion, inputs: Mapping[str, Any]
) -> None:
    """Raise :class:`BundleIntegrityError` unless every bundle input of a run is exactly the
    object the model version's signed manifest records (bucket, key and generation), and that
    object still matches it in storage."""
    for input_name, object_name in BUNDLE_INPUT_OBJECTS.items():
        ref = inputs.get(input_name)
        if ref is None:
            continue
        obj = await verified_bundle_object(store, signer, mv, object_name)
        try:
            pinned = stored_ref(ref)
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleIntegrityError(f"run input {input_name!r} is malformed") from exc
        if pinned != (obj.bucket, obj.key, obj.generation):
            raise BundleIntegrityError(
                f"run input {input_name!r} does not match the signed bundle manifest"
            )


async def artifact_link(
    store: ObjectStore,
    *,
    name: str,
    bucket: str,
    key: str,
    generation: int,
    ttl: timedelta = ARTIFACT_LINK_TTL,
) -> ArtifactLink:
    signed = await store.sign_get(bucket, key, expires_in=ttl, generation=generation)
    return ArtifactLink(
        name=name,
        url=signed.url,
        expires_at=signed.expires_at,
        size=signed.size,
        content_type=signed.content_type,
        crc32c=signed.crc32c,
        generation=signed.generation,
    )


def stored_ref(obj: dict[str, Any]) -> tuple[str, str, int]:
    """``(bucket, name, generation)`` of a stored-object JSON entry."""
    return str(obj["bucket"]), str(obj["name"]), int(obj["generation"])
