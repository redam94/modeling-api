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
* :func:`upgrade_draft` (``POST …:upgrade``, v0.17) converts a layout 1 draft to layout 2 and
  saves it as the draft (the automatic pass at start-up is
  :mod:`mapi_server.services.dashboards.upgrade`);
* grants (:func:`put_grant` / :func:`delete_grant`) name users who already hold a membership
  of the project.

Every change is audited.

v0.17: every spec is either layout (:func:`~mapi_spec.dashboard_v2.parse_dashboard_spec`). A
layout 2 spec is validated against the source catalog of its model version
(:func:`~mapi_server.services.dashboards.sources.check_version_v2`; only ``error`` issues
block a publish), and its publish also queues the revision's pictures
(:func:`~mapi_server.services.dashboards.assets.queue_pictures`). A new dashboard is layout 2:
creating one from a layout 1 spec is refused (``DASHBOARD_LAYOUT_RETIRED``) unless the
``dashboard_layout1_create`` setting allows it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction
from mapi_spec.dashboard_presets import upgrade_v1
from mapi_spec.dashboard_v2 import DashboardSpecV2, parse_dashboard_spec
from mapi_spec.dashboards import (
    DashboardCreate,
    DashboardDraftUpdate,
    DashboardSource,
    DashboardSpec,
    DashboardValidation,
    dashboard_spec_hash,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Problem, Severity
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
    "AnySpec",
    "Binding",
    "UpgradeResult",
    "archive",
    "create_dashboard",
    "delete_grant",
    "next_etag",
    "publish",
    "put_grant",
    "read_grid",
    "resolve_binding",
    "update_draft",
    "upgrade_draft",
    "validate_draft",
]

AnySpec = DashboardSpec | DashboardSpecV2
"""A spec of either layout."""

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
    """``POST /v1/dashboards``: a new draft (``CONFLICT`` when the name is taken). From
    v0.17 a new draft is layout 2 (``DASHBOARD_LAYOUT_RETIRED`` for a layout 1 spec, unless
    the ``dashboard_layout1_create`` setting allows it)."""
    if isinstance(body.spec, DashboardSpec) and not layout1_create_allowed(ctx):
        raise MapiError(
            "new dashboards use layout 2: pick a preset (GET /v1/dashboards:presets) and send "
            "its spec",
            code=ErrorCode.DASHBOARD_LAYOUT_RETIRED,
            pointer="/spec/layout_version",
        )
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


def layout1_create_allowed(ctx: ServerContext) -> bool:
    """May a new dashboard (or a layout 2 draft) take a layout 1 spec? The
    ``dashboard_layout1_create`` setting (true until the setting exists: v0.16 callers and
    tests keep working)."""
    return bool(getattr(ctx.settings, "dashboard_layout1_create", True))


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
    if (
        isinstance(body.spec, DashboardSpec)
        and isinstance(parse_dashboard_spec(row.draft_spec), DashboardSpecV2)
        and not layout1_create_allowed(ctx)
    ):
        raise MapiError(
            "the draft uses layout 2: a layout 1 spec cannot replace it",
            code=ErrorCode.DASHBOARD_LAYOUT_RETIRED,
            pointer="/spec/layout_version",
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
    session: AsyncSession, ctx: ServerContext, spec: AnySpec, issues: list[Issue]
) -> Binding | None:
    sc = ctx.scope
    return await binding_of(session, sc.tenant_id, sc.project_id, spec.source, issues)


async def binding_of(
    session: AsyncSession,
    tenant_id: str,
    project_id: str,
    src: DashboardSource,
    issues: list[Issue],
) -> Binding | None:
    """The model version ``src`` names in the project now (``None`` with an issue)."""
    if src.kind == "endpoint":
        ep = (
            await session.execute(
                select(m.Endpoint).where(
                    m.Endpoint.tenant_id == tenant_id,
                    m.Endpoint.project_id == project_id,
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
                m.Model.tenant_id == tenant_id,
                m.Model.project_id == project_id,
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


async def resolve_binding(session: AsyncSession, ctx: ServerContext, spec: AnySpec) -> Binding:
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
    session: AsyncSession, ctx: ServerContext, spec: AnySpec
) -> tuple[DashboardValidation, Binding | None]:
    """Can ``spec`` be published now? The issues point into the spec (JSON pointers). A
    layout 2 spec may also carry warnings and infos: only errors make ``ok`` false."""
    issues: list[Issue] = []
    binding = await _model_version(session, ctx, spec, issues)
    if binding is not None:
        if isinstance(spec, DashboardSpecV2):
            from mapi_server.services.dashboards.sources import check_version_v2

            issues += await check_version_v2(ctx, session, spec, binding.model_version)
        else:
            await _check_version(ctx, spec, binding.model_version, issues)
    for pointer, wf in (
        ("/refresh/workflow_id", spec.refresh.workflow_id),
        ("/actuals/refit_workflow_id", spec.actuals.refit_workflow_id),
    ):
        if wf is not None and not await _workflow_exists(session, ctx, wf):
            issues.append(
                _issue(ErrorCode.REF_UNRESOLVED, pointer, f"no workflow {wf!r} in this project")
            )
    ok = not any(i.severity is Severity.ERROR for i in issues)
    return DashboardValidation(ok=ok, issues=issues[:200]), binding


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


async def publish(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, if_match: str | None = None
) -> m.Dashboard:
    """``POST …:publish``: validate the draft, pin its model version in a new revision, make
    the caller ``run_as`` and queue a ``baseline`` readout. Needs the runner role. With
    ``If-Match``, only the draft of that etag is published (``DASHBOARD_SPEC_STALE`` when
    another editor saved one since: the publisher never makes an unseen spec live)."""
    _refuse_without_runner(ctx, "publishing a dashboard")
    row = await load_dashboard(session, ctx, dashboard_id, for_update=True)
    if row.status == "archived":
        raise Conflict("the dashboard is archived")
    if if_match and if_match.strip() not in (row.draft_etag, "W/" + row.draft_etag):
        raise MapiError(
            "the draft changed since you checked it: look at the new draft, then publish",
            code=ErrorCode.DASHBOARD_SPEC_STALE,
        )
    spec = parse_dashboard_spec(row.draft_spec)
    result, binding = await validate_draft(session, ctx, spec)
    if not result.ok or binding is None:
        first = next(i for i in result.issues if i.severity is Severity.ERROR)
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
    if isinstance(spec, DashboardSpecV2):
        from mapi_server.services.dashboards.assets import queue_pictures

        await queue_pictures(ctx, session, rev, spec)
    await _audit(
        session,
        ctx,
        AuditAction.DASHBOARD_PUBLISH,
        row.id,
        {"revision": seq, "model_version_id": mv.id, "model_ref": binding.model_ref},
    )
    return row


async def _queue_baseline(
    session: AsyncSession,
    row: m.Dashboard,
    rev: m.DashboardRevision,
    user: str | None,
    *,
    actor_kind: str = "member",
    title: str | None = None,
) -> None:
    """The ``baseline`` readout of a new revision, ``queued``: the dashboard loop submits it
    as ``run_as`` and materialises it (plan D10). The dashboard row is locked (``FOR
    UPDATE``) by the caller, so the next ``seq`` is safe; it also honours the never-lowered
    ``next_readout_seq`` (0019), so a pruned readout's seq is not reused."""
    seq = max(await _next_seq(session, m.DashboardReadout, row.id), row.next_readout_seq or 1)
    row.next_readout_seq = seq + 1
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
            title=title or f"Published on {ts:%d/%m/%Y}",
            actor_kind=actor_kind,
            actor_id=user,
            model_version_id=rev.model_version_id,
            model_ref=rev.model_ref,
            endpoint_revision=rev.endpoint_revision,
            spec_hash=rev.spec_hash,
            run_ids=[],
            created_at=ts,
        )
    )


# =================================================================================== upgrade


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    """``POST …:upgrade``: the dashboard with its layout 2 draft, what to fix and what
    changed."""

    row: m.Dashboard
    issues: list[Issue]
    notes: list[str]


def _upgrade_refused(exc: Exception) -> MapiError:
    return MapiError(
        "the draft cannot be converted to layout 2: change it (or start from a preset) and "
        "try again",
        code=ErrorCode.SCHEMA_INVALID,
        untrusted_text=str(exc)[:4000],
    )


async def upgrade_draft(
    session: AsyncSession, ctx: ServerContext, dashboard_id: str, if_match: str | None
) -> UpgradeResult:
    """``POST …:upgrade`` (editor, ``If-Match``): convert the layout 1 draft with
    :func:`~mapi_spec.dashboard_presets.upgrade_v1` against the source catalog of its model
    version and save it as the draft (a new etag). Publishing it is a normal publish.
    ``CONFLICT`` when the draft already is layout 2 or the dashboard is archived. Audited
    (``DASHBOARD_UPGRADED``, ``draft: true``)."""
    from mapi_server.services.dashboards.sources import check_version_v2, source_catalog

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
            "the draft changed since you read it: read it again and repeat the upgrade",
            code=ErrorCode.DASHBOARD_SPEC_STALE,
        )
    spec = parse_dashboard_spec(row.draft_spec)
    if not isinstance(spec, DashboardSpec):
        raise Conflict("the draft already uses layout 2")
    binding = await resolve_binding(session, ctx, spec)
    catalog = await source_catalog(ctx, session, binding.model_version)
    try:
        new, notes = upgrade_v1(spec, catalog)
    except (ValueError, ValidationError) as exc:
        raise _upgrade_refused(exc) from None
    issues = await check_version_v2(ctx, session, new, binding.model_version)
    row.draft_spec = new.model_dump(mode="json")
    row.draft_etag = next_etag(row.draft_etag)
    row.updated_at = now()
    await session.flush()
    await _audit(
        session,
        ctx,
        AuditAction.DASHBOARD_UPGRADED,
        row.id,
        {
            "from_revision": None,
            "to_revision": None,
            "draft": True,
            "preset": new.preset,
            "notes": list(notes[:20]),
        },
    )
    return UpgradeResult(row, issues[:200], notes[:50])


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
