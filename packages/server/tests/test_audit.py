"""Audit rows for data access and run integrity failures; rows never hold secrets or raw
client addresses."""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import apply_transition, load_run, now
from mapi_server_testkit.builders import ready_dataset
from mapi_spec.api import new_id
from mapi_spec.run import Actor, RunTrigger
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _audit(ctx: ServerContext) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        return list((await s.execute(select(m.AuditEvent).order_by(m.AuditEvent.ts))).scalars())


async def test_signed_download_links_are_audited(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ds, dv = await ready_dataset(ctx)
    r = await client.get(
        f"/v1/datasets/{ds.id}/versions/{dv.version}/artifacts/data.zarr.zip",
        headers={"User-Agent": "pytest-agent", "X-Request-ID": "req-123"},
    )
    assert r.status_code == 200, r.text
    [ev] = [e for e in await _audit(ctx) if e.action == "dataset.download"]
    assert ev.resource_id == f"{ds.id}@v1" and ev.tenant_id == "default"
    assert ev.request_id == "req-123" and ev.user_agent_hash is not None
    assert "pytest-agent" not in str(ev.__dict__)  # only a keyed digest is stored
    assert "X-Goog-Signature" not in str(ev.details) and r.json()["url"] not in str(ev.details)


async def test_integrity_failures_are_audited(ctx: ServerContext) -> None:
    ts = now()
    run_id = new_id("run")
    async with ctx.db.session() as s:
        s.add(
            m.Run(
                id=run_id, tenant_id="default", project_id="default", kind="fit",
                state="finalizing", attempt=1, resource_class="S", timeout_s=60, labels={},
                payload={}, inputs={}, checkpointable=False, infra_retries=0,
                finalize_attempts=0, finalize={}, last_event_id=0, created_at=ts,
                updated_at=ts,
            )
        )  # fmt: skip
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.FAIL_INTEGRITY, Actor.FINALIZER)
    [ev] = [e for e in await _audit(ctx) if e.action == "run.integrity_failure"]
    assert ev.resource_id == run_id and ev.outcome == "error"
    assert ev.code == "INTEGRITY_FAILURE" and ev.actor_kind == "service"
