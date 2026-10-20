"""Endpoints and aliases (plan §2, §8.2 "Endpoints", §11; Q13; M3).

An endpoint ``endpoint://<project>/<alias>`` serves one model version at a time.

* **Promotion** (:func:`promote`) evaluates :func:`mapi_spec.scorecard.promotion_gate` for
  the version: its sanity scorecard, the endpoint's :class:`EvaluationPolicy` (Q13: by
  default only ``evaluate_then_refit`` fits - evaluated on the holdout, then refitted on all
  data inside the same fit run, so context tails and terminal state come from the full data)
  and whether its input contract breaks what callers send (:func:`schema_breaks`). ``warn``
  needs a signed-in project admin's override with a reason (audited
  ``endpoint.promote_override``); ``fail`` blocks.
* Every promotion and rollback is a revision (the history); a rollback re-points the alias
  at an earlier revision's version without re-running the gate (it passed, or was overridden,
  before) and is audited with its reason.
* **Scoring** turns an :class:`EndpointScoreCall` into a score run of the current version.
  The call is checked against the endpoint's frozen input contract first (inline XrJSON
  envelope: variables, dims, coordinates), then by the regular CoordPlan checks; inline
  payloads are stored as run inputs (``inline.data``/``inline.scenario``) and decoded only
  in the sandbox.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime
from typing import Any, Final

from mapi_spec._types import RESERVED_STAGE
from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.capabilities import Capabilities
from mapi_spec.coords import CoordSummary, DataSummary, parse_freq, parse_time
from mapi_spec.endpoints import (
    Endpoint,
    EndpointPromote,
    EndpointRevision,
    EndpointRollback,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    EvaluationPolicy,
    capability_breaks,
    endpoint_ref,
    policy_relaxations,
    schema_breaks,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.loader import canonical_json
from mapi_spec.refs import ModelVersionRef
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role
from mapi_spec.scorecard import (
    GateOutcome,
    PromotionGateDecision,
    authorize_promotion,
)
from mapi_spec.spec import ScoreRequest
from mapi_spec.storage import check_segment
from mapi_spec.tenancy import Action, ResourceKind
from mapi_spec.validate import ScoreMetadata
from mapi_spec.webhooks import WebhookEventType
from mapi_spec.xrjson import XrJsonDataset, XrJsonVariable, check_xrjson_size, observed_labels
from pydantic import JsonValue
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.audit import write_audit
from mapi_server.services.catalog import CatalogService
from mapi_server.services.runs import Scope
from mapi_server.services.runspec import StoredRef
from mapi_server.services.scorecards import promotion_decision
from mapi_server.services.specs import live_capabilities
from mapi_server.services.webhooks.outbox import emit_event

__all__ = [
    "INLINE_PREFIX",
    "endpoint_dto",
    "endpoint_schema_issues",
    "evaluate_gate",
    "inline_summary",
    "live_snapshot",
    "load_endpoint",
    "model_ref_of",
    "promote",
    "revision_dto",
    "rollback",
    "snapshot_for",
    "store_inline_inputs",
]

INLINE_PREFIX = "inline"
"""``t/{tenant}/inline/{sha256}.json`` in the quarantine bucket (7-day lifecycle): inline
score payloads are user data the sandbox decodes, like uploads awaiting ingest."""


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------ DTOs


async def model_ref_of(session: AsyncSession, mv: m.ModelVersion) -> str:
    model = await session.get(m.Model, mv.model_id)
    name = model.name if model is not None else mv.model_id
    return f"model://{mv.project_id}/{name}@v{mv.version}"


async def endpoint_dto(session: AsyncSession, row: m.Endpoint) -> Endpoint:
    mv = await session.get(m.ModelVersion, row.model_version_id)
    ref = await model_ref_of(session, mv) if mv is not None else ""
    return Endpoint(
        id=row.id,
        alias=row.alias,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        ref=endpoint_ref(row.project_id, row.alias),
        description=row.description,
        model_id=row.model_id,
        model_version_id=row.model_version_id,
        model_ref=ref,
        revision=row.revision,
        evaluation_policy=EvaluationPolicy.model_validate(row.evaluation_policy),
        schema_snapshot=EndpointSchemaSnapshot.model_validate(row.schema_snapshot),
        gate=PromotionGateDecision.model_validate(row.gate),
        overridden=row.overridden,
        promoted_by=row.promoted_by,
        promoted_at=row.promoted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def revision_dto(row: m.EndpointRevision) -> EndpointRevision:
    return EndpointRevision(
        alias=row.alias,
        revision=row.revision,
        action=row.action,
        model_version_id=row.model_version_id,
        model_ref=row.model_ref,
        previous_model_version_id=row.previous_model_version_id,
        gate=PromotionGateDecision.model_validate(row.gate),
        overridden=row.overridden,
        reason=row.reason,
        actor_id=row.actor_id,
        token_id=row.token_id,
        created_at=row.created_at,
    )


async def load_endpoint(
    session: AsyncSession, scope: Scope, alias: str, *, for_update: bool = False
) -> m.Endpoint:
    stmt = select(m.Endpoint).where(
        m.Endpoint.tenant_id == scope.tenant_id,
        m.Endpoint.project_id == scope.project_id,
        m.Endpoint.alias == alias,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("endpoint", alias)
    return row


def _checks(decision: PromotionGateDecision) -> list[JsonValue]:
    return [str(c) for c in decision.failing_checks[:20]]


# -------------------------------------------------------------------------------- snapshot


def _schema_hash(schema: DataSchema) -> str:
    text_ = canonical_json(schema.model_dump(mode="json", exclude_none=True))
    return "sha256:" + hashlib.sha256(text_.encode()).hexdigest()


def snapshot_for(mv: m.ModelVersion) -> EndpointSchemaSnapshot:
    """The input contract of ``mv`` (raw stage), from its stored score metadata."""
    if mv.score_meta is None:
        raise MapiError(
            f"model version {mv.id} has no stored score metadata. Thus it cannot serve an "
            "endpoint (fit it again on this platform version)",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    meta = ScoreMetadata.model_validate(mv.score_meta)
    rs = meta.resolved
    raw = rs.stages.get(RESERVED_STAGE)
    if raw is None:
        raise MapiError(
            f"model version {mv.id} has no raw input schema", code=ErrorCode.CAPABILITY_UNSUPPORTED
        )
    caps: Capabilities = rs.model.capabilities
    model_schema = rs.stages.get(rs.model_stage)
    horizon = None
    if model_schema is not None:
        extend = sorted(n for n, d in model_schema.dims.items() if d.policy is DimPolicy.EXTEND)
        horizon = extend[0] if extend else None
    return EndpointSchemaSnapshot(
        inputs_stage=RESERVED_STAGE,
        input_schema=raw,
        schema_hash=_schema_hash(raw),
        score_modes=sorted(caps.score_modes),
        representation=caps.representation,
        horizon_dim=horizon,
        future_required=sorted(n for n, v in raw.vars.items() if v.future_required),
        fit_coords=dict(meta.fit_coords.get(RESERVED_STAGE, {})),
        conditions_on_actuals=_conditions(caps, meta),
    )


def _conditions(caps: Capabilities, meta: ScoreMetadata) -> bool:
    """Whether the version conditions forecasts on actuals: its plugin build can, and it was
    not fitted in a transformed space (``ScoreMetadata.data_space``)."""
    return caps.conditions_on_actuals and meta.data_space is None


async def live_snapshot(
    session: AsyncSession, catalogs: CatalogService | None, row: m.Endpoint
) -> EndpointSchemaSnapshot:
    """The endpoint's frozen input contract with ``conditions_on_actuals`` as the plugin
    build that scores the current version has it now (a plugin may learn to condition on
    actuals after the fit; stored snapshots predate the field)."""
    snapshot = EndpointSchemaSnapshot.model_validate(row.schema_snapshot)
    mv = await session.get(m.ModelVersion, row.model_version_id)
    if mv is None or mv.score_meta is None:
        return snapshot
    meta = ScoreMetadata.model_validate(mv.score_meta)
    live = await live_capabilities(session, catalogs, meta)
    caps = live if live is not None else meta.resolved.model.capabilities
    conditions = _conditions(caps, meta)
    if conditions == snapshot.conditions_on_actuals:
        return snapshot
    return snapshot.model_copy(update={"conditions_on_actuals": conditions})


async def live_capabilities_snapshot(
    session: AsyncSession, catalogs: CatalogService | None, mv: m.ModelVersion
) -> EndpointSchemaSnapshot:
    """The input contract of ``mv`` (:func:`snapshot_for`) with ``conditions_on_actuals`` as
    the plugin build that scores it now has it: what a score call of the version can send
    (``GET /v1/models/{id}/versions/{v}/inputs``, inline ``ScoreCall`` data)."""
    snapshot = snapshot_for(mv)
    meta = ScoreMetadata.model_validate(mv.score_meta)
    live = await live_capabilities(session, catalogs, meta)
    if live is None:
        return snapshot
    return snapshot.model_copy(update={"conditions_on_actuals": _conditions(live, meta)})


async def evaluate_gate(
    session: AsyncSession,
    mv: m.ModelVersion,
    policy: EvaluationPolicy,
    *,
    current: EndpointSchemaSnapshot | None,
    ctx: ServerContext | None = None,
) -> tuple[PromotionGateDecision, EndpointSchemaSnapshot]:
    """The promotion gate for ``mv`` under ``policy`` (and the snapshot it would freeze).

    The decision comes from :func:`~mapi_server.services.scorecards.promotion_decision`: the
    scorecard the runner measured at fit (stored by the finalizer), the fit mode of the
    version's fit run (Q13) and its status, plus the schema breaks against ``current`` -
    including a forecast feature the callers may rely on (actuals) that ``mv``'s plugin build
    lacks (:func:`~mapi_spec.endpoints.capability_breaks`)."""
    snapshot = snapshot_for(mv)
    if ctx is not None and mv.score_meta is not None:
        meta = ScoreMetadata.model_validate(mv.score_meta)
        live = await live_capabilities(session, ctx.catalogs, meta)
        if live is not None and _conditions(live, meta) != snapshot.conditions_on_actuals:
            snapshot = snapshot.model_copy(
                update={"conditions_on_actuals": _conditions(live, meta)}
            )
    breaks = schema_breaks(current.input_schema, snapshot.input_schema) if current else []
    if current is not None:
        breaks += capability_breaks(current, snapshot)
    required, status = False, None
    if ctx is not None:  # M3.5: governance policies may require a validated version
        from mapi_server.services.governance import promotion_requirement

        req = await promotion_requirement(session, ctx, mv)
        required, status = req.require_validated, req.validation_status
    decision = await promotion_decision(
        session,
        mv,
        policy=policy,
        schema_breaks=breaks,
        require_validated=required,
        validation_status=status,
    )
    return decision, snapshot


async def _resolve_version(
    session: AsyncSession, scope: Scope, body: EndpointPromote
) -> m.ModelVersion:
    if body.model_version_id is not None:
        mv = await session.get(m.ModelVersion, body.model_version_id)
        if mv is None or (mv.tenant_id, mv.project_id) != (scope.tenant_id, scope.project_id):
            raise NotFoundError("model version", body.model_version_id)
        return mv
    assert body.model is not None
    ref = ModelVersionRef.parse(body.model)
    if ref.project != scope.project_id:
        raise MapiError(
            f"{body.model} is not in project {scope.project_id!r}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/model",
        )
    model = (
        await session.execute(
            select(m.Model).where(
                m.Model.tenant_id == scope.tenant_id,
                m.Model.project_id == scope.project_id,
                m.Model.name == ref.name,
            )
        )
    ).scalar_one_or_none()
    if model is None:
        raise NotFoundError("model", body.model)
    found = (
        await session.execute(
            select(m.ModelVersion).where(
                m.ModelVersion.model_id == model.id,
                m.ModelVersion.version == int(ref.version.removeprefix("v")),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError("model version", body.model)
    return found


async def _lock_alias(session: AsyncSession, scope: Scope, alias: str) -> None:
    """Serialise promotions of one alias (creation included) for this transaction."""
    key = f"endpoint:{scope.tenant_id}/{scope.project_id}/{alias}"
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _actor(ctx: ServerContext) -> tuple[str | None, str | None, str | None]:
    """(person id, acting principal id, token id) of the request."""
    auth = ctx.request
    if auth is None:
        return None, None, None
    return auth.user_id, auth.principal_id, auth.token.id if auth.token else None


async def _audit(
    ctx: ServerContext,
    session: AsyncSession | None,
    action: AuditAction,
    resource_id: str | None,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    code: ErrorCode | None = None,
    details: dict[str, JsonValue] | None = None,
) -> None:
    if ctx.request is None:
        return
    ev = ctx.request.audit(
        ctx,
        action,
        ResourceKind.ENDPOINT,
        outcome,
        resource_id=resource_id,
        code=code,
        details=details,
    )
    await write_audit(ctx.db, [ev], session=session)


async def promote(
    session: AsyncSession, ctx: ServerContext, alias: str, body: EndpointPromote
) -> tuple[m.Endpoint, bool]:
    """``PUT /v1/endpoints/{alias}``: returns ``(endpoint, changed)``.

    Refusals (``PROMOTION_BLOCKED``, ``OVERRIDE_REQUIRED``, a forbidden override, a stale
    ``expected_revision``) are audited as denials in their own transaction.
    """
    scope = ctx.scope
    auth = ctx.request
    await _lock_alias(session, scope, alias)
    row = (
        await session.execute(
            select(m.Endpoint)
            .where(
                m.Endpoint.tenant_id == scope.tenant_id,
                m.Endpoint.project_id == scope.project_id,
                m.Endpoint.alias == alias,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    current_rev = row.revision if row is not None else 0
    if body.expected_revision is not None and body.expected_revision != current_rev:
        raise Conflict(
            f"endpoint {alias!r} is at revision {current_rev}, not {body.expected_revision}"
        )
    if row is None and auth is not None:
        auth.require(Action.CREATE, ResourceKind.ENDPOINT, resource_name=alias)
    mv = await _resolve_version(session, scope, body)
    stored = EvaluationPolicy.model_validate(row.evaluation_policy) if row else EvaluationPolicy()
    policy = body.evaluation_policy or stored
    can_override = (
        auth.allows(Action.MANAGE, ResourceKind.ENDPOINT, resource_name=alias)
        if (auth is not None)
        else True
    )
    relaxed = policy_relaxations(stored, policy)
    if relaxed and not can_override:
        # a laxer policy disarms the gate like an override does: the same people only
        obs_metrics.record_endpoint_promotion("forbidden")
        await _audit(
            ctx,
            None,
            AuditAction.ENDPOINT_PROMOTE,
            row.id if row else None,
            outcome=AuditOutcome.DENIED,
            code=ErrorCode.FORBIDDEN,
            details={"alias": alias, "model_version_id": mv.id, "relaxes": list(relaxed)},
        )
        raise MapiError(
            "only a signed-in endpoint admin can make the evaluation policy of the endpoint less "
            "strict ("
            + "; ".join(relaxed)
            + "), as for a gate override. API tokens cannot do this",
            code=ErrorCode.FORBIDDEN,
        )
    if (
        row is not None
        and row.model_version_id == mv.id
        and (
            body.evaluation_policy is None
            or policy.model_dump(mode="json") == row.evaluation_policy
        )
    ):
        # the version it already serves: an idempotent re-PUT (or a new description) is no
        # promotion - no new revision, and no second gate decision
        if body.description is not None and body.description != row.description:
            row.description = body.description
            row.updated_at = _now()
            return row, True
        return row, False
    current = await live_snapshot(session, ctx.catalogs, row) if row else None
    if row is not None and row.model_version_id == mv.id:
        current = None  # same version: its contract is the one callers already use
    decision, snapshot = await evaluate_gate(session, mv, policy, current=current, ctx=ctx)
    model_ref = await model_ref_of(session, mv)
    try:
        overridden = authorize_promotion(
            decision, override=body.override, reason=body.reason, can_override=can_override
        )
    except MapiError as exc:
        outcome = {
            ErrorCode.PROMOTION_BLOCKED: "blocked",
            ErrorCode.NOT_VALIDATED: "blocked",
            ErrorCode.OVERRIDE_REQUIRED: "needs_override",
            ErrorCode.FORBIDDEN: "forbidden",
        }.get(exc.code, "invalid")
        if exc.code is ErrorCode.NOT_VALIDATED:
            await _audit(
                ctx,
                None,
                AuditAction.ENDPOINT_PROMOTE_NOT_VALIDATED,
                row.id if row else None,
                outcome=AuditOutcome.DENIED,
                code=exc.code,
                details={"alias": alias, "model_version_id": mv.id},
            )
        obs_metrics.record_endpoint_promotion(outcome)
        await _audit(
            ctx,
            None,
            AuditAction.ENDPOINT_PROMOTE_OVERRIDE
            if body.override
            else AuditAction.ENDPOINT_PROMOTE,
            row.id if row else None,
            outcome=AuditOutcome.DENIED,
            code=exc.code,
            details={
                "alias": alias,
                "model_version_id": mv.id,
                "gate": decision.outcome.value,
                "failing_checks": _checks(decision),
            },
        )
        if exc.code in (
            ErrorCode.PROMOTION_BLOCKED,
            ErrorCode.OVERRIDE_REQUIRED,
            ErrorCode.NOT_VALIDATED,
        ):
            exc.issues = [
                Issue(
                    code=exc.code,
                    pointer="",
                    message=r.message_plain[:2000],
                    severity=Severity.ERROR
                    if r.outcome is GateOutcome.BLOCKED
                    else Severity.WARNING,
                    context={"gate_code": r.code, "check": r.check},
                )
                for r in decision.reasons
            ]
        raise
    ts = _now()
    person, actor, token_id = _actor(ctx)
    previous = row.model_version_id if row is not None else None
    created = row is None
    if row is None:
        row = m.Endpoint(
            id=new_id("ep"),
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            alias=alias,
            description=body.description,
            created_at=ts,
        )
        session.add(row)
    elif body.description is not None:
        row.description = body.description
    row.model_id = mv.model_id
    row.model_version_id = mv.id
    row.revision = current_rev + 1
    row.evaluation_policy = policy.model_dump(mode="json")
    row.schema_snapshot = snapshot.model_dump(mode="json", exclude_none=True)
    row.gate = decision.model_dump(mode="json", exclude_none=True)
    row.overridden = overridden
    row.promoted_by = person
    row.promoted_at = ts
    row.updated_at = ts
    await session.flush()
    session.add(
        m.EndpointRevision(
            endpoint_id=row.id,
            revision=row.revision,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            alias=alias,
            action="promote",
            model_version_id=mv.id,
            model_ref=model_ref,
            previous_model_version_id=previous,
            gate=row.gate,
            overridden=overridden,
            reason=body.reason,
            actor_id=actor,
            token_id=token_id,
            created_at=ts,
        )
    )
    details: dict[str, JsonValue] = {
        "alias": alias,
        "revision": row.revision,
        "model_version_id": mv.id,
        "previous_model_version_id": previous,
        "gate": decision.outcome.value,
    }
    if policy != stored:
        details["evaluation_policy"] = {
            "from": stored.model_dump(mode="json"),
            "to": policy.model_dump(mode="json"),
        }
    if created:
        await _audit(ctx, session, AuditAction.ENDPOINT_CREATE, row.id, details=details)
    await _audit(ctx, session, AuditAction.ENDPOINT_PROMOTE, row.id, details=details)
    if overridden:
        await _audit(
            ctx,
            session,
            AuditAction.ENDPOINT_PROMOTE_OVERRIDE,
            row.id,
            details={
                **details,
                "reason": (body.reason or "")[:500],
                "failing_checks": _checks(decision),
            },
        )
    await emit_event(
        session,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        event_type=WebhookEventType.ENDPOINT_PROMOTED,
        data={
            "alias": alias,
            "ref": endpoint_ref(scope.project_id, alias),
            "revision": row.revision,
            "model_version_id": mv.id,
            "overridden": overridden,
            "action": "promote",
        },
    )
    obs_metrics.record_endpoint_promotion("overridden" if overridden else "allowed")
    return row, True


async def rollback(
    session: AsyncSession, ctx: ServerContext, alias: str, body: EndpointRollback
) -> m.Endpoint:
    """``POST /v1/endpoints/{alias}/rollback``."""
    scope = ctx.scope
    await _lock_alias(session, scope, alias)
    row = await load_endpoint(session, scope, alias, for_update=True)
    revs = select(m.EndpointRevision).where(m.EndpointRevision.endpoint_id == row.id)
    if body.to_revision is not None:
        if body.to_revision >= row.revision:
            raise Conflict(f"revision {body.to_revision} is not earlier than {row.revision}")
        target = (
            await session.execute(revs.where(m.EndpointRevision.revision == body.to_revision))
        ).scalar_one_or_none()
        if target is None:
            raise NotFoundError("endpoint revision", f"{alias}@{body.to_revision}")
    else:
        target = (
            (
                await session.execute(
                    revs.where(
                        m.EndpointRevision.revision < row.revision,
                        m.EndpointRevision.model_version_id != row.model_version_id,
                    ).order_by(m.EndpointRevision.revision.desc())
                )
            )
            .scalars()
            .first()
        )
        if target is None:
            raise Conflict(f"endpoint {alias!r} has no earlier version to roll back to")
    mv = await session.get(m.ModelVersion, target.model_version_id)
    if mv is None or mv.status != "ready":
        raise Conflict(f"the version of revision {target.revision} is no longer available")
    from mapi_server.services.governance import promotion_requirement

    governance = await promotion_requirement(session, ctx, mv)
    if not governance.satisfied:
        # M3.5: a rollback serves an earlier version again, so it meets the same policy
        state = (governance.validation_status or "not submitted").replace("_", " ")
        raise MapiError(
            f"the governance policy needs a validated model. The version of revision "
            f"{target.revision} is {state}",
            code=ErrorCode.NOT_VALIDATED,
        )
    snapshot = snapshot_for(mv)
    ts = _now()
    person, actor, token_id = _actor(ctx)
    previous = row.model_version_id
    row.model_id = mv.model_id
    row.model_version_id = mv.id
    row.revision += 1
    row.schema_snapshot = snapshot.model_dump(mode="json", exclude_none=True)
    row.gate = dict(target.gate)
    row.overridden = target.overridden
    row.promoted_by = person
    row.promoted_at = ts
    row.updated_at = ts
    model_ref = await model_ref_of(session, mv)
    session.add(
        m.EndpointRevision(
            endpoint_id=row.id,
            revision=row.revision,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            alias=alias,
            action="rollback",
            model_version_id=mv.id,
            model_ref=model_ref,
            previous_model_version_id=previous,
            gate=row.gate,
            overridden=row.overridden,
            reason=body.reason,
            actor_id=actor,
            token_id=token_id,
            created_at=ts,
        )
    )
    await session.flush()
    await _audit(
        ctx,
        session,
        AuditAction.ENDPOINT_ROLLBACK,
        row.id,
        details={
            "alias": alias,
            "revision": row.revision,
            "to_revision": target.revision,
            "model_version_id": mv.id,
            "previous_model_version_id": previous,
            "reason": body.reason[:500],
        },
    )
    await emit_event(
        session,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        event_type=WebhookEventType.ENDPOINT_PROMOTED,
        data={
            "alias": alias,
            "ref": endpoint_ref(scope.project_id, alias),
            "revision": row.revision,
            "model_version_id": mv.id,
            "overridden": row.overridden,
            "action": "rollback",
        },
    )
    obs_metrics.record_endpoint_promotion("rollback")
    return row


async def history_count(session: AsyncSession, endpoint_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).where(m.EndpointRevision.endpoint_id == endpoint_id)
            )
        ).scalar_one()
    )


# ----------------------------------------------------------------- score calls (inline data)


def _issue(code: ErrorCode, pointer: str, message: str, **kw: Any) -> Issue:
    return Issue(code=code, pointer=pointer, message=message[:2000], **kw)


def _coord_summary(
    name: str, var: XrJsonVariable, dim: DimSpec | None, pointer: str, issues: list[Issue]
) -> CoordSummary | None:
    values = var.data if isinstance(var.data, list) else [var.data]
    n = len(values)
    kind = dim.kind if dim is not None else None
    levels = values if n <= CoordSummary.MAX_LEVELS else None
    try:
        if kind == "time":
            times = [parse_time(v) for v in values]  # type: ignore[arg-type]
            freq = dim.freq if dim is not None else None
            contiguous = True
            if freq and times:
                f = parse_freq(freq)
                ords = [f.ordinal(t) for t in times]
                if any(o is None for o in ords):
                    issues.append(
                        _issue(
                            ErrorCode.NON_CONTIGUOUS_EXTENSION,
                            pointer,
                            f"{name!r} has values that are not on its {freq!r} grid",
                        )
                    )
                    return None
                ints = [int(o) for o in ords if o is not None]
                contiguous = len(set(ints)) == n and max(ints) - min(ints) + 1 == n
            first, last = (min(times), max(times)) if times else (None, None)
            return CoordSummary(
                kind="time",
                size=n,
                start=first.isoformat() if first else None,
                end=last.isoformat() if last else None,
                freq=freq,
                contiguous=contiguous,
            )
        if kind in ("integer", "index"):
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                issues.append(
                    _issue(ErrorCode.SCHEMA_INVALID, pointer, f"{name!r} must hold integers")
                )
                return None
            ints = sorted(int(v) for v in values)  # type: ignore[arg-type]
            step = 1
            if n > 1:
                diffs = [b - a for a, b in itertools.pairwise(ints) if b > a]
                step = min(diffs) if diffs else 1
            contiguous = n == 0 or (ints[-1] - ints[0]) // step + 1 == n
            return CoordSummary(
                kind=kind,
                size=n,
                values=levels,
                start=ints[0] if n else None,
                end=ints[-1] if n else None,
                step=step,
                contiguous=contiguous,
            )
        if kind == "float":
            return CoordSummary(kind="float", size=n, values=levels)
        return CoordSummary(kind="categorical", size=n, values=levels)
    except (TypeError, ValueError) as exc:
        issues.append(
            _issue(ErrorCode.SCHEMA_INVALID, pointer, f"coordinate {name!r} is not correct: {exc}")
        )
        return None


def inline_summary(
    payload: XrJsonDataset, schema: DataSchema | None, pointer: str, issues: list[Issue]
) -> DataSummary:
    """The :class:`DataSummary` of an inline payload, from its coordinates (JSON scalars),
    for the static CoordPlan checks.

    Data values are never decoded; the targets of ``schema`` and the drivers a forecast needs
    (``future_required``) are null-checked (:func:`~mapi_spec.xrjson.observed_labels`) so
    ``observed`` says, per dim, the labels at which they hold a value - the actuals of a
    forecast and the drivers on its rows (:mod:`mapi_spec.actuals`), which the checks then
    refuse or validate before a run starts. Text where the schema wants numbers (a number
    stored as text in a spreadsheet, ``#N/A``) is refused (``SCHEMA_INVALID``)."""
    coords: dict[str, CoordSummary] = {}
    sizes: dict[str, int] = dict(payload.dims)
    for var in [*payload.coords.values(), *payload.data_vars.values()]:
        shape = _shape(var)
        for d, size in zip(var.dims, shape, strict=False):
            sizes.setdefault(d, size)
    unreadable: set[str] = set()
    for name, var in payload.coords.items():
        if list(var.dims) != [name]:
            continue  # non-index coordinates are checked in the sandbox
        dim = schema.dims.get(name) if schema is not None else None
        cs = _coord_summary(name, var, dim, f"{pointer}/coords/{name}", issues)
        if cs is not None:
            coords[name] = cs
        else:
            unreadable.add(name)  # reported; a stand-in summary would add false issues
    for d, size in sizes.items():
        if d not in coords and d not in unreadable:
            coords[d] = CoordSummary(
                kind="index", size=size, start=0 if size else None, end=size - 1 if size else None
            )
    if schema is not None:
        for name, var in payload.data_vars.items():
            vs = schema.vars.get(name)
            if vs is not None and vs.dtype in _NUMERIC:
                _refuse_text(name, var, payload, f"{pointer}/data_vars/{name}", issues)
    observed: dict[str, dict[str, CoordSummary]] = {}
    checked = list(schema.targets) if schema is not None else []
    if schema is not None:
        checked += [
            n
            for n, v in schema.vars.items()
            if v.future_required and v.role is not Role.TARGET and n not in checked
        ]
    for target in checked:
        spec = payload.data_vars.get(target)
        if spec is None:
            continue
        per_dim: dict[str, CoordSummary] = {}
        for d in spec.dims:
            labels = observed_labels(payload, target, d)
            if labels is None or d not in coords:
                continue
            dim = schema.dims.get(d) if schema is not None else None
            seen = XrJsonVariable(dims=[d], data=list(labels))
            cs = _coord_summary(d, seen, dim, "", [])  # coord issues are reported above
            if cs is not None:
                per_dim[d] = cs
        if per_dim:
            observed[target] = per_dim
    return DataSummary(vars=list(payload.data_vars), coords=coords, observed=observed)


_NUMERIC: Final = frozenset({"float64", "float32", "int64", "int32", "uint8", "bool"})


def _refuse_text(
    name: str, var: XrJsonVariable, payload: XrJsonDataset, pointer: str, issues: list[Issue]
) -> None:
    """A ``SCHEMA_INVALID`` issue naming the first text cell of a numeric variable."""

    def first(node: Any, at: tuple[int, ...]) -> tuple[tuple[int, ...], str] | None:
        if isinstance(node, list):
            for k, child in enumerate(node):
                hit = first(child, (*at, k))
                if hit is not None:
                    return hit
            return None
        return (at, node) if isinstance(node, str) else None

    hit = first(var.data, ())
    if hit is None:
        return
    at, text = hit
    where: list[str] = []
    for dim, k in zip(var.dims, at, strict=False):
        coord = payload.coords.get(dim)
        labels = coord.data if coord is not None and isinstance(coord.data, list) else None
        label = labels[k] if labels is not None and k < len(labels) else k
        where.append(f"{dim}={label}")
    issues.append(
        _issue(
            ErrorCode.SCHEMA_INVALID,
            pointer,
            f"{name!r} holds text where it needs numbers (at {', '.join(where) or 'its cell'})",
            suggested_action="Send numbers (not text) for this variable, and leave a cell empty "
            "(null) where there is no value.",
            untrusted_text=repr(text)[:200],
        )
    )


def _shape(var: XrJsonVariable) -> list[int]:
    out: list[int] = []
    node: Any = var.data
    for _ in var.dims:
        if not isinstance(node, list):
            break
        out.append(len(node))
        node = node[0] if node else None
    return out


def endpoint_schema_issues(
    call: EndpointScoreCall, snapshot: EndpointSchemaSnapshot
) -> tuple[list[Issue], DataSummary | None]:
    """Envelope checks of the inline payloads against the endpoint's input contract, and
    the summary of the payload the run reads (``scenario`` over ``data``)."""
    issues: list[Issue] = []
    summary: DataSummary | None = None
    schema = snapshot.input_schema if call.inputs_stage == snapshot.inputs_stage else None
    for name in ("data", "scenario"):
        payload = getattr(call, name)
        if not isinstance(payload, XrJsonDataset):
            continue
        pointer = f"/{name}"
        try:
            check_xrjson_size(payload.model_dump(mode="json"))
        except MapiError as exc:
            exc.pointer = pointer
            raise
        if schema is not None:
            for var, spec in payload.data_vars.items():
                vp = f"{pointer}/data_vars/{var}"
                expected = schema.vars.get(var)
                if expected is None:
                    issues.append(
                        _issue(
                            ErrorCode.SCHEMA_INVALID,
                            vp,
                            f"the endpoint's input has no variable {var!r}",
                            context={"known": sorted(schema.vars)[:50]},
                        )
                    )
                    continue
                if list(spec.dims) != list(expected.dims):
                    issues.append(
                        _issue(
                            ErrorCode.SCHEMA_INVALID,
                            f"{vp}/dims",
                            f"{var!r} must have dims {list(expected.dims)}, not {list(spec.dims)}",
                        )
                    )
            for coord in payload.coords:
                if coord not in schema.dims and coord not in schema.vars:
                    issues.append(
                        _issue(
                            ErrorCode.COORD_NOT_ALLOWED,
                            f"{pointer}/coords/{coord}",
                            f"{coord!r} is not a dim of the endpoint's input",
                        )
                    )
            if name == "data" and call.mode == "predict":
                missing = sorted(
                    n
                    for n, v in schema.vars.items()
                    if v.role
                    not in (Role.TARGET, Role.INDEX, Role.CONSTANT, Role.MASK, Role.WEIGHT)
                    and v.missing == "forbid"
                    and n not in payload.data_vars
                )
                if missing:
                    issues.append(
                        _issue(
                            ErrorCode.SCHEMA_INVALID,
                            f"{pointer}/data_vars",
                            f"the payload does not have these inputs: {missing}",
                            suggested_action="Send each input variable that the "
                            "schema of the endpoint lists (GET …/schema).",
                        )
                    )
        current = inline_summary(payload, schema, pointer, issues)
        if name == "scenario" or summary is None:
            summary = current
    return issues, summary


def score_request_for(call: EndpointScoreCall, project: str, alias: str) -> ScoreRequest:
    try:
        return call.to_score_request(endpoint_ref(project, alias))
    except ValueError as exc:
        raise MapiError(
            f"the call is not a correct score request: {exc}"[:2000], code=ErrorCode.SCHEMA_INVALID
        ) from None


async def store_inline_inputs(
    ctx: ServerContext, scope: Scope, call: EndpointScoreCall
) -> dict[str, Any]:
    """Store each inline payload (XrJSON, content-addressed) as a run input."""
    out: dict[str, Any] = {}
    tenant = check_segment(scope.tenant_id, "tenant_id")
    for input_name, payload in call.inline_inputs().items():
        body = json.dumps(
            payload.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        sha = hashlib.sha256(body).hexdigest()
        meta = await ctx.store.put(
            ctx.buckets.quarantine,
            f"t/{tenant}/{INLINE_PREFIX}/{sha}.json",
            body,
            content_type="application/json",
        )
        out[input_name] = StoredRef(meta.bucket, meta.name, meta.generation)
    return out
