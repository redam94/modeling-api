"""M4 tree datasets on the server: multi-file versions (assemblies), upload inspection, tree
ingest finalization and tree inputs of model runs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.runspec import build_runspec, input_layouts
from mapi_server_testkit.builders import TREND_SCHEMA, TREND_SPEC, simulate_outputs, submit_fit
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.fake_runner import INSPECTION, TREE_SCHEMA
from mapi_server_testkit.identity import grant_role, make_tenant, make_user, session_caller
from mapi_spec.api import DatasetVersion, Run, UploadInspection, UploadSession, new_id
from mapi_spec.run import Actor, RunState, RunTrigger, new_hmac_key
from mapi_spec.storage import dataset_prefix
from sqlalchemy import func, select

NETCDF = {"format": "netcdf"}


async def _dataset(client: Any, name: str = "cubes") -> str:
    r = await client.post("/v1/datasets", json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _upload(
    client: Any, ctx: ServerContext, ds_id: str, *, size: int = 64, arrive: bool = True
) -> UploadSession:
    r = await client.post(
        f"/v1/datasets/{ds_id}/uploads",
        json={"filename": "cube.nc", "content_type": "application/x-netcdf", "size_bytes": size},
    )
    assert r.status_code == 201, r.text
    up = UploadSession.model_validate(r.json())
    if arrive:
        await _arrive(ctx, up)
    return up


async def _arrive(ctx: ServerContext, up: UploadSession) -> None:
    await ctx.store.put(ctx.buckets.quarantine, up.upload.name, b"CDF\x01" + os.urandom(20))


def _assembly(*uploads: UploadSession) -> dict[str, Any]:
    """A concat of the first two uploads along ``year`` at the root, then one upload per
    further node."""
    sources: list[dict[str, Any]] = []
    for i, up in enumerate(uploads):
        if i < 2:
            sources.append(
                {
                    "upload_id": up.id,
                    "mapping": NETCDF,
                    "node": "/observations",
                    "combine": "concat",
                    "concat_dim": "year",
                    "concat_label": 2024 + i,
                }
            )
        else:
            sources.append({"upload_id": up.id, "mapping": NETCDF, "node": f"/extra{i}"})
    return {"sources": sources}


async def _uploads(ctx: ServerContext, ids: list[str]) -> dict[str, m.Upload]:
    async with ctx.db.session() as s:
        rows = (await s.execute(select(m.Upload).where(m.Upload.id.in_(ids)))).scalars().all()
        return {u.id: u for u in rows}


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


# ================================================================== assembly versions


async def test_an_assembly_consumes_every_upload_and_starts_one_ingest_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    ups = [await _upload(client, ctx, ds) for _ in range(3)]
    body = {"assembly": _assembly(*ups), "message": "two years and an instrument"}
    r = await client.post(
        f"/v1/datasets/{ds}/versions", json=body, headers={"Idempotency-Key": "asm-1"}
    )
    assert r.status_code == 202, r.text
    dv = DatasetVersion.model_validate(r.json())
    assert dv.status == "ingesting" and dv.sources == [u.id for u in ups]
    assert dv.layout == "dataset" and dv.tree_schema is None  # until the ingest says so
    async with ctx.db.session() as s:
        run = await s.get(m.Run, dv.ingest_run_id)
        assert run is not None and run.kind == "ingest" and run.resource_class == "S"
        assert set(run.inputs) == {"upload.0", "upload.1", "upload.2"}
        for i, up in enumerate(ups):
            assert run.inputs[f"upload.{i}"]["name"] == up.upload.name
        assert run.payload["assembly"]["sources"][1]["concat_label"] == 2025
        assert "mapping" not in run.payload and "source" not in run.payload
        row = await s.get(m.DatasetVersion, (ds, dv.version))
        assert row is not None and row.upload_id == ups[0].id
        assert row.mapping is not None and "assembly" in row.mapping
    rows = await _uploads(ctx, [u.id for u in ups])
    assert {(u.status, u.consumed_by_version) for u in rows.values()} == {("consumed", 1)}
    # the idempotent replay answers with the same version; a new request is refused
    again = await client.post(
        f"/v1/datasets/{ds}/versions", json=body, headers={"Idempotency-Key": "asm-1"}
    )
    assert again.status_code == 200 and again.json()["version"] == 1
    r = await client.post(f"/v1/datasets/{ds}/versions", json=body)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


async def test_one_bad_source_refuses_the_version_and_consumes_nothing(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    other = await _dataset(client, "other")
    a = await _upload(client, ctx, ds)
    b = await _upload(client, ctx, ds, arrive=False)
    foreign = await _upload(client, ctx, other)
    url = f"/v1/datasets/{ds}/versions"

    async def unconsumed() -> None:
        rows = await _uploads(ctx, [a.id, b.id, foreign.id])
        assert all(u.status == "pending" and u.consumed_by_version is None for u in rows.values())

    # b's file has not arrived
    r = await client.post(url, json={"assembly": _assembly(a, b)})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text
    assert b.id in r.json()["detail"]
    await unconsumed()
    # an upload of another dataset, and one that does not exist, are not found
    r = await client.post(url, json={"assembly": _assembly(a, foreign)})
    assert r.status_code == 404, r.text
    missing = UploadSession.model_construct(id="upl_missing")
    r = await client.post(url, json={"assembly": _assembly(a, missing)})
    assert r.status_code == 404, r.text
    await unconsumed()
    # an upload used twice is refused by the request schema
    r = await client.post(url, json={"assembly": _assembly(a, a)})
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    # an expired upload session
    c = await _upload(client, ctx, ds, arrive=False)
    async with ctx.db.session() as s:
        row = await s.get(m.Upload, c.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    r = await client.post(url, json={"assembly": _assembly(a, c)})
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    await unconsumed()
    assert (await client.get(f"{url}")).json()["items"] == []
    # once b arrives the same assembly goes through
    await _arrive(ctx, b)
    r = await client.post(url, json={"assembly": _assembly(a, b)})
    assert r.status_code == 202, r.text
    # and a consumed upload cannot join another version
    d = await _upload(client, ctx, ds)
    r = await client.post(url, json={"assembly": _assembly(d, a)})
    assert r.status_code == 409 and a.id in r.json()["detail"]
    assert (await _uploads(ctx, [d.id]))[d.id].status == "pending"


async def test_the_sources_together_must_fit_one_upload_cap(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mapi_server.api.datasets as datasets_api

    monkeypatch.setattr(datasets_api, "MAX_UPLOAD_BYTES", 100)
    ds = await _dataset(client)
    a = await _upload(client, ctx, ds, size=64)
    b = await _upload(client, ctx, ds, size=64)
    r = await client.post(f"/v1/datasets/{ds}/versions", json={"assembly": _assembly(a, b)})
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE", r.text
    rows = await _uploads(ctx, [a.id, b.id])
    assert {u.status for u in rows.values()} == {"pending"}
    # one of them alone fits
    r = await client.post(
        f"/v1/datasets/{ds}/versions", json={"assembly": {"sources": _assembly(a)["sources"]}}
    )
    assert r.status_code == 202, r.text


async def test_the_resource_class_follows_the_summed_size(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass

    s_cap = RESOURCE_CLASSES[ResourceClass.S].max_dataset_bytes
    ds = await _dataset(client)
    a = await _upload(client, ctx, ds, size=s_cap // 2 + 1)
    b = await _upload(client, ctx, ds, size=s_cap // 2 + 1)
    r = await client.post(f"/v1/datasets/{ds}/versions", json={"assembly": _assembly(a, b)})
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        run = await s.get(m.Run, r.json()["ingest_run_id"])
        assert run is not None and run.resource_class == "M"


# ========================================================================= tree ingest


async def _assembled_version(client: Any, ctx: ServerContext) -> tuple[str, DatasetVersion]:
    ds = await _dataset(client)
    ups = [await _upload(client, ctx, ds) for _ in range(2)]
    r = await client.post(f"/v1/datasets/{ds}/versions", json={"assembly": _assembly(*ups)})
    assert r.status_code == 202, r.text
    return ds, DatasetVersion.model_validate(r.json())


async def test_a_tree_ingest_makes_a_tree_version(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds, dv = await _assembled_version(client, ctx)
    assert dv.ingest_run_id is not None
    run, att = await _finalizing(ctx, dv.ingest_run_id)
    await simulate_outputs(ctx, run, att, mode="tree")
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    got = DatasetVersion.model_validate((await client.get(f"/v1/datasets/{ds}/versions/1")).json())
    assert got.status == "ready" and got.layout == "tree"
    assert got.tree_schema is not None and set(got.tree_schema.nodes) == set(TREE_SCHEMA["nodes"])
    assert got.tree_schema.default_node() == "/observations"
    assert got.data_schema is not None and got.data_schema.vars == {}  # the (empty) root
    assert got.summary is not None and got.summary.vars == []  # root fields only
    assert len(got.sources) == 2
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (ds, 1))
        assert row is not None
        prefix = dataset_prefix(row.tenant_id, row.sha256.removeprefix("sha256:"))  # type: ignore[union-attr]
        assert {"data.tree.zip", "tree_schema.json", "schema.json"} <= set(row.objects)
        assert "data.zarr.zip" not in row.objects
        assert row.objects["data.tree.zip"]["name"] == prefix + "data.tree.zip"
        assert row.objects["tree_schema.json"]["name"] == prefix + "tree_schema.json"
    for name in ("data.tree.zip", "tree_schema.json", "summary.json"):
        r = await client.get(f"/v1/datasets/{ds}/versions/1/artifacts/{name}")
        assert r.status_code == 200, (name, r.text)
    r = await client.get(f"/v1/datasets/{ds}/versions/1/artifacts/data.zarr.zip")
    assert r.status_code == 404


@pytest.mark.parametrize(
    "case",
    ["missing_ancestor", "unequal_dim", "not_json", "too_big", "no_tree_schema", "root_mismatch"],
)
async def test_a_bad_tree_schema_fails_the_ingest(
    client: httpx.AsyncClient, ctx: ServerContext, case: str
) -> None:
    ds, dv = await _assembled_version(client, ctx)
    assert dv.ingest_run_id is not None
    run, att = await _finalizing(ctx, dv.ingest_run_id)
    nodes = TREE_SCHEMA["nodes"]
    replace: dict[str, bytes] = {}
    if case == "missing_ancestor":
        bad = {"nodes": {"/": {}, "/a/b": nodes["/psf"]}}
        replace["tree_schema.json"] = json.dumps(bad).encode()
    elif case == "unequal_dim":
        child = {
            "dims": {"t": {"kind": "index"}},
            "vars": {"x": {"dims": ["t"], "dtype": "float64"}},
        }
        root = {**TREND_SCHEMA}
        bad = {"nodes": {"/": root, "/c": child}}
        replace["tree_schema.json"] = json.dumps(bad).encode()
        replace["schema.json"] = json.dumps(root).encode()
    elif case == "not_json":
        replace["tree_schema.json"] = b"{nodes:"
    elif case == "too_big":
        replace["tree_schema.json"] = b" " * (ctx.settings.max_sidecar_bytes + 1)
    elif case == "root_mismatch":
        replace["schema.json"] = json.dumps(TREND_SCHEMA).encode()
    await simulate_outputs(ctx, run, att, mode="tree", replace=replace)
    if case == "no_tree_schema":
        # claim a tree without its schema: rewrite the manifest without tree_schema.json
        await _drop_output(ctx, run, att, "tree_schema.json")
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED
    got = (await client.get(f"/v1/datasets/{ds}/versions/1")).json()
    assert got["status"] == "failed" and got["issues"][0]["code"] == "INTEGRITY_FAILURE"
    assert got["layout"] == "dataset" and got["tree_schema"] is None


async def _drop_output(ctx: ServerContext, run: m.Run, att: m.RunAttempt, name: str) -> None:
    """Re-sign the attempt's manifest without ``name`` (and delete the object)."""
    from mapi_spec.run import CompletePayload, OutputManifest, sign_output_manifest
    from mapi_spec.storage import crc32c, crc32c_to_base64, run_prefix

    prefix = run_prefix(run.tenant_id, run.id, att.attempt)
    raw = await ctx.store.get(ctx.buckets.runs, prefix + "manifest.json")
    manifest = OutputManifest.model_validate_json(raw)
    objects = {k: v for k, v in manifest.objects.items() if k != name}
    signed = sign_output_manifest(
        manifest.model_copy(update={"objects": objects, "signature": None}), att.hmac_key
    )
    body = signed.model_dump_json().encode()
    await ctx.store.delete(ctx.buckets.runs, prefix + name)
    await ctx.store.delete(ctx.buckets.runs, prefix + "manifest.json")
    await ctx.store.put(ctx.buckets.runs, prefix + "manifest.json", body)
    complete = CompletePayload(
        status=manifest.status,
        manifest_name=prefix + "manifest.json",
        manifest_crc32c=crc32c_to_base64(crc32c(body)),
        objects=len(objects),
        total_bytes=sum(o.size for o in objects.values()),
    )
    async with ctx.db.session() as s:
        row = await s.get(m.RunAttempt, (run.id, att.attempt))
        assert row is not None
        row.complete_payload = complete.model_dump(mode="json")


async def test_a_single_file_ingest_keeps_the_dataset_layout(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    up = await _upload(client, ctx, ds)
    r = await client.post(
        f"/v1/datasets/{ds}/versions", json={"upload_id": up.id, "mapping": NETCDF}
    )
    run, att = await _finalizing(ctx, r.json()["ingest_run_id"])
    # a dataset-layout ingest that also claims a tree schema is refused
    await simulate_outputs(
        ctx,
        run,
        att,
        add={"tree_schema.json": (json.dumps(TREE_SCHEMA).encode(), "application/json")},
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED


# ============================================================================ inspect


async def test_inspect_runs_once_and_serves_the_inspection(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    up = await _upload(client, ctx, ds, arrive=False)
    base = f"/v1/datasets/{ds}/uploads/{up.id}"
    r = await client.get(base + "/inspection")
    assert r.status_code == 404  # never requested
    r = await client.post(base + "/inspect")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"  # no file yet
    await _arrive(ctx, up)
    r = await client.post(base + "/inspect", headers={"Idempotency-Key": "insp-1"})
    assert r.status_code == 202, r.text
    run = Run.model_validate(r.json())
    assert run.kind == "inspect" and run.state == "queued" and run.resource_class == "S"
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run.id)
        assert row is not None
        assert row.timeout_s == ctx.settings.inspect_timeout_s
        assert row.priority == "interactive" and set(row.inputs) == {"upload"}
        assert row.labels == {"upload": up.id} and row.payload["source"]["filename"] == "cube.nc"
    r = await client.get(base + "/inspection")
    assert r.status_code == 409 and r.json()["code"] == "NOT_READY", r.text
    # asking again while it runs returns the same run (and so does the replayed key)
    for headers in ({}, {"Idempotency-Key": "insp-1"}):
        again = await client.post(base + "/inspect", headers=headers)
        assert again.status_code == 202 and again.json()["id"] == run.id
    att_run, att = await _finalizing(ctx, run.id)
    await simulate_outputs(ctx, att_run, att)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    r = await client.get(base + "/inspection")
    assert r.status_code == 200, r.text
    assert UploadInspection.model_validate(r.json()) == UploadInspection.model_validate(INSPECTION)
    # a finished inspection is returned as is: no second run
    again = await client.post(base + "/inspect")
    assert again.status_code == 202 and again.json()["id"] == run.id
    async with ctx.db.session() as s:
        count = await s.scalar(
            select(func.count()).select_from(m.Run).where(m.Run.kind == "inspect")
        )
        assert count == 1
    # the inspected upload still becomes a version
    r = await client.post(
        f"/v1/datasets/{ds}/versions", json={"upload_id": up.id, "mapping": NETCDF}
    )
    assert r.status_code == 202
    assert (await client.get(base + "/inspection")).status_code == 200


async def test_a_failed_inspection_can_be_asked_for_again(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    up = await _upload(client, ctx, ds)
    base = f"/v1/datasets/{ds}/uploads/{up.id}"
    first = Run.model_validate((await client.post(base + "/inspect")).json())
    async with ctx.db.session() as s:
        row = await load_run(s, None, first.id, for_update=True)
        await apply_transition(s, row, RunTrigger.CANCEL, Actor.API)
        await apply_transition(s, row, RunTrigger.CANCEL_CONFIRMED, Actor.RECONCILER)
    r = await client.get(base + "/inspection")
    assert r.status_code == 404  # it ended without an inspection
    second = await client.post(base + "/inspect")
    assert second.status_code == 202 and second.json()["id"] != first.id
    # a consumed upload that was never inspected cannot be inspected any more
    other = await _upload(client, ctx, ds)
    r = await client.post(
        f"/v1/datasets/{ds}/versions", json={"upload_id": other.id, "mapping": NETCDF}
    )
    assert r.status_code == 202
    r = await client.post(f"/v1/datasets/{ds}/uploads/{other.id}/inspect")
    assert r.status_code == 409


async def test_a_bad_inspection_fails_the_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    up = await _upload(client, ctx, ds)
    run_id = (await client.post(f"/v1/datasets/{ds}/uploads/{up.id}/inspect")).json()["id"]
    run, att = await _finalizing(ctx, run_id)
    bad = {**INSPECTION, "arrays": [{"path": "/x", "shape": [-1], "dtype": "f4"}]}
    await simulate_outputs(ctx, run, att, replace={"inspection.json": json.dumps(bad).encode()})
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED
    async with ctx.db.session() as s:
        row = await s.get(m.Upload, up.id)
        assert row is not None and row.inspection is None
    # the upload owns its inspect run: a stand-alone retry is refused with where to ask
    r = await client.post(f"/v1/runs/{run_id}:retry", json={})
    assert r.status_code == 409 and f"uploads/{up.id}/inspect" in r.json()["detail"], r.text
    again = await client.post(f"/v1/datasets/{ds}/uploads/{up.id}/inspect")
    assert again.status_code == 202 and again.json()["id"] != run_id


async def test_inspect_needs_dataset_update_and_stays_in_its_project(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await make_tenant(ctx, "alpha")
    await make_tenant(ctx, "beta")
    eve = await make_user(ctx, "eve@alpha.test")
    vic = await make_user(ctx, "vic@alpha.test")
    bob = await make_user(ctx, "bob@beta.test")
    await grant_role(ctx, eve, "alpha", None, "runner")
    await grant_role(ctx, vic, "alpha", None, "viewer")
    await grant_role(ctx, bob, "beta", None, "admin")
    editor = await session_caller(ctx, client, eve, tenant_id="alpha", project_id="main")
    viewer = await session_caller(ctx, client, vic, tenant_id="alpha", project_id="main")
    outsider = await session_caller(ctx, client, bob, tenant_id="beta", project_id="main")
    ds = await _dataset(editor)
    up = await _upload(editor, ctx, ds)
    base = f"/v1/datasets/{ds}/uploads/{up.id}"
    r = await viewer.post(base + "/inspect")
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", r.text
    r = await outsider.post(base + "/inspect")
    assert r.status_code == 404, r.text
    r = await editor.post(base + "/inspect")
    assert r.status_code == 202, r.text
    r = await viewer.get(base + "/inspection")
    assert r.status_code == 409 and r.json()["code"] == "NOT_READY"
    r = await outsider.get(base + "/inspection")
    assert r.status_code == 404
    # an upload is addressed through its own dataset only
    ds2 = await _dataset(editor, "second")
    r = await editor.get(f"/v1/datasets/{ds2}/uploads/{up.id}/inspection")
    assert r.status_code == 404


# ======================================================================== tree inputs

TREE_SPEC = TREND_SPEC.replace(
    "  dataset: dataset://default/co2@latest\n",
    "  dataset: dataset://default/co2@latest\n  nodes: {s: {path: /series}}\n",
)


async def _ready_tree(ctx: ServerContext, name: str = "co2") -> tuple[m.Dataset, m.DatasetVersion]:
    """A ready tree version whose ``/series`` node is the trend data."""
    data = os.urandom(1024)
    sha = hashlib.sha256(data).hexdigest()
    prefix = dataset_prefix(ctx.scope.tenant_id, sha)
    meta = await ctx.store.put(ctx.buckets.data, prefix + "data.tree.zip", data)
    tree = {"nodes": {"/": {}, "/series": TREND_SCHEMA}}
    async with ctx.db.session() as s:
        ds = m.Dataset(
            id=new_id("ds"),
            tenant_id=ctx.scope.tenant_id,
            project_id=ctx.scope.project_id,
            name=name,
            labels={},
            latest_version=1,
            next_version=2,
        )
        s.add(ds)
        await s.flush()
        dv = m.DatasetVersion(
            dataset_id=ds.id,
            version=1,
            tenant_id=ds.tenant_id,
            project_id=ds.project_id,
            status="ready",
            sha256=f"sha256:{sha}",
            size_bytes=len(data),
            data_schema={},
            layout="tree",
            tree_schema=tree,
            sources=["upl_a", "upl_b"],
            objects={
                "data.tree.zip": {
                    "bucket": meta.bucket,
                    "name": meta.name,
                    "generation": meta.generation,
                    "size": meta.size,
                    "crc32c": meta.crc32c,
                    "md5": meta.md5_hash,
                    "content_type": None,
                }
            },
            issues=[],
            ready_at=datetime.now(UTC),
        )
        s.add(dv)
    return ds, dv


async def _store_spec(ctx: ServerContext, yaml: str) -> tuple[str, Any]:
    from mapi_server.services.specs import validate_document

    async with ctx.db.session() as s:
        v = await validate_document(s, ctx.scope, ctx.catalogs, text=yaml)
        spec = m.Spec(
            id=new_id("spc"),
            tenant_id=ctx.scope.tenant_id,
            project_id=ctx.scope.project_id,
            name="trend",
            kind="ModelSpec",
            latest_revision=1,
        )
        s.add(spec)
        await s.flush()
        s.add(
            m.SpecRevision(
                spec_id=spec.id,
                revision=1,
                tenant_id=spec.tenant_id,
                project_id=spec.project_id,
                kind="ModelSpec",
                hash=v.hash or "",
                yaml=yaml,
                document=v.document,
                valid=v.valid,
                issues=[],
                resolved=v.resolved.model_dump(mode="json") if v.resolved else None,
            )
        )
        return spec.id, v


async def test_a_fit_on_a_tree_version_reads_data_tree_zip(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    _, dv = await _ready_tree(ctx)
    spec_id, v = await _store_spec(ctx, TREE_SPEC)
    assert v.valid, v.issues
    run_id = await submit_fit(ctx, spec_id)
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        assert run.inputs["dataset"]["name"] == dv.objects["data.tree.zip"]["name"]
        att = m.RunAttempt(
            run_id=run.id,
            attempt=1,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            nonce="ab" * 32,
            hmac_key=new_hmac_key(),
            executor="fake",
            deadline=datetime.now(UTC) + timedelta(hours=1),
        )
        rs = await build_runspec(ctx.store, run=run, attempt=att, runs_bucket=ctx.buckets.runs)
    assert rs.input_layouts == {"dataset": "tree"}
    assert "input_layouts" in rs.model_dump(mode="json")
    # the spec validator resolved the pipeline input from the selected node
    r = await client.post("/v1/specs:validate", json={"yaml": TREE_SPEC})
    assert r.status_code == 200 and r.json()["valid"], r.text


async def test_a_tree_version_without_nodes_is_a_static_issue(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _ready_tree(ctx)
    r = await client.post("/v1/specs:validate", json={"yaml": TREND_SPEC})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["valid"]
    assert any("nodes" in i["message"] for i in body["issues"]), body["issues"]


def test_input_layouts_mark_only_tree_objects() -> None:
    inputs = {
        "dataset": {"bucket": "d", "name": "t/x/datasets/ab/data.tree.zip", "generation": 1},
        "side.prices": {"bucket": "d", "name": "t/x/datasets/cd/data.zarr.zip", "generation": 1},
        "upload.0": {"bucket": "q", "name": "t/x/uploads/upl_1", "generation": 1},
    }
    assert input_layouts(inputs) == {"dataset": "tree"}
    assert input_layouts({}) == {}


async def test_a_dataset_runspec_has_no_layouts(ctx: ServerContext) -> None:
    """Runs on plain datasets keep the RunSpec older runners understand."""
    from mapi_server_testkit.builders import queued_fit

    run_id = await queued_fit(ctx)
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        att = m.RunAttempt(
            run_id=run.id,
            attempt=1,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            nonce="cd" * 32,
            hmac_key=new_hmac_key(),
            executor="fake",
            deadline=datetime.now(UTC) + timedelta(hours=1),
        )
        rs = await build_runspec(ctx.store, run=run, attempt=att, runs_bucket=ctx.buckets.runs)
    assert rs.input_layouts == {} and "input_layouts" not in rs.model_dump(mode="json")
