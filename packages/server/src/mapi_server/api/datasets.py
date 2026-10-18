"""Datasets: create, upload sessions (signed PUT into quarantine), versions via ingest runs.

The API never sees dataset bytes: the browser PUTs to a signed URL in the quarantine bucket
and a sandboxed ingest run parses the file. Version metadata (schema, summary, profile and
preview refs) comes from the verified ingest outputs.

M4: a version may combine several uploads (``assembly``: one ingest run with inputs
``upload.<i>``) and be a tree (``layout: tree``, ``data.tree.zip`` + ``tree_schema``). An
upload can be *inspected* first (an ``inspect`` run listing its groups and arrays from
metadata only), which a raw HDF5 mapping needs.
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
    Run,
    UploadCreate,
    UploadSession,
    new_id,
)
from mapi_spec.audit import AuditAction
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.ingest import UploadInspection
from mapi_spec.jobs import DATATREE_OUTPUT, INGEST_COLUMNS, TREE_SCHEMA_OUTPUT
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.run import RunState
from mapi_spec.schema import DataSchema
from mapi_spec.storage import DATASET_OBJECTS, NotFound, ObjectMeta, SignedPut, quarantine_key
from mapi_spec.tenancy import ResourceKind
from mapi_spec.tree import TreeSchema
from mapi_spec.views import EXPLORE_PREFIX
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
from mapi_server.api.problems import responses_for
from mapi_server.api.runs import run_view
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import write_audit
from mapi_server.services.bundles import artifact_link
from mapi_server.services.quotas import admit_storage, charge_egress
from mapi_server.services.runs import load_run, now
from mapi_server.services.submit import submit_ingest, submit_inspect

__all__ = ["router"]

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])

UPLOAD_TTL = timedelta(hours=1)
MAX_UPLOAD_BYTES = RESOURCE_CLASSES[ResourceClass.L].max_dataset_bytes
ARTIFACT_NAMES = (
    *DATASET_OBJECTS,
    "summary.json",
    INGEST_COLUMNS,
    DATATREE_OUTPUT,  # M4 tree versions
    TREE_SCHEMA_OUTPUT,
)
DatasetId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
UploadId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]


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
        sources=[str(u) for u in dv.sources] or ([dv.upload_id] if dv.upload_id else []),
        issues=[Issue.model_validate(i) for i in dv.issues],
        message=dv.message,
        created_at=dv.created_at,
        ready_at=dv.ready_at,
        layout="tree" if dv.layout == "tree" else "dataset",
        tree_schema=TreeSchema.model_validate(dv.tree_schema) if dv.tree_schema else None,
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
    async with ctx.db.session() as system:
        decision = await admit_storage(
            session, system, tenant_id=ds.tenant_id, nbytes=body.size_bytes, now=now()
        )
    if not decision.allowed:
        assert decision.code is not None
        raise ProblemWithHeaders(
            decision.message or "storage quota exceeded",
            code=decision.code,
            headers=decision.headers(),
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
    if body.assembly is not None:
        wanted = [
            (src.upload_id, f"/body/assembly/sources/{i}/upload_id")
            for i, src in enumerate(body.assembly.sources)
        ]
    else:
        assert body.upload_id is not None and body.mapping is not None  # the DTO checks it
        wanted = [(body.upload_id, "/body/upload_id")]
    # every upload is checked before any is consumed: one bad source refuses the version
    # and leaves all of them usable (the transaction is all or nothing)
    ready = await _ready_uploads(ctx, session, ds, wanted)
    total = sum(upload.size_bytes for upload, _ in ready)
    if total > MAX_UPLOAD_BYTES:
        raise MapiError(
            f"the uploads of one version are limited to {MAX_UPLOAD_BYTES} bytes together "
            f"(these are {total})",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            pointer="/body/assembly",
        )
    version = ds.next_version
    ds.next_version += 1
    ds.updated_at = now()
    for upload, _ in ready:
        upload.status = "consumed"
        upload.consumed_by_version = version
    stored_mapping = (
        {"assembly": body.assembly.model_dump(mode="json", exclude_none=True)}
        if body.assembly is not None
        else body.mapping.model_dump(mode="json")  # type: ignore[union-attr]
    )
    dv = m.DatasetVersion(
        dataset_id=ds.id,
        version=version,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        status="ingesting",
        upload_id=ready[0][0].id,
        sources=[upload.id for upload, _ in ready],
        mapping=stored_mapping,
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
        uploads=[(upload, meta.generation) for upload, meta in ready],
        mapping=body.mapping.model_dump(mode="json") if body.mapping is not None else None,
        assembly=body.assembly,
    )
    dv.ingest_run_id = run.id
    await session.flush()
    await idem.complete("dataset_version", str(version), 202)
    return JSONResponse(version_dto(ds, dv).model_dump(mode="json"), status_code=202)


async def _upload(
    session: Session, ds: m.Dataset, upload_id: str, *, pointer: str | None = None
) -> m.Upload:
    """The (row-locked) upload ``upload_id`` of dataset ``ds`` (404 otherwise)."""
    upload = await session.get(m.Upload, upload_id, with_for_update=True)
    if upload is None or upload.dataset_id != ds.id or upload.tenant_id != ds.tenant_id:
        raise NotFoundError("upload", upload_id)
    return upload


async def _arrived(ctx: Ctx, upload: m.Upload, *, pointer: str = "") -> ObjectMeta:
    """The uploaded object of an upload whose file arrived in time (409 otherwise)."""
    if upload.expires_at < datetime.now(UTC) and upload.status == "pending":
        upload.status = "expired"
    if upload.status == "expired":
        raise Conflict(f"upload {upload.id} expired before the file arrived", pointer=pointer)
    try:
        meta = await ctx.store.stat(upload.bucket, upload.object_name)
    except NotFound:
        raise Conflict(
            f"upload {upload.id} has not finished: no object exists yet", pointer=pointer
        ) from None
    if meta.size > upload.size_bytes:
        raise Conflict(
            f"the object of upload {upload.id} is larger than the declared size", pointer=pointer
        )
    return meta


async def _ready_uploads(
    ctx: Ctx, session: Session, ds: m.Dataset, wanted: list[tuple[str, str]]
) -> list[tuple[m.Upload, ObjectMeta]]:
    """Lock and check every ``(upload id, pointer)`` (in source order): it belongs to
    ``ds``, is used once, its file arrived and it is not consumed yet. Nothing is changed
    except marking expired uploads ``expired``."""
    seen: set[str] = set()
    for upload_id, pointer in wanted:
        if upload_id in seen:  # the DTO refuses it too; keep the guard next to the consume
            raise Conflict(f"upload {upload_id} is used twice", pointer=pointer)
        seen.add(upload_id)
    rows: dict[str, m.Upload] = {}
    for upload_id, pointer in sorted(wanted):  # one lock order: no deadlocks between calls
        rows[upload_id] = await _upload(session, ds, upload_id, pointer=pointer)
    ready: list[tuple[m.Upload, ObjectMeta]] = []
    for upload_id, pointer in wanted:
        upload = rows[upload_id]
        if upload.status == "consumed":
            raise Conflict(
                f"upload {upload_id} was already turned into a dataset version", pointer=pointer
            )
        ready.append((upload, await _arrived(ctx, upload, pointer=pointer)))
    return ready


# ============================================================================= inspect


@router.post(
    "/{dataset_id}/uploads/{upload_id}/inspect",
    response_model=Run,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="inspect_upload",
    summary="List the groups and arrays of an upload (an inspect run)",
    responses=responses_for("inspect_upload"),
)
async def inspect_upload(
    ctx: Ctx,
    session: Session,
    request: Request,
    dataset_id: DatasetId,
    upload_id: UploadId,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """List the groups and arrays of an upload (an ``inspect`` run reading metadata only;
    M4). Idempotent per upload: while an inspection runs, or once one succeeded, that run
    is returned instead of starting another (a failed one is started again)."""
    ds = await _dataset(ctx, session, dataset_id)
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST {request.url.path}", None)
    if replay is not None:
        replayed = await load_run(session, ctx.scope, replay.resource_id)
        return JSONResponse(
            (await run_view(session, replayed)).model_dump(mode="json"), status_code=202
        )
    upload = await _upload(session, ds, upload_id)
    run = await _current_inspect_run(session, upload)
    if run is None:
        if upload.status == "consumed":
            raise Conflict(
                f"upload {upload.id} was turned into a dataset version and was never inspected"
            )
        meta = await _arrived(ctx, upload)
        run = await submit_inspect(
            session, ctx, dataset=ds, upload=upload, generation=meta.generation
        )
        upload.inspect_run_id = run.id
        upload.inspection = None
        await session.flush()
    await idem.complete("run", run.id, 202)
    return JSONResponse((await run_view(session, run)).model_dump(mode="json"), status_code=202)


async def _current_inspect_run(session: Session, upload: m.Upload) -> m.Run | None:
    """The upload's inspect run unless it ended without an inspection (failed, cancelled)."""
    if upload.inspect_run_id is None:
        return None
    run = await session.get(m.Run, upload.inspect_run_id)
    if run is None:
        return None
    state = RunState(run.state)
    if state.terminal and (state is not RunState.SUCCEEDED or upload.inspection is None):
        return None
    return run


@router.get(
    "/{dataset_id}/uploads/{upload_id}/inspection",
    response_model=UploadInspection,
    operation_id="get_upload_inspection",
    summary="The groups and arrays an inspect run found",
    responses=responses_for("get_upload_inspection"),
)
async def get_upload_inspection(
    ctx: Ctx, session: Session, dataset_id: DatasetId, upload_id: UploadId
) -> UploadInspection:
    """What the upload's inspect run found (``409 NOT_READY`` while it runs; ``404`` when
    no inspection was requested or its run ended without one - the run's ``problem`` says
    why it failed)."""
    ds = await _dataset(ctx, session, dataset_id)
    upload = await session.get(m.Upload, upload_id)
    if upload is None or upload.dataset_id != ds.id or upload.tenant_id != ds.tenant_id:
        raise NotFoundError("upload", upload_id)
    if upload.inspection is not None:
        return UploadInspection.model_validate(upload.inspection)
    if upload.inspect_run_id is None:
        raise NotFoundError("upload inspection", upload_id)
    run = await session.get(m.Run, upload.inspect_run_id)
    if run is None:
        raise NotFoundError("upload inspection", upload_id)
    state = RunState(run.state)
    if not state.terminal:
        raise MapiError(
            f"the inspection of upload {upload_id} is still running (run {run.id}, "
            f"{state.value}); try again shortly",
            code=ErrorCode.NOT_READY,
        )
    raise NotFoundError(
        "upload inspection", f"{upload_id} (inspect run {run.id} ended {state.value})"
    )


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


def _artifact_allowed(name: str) -> bool:
    """The fixed ingest outputs, plus (M3.5) the Data Explorer views an ingest writes under
    ``ui/explore/`` (JSON only)."""
    if name in ARTIFACT_NAMES:
        return True
    parts = name.split("/")
    return (
        name.startswith(EXPLORE_PREFIX)
        and name.endswith(".json")
        and all(p and p not in (".", "..") for p in parts)
        and len(name) <= 256
    )


async def _artifact(
    ctx: Ctx, session: Session, dataset_id: str, version: int, name: str, *, audit: bool = False
) -> ArtifactLink:
    _, dv = await _version(ctx, session, dataset_id, version)
    obj = dv.objects.get(name) if _artifact_allowed(name) else None
    if obj is None:
        raise NotFoundError("artifact", name)
    link = await artifact_link(
        ctx.store,
        name=name,
        bucket=obj["bucket"],
        key=obj["name"],
        generation=int(obj["generation"]),
    )
    await charge_egress(ctx, dv.tenant_id, link.size or 0)
    if audit and ctx.request is not None:  # a signed download URL left the API
        await write_audit(
            ctx.db,
            [
                ctx.request.audit(
                    ctx,
                    AuditAction.DATASET_DOWNLOAD,
                    ResourceKind.DATASET,
                    resource_id=f"{dataset_id}@v{version}",
                    details={"name": name},
                )
            ],
            session=session,
        )
    return link


@router.get(
    "/{dataset_id}/versions/{version}/artifacts/{name:path}",
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
    ``preview.json``/``summary.json``/``columns.json`` and (M3.5) the Data Explorer views
    ``ui/explore/…`` written at ingest (15 min, HTTP Range works)."""
    return await _artifact(ctx, session, dataset_id, version, name, audit=True)


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
