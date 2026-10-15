"""Run submission: resolve, statically validate, pin inputs, then CREATED → QUEUED.

The API owns ``CREATED``/``REJECTED`` (plan §8.3). Requests that fail static validation are
refused with the first error's code (no run is created); accepted runs go straight to
``QUEUED`` for the dispatcher. Every input object is pinned to a generation here, so the
RunSpec built later signs exactly what was validated.
"""

from __future__ import annotations

import copy
from typing import Any

from mapi_spec._types import Duration
from mapi_spec.api import RunCreate, RunRetry
from mapi_spec.capabilities import Capabilities
from mapi_spec.errors import ErrorCode, Issue, MapiError, ValidationFailed
from mapi_spec.executor import RunKind
from mapi_spec.jobs import (
    BUNDLE_MANIFEST_OUTPUT,
    FIT_BUNDLE,
    INGEST_DATA,
    INPUT_BUNDLE,
    INPUT_BUNDLE_MANIFEST,
    INPUT_DATASET,
    INPUT_PLOTS,
    INPUT_UPLOAD,
    DeriveJob,
    DescribeJob,
    EvaluateJob,
    IngestJob,
    IngestSource,
    RenderJob,
    parse_model_job_params,
    side_table_input,
)
from mapi_spec.loader import spec_hash
from mapi_spec.refs import DatasetRef
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.spec import ModelSpec, ScoreRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.bundles import verified_bundle_object
from mapi_server.services.runs import Scope, apply_transition, create_run
from mapi_server.services.runspec import StoredRef
from mapi_server.services.specs import (
    Validation,
    check_score_request,
    pin_document,
    resolve_dataset,
    validate_document,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "data_ref",
    "submit_describe",
    "submit_ingest",
    "submit_model_run",
    "submit_retry",
    "submit_run",
    "submit_spec_run",
]

DEFAULT_TIMEOUT_S = 1800
BUNDLE_INPUT = INPUT_BUNDLE


def data_ref(dv: m.DatasetVersion, name: str = INGEST_DATA) -> dict[str, Any]:
    obj = dv.objects.get(name)
    if obj is None:
        raise MapiError(
            f"dataset version {dv.version} has no {name}", code=ErrorCode.REF_UNRESOLVED
        )
    return StoredRef(obj["bucket"], obj["name"], int(obj["generation"]))


async def _accept(session: AsyncSession, run: m.Run) -> m.Run:
    await apply_transition(session, run, RunTrigger.ACCEPT, Actor.API)
    return run


async def load_revision(
    session: AsyncSession, scope: Scope, spec_id: str, revision: int
) -> tuple[m.Spec, m.SpecRevision]:
    spec = await session.get(m.Spec, spec_id)
    if spec is None or spec.tenant_id != scope.tenant_id or spec.project_id != scope.project_id:
        raise NotFoundError("spec", spec_id)
    rev = await session.get(m.SpecRevision, (spec_id, revision))
    if rev is None:
        raise NotFoundError("spec revision", f"{spec_id}@r{revision}")
    return spec, rev


async def submit_spec_run(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    kind: RunKind,
    spec_id: str,
    revision: int,
    resource_class: ResourceClass | None = None,
    timeout: Duration | None = None,
    labels: dict[str, str] | None = None,
) -> m.Run:
    """``fit`` or ``validate`` (dry-run) from a stored revision, re-validated now."""
    scope = ctx.scope
    _, rev = await load_revision(session, scope, spec_id, revision)
    v: Validation = await validate_document(
        session, scope, ctx.catalogs, document=copy.deepcopy(rev.document)
    )
    if not isinstance(v.typed, ModelSpec):
        raise MapiError(
            f"{kind.value} runs need a ModelSpec revision", code=ErrorCode.SCHEMA_INVALID
        )
    if not v.valid or v.resolved is None or v.dataset is None:
        raise ValidationFailed(v.issues)
    spec = v.typed
    caps: Capabilities = v.resolved.model.capabilities
    payload = pin_document(rev.document, v)
    if kind is RunKind.FIT and spec.model.fit.checkpoint is not None:
        checkpointable = caps.checkpointable
    else:
        checkpointable = False
    inputs = {INPUT_DATASET: data_ref(v.dataset)}
    for name, tv in v.side_tables.items():
        inputs[side_table_input(name)] = data_ref(tv)
    run = await create_run(
        session,
        scope,
        kind=kind,
        resource_class=resource_class or spec.execution.resource_class,
        timeout_s=(timeout or spec.execution.timeout).seconds,
        payload=payload,
        inputs=inputs,
        labels=labels,
        spec_id=spec_id,
        spec_revision=revision,
        spec_hash=spec_hash(payload),
        dataset_id=v.dataset.dataset_id,
        dataset_version=v.dataset.version,
        catalog_digest=v.catalog.digest if v.catalog else None,
        checkpointable=checkpointable,
    )
    return await _accept(session, run)


async def _ready_model_version(session: AsyncSession, scope: Scope, mv_id: str) -> m.ModelVersion:
    mv = await session.get(m.ModelVersion, mv_id)
    if mv is None or mv.tenant_id != scope.tenant_id or mv.project_id != scope.project_id:
        raise NotFoundError("model version", mv_id)
    if mv.status != "ready":
        raise Conflict(f"model version {mv_id} is {mv.status}; only ready versions can be used")
    return mv


async def _model_ref(session: AsyncSession, mv: m.ModelVersion) -> str:
    model = await session.get(m.Model, mv.model_id)
    assert model is not None
    return f"model://{mv.project_id}/{model.name}@v{mv.version}"


async def _fit_dataset(session: AsyncSession, mv: m.ModelVersion) -> m.DatasetVersion:
    """The dataset version the model version was fitted on."""
    fit = await session.get(m.Run, mv.run_id)
    if fit is None or fit.dataset_id is None or fit.dataset_version is None:
        raise MapiError(
            f"model version {mv.id} has no recorded fit dataset; pass a dataset ref",
            code=ErrorCode.REF_UNRESOLVED,
        )
    dv = await session.get(m.DatasetVersion, (fit.dataset_id, fit.dataset_version))
    if dv is None or dv.status != "ready":
        raise MapiError(
            f"the fit dataset of model version {mv.id} is no longer available",
            code=ErrorCode.REF_UNRESOLVED,
        )
    return dv


async def _dataset_input(
    session: AsyncSession, scope: Scope, ref: str | DatasetRef | None, pointer: str
) -> m.DatasetVersion | None:
    if ref is None:
        return None
    if isinstance(ref, str):
        try:
            ref = DatasetRef.parse(ref)
        except ValueError as exc:
            raise MapiError(
                f"{ref!r} is not a dataset reference",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=pointer,
                untrusted_text=str(exc)[:2000],
            ) from None
    dv, why = await resolve_dataset(session, scope, ref)
    if dv is None:
        raise MapiError(
            why or f"cannot resolve {ref}", code=ErrorCode.REF_UNRESOLVED, pointer=pointer
        )
    return dv


async def _model_job_inputs(
    session: AsyncSession,
    ctx: ServerContext,
    mv: m.ModelVersion,
    job: EvaluateJob | DeriveJob | RenderJob,
) -> dict[str, Any]:
    """Inputs of an ``evaluate``/``derive``/``render`` run besides the bundle."""
    scope = ctx.scope
    if isinstance(job, RenderJob):
        if "plots.json" not in (mv.manifest.get("objects") or {}):
            raise MapiError(
                f"model version {mv.id} has no plots to render",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        plots = await verified_bundle_object(ctx.store, ctx.signer, mv, "plots.json")
        return {INPUT_PLOTS: StoredRef(plots.bucket, plots.key, plots.generation)}
    if isinstance(job, EvaluateJob):
        req = job.request
        dv = await _dataset_input(session, scope, req.data if req else None, "/params/request/data")
        tables: dict[str, Any] = dict(req.side_tables) if req else {}
    else:
        dv = await _dataset_input(session, scope, job.data, "/params/data")
        tables = {}
    if dv is None:
        dv = await _fit_dataset(session, mv)
    inputs = {INPUT_DATASET: data_ref(dv)}
    for name, ref in tables.items():
        tv = await _dataset_input(session, scope, ref, f"/params/request/side_tables/{name}")
        assert tv is not None
        inputs[side_table_input(name)] = data_ref(tv)
    return inputs


async def submit_model_run(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    kind: RunKind,
    model_version_id: str,
    request: ScoreRequest | None = None,
    params: dict[str, Any] | None = None,
    resource_class: ResourceClass | None = None,
    timeout: Duration | None = None,
    labels: dict[str, str] | None = None,
) -> m.Run:
    """``score``/``evaluate``/``derive``/``render`` against a verified model version."""
    scope = ctx.scope
    mv = await _ready_model_version(session, scope, model_version_id)
    ref = await _model_ref(session, mv)
    inputs: dict[str, Any] = {}
    payload: dict[str, Any]
    if kind is RunKind.SCORE:
        if request is None:
            raise MapiError("score runs need a request", code=ErrorCode.SCHEMA_INVALID)
        issues: list[Issue] = []
        datasets = await check_score_request(session, scope, request, mv, issues)
        if any(i.severity == "error" for i in issues):
            raise ValidationFailed(issues)
        payload = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload["model"] = ref
        for name, dv in datasets.items():
            inputs[name] = data_ref(dv)
    else:
        job = parse_model_job_params(kind, dict(params or {}))
        payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
        inputs.update(await _model_job_inputs(session, ctx, mv, job))
    bundle = await verified_bundle_object(ctx.store, ctx.signer, mv, FIT_BUNDLE)
    inputs[BUNDLE_INPUT] = StoredRef(bundle.bucket, bundle.key, bundle.generation)
    if BUNDLE_MANIFEST_OUTPUT in (mv.manifest.get("objects") or {}):
        sdk_manifest = await verified_bundle_object(
            ctx.store, ctx.signer, mv, BUNDLE_MANIFEST_OUTPUT
        )
        inputs[INPUT_BUNDLE_MANIFEST] = StoredRef(
            sdk_manifest.bucket, sdk_manifest.key, sdk_manifest.generation
        )
    run = await create_run(
        session,
        scope,
        kind=kind,
        resource_class=resource_class or ResourceClass.S,
        timeout_s=(timeout.seconds if timeout else DEFAULT_TIMEOUT_S),
        payload=payload,
        inputs=inputs,
        labels=labels,
        spec_hash=spec_hash(payload),
        model_version_id=mv.id,
        catalog_digest=mv.manifest.get("catalog_digest"),
    )
    return await _accept(session, run)


async def submit_run(session: AsyncSession, ctx: ServerContext, body: RunCreate) -> m.Run:
    """``POST /v1/runs``."""
    if body.kind in (RunKind.FIT, RunKind.VALIDATE):
        assert body.spec_id is not None and body.revision is not None
        return await submit_spec_run(
            session,
            ctx,
            kind=body.kind,
            spec_id=body.spec_id,
            revision=body.revision,
            resource_class=body.resource_class,
            timeout=body.timeout,
            labels=dict(body.labels),
        )
    assert body.model_version_id is not None
    return await submit_model_run(
        session,
        ctx,
        kind=body.kind,
        model_version_id=body.model_version_id,
        request=body.request,
        params=dict(body.params),
        resource_class=body.resource_class,
        timeout=body.timeout,
        labels=dict(body.labels),
    )


async def submit_retry(
    session: AsyncSession, ctx: ServerContext, old: m.Run, body: RunRetry
) -> m.Run:
    """A *new* run from a failed one (same resolved payload and pinned inputs)."""
    if old.state != RunState.FAILED.value:
        raise Conflict(f"only failed runs can be retried (run is {old.state})")
    payload = copy.deepcopy(old.payload)
    if body.seed is not None:
        if old.kind == RunKind.FIT.value and isinstance(payload.get("model"), dict):
            payload["model"].setdefault("fit", {})["seed"] = body.seed
        elif old.kind == RunKind.SCORE.value:
            payload["seed"] = body.seed
        else:
            raise MapiError(
                f"{old.kind} runs take no seed", code=ErrorCode.SCHEMA_INVALID, pointer="/seed"
            )
    labels = dict(old.labels)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind(old.kind),
        resource_class=body.resource_class or ResourceClass(old.resource_class),
        timeout_s=old.timeout_s,
        payload=payload,
        inputs=old.inputs,
        labels=labels,
        spec_id=old.spec_id,
        spec_revision=old.spec_revision,
        spec_hash=spec_hash(payload),
        model_version_id=old.model_version_id,
        dataset_id=old.dataset_id,
        dataset_version=old.dataset_version,
        retry_of=old.id,
        catalog_digest=old.catalog_digest,
        checkpointable=old.checkpointable,
    )
    return await _accept(session, run)


async def submit_ingest(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    dataset: m.Dataset,
    version: int,
    upload: m.Upload,
    generation: int,
    mapping: dict[str, Any],
) -> m.Run:
    job = IngestJob(
        mapping=mapping,
        dataset=f"dataset://{dataset.project_id}/{dataset.name}@v{version}",
        source=IngestSource(
            filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=upload.size_bytes,
        ),
    )
    payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
    snapshot = await ctx.catalogs.get(session)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind.INGEST,
        resource_class=ResourceClass.S,
        timeout_s=DEFAULT_TIMEOUT_S,
        payload=payload,
        inputs={INPUT_UPLOAD: StoredRef(upload.bucket, upload.object_name, generation)},
        spec_hash=spec_hash(payload),
        dataset_id=dataset.id,
        dataset_version=version,
        catalog_digest=snapshot.digest if snapshot else None,
    )
    return await _accept(session, run)


async def submit_describe(session: AsyncSession, ctx: ServerContext) -> m.Run | None:
    """Queue one ``describe`` run for the current image digest (unless one is pending)."""
    image = ctx.catalogs.image_digest
    pending = (
        await session.execute(
            select(m.Run.id).where(
                m.Run.kind == RunKind.DESCRIBE.value,
                m.Run.payload["image_digest"].astext == image,
                m.Run.state.notin_(
                    [s.value for s in RunState if s.terminal and s is not RunState.SUCCEEDED]
                ),
            )
        )
    ).first()
    if pending is not None:
        return None
    payload = DescribeJob(image_digest=image).model_dump(mode="json", exclude_none=True)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind.DESCRIBE,
        resource_class=ResourceClass.S,
        timeout_s=600,
        payload=payload,
        spec_hash=spec_hash(payload),
    )
    return await _accept(session, run)
