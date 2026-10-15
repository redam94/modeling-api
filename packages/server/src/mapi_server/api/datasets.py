"""Datasets: create, upload sessions (signed PUT into quarantine), versions via ingest runs.

The API never sees dataset bytes: the browser PUTs to a signed URL in the quarantine bucket
and a sandboxed ingest run parses the file. Version metadata (schema, summary, profile and
preview refs) comes from the verified ingest outputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Path, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from mapi_spec.api import (
    ArtifactLink,
    Dataset,
    DatasetCreate,
    DatasetVersion,
    DatasetVersionCreate,
    DatasetVersionStatus,
    ObjectRef,
    Page,
    UploadCreate,
    UploadSession,
    new_id,
)
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.schema import DataSchema
from mapi_spec.storage import DATASET_OBJECTS, NotFound, SignedPut, quarantine_key
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
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.bundles import artifact_link
from mapi_server.services.runs import now
from mapi_server.services.submit import submit_ingest

__all__ = ["router"]

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])

UPLOAD_TTL = timedelta(hours=1)
MAX_UPLOAD_BYTES = RESOURCE_CLASSES[ResourceClass.L].max_dataset_bytes
ARTIFACT_NAMES = (*DATASET_OBJECTS, "summary.json")
DatasetId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


def _ref(ds: m.Dataset) -> str:
    return f"dataset://{ds.project_id}/{ds.name}"


def dataset_dto(ds: m.Dataset) -> Dataset:
    return Dataset(
        id=ds.id,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        name=ds.name,
        ref=_ref(ds),
        description=ds.description,
        labels=ds.labels,
        latest_version=ds.latest_version,
        created_at=ds.created_at,
        updated_at=ds.updated_at,
    )


def version_dto(ds: m.Dataset, dv: m.DatasetVersion) -> DatasetVersion:
    return DatasetVersion(
        dataset_id=dv.dataset_id,
        tenant_id=dv.tenant_id,
        project_id=dv.project_id,
        version=dv.version,
        ref=f"{_ref(ds)}@v{dv.version}",
        status=DatasetVersionStatus(dv.status),
        sha256=dv.sha256,
        size_bytes=dv.size_bytes,
        data_schema=DataSchema.model_validate(dv.data_schema) if dv.data_schema else None,
        summary=DataSummary.model_validate(dv.summary) if dv.summary else None,
        profile=ObjectRef.model_validate(dv.profile) if dv.profile else None,
        preview=ObjectRef.model_validate(dv.preview) if dv.preview else None,
        ingest_run_id=dv.ingest_run_id,
        issues=[Issue.model_validate(i) for i in dv.issues],
        message=dv.message,
        created_at=dv.created_at,
        ready_at=dv.ready_at,
    )


async def _dataset(ctx: Ctx, session: Session, dataset_id: str, *, lock: bool = False) -> m.Dataset:
    ds = await session.get(m.Dataset, dataset_id, with_for_update=lock)
    if ds is None or ds.tenant_id != ctx.scope.tenant_id or ds.project_id != ctx.scope.project_id:
        raise NotFoundError("dataset", dataset_id)
    return ds


@router.post(
    "", response_model=Dataset, status_code=status.HTTP_201_CREATED, operation_id="create_dataset"
)
async def create_dataset(ctx: Ctx, session: Session, body: DatasetCreate) -> Dataset:
    ts = now()
    ds = m.Dataset(
        id=new_id("ds"),
        tenant_id=ctx.scope.tenant_id,
        project_id=ctx.scope.project_id,
        name=body.name,
        description=body.description,
        labels=dict(body.labels),
        next_version=1,
        created_at=ts,
        updated_at=ts,
    )
    try:
        async with session.begin_nested():
            session.add(ds)
    except DBIntegrityError:
        raise Conflict(f"a dataset named {body.name!r} already exists") from None
    return dataset_dto(ds)


@router.get("", response_model=Page[Dataset], operation_id="list_datasets")
async def list_datasets(
    ctx: Ctx, session: Session, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[Dataset]:
    stmt = select(m.Dataset).where(
        m.Dataset.tenant_id == ctx.scope.tenant_id, m.Dataset.project_id == ctx.scope.project_id
    )
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.Dataset.id < str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.Dataset.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Dataset](
        items=[dataset_dto(d) for d in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.get("/{dataset_id}", response_model=Dataset, operation_id="get_dataset")
async def get_dataset(ctx: Ctx, session: Session, dataset_id: DatasetId) -> Dataset:
    return dataset_dto(await _dataset(ctx, session, dataset_id))


@router.post(
    "/{dataset_id}/uploads",
    response_model=UploadSession,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_upload",
)
async def create_upload(
    ctx: Ctx, session: Session, dataset_id: DatasetId, body: UploadCreate
) -> UploadSession:
    ds = await _dataset(ctx, session, dataset_id)
    if body.size_bytes > MAX_UPLOAD_BYTES:
        raise MapiError(
            f"uploads are limited to {MAX_UPLOAD_BYTES} bytes",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            pointer="/body/size_bytes",
        )
    upload_id = new_id("upl")
    name = quarantine_key(ds.tenant_id, upload_id)
    signed: SignedPut = await ctx.store.sign_put(
        ctx.buckets.quarantine,
        name,
        expires_in=UPLOAD_TTL,
        content_type=body.content_type,
        max_bytes=body.size_bytes,
        if_generation_match=0,
    )
    row = m.Upload(
        id=upload_id,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        dataset_id=ds.id,
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        status="pending",
        bucket=ctx.buckets.quarantine,
        object_name=name,
        upload=signed.model_dump(mode="json"),
        expires_at=signed.expires_at,
        created_at=now(),
    )
    session.add(row)
    await session.flush()
    return _upload_dto(row)


def _upload_dto(u: m.Upload) -> UploadSession:
    return UploadSession(
        id=u.id,
        tenant_id=u.tenant_id,
        project_id=u.project_id,
        dataset_id=u.dataset_id,
        filename=u.filename,
        content_type=u.content_type,
        size_bytes=u.size_bytes,
        status=u.status,
        upload=SignedPut.model_validate(u.upload),
        expires_at=u.expires_at,
        created_at=u.created_at,
    )


@router.post(
    "/{dataset_id}/versions",
    response_model=DatasetVersion,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_dataset_version",
)
async def create_dataset_version(
    ctx: Ctx,
    session: Session,
    request: Request,
    dataset_id: DatasetId,
    body: DatasetVersionCreate,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    ds = await _dataset(ctx, session, dataset_id, lock=True)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST {request.url.path}", await request.json())
    if replay is not None:
        dv = await session.get(m.DatasetVersion, (ds.id, int(replay.resource_id)))
        assert dv is not None
        return JSONResponse(version_dto(ds, dv).model_dump(mode="json"), status_code=200)
    upload = await session.get(m.Upload, body.upload_id, with_for_update=True)
    if upload is None or upload.dataset_id != ds.id:
        raise NotFoundError("upload", body.upload_id)
    if upload.status == "consumed":
        raise Conflict("this upload was already turned into a dataset version")
    if upload.expires_at < datetime.now(UTC) and upload.status == "pending":
        upload.status = "expired"
    if upload.status == "expired":
        raise Conflict("this upload session expired before the file arrived")
    try:
        meta = await ctx.store.stat(upload.bucket, upload.object_name)
    except NotFound:
        raise Conflict("the upload has not finished: no object exists yet") from None
    if meta.size > upload.size_bytes:
        raise Conflict("the uploaded object is larger than the declared size")
    version = ds.next_version
    ds.next_version += 1
    ds.updated_at = now()
    upload.status = "consumed"
    upload.consumed_by_version = version
    dv = m.DatasetVersion(
        dataset_id=ds.id,
        version=version,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        status="ingesting",
        upload_id=upload.id,
        mapping=body.mapping.model_dump(mode="json"),
        message=body.message,
        objects={},
        issues=[],
        created_at=now(),
    )
    session.add(dv)
    await session.flush()
    run = await submit_ingest(
        session,
        ctx,
        dataset=ds,
        version=version,
        upload=upload,
        generation=meta.generation,
        mapping=body.mapping.model_dump(mode="json"),
    )
    dv.ingest_run_id = run.id
    await session.flush()
    await idem.complete("dataset_version", str(version), 202)
    return JSONResponse(version_dto(ds, dv).model_dump(mode="json"), status_code=202)


@router.get(
    "/{dataset_id}/versions",
    response_model=Page[DatasetVersion],
    operation_id="list_dataset_versions",
)
async def list_dataset_versions(
    ctx: Ctx,
    session: Session,
    dataset_id: DatasetId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[DatasetVersion]:
    ds = await _dataset(ctx, session, dataset_id)
    stmt = select(m.DatasetVersion).where(m.DatasetVersion.dataset_id == ds.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.DatasetVersion.version < int(after))
    rows = list(
        (
            await session.execute(stmt.order_by(m.DatasetVersion.version.desc()).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[DatasetVersion](
        items=[version_dto(ds, v) for v in rows],
        next_cursor=encode_cursor(rows[-1].version) if more and rows else None,
    )


async def _version(
    ctx: Ctx, session: Session, dataset_id: str, version: int
) -> tuple[m.Dataset, m.DatasetVersion]:
    ds = await _dataset(ctx, session, dataset_id)
    dv = await session.get(m.DatasetVersion, (ds.id, version))
    if dv is None:
        raise NotFoundError("dataset version", f"{dataset_id}@v{version}")
    return ds, dv


@router.get(
    "/{dataset_id}/versions/{version}",
    response_model=DatasetVersion,
    operation_id="get_dataset_version",
)
async def get_dataset_version(
    ctx: Ctx, session: Session, dataset_id: DatasetId, version: Annotated[int, Path(ge=1)]
) -> DatasetVersion:
    ds, dv = await _version(ctx, session, dataset_id, version)
    return version_dto(ds, dv)


async def _artifact(
    ctx: Ctx, session: Session, dataset_id: str, version: int, name: str
) -> ArtifactLink:
    _, dv = await _version(ctx, session, dataset_id, version)
    obj = dv.objects.get(name) if name in ARTIFACT_NAMES else None
    if obj is None:
        raise NotFoundError("artifact", name)
    return await artifact_link(
        ctx.store,
        name=name,
        bucket=obj["bucket"],
        key=obj["name"],
        generation=int(obj["generation"]),
    )


@router.get(
    "/{dataset_id}/versions/{version}/artifacts/{name}",
    response_model=ArtifactLink,
    operation_id="get_dataset_artifact",
)
async def get_dataset_artifact(
    ctx: Ctx,
    session: Session,
    dataset_id: DatasetId,
    version: Annotated[int, Path(ge=1)],
    name: str,
) -> ArtifactLink:
    """Signed, generation-pinned URL for ``data.zarr.zip``/``schema.json``/``profile.json``/
    ``preview.json`` (15 min, HTTP Range works)."""
    return await _artifact(ctx, session, dataset_id, version, name)


@router.get(
    "/{dataset_id}/versions/{version}/{view}",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    operation_id="redirect_dataset_view",
    responses={307: {"description": "Redirect to a signed URL"}},
)
async def redirect_dataset_view(
    ctx: Ctx,
    session: Session,
    dataset_id: DatasetId,
    version: Annotated[int, Path(ge=1)],
    view: Annotated[str, Path(pattern=r"^(preview|schema|profile|summary)$")],
) -> RedirectResponse:
    """``…/preview``, ``…/schema``, ``…/profile``, ``…/summary``: 307 to the signed URL."""
    link = await _artifact(ctx, session, dataset_id, version, f"{view}.json")
    return RedirectResponse(link.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
