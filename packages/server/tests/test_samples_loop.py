"""The sample loop (v0.18 D20): an installed sample goes ``provisioning → ingesting → fitting
→ ready`` as its installer, with runs on a fake executor.

The table is stored content-addressed and checked against its checksum; the ingest and the
fit are the installer's runs (admission and quotas apply: a refusal fails the sample with a
plain problem, and a retry reuses the ready dataset version); the fit uses the sample's
profile and labels; ``ready`` seeds the project's library. An installer who lost the right to
start runs fails the sample; a tenant admin's retry takes it over.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run, now
from mapi_server.services.samples import SAMPLE_OBJECT_PREFIX, sample_bytes
from mapi_server.services.samples_loop import SampleLoop
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.builders import simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.identity import grant_role, make_tenant, make_user, session_caller
from mapi_spec.guided import draft_schema
from mapi_spec.run import Actor, RunTrigger
from mapi_spec.samples import SAMPLE_LABEL, SAMPLES, SampleInfo, sample_draft
from sqlalchemy import delete, insert, select, update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

BASE = "/v1/tenants/acme/samples"
GEO = f"{BASE}/geo_mmm"
GUIDED_CATALOG = Path(__file__).parent / "fixtures" / "catalog_guided.json"
SAMPLE = SAMPLES["geo_mmm"]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "catalog_path": GUIDED_CATALOG}
    )


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


async def _people(
    ctx: ServerContext, client: httpx.AsyncClient, *, tier: str = "internal"
) -> dict[str, Any]:
    """An editor and a tenant admin of ``acme`` (``internal``: the fake executor's process
    isolation may run its work)."""
    await make_tenant(ctx, "acme", projects=("growth",), tier=tier)
    editor = await make_user(ctx, "editor@acme.test")
    await grant_role(ctx, editor, "acme", "growth", "editor")
    admin = await make_user(ctx, "admin@acme.test")
    await grant_role(ctx, admin, "acme", None, "admin")
    return {
        "editor": await session_caller(ctx, client, editor),
        "editor_user": editor,
        "admin": await session_caller(ctx, client, admin),
        "admin_user": admin,
    }


async def _row(ctx: ServerContext) -> m.TenantSample:
    async with ctx.db.session() as s:
        row = (await s.execute(select(m.TenantSample))).scalar_one()
        return row


async def _run(ctx: ServerContext, run_id: str | None) -> m.Run:
    assert run_id is not None
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        assert run is not None
        return run


def _ingest_outputs() -> dict[str, bytes]:
    """What a real ingest of the sample table reports (the schema its mapping promises)."""
    draft, _ = sample_draft(SAMPLE, project="sample")
    schema = draft_schema(draft.mapping, SAMPLE.answers.columns)
    summary = {
        "vars": ["revenue_gbp", "avg_price", "spend"],
        "coords": {
            "week": {"kind": "time", "size": 104, "start": "2024-01-07", "end": "2025-12-28",
                     "freq": "W-SUN", "contiguous": True},
            "region": {"kind": "categorical", "size": 4},
            "channel": {"kind": "categorical", "size": 3},
        },
    }  # fmt: skip
    return {
        "schema.json": schema.model_dump_json(exclude_none=True).encode(),
        "summary.json": json.dumps(summary).encode(),
        "columns.json": json.dumps(
            [c.model_dump(mode="json", exclude_none=True) for c in SAMPLE.answers.columns]
        ).encode(),
    }


async def _finish(
    ctx: ServerContext, run_id: str | None, *, replace: dict[str, bytes] | None = None
) -> m.Run:
    """Dispatch the QUEUED run on a fake executor, write its outputs and finalize it."""
    assert run_id is not None
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run.id, run.attempt))
        assert att is not None
    await simulate_outputs(ctx, run, att, replace=replace)
    await Finalizer(ctx).finalize(run.id, run.attempt)
    return await _run(ctx, run_id)


async def _policy(ctx: ServerContext, **limits: object) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            insert(m.QuotaPolicy).values(
                scope="tenant", subject_id="acme", limits=limits, updated_at=now()
            )
        )


async def test_a_sample_goes_from_install_to_ready_as_its_installer(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    editor_id = who["editor_user"].id
    r = await who["editor"].post(f"{GEO}:install")
    assert r.status_code == 202, r.text
    loop = SampleLoop(ctx)

    # provisioning → ingesting: the table stored once, content-addressed, and ingested
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "ingesting" and row.dataset_id is not None and row.dataset_version == 1
    async with ctx.db.session() as s:
        dv = await s.get(m.DatasetVersion, (row.dataset_id, 1))
        ds = await s.get(m.Dataset, row.dataset_id)
        assert dv is not None and ds is not None
        assert (ds.project_id, ds.name, ds.labels) == ("sample", SAMPLE.dataset, SAMPLE.labels)
        upload = await s.get(m.Upload, dv.upload_id)
        assert upload is not None and upload.status == "consumed"
    data = sample_bytes("geo_mmm")
    assert upload.object_name.startswith(f"t/acme/{SAMPLE_OBJECT_PREFIX}/")
    assert await ctx.store.get(upload.bucket, upload.object_name) == data
    ingest = await _run(ctx, dv.ingest_run_id)
    assert ingest.kind == "ingest" and ingest.created_by == editor_id
    assert ingest.project_id == "sample"
    assert await loop.once() == 0  # nothing to do while the ingest runs

    # ingesting → fitting: the Guided spec with the sample's profile, fitted as the installer
    await _finish(ctx, ingest.id, replace=_ingest_outputs())
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "fitting" and row.spec_id is not None
    fit = await _run(ctx, row.run_id)
    assert fit.kind == "fit" and fit.created_by == editor_id and fit.spec_id == row.spec_id
    assert fit.labels[SAMPLE_LABEL] == "geo_mmm"
    assert fit.payload["model"]["fit"] | SAMPLE.fit == fit.payload["model"]["fit"]
    assert fit.resource_class == "M"
    async with ctx.db.session() as s:
        spec = await s.get(m.Spec, row.spec_id)
        assert spec is not None and spec.project_id == "sample"
    [info] = [SampleInfo.model_validate(x) for x in (await who["editor"].get(BASE)).json()]
    assert info.state == "fitting" and info.run_id == fit.id and info.progress is not None

    # fitting → ready: the model version recorded and the library seeded
    done = await _finish(ctx, fit.id)
    assert done.output_model_version_id is not None
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "ready" and row.model_id is not None and row.model_version == 1
    async with ctx.db.session() as s:
        items = list(
            (
                await s.execute(
                    select(m.LibraryItem).where(
                        m.LibraryItem.tenant_id == "acme", m.LibraryItem.project_id == "sample"
                    )
                )
            ).scalars()
        )
    assert sorted(i.kind for i in items) == ["chart_template", "model_recipe", "prep_preset"]
    assert {i.created_by for i in items} == {"system"}
    [info] = [SampleInfo.model_validate(x) for x in (await who["editor"].get(BASE)).json()]
    assert info.state == "ready" and info.progress == 1.0 and info.model_id == row.model_id
    assert await loop.once() == 0  # ready waits for a person


async def test_a_quota_refusal_fails_the_sample_plainly_and_a_retry_reuses_the_data(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    assert (await who["editor"].post(f"{GEO}:install")).status_code == 202
    loop = SampleLoop(ctx)
    await loop.once()
    row = await _row(ctx)
    async with ctx.db.session() as s:
        dv = await s.get(m.DatasetVersion, (row.dataset_id, 1))
        assert dv is not None
    await _finish(ctx, dv.ingest_run_id, replace=_ingest_outputs())
    await _policy(ctx, concurrent_runs=0)

    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "failed" and row.run_id is None and row.spec_id is None
    assert row.problem is not None and row.problem["code"] == "QUOTA_EXCEEDED"
    assert row.problem["detail"]  # plain words, not a stack
    [info] = [SampleInfo.model_validate(x) for x in (await who["editor"].get(BASE)).json()]
    assert info.state == "failed" and info.can_retry and info.problem is not None

    async with ctx.db.session() as s:
        await s.execute(delete(m.QuotaPolicy))
    r = await who["editor"].post(f"{GEO}:retry")
    assert r.status_code == 202, r.text
    assert await loop.once() == 1
    row = await _row(ctx)
    # the ready version is reused: provisioning → ingesting → fitting in one pass
    assert row.state == "fitting" and row.attempts == 2 and row.dataset_version == 1
    async with ctx.db.session() as s:
        ingests = (await s.execute(select(m.Run).where(m.Run.kind == "ingest"))).all()
    assert len(ingests) == 1


async def test_an_installer_who_left_fails_the_sample_until_an_admin_takes_it_over(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    assert (await who["editor"].post(f"{GEO}:install")).status_code == 202
    async with ctx.db.session() as s:
        await s.execute(
            delete(m.Membership).where(m.Membership.principal_id == who["editor_user"].id)
        )
    loop = SampleLoop(ctx)
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "failed" and row.problem is not None
    assert row.problem["code"] == "FORBIDDEN"
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.Run))).first() is None  # nothing started

    r = await who["admin"].post(f"{GEO}:retry")
    assert r.status_code == 202, r.text
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "ingesting" and row.created_by == who["admin_user"].id
    async with ctx.db.session() as s:
        [ingest] = (await s.execute(select(m.Run))).scalars().all()
    assert ingest.created_by == who["admin_user"].id


async def test_a_failed_ingest_or_fit_fails_the_sample(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    assert (await who["editor"].post(f"{GEO}:install")).status_code == 202
    loop = SampleLoop(ctx)
    await loop.once()
    row = await _row(ctx)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DatasetVersion)
            .where(m.DatasetVersion.dataset_id == row.dataset_id)
            .values(status="failed")
        )
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "failed" and row.problem is not None
    assert "data could not be read" in row.problem["detail"]

    # a fit that ends without a model version
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DatasetVersion)
            .where(m.DatasetVersion.dataset_id == row.dataset_id)
            .values(status="ingesting")
        )
    assert (await who["editor"].post(f"{GEO}:retry")).status_code == 202
    await loop.once()
    assert (await _row(ctx)).state == "ingesting"
    ingest = await _run(ctx, (await _ingest_run_id(ctx, row.dataset_id)))
    await _finish(ctx, ingest.id, replace=_ingest_outputs())
    await loop.once()
    row = await _row(ctx)
    assert row.state == "fitting"
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run)
            .where(m.Run.id == row.run_id)
            .values(state="failed", problem={"code": "TIMEOUT", "title": "t", "status": 504})
        )
    assert await loop.once() == 1
    row = await _row(ctx)
    assert row.state == "failed" and row.problem is not None
    assert row.problem["code"] == "TIMEOUT" and "did not finish" in row.problem["detail"]


async def _ingest_run_id(ctx: ServerContext, dataset_id: str | None) -> str:
    async with ctx.db.session() as s:
        ids = (
            await s.execute(
                select(m.Run.id)
                .where(m.Run.kind == "ingest", m.Run.dataset_id == dataset_id)
                .order_by(m.Run.created_at.desc())
            )
        ).scalars()
        return str(next(iter(ids)))


async def test_a_public_tier_tenant_fits_the_sample_on_its_largest_class(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    assert (await who["editor"].post(f"{GEO}:install")).status_code == 202
    loop = SampleLoop(ctx)
    await loop.once()
    row = await _row(ctx)
    await _finish(ctx, await _ingest_run_id(ctx, row.dataset_id), replace=_ingest_outputs())
    async with ctx.db.session() as s:  # the public tier allows class S at most
        await s.execute(update(m.Tenant).where(m.Tenant.id == "acme").values(trust_tier="public"))
    await loop.once()
    row = await _row(ctx)
    assert row.state == "fitting", row.problem
    assert (await _run(ctx, row.run_id)).resource_class == "S"  # not refused for its class


async def test_the_loop_leaves_samples_alone_when_they_are_turned_off(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    assert (await who["editor"].post(f"{GEO}:install")).status_code == 202
    ctx.settings = ctx.settings.model_copy(update={"samples_enabled": False})
    assert await SampleLoop(ctx).once() == 0
    assert (await _row(ctx)).state == "provisioning"


def test_the_sample_table_ships_with_the_package_and_matches_its_checksum() -> None:
    from importlib import resources

    path = resources.files("mapi_server").joinpath("samples", "data", SAMPLE.data_file)
    assert path.is_file()
    assert sample_bytes("geo_mmm") == path.read_bytes()
    header = path.read_bytes().splitlines()[0].decode()
    assert header.split(",") == [c.name for c in SAMPLE.answers.columns]
