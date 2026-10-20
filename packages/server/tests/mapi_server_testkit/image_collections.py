"""Image collection builders for server tests (v0.15): uploads, parts, ready versions and the
outputs a collection ingest run writes. The server never decodes images or parquet, so random
bytes stand in for the manifest and the zip parts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_spec.api import UploadSession, new_id
from mapi_spec.collection import (
    COLLECTION_INFO,
    COLLECTION_MANIFEST,
    COLLECTION_MEMBERS,
    PACK_OBJECT_PREFIX,
    CollectionInfo,
    PackInfo,
    pack_digest,
    pack_object_name,
)
from mapi_spec.storage import ObjectMeta, crc32c, crc32c_to_base64, dataset_prefix, md5_base64

from .builders import TREND_SCHEMA, TREND_SUMMARY


def digests(n: int, seed: str = "img") -> list[str]:
    """``n`` distinct sha256 hex digests (stand-ins for image hashes)."""
    return [hashlib.sha256(f"{seed}-{i}".encode()).hexdigest() for i in range(n)]


def members_bin(shas: list[str]) -> bytes:
    """``members.bin``: sorted unique raw digests."""
    return b"".join(sorted({bytes.fromhex(s) for s in shas}))


def digest_of(data: bytes) -> str:
    """The pack digest the store computes for ``data``."""
    return pack_digest(len(data), crc32c_to_base64(crc32c(data)), md5_base64(data))


def stored(meta: ObjectMeta) -> dict[str, Any]:
    return {
        "bucket": meta.bucket,
        "name": meta.name,
        "generation": meta.generation,
        "size": meta.size,
        "crc32c": meta.crc32c,
        "md5": meta.md5_hash,
        "content_type": meta.content_type,
    }


def collection_info(
    packs: list[PackInfo], n_images: int, *, base_version: int | None = None
) -> CollectionInfo:
    return CollectionInfo(
        n_images=n_images,
        dim="image",
        var="pixels",
        label=True,
        split=False,
        classes=[{"label": "cat", "count": n_images}],  # type: ignore[list-item]
        mode="rgb",
        size=(8, 8),
        min_size=(8, 8),
        max_size=(8, 8),
        formats={"png": n_images},
        total_bytes=sum(p.size for p in packs),
        packs=packs,
        new_images=sum(p.images for p in packs if p.source == "upload"),
        reused_images=sum(p.images for p in packs if p.source == "base"),
        base_version=base_version,
        manifest_sha256="0" * 64,
    )


def pack_info(
    data: bytes, *, images: int, source: str = "upload", input: str | None = "part.0000"
) -> PackInfo:
    return PackInfo(
        digest=digest_of(data),
        size=len(data),
        crc32c=crc32c_to_base64(crc32c(data)),
        md5=md5_base64(data),
        entries=images,
        images=images,
        source=source,  # type: ignore[arg-type]
        input=input if source == "upload" else None,
    )


async def ready_collection(
    ctx: ServerContext,
    *,
    name: str = "pets",
    dataset_id: str | None = None,
    version: int = 1,
    shas: list[str] | None = None,
    status: str = "ready",
) -> tuple[str, m.DatasetVersion, list[bytes]]:
    """A dataset (or ``dataset_id``) with a collection version holding ``shas`` in one pack.
    Its schema is the trend schema, so the trend spec validates against it. Returns
    ``(dataset id, version row, [pack bytes])``."""
    shas = shas if shas is not None else digests(3)
    pack = b"PK" + os.urandom(64)
    info = collection_info([pack_info(pack, images=len(shas))], len(shas))
    manifest = os.urandom(256)
    sha = digest_of(manifest)
    prefix = dataset_prefix(ctx.scope.tenant_id, sha)
    bucket = ctx.buckets.data
    objects: dict[str, Any] = {}
    for obj, data in (
        (COLLECTION_MANIFEST, manifest),
        (COLLECTION_INFO, info.model_dump_json().encode()),
        (COLLECTION_MEMBERS, members_bin(shas)),
        ("schema.json", json.dumps(TREND_SCHEMA).encode()),
    ):
        objects[obj] = stored(await ctx.store.put(bucket, prefix + obj, data))
    digest = info.packs[0].digest
    meta = await ctx.store.put(bucket, pack_object_name(ctx.scope.tenant_id, digest), pack)
    objects[PACK_OBJECT_PREFIX + digest] = stored(meta)
    async with ctx.db.session() as s:
        if dataset_id is None:
            ds = m.Dataset(
                id=new_id("ds"),
                tenant_id=ctx.scope.tenant_id,
                project_id=ctx.scope.project_id,
                name=name,
                labels={},
                latest_version=version,
                next_version=version + 1,
            )
            s.add(ds)
            await s.flush()
            dataset_id = ds.id
        else:
            row = await s.get(m.Dataset, dataset_id)
            assert row is not None
            row.next_version = max(row.next_version, version + 1)
            row.latest_version = max(row.latest_version or 0, version)
        dv = m.DatasetVersion(
            dataset_id=dataset_id,
            version=version,
            tenant_id=ctx.scope.tenant_id,
            project_id=ctx.scope.project_id,
            status=status,
            sha256=f"sha256:{sha}",
            size_bytes=info.total_bytes,
            data_schema=TREND_SCHEMA,
            summary=TREND_SUMMARY,
            objects=objects,
            issues=[],
            layout="collection",
            collection=info.model_dump(mode="json"),
            ready_at=datetime.now(UTC) if status == "ready" else None,
        )
        s.add(dv)
    return dataset_id, dv, [pack]


async def upload(
    client: httpx.AsyncClient,
    ctx: ServerContext,
    ds_id: str,
    data: bytes | None,
    *,
    filename: str = "part.zip",
    size: int | None = None,
) -> UploadSession:
    """Create an upload and, unless ``data`` is ``None``, PUT ``data`` to its object."""
    r = await client.post(
        f"/v1/datasets/{ds_id}/uploads",
        json={
            "filename": filename,
            "content_type": "application/zip",
            "size_bytes": size if size is not None else len(data or b"x" * 64),
        },
    )
    assert r.status_code == 201, r.text
    up = UploadSession.model_validate(r.json())
    if data is not None:
        await ctx.store.put(ctx.buckets.quarantine, up.upload.name, data)
    return up
