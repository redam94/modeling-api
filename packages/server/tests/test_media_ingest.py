"""Run media through the event ingestor (v0.14): a verified ``media`` event stores a row and a
copy in the bundles bucket; replays are duplicates; objects that do not match the event are
rejected with a ``MEDIA_REJECTED`` warning (never a sandbox violation); budgets."""

from __future__ import annotations

import hashlib

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services import media as media_mod
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.events import EventIngestor, IngestResult
from mapi_server_testkit.builders import queued_fit, signed_event
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.media import media_payload, png_bytes, table_bytes, upload_media
from mapi_spec.media import media_name
from mapi_spec.storage import run_media_store_key
from mapi_spec.tracking import SCALAR_STEP, TrackingLimits
from sqlalchemy import func, select


@pytest.fixture
async def provisioned(ctx: ServerContext) -> m.RunAttempt:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        return att


async def _rows(ctx: ServerContext, run_id: str) -> list[m.RunMedia]:
    async with ctx.db.session() as s:
        rows = await s.execute(
            select(m.RunMedia).where(m.RunMedia.run_id == run_id).order_by(m.RunMedia.step)
        )
        return list(rows.scalars())


async def _warnings(ctx: ServerContext, run_id: str) -> list[dict[str, object]]:
    async with ctx.db.session() as s:
        rows = await s.execute(
            select(m.RunEvent.payload).where(
                m.RunEvent.run_id == run_id, m.RunEvent.type == "warning"
            )
        )
        return list(rows.scalars())


async def test_a_media_event_stores_a_row_and_a_verified_copy(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    att = provisioned
    data = png_bytes(4, 3)
    payload = media_payload(data, labels={"split": "val"}, caption="epoch 0")
    await upload_media(ctx, att.run_id, 1, data, payload.name)
    ing = EventIngestor(ctx)
    ev = signed_event(att, 0, payload)
    assert await ing.ingest(ev) is IngestResult.ACCEPTED
    assert await ing.ingest(ev) is IngestResult.DUPLICATE
    # the same item in a re-sent event is a duplicate too
    assert await ing.ingest(signed_event(att, 1, payload)) is IngestResult.DUPLICATE
    (row,) = await _rows(ctx, att.run_id)
    assert (row.key, row.labels, row.labels_key, row.step) == (
        "samples",
        {"split": "val"},
        "split=val",
        0,
    )
    assert (row.kind, row.content_type, row.size, row.width, row.height) == (
        "image",
        "image/png",
        len(data),
        4,
        3,
    )
    assert row.sha256 == hashlib.sha256(data).hexdigest() and row.source == "runner"
    assert row.caption == "epoch 0" and row.id.startswith("rm_")
    assert row.bucket == ctx.buckets.bundles
    assert row.object_name == run_media_store_key("default", att.run_id, payload.name)
    assert await ctx.store.get(row.bucket, row.object_name) == data
    async with ctx.db.session() as s:
        stored = (await s.execute(select(func.count()).select_from(m.RunEvent))).scalar_one()
        violations = (await s.get(m.RunAttempt, (att.run_id, 1))).violations  # type: ignore[union-attr]
    assert stored == 0  # media events are never appended to run_events
    assert violations == 0


async def test_new_bytes_replace_an_item_and_tables_and_scalars_are_stored(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    att = provisioned
    ing = EventIngestor(ctx)
    first, second = png_bytes(4, 3), png_bytes(4, 3, shade=7)
    for seq, data in enumerate((first, second)):
        payload = media_payload(data)
        await upload_media(ctx, att.run_id, 1, data, payload.name)
        assert await ing.ingest(signed_event(att, seq, payload)) is IngestResult.ACCEPTED
    (row,) = await _rows(ctx, att.run_id)
    assert row.sha256 == hashlib.sha256(second).hexdigest()
    table = table_bytes(3)
    payload = media_payload(table, key="counts", content_type="application/json", step=None)
    await upload_media(ctx, att.run_id, 1, table, payload.name)
    assert await ing.ingest(signed_event(att, 5, payload)) is IngestResult.ACCEPTED
    rows = await _rows(ctx, att.run_id)
    scalar = next(r for r in rows if r.key == "counts")
    assert scalar.step == SCALAR_STEP and scalar.kind == "table"
    assert (scalar.width, scalar.height) == (None, None)


@pytest.mark.parametrize("problem", ["missing", "sha", "magic", "size", "dimensions"])
async def test_objects_that_do_not_match_the_event_are_rejected_with_a_warning(
    ctx: ServerContext, provisioned: m.RunAttempt, problem: str
) -> None:
    att = provisioned
    data = png_bytes(4, 3)
    payload = media_payload(data)
    if problem == "sha":  # another image under the announced name
        await upload_media(ctx, att.run_id, 1, png_bytes(4, 3, shade=9), payload.name)
    elif problem == "magic":  # not an image, under its own sha256 name
        junk = b"GIF89a" + b"\x00" * 40
        payload = media_payload(junk, sha256=hashlib.sha256(junk).hexdigest())
        await upload_media(ctx, att.run_id, 1, junk, payload.name)
    elif problem == "size":
        payload = media_payload(data, size=len(data) + 1)
        await upload_media(ctx, att.run_id, 1, data, payload.name)
    elif problem == "dimensions":
        payload = media_payload(data, width=40, height=30)
        await upload_media(ctx, att.run_id, 1, data, payload.name)
    result = await EventIngestor(ctx).ingest(signed_event(att, 0, payload))
    assert result is IngestResult.INVALID
    assert await _rows(ctx, att.run_id) == []
    (warning,) = await _warnings(ctx, att.run_id)
    assert warning["code"] == "MEDIA_REJECTED"
    assert "samples" in str(warning["message"])
    async with ctx.db.session() as s:
        attempt = await s.get(m.RunAttempt, (att.run_id, 1))
        assert attempt is not None and attempt.violations == 0  # not a sandbox violation
    stored = await ctx.store.list(ctx.buckets.bundles, prefix=f"t/default/run-media/{att.run_id}/")
    assert not stored.items


async def test_media_over_the_run_budget_is_dropped_with_a_warning(
    ctx: ServerContext, provisioned: m.RunAttempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_mod, "TrackingLimits", lambda: TrackingLimits(media_items=1))
    att = provisioned
    ing = EventIngestor(ctx)
    results = []
    for step in range(4):
        data = png_bytes(4, 3, shade=step)
        payload = media_payload(data, step=step)
        await upload_media(ctx, att.run_id, 1, data, payload.name)
        results.append(await ing.ingest(signed_event(att, step, payload)))
    assert results == [IngestResult.ACCEPTED] + [IngestResult.INVALID] * 3
    assert [r.step for r in await _rows(ctx, att.run_id)] == [0]
    # every item over the budget is dropped; the attempt gets one MEDIA_BUDGET warning
    (warning,) = await _warnings(ctx, att.run_id)
    assert warning["code"] == "MEDIA_BUDGET"
    assert "1 media items" in str(warning["message"])


async def test_the_store_copy_is_reused_when_it_already_exists(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    """Two items with the same bytes share one content-addressed copy."""
    att = provisioned
    ing = EventIngestor(ctx)
    data = png_bytes(4, 3)
    for step in range(2):
        payload = media_payload(data, step=step)
        if step == 0:
            await upload_media(ctx, att.run_id, 1, data, payload.name)
        assert await ing.ingest(signed_event(att, step, payload)) is IngestResult.ACCEPTED
    rows = await _rows(ctx, att.run_id)
    assert len(rows) == 2 and rows[0].object_name == rows[1].object_name
    assert rows[0].generation == rows[1].generation
    assert rows[0].object_name.endswith(media_name(rows[0].sha256, "image/png"))


async def _stored(ctx: ServerContext, run_id: str) -> set[str]:
    page = await ctx.store.list(ctx.buckets.bundles, prefix=f"t/default/run-media/{run_id}/")
    return {o.name for o in page.items}


async def test_a_replaced_item_deletes_its_old_object(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    """Replacing an item at the same key, labels and step does not leave its old bytes
    behind (the budget counts only the rows), unless another row still uses them."""
    att = provisioned
    ing = EventIngestor(ctx)
    images = [png_bytes(4, 3, shade=s) for s in range(4)]
    for seq, (step, data) in enumerate([(0, images[0]), (1, images[0])]):
        payload = media_payload(data, step=step)
        await upload_media(ctx, att.run_id, 1, data, payload.name)
        assert await ing.ingest(signed_event(att, seq, payload)) is IngestResult.ACCEPTED
    shared = run_media_store_key("default", att.run_id, media_payload(images[0]).name)
    assert await _stored(ctx, att.run_id) == {shared}
    for seq, data in enumerate(images[1:], start=2):  # step 0 replaced again and again
        payload = media_payload(data, step=0)
        await upload_media(ctx, att.run_id, 1, data, payload.name)
        assert await ing.ingest(signed_event(att, seq, payload)) is IngestResult.ACCEPTED
    rows = await _rows(ctx, att.run_id)
    assert {r.object_name for r in rows} == await _stored(ctx, att.run_id)
    # step 1 still holds the first bytes; only the newest replacement of step 0 is kept
    last = run_media_store_key("default", att.run_id, media_payload(images[3]).name)
    assert await _stored(ctx, att.run_id) == {shared, last}
