"""Run media routes (v0.14): list and read the images and tables a run logged, their series,
and media writes from API and notebook users.

Reads sign a GET of each stored copy (``links``, on by default); the bytes never pass through
the control plane on the way out. ``POST /v1/runs/{id}/media`` reads its body itself: the
``Content-Length`` is checked before a byte is read, and the read stops at the same cap
(there is no body-size middleware on this route). Writes lock the run row, are allowed in
any run state, go in with ``source="api"`` and are audited (``run.log``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mapi_spec.api import MediaKeySummary, MediaWrite, Page, RunMedia
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.media import MAX_API_MEDIA_BYTES, MEDIA_KEY_RE, MediaKind
from mapi_spec.tenancy import ResourceKind
from mapi_spec.tracking import SCALAR_STEP, labels_key, parse_labels_key
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.runs import RunId
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.audit import write_audit
from mapi_server.services.media import media_view, media_views, write_api_media
from mapi_server.services.runs import load_run

__all__ = ["MAX_MEDIA_BODY_BYTES", "router"]

router = APIRouter(prefix="/v1/runs", tags=["runs"])

MAX_MEDIA_BODY_BYTES = 4 * MAX_API_MEDIA_BYTES // 3 + 65536
"""Bytes of a ``POST /v1/runs/{id}/media`` body: the base64 of the largest item plus room
for the other fields."""

MediaId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


def _bad_query(detail: str, pointer: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def _too_large() -> MapiError:
    return MapiError(
        f"the request body exceeds {MAX_MEDIA_BODY_BYTES} bytes; an item logged through the "
        f"API has at most {MAX_API_MEDIA_BYTES} bytes (log larger images from the run)",
        code=ErrorCode.PAYLOAD_TOO_LARGE,
    )


@router.get("/{run_id}/media", response_model=Page[RunMedia], operation_id="list_run_media")
async def list_run_media(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    key: Annotated[str | None, Query(pattern=MEDIA_KEY_RE)] = None,
    kind: MediaKind | None = None,
    attempt: Annotated[int | None, Query(ge=1)] = None,
    links: bool = True,
) -> Page[RunMedia]:
    """The run's media, ordered by key, labels, step and id. ``links`` (default true) adds a
    signed GET (``url``, ``expires_at``) of each item; ``caption`` is untrusted text."""
    run = await load_run(session, ctx.scope, run_id)
    r = m.RunMedia
    stmt = select(r).where(r.run_id == run.id)
    if key is not None:
        stmt = stmt.where(r.key == key)
    if kind is not None:
        stmt = stmt.where(r.kind == kind)
    if attempt is not None:
        stmt = stmt.where(r.attempt == attempt)
    after = decode_cursor(cursor)
    if after is not None:
        if not (
            isinstance(after, list)
            and len(after) == 4
            and isinstance(after[0], str)
            and isinstance(after[1], str)
            and isinstance(after[2], int)
            and isinstance(after[3], str)
        ):
            raise _bad_query("the cursor is invalid", "/query/cursor")
        k, lk, st, mid = after
        stmt = stmt.where(
            or_(
                r.key > k,
                and_(r.key == k, r.labels_key > lk),
                and_(r.key == k, r.labels_key == lk, r.step > st),
                and_(r.key == k, r.labels_key == lk, r.step == st, r.id > mid),
            )
        )
    rows = list(
        (
            await session.execute(stmt.order_by(r.key, r.labels_key, r.step, r.id).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows else None
    return Page[RunMedia](
        items=await media_views(ctx, rows, links=links),
        next_cursor=encode_cursor([last.key, last.labels_key, last.step, last.id])
        if more and last
        else None,
    )


@router.get(
    "/{run_id}/media-keys", response_model=Page[MediaKeySummary], operation_id="list_run_media_keys"
)
async def list_run_media_keys(
    ctx: Ctx,
    session: Session,
    run_id: RunId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    attempt: Annotated[int | None, Query(ge=1)] = None,
) -> Page[MediaKeySummary]:
    """The run's media series (key + labels) with their item counts and steps, ordered by
    key and labels."""
    run = await load_run(session, ctx.scope, run_id)
    r = m.RunMedia
    stepped = func.nullif(r.step, SCALAR_STEP)
    stmt = select(
        r.key,
        r.labels_key,
        func.min(r.kind).label("kind"),
        func.count().label("n"),
        func.min(stepped).label("first_step"),
        func.max(stepped).label("last_step"),
        func.max(r.created_at).label("last_created_at"),
    ).where(r.run_id == run.id)
    if attempt is not None:
        stmt = stmt.where(r.attempt == attempt)
    after = decode_cursor(cursor)
    if after is not None:
        if not (
            isinstance(after, list) and len(after) == 2 and all(isinstance(a, str) for a in after)
        ):
            raise _bad_query("the cursor is invalid", "/query/cursor")
        stmt = stmt.where(or_(r.key > after[0], and_(r.key == after[0], r.labels_key > after[1])))
    rows = (
        await session.execute(
            stmt.group_by(r.key, r.labels_key).order_by(r.key, r.labels_key).limit(limit + 1)
        )
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        MediaKeySummary(
            key=x.key,
            labels=parse_labels_key(x.labels_key),
            kind=x.kind,
            count=int(x.n),
            first_step=None if x.first_step is None else int(x.first_step),
            last_step=None if x.last_step is None else int(x.last_step),
            last_created_at=x.last_created_at,
        )
        for x in rows
    ]
    last = items[-1] if items else None
    return Page[MediaKeySummary](
        items=items,
        next_cursor=encode_cursor([last.key, labels_key(last.labels)]) if more and last else None,
    )


@router.get("/{run_id}/media/{media_id}", response_model=RunMedia, operation_id="get_run_media")
async def get_run_media(ctx: Ctx, session: Session, run_id: RunId, media_id: MediaId) -> RunMedia:
    """One media item with a signed GET of the stored copy (verify ``size`` and ``crc32c``)."""
    run = await load_run(session, ctx.scope, run_id)
    row = (
        await session.execute(
            select(m.RunMedia).where(m.RunMedia.run_id == run.id, m.RunMedia.id == media_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("media", media_id)
    return await media_view(ctx, row, links=True)


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_MEDIA_BODY_BYTES:
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MEDIA_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(raw: bytes) -> MediaWrite:
    try:
        return MediaWrite.model_validate_json(raw)
    except ValidationError as exc:
        errors: list[Any] = [
            {**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False)
        ]
        raise RequestValidationError(errors) from None


@router.post(
    "/{run_id}/media",
    response_model=RunMedia,
    status_code=status.HTTP_201_CREATED,
    operation_id="log_run_media",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/MediaWrite"}}
            },
        }
    },
)
async def log_run_media(
    ctx: Ctx, auth: Auth, session: Session, request: Request, run_id: RunId
) -> JSONResponse:
    """Log one image (PNG/JPEG) or table (JSON) into the run's current attempt (also after
    the run finished). The same bytes at the same key, labels and step are stored once;
    other bytes replace the item. Over the run's media budget: ``QUOTA_EXCEEDED``."""
    body = _parse(await _read_body(request))
    run = await load_run(session, ctx.scope, run_id, for_update=True)
    row, created = await write_api_media(session, ctx, run, body)
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.RUN_LOG,
                ResourceKind.RUN,
                resource_id=run.id,
                details={"media": 1, "kind": body.kind, "stored": created},
            )
        ],
        session=session,
    )
    view = await media_view(ctx, row, links=True)
    return JSONResponse(view.model_dump(mode="json"), status_code=status.HTTP_201_CREATED)
