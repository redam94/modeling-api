"""Pure helpers of the v0.15 calls shared by the sync and async clients: ``blobs/missing``
chunking, what to do with an upload session on resume, and the body of a collection version.
No I/O here."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from mapi_spec.api import BlobMissing, BlobQuery, UploadSession
from mapi_spec.collection import MAX_BLOB_QUERY, CollectionOptions, CollectionSource
from mapi_spec.ingest import IngestMapping, WeightsOptions

from mapi_client.collections import CollectionPlan, check_sha256

__all__ = [
    "MANIFEST_FILENAME",
    "Progress",
    "UploadAction",
    "blob_queries",
    "collection_key",
    "collection_options",
    "collection_source",
    "merge_missing",
    "next_base",
    "part_filename",
    "upload_action",
    "weights_mapping",
]

MANIFEST_FILENAME: Final = "manifest.csv"
_RENEW_MARGIN: Final = timedelta(minutes=2)
"""A signed PUT that expires sooner than this is renewed before the upload starts."""
_KEY_NAMESPACE: Final = uuid.UUID("6f0b3c58-2d1e-4b8a-9a77-5d3c1e0f4a21")

UploadAction = Literal["done", "put", "renew", "new"]
Progress = Callable[[str, int, int], None]
"""``progress(stage, done, total)``: stages ``hash``, ``check``, ``upload`` (bytes),
``create``, ``wait``."""


def part_filename(index: int) -> str:
    return f"part-{index:04d}.zip"


def blob_queries(shas: Sequence[str], base_version: int | None) -> list[BlobQuery]:
    """``shas`` in chunks of :data:`MAX_BLOB_QUERY` (the first chunk carries ``base_version``;
    :func:`next_base` pins the later ones to the version the server chose)."""
    unique = sorted(set(check_sha256(shas)))
    return [
        BlobQuery(sha256=unique[i : i + MAX_BLOB_QUERY], base_version=base_version)
        for i in range(0, len(unique), MAX_BLOB_QUERY)
    ]


def next_base(first: BlobMissing) -> int:
    """The ``base_version`` of the chunks after the first: the version the server compared
    against, or ``0`` (no base) - so every chunk is answered against the same version."""
    return first.base_version if first.base_version is not None else 0


def merge_missing(results: Sequence[BlobMissing], shas: Sequence[str]) -> BlobMissing:
    """One :class:`BlobMissing` for every chunk (``missing`` in the order of ``shas``)."""
    if not results:
        raise ValueError("no blobs/missing answers to merge")
    missing = {s for r in results for s in r.missing}
    seen: set[str] = set()
    ordered: list[str] = []
    for sha in shas:
        if sha in missing and sha not in seen:
            seen.add(sha)
            ordered.append(sha)
    return BlobMissing(
        base_version=results[0].base_version,
        missing=ordered,
        present=sum(r.present for r in results),
    )


def upload_action(session: UploadSession, now: datetime | None = None) -> UploadAction:
    """What a resumed upload does with a saved session: ``done`` (the file arrived),
    ``new`` (consumed: start a new upload), ``renew`` (the signed PUT expired or is about to)
    or ``put``."""
    if session.received:
        return "done"
    if session.status == "consumed":
        return "new"
    now = now or datetime.now(UTC)
    if session.status == "expired" or session.expires_at <= now + _RENEW_MARGIN:
        return "renew"
    if session.status == "uploaded":
        return "done"
    return "put"


def collection_options(
    plan: CollectionPlan, options: CollectionOptions | None, *, label_from_folder: bool
) -> CollectionOptions:
    """``options`` (default ones when ``None``) with the plan's path column and, with
    ``label_from_folder``, the folder labels as the label column."""
    opts = options or CollectionOptions()
    update: dict[str, object] = {}
    if opts.path_column != plan.path_column and (options is None or opts.path_column == "path"):
        update["path_column"] = plan.path_column
    if label_from_folder and opts.label_column is None and plan.label_column is not None:
        update["label_column"] = plan.label_column
    if not update:
        return opts
    return CollectionOptions.model_validate({**opts.model_dump(), **update})


def collection_source(
    manifest_upload_id: str,
    part_upload_ids: Sequence[str],
    base_version: int | None,
    options: CollectionOptions,
) -> CollectionSource:
    return CollectionSource(
        manifest_upload_id=manifest_upload_id,
        part_upload_ids=list(part_upload_ids),
        base_version=base_version,
        options=options,
    )


def collection_key(dataset_id: str, manifest_upload_id: str, part_ids: Sequence[str]) -> str:
    """A stable Idempotency-Key for the version create: a retry after a lost response (or a
    resumed upload of the same uploads) finds the version instead of making a second one."""
    name = "\n".join([dataset_id, manifest_upload_id, *part_ids])
    return str(uuid.uuid5(_KEY_NAMESPACE, name))


def weights_mapping(arch: dict[str, object] | Path | None = None) -> IngestMapping:
    """The mapping of a safetensors weights upload; ``arch`` is the architecture JSON (a
    mapping or a ``.json`` file) for files whose metadata has no ``mapi_arch``."""
    if isinstance(arch, Path):
        import json

        arch = json.loads(arch.read_text(encoding="utf-8"))
        if not isinstance(arch, dict):
            raise ValueError("the architecture file must hold a JSON object")
    weights = WeightsOptions.model_validate({"arch": arch} if arch is not None else {})
    return IngestMapping.model_validate({"format": "safetensors", "weights": weights})
