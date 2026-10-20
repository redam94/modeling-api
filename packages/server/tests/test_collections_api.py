"""v0.15 image collections on the API: resumable uploads (``get_upload``, ``renew_upload``),
collection versions (manifest + parts → one streaming ingest run, base version rules and
caps), ``blobs/missing`` from a base version's ``members.bin``, and collection inputs of the
runs that read a collection version."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mapi_server.api import datasets as datasets_api
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runspec import input_layouts
from mapi_server.services.specs import class_for_size, data_cap_issue, validate_document
from mapi_server.services.submit import (
    collection_ingest_timeout_s,
    data_ref,
    dataset_inputs,
)
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset, submit_fit
from mapi_server_testkit.image_collections import digests, members_bin, ready_collection, upload
from mapi_spec.api import BlobMissing, DatasetVersion, UploadSession, new_id
from mapi_spec.collection import MAX_COLLECTION_PARTS, PACK_OBJECT_PREFIX, CollectionInfo
from mapi_spec.errors import MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.storage import NotFound, ObjectMeta

pytestmark = pytest.mark.integration


async def _dataset(client: httpx.AsyncClient, name: str = "pets") -> str:
    r = await client.post("/v1/datasets", json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _body(manifest: UploadSession, *parts: UploadSession, **source: Any) -> dict[str, Any]:
    return {
        "collection": {
            "manifest_upload_id": manifest.id,
            "part_upload_ids": [p.id for p in parts],
            "options": {"label_column": "label"},
            **source,
        }
    }


async def _run(ctx: ServerContext, run_id: str | None) -> m.Run:
    assert run_id is not None
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        s.expunge(run)
        return run


# ============================================================================ uploads


async def test_get_upload_says_whether_the_file_arrived_and_renew_signs_again(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    arrived = await upload(client, ctx, ds, b"PK" + os.urandom(30))
    pending = await upload(client, ctx, ds, None)
    r = await client.get(f"/v1/datasets/{ds}/uploads/{arrived.id}")
    assert r.status_code == 200 and r.json()["received"] is True
    r = await client.get(f"/v1/datasets/{ds}/uploads/{pending.id}")
    assert r.status_code == 200 and r.json()["received"] is False
    # an upload whose file arrived has nothing to renew
    r = await client.post(f"/v1/datasets/{ds}/uploads/{arrived.id}/renew")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    # an expired upload whose file did not arrive gets a fresh URL and is pending again
    async with ctx.db.session() as s:
        row = await s.get(m.Upload, pending.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        row.status = "expired"
    r = await client.post(f"/v1/datasets/{ds}/uploads/{pending.id}/renew")
    assert r.status_code == 200, r.text
    renewed = UploadSession.model_validate(r.json())
    assert renewed.received is False and renewed.status == "pending"
    assert renewed.expires_at > datetime.now(UTC) + timedelta(minutes=50)
    assert renewed.upload.name == pending.upload.name  # the same object, signed again
    # a PUT to the renewed URL arrives; the version create then accepts the upload
    await ctx.store.put(ctx.buckets.quarantine, renewed.upload.name, b"PK" + os.urandom(30))
    r = await client.get(f"/v1/datasets/{ds}/uploads/{pending.id}")
    assert r.json()["received"] is True
    # unknown uploads and uploads of another dataset are 404
    other = await _dataset(client, "other")
    for path in (f"/v1/datasets/{other}/uploads/{arrived.id}", f"/v1/datasets/{ds}/uploads/nope"):
        assert (await client.get(path)).status_code == 404
        assert (await client.post(path + "/renew")).status_code == 404


async def test_a_consumed_upload_is_received_and_cannot_be_renewed(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    manifest = await upload(client, ctx, ds, b"path,sha256\n", filename="m.csv")
    r = await client.post(f"/v1/datasets/{ds}/versions", json=_body(manifest, base_version=0))
    assert r.status_code == 202, r.text
    await ctx.store.delete(ctx.buckets.quarantine, manifest.upload.name)
    got = (await client.get(f"/v1/datasets/{ds}/uploads/{manifest.id}")).json()
    assert got["status"] == "consumed" and got["received"] is True
    r = await client.post(f"/v1/datasets/{ds}/uploads/{manifest.id}/renew")
    assert r.status_code == 409


async def test_an_arrived_upload_stays_usable_after_its_put_expired(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A resumed collection upload uses a manifest that arrived hours before: once its file
    arrived, an upload is ``uploaded``, ``get_upload`` and the version create agree, and the
    expiry of its PUT URL does not refuse it."""
    ds = await _dataset(client)
    manifest = await upload(client, ctx, ds, b"path,sha256\n", filename="m.csv")
    part = await upload(client, ctx, ds, b"PK" + os.urandom(40))
    async with ctx.db.session() as s:
        for up in (manifest, part):
            row = await s.get(m.Upload, up.id)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(hours=3)
    got = (await client.get(f"/v1/datasets/{ds}/uploads/{manifest.id}")).json()
    assert got["received"] is True and got["status"] == "uploaded"
    assert (await client.post(f"/v1/datasets/{ds}/uploads/{manifest.id}/renew")).status_code == 409
    r = await client.post(f"/v1/datasets/{ds}/versions", json=_body(manifest, part, base_version=0))
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        for up in (manifest, part):
            row = await s.get(m.Upload, up.id)
            assert row is not None and row.status == "consumed"


async def test_the_housekeeper_keeps_arrived_uploads_and_expires_the_others(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.services.housekeeping import UPLOAD_KEEP, Housekeeper

    ds = await _dataset(client)
    arrived = await upload(client, ctx, ds, b"PK" + os.urandom(40))
    missing = await upload(client, ctx, ds, None)
    async with ctx.db.session() as s:
        for up in (arrived, missing):
            row = await s.get(m.Upload, up.id)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(hours=2)
    await Housekeeper(ctx).sweep_once()
    got = (await client.get(f"/v1/datasets/{ds}/uploads/{arrived.id}")).json()
    assert got["status"] == "uploaded" and got["received"] is True
    got = (await client.get(f"/v1/datasets/{ds}/uploads/{missing.id}")).json()
    assert got["status"] == "expired" and got["received"] is False
    # an expired upload is not received even when a stale object is left: renew replaces it
    await ctx.store.put(ctx.buckets.quarantine, missing.upload.name, b"stale")
    assert (await client.get(f"/v1/datasets/{ds}/uploads/{missing.id}")).json()["received"] is False
    r = await client.post(f"/v1/datasets/{ds}/uploads/{missing.id}/renew")
    assert r.status_code == 200 and r.json()["status"] == "pending", r.text
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.quarantine, missing.upload.name)
    # an arrived file that no version used is kept for UPLOAD_KEEP, then it expires
    async with ctx.db.session() as s:
        row = await s.get(m.Upload, arrived.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - UPLOAD_KEEP - timedelta(minutes=1)
    await Housekeeper(ctx).sweep_once()
    assert (await client.get(f"/v1/datasets/{ds}/uploads/{arrived.id}")).json()["status"] == (
        "expired"
    )
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.quarantine, arrived.upload.name)


async def test_a_dataset_reader_cannot_get_the_signed_put_of_an_upload(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """The answer of ``get_upload`` holds a signed PUT URL: it needs ``update`` on the
    dataset, as ``create_upload`` and ``renew_upload``."""
    from mapi_server_testkit.identity import grant_role, make_user, session_caller

    ds = await _dataset(client)
    pending = await upload(client, ctx, ds, None)
    viewer = await make_user(ctx, "viewer@default.test")
    await grant_role(ctx, viewer, "default", "default", "viewer")
    vc = await session_caller(ctx, client, viewer)
    assert (await vc.get(f"/v1/datasets/{ds}")).status_code == 200
    for r in (
        await vc.get(f"/v1/datasets/{ds}/uploads/{pending.id}"),
        await vc.post(f"/v1/datasets/{ds}/uploads/{pending.id}/renew"),
    ):
        assert r.status_code == 403, r.text
        assert "upload" not in r.json() or r.json().get("upload") is None


# ================================================================== collection versions


async def test_a_collection_version_starts_one_streaming_ingest_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    manifest = await upload(client, ctx, ds, b"path,sha256,label\n", filename="m.csv")
    parts = [await upload(client, ctx, ds, b"PK" + os.urandom(100)) for _ in range(2)]
    body = {**_body(manifest, *parts), "message": "first images"}
    r = await client.post(
        f"/v1/datasets/{ds}/versions", json=body, headers={"Idempotency-Key": "col-1"}
    )
    assert r.status_code == 202, r.text
    dv = DatasetVersion.model_validate(r.json())
    assert dv.status == "ingesting" and dv.sources == [manifest.id, *(p.id for p in parts)]
    run = await _run(ctx, dv.ingest_run_id)
    assert run.kind == "ingest" and run.resource_class == "M"
    assert set(run.inputs) == {"manifest", "part.0000", "part.0001"}
    assert run.inputs["manifest"]["name"] == manifest.upload.name
    assert run.inputs["part.0001"]["name"] == parts[1].upload.name
    job = run.payload["collection"]
    assert job["parts"] == 2 and "base_version" not in job and job["base_packs"] == 0
    assert job["options"]["label_column"] == "label"
    assert run.payload["source"]["filename"] == "m.csv"
    assert "mapping" not in run.payload and "assembly" not in run.payload
    assert run.timeout_s == collection_ingest_timeout_s(sum(p.size_bytes for p in parts))
    assert input_layouts(run.inputs) == {}  # uploads, not collection versions
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (ds, 1))
        assert row is not None and row.mapping is not None and "collection" in row.mapping
        uploads = [await s.get(m.Upload, u.id) for u in (manifest, *parts)]
        assert {(u.status, u.consumed_by_version) for u in uploads if u} == {("consumed", 1)}
    again = await client.post(
        f"/v1/datasets/{ds}/versions", json=body, headers={"Idempotency-Key": "col-1"}
    )
    assert again.status_code == 200 and again.json()["version"] == 1


def test_the_ingest_timeout_grows_with_the_part_bytes() -> None:
    assert collection_ingest_timeout_s(0) == 1800
    assert collection_ingest_timeout_s(20_000_000) == 1801
    assert collection_ingest_timeout_s(20_000_001) == 1802
    assert collection_ingest_timeout_s(10**15) == 24 * 3600


async def test_the_latest_ready_collection_version_is_the_default_base(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds, base, _ = await ready_collection(ctx)
    # a newer version that is not a collection is not a base
    async with ctx.db.session() as s:
        s.add(
            m.DatasetVersion(
                dataset_id=ds, version=2, tenant_id=ctx.scope.tenant_id,
                project_id=ctx.scope.project_id, status="ready", objects={}, issues=[],
            )
        )  # fmt: skip
        row = await s.get(m.Dataset, ds)
        assert row is not None
        row.next_version = 3
    manifest = await upload(client, ctx, ds, b"sha256\n", filename="m.csv")
    part = await upload(client, ctx, ds, b"PK" + os.urandom(40))
    r = await client.post(f"/v1/datasets/{ds}/versions", json=_body(manifest, part))
    assert r.status_code == 202, r.text
    run = await _run(ctx, r.json()["ingest_run_id"])
    assert run.payload["collection"]["base_version"] == 1
    assert run.payload["collection"]["base_packs"] == 1
    assert set(run.inputs) == {
        "manifest", "part.0000", "base.manifest", "base.info", "base.pack.0000",
    }  # fmt: skip
    info = CollectionInfo.model_validate(base.collection)
    pack = base.objects[PACK_OBJECT_PREFIX + info.packs[0].digest]
    assert run.inputs["base.pack.0000"]["name"] == pack["name"]
    assert run.inputs["base.info"]["name"] == base.objects["collection.json"]["name"]
    # base_version 0: no base, even with a ready collection version
    manifest = await upload(client, ctx, ds, b"sha256\n", filename="m.csv")
    r = await client.post(f"/v1/datasets/{ds}/versions", json=_body(manifest, base_version=0))
    assert r.status_code == 202, r.text
    run = await _run(ctx, r.json()["ingest_run_id"])
    assert set(run.inputs) == {"manifest"} and "base_version" not in run.payload["collection"]


async def test_collection_versions_refuse_bad_bases_parts_and_caps(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds, _, _ = await ready_collection(ctx)
    flat, _ = await ready_dataset(ctx, "flat")
    async with ctx.db.session() as s:  # v2: a collection version still ingesting
        s.add(
            m.DatasetVersion(
                dataset_id=ds, version=2, tenant_id=ctx.scope.tenant_id,
                project_id=ctx.scope.project_id, status="ingesting", layout="collection",
                objects={}, issues=[],
            )
        )  # fmt: skip
        row = await s.get(m.Dataset, ds)
        assert row is not None
        row.next_version = 3

    async def create(body: dict[str, Any], dataset: str = ds) -> httpx.Response:
        return await client.post(f"/v1/datasets/{dataset}/versions", json=body)

    async def manifest(dataset: str = ds) -> UploadSession:
        return await upload(client, ctx, dataset, b"sha256\n", filename="m.csv")

    m1 = await manifest()
    r = await create(_body(m1, base_version=2))
    assert r.status_code == 409 and "not ready" in r.json()["detail"]
    assert r.json()["errors"][0]["pointer"] == "/body/collection/base_version"
    r = await create(_body(m1, base_version=9))
    assert r.status_code == 404
    # a plain dataset version is not a base
    m2 = await manifest(flat.id)
    r = await create(_body(m2, base_version=1), flat.id)
    assert r.status_code == 409 and "not an image collection" in r.json()["detail"]
    # too many parts: the DTO refuses it
    many = [new_id("upl") for _ in range(MAX_COLLECTION_PARTS + 1)]
    r = await create({"collection": {"manifest_upload_id": m1.id, "part_upload_ids": many}})
    assert r.status_code == 422
    # a part without a server MD5 (a composed object) has no pack address
    part = await upload(client, ctx, ds, b"PK" + os.urandom(40))
    real_stat = ctx.store.stat

    async def stat(bucket: str, name: str, **kw: Any) -> ObjectMeta:
        meta = await real_stat(bucket, name, **kw)
        if name == part.upload.name:
            return meta.model_copy(update={"md5_hash": None})
        return meta

    monkeypatch.setattr(ctx.store, "stat", stat)
    r = await create(_body(m1, part))
    assert r.status_code == 409 and "MD5" in r.json()["detail"]
    assert r.json()["errors"][0]["pointer"] == "/body/collection/part_upload_ids/0"
    monkeypatch.setattr(ctx.store, "stat", real_stat)
    # caps: the manifest and each part
    monkeypatch.setattr(datasets_api, "MAX_PART_BYTES", 10)
    r = await create(_body(m1, part))
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    monkeypatch.setattr(datasets_api, "MAX_MANIFEST_BYTES", 3)
    r = await create(_body(m1, part))
    assert r.status_code == 413
    assert r.json()["errors"][0]["pointer"] == "/body/collection/manifest_upload_id"
    # nothing was consumed by the refused requests
    async with ctx.db.session() as s:
        for up in (m1, part):
            row_up = await s.get(m.Upload, up.id)
            assert row_up is not None and row_up.status == "pending"


async def test_a_manifest_that_did_not_arrive_is_refused(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    manifest = await upload(client, ctx, ds, None, filename="m.csv")
    r = await client.post(f"/v1/datasets/{ds}/versions", json=_body(manifest, base_version=0))
    assert r.status_code == 409
    assert r.json()["errors"][0]["pointer"] == "/body/collection/manifest_upload_id"


# ======================================================================= blobs/missing


async def test_missing_images_are_answered_from_the_base_members(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    held = digests(3)
    ds, _, _ = await ready_collection(ctx, shas=held)
    new = digests(2, seed="new")
    query = [new[0], held[2], held[0], new[1], held[1]]
    r = await client.post(f"/v1/datasets/{ds}/blobs/missing", json={"sha256": query})
    assert r.status_code == 200, r.text
    got = BlobMissing.model_validate(r.json())
    assert got.base_version == 1 and got.missing == [new[0], new[1]] and got.present == 3
    # the cached members answer again (no second read of members.bin)
    calls = len(getattr(ctx.store, "calls", []))
    r = await client.post(f"/v1/datasets/{ds}/blobs/missing", json={"sha256": [held[1]]})
    assert r.json() == {"base_version": 1, "missing": [], "present": 1}
    assert not [c for c in getattr(ctx.store, "calls", [])[calls:] if c[0] == "get"]
    # base_version 0: everything is missing
    r = await client.post(
        f"/v1/datasets/{ds}/blobs/missing", json={"sha256": query, "base_version": 0}
    )
    assert r.json() == {"base_version": None, "missing": query, "present": 0}
    # explicit versions: unknown → 404, not a collection → 409; duplicates → 422
    r = await client.post(
        f"/v1/datasets/{ds}/blobs/missing", json={"sha256": query, "base_version": 5}
    )
    assert r.status_code == 404
    flat, _ = await ready_dataset(ctx, "flat")
    r = await client.post(
        f"/v1/datasets/{flat.id}/blobs/missing", json={"sha256": query, "base_version": 1}
    )
    assert r.status_code == 409
    r = await client.post(f"/v1/datasets/{flat.id}/blobs/missing", json={"sha256": query})
    assert r.json()["base_version"] is None and r.json()["missing"] == query
    r = await client.post(f"/v1/datasets/{ds}/blobs/missing", json={"sha256": [new[0]] * 2})
    assert r.status_code == 422


def test_member_search_is_exact_over_sorted_records() -> None:
    shas = digests(50)
    data = members_bin(shas)
    assert all(datasets_api._has_member(data, bytes.fromhex(s)) for s in shas)
    assert not any(datasets_api._has_member(data, bytes.fromhex(s)) for s in digests(20, "x"))
    assert not datasets_api._has_member(b"", bytes.fromhex(shas[0]))


# =============================================================== reading a collection


async def test_a_collection_version_reads_as_manifest_info_and_packs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds, dv, _ = await ready_collection(ctx, name="co2")
    got = DatasetVersion.model_validate((await client.get(f"/v1/datasets/{ds}/versions/1")).json())
    assert got.layout == "collection" and got.collection is not None
    assert got.collection.n_images == 3
    for name in ("manifest.parquet", "collection.json"):
        r = await client.get(f"/v1/datasets/{ds}/versions/1/artifacts/{name}")
        assert r.status_code == 200, (name, r.text)
    r = await client.get(f"/v1/datasets/{ds}/versions/1/artifacts/members.bin")
    assert r.status_code == 404
    inputs = dataset_inputs(dv)
    assert set(inputs) == {"dataset", "dataset.info", "dataset.pack.0000"}
    assert inputs["dataset"]["name"].endswith("/manifest.parquet")
    assert set(dataset_inputs(dv, "scenario")) == {
        "scenario", "scenario.info", "scenario.pack.0000",
    }  # fmt: skip
    assert input_layouts(inputs) == {"dataset": "collection"}
    with pytest.raises(MapiError, match="not image collections") as exc:
        data_ref(dv)
    assert exc.value.code == "SCHEMA_INVALID"
    # a fit of a spec on the collection version pins the collection inputs
    from mapi_server_testkit.builders import trend_spec

    async with ctx.db.session() as s:  # trend_spec adds its own co2 dataset: rename ours
        row = await s.get(m.Dataset, ds)
        assert row is not None
        row.name = "co2-images"
    # a collection takes row splits only (no time holdout) and no evaluation without one
    yaml = (
        TREND_SPEC.replace("co2@latest", "co2-images@latest")
        .replace("  split: {kind: time_holdout, dim: t, last: 4}\n", "")
        .replace("evaluate: {metrics: [rmse]}\n", "")
    )
    spec_id = await trend_spec(ctx, yaml=yaml)
    run = await _run(ctx, await submit_fit(ctx, spec_id))
    assert set(run.inputs) == {"dataset", "dataset.info", "dataset.pack.0000"}
    assert run.dataset_id == ds and input_layouts(run.inputs) == {"dataset": "collection"}


async def test_collections_are_not_side_tables_and_do_not_count_against_data_caps(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx)
    _, dv, _ = await ready_collection(ctx)
    spec = TREND_SPEC.replace(
        "  split:", "  side_tables: {extra: {ref: dataset://default/pets@latest}}\n  split:"
    )
    async with ctx.db.session() as s:
        v = await validate_document(s, ctx.scope, ctx.catalogs, text=spec)
    issues = [i for i in v.issues if i.pointer == "/data/side_tables/extra/ref"]
    assert issues and "not image collections" in issues[0].message
    big = m.DatasetVersion(layout="collection", size_bytes=10**15, version=1)
    assert data_cap_issue(ResourceClass.S, [big]) is None
    assert data_cap_issue(ResourceClass.S, [m.DatasetVersion(size_bytes=10**15)]) is not None
    assert class_for_size(10**15) is None and class_for_size(1) is ResourceClass.S
    assert dv.layout == "collection"


async def test_explore_runs_on_a_collection_version_read_its_packs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds, _, _ = await ready_collection(ctx)
    r = await client.post(f"/v1/datasets/{ds}/versions/1/explore", json={})
    assert r.status_code in (200, 202), r.text
    run = await _run(ctx, r.json()["run"]["id"])
    assert set(run.inputs) == {"dataset", "dataset.info", "dataset.pack.0000"}
