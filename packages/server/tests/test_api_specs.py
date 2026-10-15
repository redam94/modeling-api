"""Specs: static validation (JSON pointers, per-stage schemas), revisions, diff, dry-run."""

from __future__ import annotations

import httpx
from mapi_server.context import ServerContext
from mapi_server_testkit.builders import LEAKY_SPEC, TREND_SPEC, ready_dataset
from mapi_spec.api import SpecDoc, SpecRevision, ValidateResponse


async def test_validate_valid_spec_reports_stages(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx)
    r = await client.post("/v1/specs:validate", json={"yaml": TREND_SPEC})
    assert r.status_code == 200
    v = ValidateResponse.model_validate(r.json())
    assert v.valid, v.issues
    assert v.kind == "ModelSpec"
    assert v.hash and v.hash.startswith("sha256:")
    assert [s.name for s in v.stages] == ["raw", "scale_y"]
    assert [s.split for s in v.stages] == [True, False]
    assert v.resolved is not None and v.resolved.model.resolved == "ts.hsgp_trend@1.0.0"
    assert v.catalog_digest is not None


async def test_validate_reports_every_problem_with_pointers(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx)
    v = ValidateResponse.model_validate(
        (await client.post("/v1/specs:validate", json={"yaml": LEAKY_SPEC})).json()
    )
    assert not v.valid
    assert any(i.code == "LEAKAGE_RISK" for i in v.issues)
    # unknown dataset → REF_UNRESOLVED at /data/dataset, still 200
    doc = TREND_SPEC.replace("dataset://default/co2@latest", "dataset://default/nope@latest")
    v = ValidateResponse.model_validate(
        (await client.post("/v1/specs:validate", json={"yaml": doc})).json()
    )
    assert [(i.code, i.pointer) for i in v.issues] == [("REF_UNRESOLVED", "/data/dataset")]
    # structural errors
    v = ValidateResponse.model_validate(
        (
            await client.post(
                "/v1/specs:validate",
                json={"document": {"apiVersion": "mapi/v1", "kind": "ModelSpec"}},
            )
        ).json()
    )
    assert not v.valid and {i.pointer for i in v.issues} >= {"/metadata", "/data", "/model"}


async def test_validate_dataset_override_and_project(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx, "other")
    body = {"yaml": TREND_SPEC, "dataset": "dataset://default/other@v1"}
    v = ValidateResponse.model_validate((await client.post("/v1/specs:validate", json=body)).json())
    assert v.valid, v.issues
    doc = TREND_SPEC.replace("project: default", "project: growth")
    v = ValidateResponse.model_validate(
        (await client.post("/v1/specs:validate", json={"yaml": doc})).json()
    )
    assert any(i.pointer == "/metadata/project" for i in v.issues)


async def test_validate_unparseable_and_oversized(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/specs:validate", json={"yaml": "a: &x 1\nb: *x\n"})
    assert r.status_code == 400 and r.json()["code"] == "SPEC_PARSE_ERROR"
    r = await client.post("/v1/specs:validate", json={"yaml": "a: [" + "[" * 40 + "]" * 41})
    assert r.status_code in (400, 413)
    r = await client.post("/v1/specs:validate", json={})
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"


async def test_create_spec_revisions_and_diff(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx)
    r = await client.post("/v1/specs", json={"yaml": TREND_SPEC, "message": "first"})
    assert r.status_code == 201
    spec = SpecDoc.model_validate(r.json())
    assert spec.name == "trend" and spec.latest_revision == 1 and spec.latest is not None
    assert spec.latest.valid and spec.latest.ref == "spec://default/trend@r1"
    # duplicate name
    assert (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).status_code == 409
    # stale parent
    body = {"yaml": LEAKY_SPEC, "parent_revision": 7}
    r = await client.post(f"/v1/specs/{spec.id}/revisions", json=body)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    # invalid revisions are stored with their issues
    body["parent_revision"] = 1
    r = await client.post(f"/v1/specs/{spec.id}/revisions", json=body)
    assert r.status_code == 201
    rev = SpecRevision.model_validate(r.json())
    assert rev.revision == 2 and not rev.valid and rev.parent_revision == 1
    assert any(i.code == "LEAKAGE_RISK" for i in rev.issues)
    # renaming is refused
    renamed = TREND_SPEC.replace("name: trend", "name: other")
    r = await client.post(
        f"/v1/specs/{spec.id}/revisions", json={"yaml": renamed, "parent_revision": 2}
    )
    assert r.status_code == 409
    # list + get + diff
    page = (await client.get(f"/v1/specs/{spec.id}/revisions?limit=1")).json()
    assert [x["revision"] for x in page["items"]] == [2] and page["next_cursor"]
    more = (await client.get(f"/v1/specs/{spec.id}/revisions?cursor={page['next_cursor']}")).json()
    assert [x["revision"] for x in more["items"]] == [1]
    assert (await client.get(f"/v1/specs/{spec.id}/revisions/1")).json()["valid"] is True
    assert (await client.get(f"/v1/specs/{spec.id}/revisions/9")).status_code == 404
    diff = (await client.get(f"/v1/specs/{spec.id}/revisions/2/diff")).json()
    assert diff["from_revision"] == 1 and diff["to_revision"] == 2
    assert diff["changes"] == [
        {"op": "add", "path": "/data/split/after_step", "old": None, "new": "scale_y"}
    ]
    got = SpecDoc.model_validate((await client.get(f"/v1/specs/{spec.id}")).json())
    assert got.latest_revision == 2 and got.latest is not None and got.latest.revision == 2
    listing = (await client.get("/v1/specs")).json()
    assert [s["id"] for s in listing["items"]] == [spec.id]


async def test_create_spec_needs_a_name(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/specs", json={"yaml": "apiVersion: mapi/v1\nkind: ModelSpec\n"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["pointer"] == "/metadata/name"


async def test_dry_run_creates_a_validate_run(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx)
    spec = (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).json()
    url = f"/v1/specs/{spec['id']}/revisions/1:dry-run"
    r = await client.post(url, headers={"Idempotency-Key": "dry-1"})
    assert r.status_code == 202
    run = r.json()
    assert run["kind"] == "validate" and run["state"] == "queued"
    again = await client.post(url, headers={"Idempotency-Key": "dry-1"})
    assert again.status_code == 200 and again.json()["id"] == run["id"]
    assert (await client.post(f"/v1/specs/{spec['id']}/revisions/5:dry-run")).status_code == 404
