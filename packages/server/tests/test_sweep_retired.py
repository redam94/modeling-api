"""``Sweep`` is retired (v0.14): new Sweep specs and revisions are refused at ``/kind``;
Sweep specs stored before still read and list."""

from __future__ import annotations

import httpx
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset
from mapi_spec.api import new_id

SWEEP = """\
apiVersion: mapi/v1
kind: Sweep
metadata: {name: lengths, project: default}
base: spec://default/trend@r1
grid:
  /model/settings/l_max: [2, 4, 8]
"""


async def test_new_sweep_documents_are_refused(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    r = await client.post("/v1/specs", json={"yaml": SWEEP})
    assert r.status_code == 422, r.text
    problem = r.json()
    assert problem["code"] == "SCHEMA_INVALID"
    assert "retired" in problem["detail"] and "search.grid" in problem["detail"]
    assert problem["errors"][0]["pointer"] == "/kind"

    await ready_dataset(ctx)
    spec = (await client.post("/v1/specs", json={"yaml": TREND_SPEC})).json()
    r = await client.post(
        f"/v1/specs/{spec['id']}/revisions",
        json={"yaml": SWEEP.replace("lengths", "trend"), "parent_revision": 1},
    )
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/kind"


async def test_stored_sweep_specs_still_read_and_list(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    async with ctx.db.session() as s:
        spec = m.Spec(
            id=new_id("spc"),
            tenant_id="default",
            project_id="default",
            name="old-sweep",
            kind="Sweep",
            latest_revision=1,
        )
        s.add(spec)
        await s.flush()
        s.add(
            m.SpecRevision(
                spec_id=spec.id,
                revision=1,
                tenant_id="default",
                project_id="default",
                kind="Sweep",
                hash="sha256:" + "0" * 64,
                yaml=SWEEP,
                document={"apiVersion": "mapi/v1", "kind": "Sweep"},
                valid=False,
                issues=[],
                resolved=None,
            )
        )
        spec_id = spec.id
    r = await client.get(f"/v1/specs/{spec_id}")
    assert r.status_code == 200 and r.json()["kind"] == "Sweep"
    listed = (await client.get("/v1/specs")).json()["items"]
    assert spec_id in {x["id"] for x in listed}
