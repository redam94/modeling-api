"""Run media (v0.14): images and tables a run logged, verified and kept in the bundles bucket.

Two writers end here:

* the **runner**: the supervisor uploads a logged item through the attempt's create-only
  ``media/`` POST policy and emits a signed ``media`` event. :func:`ingest_media` downloads
  the object (at most :data:`~mapi_spec.media.MAX_IMAGE_BYTES`), checks its size, sha256,
  CRC32C and content again (:func:`~mapi_spec.media.check_media`; the sandbox is never
  trusted), writes the verified bytes create-only to the **bundles** bucket at
  :func:`~mapi_spec.storage.run_media_store_key` (the runs bucket expires run scratch) and
  upserts the ``run_media`` row. A failed check appends a ``MEDIA_REJECTED`` warning and
  stores nothing; it is not a sandbox violation (the event itself verified);
* the **API** (``POST /v1/runs/{id}/media``): :func:`write_api_media` stores the decoded
  body the same way, ``source="api"``.

One row per ``(run, attempt, key, labels, step)``: the same bytes again are a duplicate,
other bytes replace the row (and take a new ``seq``, the live stream's cursor); the replaced
object is deleted when no other row of the run still points at it, so the stored bytes stay
what the budget counts. Budgets are the :class:`~mapi_spec.tracking.TrackingLimits` defaults
per run, over all attempts and API items (rows and stored bytes): the runner path drops what
is over them (one ``MEDIA_BUDGET`` warning per attempt), the API path refuses
(``QUOTA_EXCEEDED``). The control plane decodes no image: the checks read headers only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any, Literal

from mapi_spec.api import ARTIFACT_LINK_TTL, RunMedia, new_id
from mapi_spec.errors import ErrorCode, MapiError, QuotaExceeded
from mapi_spec.executor import RunEvent, RunEventType
from mapi_spec.media import MAX_IMAGE_BYTES, ImageInfo, check_media, media_name
from mapi_spec.run import MediaPayload, WarningPayload
from mapi_spec.storage import (
    NotFound,
    ObjectMeta,
    PreconditionFailed,
    crc32c,
    crc32c_to_base64,
    run_media_prefix,
    run_media_store_key,
)
from mapi_spec.tracking import SCALAR_STEP, TrackingLimits, check_labels, labels_key
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.notify import notify_run
from mapi_server.services.runs import append_event, now

if TYPE_CHECKING:
    from mapi_spec.api import MediaWrite

    from mapi_server.services.events import IngestResult

__all__ = [
    "MEDIA_BUDGET",
    "MEDIA_REJECTED",
    "MediaRejected",
    "ingest_media",
    "media_view",
    "media_views",
    "write_api_media",
]

MEDIA_REJECTED = "MEDIA_REJECTED"
"""Warning code of a runner media item the server did not store."""
MEDIA_BUDGET = "MEDIA_BUDGET"
"""Warning code of runner media over the run's budget (once per attempt; the supervisor uses
the same code for its own, per-attempt budget)."""

log = logging.getLogger(__name__)

Source = Literal["runner", "api"]
_SIGN_CONCURRENCY = 16


class MediaRejected(Exception):
    """A runner media item failed a check (the message is safe to show)."""


class _OverBudget(MediaRejected):
    """A runner media item over the run's budget."""


def _sha256_ref(hex_digest: str) -> str:
    return f"sha256:{hex_digest}"


async def media_view(ctx: ServerContext, row: m.RunMedia, *, links: bool) -> RunMedia:
    """The API view of a media row; ``links`` adds a signed GET of the stored copy."""
    url = expires_at = None
    if links:
        signed = await ctx.store.sign_get(
            row.bucket, row.object_name, expires_in=ARTIFACT_LINK_TTL, generation=row.generation
        )
        url, expires_at = signed.url, signed.expires_at
    return RunMedia(
        id=row.id,
        run_id=row.run_id,
        attempt=row.attempt,
        key=row.key,
        labels=dict(row.labels or {}),
        step=None if row.step == SCALAR_STEP else row.step,
        kind=row.kind,
        content_type=row.content_type,
        size=row.size,
        sha256=_sha256_ref(row.sha256),
        crc32c=row.crc32c,
        width=row.width,
        height=row.height,
        caption=row.caption,
        source=row.source,
        created_at=row.created_at,
        url=url,
        expires_at=expires_at,
    )


async def media_views(ctx: ServerContext, rows: list[m.RunMedia], *, links: bool) -> list[RunMedia]:
    """Views of several rows; links are signed concurrently (a signature can be a storage
    round trip), at most :data:`_SIGN_CONCURRENCY` at a time."""
    if not links:
        return [await media_view(ctx, r, links=False) for r in rows]
    gate = asyncio.Semaphore(_SIGN_CONCURRENCY)

    async def one(row: m.RunMedia) -> RunMedia:
        async with gate:
            return await media_view(ctx, row, links=True)

    return list(await asyncio.gather(*(one(r) for r in rows)))


# ================================================================================== storing


async def _budget(
    session: AsyncSession,
    run: m.Run,
    conflict: tuple[int, str, str, int],
    size: int,
    limits: TrackingLimits,
) -> str | None:
    """Why storing ``size`` more bytes at ``conflict`` would exceed the run's media budget
    (``None``: it fits). A row that replaces another frees the old one's bytes."""
    attempt, key, lk, step = conflict
    r = m.RunMedia
    count, total = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(r.size), 0)).where(r.run_id == run.id)
        )
    ).one()
    old = (
        await session.execute(
            select(r.size).where(
                r.run_id == run.id,
                r.attempt == attempt,
                r.key == key,
                r.labels_key == lk,
                r.step == step,
            )
        )
    ).scalar_one_or_none()
    if old is None and int(count) >= limits.media_items:
        return f"the run already has {int(count)} media items (at most {limits.media_items})"
    if int(total) - int(old or 0) + size > limits.media_bytes:
        return f"the run's media would exceed {limits.media_bytes} bytes"
    return None


async def _store_copy(
    ctx: ServerContext, run: m.Run, name: str, data: bytes, content_type: str
) -> ObjectMeta:
    """Write the verified bytes create-only to the bundles bucket (already there: the same
    content-addressed object, checked by size and CRC32C)."""
    bucket = ctx.buckets.bundles
    key = run_media_store_key(run.tenant_id, run.id, name)
    try:
        return await ctx.store.put(
            bucket, key, data, content_type=content_type, if_generation_match=0
        )
    except PreconditionFailed:
        meta = await ctx.store.stat(bucket, key)
        if meta.size != len(data) or meta.crc32c != crc32c_to_base64(crc32c(data)):
            raise MediaRejected(f"a different object is already stored at {name}") from None
        return meta


async def _upsert(
    session: AsyncSession,
    run: m.Run,
    *,
    attempt: int,
    key: str,
    labels: dict[str, str],
    step: int | None,
    content_type: str,
    kind: str,
    sha256_hex: str,
    info: ImageInfo | None,
    caption: str | None,
    meta: ObjectMeta,
    source: Source,
) -> m.RunMedia | None:
    """Insert or replace the row; ``None`` when the same bytes are already stored there."""
    values: dict[str, Any] = {
        "id": new_media_id(),
        "run_id": run.id,
        "attempt": attempt,
        "key": key,
        "labels_key": labels_key(labels),
        "labels": labels,
        "step": SCALAR_STEP if step is None else step,
        "kind": kind,
        "content_type": content_type,
        "sha256": sha256_hex,
        "size": meta.size,
        "crc32c": meta.crc32c,
        "width": info.width if info is not None else None,
        "height": info.height if info is not None else None,
        "caption": caption,
        "bucket": meta.bucket,
        "object_name": meta.name,
        "generation": meta.generation,
        "source": source,
        "created_at": now(),
        "tenant_id": run.tenant_id,
        "project_id": run.project_id,
    }
    ins = insert(m.RunMedia).values(**values)
    replaced = (
        "kind",
        "content_type",
        "sha256",
        "size",
        "crc32c",
        "width",
        "height",
        "caption",
        "bucket",
        "object_name",
        "generation",
        "source",
        "created_at",
    )
    stmt = (
        ins.on_conflict_do_update(
            index_elements=["run_id", "attempt", "key", "labels_key", "step"],
            set_={**{f: ins.excluded[f] for f in replaced}, "seq": func.nextval(m.TRACKING_SEQ)},
            where=m.RunMedia.sha256 != ins.excluded.sha256,
        )
        .returning(m.RunMedia)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def new_media_id() -> str:
    return new_id("rm")


_ObjectRef = tuple[str, str, int]


def _ref(row: m.RunMedia | None) -> _ObjectRef | None:
    """``(bucket, object_name, generation)`` of a row, read before the upsert overwrites it."""
    return None if row is None else (row.bucket, row.object_name, row.generation)


async def _drop_replaced(
    session: AsyncSession, ctx: ServerContext, run: m.Run, old: _ObjectRef | None
) -> None:
    """Delete the object of a replaced row when no row of the run still points at it (rows
    with the same bytes share one content-addressed object). The caller holds the run row
    lock, so no other writer of the run can take the object meanwhile; a later write of the
    same bytes stores it again. A delete after the commit would race the next writer, so it
    runs before: a write that then rolls back leaves the old row without its bytes until the
    event is delivered again or the API call is retried."""
    if old is None:
        return
    bucket, name, generation = old
    r = m.RunMedia
    still = (
        await session.execute(
            select(func.count()).where(
                r.run_id == run.id, r.bucket == bucket, r.object_name == name
            )
        )
    ).scalar_one()
    if int(still):
        return
    try:
        await ctx.store.delete(bucket, name, generation=generation)
    except (NotFound, MapiError) as exc:  # already gone or retention-locked: best effort
        log.info("could not delete the replaced media object %s: %s", name, exc)


async def _current(
    session: AsyncSession, run_id: str, attempt: int, key: str, lk: str, step: int
) -> m.RunMedia | None:
    r = m.RunMedia
    return (
        await session.execute(
            select(r).where(
                r.run_id == run_id,
                r.attempt == attempt,
                r.key == key,
                r.labels_key == lk,
                r.step == step,
            )
        )
    ).scalar_one_or_none()


# =================================================================================== runner


async def ingest_media(
    session: AsyncSession,
    ctx: ServerContext,
    run: m.Run,
    event: RunEvent,
    payload: MediaPayload,
) -> IngestResult:
    """Verify and store one ``media`` event (the caller holds the run row lock)."""
    from mapi_server.services.events import IngestResult

    labels = check_labels(payload.labels)
    lk = labels_key(labels)
    step = SCALAR_STEP if payload.step is None else payload.step
    old = await _current(session, run.id, event.attempt, payload.key, lk, step)
    if old is not None and old.sha256 == payload.sha256:
        return IngestResult.DUPLICATE
    try:
        over = await _budget(
            session, run, (event.attempt, payload.key, lk, step), payload.size, TrackingLimits()
        )
        if over is not None:
            raise _OverBudget(over)
        data, info = await _verified_object(ctx, run, event.attempt, payload)
        meta = await _store_copy(ctx, run, payload.name, data, payload.content_type)
    except _OverBudget as exc:
        await _over_budget(session, run, event, str(exc))
        return IngestResult.INVALID
    except MediaRejected as exc:
        await _reject(session, run, event, payload, str(exc))
        return IngestResult.INVALID
    old_ref = _ref(old)
    row = await _upsert(
        session,
        run,
        attempt=event.attempt,
        key=payload.key,
        labels=labels,
        step=payload.step,
        content_type=payload.content_type,
        kind=payload.kind,
        sha256_hex=payload.sha256,
        info=info,
        caption=payload.caption,
        meta=meta,
        source="runner",
    )
    if row is None:
        return IngestResult.DUPLICATE
    await _drop_replaced(session, ctx, run, old_ref)
    await notify_run(session, run.id, "tracking")
    return IngestResult.ACCEPTED


async def _verified_object(
    ctx: ServerContext, run: m.Run, attempt: int, payload: MediaPayload
) -> tuple[bytes, ImageInfo | None]:
    key = run_media_prefix(run.tenant_id, run.id, attempt) + payload.name
    try:
        data = await ctx.store.get(ctx.buckets.runs, key, max_bytes=MAX_IMAGE_BYTES)
    except NotFound:
        raise MediaRejected(f"the media object {payload.name} was not uploaded") from None
    except MapiError as exc:
        raise MediaRejected(f"the media object {payload.name} cannot be read: {exc}") from None
    if len(data) != payload.size:
        raise MediaRejected(
            f"the media object {payload.name} has {len(data)} bytes, the event says {payload.size}"
        )
    if hashlib.sha256(data).hexdigest() != payload.sha256:
        raise MediaRejected(f"the sha256 of the media object {payload.name} does not match")
    if crc32c_to_base64(crc32c(data)) != payload.crc32c:
        raise MediaRejected(f"the CRC32C of the media object {payload.name} does not match")
    try:
        info = check_media(payload.kind, payload.content_type, data)
    except ValueError as exc:
        raise MediaRejected(f"the media object {payload.name} is not valid: {exc}") from None
    if info is not None and (info.width, info.height) != (payload.width, payload.height):
        raise MediaRejected(
            f"the image {payload.name} is {info.width}x{info.height}, the event says "
            f"{payload.width}x{payload.height}"
        )
    return data, info


async def _over_budget(session: AsyncSession, run: m.Run, event: RunEvent, why: str) -> None:
    """One ``MEDIA_BUDGET`` warning per attempt: the run's budget counts every attempt and
    the API items, so an attempt can reach it with a fresh supervisor budget and send many
    more items; each of them is dropped, only the first one is reported."""
    e = m.RunEvent
    warned = (
        await session.execute(
            select(e.id)
            .where(
                e.run_id == run.id,
                e.attempt == event.attempt,
                e.type == RunEventType.WARNING.value,
                e.payload["code"].astext == MEDIA_BUDGET,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if warned is not None:
        return
    warning = WarningPayload(
        code=MEDIA_BUDGET,
        message=f"the run logged more media than its budget ({why}). "
        "The platform does not keep the media after the budget",
    )
    await append_event(
        session,
        run,
        type=RunEventType.WARNING,
        payload=warning.model_dump(mode="json", exclude_none=True),
        attempt=event.attempt,
    )


async def _reject(
    session: AsyncSession, run: m.Run, event: RunEvent, payload: MediaPayload, why: str
) -> None:
    warning = WarningPayload(
        code=MEDIA_REJECTED,
        message=f"media {payload.key!r} was not stored: {why}"[:2000],
    )
    await append_event(
        session,
        run,
        type=RunEventType.WARNING,
        payload=warning.model_dump(mode="json", exclude_none=True),
        attempt=event.attempt,
    )


# ====================================================================================== API


async def write_api_media(
    session: AsyncSession, ctx: ServerContext, run: m.Run, body: MediaWrite
) -> tuple[m.RunMedia, bool]:
    """Store an API-logged item in the run's current attempt; returns ``(row, created)``
    (``created`` is false when the same bytes were already stored at that key/labels/step).
    Over the budget: ``QUOTA_EXCEEDED`` (nothing is stored)."""
    data = body.decoded()
    try:
        info = check_media(body.kind, body.content_type, data)
    except ValueError as exc:  # the request model already checked; kept as a second guard
        raise MapiError(str(exc), code=ErrorCode.SCHEMA_INVALID, pointer="/data") from None
    labels = check_labels(body.labels)
    lk = labels_key(labels)
    step = SCALAR_STEP if body.step is None else body.step
    sha = hashlib.sha256(data).hexdigest()
    existing = await _current(session, run.id, run.attempt, body.key, lk, step)
    if existing is not None and existing.sha256 == sha:
        return existing, False
    over = await _budget(
        session, run, (run.attempt, body.key, lk, step), len(data), TrackingLimits()
    )
    if over is not None:
        raise QuotaExceeded(over)
    try:
        meta = await _store_copy(
            ctx, run, media_name(sha, body.content_type), data, body.content_type
        )
    except MediaRejected as exc:  # another object under a content-addressed name
        raise MapiError(str(exc), code=ErrorCode.INTEGRITY_FAILURE) from None
    old_ref = _ref(existing)
    row = await _upsert(
        session,
        run,
        attempt=run.attempt,
        key=body.key,
        labels=labels,
        step=body.step,
        content_type=body.content_type,
        kind=body.kind,
        sha256_hex=sha,
        info=info,
        caption=body.caption,
        meta=meta,
        source="api",
    )
    if row is None:  # a concurrent write stored the same bytes
        row = await _current(session, run.id, run.attempt, body.key, lk, step)
        assert row is not None
        return row, False
    await _drop_replaced(session, ctx, run, old_ref)
    await notify_run(session, run.id, "tracking")
    return row, True
