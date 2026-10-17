"""Model governance (M3.5): the check catalog, policies (project and tenant-wide,
versioned), validations of model versions, reviewers' approvals and the evidence pack.

Validations run the policies' checks as a sandboxed ``governance`` run (``202``); the
validation advances when the run finishes (``checks_passed`` / ``checks_failed``, then
``validated`` or ``awaiting_approval``). Approvals need the ``reviewer`` capability (or a
project admin) and are refused to the submitter and to API tokens. Every change is audited
and emitted as a webhook event. See :mod:`mapi_server.services.governance`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from mapi_spec.api import Page
from mapi_spec.audit import AuditAction
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    ApprovalCreate,
    CheckResult,
    EvidencePack,
    EvidencePackRequest,
    GovernanceCheckDef,
    GovernancePolicy,
    GovernancePolicyCreate,
    GovernancePolicyUpdate,
    ModelValidation,
    ValidationNote,
    ValidationRequest,
    ValidationStatus,
)
from mapi_spec.tenancy import ResourceKind
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from mapi_server.api.models import ModelId, VersionNo, _mv
from mapi_server.api.prior_translations import problem_responses
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services import governance as gov
from mapi_server.services.audit import write_audit

__all__ = ["router"]

router = APIRouter(tags=["governance"])

PolicyId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
ValidationId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
TenantSlug = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
_POL = "/v1/governance/policies"
_TPOL = "/v1/tenants/{tenant_id}/governance/policies"
_MV = "/v1/models/{model_id}/versions/{version}"
_VAL = "/v1/validations/{validation_id}"


def _page(items: list[Any], limit: int, cursor: str | None) -> tuple[list[Any], str | None]:
    """Offset pagination over an already ordered list."""
    start = int(decode_cursor(cursor) or 0)
    chunk = items[start : start + limit]
    more = start + limit < len(items)
    return chunk, encode_cursor(start + limit) if more else None


# ============================================================================== catalog


@router.get(
    "/v1/catalog/governance-checks",
    response_model=Page[GovernanceCheckDef],
    operation_id="list_governance_checks",
    summary="The governance check catalog",
    responses=problem_responses("list_governance_checks"),
)
async def list_governance_checks(
    limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[GovernanceCheckDef]:
    """Every check a policy can require, with its About card, metric, direction, default
    thresholds and parameter schema."""
    items, nxt = _page(list(GOVERNANCE_CHECKS.values()), limit, cursor)
    return Page[GovernanceCheckDef](items=items, next_cursor=nxt)


# ============================================================================= policies


async def _list(
    session: AsyncSession,
    tenant_id: str,
    project_id: str | None,
    scope: str | None,
    limit: int,
    cursor: str | None,
) -> Page[GovernancePolicy]:
    await gov.seed_default_policies(session, tenant_id)
    rows = await gov.latest_policy_rows(
        session,
        tenant_id,
        project_id=project_id,
        include_tenant=scope in (None, "tenant"),
        include_project=scope in (None, "project"),
    )
    items, nxt = _page([gov.policy_dto(r) for r in rows], limit, cursor)
    return Page[GovernancePolicy](items=items, next_cursor=nxt)


@router.get(
    _POL,
    response_model=Page[GovernancePolicy],
    operation_id="list_policies",
    summary="Policies in force in the project (its own and the tenant's)",
    responses=problem_responses("list_policies"),
)
async def list_policies(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    scope: Annotated[Literal["tenant", "project"] | None, Query()] = None,
) -> Page[GovernancePolicy]:
    """Tenant-wide policies first (every project's floor), then the project's own. The
    platform's default policies are seeded on the tenant's first use."""
    sc = ctx.scope
    return await _list(session, sc.tenant_id, sc.project_id, scope, limit, cursor)


@router.post(
    _POL,
    response_model=GovernancePolicy,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_policy",
    summary="Add a project policy (version 1)",
    responses=problem_responses("create_policy"),
)
async def create_policy(
    ctx: Ctx, session: Session, body: GovernancePolicyCreate
) -> GovernancePolicy:
    sc = ctx.scope
    row = await gov.create_policy(
        session, ctx, body, tenant_id=sc.tenant_id, project_id=sc.project_id
    )
    return gov.policy_dto(row)


@router.get(
    _POL + "/{policy_id}",
    response_model=GovernancePolicy,
    operation_id="get_policy",
    summary="The latest version of a policy",
    responses=problem_responses("get_policy"),
)
async def get_policy(ctx: Ctx, session: Session, policy_id: PolicyId) -> GovernancePolicy:
    """A project policy, or a tenant-wide one in force in the project."""
    sc = ctx.scope
    try:
        row = await gov.load_policy(session, sc.tenant_id, sc.project_id, policy_id)
    except NotFoundError:
        row = await gov.load_policy(session, sc.tenant_id, None, policy_id)
    return gov.policy_dto(row)


@router.put(
    _POL + "/{policy_id}",
    response_model=GovernancePolicy,
    operation_id="update_policy",
    summary="Save a new version (expected_version guards concurrent edits)",
    responses=problem_responses("update_policy"),
)
async def update_policy(
    ctx: Ctx, session: Session, policy_id: PolicyId, body: GovernancePolicyUpdate
) -> GovernancePolicy:
    sc = ctx.scope
    row = await gov.update_policy(
        session, ctx, policy_id, body, tenant_id=sc.tenant_id, project_id=sc.project_id
    )
    return gov.policy_dto(row)


@router.delete(
    _POL + "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_policy",
    summary="Retire a policy (its versions stay for audit)",
    responses=problem_responses("delete_policy"),
)
async def delete_policy(ctx: Ctx, session: Session, policy_id: PolicyId) -> Response:
    sc = ctx.scope
    await gov.retire_policy(
        session, ctx, policy_id, tenant_id=sc.tenant_id, project_id=sc.project_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    _POL + "/{policy_id}/versions",
    response_model=Page[GovernancePolicy],
    operation_id="list_policy_versions",
    summary="Every version of a policy, newest first",
    responses=problem_responses("list_policy_versions"),
)
async def list_policy_versions(
    ctx: Ctx,
    session: Session,
    policy_id: PolicyId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[GovernancePolicy]:
    sc = ctx.scope
    try:
        rows = await gov.policy_versions(session, sc.tenant_id, sc.project_id, policy_id)
    except NotFoundError:
        rows = await gov.policy_versions(session, sc.tenant_id, None, policy_id)
    items, nxt = _page([gov.policy_dto(r) for r in rows], limit, cursor)
    return Page[GovernancePolicy](items=items, next_cursor=nxt)


# --------------------------------------------------------------------- tenant policies


@router.get(
    _TPOL,
    response_model=Page[GovernancePolicy],
    operation_id="list_tenant_policies",
    summary="Tenant-wide policies (every project's floor)",
    responses=problem_responses("list_tenant_policies"),
)
async def list_tenant_policies(
    session: Session,
    tenant_id: TenantSlug,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[GovernancePolicy]:
    return await _list(session, tenant_id, None, "tenant", limit, cursor)


@router.post(
    _TPOL,
    response_model=GovernancePolicy,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_tenant_policy",
    summary="Add a tenant-wide policy",
    responses=problem_responses("create_tenant_policy"),
)
async def create_tenant_policy(
    ctx: Ctx, session: Session, tenant_id: TenantSlug, body: GovernancePolicyCreate
) -> GovernancePolicy:
    row = await gov.create_policy(session, ctx, body, tenant_id=tenant_id, project_id=None)
    return gov.policy_dto(row)


@router.get(
    _TPOL + "/{policy_id}",
    response_model=GovernancePolicy,
    operation_id="get_tenant_policy",
    summary="A tenant-wide policy",
    responses=problem_responses("get_tenant_policy"),
)
async def get_tenant_policy(
    session: Session, tenant_id: TenantSlug, policy_id: PolicyId
) -> GovernancePolicy:
    return gov.policy_dto(await gov.load_policy(session, tenant_id, None, policy_id))


@router.put(
    _TPOL + "/{policy_id}",
    response_model=GovernancePolicy,
    operation_id="update_tenant_policy",
    summary="Save a new version of a tenant-wide policy",
    responses=problem_responses("update_tenant_policy"),
)
async def update_tenant_policy(
    ctx: Ctx,
    session: Session,
    tenant_id: TenantSlug,
    policy_id: PolicyId,
    body: GovernancePolicyUpdate,
) -> GovernancePolicy:
    row = await gov.update_policy(
        session, ctx, policy_id, body, tenant_id=tenant_id, project_id=None
    )
    return gov.policy_dto(row)


@router.delete(
    _TPOL + "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_tenant_policy",
    summary="Retire a tenant-wide policy",
    responses=problem_responses("delete_tenant_policy"),
)
async def delete_tenant_policy(
    ctx: Ctx, session: Session, tenant_id: TenantSlug, policy_id: PolicyId
) -> Response:
    await gov.retire_policy(session, ctx, policy_id, tenant_id=tenant_id, project_id=None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ========================================================================== validations


async def _validation_page(
    session: AsyncSession, stmt: Any, limit: int, cursor: str | None
) -> Page[ModelValidation]:
    rows = list((await session.execute(stmt)).scalars())
    chunk, nxt = _page(rows, limit, cursor)
    return Page[ModelValidation](
        items=[await gov.validation_dto(session, r) for r in chunk], next_cursor=nxt
    )


@router.post(
    _MV + "/validations",
    response_model=ModelValidation,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="request_model_validation",
    summary="Submit a model version: runs the policy checks as a governance run",
    responses={
        200: {"model": ModelValidation, "description": "Idempotent replay"},
        **problem_responses("request_model_validation"),
    },
)
async def request_model_validation(
    ctx: Ctx,
    session: Session,
    request: Request,
    model_id: ModelId,
    version: VersionNo,
    body: ValidationRequest,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Every policy in force for the model's family (plus any ``policy_ids``, which can only
    add policies) decides the checks; they run in the sandbox and the validation advances
    when they finish."""
    idem = Idempotency(session, ctx.scope, idempotency_key)
    replay = await idem.begin(f"POST {_MV}/validations", await request.json())
    if replay is not None:
        v = await gov.load_validation(session, ctx.scope, replay.resource_id)
        return JSONResponse((await gov.validation_dto(session, v)).model_dump(mode="json"), 200)
    _model, mv = await _mv(ctx, session, model_id, version)
    v = await gov.request_validation(session, ctx, mv, body)
    await idem.complete("validation", v.id, 202)
    return JSONResponse((await gov.validation_dto(session, v)).model_dump(mode="json"), 202)


@router.get(
    _MV + "/validations",
    response_model=Page[ModelValidation],
    operation_id="list_model_validations",
    summary="Validations of a model version, newest first",
    responses=problem_responses("list_model_validations"),
)
async def list_model_validations(
    ctx: Ctx,
    session: Session,
    model_id: ModelId,
    version: VersionNo,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[ModelValidation]:
    _model, mv = await _mv(ctx, session, model_id, version)
    stmt = (
        select(m.ModelValidation)
        .where(m.ModelValidation.model_version_id == mv.id)
        .order_by(m.ModelValidation.created_at.desc(), m.ModelValidation.id.desc())
    )
    return await _validation_page(session, stmt, limit, cursor)


@router.get(
    "/v1/validations",
    response_model=Page[ModelValidation],
    operation_id="list_validations",
    summary="Validations of the project (filter: status, e.g. the reviewers' queue)",
    responses=problem_responses("list_validations"),
)
async def list_validations(
    ctx: Ctx,
    session: Session,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_: Annotated[ValidationStatus | None, Query(alias="status")] = None,
) -> Page[ModelValidation]:
    """``?status=awaiting_approval`` is the reviewers' queue."""
    sc = ctx.scope
    stmt = select(m.ModelValidation).where(
        m.ModelValidation.tenant_id == sc.tenant_id, m.ModelValidation.project_id == sc.project_id
    )
    if status_ is not None:
        stmt = stmt.where(m.ModelValidation.status == status_.value)
    stmt = stmt.order_by(m.ModelValidation.updated_at.desc(), m.ModelValidation.id.desc())
    return await _validation_page(session, stmt, limit, cursor)


@router.get(
    _VAL,
    response_model=ModelValidation,
    operation_id="get_validation",
    summary="A validation: status, results, approvals, history",
    responses=problem_responses("get_validation"),
)
async def get_validation(
    ctx: Ctx, session: Session, validation_id: ValidationId
) -> ModelValidation:
    v = await gov.load_validation(session, ctx.scope, validation_id)
    return await gov.validation_dto(session, v)


@router.get(
    _VAL + "/evidence",
    response_model=Page[CheckResult],
    operation_id="list_validation_evidence",
    summary="Every check result with its evidence",
    responses=problem_responses("list_validation_evidence"),
)
async def list_validation_evidence(
    ctx: Ctx,
    session: Session,
    validation_id: ValidationId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[CheckResult]:
    v = await gov.load_validation(session, ctx.scope, validation_id)
    results = [CheckResult.model_validate(r) for r in v.results or []]
    items, nxt = _page(results, limit, cursor)
    return Page[CheckResult](items=items, next_cursor=nxt)


@router.post(
    _VAL + ":cancel",
    response_model=ModelValidation,
    operation_id="cancel_validation",
    summary="Stop running checks (back to draft)",
    responses=problem_responses("cancel_validation"),
)
async def cancel_validation(
    ctx: Ctx, session: Session, validation_id: ValidationId, body: ValidationNote
) -> ModelValidation:
    v = await gov.load_validation(session, ctx.scope, validation_id, for_update=True)
    await gov.cancel_validation(session, ctx, v, body.note)
    return await gov.validation_dto(session, v)


@router.post(
    _VAL + ":resubmit",
    response_model=ModelValidation,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="resubmit_validation",
    summary="Run the checks again after a failure or a rejection",
    responses=problem_responses("resubmit_validation"),
)
async def resubmit_validation(
    ctx: Ctx, session: Session, validation_id: ValidationId, body: ValidationNote
) -> ModelValidation:
    """A new round under the policies in force now (earlier approvals no longer count)."""
    v = await gov.load_validation(session, ctx.scope, validation_id, for_update=True)
    await gov.resubmit_validation(session, ctx, v, body.note)
    return await gov.validation_dto(session, v)


@router.post(
    _VAL + "/approvals",
    response_model=ModelValidation,
    operation_id="decide_validation",
    summary="A reviewer approves or rejects (never the submitter; audited)",
    responses=problem_responses("decide_validation"),
)
async def decide_validation(
    ctx: Ctx, session: Session, validation_id: ValidationId, body: ApprovalCreate
) -> ModelValidation:
    """Needs the ``reviewer`` capability (or project admin) and a signed-in session; a
    rejection needs a comment."""
    v = await gov.load_validation(session, ctx.scope, validation_id, for_update=True)
    await gov.decide(session, ctx, v, body)
    return await gov.validation_dto(session, v)


@router.post(
    _VAL + ":deprecate",
    response_model=ModelValidation,
    operation_id="deprecate_validation",
    summary="Retire a validated model version (audited with the reason)",
    responses=problem_responses("deprecate_validation"),
)
async def deprecate_validation(
    ctx: Ctx, session: Session, validation_id: ValidationId, body: ValidationNote
) -> ModelValidation:
    v = await gov.load_validation(session, ctx.scope, validation_id, for_update=True)
    await gov.deprecate_validation(session, ctx, v, body.note)
    return await gov.validation_dto(session, v)


@router.post(
    _VAL + "/evidence-pack",
    response_model=EvidencePack,
    operation_id="export_evidence_pack",
    summary="The audit export (JSON, and a signed link to the HTML rendering)",
    responses=problem_responses("export_evidence_pack"),
)
async def export_evidence_pack(
    ctx: Ctx, session: Session, validation_id: ValidationId, body: EvidencePackRequest
) -> EvidencePack:
    """Self-contained: the HTML has inline styles, no scripts and no external assets."""
    v = await gov.load_validation(session, ctx.scope, validation_id)
    pack = await gov.evidence_pack(session, ctx, v)
    if body.format == "html":
        link = await gov.store_evidence_html(ctx, v, pack)
        pack = pack.model_copy(update={"download": link.model_dump(mode="json")})
    await _audit_export(ctx, session, v, body.format, pack.digest)
    return pack


async def _audit_export(
    ctx: ServerContext, session: AsyncSession, v: m.ModelValidation, fmt: str, digest: str | None
) -> None:
    if ctx.request is None:  # pragma: no cover - routes always have a caller
        return
    await write_audit(
        ctx.db,
        [
            ctx.request.audit(
                ctx,
                AuditAction.MODEL_EVIDENCE_EXPORT,
                ResourceKind.MODEL,
                resource_id=v.id,
                details={"format": fmt, "digest": digest},
            )
        ],
        session=session,
    )
