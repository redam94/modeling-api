"""Dashboard drafts, validation, publishing, archiving and client grants (v0.16, plan D6-D7).

* :func:`create_dashboard` stores a draft (``name`` unique per project);
* :func:`update_draft` replaces the draft spec under ``If-Match`` (``draft_etag``): a stale
  etag is ``DASHBOARD_SPEC_STALE`` (412), no etag ``PRECONDITION_FAILED``;
* :func:`validate_draft` resolves the source to a model version and checks what the readouts
  and inputs need (a ready version that ran no uploaded code, the response grid for
  what-ifs with every channel in it, the KPI among the model's variables, an actuals
  template, linked workflows in the project);
* :func:`publish` pins that version in an immutable :class:`~mapi_server.db.models.
  DashboardRevision`, makes the caller (a runner) the ``run_as`` owner of the live inputs and
  queues the baseline readout (the dashboard loop submits it as that owner);
* :func:`archive` hides the dashboard from clients and revokes every share link;
* grants (:func:`put_grant` / :func:`delete_grant`) name users who already hold a membership
  of the project.

Every change is audited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction
from mapi_spec.dashboards import (
    DashboardCreate,
    DashboardDraftUpdate,
    DashboardSpec,
    DashboardValidation,
    dashboard_spec_hash,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Problem
from mapi_spec.tenancy import Action, ResourceKind
from mapi_spec.views import RESPONSE_GRID_VIEW, ResponseGridView
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.jsonlimits import loads_limited
from mapi_server.services.audit import write_audit
from mapi_server.services.bundles import verified_bundle_object
from mapi_server.services.dashboards.access import load_dashboard
from mapi_server.services.endpoints import model_ref_of, snapshot_for
from mapi_server.services.runs import now
from mapi_server.services.sharing import ran_uploaded_code

__all__ = [
    "Binding",
    "archive",
    "create_dashboard",
    "delete_grant",
    "next_etag",
    "publish",
    "put_grant",
    "read_grid",
    "resolve_binding",
    "update_draft",
    "validate_draft",
]

log = logging.getLogger(__name__)

_FIRST_ETAG: Final = '"1"'


def next_etag(etag: str) -> str:
    """``"n"`` → ``"n+1"`` (a counter in an entity tag; anything else restarts at 2)."""
    try:
        return f'"{int(etag.strip(chr(34))) + 1}"'
    except ValueError:
        return '"2"'


def _user(ctx: ServerContext) -> str:
    auth = ctx.request
    return (auth.user_id or auth.principal_id or "") if auth is not None else "system"


async def _audit(
    session: AsyncSession,
    ctx: ServerContext,
    action: AuditAction,
    resource_id: str,
    details: dict[str, JsonValue] | None = None,
    kind: ResourceKind = ResourceKind.DASHBOARD,
) -> None:
    if ctx.request is None:
        return
    await write_audit(
        ctx.db,
        [ctx.request.audit(ctx, action, kind, resource_id=resource_id, details=details)],
        session=session,
    )


# =================================================================================== drafts


async def create_dashboard(
    session: AsyncSession, ctx: ServerContext, body: DashboardCreate
) -> m.Dashboard:
    """``POST /v1/dashboards``: a new draft (``CONFLICT`` when the name is taken)."""
    sc = ctx.scope
    taken = (
        await session.execute(
            select(m.Dashboard.id).where(
                m.Dashboard.tenant_id == sc.tenant_id,
                m.Dashboard.project_id == sc.project_id,
                m.Dashboard.name == body.name,
            )
        )
    ).first()
    if taken is not None:
        raise Conflict(f"a dashboard named {body.name!r} exists in this project")
    ts = now()
    row = m.Dashboard(
        id=new_id("dsh"),
        tenant_id=sc.tenant_id,
        project_id=sc.project_id,
        name=body.name,
        status="draft",
        draft_spec=body.spec.model_dump(mode="json"),
        draft_etag=_FIRST_ETAG,
        live_inputs="ok",
        created_by=_user(ctx),
        created_at=ts,
        updated_at=ts,
    )
    session.add(row)
    await session.flush()
    await _audit(session, ctx, AuditAction.DASHBOARD_CREATE, row.id, {"name": row.name})
    return row


async def update_draft(
    session: AsyncSession,
    ctx: ServerContext,
    dashboard_id: str,
    body: DashboardDraftUpdate,
    if_match: str | None,
) -> m.Dashboard:
    """``PUT …/draft``: replace the draft when ``If-Match`` names its current etag."""
    row = await load_dashboard(session, ctx, dashboard_id, for_update=True)
    if row.status == "archived":
        raise Conflict("the dashboard is archived; its draft cannot change")
    if not if_match:
        raise MapiError(
            "send If-Match with the draft_etag you read (the draft may have changed)",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    if if_match.strip() not in (row.draft_etag, "W/" + row.draft_etag):
        raise MapiError(
            "the draft changed since you read it: read it again and repeat the change",
            code=ErrorCode.DASHBOARD_SPEC_STALE,
        )
    row.draft_spec = body.spec.model_dump(mode="json")
    row.draft_etag = next_etag(row.draft_etag)
    row.updated_at = now()
    await session.flush()
    return row


# =============================================================================== validation


@dataclass(frozen=True, slots=True)
class Binding:
    """What a spec resolves to: the model version a revision pins (and, for an endpoint
    source, the alias and endpoint revision it was resolved from)."""

    model_version: m.ModelVersion
    model_ref: str
    endpoint_alias: str | None = None
    endpoint_revision: int | None = None


def _issue(code: ErrorCode, pointer: str, message: str) -> Issue:
    return Issue(code=code, pointer=pointer, message=message)


async def _model_version(
    session: AsyncSession, ctx: ServerContext, spec: DashboardSpec, issues: list[Issue]
) -> Binding | None:
    sc = ctx.scope
    src = spec.source
    if src.kind == "endpoint":
        ep = (
            await session.execute(
                select(m.Endpoint).where(
                    m.Endpoint.tenant_id == sc.tenant_id,
                    m.Endpoint.project_id == sc.project_id,
                    m.Endpoint.alias == src.alias,
                )
            )
        ).scalar_one_or_none()
        if ep is None:
            issues.append(
                _issue(ErrorCode.REF_UNRESOLVED, "/source/alias", f"no endpoint {src.alias!r}")
            )
            return None
        mv = await session.get(m.ModelVersion, ep.model_version_id)
        if mv is None:  # pragma: no cover - foreign key
            issues.append(_issue(ErrorCode.REF_UNRESOLVED, "/source/alias", "no model version"))
            return None
        return Binding(mv, await model_ref_of(session, mv), ep.alias, ep.revision)
    model = (
        await session.execute(
            select(m.Model).where(
                m.Model.tenant_id == sc.tenant_id,
                m.Model.project_id == sc.project_id,
                or_(m.Model.name == src.model_id, m.Model.id == src.model_id),
            )
        )
    ).scalar_one_or_none()
    if model is None:
        issues.append(
            _issue(ErrorCode.REF_UNRESOLVED, "/source/model_id", f"no model {src.model_id!r}")
        )
        return None
    stmt = select(m.ModelVersion).where(m.ModelVersion.model_id == model.id)
    if src.version is not None:
        stmt = stmt.where(m.ModelVersion.version == src.version)
    else:  # the latest ready version
        stmt = stmt.where(m.ModelVersion.status == "ready")
    mv = (
        await session.execute(stmt.order_by(m.ModelVersion.version.desc()).limit(1))
    ).scalar_one_or_none()
    if mv is None:
        which = f"version {src.version}" if src.version is not None else "a ready version"
        issues.append(
            _issue(ErrorCode.REF_UNRESOLVED, "/source", f"model {src.model_id!r} has no {which}")
        )
        return None
    return Binding(mv, await model_ref_of(session, mv))


async def resolve_binding(
    session: AsyncSession, ctx: ServerContext, spec: DashboardSpec
) -> Binding:
    """The model version ``spec`` pins now (``REF_UNRESOLVED`` with the issue otherwise)."""
    issues: list[Issue] = []
    binding = await _model_version(session, ctx, spec, issues)
    if binding is None:
        raise MapiError(issues[0].message, code=ErrorCode.REF_UNRESOLVED, issues=issues)
    return binding


async def read_grid(
    ctx: ServerContext, mv: m.ModelVersion, *, max_bytes: int | None = None
) -> ResponseGridView | None:
    """The version's response grid (``ui/grids/response_curves.json``), read only after the
    signed manifest verifies, size- and depth-capped and validated (JSON only, never
    unpickled). ``None`` when the bundle has none; ``INTEGRITY_FAILURE`` and an invalid or
    oversized grid raise."""
    cap = max_bytes if max_bytes is not None else ctx.settings.dashboard_grid_max_bytes
    try:
        obj = await verified_bundle_object(ctx.store, ctx.signer, mv, RESPONSE_GRID_VIEW)
    except NotFoundError:
        return None
    if obj.size > cap:
        raise MapiError(
            f"the response grid is {obj.size} bytes (max {cap})",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
        )
    raw = await ctx.store.get(obj.bucket, obj.key, generation=obj.generation, max_bytes=cap)
    try:
        return ResponseGridView.model_validate(loads_limited(raw))
    except (ValidationError, ValueError) as exc:
        raise MapiError(
            "the model's response grid is not valid",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None


async def _workflow_exists(session: AsyncSession, ctx: ServerContext, workflow_id: str) -> bool:
    sc = ctx.scope
    found = (
        await session.execute(
            select(m.Workflow.id).where(
                m.Workflow.id == workflow_id,
                m.Workflow.tenant_id == sc.tenant_id,
                m.Workflow.project_id == sc.project_id,
                m.Workflow.archived.is_(False),
            )
        )
    ).first()
    return found is not None


async def validate_draft(
    session: AsyncSession, ctx: ServerContext, spec: DashboardSpec
) -> tuple[DashboardValidation, Binding | None]:
    """Can ``spec`` be published now? The issues point into the spec (JSON pointers)."""
    issues: list[Issue] = []
    binding = await _model_version(session, ctx, spec, issues)
    if binding is not None:
        await _check_version(ctx, spec, binding.model_version, issues)
    for pointer, wf in (
        ("/refresh/workflow_id", spec.refresh.workflow_id),
        ("/actuals/refit_workflow_id", spec.actuals.refit_workflow_id),
    ):
        if wf is not None and not await _workflow_exists(session, ctx, wf):
            issues.append(
                _issue(ErrorCode.REF_UNRESOLVED, pointer, f"no workflow {wf!r} in this project")
            )
    return DashboardValidation(ok=not issues, issues=issues), binding


async def _check_version(
    ctx: ServerContext, spec: DashboardSpec, mv: m.ModelVersion, issues: list[Issue]
) -> None:
    if mv.status != "ready":
        issues.append(_issue(ErrorCode.NOT_READY, "/source", f"model version {mv.id} is not ready"))
    if ran_uploaded_code(mv):
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/source",
                "the model version ran uploaded plugin code; dashboards show only versions "
                "fitted with platform plugins",
            )
        )
        return
    try:
        grid = await read_grid(ctx, mv)
    except MapiError as exc:
        issues.append(_issue(exc.code, "/source", f"the response grid cannot be read: {exc}"))
        grid = None
    if grid is None and spec.whatif.enabled:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/whatif/enabled",
                "the model version has no response grid: turn what-ifs off",
            )
        )
    if grid is not None:
        known = set(grid.channels)
        for i, ch in enumerate(spec.channels):
            if ch.name not in known:
                issues.append(
                    _issue(
                        ErrorCode.BINDING_INVALID,
                        f"/channels/{i}/name",
                        f"channel {ch.name!r} is not in the model's response grid",
                    )
                )
    variables: set[str] = {grid.kpi} if grid is not None else set()
    actuals_ok = True
    try:
        variables |= set(snapshot_for(mv).input_schema.vars)
    except MapiError:
        actuals_ok = False
    if spec.kpi.variable not in variables:
        issues.append(
            _issue(
                ErrorCode.BINDING_INVALID,
                "/kpi/variable",
                f"{spec.kpi.variable!r} is not a variable of the model",
            )
        )
    if spec.actuals.enabled and not actuals_ok:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/actuals/enabled",
                "the model version has no input contract for actuals: turn uploads off",
            )
        )


# =================================================================================== publish


def _may_run(ctx: ServerContext) -> bool:
    """The runner rule of D7: the live inputs spend the publisher's compute."""
    auth = ctx.request
    return auth is not None and (
        auth.allows(Action.CREATE, ResourceKind.RUN) and auth.allows(Action.RUN, ResourceKind.MODEL)
    )


def _refuse_without_runner(ctx: ServerContext, what: str) -> None:
    if not _may_run(ctx):
        raise MapiError(
            f"{what} needs the runner role: the dashboard's live inputs run as you and use "
            "the project's compute",
            code=ErrorCode.FORBIDDEN,
        )


async def _next_seq(
    session: AsyncSession,
    table: type[m.DashboardRevision] | type[m.DashboardReadout],
    dashboard_id: str,
) -> int:
    """The next ``seq`` of a dashboard's revisions or readouts (the caller holds the
    dashboard row ``FOR UPDATE``, so two writers never pick the same one)."""
    top = await session.execute(
        select(func.coalesce(func.max(table.seq), 0)).where(table.dashboard_id == dashboard_id)
    )
    return int(top.scalar_one()) + 1


async def publish(session: AsyncSession, ctx: ServerContext, dashboard_id: str) -> m.Dashboard:
    """``POST …:publish``: validate the draft, pin its model version in a new revision, make
    the caller ``run_as`` and queue a ``baseline`` readout. Needs the runner role."""
    _refuse_without_runner(ctx, "publishing a dashboard")
    row = await load_dashboard(session, ctx, dashboard_id, for_update=True)
    if row.status == "archived":
        raise Conflict("the dashboard is archived")
    spec = DashboardSpec.model_validate(row.draft_spec)
    result, binding = await validate_draft(session, ctx, spec)
    if not result.ok or binding is None:
        first = result.issues[0]
        not_ready = first.code is ErrorCode.NOT_READY
        raise MapiError(
            f"the dashboard cannot be published: {first.message}",
            code=ErrorCode.NOT_READY if not_ready else ErrorCode.SCHEMA_INVALID,
            pointer=first.pointer,
            issues=result.issues,
        )
    seq = await _next_seq(session, m.DashboardRevision, row.id)
    ts = now()
    user = _user(ctx)
    mv = binding.model_version
    rev = m.DashboardRevision(
        id=new_id("dsr"),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        dashboard_id=row.id,
        seq=seq,
        spec=spec.model_dump(mode="json"),
        spec_hash=dashboard_spec_hash(spec),
        model_version_id=mv.id,
        model_ref=binding.model_ref,
        endpoint_alias=binding.endpoint_alias,
        endpoint_revision=binding.endpoint_revision,
        created_by=user,
        created_at=ts,
    )
    session.add(rev)
    await session.flush()
    row.status = "published"
    row.published_revision_id = rev.id
    row.run_as = user
    row.live_inputs = "ok"
    row.problem = None
    row.updated_at = ts
    await _queue_baseline(session, row, rev, user)
    await session.flush()
    await _audit(
        session,
        ctx,
        AuditAction.DASHBOARD_PUBLISH,
        row.id,
        {"revision": seq, "model_version_id": mv.id, "model_ref": binding.model_ref},
    )
    return row


async def _queue_baseline(
    session: AsyncSession, row: m.Dashboard, rev: m.DashboardRevision, user: str
) -> None:
    """The ``baseline`` readout of a new revision, ``queued``: the dashboard loop submits it
    as ``run_as`` and materialises it (plan D10). The dashboard row is locked (``FOR
    UPDATE``) by the caller, so the next ``seq`` is safe."""
    seq = await _next_seq(session, m.DashboardReadout, row.id)
    ts = now()
    session.add(
        m.DashboardReadout(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            dashboard_id=row.id,
            seq=seq,
            revision_id=rev.id,
            kind="baseline",
            trigger="publish",
            visibility="published",
            status="queued",
            title=f"Published on {ts:%d/%m/%Y}",
            actor_kind="member",
            actor_id=user,
            model_version_id=rev.model_version_id,
            model_ref=rev.model_ref,
            endpoint_revision=rev.endpoint_revision,
            spec_hash=rev.spec_hash,
            run_ids=[],
            created_at=ts,
        )
    )


async def archive(session: AsyncSession, ctx: ServerContext, dashboard_id: str) -> m.Dashboard:
    """``POST …:archive``: clients and share links lose the dashboard; every share link is
    revoked (its sessions end). Idempotent: archiving again changes nothing."""
    row = await load_dashboard(session, ctx, dashboard_id, for_update=True)
    if row.status == "archived":
        return row
    ts = now()
    row.status = "archived"
    row.archived_at = ts
    row.updated_at = ts
    res = await session.execute(
        update(m.DashboardShare)
        .where(m.DashboardShare.dashboard_id == row.id, m.DashboardShare.revoked_at.is_(None))
        .values(revoked_at=ts, session_version=m.DashboardShare.session_version + 1)
        .execution_options(synchronize_session=False)
    )
    # an archived dashboard spends nothing more: open requests are superseded, approved ones
    # whose workflow has not started fail (queued readouts fail when the loop sees them)
    await session.execute(
        update(m.DashboardRefitRequest)
        .where(
            m.DashboardRefitRequest.dashboard_id == row.id,
            m.DashboardRefitRequest.status == "open",
        )
        .values(status="superseded", decided_at=ts)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(m.DashboardRefitRequest)
        .where(
            m.DashboardRefitRequest.dashboard_id == row.id,
            m.DashboardRefitRequest.status == "approved",
            m.DashboardRefitRequest.workflow_run_id.is_(None),
        )
        .values(
            status="failed",
            problem=Problem.from_code(
                ErrorCode.DASHBOARD_NOT_PUBLISHED, "the dashboard was archived"
            ).model_dump(mode="json", exclude_none=True),
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    await _audit(
        session,
        ctx,
        AuditAction.DASHBOARD_ARCHIVE,
        row.id,
        {"shares_revoked": int(getattr(res, "rowcount", 0) or 0)},
    )
    return row


# ==================================================================================== grants


async def _membership(
    session: AsyncSession, ctx: ServerContext, user_id: str
) -> m.Membership | None:
    """The user's grant in this project (a project grant first, else a tenant-wide one)."""
    sc = ctx.scope
    rows = (
        await session.execute(
            select(m.Membership).where(
                m.Membership.tenant_id == sc.tenant_id,
                m.Membership.principal_id == user_id,
                or_(m.Membership.project_id == sc.project_id, m.Membership.project_id.is_(None)),
            )
        )
    ).scalars()
    found = sorted(rows, key=lambda r: r.project_id is None)
    return found[0] if found else None


async def put_grant(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, user_id: str
) -> tuple[m.DashboardGrant, m.Membership]:
    """``PUT …/grants/{user_id}`` (idempotent). The user must already be a member of the
    project (invite a new client as a member with the ``client`` role first)."""
    row = await load_dashboard(session, ctx, dashboard_id, for_update=True)
    if row.status == "archived":
        raise Conflict("the dashboard is archived")
    member = await _membership(session, ctx, user_id)
    if member is None:
        raise NotFoundError("member", user_id)
    inserted = await session.execute(
        insert(m.DashboardGrant)
        .values(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            dashboard_id=row.id,
            user_id=user_id,
            granted_by=_user(ctx),
            created_at=now(),
        )
        .on_conflict_do_nothing()
        .returning(m.DashboardGrant.user_id)
    )
    if inserted.first() is not None:
        await _audit(
            session,
            ctx,
            AuditAction.DASHBOARD_GRANT,
            row.id,
            {"user_id": user_id, "role": member.role},
        )
    grant = await session.get(m.DashboardGrant, (row.id, user_id), populate_existing=True)
    assert grant is not None
    return grant, member


async def delete_grant(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, user_id: str
) -> None:
    """``DELETE …/grants/{user_id}`` (idempotent: an absent grant answers the same)."""
    row = await load_dashboard(session, ctx, dashboard_id)
    grant = await session.get(m.DashboardGrant, (row.id, user_id))
    if grant is None:
        return
    await session.delete(grant)
    await session.flush()
    await _audit(session, ctx, AuditAction.DASHBOARD_UNGRANT, row.id, {"user_id": user_id})
