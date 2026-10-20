"""v0.15: finalizing a collection ingest. The finalizer never parses the manifest; it checks
every pack ``collection.json`` lists against the store (an uploaded part's digest from the
store's metadata of the pinned generation; a base pack from the base version) and copies
parts create-only to their shared, content-addressed pack address."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer, dataset_sha
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import TREND_SCHEMA, TREND_SUMMARY, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.image_collections import (
    collection_info,
    digest_of,
    digests,
    members_bin,
    pack_info,
    ready_collection,
    upload,
)
from mapi_spec.api import DatasetVersion
from mapi_spec.collection import CollectionInfo, pack_digest, pack_object_name
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.storage import NotFound, dataset_prefix

pytestmark = pytest.mark.integration

JSON = "application/json"


async def _finalizing(ctx: ServerContext, run_id: str) -> tuple[m.Run, m.RunAttempt]:
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        assert run.state == RunState.PROVISIONING.value, run.state
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    return run, att


async def _version(
    client: httpx.AsyncClient, ctx: ServerContext, ds: str, parts: list[bytes], **source: Any
) -> str:
    """Create a collection version of ``parts``; returns its ingest run id."""
    manifest = await upload(client, ctx, ds, b"path,sha256\n", filename="m.csv")
    ups = [await upload(client, ctx, ds, p) for p in parts]
    body = {
        "collection": {
            "manifest_upload_id": manifest.id,
            "part_upload_ids": [u.id for u in ups],
            **source,
        }
    }
    r = await client.post(f"/v1/datasets/{ds}/versions", json=body)
    assert r.status_code == 202, r.text
    return str(r.json()["ingest_run_id"])


def _outputs(info: CollectionInfo, shas: list[str], **replace: bytes) -> dict[str, Any]:
    out = {
        "manifest.parquet": (os.urandom(300), "application/vnd.apache.parquet"),
        "collection.json": (info.model_dump_json().encode(), JSON),
        "members.bin": (members_bin(shas), "application/octet-stream"),
        "schema.json": (json.dumps(TREND_SCHEMA).encode(), JSON),
        "summary.json": (json.dumps(TREND_SUMMARY).encode(), JSON),
        "ui/explore/index.json": (b"{}", JSON),
    }
    for name, data in replace.items():
        out[name.replace("_", ".")] = (data, out[name.replace("_", ".")][1])
    return out


async def _dataset(client: httpx.AsyncClient) -> str:
    r = await client.post("/v1/datasets", json={"name": "pets"})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_a_collection_ingest_copies_its_parts_to_shared_packs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    parts = [b"PK" + os.urandom(120), b"PK" + os.urandom(80)]
    run_id = await _version(client, ctx, ds, parts, base_version=0)
    run, att = await _finalizing(ctx, run_id)
    shas = digests(5)
    info = collection_info(
        [pack_info(parts[0], images=3), pack_info(parts[1], images=2, input="part.0001")], 5
    )
    await simulate_outputs(ctx, run, att, add=_outputs(info, shas), defaults=False)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    got = DatasetVersion.model_validate((await client.get(f"/v1/datasets/{ds}/versions/1")).json())
    assert got.status == "ready" and got.layout == "collection"
    assert got.collection == info and got.size_bytes == info.total_bytes
    assert got.data_schema is not None and got.summary is not None
    tenant = ctx.scope.tenant_id
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (ds, 1))
        assert row is not None and row.sha256 is not None
    prefix = dataset_prefix(tenant, row.sha256.removeprefix("sha256:"))
    assert row.objects["manifest.parquet"]["name"] == prefix + "manifest.parquet"
    assert row.objects["ui/explore/index.json"]["name"].startswith(prefix + f"runs/{run.id}/")
    for data in parts:
        key = f"pack/{digest_of(data)}"
        obj = row.objects[key]
        assert obj["bucket"] == ctx.buckets.data
        assert obj["name"] == pack_object_name(tenant, digest_of(data))
        assert await ctx.store.get(obj["bucket"], obj["name"]) == data
    assert sum(k.startswith("pack/") for k in row.objects) == 2
    meta = await ctx.store.stat(ctx.buckets.runs, att_prefix(run, att) + "manifest.parquet")
    assert row.sha256 == f"sha256:{dataset_sha(meta)}"
    # a replay of the finalization does nothing
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is None
    # a later version uploading the same part bytes lands on the same pack (create-only copy)
    run_id = await _version(client, ctx, ds, [parts[0]], base_version=0)
    run, att = await _finalizing(ctx, run_id)
    info = collection_info([pack_info(parts[0], images=3)], 3)
    await simulate_outputs(ctx, run, att, add=_outputs(info, shas[:3]), defaults=False)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED


async def test_the_same_manifest_again_with_other_sidecars_is_a_new_version(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """The manifest addresses the version; collection.json and the schema depend on the base
    version and the options too, so they are each ingest's own and never collide."""
    ds = await _dataset(client)
    part = b"PK" + os.urandom(100)
    manifest = os.urandom(300)
    shas = digests(3)
    names = []
    for columns in ([], ["note"]):
        run_id = await _version(client, ctx, ds, [part], base_version=0)
        run, att = await _finalizing(ctx, run_id)
        info = collection_info([pack_info(part, images=3)], 3).model_copy(
            update={"columns": columns}
        )
        out = _outputs(info, shas, manifest_parquet=manifest)
        await simulate_outputs(ctx, run, att, add=out, defaults=False)
        assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
        async with ctx.db.session() as s:
            row = await s.get(m.DatasetVersion, (ds, len(names) + 1))
            assert row is not None and row.sha256 is not None
            prefix = dataset_prefix(ctx.scope.tenant_id, row.sha256.removeprefix("sha256:"))
            assert row.objects["members.bin"]["name"] == prefix + "members.bin"
            own = prefix + f"runs/{run.id}/"
            assert row.objects["collection.json"]["name"] == own + "collection.json"
            names.append(row.objects["manifest.parquet"]["name"])
    assert names[0] == names[1]  # one shared manifest


def att_prefix(run: m.Run, att: m.RunAttempt) -> str:
    from mapi_spec.storage import run_prefix

    return run_prefix(run.tenant_id, run.id, att.attempt)


async def test_base_packs_come_from_the_base_version(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    held = digests(3)
    ds, base, base_packs = await ready_collection(ctx, shas=held)
    part = b"PK" + os.urandom(64)
    run_id = await _version(client, ctx, ds, [part])
    run, att = await _finalizing(ctx, run_id)
    assert run.payload["collection"]["base_version"] == 1
    info = collection_info(
        [pack_info(part, images=1), pack_info(base_packs[0], images=3, source="base")],
        4,
        base_version=1,
    )
    shas = [*held, *digests(1, "new")]
    await simulate_outputs(ctx, run, att, add=_outputs(info, shas), defaults=False)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (ds, 2))
        assert row is not None and row.layout == "collection"
    key = f"pack/{digest_of(base_packs[0])}"
    assert row.objects[key] == base.objects[key]  # shared, not copied again
    assert row.objects[f"pack/{digest_of(part)}"]["name"] == pack_object_name(
        ctx.scope.tenant_id, digest_of(part)
    )


async def test_a_pinned_base_that_no_pack_comes_from_is_not_an_integrity_failure(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """The server pins the latest ready version as the base, but when every image is new (or
    in a part) the ingest uses no base pack and collection.json names no base version."""
    ds, _, _ = await ready_collection(ctx)
    part = b"PK" + os.urandom(64)
    run_id = await _version(client, ctx, ds, [part])
    run, att = await _finalizing(ctx, run_id)
    assert run.payload["collection"]["base_version"] == 1
    info = collection_info([pack_info(part, images=2)], 2)
    assert info.base_version is None
    await simulate_outputs(ctx, run, att, add=_outputs(info, digests(2, "new")), defaults=False)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    got = DatasetVersion.model_validate((await client.get(f"/v1/datasets/{ds}/versions/2")).json())
    assert got.status == "ready" and got.collection is not None
    assert got.collection.base_version is None


async def test_a_version_is_charged_the_whole_parts_it_stores(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A part is copied whole to the tenant's packs, so the version is charged its bytes,
    also the entries the manifest does not reference."""
    ds = await _dataset(client)
    part = b"PK" + os.urandom(4000)
    run_id = await _version(client, ctx, ds, [part], base_version=0)
    run, att = await _finalizing(ctx, run_id)
    pack = pack_info(part, images=1).model_copy(update={"entries": 60})
    info = collection_info([pack], 1).model_copy(update={"total_bytes": 10})
    await simulate_outputs(ctx, run, att, add=_outputs(info, digests(1)), defaults=False)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    got = DatasetVersion.model_validate((await client.get(f"/v1/datasets/{ds}/versions/1")).json())
    assert got.size_bytes == len(part)


async def _failed(ctx: ServerContext, run: m.Run, att: m.RunAttempt, needle: str) -> None:
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run.id)
        assert row is not None and row.problem is not None
        assert row.problem["code"] == "INTEGRITY_FAILURE", row.problem
        assert needle in row.problem["detail"], row.problem["detail"]
        dv = await s.get(m.DatasetVersion, (row.dataset_id, row.dataset_version))
        assert dv is not None and dv.status != "ready"


@pytest.mark.parametrize("case", ["wrong_digest", "wrong_input", "base_input", "members"])
async def test_a_pack_the_store_does_not_back_fails_the_ingest(
    client: httpx.AsyncClient, ctx: ServerContext, case: str
) -> None:
    ds = await _dataset(client)
    part = b"PK" + os.urandom(90)
    run_id = await _version(client, ctx, ds, [part], base_version=0)
    run, att = await _finalizing(ctx, run_id)
    pack = pack_info(part, images=2)
    shas = digests(2)
    replace: dict[str, bytes] = {}
    if case == "wrong_digest":  # a claim about other bytes than the pinned upload's
        other = b"PK" + os.urandom(90)
        pack = pack_info(other, images=2)
        needle = "does not match the upload"
    elif case == "wrong_input":
        pack = pack.model_copy(update={"input": "part.0003"})
        needle = "names no part input"
    elif case == "base_input":  # the manifest upload is not a part
        pack = pack.model_copy(update={"input": "manifest"})
        needle = "names no part input"
    else:
        replace["members_bin"] = os.urandom(33)
        needle = "members.bin"
    info = collection_info([pack], 2)
    await simulate_outputs(ctx, run, att, add=_outputs(info, shas, **replace), defaults=False)
    await _failed(ctx, run, att, needle)
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.data, pack_object_name(ctx.scope.tenant_id, pack.digest))


async def test_a_base_pack_the_base_version_lacks_fails_the_ingest(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds, _, _ = await ready_collection(ctx)
    part = b"PK" + os.urandom(64)
    run_id = await _version(client, ctx, ds, [part])
    run, att = await _finalizing(ctx, run_id)
    stranger = b"PK" + os.urandom(64)  # a pack of no version of this dataset
    info = collection_info(
        [pack_info(part, images=1), pack_info(stranger, images=2, source="base")],
        3,
        base_version=1,
    )
    await simulate_outputs(ctx, run, att, add=_outputs(info, digests(3)), defaults=False)
    await _failed(ctx, run, att, "is not a pack of base version v1")


async def test_collection_outputs_are_checked(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    part = b"PK" + os.urandom(64)
    run_id = await _version(client, ctx, ds, [part], base_version=0)
    run, att = await _finalizing(ctx, run_id)
    info = collection_info([pack_info(part, images=2)], 2)
    outputs = _outputs(info, digests(2))
    outputs["data.zarr.zip"] = (os.urandom(64), "application/zip")
    await simulate_outputs(ctx, run, att, add=outputs, defaults=False)
    await _failed(ctx, run, att, "does not upload data.zarr.zip")


async def test_a_bad_collection_json_fails_the_ingest(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    part = b"PK" + os.urandom(64)
    run_id = await _version(client, ctx, ds, [part], base_version=0)
    run, att = await _finalizing(ctx, run_id)
    info = collection_info([pack_info(part, images=2)], 2)
    bad = json.loads(info.model_dump_json())
    bad["n_images"] = 0
    outputs = _outputs(info, digests(2), collection_json=json.dumps(bad).encode())
    await simulate_outputs(ctx, run, att, add=outputs, defaults=False)
    await _failed(ctx, run, att, "does not match its schema")


def test_dataset_sha_is_the_pack_digest_basis() -> None:
    from mapi_spec.storage import ObjectMeta

    meta = ObjectMeta.model_validate(
        {
            "bucket": "mapi-data",
            "name": "t/default/x",
            "generation": 1,
            "size": 7,
            "crc32c": "AAAAAA==",
            "md5_hash": "1B2M2Y8AsgTpgAmY7PhCfg==",
            "time_created": "2026-10-20T00:00:00Z",
        }
    )
    assert dataset_sha(meta) == pack_digest(7, "AAAAAA==", "1B2M2Y8AsgTpgAmY7PhCfg==")
    # the formula of every version before v0.15
    import hashlib

    from mapi_spec.loader import canonical_json

    basis = canonical_json({"size": 7, "crc32c": "AAAAAA==", "md5": "1B2M2Y8AsgTpgAmY7PhCfg=="})
    assert dataset_sha(meta) == hashlib.sha256(basis.encode()).hexdigest()
