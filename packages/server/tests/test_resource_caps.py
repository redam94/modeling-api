"""Per-resource-class data caps (plan §8.5): checked at ingest and at spec validation."""

from __future__ import annotations

import httpx
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset
from mapi_spec.api import UploadSession, ValidateResponse
from mapi_spec.resources import GB

MAPPING = {
    "format": "csv",
    "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
    "vars": {"y": {"column": "co2", "role": "target"}},
}


async def _ingest_class(client: httpx.AsyncClient, ctx: ServerContext, size: int) -> str:
    ds = (await client.post("/v1/datasets", json={"name": f"d{size}"})).json()
    up = UploadSession.model_validate(
        (
            await client.post(
                f"/v1/datasets/{ds['id']}/uploads",
                json={"filename": "d.csv", "content_type": "text/csv", "size_bytes": size},
            )
        ).json()
    )
    await ctx.store.put(ctx.buckets.quarantine, up.upload.name, b"date,co2\n2020-01,1\n")
    r = await client.post(
        f"/v1/datasets/{ds['id']}/versions", json={"upload_id": up.id, "mapping": MAPPING}
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/v1/runs/{r.json()['ingest_run_id']}")).json()
    return str(run["resource_class"])


async def test_ingest_runs_at_the_class_the_upload_size_needs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    assert await _ingest_class(client, ctx, 64) == "S"
    assert await _ingest_class(client, ctx, GB // 2 + 1) == "M"  # S caps datasets at 0.5 GB
    assert await _ingest_class(client, ctx, 3 * GB) == "L"


async def _big_dataset(ctx: ServerContext, size: int) -> None:
    _, dv = await ready_dataset(ctx)
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (dv.dataset_id, dv.version))
        assert row is not None
        row.size_bytes = size


async def test_spec_validation_refuses_a_dataset_over_the_class_cap(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _big_dataset(ctx, 2 * GB)
    v = ValidateResponse.model_validate(
        (await client.post("/v1/specs:validate", json={"yaml": TREND_SPEC})).json()
    )
    assert not v.valid
    issue = next(i for i in v.issues if i.pointer == "/execution/resource_class")
    assert issue.code == "PAYLOAD_TOO_LARGE" and "M" in issue.message
    big = TREND_SPEC.replace("evaluate:", "execution: {resource_class: M}\nevaluate:")
    v = ValidateResponse.model_validate(
        (await client.post("/v1/specs:validate", json={"yaml": big})).json()
    )
    assert v.valid, v.issues


async def test_run_submission_checks_the_effective_class(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await _big_dataset(ctx, 1 * GB)
    r = await client.post("/v1/specs", json={"yaml": TREND_SPEC})
    assert r.status_code == 201, r.text
    spec_id = r.json()["id"]
    r = await client.post("/v1/runs", json={"kind": "fit", "spec_id": spec_id, "revision": 1})
    assert r.status_code in (413, 422) and r.json()["code"] == "PAYLOAD_TOO_LARGE", r.text
    r = await client.post(
        "/v1/runs",
        json={"kind": "fit", "spec_id": spec_id, "revision": 1, "resource_class": "M"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["resource_class"] == "M"
