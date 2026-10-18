"""Datasets: create, upload sessions into quarantine, versions through ingest runs."""

from __future__ import annotations

import httpx
from mapi_server.context import ServerContext
from mapi_server_testkit.builders import ready_dataset
from mapi_spec.api import Dataset, DatasetVersion, UploadSession


async def _dataset(client: httpx.AsyncClient, name: str = "co2") -> Dataset:
    r = await client.post("/v1/datasets", json={"name": name, "description": "monthly CO2"})
    assert r.status_code == 201, r.text
    return Dataset.model_validate(r.json())


async def test_create_list_get_dataset(client: httpx.AsyncClient) -> None:
    ds = await _dataset(client)
    assert ds.ref == "dataset://default/co2" and ds.latest_version is None
    r = await client.post("/v1/datasets", json={"name": "co2"})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    other = await _dataset(client, "rain")
    page = (await client.get("/v1/datasets?limit=1")).json()
    assert [d["id"] for d in page["items"]] == [other.id]  # newest first
    page2 = (await client.get(f"/v1/datasets?limit=1&cursor={page['next_cursor']}")).json()
    assert [d["id"] for d in page2["items"]] == [ds.id] and page2["next_cursor"] is None
    assert (await client.get(f"/v1/datasets/{ds.id}")).json()["name"] == "co2"
    assert (await client.get("/v1/datasets/ds_missing")).status_code == 404
    r = await client.post("/v1/datasets", json={"name": "Bad Name"})
    assert r.status_code == 422


async def test_upload_session_is_a_write_once_signed_put(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    body = {"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 1234}
    r = await client.post(f"/v1/datasets/{ds.id}/uploads", json=body)
    assert r.status_code == 201
    up = UploadSession.model_validate(r.json())
    assert up.status == "pending"
    assert up.upload.name == f"t/default/uploads/{up.id}"
    assert up.upload.headers["x-goog-if-generation-match"] == "0"
    assert up.upload.max_bytes == 1234
    assert ctx.buckets.quarantine in up.upload.url
    too_big = {**body, "size_bytes": 10**12}
    r = await client.post(f"/v1/datasets/{ds.id}/uploads", json=too_big)
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    bad = {**body, "filename": "../etc/passwd"}
    assert (await client.post(f"/v1/datasets/{ds.id}/uploads", json=bad)).status_code == 422


async def test_version_requires_a_finished_upload_and_starts_an_ingest_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    up = UploadSession.model_validate(
        (
            await client.post(
                f"/v1/datasets/{ds.id}/uploads",
                json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 64},
            )
        ).json()
    )
    mapping = {
        "format": "csv",
        "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
        "vars": {"y": {"column": "co2", "role": "target"}},
    }
    body = {"upload_id": up.id, "mapping": mapping}
    r = await client.post(f"/v1/datasets/{ds.id}/versions", json=body)
    assert r.status_code == 409  # nothing uploaded yet
    await ctx.store.put(ctx.buckets.quarantine, up.upload.name, b"date,co2\n2020-01,1\n")
    r = await client.post(
        f"/v1/datasets/{ds.id}/versions", json=body, headers={"Idempotency-Key": "ing-1"}
    )
    assert r.status_code == 202, r.text
    dv = DatasetVersion.model_validate(r.json())
    assert dv.version == 1 and dv.status == "ingesting" and dv.ingest_run_id
    run = (await client.get(f"/v1/runs/{dv.ingest_run_id}")).json()
    assert run["kind"] == "ingest" and run["state"] == "queued" and run["dataset_id"] == ds.id
    # idempotent replay → same version, 200
    again = await client.post(
        f"/v1/datasets/{ds.id}/versions", json=body, headers={"Idempotency-Key": "ing-1"}
    )
    assert again.status_code == 200 and again.json()["version"] == 1
    # the upload is consumed now
    r = await client.post(f"/v1/datasets/{ds.id}/versions", json=body)
    assert r.status_code == 409
    versions = (await client.get(f"/v1/datasets/{ds.id}/versions")).json()
    assert [v["version"] for v in versions["items"]] == [1]
    got = (await client.get(f"/v1/datasets/{ds.id}/versions/1")).json()
    assert got["status"] == "ingesting"
    assert (await client.get(f"/v1/datasets/{ds.id}/versions/2")).status_code == 404
    # no artifacts until the ingest run is verified
    r = await client.get(f"/v1/datasets/{ds.id}/versions/1/artifacts/data.zarr.zip")
    assert r.status_code == 404


async def test_dataset_artifacts_and_views(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    ds, dv = await ready_dataset(ctx)
    r = await client.get(f"/v1/datasets/{ds.id}/versions/1/artifacts/data.zarr.zip")
    assert r.status_code == 200
    link = r.json()
    assert link["generation"] == dv.objects["data.zarr.zip"]["generation"]
    assert "generation=" in link["url"]
    r = await client.get(f"/v1/datasets/{ds.id}/versions/1/artifacts/manifest.json")
    assert r.status_code == 404  # only the well-known dataset objects are served
    r = await client.get(f"/v1/datasets/{ds.id}/versions/1/preview", follow_redirects=False)
    assert r.status_code == 404  # this fixture version has no preview.json
    r = await client.get(f"/v1/datasets/{ds.id}/versions/1/secrets", follow_redirects=False)
    assert r.status_code == 422


async def test_m4_versions_refused_until_the_backend_lands(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = await _dataset(client)
    up = UploadSession.model_validate(
        (
            await client.post(
                f"/v1/datasets/{ds.id}/uploads",
                json={"filename": "obs.h5", "content_type": "application/x-hdf5", "size_bytes": 64},
            )
        ).json()
    )
    await ctx.store.put(ctx.buckets.quarantine, up.upload.name, b"\x89HDF\r\n\x1a\n" + b"\0" * 8)
    hdf5 = {
        "format": "hdf5",
        "hdf5": {"vars": [{"path": "/img", "name": "image", "dims": ["row"]}]},
    }
    r = await client.post(
        f"/v1/datasets/{ds.id}/versions", json={"upload_id": up.id, "mapping": hdf5}
    )
    assert r.status_code == 422 and r.json()["code"] == "CAPABILITY_UNSUPPORTED", r.text
    asm = {"sources": [{"upload_id": up.id, "mapping": {"format": "netcdf"}}]}
    r = await client.post(f"/v1/datasets/{ds.id}/versions", json={"assembly": asm})
    assert r.status_code == 422 and r.json()["code"] == "CAPABILITY_UNSUPPORTED", r.text
    # neither consumed the upload: a supported mapping still works and reports its source
    r = await client.post(
        f"/v1/datasets/{ds.id}/versions",
        json={"upload_id": up.id, "mapping": {"format": "netcdf"}},
    )
    assert r.status_code == 202, r.text
    dv = DatasetVersion.model_validate(r.json())
    assert dv.sources == [up.id]
    got = DatasetVersion.model_validate(
        (await client.get(f"/v1/datasets/{ds.id}/versions/{dv.version}")).json()
    )
    assert got.sources == [up.id]
