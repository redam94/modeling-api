"""Model governance (M3.5): versioned policies, validations of model versions, approvals,
the evidence pack, and the promotion requirement.

* **Policies** (:class:`~mapi_server.db.models.GovernancePolicyRow`) are versioned rows,
  tenant-wide (every project's floor) or per project. The platform's
  :data:`~mapi_spec.governance.DEFAULT_POLICIES` are seeded as tenant policies the first time
  a tenant uses governance (:func:`seed_default_policies`).
* **Validations** follow the pure state machine of :func:`mapi_spec.governance.transition`:
  a request creates one and submits it, which starts a sandboxed ``governance`` run
  (:class:`~mapi_spec.governance.GovernanceJob`); the finalizer stores the run's
  ``governance.json`` results (:func:`record_report`) and the run's terminal transition
  advances the validation (:func:`governance_run_finished`): ``checks_passed`` then
  ``validated`` or ``awaiting_approval``, or ``checks_failed``. Every transition is audited
  and emitted as a webhook event in the transaction that causes it.
* **Approvals** need a reviewer (the ``reviewer`` capability or project admin) who is not
  the submitter; ``approvals_required`` approvals in the current round validate, one
  rejection rejects.
* **Promotion**: when a policy in force says ``require_validated_for_promotion``, only a
  ``validated`` version passes the promotion gate (:func:`promotion_requirement`).
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from mapi_spec.api import ArtifactLink, new_id
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.governance import (
    DEFAULT_POLICIES,
    EVIDENCE_PACK_FORMAT,
    GOVERNANCE_CHECKS,
    GOVERNANCE_OUTPUT,
    SYSTEM_ACTOR,
    Approval,
    ApprovalCreate,
    CheckResult,
    CheckStatus,
    EvidencePack,
    GovernanceJob,
    GovernancePolicy,
    GovernancePolicyCreate,
    GovernancePolicyUpdate,
    GovernanceReport,
    ModelValidation,
    PolicyCheck,
    RefitBudget,
    ValidationEvent,
    ValidationRequest,
    ValidationStatus,
    ValidationTransitionRecord,
    after_checks,
    check_approver,
    effective_checks,
    policies_for,
    requires_validation,
    rollup_results,
    transition,
)
from mapi_spec.jobs import (
    BUNDLE_MANIFEST_OUTPUT,
    FIT_BUNDLE,
    INPUT_BUNDLE,
    INPUT_BUNDLE_MANIFEST,
    INPUT_DATASET,
    INPUT_EVALUATION,
    INPUT_EVALUATION_MANIFEST,
)
from mapi_spec.loader import canonical_json, spec_hash
from mapi_spec.refs import DatasetRef
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.scorecard import CheckDirection
from mapi_spec.tenancy import Action, PrincipalKind, ResourceKind
from mapi_spec.webhooks import WebhookEventType
from pydantic import JsonValue, ValidationError
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.audit import add_audit
from mapi_server.services.bundles import artifact_link, verified_bundle_object
from mapi_server.services.runs import Scope, create_run, now
from mapi_server.services.runspec import StoredRef
from mapi_server.services.webhooks.outbox import emit_event

__all__ = [
    "EVALUATION_BUNDLE",
    "EVALUATION_MANIFEST",
    "GOVERNANCE_RUN_TIMEOUT_S",
    "PolicyRequirement",
    "cancel_validation",
    "create_policy",
    "decide",
    "deprecate_validation",
    "evidence_pack",
    "evidence_pack_html",
    "governance_run_finished",
    "latest_policy_rows",
    "load_policy",
    "load_validation",
    "model_family",
    "parse_report",
    "policies_in_force",
    "policy_dto",
    "policy_versions",
    "promotion_requirement",
    "record_report",
    "request_validation",
    "resubmit_validation",
    "retire_policy",
    "seed_default_policies",
    "update_policy",
    "validation_dto",
    "workflow_policy_resolver",
    "workflow_validation_submitter",
]

log = logging.getLogger(__name__)

GOVERNANCE_RUN_TIMEOUT_S: Final = 3600
EVALUATION_BUNDLE: Final = "evaluation/bundle.zarr.zip"
EVALUATION_MANIFEST: Final = "evaluation/manifest.json"
REPORT_MAX_BYTES: Final = 4 * 1024 * 1024
_OPEN: Final = frozenset(
    {
        ValidationStatus.CHECKS_RUNNING,
        ValidationStatus.CHECKS_PASSED,
        ValidationStatus.AWAITING_APPROVAL,
        ValidationStatus.VALIDATED,
    }
)
"""A model version with a validation in one of these states cannot start another."""

_EVENT_WEBHOOK: Final[dict[ValidationStatus, WebhookEventType]] = {
    ValidationStatus.CHECKS_PASSED: WebhookEventType.MODEL_VALIDATION_CHECKS_PASSED,
    ValidationStatus.CHECKS_FAILED: WebhookEventType.MODEL_VALIDATION_CHECKS_FAILED,
    ValidationStatus.AWAITING_APPROVAL: WebhookEventType.MODEL_VALIDATION_AWAITING_APPROVAL,
    ValidationStatus.VALIDATED: WebhookEventType.MODEL_VALIDATED,
    ValidationStatus.REJECTED: WebhookEventType.MODEL_REJECTED,
    ValidationStatus.DEPRECATED: WebhookEventType.MODEL_DEPRECATED,
}


# ============================================================================== actors


@dataclass(frozen=True, slots=True)
class _Actor:
    """Who caused a change: a request's caller, or the platform (finalizer, reconciler)."""

    ctx: ServerContext | None
    id: str | None

    @classmethod
    def of(cls, ctx: ServerContext) -> _Actor:
        auth = ctx.request
        return cls(ctx, auth.user_id if auth is not None else SYSTEM_ACTOR)

    def audit(
        self,
        action: AuditAction,
        resource_kind: ResourceKind,
        *,
        tenant_id: str,
        project_id: str | None,
        resource_id: str | None,
        details: dict[str, JsonValue] | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        code: ErrorCode | None = None,
    ) -> Any:
        if self.ctx is not None and self.ctx.request is not None:
            return self.ctx.request.audit(
                self.ctx,
                action,
                resource_kind,
                outcome,
                resource_id=resource_id,
                code=code,
                details=details,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        return make_audit_event(
            action=action,
            resource_kind=resource_kind,
            outcome=outcome,
            ts=now(),
            fingerprint_key=b"",
            tenant_id=tenant_id,
            project_id=project_id,
            actor_kind=PrincipalKind.SERVICE,
            actor_id="svc_governance",
            resource_id=resource_id,
            code=code,
            details=details,
        )


# ============================================================================= policies


def policy_dto(row: m.GovernancePolicyRow) -> GovernancePolicy:
    return GovernancePolicy.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "project_id": row.project_id,
            "scope": row.scope,
            "version": row.version,
            "name": row.name,
            "description": row.description,
            "applies_to": row.applies_to or {},
            "checks": row.checks or [],
            "require_approval": row.require_approval,
            "approvals_required": row.approvals_required,
            "require_validated_for_promotion": row.require_validated_for_promotion,
            "active": row.active and row.retired_at is None,
            "created_at": row.created_at,
            "created_by": row.created_by,
        }
    )


def _row(
    body: GovernancePolicyCreate,
    *,
    policy_id: str,
    version: int,
    tenant_id: str,
    project_id: str | None,
    created_by: str | None,
) -> m.GovernancePolicyRow:
    return m.GovernancePolicyRow(
        id=policy_id,
        version=version,
        tenant_id=tenant_id,
        project_id=project_id,
        scope="tenant" if project_id is None else "project",
        name=body.name,
        description=body.description,
        applies_to=body.applies_to.model_dump(mode="json"),
        checks=[c.model_dump(mode="json", exclude_none=True) for c in body.checks],
        require_approval=body.require_approval,
        approvals_required=body.approvals_required,
        require_validated_for_promotion=body.require_validated_for_promotion,
        active=body.active,
        created_at=now(),
        created_by=created_by,
    )


async def seed_default_policies(session: AsyncSession, tenant_id: str) -> int:
    """Insert :data:`~mapi_spec.governance.DEFAULT_POLICIES` as tenant policies when the
    tenant has never had a policy (retired ones count, so they are never re-seeded).
    Returns how many were inserted."""
    have = (
        await session.execute(
            select(func.count())
            .select_from(m.GovernancePolicyRow)
            .where(m.GovernancePolicyRow.tenant_id == tenant_id)
        )
    ).scalar_one()
    if have:
        return 0
    for body in DEFAULT_POLICIES:
        session.add(
            _row(
                body,
                policy_id=new_id("pol"),
                version=1,
                tenant_id=tenant_id,
                project_id=None,
                created_by=SYSTEM_ACTOR,
            )
        )
    await session.flush()
    return len(DEFAULT_POLICIES)


async def latest_policy_rows(
    session: AsyncSession,
    tenant_id: str,
    *,
    project_id: str | None,
    include_tenant: bool = True,
    include_project: bool = True,
    include_retired: bool = False,
) -> list[m.GovernancePolicyRow]:
    """The latest version of every policy visible in ``project_id`` (tenant-wide ones
    first), oldest first within each scope."""
    latest = (
        select(
            m.GovernancePolicyRow.id.label("pid"),
            func.max(m.GovernancePolicyRow.version).label("v"),
        )
        .where(m.GovernancePolicyRow.tenant_id == tenant_id)
        .group_by(m.GovernancePolicyRow.id)
        .subquery()
    )
    stmt = select(m.GovernancePolicyRow).join(
        latest,
        (m.GovernancePolicyRow.id == latest.c.pid) & (m.GovernancePolicyRow.version == latest.c.v),
    )
    scopes: list[ColumnElement[bool]] = []
    if include_tenant:
        scopes.append(m.GovernancePolicyRow.project_id.is_(None))
    if include_project and project_id is not None:
        scopes.append(m.GovernancePolicyRow.project_id == project_id)
    if not scopes:
        return []

    stmt = stmt.where(m.GovernancePolicyRow.tenant_id == tenant_id, or_(*scopes))
    if not include_retired:
        stmt = stmt.where(m.GovernancePolicyRow.retired_at.is_(None))
    rows = list((await session.execute(stmt)).scalars())
    return sorted(rows, key=lambda r: (r.project_id is not None, r.created_at, r.id))


async def load_policy(
    session: AsyncSession,
    tenant_id: str,
    project_id: str | None,
    policy_id: str,
    *,
    version: int | None = None,
    for_update: bool = False,
) -> m.GovernancePolicyRow:
    """The latest (or ``version``) row of a policy in exactly this scope (404 otherwise)."""
    stmt = select(m.GovernancePolicyRow).where(
        m.GovernancePolicyRow.tenant_id == tenant_id,
        m.GovernancePolicyRow.id == policy_id,
    )
    stmt = stmt.where(
        m.GovernancePolicyRow.project_id.is_(None)
        if project_id is None
        else m.GovernancePolicyRow.project_id == project_id
    )
    if version is not None:
        stmt = stmt.where(m.GovernancePolicyRow.version == version)
    stmt = stmt.order_by(m.GovernancePolicyRow.version.desc()).limit(1)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("policy", policy_id)
    return row


def _check_policy(body: GovernancePolicyCreate) -> None:
    for i, c in enumerate(body.checks):
        if c.check not in GOVERNANCE_CHECKS:  # pragma: no cover - the DTO refuses it first
            raise MapiError(
                f"unknown governance check {c.check!r}",
                code=ErrorCode.POLICY_INVALID,
                pointer=f"/checks/{i}/check",
            )


async def create_policy(
    session: AsyncSession,
    ctx: ServerContext,
    body: GovernancePolicyCreate,
    *,
    tenant_id: str,
    project_id: str | None,
) -> m.GovernancePolicyRow:
    """A new policy (version 1), audited as ``policy.create``."""
    _check_policy(body)
    await seed_default_policies(session, tenant_id)
    actor = _Actor.of(ctx)
    row = _row(
        body,
        policy_id=new_id("pol"),
        version=1,
        tenant_id=tenant_id,
        project_id=project_id,
        created_by=actor.id,
    )
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.POLICY_CREATE,
                _policy_kind(project_id),
                tenant_id=tenant_id,
                project_id=project_id,
                resource_id=row.id,
                details={"version": 1, "checks": [c.check for c in body.checks]},
            )
        ],
    )
    return row


def _policy_kind(project_id: str | None) -> ResourceKind:
    return ResourceKind.TENANT_POLICY if project_id is None else ResourceKind.POLICY


async def update_policy(
    session: AsyncSession,
    ctx: ServerContext,
    policy_id: str,
    body: GovernancePolicyUpdate,
    *,
    tenant_id: str,
    project_id: str | None,
) -> m.GovernancePolicyRow:
    """Save the next version (``CONFLICT`` when ``expected_version`` is stale or the policy
    is retired); the previous versions stay for audit."""
    _check_policy(body)
    current = await load_policy(session, tenant_id, project_id, policy_id, for_update=True)
    if current.retired_at is not None:
        raise Conflict(f"policy {policy_id} is retired; create a new one")
    if body.expected_version is not None and body.expected_version != current.version:
        raise Conflict(
            f"policy {policy_id} is at version {current.version}, not {body.expected_version}"
        )
    actor = _Actor.of(ctx)
    row = _row(
        body,
        policy_id=policy_id,
        version=current.version + 1,
        tenant_id=tenant_id,
        project_id=project_id,
        created_by=actor.id,
    )
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.POLICY_UPDATE,
                _policy_kind(project_id),
                tenant_id=tenant_id,
                project_id=project_id,
                resource_id=policy_id,
                details={"version": row.version, "previous_version": current.version},
            )
        ],
    )
    return row


async def retire_policy(
    session: AsyncSession,
    ctx: ServerContext,
    policy_id: str,
    *,
    tenant_id: str,
    project_id: str | None,
) -> None:
    """Retire the policy (its versions stay; idempotent)."""
    row = await load_policy(session, tenant_id, project_id, policy_id, for_update=True)
    if row.retired_at is not None:
        return
    row.retired_at = now()
    row.active = False
    await add_audit(
        session,
        [
            _Actor.of(ctx).audit(
                AuditAction.POLICY_DELETE,
                _policy_kind(project_id),
                tenant_id=tenant_id,
                project_id=project_id,
                resource_id=policy_id,
                details={"version": row.version},
            )
        ],
    )


async def policy_versions(
    session: AsyncSession, tenant_id: str, project_id: str | None, policy_id: str
) -> list[m.GovernancePolicyRow]:
    await load_policy(session, tenant_id, project_id, policy_id)
    stmt = (
        select(m.GovernancePolicyRow)
        .where(
            m.GovernancePolicyRow.tenant_id == tenant_id,
            m.GovernancePolicyRow.id == policy_id,
        )
        .order_by(m.GovernancePolicyRow.version.desc())
    )
    return list((await session.execute(stmt)).scalars())


# ======================================================================= model context


async def model_family(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion
) -> tuple[str, str]:
    """``(family, plugin id)`` of a model version: the manifest's ``family`` from the
    catalog when known, else the plugin id's namespace."""
    ref = str((mv.lineage or {}).get("plugin") or "")
    plugin_id = ref.split("@", 1)[0]
    family = plugin_id.split(".", 1)[0] if plugin_id else "unknown"
    try:
        snapshot = await ctx.catalogs.get(session)
    except Exception:  # pragma: no cover - the catalog is optional here
        snapshot = None
    if snapshot is not None:
        for entry in snapshot.catalog.models:
            if entry.ref == ref or (entry.id == plugin_id and not ref.count("@")):
                return entry.manifest.model_family, plugin_id
    return family, plugin_id or "unknown"


async def _template_of(session: AsyncSession, mv: m.ModelVersion) -> str | None:
    run = await session.get(m.Run, mv.run_id)
    meta = (run.payload if run is not None else {}).get("metadata") or {}
    labels = meta.get("labels") if isinstance(meta, dict) else None
    guided = labels.get("guided") if isinstance(labels, dict) else None
    return guided if isinstance(guided, str) else None


async def policies_in_force(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion
) -> list[GovernancePolicy]:
    """The active policies covering ``mv`` (tenant-wide first), seeding the defaults on the
    tenant's first use of governance."""
    await seed_default_policies(session, mv.tenant_id)
    family, plugin_id = await model_family(session, ctx, mv)
    rows = await latest_policy_rows(session, mv.tenant_id, project_id=mv.project_id)
    return policies_for(
        (policy_dto(r) for r in rows),
        project_id=mv.project_id,
        family=family,
        model_id=plugin_id,
        template=await _template_of(session, mv),
    )


@dataclass(frozen=True, slots=True)
class PolicyRequirement:
    """What the policies in force demand of a model version at promotion.

    ``validation_status`` is the latest validation's status, except that a ``validated``
    validation which did not cover every gating policy in force (at its current version)
    reads :data:`STALE_VALIDATION` - the gate refuses it like any other non-validated
    status. ``uncovered`` lists those policies (``id@version``)."""

    require_validated: bool
    validation_status: str | None
    policies: list[str]
    uncovered: list[str] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return not self.require_validated or self.validation_status == "validated"


STALE_VALIDATION: Final = "validated under other or older policies"
"""What the promotion gate reports for a validation that missed a gating policy now in force."""


def _policy_refs(refs: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ref in refs:
        pid, _, ver = str(ref).partition("@")
        try:
            out[pid] = max(out.get(pid, 0), int(ver or 0))
        except ValueError:
            continue
    return out


def _check_covered(want: PolicyCheck, held: PolicyCheck | None) -> bool:
    """Whether a check held at ``held`` is at least as demanding as ``want``."""
    if held is None or held.severity.rank < want.severity.rank:
        return False
    if any(held.params.get(k) != v for k, v in want.params.items()):
        return False
    w, h = want.thresholds, held.thresholds
    if w is None:
        return True
    if h is None:
        return False
    if GOVERNANCE_CHECKS[want.check].direction is CheckDirection.MIN:
        return h.warn >= w.warn and h.fail >= w.fail
    return h.warn <= w.warn and h.fail <= w.fail


def validation_covers(v: m.ModelValidation, policy: GovernancePolicy) -> bool:
    """Whether validation ``v`` held its model to everything ``policy`` asks now: it
    names the policy at its current version (or later), or it was held to every check of
    the policy at least as strictly and had the approvals the policy wants."""
    refs = _policy_refs(v.policies or [])
    if refs.get(policy.id, 0) >= policy.version:
        return True
    held = {c.check: c for c in (PolicyCheck.model_validate(x) for x in v.required_checks or [])}
    if not all(_check_covered(c, held.get(c.check)) for c in policy.checks):
        return False
    if policy.require_approval:
        return bool(v.require_approval) and v.approvals_required >= policy.approvals_required
    return True


def uncovered_policies(v: m.ModelValidation, policies: Iterable[GovernancePolicy]) -> list[str]:
    """The gating policies (``require_validated_for_promotion``) ``v`` does not cover."""
    return [
        f"{p.id}@{p.version}"
        for p in policies
        if p.active and p.require_validated_for_promotion and not validation_covers(v, p)
    ]


async def _in_force_now(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion
) -> list[GovernancePolicy]:
    """:func:`policies_in_force` without seeding (read-only paths)."""
    family, plugin_id = await model_family(session, ctx, mv)
    rows = await latest_policy_rows(session, mv.tenant_id, project_id=mv.project_id)
    return policies_for(
        (policy_dto(r) for r in rows),
        project_id=mv.project_id,
        family=family,
        model_id=plugin_id,
        template=await _template_of(session, mv),
    )


async def promotion_requirement(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion
) -> PolicyRequirement:
    """Whether a policy requires ``validated`` to promote ``mv``, and its latest status.

    A ``validated`` latest validation passes only when it covered every gating policy in
    force now (:func:`validation_covers`): naming a lenient policy, or validating before
    a policy was added or made stricter, does not get past the gate.

    Read-only: it does not seed defaults (the defaults never require validation)."""
    pols = await _in_force_now(session, ctx, mv)
    latest = await _latest_validation(session, mv.id)
    status = latest.status if latest is not None else None
    uncovered: list[str] = []
    if latest is not None and status == ValidationStatus.VALIDATED.value:
        uncovered = uncovered_policies(latest, pols)
        if uncovered:
            status = STALE_VALIDATION
    return PolicyRequirement(
        require_validated=requires_validation(pols),
        validation_status=status,
        policies=[f"{p.id}@{p.version}" for p in pols if p.require_validated_for_promotion],
        uncovered=uncovered,
    )


# ========================================================================= validations


async def _latest_validation(session: AsyncSession, mv_id: str) -> m.ModelValidation | None:
    stmt = (
        select(m.ModelValidation)
        .where(m.ModelValidation.model_version_id == mv_id)
        .order_by(m.ModelValidation.created_at.desc(), m.ModelValidation.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def load_validation(
    session: AsyncSession, scope: Scope, validation_id: str, *, for_update: bool = False
) -> m.ModelValidation:
    stmt = select(m.ModelValidation).where(
        m.ModelValidation.id == validation_id,
        m.ModelValidation.tenant_id == scope.tenant_id,
        m.ModelValidation.project_id == scope.project_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("validation", validation_id)
    return row


async def _approvals(session: AsyncSession, v: m.ModelValidation) -> list[m.ValidationApproval]:
    stmt = (
        select(m.ValidationApproval)
        .where(
            m.ValidationApproval.validation_id == v.id,
            m.ValidationApproval.round == v.round,
        )
        .order_by(m.ValidationApproval.decided_at, m.ValidationApproval.id)
    )
    return list((await session.execute(stmt)).scalars())


async def validation_dto(session: AsyncSession, v: m.ModelValidation) -> ModelValidation:
    approvals = [
        Approval(
            id=a.id,
            validation_id=a.validation_id,
            approver_id=a.approver_id,
            decision=a.decision,
            comment=a.comment,
            decided_at=a.decided_at,
        )
        for a in await _approvals(session, v)
    ]
    return ModelValidation.model_validate(
        {
            "id": v.id,
            "tenant_id": v.tenant_id,
            "project_id": v.project_id,
            "model_id": v.model_id,
            "version": v.version,
            "status": v.status,
            "policies": v.policies or [],
            "required_checks": v.required_checks or [],
            "results": v.results or [],
            "failing_checks": v.failing_checks or [],
            "require_approval": v.require_approval,
            "approvals_required": v.approvals_required,
            "approvals": [a.model_dump(mode="json") for a in approvals],
            "history": v.history or [],
            "workflow_run_id": v.run_id,
            "submitted_by": v.submitted_by,
            "submitted_at": v.submitted_at,
            "updated_at": v.updated_at,
        }
    )


async def _apply(
    session: AsyncSession,
    v: m.ModelVersion | m.ModelValidation,
    event: ValidationEvent,
    actor: _Actor,
    *,
    note: str | None = None,
) -> ValidationStatus:
    """One state-machine step on a validation row: history, audit and webhook."""
    assert isinstance(v, m.ModelValidation)
    before = ValidationStatus(v.status)
    after = transition(before, event, require_approval=v.require_approval)
    ts = now()
    record = ValidationTransitionRecord(
        from_status=before,
        to_status=after,
        event=event,
        actor_id=actor.id,
        at=ts,
        note=note[:2000] if note else None,
    )
    v.status = after.value
    v.history = [*(v.history or []), record.model_dump(mode="json", exclude_none=True)]
    v.updated_at = ts
    await session.flush()
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.MODEL_VALIDATION_TRANSITION,
                ResourceKind.MODEL,
                tenant_id=v.tenant_id,
                project_id=v.project_id,
                resource_id=v.id,
                details={
                    "from": before.value,
                    "to": after.value,
                    "event": event.value,
                    "model_version_id": v.model_version_id,
                },
            )
        ],
    )
    hook = _EVENT_WEBHOOK.get(after)
    if hook is not None:
        await emit_event(
            session,
            tenant_id=v.tenant_id,
            project_id=v.project_id,
            event_type=hook,
            data={
                "validation_id": v.id,
                "model_id": v.model_id,
                "model_version_id": v.model_version_id,
                "version": v.version,
                "state": after.value,
                "failing_checks": list(v.failing_checks or []),
            },
        )
    return after


async def _resolve_policies(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion, wanted: Sequence[str]
) -> list[GovernancePolicy]:
    """Every policy in force for ``mv`` (the tenant floor and the project's own) plus the
    active policies ``wanted`` names. Naming policies only ever *adds* to what a model is
    held to: the policies in force cannot be left out (``POLICY_INVALID`` for a name that
    is not an active policy of the project or tenant, or when nothing applies at all)."""
    pols = await policies_in_force(session, ctx, mv)
    if wanted:
        rows = {
            r.id: r
            for r in await latest_policy_rows(session, mv.tenant_id, project_id=mv.project_id)
        }
        have = {p.id for p in pols}
        for i, pid in enumerate(wanted):
            row = rows.get(pid)
            if row is None or not row.active:
                raise MapiError(
                    f"policy {pid} is not an active policy of this project or tenant",
                    code=ErrorCode.POLICY_INVALID,
                    pointer=f"/policy_ids/{i}",
                )
            if pid not in have:
                pols.append(policy_dto(row))
                have.add(pid)
    if not pols:
        raise MapiError(
            "no governance policy applies to this model; add a policy for its family "
            "or name one in policy_ids",
            code=ErrorCode.POLICY_INVALID,
            pointer="/policy_ids",
        )
    return pols


def _approval_of(pols: Sequence[GovernancePolicy]) -> tuple[bool, int]:
    """``(require_approval, approvals_required)`` over every policy a validation is held to."""
    return (
        any(p.require_approval for p in pols),
        max((p.approvals_required for p in pols if p.require_approval), default=1),
    )


async def _lock_version(session: AsyncSession, mv: m.ModelVersion) -> None:
    """Serialise validation requests of one model version (one open validation each)."""
    await session.execute(
        select(m.ModelVersion.id).where(m.ModelVersion.id == mv.id).with_for_update()
    )


async def _blocks_new_validation(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion, latest: m.ModelValidation
) -> bool:
    """An open validation blocks a new one; so does a ``validated`` one unless it no longer
    covers a gating policy in force (a policy was added or made stricter since)."""
    status = ValidationStatus(latest.status)
    if status not in _OPEN:
        return False
    if status is not ValidationStatus.VALIDATED:
        return True
    return not uncovered_policies(latest, await policies_in_force(session, ctx, mv))


def _evaluation_present(mv: m.ModelVersion) -> bool:
    objects = (mv.manifest or {}).get("objects") or {}
    return EVALUATION_BUNDLE in objects and EVALUATION_MANIFEST in objects


async def _model_ref(session: AsyncSession, mv: m.ModelVersion) -> str:
    model = await session.get(m.Model, mv.model_id)
    name = model.name if model is not None else mv.model_id
    return f"model://{mv.project_id}/{name}@v{mv.version}"


async def _submit_run(
    session: AsyncSession,
    ctx: ServerContext,
    v: m.ModelValidation,
    mv: m.ModelVersion,
    checks: Sequence[PolicyCheck],
) -> m.Run:
    """Queue the sandboxed ``governance`` run of a validation."""
    from mapi_server.services.specs import resolve_dataset
    from mapi_server.services.submit import _accept, data_ref, fit_dataset_version

    dv: m.DatasetVersion | None = None
    if v.dataset:
        try:
            ref = DatasetRef.parse(v.dataset)
        except ValueError:
            raise MapiError(
                f"{v.dataset!r} is not a dataset reference",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/dataset",
            ) from None
        dv, why = await resolve_dataset(session, Scope(mv.tenant_id, mv.project_id), ref)
        if dv is None:
            raise MapiError(
                why or f"cannot resolve {v.dataset}",
                code=ErrorCode.REF_UNRESOLVED,
                pointer="/dataset",
            )
    else:
        dv = await fit_dataset_version(session, mv)
    job = GovernanceJob(
        validation_id=v.id,
        model_ref=await _model_ref(session, mv),
        checks=list(checks),
        budget=RefitBudget(),
        seed=int(hashlib.sha256(v.id.encode()).hexdigest()[:7], 16),
    )
    payload = job.model_dump(mode="json", exclude_none=True)
    bundle = await verified_bundle_object(ctx.store, ctx.signer, mv, FIT_BUNDLE)
    inputs: dict[str, Any] = {
        INPUT_DATASET: data_ref(dv),
        INPUT_BUNDLE: StoredRef(bundle.bucket, bundle.key, bundle.generation),
    }
    objects = (mv.manifest or {}).get("objects") or {}
    if BUNDLE_MANIFEST_OUTPUT in objects:
        sdk = await verified_bundle_object(ctx.store, ctx.signer, mv, BUNDLE_MANIFEST_OUTPUT)
        inputs[INPUT_BUNDLE_MANIFEST] = StoredRef(sdk.bucket, sdk.key, sdk.generation)
    if _evaluation_present(mv):
        ev = await verified_bundle_object(ctx.store, ctx.signer, mv, EVALUATION_BUNDLE)
        evm = await verified_bundle_object(ctx.store, ctx.signer, mv, EVALUATION_MANIFEST)
        inputs[INPUT_EVALUATION] = StoredRef(ev.bucket, ev.key, ev.generation)
        inputs[INPUT_EVALUATION_MANIFEST] = StoredRef(evm.bucket, evm.key, evm.generation)
    run = await create_run(
        session,
        Scope(mv.tenant_id, mv.project_id),
        kind=RunKind.GOVERNANCE,
        resource_class=ResourceClass.S,
        timeout_s=GOVERNANCE_RUN_TIMEOUT_S,
        payload=payload,
        inputs=inputs,
        labels={"validation": v.id},
        spec_hash=spec_hash(payload),
        model_version_id=mv.id,
        dataset_id=dv.dataset_id,
        dataset_version=dv.version,
        catalog_digest=(mv.manifest or {}).get("catalog_digest"),
    )
    return await _accept(session, ctx, run, untrusted=bool((mv.lineage or {}).get("overlay_hash")))


async def request_validation(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion, body: ValidationRequest
) -> m.ModelValidation:
    """Create a validation of ``mv`` and submit it: the policies' checks run as a
    ``governance`` run (``VALIDATION_TRANSITION_INVALID`` when the version already has an
    open or validated validation; ``POLICY_INVALID`` without an applicable policy).

    The validation is held to every policy in force plus the ones ``policy_ids`` names
    (:func:`_resolve_policies`); concurrent requests for one version are serialised on
    the version row, so only one of them creates a validation."""
    if mv.status != "ready":
        raise Conflict(f"model version {mv.id} is {mv.status}; only ready versions are validated")
    await _lock_version(session, mv)
    latest = await _latest_validation(session, mv.id)
    if latest is not None and await _blocks_new_validation(session, ctx, mv, latest):
        raise MapiError(
            f"model version v{mv.version} already has a validation that is {latest.status} "
            f"({latest.id}); resubmit or deprecate that one",
            code=ErrorCode.VALIDATION_TRANSITION_INVALID,
        )
    pols = await _resolve_policies(session, ctx, mv, body.policy_ids)
    required = effective_checks(pols)
    require_approval, approvals_required = _approval_of(pols)
    actor = _Actor.of(ctx)
    ts = now()
    v = m.ModelValidation(
        id=new_id("val"),
        tenant_id=mv.tenant_id,
        project_id=mv.project_id,
        model_id=mv.model_id,
        model_version_id=mv.id,
        version=mv.version,
        status=ValidationStatus.DRAFT.value,
        round=1,
        policies=[f"{p.id}@{p.version}" for p in pols],
        required_checks=[c.model_dump(mode="json", exclude_none=True) for c in required],
        results=[],
        failing_checks=[],
        require_approval=require_approval,
        approvals_required=approvals_required,
        history=[],
        dataset=body.dataset,
        note=body.note,
        submitted_by=actor.id,
        submitted_at=ts,
        created_at=ts,
        updated_at=ts,
    )
    session.add(v)
    await session.flush()
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.MODEL_VALIDATION_REQUEST,
                ResourceKind.MODEL,
                tenant_id=v.tenant_id,
                project_id=v.project_id,
                resource_id=v.id,
                details={
                    "model_version_id": mv.id,
                    "policies": list(v.policies),
                    "checks": [c.check for c in required],
                },
            )
        ],
    )
    run = await _submit_run(session, ctx, v, mv, required)
    v.run_id = run.id
    await _apply(session, v, ValidationEvent.SUBMIT, actor, note=body.note)
    return v


async def resubmit_validation(
    session: AsyncSession, ctx: ServerContext, v: m.ModelValidation, note: str | None
) -> m.ModelValidation:
    """Run the checks again after a failure or rejection: a new round with the policies in
    force now and a new governance run."""
    transition(v.status, ValidationEvent.RESUBMIT, require_approval=v.require_approval)
    mv = await session.get(m.ModelVersion, v.model_version_id)
    if mv is None:  # pragma: no cover - cascades with the version
        raise NotFoundError("model version", v.model_version_id)
    pols = await _resolve_policies(session, ctx, mv, [])
    required = effective_checks(pols)
    v.policies = [f"{p.id}@{p.version}" for p in pols]
    v.required_checks = [c.model_dump(mode="json", exclude_none=True) for c in required]
    v.require_approval, v.approvals_required = _approval_of(pols)
    v.round += 1
    v.results = []
    v.failing_checks = []
    actor = _Actor.of(ctx)
    v.submitted_by = actor.id
    v.submitted_at = now()
    run = await _submit_run(session, ctx, v, mv, required)
    v.run_id = run.id
    await _apply(session, v, ValidationEvent.RESUBMIT, actor, note=note)
    return v


async def cancel_validation(
    session: AsyncSession, ctx: ServerContext, v: m.ModelValidation, note: str | None
) -> m.ModelValidation:
    """Stop the checks (back to ``draft``) and cancel the governance run."""
    from mapi_spec.run import Actor, RunTrigger

    from mapi_server.services.runs import apply_transition, load_run

    transition(v.status, ValidationEvent.CANCEL)
    if v.run_id is not None:
        run = await load_run(session, None, v.run_id, for_update=True)
        state = RunState(run.state)
        if not state.terminal and state is not RunState.FINALIZING:
            await apply_transition(session, run, RunTrigger.CANCEL, Actor.API)
    if v.status == ValidationStatus.CHECKS_RUNNING.value:
        await _apply(session, v, ValidationEvent.CANCEL, _Actor.of(ctx), note=note)
    return v


async def deprecate_validation(
    session: AsyncSession, ctx: ServerContext, v: m.ModelValidation, note: str | None
) -> m.ModelValidation:
    """Retire a validated version (a reason is required; audited as ``model.deprecate``)."""
    if not (note and note.strip()):
        raise MapiError(
            "a deprecation needs a reason (note)", code=ErrorCode.SCHEMA_INVALID, pointer="/note"
        )
    actor = _Actor.of(ctx)
    await _apply(session, v, ValidationEvent.DEPRECATE, actor, note=note)
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.MODEL_DEPRECATE,
                ResourceKind.MODEL,
                tenant_id=v.tenant_id,
                project_id=v.project_id,
                resource_id=v.id,
                details={"model_version_id": v.model_version_id, "reason": note[:500]},
            )
        ],
    )
    return v


async def decide(
    session: AsyncSession, ctx: ServerContext, v: m.ModelValidation, body: ApprovalCreate
) -> m.ModelValidation:
    """A reviewer's decision: never the submitter, once per round; ``approvals_required``
    approvals validate, a rejection rejects."""
    auth = ctx.request
    if auth is None or auth.context is None:  # pragma: no cover - routes always have a caller
        raise MapiError("approvals need a signed-in reviewer", code=ErrorCode.FORBIDDEN)
    actor = _Actor.of(ctx)
    if auth.token is not None:
        raise MapiError("API tokens cannot approve model validations", code=ErrorCode.FORBIDDEN)
    status = ValidationStatus(v.status)
    if status is not ValidationStatus.AWAITING_APPROVAL:
        raise MapiError(
            f"a validation in status {status.value!r} cannot be approved or rejected",
            code=ErrorCode.VALIDATION_TRANSITION_INVALID,
        )
    if body.decision == "approve" and v.submitted_by in (None, "", SYSTEM_ACTOR):
        # a second-person approval needs a first person: reject it and resubmit instead
        raise MapiError(
            "no person submitted this validation, so it cannot get a second person's "
            "approval; reject it and resubmit",
            code=ErrorCode.FORBIDDEN,
        )
    previous = await _approvals(session, v)
    check_approver(
        submitter_id=v.submitted_by or "",
        approver_id=actor.id or "",
        is_reviewer=auth.context.allows(Action.APPROVE, ResourceKind.MODEL),
        already_approved=[a.approver_id for a in previous],
    )
    ts = now()
    session.add(
        m.ValidationApproval(
            id=new_id("apv"),
            tenant_id=v.tenant_id,
            project_id=v.project_id,
            validation_id=v.id,
            round=v.round,
            approver_id=actor.id or "",
            decision=body.decision,
            comment=body.comment,
            decided_at=ts,
        )
    )
    await session.flush()
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.MODEL_APPROVE
                if body.decision == "approve"
                else AuditAction.MODEL_REJECT,
                ResourceKind.MODEL,
                tenant_id=v.tenant_id,
                project_id=v.project_id,
                resource_id=v.id,
                details={"model_version_id": v.model_version_id, "round": v.round},
            )
        ],
    )
    if body.decision == "reject":
        await _apply(session, v, ValidationEvent.REJECT, actor, note=body.comment)
        return v
    approved = 1 + sum(1 for a in previous if a.decision == "approve")
    if approved >= v.approvals_required:
        await _apply(session, v, ValidationEvent.APPROVE, actor, note=body.comment)
    return v


# ===================================================================== governance runs


def parse_report(raw: bytes) -> GovernanceReport:
    """Validate ``governance.json`` bytes (``INTEGRITY_FAILURE`` if it is not a report)."""
    try:
        return GovernanceReport.model_validate(loads_limited(raw))
    except (JsonTooDeep, ValueError, ValidationError) as exc:
        raise MapiError(
            f"{GOVERNANCE_OUTPUT} is not a valid governance report",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=str(exc)[:8000],
        ) from None


async def record_report(session: AsyncSession, run: m.Run, report: GovernanceReport) -> None:
    """Store a finished governance run's results on its validation (the finalizer, in the
    transaction that marks the run succeeded; the state advances on the terminal
    transition, :func:`governance_run_finished`)."""
    v = await _validation_of_run(session, run)
    if v is None or v.status != ValidationStatus.CHECKS_RUNNING.value:
        return
    ts = now()
    results = [
        r.model_copy(update={"run_id": run.id, "computed_at": r.computed_at or ts})
        for r in report.results
    ]
    required = [PolicyCheck.model_validate(c) for c in v.required_checks or []]
    known = {c.check for c in required}
    v.results = [r.model_dump(mode="json", exclude_none=True) for r in results if r.check in known]
    _passed, failing = rollup_results(results, required)
    v.failing_checks = failing
    await session.flush()


async def _validation_of_run(session: AsyncSession, run: m.Run) -> m.ModelValidation | None:
    stmt = (
        select(m.ModelValidation)
        .where(m.ModelValidation.run_id == run.id, m.ModelValidation.tenant_id == run.tenant_id)
        .with_for_update()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def governance_run_finished(session: AsyncSession, run: m.Run, state: RunState) -> None:
    """Advance the validation of a governance run that just became terminal (called by
    :func:`mapi_server.services.runs.apply_transition` in the same transaction)."""
    v = await _validation_of_run(session, run)
    if v is None or v.status != ValidationStatus.CHECKS_RUNNING.value:
        return
    actor = _Actor(None, SYSTEM_ACTOR)
    if state is RunState.CANCELLED:
        await _apply(
            session, v, ValidationEvent.CANCEL, actor, note="the governance run was cancelled"
        )
        return
    required = [PolicyCheck.model_validate(c) for c in v.required_checks or []]
    results = [CheckResult.model_validate(r) for r in v.results or []]
    if state is not RunState.SUCCEEDED or not results:
        problem = run.problem if isinstance(run.problem, dict) else {}
        why = str(problem.get("detail") or problem.get("title") or "the governance run failed")
        ts = now()
        results = [
            CheckResult(
                check=c.check,
                status=CheckStatus.ERROR,
                severity=c.severity,
                metric=GOVERNANCE_CHECKS[c.check].metric,
                message_plain=f"The check could not run: {why}"[:2000],
                suggested_action="Look at the governance run's logs, fix the problem and resubmit.",
                run_id=run.id,
                computed_at=ts,
            )
            for c in required
        ]
        v.results = [r.model_dump(mode="json", exclude_none=True) for r in results]
    passed, failing = rollup_results(results, required)
    v.failing_checks = failing
    for event in after_checks(passed, require_approval=v.require_approval):
        await _apply(session, v, event, actor)


# ============================================================================ workflows


async def workflow_policy_resolver(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion, policy_id: str | None
) -> list[GovernancePolicy]:
    """The policies a workflow's ``governance.policy`` node holds ``mv`` to: the policies in
    force for its family, plus the named one (empty when it is not an active policy of the
    project or tenant: the node then fails with ``POLICY_INVALID``). Naming a policy never
    drops the tenant floor or the project's policies."""
    if policy_id is None:
        return await policies_in_force(session, ctx, mv)
    try:
        return await _resolve_policies(session, ctx, mv, [policy_id])
    except MapiError:
        return []


async def workflow_validation_submitter(
    session: AsyncSession,
    ctx: ServerContext,
    mv: m.ModelVersion,
    evidence: dict[str, object],
    workflow_run_id: str,
) -> str | None:
    """Record a workflow's governance evidence as a validation of ``mv`` (``submit: true``
    on a ``governance.policy`` node whose checks passed): the checks already ran in the
    workflow's sandboxed node, so the validation goes straight from ``checks_running`` to
    ``validated``, or to ``awaiting_approval`` when a policy wants a reviewer. ``None``
    when the version already has an open or validated validation.

    The validation is held to every policy in force (the policies the evidence names only
    add to them), so evidence without a result for a required blocking check fails. It is
    submitted by the person who started the workflow run (for a schedule or a trigger,
    their creator), so that person cannot approve it; :data:`SYSTEM_ACTOR` only when the
    run has no creator, and such a validation cannot be approved at all."""
    await _lock_version(session, mv)
    latest = await _latest_validation(session, mv.id)
    if latest is not None and await _blocks_new_validation(session, ctx, mv, latest):
        return None
    raw_results = evidence.get("results")
    results = [
        CheckResult.model_validate(r)
        for r in (raw_results if isinstance(raw_results, list) else [])
        if isinstance(r, dict)
    ]
    # every policy in force, whatever the node named (the evidence's policies can only add
    # active ones); a required check the evidence has no result for fails the rollup
    named = evidence.get("policies")
    extra = [str(x).split("@", 1)[0] for x in (named if isinstance(named, list) else [])[:8] if x]
    pols = await policies_in_force(session, ctx, mv)
    active = {
        r.id: r
        for r in await latest_policy_rows(session, mv.tenant_id, project_id=mv.project_id)
        if r.active
    }
    for pid in extra:
        if pid in active and pid not in {p.id for p in pols}:
            pols.append(policy_dto(active[pid]))
    required = effective_checks(pols)
    require_approval, approvals_required = _approval_of(pols)
    wr = await session.get(m.WorkflowRunRow, workflow_run_id)
    submitter = (wr.created_by if wr is not None else None) or SYSTEM_ACTOR
    actor = _Actor(None, submitter)
    ts = now()
    passed, failing = rollup_results(results, required)
    v = m.ModelValidation(
        id=new_id("val"),
        tenant_id=mv.tenant_id,
        project_id=mv.project_id,
        model_id=mv.model_id,
        model_version_id=mv.id,
        version=mv.version,
        status=ValidationStatus.DRAFT.value,
        round=1,
        policies=[f"{p.id}@{p.version}" for p in pols],
        required_checks=[c.model_dump(mode="json", exclude_none=True) for c in required],
        results=[r.model_dump(mode="json", exclude_none=True) for r in results],
        failing_checks=failing,
        require_approval=require_approval,
        approvals_required=approvals_required,
        history=[],
        run_id=workflow_run_id,
        note="Checks run by a workflow's governance node.",
        submitted_by=submitter,
        submitted_at=ts,
        created_at=ts,
        updated_at=ts,
    )
    session.add(v)
    await session.flush()
    await add_audit(
        session,
        [
            actor.audit(
                AuditAction.MODEL_VALIDATION_REQUEST,
                ResourceKind.MODEL,
                tenant_id=v.tenant_id,
                project_id=v.project_id,
                resource_id=v.id,
                details={
                    "model_version_id": mv.id,
                    "policies": list(v.policies),
                    "checks": [c.check for c in required],
                    "workflow_run_id": workflow_run_id,
                    "submitted_by": submitter,
                },
            )
        ],
    )
    await _apply(session, v, ValidationEvent.SUBMIT, actor, note="submitted by a workflow")
    platform = _Actor(None, SYSTEM_ACTOR)
    for event in after_checks(passed, require_approval=v.require_approval):
        await _apply(session, v, event, platform)
    return v.id


# ======================================================================== evidence pack


def _digest(pack: EvidencePack) -> str:
    body = pack.model_dump(
        mode="json", exclude={"digest", "download", "generated_at", "generated_by"}
    )
    return "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()


async def evidence_pack(
    session: AsyncSession, ctx: ServerContext, v: m.ModelValidation
) -> EvidencePack:
    """The audit export of a validation (JSON content; :func:`evidence_pack_html` renders
    it). ``digest`` is the sha256 of the canonical JSON content."""
    mv = await session.get(m.ModelVersion, v.model_version_id)
    if mv is None:  # pragma: no cover - cascades with the version
        raise NotFoundError("model version", v.model_version_id)
    pols: list[GovernancePolicy] = []
    for ref in v.policies or []:
        pid, _, ver = str(ref).partition("@")
        rows = (
            await session.execute(
                select(m.GovernancePolicyRow).where(
                    m.GovernancePolicyRow.tenant_id == v.tenant_id,
                    m.GovernancePolicyRow.id == pid,
                    m.GovernancePolicyRow.version == int(ver or 0),
                )
            )
        ).scalars()
        pols.extend(policy_dto(r) for r in rows)
    actor = _Actor.of(ctx)
    pack = EvidencePack(
        format=EVIDENCE_PACK_FORMAT,
        validation=await validation_dto(session, v),
        model_ref=await _model_ref(session, mv),
        lineage=dict(mv.lineage or {}),
        policies=pols,
        scorecard=dict(mv.scorecard) if mv.scorecard else None,
        generated_at=now(),
        generated_by=actor.id,
    )
    return pack.model_copy(update={"digest": _digest(pack)})


_CSS: Final = (
    "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;color:#1d1d1f;"
    "max-width:72rem}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:"
    "1px solid #ddd;padding-bottom:.25rem}table{border-collapse:collapse;width:100%;"
    "margin:.5rem 0}th,td{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;"
    "vertical-align:top;font-size:.9rem}th{background:#f5f5f7}pre{background:#f5f5f7;"
    "padding:.5rem;overflow:auto;font-size:.8rem;white-space:pre-wrap}.pass{color:#1b7f3b}"
    ".warn{color:#a35c00}.fail,.error{color:#b00020}.skipped{color:#666}.meta{color:#555}"
)


def _check_title(check: str) -> str:
    d = GOVERNANCE_CHECKS.get(check)
    return d.title if d is not None else check


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _table(head: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def evidence_pack_html(pack: EvidencePack) -> str:
    """A static, self-contained HTML rendering of an evidence pack: inline CSS, no scripts,
    no external assets (a CSP meta tag forbids any), every value escaped."""
    v = pack.validation
    parts: list[str] = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; img-src data:\">",
        f"<title>Evidence pack - {_e(pack.model_ref)}</title><style>{_CSS}</style></head><body>",
        f'<h1>Model governance evidence pack</h1><p class="meta">{_e(pack.model_ref)} - '
        f'validation {_e(v.id)} - status <strong class="{_e(v.status.value)}">'
        f"{_e(v.status.value.replace('_', ' '))}</strong></p>",
        f'<p class="meta">Generated {_e(pack.generated_at.isoformat())} by '
        f"{_e(pack.generated_by or 'system')}. Content digest {_e(pack.digest)} "
        f"(format {_e(pack.format)}).</p>",
        "<h2>Checks</h2>",
        _table(
            ["Check", "Result", "Severity", "Value", "Threshold", "What it means", "What to do"],
            [
                [
                    f"<strong>{_e(_check_title(r.check))}</strong><br><code>{_e(r.check)}</code>",
                    f'<span class="{_e(r.status.value)}">{_e(r.status.value)}</span>',
                    _e(r.severity.value),
                    _e("" if r.value is None else f"{r.value:.4g}"),
                    _e("" if r.threshold is None else f"{r.threshold:.4g}"),
                    _e(r.message_plain),
                    _e(r.suggested_action or ""),
                ]
                for r in v.results
            ],
        ),
    ]
    if v.failing_checks:
        parts.append(
            f"<p><strong>Blocking checks not passed:</strong> {_e(', '.join(v.failing_checks))}</p>"
        )
    parts.append("<h2>Evidence</h2>")
    for r in v.results:
        if not r.evidence and not r.message_expert:
            continue
        parts.append(f"<h3>{_e(r.check)}</h3>")
        if r.message_expert:
            parts.append(f'<p class="meta">{_e(r.message_expert)}</p>')
        for item in r.evidence:
            caption = f" - {_e(item.caption_plain)}" if item.caption_plain else ""
            if item.kind == "metric":
                parts.append(f"<p><code>{_e(item.name)}</code> = {_e(item.value)}{caption}</p>")
            else:
                body = json.dumps(item.inline, indent=2, sort_keys=True, default=str)[:20_000]
                parts.append(f"<p><code>{_e(item.name)}</code>{caption}</p><pre>{_e(body)}</pre>")
    parts += [
        "<h2>Approvals</h2>",
        _table(
            ["Reviewer", "Decision", "Comment", "When"],
            [
                [
                    _e(a.approver_id),
                    _e(a.decision),
                    _e(a.comment or ""),
                    _e(a.decided_at.isoformat()),
                ]
                for a in v.approvals
            ],
        )
        if v.approvals
        else "<p>No approvals recorded.</p>",
        "<h2>History</h2>",
        _table(
            ["When", "From", "To", "Event", "By", "Note"],
            [
                [
                    _e(h.at.isoformat()),
                    _e(h.from_status.value),
                    _e(h.to_status.value),
                    _e(h.event.value),
                    _e(h.actor_id or ""),
                    _e(h.note or ""),
                ]
                for h in v.history
            ],
        ),
        "<h2>Policies</h2>",
        _table(
            ["Policy", "Version", "Scope", "Checks", "Approval", "Gates promotion"],
            [
                [
                    _e(p.name),
                    _e(p.version),
                    _e(p.scope),
                    _e(", ".join(f"{c.check} ({c.severity.value})" for c in p.checks)),
                    _e(f"{p.approvals_required} reviewer(s)" if p.require_approval else "no"),
                    _e("yes" if p.require_validated_for_promotion else "no"),
                ]
                for p in pack.policies
            ],
        ),
        "<h2>Lineage</h2>",
        f"<pre>{_e(json.dumps(pack.lineage, indent=2, sort_keys=True, default=str))}</pre>",
    ]
    if pack.scorecard:
        parts += [
            "<h2>Sanity scorecard</h2>",
            f"<pre>{_e(json.dumps(pack.scorecard, indent=2, sort_keys=True, default=str))}</pre>",
        ]
    parts.append("</body></html>")
    return "".join(parts)


async def store_evidence_html(
    ctx: ServerContext, v: m.ModelValidation, pack: EvidencePack
) -> ArtifactLink:
    """Write the HTML rendering (content-addressed by the pack's digest) to the data bucket
    and return a signed link to it."""
    body = evidence_pack_html(pack).encode("utf-8")
    digest = (pack.digest or "sha256:").split(":", 1)[-1][:32]
    key = f"t/{v.tenant_id}/evidence/{v.id}/{digest}.html"
    bucket = ctx.buckets.data
    meta = await ctx.store.put(bucket, key, body, content_type="text/html; charset=utf-8")
    return await artifact_link(
        ctx.store, name=f"evidence-{v.id}.html", bucket=bucket, key=key, generation=meta.generation
    )


async def latest_status_by_version(
    session: AsyncSession, mv_ids: Sequence[str]
) -> dict[str, tuple[str, datetime]]:
    """``{model_version_id: (status, updated_at)}`` of each version's latest validation."""
    if not mv_ids:
        return {}
    rows = (
        await session.execute(
            select(m.ModelValidation)
            .where(m.ModelValidation.model_version_id.in_(list(mv_ids)))
            .order_by(m.ModelValidation.created_at)
        )
    ).scalars()
    out: dict[str, tuple[str, datetime]] = {}
    for r in rows:
        out[r.model_version_id] = (r.status, r.updated_at)
    return out
