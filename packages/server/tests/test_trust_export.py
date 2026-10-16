"""GDPR export (M3): the archive holds exactly the tenant's own rows and objects, never
credentials, is offered through a short-lived signed link, is rate-limited, and expires."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.gdpr_export import REDACTED, ExportBuilder, export_object_name
from mapi_server.services.lifecycle import InlineLifecycleJobs, LifecycleScheduler
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import session_caller, token_caller
from mapi_server_testkit.trust import seed_tenant
from mapi_spec.storage import NotFound
from mapi_spec.trust import GdprExport
from sqlalchemy import update

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


async def _archive(ctx: ServerContext, tenant_id: str, export_id: str) -> zipfile.ZipFile:
    data = await ctx.store.get(ctx.buckets.data, export_object_name(tenant_id, export_id))
    return zipfile.ZipFile(io.BytesIO(data))


def _rows(zf: zipfile.ZipFile, name: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in zf.read(name).decode().splitlines() if line]


async def test_export_contains_the_tenant_and_nothing_else(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    beta = await seed_tenant(ctx, "beta")
    owner = await session_caller(ctx, client, alpha.user)
    r = await owner.post("/v1/tenants/alpha/exports", json={})
    assert r.status_code == 202, r.text
    exp = GdprExport.model_validate(r.json())
    assert exp.state == "queued" and exp.download_url is None
    assert exp.include == ["metadata", "datasets", "models", "audit", "usage"]
    # one at a time
    again = await owner.post("/v1/tenants/alpha/exports", json={"include": ["audit"]})
    assert again.status_code == 429 and again.json()["code"] == "QUOTA_EXCEEDED"
    assert int(again.headers["retry-after"]) > 0
    # tokens cannot export (tenant administration needs a session)
    r = await token_caller(client, alpha.token).post("/v1/tenants/alpha/exports", json={})
    assert r.status_code == 403
    jobs = InlineLifecycleJobs(ctx)
    assert (await LifecycleScheduler(ctx, jobs).tick())["exports"] == 1
    await jobs.drain()

    r = await owner.get(f"/v1/tenants/alpha/exports/{exp.id}")
    ready = GdprExport.model_validate(r.json())
    assert ready.state == "ready" and ready.download_url and ready.size_bytes
    assert r.headers["cache-control"] == "no-store"
    assert ready.expires_at is not None and ready.ready_at is not None
    assert ready.expires_at - ready.ready_at == timedelta(days=7)
    zf = await _archive(ctx, "alpha", exp.id)
    names = set(zf.namelist())
    assert {
        "README.txt",
        "manifest.json",
        "metadata/datasets.jsonl",
        "metadata/runs.jsonl",
        "metadata/api_tokens.jsonl",
        "audit/audit_events.jsonl",
        "usage/cost_ledger.jsonl",
        "datasets/sales/v1/data.zarr.zip",
        "models/mmm/v1/manifest.json",
        "models/mmm/v1/model_version.json",
    } <= names
    assert zf.read("datasets/sales/v1/data.zarr.zip") == alpha.data_bytes
    for name in names:
        if name.endswith(".jsonl"):
            for row in _rows(zf, name):
                assert row.get("tenant_id") == "alpha", (name, row)
    [token] = _rows(zf, "metadata/api_tokens.jsonl")
    assert token["secret_hash"] == REDACTED and token["prefix"]
    [attempt] = _rows(zf, "metadata/run_attempts.jsonl")
    assert attempt["hmac_key"] == REDACTED and attempt["nonce"] == REDACTED
    [idem] = _rows(zf, "metadata/idempotency_keys.jsonl")
    assert idem["key"] == REDACTED
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["tenant_id"] == "alpha" and manifest["skipped"] == []
    assert manifest["rows"]["metadata/runs.jsonl"] == 1
    entry = manifest["entries"]["datasets/sales/v1/data.zarr.zip"]
    assert entry["size"] == len(alpha.data_bytes)
    blob = b"".join(zf.read(n) for n in names)
    assert beta.dataset_id.encode() not in blob and beta.run_id.encode() not in blob
    assert alpha.token.encode() not in blob
    # the other tenant cannot read it
    other = await session_caller(ctx, client, beta.user)
    assert (await other.get(f"/v1/tenants/alpha/exports/{exp.id}")).status_code == 404
    assert (await other.get(f"/v1/tenants/beta/exports/{exp.id}")).status_code == 404
    async with ctx.db.session() as s:
        n = await s.get(m.TenantExport, exp.id)
    assert n is not None and n.sha256 and n.sha256.startswith("sha256:")


async def test_parts_size_limit_and_expiry(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    ctx.settings = ctx.settings.model_copy(update={"export_max_inline_bytes": 100})
    alpha = await seed_tenant(ctx, "alpha")
    owner = await session_caller(ctx, client, alpha.user)
    r = await owner.post("/v1/tenants/alpha/exports", json={"include": ["datasets", "audit"]})
    exp_id = r.json()["id"]
    assert await ExportBuilder(ctx).run(exp_id) == "ready"
    assert await ExportBuilder(ctx).run(exp_id) == "skipped"  # only queued exports are built
    zf = await _archive(ctx, "alpha", exp_id)
    names = set(zf.namelist())
    assert "audit/audit_events.jsonl" in names
    assert not any(n.startswith(("metadata/", "usage/", "models/")) for n in names)
    manifest = json.loads(zf.read("manifest.json"))
    [skip] = manifest["skipped"]
    assert skip["reason"] == "export size limit" and skip["size"] == len(alpha.data_bytes)
    # the archive expires: state "expired", the object is deleted, no link any more
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantExport).values(expires_at=now() - timedelta(seconds=1)))
    r = await owner.get(f"/v1/tenants/alpha/exports/{exp_id}")
    assert r.json()["state"] == "expired" and r.json()["download_url"] is None
    await LifecycleScheduler(ctx, InlineLifecycleJobs(ctx)).tick()
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.data, export_object_name("alpha", exp_id))
    # a day's allowance
    ctx.settings = ctx.settings.model_copy(update={"exports_per_day": 1})
    r = await owner.post("/v1/tenants/alpha/exports", json={})
    assert r.status_code == 429 and "today" in r.json()["detail"]


async def test_a_failed_build_is_reported(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    owner = await session_caller(ctx, client, alpha.user)
    exp_id = (await owner.post("/v1/tenants/alpha/exports", json={})).json()["id"]

    async def broken_put(*args: object, **kw: object) -> object:
        raise ConnectionError("storage is down")

    monkeypatch.setattr(ctx.store, "put", broken_put)
    assert await ExportBuilder(ctx).run(exp_id) == "failed"
    r = await owner.get(f"/v1/tenants/alpha/exports/{exp_id}")
    assert r.json()["state"] == "failed"
    # a failed export does not hold the "one at a time" slot
    assert (await owner.post("/v1/tenants/alpha/exports", json={})).status_code == 202
