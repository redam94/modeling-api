"""Services extracted from routes for the v0.18 sample loop, and the v0.18 tables in tenant
deletion and GDPR export.

``services.datasets`` (dataset rows, upload admission, versions with their ingest run),
``services.specs.create_spec_row`` and the public ``services.endpoints.put_content_addressed``
behave as the routes they came from did (the route suites cover the routes themselves); the
library, onboarding and sample tables are found by the purge and the export like every other
tenant table.
"""

from __future__ import annotations

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict
from mapi_server.services import endpoints
from mapi_server.services.datasets import create_dataset_row, create_version
from mapi_server.services.deletion import TenantPurger
from mapi_server.services.gdpr_export import ExportBuilder
from mapi_server.services.runs import now
from mapi_server.services.specs import create_spec_row, validate_document
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset
from mapi_spec.api import new_id
from mapi_spec.ingest import IngestMapping
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

V018_TABLES = ("library_items", "library_item_revisions", "onboarding_progress", "tenant_samples")


async def test_create_dataset_row_refuses_a_taken_name_and_keeps_the_transaction(
    ctx: ServerContext,
) -> None:
    async with ctx.db.session() as s:
        ds = await create_dataset_row(s, ctx.scope, name="sales", labels={"team": "growth"})
        with pytest.raises(Conflict):
            await create_dataset_row(s, ctx.scope, name="sales")
        other = await create_dataset_row(s, ctx.scope, name="costs")  # still usable
    async with ctx.db.session() as s:
        names = set((await s.execute(select(m.Dataset.name))).scalars())
        stored = await s.get(m.Dataset, ds.id)
    assert names == {"sales", "costs"} and other.next_version == 1
    assert stored is not None and stored.labels == {"team": "growth"}


async def test_create_version_consumes_the_upload_and_submits_the_ingest(
    ctx: ServerContext,
) -> None:
    body = b"date,co2\n2020-01-01,410.1\n2020-02-01,411.0\n"
    meta = await ctx.store.put(ctx.buckets.quarantine, "t/default/uploads/upl_x", body)
    mapping = IngestMapping.model_json_schema()["examples"][0]
    async with ctx.db.session() as s:
        ds = await create_dataset_row(s, ctx.scope, name="co2")
        upload = m.Upload(
            id=new_id("upl"),
            tenant_id=ds.tenant_id,
            project_id=ds.project_id,
            dataset_id=ds.id,
            filename="co2.csv",
            content_type="text/csv",
            size_bytes=len(body),
            status="uploaded",
            bucket=meta.bucket,
            object_name=meta.name,
            upload={},
            expires_at=now(),
            created_at=now(),
        )
        s.add(upload)
        await s.flush()
        dv = await create_version(
            s, ctx, ds, [(upload, meta.generation)], mapping=mapping, message="first"
        )
        assert (dv.version, ds.next_version, dv.status) == (1, 2, "ingesting")
        assert upload.status == "consumed" and upload.consumed_by_version == 1
        assert dv.mapping == mapping and dv.sources == [upload.id] and dv.message == "first"
        run = await s.get(m.Run, dv.ingest_run_id)
        assert run is not None and run.kind == "ingest" and run.state == "queued"
        assert run.inputs["upload"]["generation"] == meta.generation
        with pytest.raises(ValueError):
            await create_version(s, ctx, ds, [(upload, meta.generation)])


async def test_create_spec_row_stores_revision_one_once(ctx: ServerContext) -> None:
    await ready_dataset(ctx)
    async with ctx.db.session() as s:
        v = await validate_document(s, ctx.scope, ctx.catalogs, text=TREND_SPEC)
        spec, rev = await create_spec_row(s, ctx.scope, v, yaml=TREND_SPEC, message="hello")
        assert (spec.name, spec.kind, spec.latest_revision) == ("trend", "ModelSpec", 1)
        assert (rev.revision, rev.valid, rev.message, rev.yaml) == (1, True, "hello", TREND_SPEC)
        assert rev.dataset_version == 1 and rev.catalog_digest is not None
        with pytest.raises(Conflict):
            await create_spec_row(s, ctx.scope, v, yaml=TREND_SPEC)
    async with ctx.db.session() as s:
        assert len((await s.execute(select(m.Spec))).all()) == 1


async def test_put_content_addressed_reuses_the_live_generation(ctx: ServerContext) -> None:
    name = "t/default/samples/abc.csv"
    first = await endpoints.put_content_addressed(
        ctx, ctx.buckets.quarantine, name, b"a,b\n1,2\n", content_type="text/csv"
    )
    again = await endpoints.put_content_addressed(
        ctx, ctx.buckets.quarantine, name, b"a,b\n1,2\n", content_type="text/csv"
    )
    assert again.generation == first.generation
    assert first.content_type == "text/csv"
    assert endpoints._put_content_addressed is endpoints.put_content_addressed  # old name


async def test_purge_and_export_cover_the_v018_tables(ctx: ServerContext) -> None:
    async with ctx.db.session() as s:
        order = await TenantPurger(ctx)._tables(s)
    assert set(V018_TABLES) <= set(order)
    # children before parents: a sample row and a library item go before their project
    assert order.index("tenant_samples") < order.index("projects")
    assert order.index("library_item_revisions") < order.index("library_items")
    assert order.index("library_items") < order.index("projects")
    tables = dict(await ExportBuilder(ctx)._tables())
    for table in V018_TABLES:
        assert tables[table] is True  # exported through the tenant's own RLS session
