"""Run submission: resolve, statically validate, pin inputs, then CREATED → QUEUED.

The API owns ``CREATED``/``REJECTED`` (plan §8.3). Requests that fail static validation are
refused with the first error's code (no run is created); accepted runs go straight to
``QUEUED`` for the dispatcher. Every input object is pinned to a generation here, so the
RunSpec built later signs exactly what was validated.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from mapi_spec._types import Duration
from mapi_spec.api import RunCreate, RunRetry
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.capabilities import Capabilities
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, MapiError, ValidationFailed
from mapi_spec.executor import RunKind
from mapi_spec.ingest import IngestAssembly
from mapi_spec.jobs import (
    BUNDLE_MANIFEST_OUTPUT,
    DATATREE_OUTPUT,
    FIT_BUNDLE,
    INGEST_DATA,
    INPUT_BUNDLE,
    INPUT_BUNDLE_MANIFEST,
    INPUT_DATASET,
    INPUT_HISTORY,
    INPUT_PLOTS,
    INPUT_UPLOAD,
    DeriveJob,
    DescribeJob,
    EvaluateJob,
    IngestJob,
    IngestSource,
    InspectJob,
    RenderJob,
    parse_model_job_params,
    side_table_input,
    upload_input,
)
from mapi_spec.loader import spec_hash
from mapi_spec.quotas import QuotaDemand, default_priority
from mapi_spec.refs import DatasetRef
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.spec import DEFAULT_FIT_SEED, FutureCovariates, ModelSpec, ScoreRequest
from mapi_spec.tenancy import ResourceKind, TrustTier
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError, ProblemWithHeaders
from mapi_server.executors import PlacementRefused, placement_refusal, reported_caps
from mapi_server.services.audit import write_audit
from mapi_server.services.bundles import verified_bundle_object, verify_bundle_inputs
from mapi_server.services.catalog import CatalogSnapshot
from mapi_server.services.cost import run_estimate
from mapi_server.services.evidence import placement_problem
from mapi_server.services.experiments import check_run_experiment
from mapi_server.services.quotas import admit_run
from mapi_server.services.runs import Scope, apply_transition, create_run, now
from mapi_server.services.runspec import StoredRef
from mapi_server.services.specs import (
    Validation,
    check_score_request,
    class_for_size,
    data_cap_issue,
    pin_document,
    resolve_dataset,
    score_draw_cap,
    validate_document,
)
from mapi_server.services.verification import require_uploaded_code_allowed

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "admit",
    "data_object_name",
    "data_ref",
    "submit_describe",
    "submit_ingest",
    "submit_inspect",
    "submit_model_run",
    "submit_retry",
    "submit_run",
    "submit_spec_run",
]

DEFAULT_TIMEOUT_S = 1800
BUNDLE_INPUT = INPUT_BUNDLE


def data_object_name(dv: m.DatasetVersion) -> str:
    """The data object of a dataset version: ``data.tree.zip`` for a tree version (M4),
    else ``data.zarr.zip``."""
    return DATATREE_OUTPUT if dv.layout == "tree" else INGEST_DATA


def data_ref(dv: m.DatasetVersion, name: str | None = None) -> dict[str, Any]:
    """The generation-pinned input ref of a version's data (``name`` defaults to
    :func:`data_object_name`; the RunSpec then tells the runner its layout)."""
    name = name or data_object_name(dv)
    obj = dv.objects.get(name)
    if obj is None:
        raise MapiError(
            f"dataset version {dv.version} has no {name}", code=ErrorCode.REF_UNRESOLVED
        )
    return StoredRef(obj["bucket"], obj["name"], int(obj["generation"]))


async def _accept(
    session: AsyncSession, ctx: ServerContext, run: m.Run, *, untrusted: bool = False
) -> m.Run:
    """Admit (priority, estimate, kill switches, quotas) and queue a ``CREATED`` run."""
    await admit(session, ctx, run, untrusted=untrusted)
    await apply_transition(session, run, RunTrigger.ACCEPT, Actor.API)
    return run


async def admit(
    session: AsyncSession,
    ctx: ServerContext,
    run: m.Run,
    *,
    untrusted: bool = False,
    audit_action: AuditAction | None = AuditAction.RUN_CREATE,
) -> None:
    """Stamp scheduling and accounting fields, then check kill switches and quotas.

    Runs created by a request are checked against placement (an executor that reported
    recently must be able to take the tenant's tier: ``INFRA_ERROR``/503 otherwise), the kill
    switches and the token, tenant and global quotas (``QUOTA_EXCEEDED``/429, audited; no run
    is created). Platform runs (``describe``) are only stamped.
    """
    settings = ctx.settings
    kind = RunKind(run.kind)
    rc = ResourceClass(run.resource_class)
    run.priority = default_priority(kind).value
    run.untrusted_code = untrusted
    wall = min(settings.estimate_wall_s.get(kind.value, 300.0), float(run.timeout_s))
    estimate = run_estimate(ctx.price_table, rc, wall, max(1, run.task_count or 1))
    run.estimate = estimate.model_dump(mode="json") if estimate is not None else None
    auth = ctx.request
    if untrusted:  # cut line A: uploaded code needs a trusted tier or identity verification
        try:
            await require_uploaded_code_allowed(session, ctx, run.tenant_id)
        except MapiError as exc:
            if auth is not None:
                await write_audit(
                    ctx.db,
                    [auth.audit(ctx, AuditAction.RUN_CREATE, ResourceKind.RUN,
                                AuditOutcome.DENIED, code=exc.code,
                                details={"kind": kind.value, "untrusted_code": True})],
                )  # fmt: skip
            raise
    if auth is None:
        return
    run.token_id = auth.token.id if auth.token is not None else None
    run.created_by = auth.user_id
    await _check_placement(session, ctx, run, rc)
    demand = QuotaDemand(
        new_runs=1,
        vcpu_seconds=estimate.vcpu_seconds if estimate is not None else 0.0,
        fanout_tasks=max(1, run.task_count or 1),
        resource_class=rc,
    )
    tenant = await session.get(m.Tenant, run.tenant_id)
    async with ctx.db.session() as system:
        if tenant is not None:
            # refuse up front what no executor can place (trust tier, class, residency,
            # shared-tier evidence) instead of failing the run at lease
            problem = await placement_problem(
                system,
                tier=tenant.trust_tier,
                resource_class=rc,
                residency=tenant.data_residency,
                now=now(),
            )
            if problem is not None:
                detail = f"no executor can run {tenant.trust_tier}-tier work now ({problem})"
                raise MapiError(detail[:2000], code=ErrorCode.INFRA_ERROR)
        decision = await admit_run(
            session,
            system,
            tenant_id=run.tenant_id,
            token=auth.token,
            demand=demand,
            untrusted_code=untrusted,
            now=now(),
        )
    if not decision.allowed:
        assert decision.code is not None
        details: dict[str, JsonValue] = {
            "kind": kind.value,
            "resource_class": rc.value,
            "scope": decision.scope.value if decision.scope else None,
            "limit_name": decision.limit_name.value if decision.limit_name else None,
        }
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.QUOTA_DENIED,
                    ResourceKind.RUN,
                    AuditOutcome.DENIED,
                    code=decision.code,
                    details=details,
                )
            ],
        )
        raise ProblemWithHeaders(
            decision.message or "quota exceeded", code=decision.code, headers=decision.headers()
        )
    if audit_action is None:
        return
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                audit_action,
                ResourceKind.RUN,
                resource_id=run.id,
                details={"kind": kind.value, "resource_class": rc.value},
            )
        ],
        session=session,
    )


async def _check_placement(
    session: AsyncSession, ctx: ServerContext, run: m.Run, rc: ResourceClass
) -> None:
    """Refuse at admission (403) what no running executor could place for good: the
    resource class or the tenant's residency. Executors are known from their dispatchers'
    recent reports; without any, the dispatcher decides. A trust tier no executor can take
    *now* (isolation, egress and DNS enforcement, shared-tier evidence) is a platform state,
    not the tenant's: :func:`mapi_server.services.evidence.placement_problem` refuses it with
    503."""
    tenant = await session.get(m.Tenant, run.tenant_id)
    if tenant is None:
        return
    max_age = timedelta(seconds=6 * ctx.settings.executor_report_interval_s)
    async with ctx.db.session() as system:
        offered = await reported_caps(system, max_age=max_age)
    why = placement_refusal(
        offered,
        trust_tier=TrustTier.INTERNAL.value,  # no isolation requirement: class and residency
        resource_class=rc,
        residency=tenant.data_residency,
    )
    if why is not None:
        raise PlacementRefused(
            f"no executor of this platform can run this tenant's work ({why}); "
            "an operator must change the tenant's residency"
        )


def _overlay(snapshot: CatalogSnapshot | None, model_ref: str | None) -> bool:
    """Does the resolved model plugin ship tenant code (an overlay tarball)?"""
    if snapshot is None or not model_ref:
        return False
    for entry in snapshot.catalog.models:
        if entry.ref == model_ref:
            return entry.overlay_hash is not None
    return False


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
        session,
        scope,
        ctx.catalogs,
        document=copy.deepcopy(rev.document),
        resource_class=resource_class,
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
    if isinstance(payload.get("model"), dict):
        # pin the effective seed like `use:` ranges, so lineage and replays record what the
        # runner used (it falls back to DEFAULT_FIT_SEED when the spec omits one)
        fit = payload["model"].setdefault("fit", {})
        if isinstance(fit, dict) and fit.get("seed") is None:
            fit["seed"] = DEFAULT_FIT_SEED
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
    return await _accept(
        session, ctx, run, untrusted=_overlay(v.catalog, v.resolved.model.resolved)
    )


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


async def fit_dataset_version(session: AsyncSession, mv: m.ModelVersion) -> m.DatasetVersion:
    """The ready dataset version ``mv`` was fitted on (``REF_UNRESOLVED`` otherwise)."""
    return await _fit_dataset(session, mv)


async def fit_dataset_ref(session: AsyncSession, mv: m.ModelVersion) -> str:
    """``dataset://<project>/<name>@v<n>``: the dataset version ``mv`` was fitted on
    (``REF_UNRESOLVED`` when it is not recorded or no longer ready)."""
    dv = await _fit_dataset(session, mv)
    ds = await session.get(m.Dataset, dv.dataset_id)
    if ds is None:
        raise MapiError(
            f"the fit dataset of model version {mv.id} is no longer available",
            code=ErrorCode.REF_UNRESOLVED,
        )
    return f"dataset://{ds.project_id}/{ds.name}@v{dv.version}"


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
    read: list[m.DatasetVersion],
) -> dict[str, Any]:
    """Inputs of an ``evaluate``/``derive``/``render`` run besides the bundle (the dataset
    versions it reads are appended to ``read``)."""
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
    read.append(dv)
    for name, ref in tables.items():
        tv = await _dataset_input(session, scope, ref, f"/params/request/side_tables/{name}")
        assert tv is not None
        inputs[side_table_input(name)] = data_ref(tv)
        read.append(tv)
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
    extra_inputs: dict[str, Any] | None = None,
    inline_summary: DataSummary | None = None,
) -> m.Run:
    """``score``/``evaluate``/``derive``/``render`` against a verified model version.

    ``extra_inputs`` are further pinned run inputs (an endpoint call's inline payloads,
    already stored) and ``inline_summary`` summarises the one the score run reads."""
    scope = ctx.scope
    mv = await _ready_model_version(session, scope, model_version_id)
    ref = await _model_ref(session, mv)
    inputs: dict[str, Any] = dict(extra_inputs or {})
    read: list[m.DatasetVersion] = []
    payload: dict[str, Any]
    if kind is RunKind.SCORE:
        if request is None:
            raise MapiError("score runs need a request", code=ErrorCode.SCHEMA_INVALID)
        issues: list[Issue] = []
        datasets = await check_score_request(
            session, scope, request, mv, issues, inline=inline_summary, catalogs=ctx.catalogs
        )
        if any(i.severity == "error" for i in issues):
            raise ValidationFailed(issues)
        req_payload = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        req_payload["model"] = ref
        # the runner enforces the tier's draw cap again (plan Q2)
        payload = {"request": req_payload, "max_draws": await score_draw_cap(session, scope)}
        for name, dv in datasets.items():
            inputs[name] = data_ref(dv)
            read.append(dv)
        if _needs_history(request):
            fit_dv = await _fit_dataset(session, mv)
            inputs[INPUT_HISTORY] = data_ref(fit_dv)
            read.append(fit_dv)
    else:
        job = parse_model_job_params(kind, dict(params or {}))
        payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
        inputs.update(await _model_job_inputs(session, ctx, mv, job, read))
    cap = data_cap_issue(resource_class or ResourceClass.S, read, "/resource_class")
    if cap is not None:
        raise ValidationFailed([cap])
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
    return await _accept(session, ctx, run, untrusted=bool((mv.lineage or {}).get("overlay_hash")))


def _needs_history(request: ScoreRequest) -> bool:
    """A forecast whose missing covariates are extended from the fitted series (M3)."""
    return request.mode == "forecast" and request.future_covariates in (
        FutureCovariates.CARRY_LAST,
        FutureCovariates.SEASONAL_NAIVE,
    )


async def submit_run(session: AsyncSession, ctx: ServerContext, body: RunCreate) -> m.Run:
    """``POST /v1/runs``. ``experiment_id`` (v0.14) must be an experiment of the project
    that is not archived (unknown: 422 at ``/experiment_id``; archived: 409)."""
    if body.experiment_id is not None:
        await check_run_experiment(session, ctx.scope, body.experiment_id)
    if body.kind in (RunKind.FIT, RunKind.VALIDATE):
        assert body.spec_id is not None and body.revision is not None
        run = await submit_spec_run(
            session,
            ctx,
            kind=body.kind,
            spec_id=body.spec_id,
            revision=body.revision,
            resource_class=body.resource_class,
            timeout=body.timeout,
            labels=dict(body.labels),
        )
    else:
        assert body.model_version_id is not None
        run = await submit_model_run(
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
    if body.experiment_id is not None:
        run.experiment_id = body.experiment_id
        await session.flush()
    return run


async def _refuse_orchestrated_retry(session: AsyncSession, old: m.Run) -> None:
    """Workflow runs, their node runs, governance runs and (M4) inspect runs are driven by
    their owner (the workflow orchestrator, a validation, an upload): a stand-alone retry
    would be a run nobody collects, so it is refused (``CONFLICT``) with where to start
    again instead."""
    kind = RunKind(old.kind)
    if kind in (RunKind.WORKFLOW, RunKind.WORKFLOW_NODE):
        workflow_id: object = (old.payload or {}).get("workflow_id")
        if kind is RunKind.WORKFLOW_NODE:
            parent = (old.payload or {}).get("workflow_run_id") or old.parent_run_id
            wr = await session.get(m.WorkflowRunRow, str(parent)) if parent else None
            workflow_id = wr.workflow_id if wr is not None else None
        where = f"POST /v1/workflows/{workflow_id}/runs" if workflow_id else "the workflow"
        raise Conflict(
            f"{kind.value} runs are not retried one by one: start the workflow again "
            f"({where}); unchanged nodes are reused from the cache"
        )
    if kind is RunKind.INSPECT:
        # M4: the upload records its inspect run; ask the upload instead
        upload = (old.labels or {}).get("upload") or "{upload_id}"
        raise Conflict(
            "inspect runs are not retried one by one: inspect the upload again "
            f"(POST /v1/datasets/{old.dataset_id or '{dataset_id}'}/uploads/{upload}/inspect)"
        )
    if kind is RunKind.GOVERNANCE:
        validation = (old.labels or {}).get("validation")
        raise Conflict(
            "governance runs are not retried one by one: resubmit the validation "
            f"(POST /v1/validations/{validation or '{validation_id}'}:resubmit)"
        )


async def submit_retry(
    session: AsyncSession, ctx: ServerContext, old: m.Run, body: RunRetry
) -> m.Run:
    """A *new* run from a failed one (same resolved payload and pinned inputs)."""
    if old.state != RunState.FAILED.value:
        raise Conflict(f"only failed runs can be retried (run is {old.state})")
    await _refuse_orchestrated_retry(session, old)
    if old.experiment_id is not None:  # the retry joins it: an archived one refuses (409)
        await check_run_experiment(session, ctx.scope, old.experiment_id)
    if old.model_version_id is not None:  # its pinned bundle inputs must still verify
        mv = await _ready_model_version(session, ctx.scope, old.model_version_id)
        await verify_bundle_inputs(ctx.store, ctx.signer, mv, old.inputs)
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
        experiment_id=old.experiment_id,  # v0.14: a retry stays in its run's experiment
    )
    return await _accept(session, ctx, run, untrusted=old.untrusted_code)


async def submit_ingest(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    dataset: m.Dataset,
    version: int,
    uploads: list[tuple[m.Upload, int]],
    mapping: dict[str, Any] | None = None,
    assembly: IngestAssembly | None = None,
) -> m.Run:
    """One ``ingest`` run for a new dataset version: a single upload with its ``mapping``
    (input ``upload``) or, M4, the uploads of an ``assembly`` in source order (inputs
    ``upload.<i>``). ``uploads`` pairs each upload with the generation it is pinned to."""
    if (mapping is None) == (assembly is None) or not uploads:
        raise ValueError("give a mapping (one upload) or an assembly")
    total = sum(upload.size_bytes for upload, _ in uploads)
    # plan §8.5: the smallest class whose dataset cap holds the upload(s) (the API caps the
    # total at class L's limit, so one always fits)
    resource_class = class_for_size(total) or ResourceClass.L
    # M3: whatever an upload decodes to must fit the class's scratch budget (Cloud Run's
    # filesystem lives in RAM), so a decompression bomb fails as PAYLOAD_TOO_LARGE first
    scratch = RESOURCE_CLASSES[resource_class].scratch_bytes
    caps: dict[str, Any] = {} if scratch is None else {"max_uncompressed_bytes": scratch}
    ref = f"dataset://{dataset.project_id}/{dataset.name}@v{version}"
    inputs: dict[str, Any]
    if assembly is not None:
        if len(uploads) != len(assembly.sources):
            raise ValueError("one upload per assembly source")
        job = IngestJob(assembly=assembly, dataset=ref, **caps)
        inputs = {
            upload_input(i): StoredRef(upload.bucket, upload.object_name, generation)
            for i, (upload, generation) in enumerate(uploads)
        }
    else:
        upload, generation = uploads[0]
        job = IngestJob(
            mapping=mapping,
            dataset=ref,
            source=IngestSource(
                filename=upload.filename,
                content_type=upload.content_type,
                size_bytes=upload.size_bytes,
            ),
            **caps,
        )
        inputs = {INPUT_UPLOAD: StoredRef(upload.bucket, upload.object_name, generation)}
    payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
    snapshot = await ctx.catalogs.get(session)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind.INGEST,
        resource_class=resource_class,
        timeout_s=DEFAULT_TIMEOUT_S,
        payload=payload,
        inputs=inputs,
        spec_hash=spec_hash(payload),
        dataset_id=dataset.id,
        dataset_version=version,
        catalog_digest=snapshot.digest if snapshot else None,
    )
    return await _accept(session, ctx, run)


async def submit_inspect(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    dataset: m.Dataset,
    upload: m.Upload,
    generation: int,
) -> m.Run:
    """An ``inspect`` run (M4) listing the groups and arrays of ``upload`` from its metadata:
    the short ``inspect_timeout_s``, interactive priority, and the resource class an ingest
    of the upload would get (its whole file is downloaded and its declared arrays must fit
    the same scratch budget, so a file ingest accepts is never refused here). The caller
    records the run on the upload (``inspect_run_id``)."""
    resource_class = class_for_size(upload.size_bytes) or ResourceClass.L
    scratch = RESOURCE_CLASSES[resource_class].scratch_bytes
    caps: dict[str, Any] = {} if scratch is None else {"max_uncompressed_bytes": scratch}
    job = InspectJob(
        source=IngestSource(
            filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=upload.size_bytes,
        ),
        **caps,
    )
    payload = job.model_dump(mode="json", by_alias=True, exclude_none=True)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind.INSPECT,
        resource_class=resource_class,
        timeout_s=ctx.settings.inspect_timeout_s,
        payload=payload,
        inputs={INPUT_UPLOAD: StoredRef(upload.bucket, upload.object_name, generation)},
        labels={"upload": upload.id},
        spec_hash=spec_hash(payload),
        dataset_id=dataset.id,
    )
    return await _accept(session, ctx, run)


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
    return await _accept(session, ctx, run)
