"""Specs: synchronous static validation, immutable revisions, diff and sandboxed dry-run."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.responses import JSONResponse
from mapi_spec.api import (
    Page,
    Run,
    SpecCreate,
    SpecDoc,
    SpecRevision,
    SpecRevisionCreate,
    StageSchema,
    ValidateRequest,
    ValidateResponse,
    new_id,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, json_pointer
from mapi_spec.executor import RunKind
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError as DBIntegrityError

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Ctx,
    Idempotency,
    IdempotencyKeyHeader,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.runs import render_run
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.guided import enforce_guided_lock
from mapi_server.services.runs import now
from mapi_server.services.specs import Validation, validate_document
from mapi_server.services.submit import submit_spec_run

__all__ = ["DiffEntry", "SpecDiff", "diff_documents", "router"]

router = APIRouter(prefix="/v1/specs", tags=["specs"])

SpecId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$")


def _validate_response(v: Validation) -> ValidateResponse:
    return ValidateResponse(
        valid=v.valid,
        kind=v.kind,
        hash=v.hash,
        issues=v.issues,
        stages=[StageSchema(name=n, data_schema=s, split=split) for n, s, split in v.stages],
        resolved=v.resolved,
        catalog_digest=v.catalog.digest if v.catalog else None,
    )


@router.post(":validate", response_model=ValidateResponse, operation_id="validate_spec")
async def validate_spec(ctx: Ctx, session: Session, body: ValidateRequest) -> ValidateResponse:
    """200 even for invalid documents: the verdict is ``valid`` + ``issues``."""
    v = await validate_document(
        session,
        ctx.scope,
        ctx.catalogs,
        text=body.yaml,
        document=dict(body.document) if body.document is not None else None,
        dataset_override=body.dataset,
    )
    return _validate_response(v)


def _revision_dto(spec: m.Spec, rev: m.SpecRevision) -> SpecRevision:
    return SpecRevision(
        spec_id=rev.spec_id,
        tenant_id=rev.tenant_id,
        project_id=rev.project_id,
        revision=rev.revision,
        ref=f"spec://{spec.project_id}/{spec.name}@r{rev.revision}",
        kind=rev.kind,
        hash=rev.hash,
        yaml=rev.yaml,
        document=rev.document,
        valid=rev.valid,
        issues=[Issue.model_validate(i) for i in rev.issues],
        parent_revision=rev.parent_revision,
        message=rev.message,
        created_at=rev.created_at,
    )


def _spec_dto(spec: m.Spec, latest: m.SpecRevision | None) -> SpecDoc:
    return SpecDoc(
        id=spec.id,
        tenant_id=spec.tenant_id,
        project_id=spec.project_id,
        name=spec.name,
        kind=spec.kind,
        ref=f"spec://{spec.project_id}/{spec.name}",
        latest_revision=spec.latest_revision,
        latest=_revision_dto(spec, latest) if latest is not None else None,
        created_at=spec.created_at,
        updated_at=spec.updated_at,
    )


def _name_and_kind(v: Validation) -> tuple[str, str]:
    meta = v.document.get("metadata")
    name = meta.get("name") if isinstance(meta, dict) else None
    if v.kind is None:
        raise MapiError(
            "the document has no valid kind",
            code=ErrorCode.SCHEMA_INVALID,
            issues=[Issue(code=ErrorCode.SCHEMA_INVALID, pointer="/kind", message="unknown kind")],
        )
    if not isinstance(name, str) or not _SLUG.match(name):
        raise MapiError(
            "stored specs need metadata.name (a slug)",
            code=ErrorCode.SCHEMA_INVALID,
            issues=[
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/metadata/name",
                    message="metadata.name is required to store a spec",
                )
            ],
        )
    return name, v.kind


def _revision_row(
    spec: m.Spec, revision: int, yaml: str, v: Validation, parent: int | None, message: str | None
) -> m.SpecRevision:
    return m.SpecRevision(
        spec_id=spec.id,
        revision=revision,
        tenant_id=spec.tenant_id,
        project_id=spec.project_id,
        kind=v.kind or spec.kind,
        hash=v.hash or "",
        yaml=yaml,
        document=v.document,
        valid=v.valid,
        issues=[i.model_dump(mode="json") for i in v.issues],
        resolved=v.resolved.model_dump(mode="json") if v.resolved else None,
        catalog_digest=v.catalog.digest if v.catalog else None,
        dataset_id=v.dataset.dataset_id if v.dataset else None,
        dataset_version=v.dataset.version if v.dataset else None,
        parent_revision=parent,
        message=message,
        created_at=now(),
    )


@router.post(
    "", response_model=SpecDoc, status_code=status.HTTP_201_CREATED, operation_id="create_spec"
)
async def create_spec(ctx: Ctx, session: Session, body: SpecCreate) -> SpecDoc:
    """Store revision 1 (invalid documents are stored too, with ``valid: false``).

    In a Guided-locked project, members below admin may store Guided specs only (``403``
    with the offending pointers otherwise; see ``Project.guided_locked``)."""
    v = await validate_document(session, ctx.scope, ctx.catalogs, text=body.yaml)
    name, kind = _name_and_kind(v)
    await enforce_guided_lock(session, ctx.scope, ctx.request, v.document)
    ts = now()
    spec = m.Spec(
        id=new_id("spc"),
        tenant_id=ctx.scope.tenant_id,
        project_id=ctx.scope.project_id,
        name=name,
        kind=kind,
        latest_revision=1,
        created_at=ts,
        updated_at=ts,
    )
    try:
        async with session.begin_nested():
            session.add(spec)
    except DBIntegrityError:
        raise Conflict(f"a spec named {name!r} already exists; add a revision instead") from None
    rev = _revision_row(spec, 1, body.yaml, v, None, body.message)
    session.add(rev)
    await session.flush()
    return _spec_dto(spec, rev)


@router.get("", response_model=Page[SpecDoc], operation_id="list_specs")
async def list_specs(
    ctx: Ctx, session: Session, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[SpecDoc]:
    stmt = select(m.Spec).where(
        m.Spec.tenant_id == ctx.scope.tenant_id, m.Spec.project_id == ctx.scope.project_id
    )
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Spec.id < str(after))
    rows = list((await session.execute(stmt.order_by(m.Spec.id.desc()).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[SpecDoc](
        items=[_spec_dto(s, None) for s in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


async def _spec(ctx: Ctx, session: Session, spec_id: str, *, lock: bool = False) -> m.Spec:
    spec = await session.get(m.Spec, spec_id, with_for_update=lock)
    if (
        spec is None
        or spec.tenant_id != ctx.scope.tenant_id
        or spec.project_id != ctx.scope.project_id
    ):
        raise NotFoundError("spec", spec_id)
    return spec


async def _rev(session: Session, spec: m.Spec, revision: int) -> m.SpecRevision:
    rev = await session.get(m.SpecRevision, (spec.id, revision))
    if rev is None:
        raise NotFoundError("spec revision", f"{spec.id}@r{revision}")
    return rev


@router.get("/{spec_id}", response_model=SpecDoc, operation_id="get_spec")
async def get_spec(ctx: Ctx, session: Session, spec_id: SpecId) -> SpecDoc:
    spec = await _spec(ctx, session, spec_id)
    return _spec_dto(spec, await _rev(session, spec, spec.latest_revision))


@router.post(
    "/{spec_id}/revisions",
    response_model=SpecRevision,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_spec_revision",
)
async def create_spec_revision(
    ctx: Ctx, session: Session, spec_id: SpecId, body: SpecRevisionCreate
) -> SpecRevision:
    spec = await _spec(ctx, session, spec_id, lock=True)
    if body.parent_revision != spec.latest_revision:
        raise Conflict(
            f"parent_revision {body.parent_revision} is stale; the latest revision is "
            f"{spec.latest_revision}",
            pointer="/body/parent_revision",
        )
    v = await validate_document(session, ctx.scope, ctx.catalogs, text=body.yaml)
    name, kind = _name_and_kind(v)
    parent = await _rev(session, spec, spec.latest_revision)
    await enforce_guided_lock(session, ctx.scope, ctx.request, v.document, parent.document)
    if name != spec.name:
        raise Conflict("metadata.name differs from the spec's name; renaming is not supported")
    if kind != spec.kind:
        raise Conflict(f"a {spec.kind} spec cannot get a {kind} revision")
    spec.latest_revision += 1
    spec.updated_at = now()
    rev = _revision_row(
        spec, spec.latest_revision, body.yaml, v, body.parent_revision, body.message
    )
    session.add(rev)
    await session.flush()
    return _revision_dto(spec, rev)


@router.get(
    "/{spec_id}/revisions",
    response_model=Page[SpecRevision],
    operation_id="list_spec_revisions",
)
async def list_spec_revisions(
    ctx: Ctx,
    session: Session,
    spec_id: SpecId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[SpecRevision]:
    spec = await _spec(ctx, session, spec_id)
    stmt = select(m.SpecRevision).where(m.SpecRevision.spec_id == spec.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.SpecRevision.revision < int(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.SpecRevision.revision.desc()).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[SpecRevision](
        items=[_revision_dto(spec, r) for r in rows],
        next_cursor=encode_cursor(rows[-1].revision) if more and rows else None,
    )


@router.get(
    "/{spec_id}/revisions/{revision}",
    response_model=SpecRevision,
    operation_id="get_spec_revision",
)
async def get_spec_revision(
    ctx: Ctx, session: Session, spec_id: SpecId, revision: Annotated[int, Path(ge=1)]
) -> SpecRevision:
    spec = await _spec(ctx, session, spec_id)
    return _revision_dto(spec, await _rev(session, spec, revision))


# ------------------------------------------------------------------------------------ diff


class DiffEntry(BaseModel):
    op: str = Field(..., pattern=r"^(add|remove|replace)$")
    path: str = Field(..., description="RFC 6901 pointer into the documents")
    old: Any = None
    new: Any = None


class SpecDiff(BaseModel):
    spec_id: str
    from_revision: int
    to_revision: int
    from_hash: str
    to_hash: str
    changes: list[DiffEntry]


def diff_documents(old: Any, new: Any, path: tuple[str | int, ...] = ()) -> list[DiffEntry]:
    """Structural diff of two JSON documents (dict keys by name, lists by index)."""
    ptr = json_pointer(*path)
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[DiffEntry] = []
        for key in sorted(set(old) | set(new)):
            if key not in new:
                out.append(DiffEntry(op="remove", path=json_pointer(*path, key), old=old[key]))
            elif key not in old:
                out.append(DiffEntry(op="add", path=json_pointer(*path, key), new=new[key]))
            else:
                out.extend(diff_documents(old[key], new[key], (*path, key)))
        return out
    if isinstance(old, list) and isinstance(new, list):
        out = []
        for i in range(max(len(old), len(new))):
            if i >= len(new):
                out.append(DiffEntry(op="remove", path=json_pointer(*path, i), old=old[i]))
            elif i >= len(old):
                out.append(DiffEntry(op="add", path=json_pointer(*path, i), new=new[i]))
            else:
                out.extend(diff_documents(old[i], new[i], (*path, i)))
        return out
    if old != new or type(old) is not type(new):
        return [DiffEntry(op="replace", path=ptr, old=old, new=new)]
    return []


@router.get(
    "/{spec_id}/revisions/{revision}/diff",
    response_model=SpecDiff,
    operation_id="diff_spec_revisions",
)
async def diff_spec_revisions(
    ctx: Ctx,
    session: Session,
    spec_id: SpecId,
    revision: Annotated[int, Path(ge=1)],
    against: Annotated[int | None, Query(ge=1, description="Default: revision - 1")] = None,
) -> SpecDiff:
    """What changed from ``against`` (default: the previous revision) to ``revision``."""
    spec = await _spec(ctx, session, spec_id)
    to_rev = await _rev(session, spec, revision)
    base = against if against is not None else max(revision - 1, 1)
    from_rev = await _rev(session, spec, base)
    return SpecDiff(
        spec_id=spec.id,
        from_revision=from_rev.revision,
        to_revision=to_rev.revision,
        from_hash=from_rev.hash,
        to_hash=to_rev.hash,
        changes=diff_documents(from_rev.document, to_rev.document),
    )


@router.post(
    "/{spec_id}/revisions/{revision}:dry-run",
    response_model=Run,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="dry_run_spec",
    responses={
        202: {"model": Run, "description": "The validate run"},
        200: {"model": Run, "description": "Idempotent replay"},
    },
)
async def dry_run_spec(
    ctx: Ctx,
    session: Session,
    request: Request,
    spec_id: SpecId,
    revision: Annotated[int, Path(ge=1)],
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Explicit sandboxed dry-run: the same as ``POST /v1/runs`` with ``kind: validate``."""
    await _spec(ctx, session, spec_id)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST {request.url.path}", {})
    if replay is not None:
        return await render_run(session, replay.resource_id, 200)
    run = await submit_spec_run(
        session, ctx, kind=RunKind.VALIDATE, spec_id=spec_id, revision=revision
    )
    await idem.complete("run", run.id, 202)
    return await render_run(session, run.id, 202)
