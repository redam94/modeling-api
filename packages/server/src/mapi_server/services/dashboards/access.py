"""Who may see which dashboard, and what they see of it (v0.16, plan D4-D5).

One function decides visibility, :func:`load_dashboard`:

* viewers and up see every dashboard of the project (a ``draft`` one needs the editor
  role: drafts are work in progress);
* a ``client`` sees a dashboard only when it is ``published`` and a ``dashboard_grants`` row
  names them; anything else answers ``404`` (as for a dashboard that does not exist). A
  granted dashboard made with uploaded code answers ``403 UPLOADED_CODE_NOT_SHAREABLE``
  (v0.19, plan D16): every client read and input starts here.

:func:`member_context` turns a signed-in caller and a dashboard into the
:class:`DashboardContext` the readout, input and tool functions act on; share-link
sessions build theirs in :mod:`mapi_server.services.dashboards.shares`. The DTO builders
here (:func:`dashboard_dto`, :func:`client_dashboard`, :func:`readout_dto`, ...) are the
only way rows become responses, so the analyst, client and share views cannot drift.

v0.17: a stored spec is either layout (:func:`any_spec`, :func:`any_revision_spec`).
:func:`revision_spec` stays the layout 1 accessor of the media planner's inputs (what-ifs,
the grid, refits of v0.16) and answers ``DASHBOARD_INPUT_DISABLED`` for a layout 2 revision.
:func:`effective_inputs` and :func:`client_dashboard` take either layout; image batch
scoring is for members only (plan §10.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal

from mapi_spec.dashboard_tools import PrincipalKind
from mapi_spec.dashboard_v2 import (
    UPGRADE_NOTE,
    UPGRADE_NOTE_RANGES,
    BatchForm,
    DashboardSpecV2,
    ScenarioForm,
    TabKeyV2,
    kpi_of_subject,
    panel_meta,
    parse_dashboard_spec,
)
from mapi_spec.dashboards import (
    ClientDashboard,
    Dashboard,
    DashboardGrant,
    DashboardRevision,
    DashboardShare,
    DashboardSpec,
    DashboardSummary,
    EffectiveInputs,
    Readout,
    ReadoutHeader,
    ReadoutSnapshot,
    TabKey,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Problem
from mapi_spec.tenancy import Action, ProjectRole, ResourceKind
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, unauthenticated
from mapi_server.services.endpoints import live_capabilities_snapshot
from mapi_server.services.runs import now
from mapi_server.services.sharing import require_shareable

__all__ = [
    "OVERVIEW_KINDS",
    "ActualsMode",
    "AnySpec",
    "DashboardContext",
    "actuals_mode",
    "any_revision_spec",
    "any_spec",
    "caller_is_client",
    "client_dashboard",
    "dashboard_dto",
    "effective_inputs",
    "grant_dto",
    "is_mine",
    "latest_readout",
    "load_dashboard",
    "load_revision",
    "member_context",
    "readout_dto",
    "readout_header",
    "refit_statuses",
    "revision_dto",
    "revision_spec",
    "share_dto",
    "share_status",
    "summary_dto",
    "viewer_problem",
]

ActualsMode = Literal["conditioned", "compared"]
AnySpec = DashboardSpec | DashboardSpecV2
"""A spec of either layout."""


@dataclass(frozen=True, slots=True)
class DashboardContext:
    """One caller acting on one published dashboard (plan I-3).

    ``principal_id`` is the user id (members, clients) or the share id (share links);
    ``inputs`` are what the caller may use (the published spec, narrowed by a share link);
    ``is_analyst``: a viewer-or-higher member (sees every readout, private ones included)."""

    tenant_id: str
    project_id: str
    dashboard_id: str
    revision_id: str
    principal_kind: PrincipalKind
    principal_id: str
    inputs: EffectiveInputs
    is_analyst: bool


# ================================================================================== callers


def caller_is_client(auth: RequestAuth | None) -> bool:
    """Is the caller's effective role in the project ``client`` (the rank-0 role)?"""
    ctx = auth.context if auth is not None else None
    return ctx is not None and ctx.role is ProjectRole.CLIENT


def _is_analyst(auth: RequestAuth | None) -> bool:
    ctx = auth.context if auth is not None else None
    if ctx is None:
        return False
    return ctx.operator or (ctx.role is not None and ctx.role >= ProjectRole.VIEWER)


# ================================================================================== loading


async def load_dashboard(
    session: AsyncSession,
    ctx: ServerContext,
    dashboard_id: str,
    *,
    for_update: bool = False,
) -> m.Dashboard:
    """The dashboard if the caller may see it, else ``404`` (plan D5: the one decision).

    A client needs a published dashboard granted to them; a draft needs the editor role."""
    sc = ctx.scope
    stmt = select(m.Dashboard).where(
        m.Dashboard.id == dashboard_id,
        m.Dashboard.tenant_id == sc.tenant_id,
        m.Dashboard.project_id == sc.project_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("dashboard", dashboard_id)
    auth = ctx.request
    if caller_is_client(auth):
        assert auth is not None
        if row.status != "published" or row.published_revision_id is None:
            raise NotFoundError("dashboard", dashboard_id)
        granted = await session.get(m.DashboardGrant, (row.id, auth.user_id or ""))
        if granted is None:
            raise NotFoundError("dashboard", dashboard_id)
        await require_shareable(session, dashboard=row)
    elif row.status == "draft" and not (
        auth is None or auth.allows(Action.UPDATE, ResourceKind.DASHBOARD)
    ):
        raise NotFoundError("dashboard", dashboard_id)
    return row


async def load_revision(session: AsyncSession, row: m.Dashboard) -> m.DashboardRevision | None:
    """The published revision of a dashboard (``None`` while it was never published)."""
    if row.published_revision_id is None:
        return None
    rev = await session.get(m.DashboardRevision, row.published_revision_id)
    if rev is None or rev.dashboard_id != row.id:  # pragma: no cover - written together
        return None
    return rev


def any_spec(stored: Mapping[str, Any]) -> AnySpec:
    """A stored spec (a draft or a revision) of either layout."""
    return parse_dashboard_spec(stored)


def any_revision_spec(rev: m.DashboardRevision) -> AnySpec:
    """The spec of a revision, of either layout."""
    return parse_dashboard_spec(rev.spec)


def revision_spec(rev: m.DashboardRevision) -> DashboardSpec:
    """The layout 1 spec of a revision (the media planner inputs of v0.16).
    ``DASHBOARD_INPUT_DISABLED`` for a layout 2 revision: use :func:`any_revision_spec`."""
    spec = parse_dashboard_spec(rev.spec)
    if not isinstance(spec, DashboardSpec):
        raise MapiError(
            "this input is part of the layout 1 media planner: the dashboard uses layout 2",
            code=ErrorCode.DASHBOARD_INPUT_DISABLED,
        )
    return spec


async def actuals_mode(
    session: AsyncSession, ctx: ServerContext, model_version_id: str
) -> ActualsMode:
    """``conditioned`` when the pinned version's plugin build conditions forecasts on actuals
    (statsmodels), else ``compared`` (every MMM: the model's prediction is compared with
    the numbers; the model does not change)."""
    mv = await session.get(m.ModelVersion, model_version_id)
    if mv is None or mv.score_meta is None:
        return "compared"
    try:
        snapshot = await live_capabilities_snapshot(session, ctx.catalogs, mv)
    except (MapiError, ValidationError):
        return "compared"
    return "conditioned" if snapshot.conditions_on_actuals else "compared"


_ACCURACY_KIND: Final[dict[str, Literal["error", "classification", "effect"]]] = {
    "classifier": "classification",
    "causal_impact": "effect",
}


def effective_inputs(
    spec: AnySpec,
    mode: ActualsMode | None,
    allowed: Iterable[str] | None = None,
    *,
    client: bool = False,
) -> EffectiveInputs:
    """The published spec's inputs, narrowed by a share link's ``allowed_inputs`` (``None``:
    a signed-in caller, no narrowing). Layout 2 adds scenario rows and batch scoring (and
    the accuracy an upload gives); batch scoring of an image model is for members only
    (``client``: a client user; a share link is never a member)."""
    narrow = None if allowed is None else set(allowed)

    def on(key: str, enabled: bool) -> bool:
        return enabled and (narrow is None or key in narrow)

    if isinstance(spec, DashboardSpec):
        whatif = on("whatif", spec.whatif.enabled)
        actuals = on("actuals", spec.actuals.enabled)
        return EffectiveInputs(
            whatif=whatif,
            exact=whatif and spec.whatif.exact,
            actuals=actuals,
            actuals_mode=(mode or "compared") if actuals else None,
            refit_requests=actuals and spec.actuals.allow_refit_requests,
        )
    planner = spec.mmm if spec.preset == "mmm_planner" else None
    whatif = planner is not None and on("whatif", planner.whatif.enabled)
    actuals = on("actuals", spec.actuals.enabled)
    member = narrow is None and not client
    batch = spec.batch is not None and on("batch", spec.batch.enabled)
    if spec.preset == "image" and not member:
        batch = False
    return EffectiveInputs(
        whatif=whatif,
        exact=whatif and planner is not None and planner.whatif.exact,
        actuals=actuals,
        actuals_mode=(mode or "compared") if actuals else None,
        refit_requests=actuals and spec.actuals.allow_refit_requests,
        scenario_rows=on("scenario_rows", spec.scenario_rows.enabled),
        batch=batch,
        accuracy_kind=_ACCURACY_KIND.get(spec.preset, "error") if actuals else None,
    )


async def member_context(
    ctx: ServerContext,
    auth: RequestAuth,
    dashboard_id: str,
    *,
    session: AsyncSession | None = None,
) -> DashboardContext:
    """The :class:`DashboardContext` of a signed-in caller (member or client) on a published
    dashboard: ``404`` when they may not see it, ``DASHBOARD_NOT_PUBLISHED`` (409) for an
    analyst on a dashboard that has no published revision yet."""
    rctx = ctx if ctx.request is auth else ctx.for_request(auth)
    if session is None:
        if auth.tenant_id is None:
            raise unauthenticated()
        async with ctx.db.tenant_session(auth.tenant_id) as own:
            return await member_context(rctx, auth, dashboard_id, session=own)
    row = await load_dashboard(session, rctx, dashboard_id)
    rev = await load_revision(session, row)
    if rev is None:
        raise MapiError(
            "the dashboard is not published yet: publish it first",
            code=ErrorCode.DASHBOARD_NOT_PUBLISHED,
        )
    spec = any_revision_spec(rev)
    mode = await actuals_mode(session, rctx, rev.model_version_id) if spec.actuals.enabled else None
    client = caller_is_client(auth)
    return DashboardContext(
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        dashboard_id=row.id,
        revision_id=rev.id,
        principal_kind="client" if client else "member",
        principal_id=auth.user_id or auth.principal_id or "",
        inputs=effective_inputs(spec, mode, client=client),
        is_analyst=not client and _is_analyst(auth),
    )


# ================================================================================== readouts


def _period(row: m.DashboardReadout) -> tuple[date, date] | None:
    if row.period_start is None or row.period_end is None:
        return None
    return (row.period_start, row.period_end)


def is_mine(row: m.DashboardReadout, dctx: DashboardContext) -> bool:
    """Whether ``dctx``'s caller made the readout (the server tools' ``_own`` rule)."""
    return row.actor_kind == dctx.principal_kind and row.actor_id == dctx.principal_id


def readout_header(
    row: m.DashboardReadout, refit_status: str | None = None, *, mine: bool | None = None
) -> ReadoutHeader:
    return ReadoutHeader.model_validate(
        {
            "seq": row.seq,
            "kind": row.kind,
            "trigger": row.trigger,
            "visibility": row.visibility,
            "status": row.status,
            "title": row.title,
            "period": _period(row),
            "model_version_id": row.model_version_id,
            "model_ref": row.model_ref,
            "endpoint_revision": row.endpoint_revision,
            "created_at": row.created_at,
            "ready_at": row.ready_at,
            "refit_status": refit_status,
            "mine": mine,
        }
    )


READOUT_FAILED_DETAIL: Final = (
    "This result could not be worked out. Your analyst can see why and can try again."
)
"""The only explanation a client user or a share link gets for a failed readout."""


def viewer_problem(stored: Mapping[str, Any]) -> Problem:
    """A failed readout's problem as a client user or a share link sees it: the code and a
    fixed title and detail. The run's own detail, ``untrusted_text`` (exception text from the
    publisher's model code), ``errors`` and ``instance`` stay with the analysts."""
    try:
        code = ErrorCode(str(stored.get("code")))
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    return Problem.from_code(code, READOUT_FAILED_DETAIL)


def readout_dto(
    row: m.DashboardReadout,
    refit_status: str | None = None,
    *,
    analyst: bool = False,
    mine: bool | None = None,
) -> Readout:
    """A readout with its snapshot (only once ``ready``) or its problem (``failed``): the full
    problem for an ``analyst``, else :func:`viewer_problem`."""
    head = readout_header(row, refit_status, mine=mine)
    snapshot = (
        ReadoutSnapshot.model_validate(row.snapshot)
        if row.status == "ready" and row.snapshot is not None
        else None
    )
    problem = None
    if row.problem:
        problem = Problem.model_validate(row.problem) if analyst else viewer_problem(row.problem)
    return Readout(**head.model_dump(), snapshot=snapshot, problem=problem)


async def refit_statuses(
    session: AsyncSession, dashboard_id: str, seqs: Sequence[int]
) -> dict[int, str]:
    """The refit-request status of each of these readouts that has one (newest request)."""
    if not seqs:
        return {}
    rows = await session.execute(
        select(m.DashboardRefitRequest.readout_seq, m.DashboardRefitRequest.status)
        .where(
            m.DashboardRefitRequest.dashboard_id == dashboard_id,
            m.DashboardRefitRequest.readout_seq.in_(list(seqs)),
        )
        .order_by(m.DashboardRefitRequest.created_at)
    )
    return {int(seq): str(status) for seq, status in rows}


OVERVIEW_KINDS: Final = ("baseline", "refresh", "scenario", "actuals")
"""The readout kinds that lead a client's Overview and give the "readout before" of a KPI
(v0.17 R6: a published kept case led it, one case compared with the whole baseline)."""


async def latest_readout(
    session: AsyncSession, dashboard_id: str, *, published_only: bool
) -> m.DashboardReadout | None:
    """The newest readout: for clients and share links the newest ready, published one of
    the dashboard's own kinds (:data:`OVERVIEW_KINDS`: a kept case or a scored file the
    editor published stays in the history, never the Overview); for analysts the newest of
    any state."""
    stmt = select(m.DashboardReadout).where(m.DashboardReadout.dashboard_id == dashboard_id)
    if published_only:
        stmt = stmt.where(
            m.DashboardReadout.visibility == "published",
            m.DashboardReadout.status == "ready",
            m.DashboardReadout.kind.in_(OVERVIEW_KINDS),
        )
    return (
        await session.execute(stmt.order_by(m.DashboardReadout.seq.desc()).limit(1))
    ).scalar_one_or_none()


# ================================================================================ dashboards


def revision_dto(rev: m.DashboardRevision) -> DashboardRevision:
    return DashboardRevision(
        id=rev.id,
        seq=rev.seq,
        spec=any_revision_spec(rev),
        spec_hash=rev.spec_hash,
        model_version_id=rev.model_version_id,
        model_ref=rev.model_ref,
        endpoint_alias=rev.endpoint_alias,
        endpoint_revision=rev.endpoint_revision,
        created_by=rev.created_by,
        created_at=rev.created_at,
    )


async def dashboard_dto(
    session: AsyncSession, row: m.Dashboard, *, upgrade_issues: Sequence[Issue] = ()
) -> Dashboard:
    """The analyst's view (viewers and up). ``upgrade_issues``: what stopped the automatic
    layout 2 upgrade (``problem == "upgrade_needs_review"``)."""
    rev = await load_revision(session, row)
    latest = await latest_readout(session, row.id, published_only=False)
    head = None
    if latest is not None:
        statuses = await refit_statuses(session, row.id, [latest.seq])
        head = readout_header(latest, statuses.get(latest.seq))
    return Dashboard(
        id=row.id,
        name=row.name,
        status=row.status,
        draft_spec=any_spec(row.draft_spec),
        draft_etag=row.draft_etag,
        published_revision=revision_dto(rev) if rev is not None else None,
        run_as=row.run_as,
        live_inputs=row.live_inputs,
        problem=row.problem,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        latest_readout=head,
        upgrade_issues=list(upgrade_issues)[:50],
    )


def summary_dto(row: m.Dashboard, spec: AnySpec, latest: ReadoutHeader | None) -> DashboardSummary:
    """A list row: ``spec`` is the published one for clients, the draft for analysts."""
    return DashboardSummary(
        id=row.id,
        name=row.name,
        title=spec.title,
        client_name=spec.client_name,
        status=row.status,
        period_label=spec.period_label,
        live_inputs=row.live_inputs,
        latest_readout=latest,
        updated_at=row.updated_at,
    )


def _tabs(tabs: Sequence[TabKey], inputs: EffectiveInputs) -> list[TabKey]:
    """The tabs the viewer can use: no scenarios without what-ifs, no upload without actuals."""
    hidden: set[TabKey] = set()
    if not inputs.whatif:
        hidden.add("scenarios")
    if not inputs.actuals:
        hidden.add("update")
    return [t for t in tabs if t not in hidden]


def _sections(inputs: EffectiveInputs) -> list[TabKeyV2]:
    """The layout 2 readout sections the viewer can use (each input only when allowed)."""
    out: list[TabKeyV2] = ["overview"]
    if inputs.scenario_rows:
        out.append("try")
    if inputs.actuals:
        out.append("accuracy")
    if inputs.batch:
        out.append("batch")
    out.append("history")
    return out


_PLANNER_TABS: Final[tuple[TabKey, ...]] = (
    "this_period",
    "trends",
    "scenarios",
    "update",
    "history",
)


async def upgrade_note(
    session: AsyncSession, rev: m.DashboardRevision, spec: DashboardSpecV2
) -> str | None:
    """The one-time note of a layout 2 revision that follows a layout 1 one (the automatic
    upgrade, or an editor's ``:upgrade``): the numbers come from the same model (and, when
    outcome panels show predictive ranges, that the ranges are wider)."""
    if rev.seq <= 1:
        return None
    prev = (
        await session.execute(
            select(m.DashboardRevision.spec).where(
                m.DashboardRevision.dashboard_id == rev.dashboard_id,
                m.DashboardRevision.seq == rev.seq - 1,
            )
        )
    ).scalar_one_or_none()
    if not isinstance(prev, dict) or prev.get("layout_version", 1) == 2:
        return None
    if any(getattr(p, "band", None) == "outcome" for p in spec.panels):
        return f"{UPGRADE_NOTE} {UPGRADE_NOTE_RANGES}"[:400]
    return UPGRADE_NOTE


async def client_dashboard(
    session: AsyncSession,
    row: m.Dashboard,
    rev: m.DashboardRevision,
    inputs: EffectiveInputs,
    *,
    ends_at: datetime | None = None,
) -> ClientDashboard:
    """What a client user or a share link sees (the published revision only, no ids of
    people). ``latest_readout`` is the newest ready, published readout. A layout 2 revision
    adds its preset, subject, panels (titles and kinds only), the forms of the inputs the
    viewer may use, the sections and the one-time upgrade note."""
    spec = any_revision_spec(rev)
    latest = await latest_readout(session, row.id, published_only=True)
    readout = None
    if latest is not None:
        statuses = await refit_statuses(session, row.id, [latest.seq])
        readout = readout_dto(latest, statuses.get(latest.seq))
    if isinstance(spec, DashboardSpec):
        return ClientDashboard(
            id=row.id,
            title=spec.title,
            client_name=spec.client_name,
            summary=spec.summary,
            note=spec.note,
            period_label=spec.period_label,
            kpi=spec.kpi,
            channels=list(spec.channels),
            tiles=list(spec.tiles),
            tabs=_tabs(spec.tabs, inputs),
            inputs=inputs,
            live_inputs=row.live_inputs,
            show_technical=spec.show_technical,
            latest_readout=readout,
            ends_at=ends_at,
        )
    planner = spec.mmm if spec.preset == "mmm_planner" else None
    rows = spec.scenario_rows
    scenario_form = (
        ScenarioForm(
            max_rows=rows.max_rows, outputs=list(rows.outputs), inputs=list(rows.inputs),
            horizon=rows.horizon,
        )
        if inputs.scenario_rows
        else None
    )  # fmt: skip
    batch_form = None
    if inputs.batch and spec.batch is not None:
        drivers = [i.name for i in rows.inputs if i.name != spec.batch.id_column]
        batch_form = BatchForm(
            max_rows=spec.batch.max_rows,
            id_column=spec.batch.id_column,
            columns=[spec.batch.id_column, *drivers][:64],
            outputs=list(spec.batch.outputs),
        )
    return ClientDashboard(
        id=row.id,
        title=spec.title,
        client_name=spec.client_name,
        summary=spec.summary,
        note=spec.note,
        period_label=spec.period_label,
        kpi=kpi_of_subject(spec.subject),
        channels=list(planner.channels) if planner is not None else [],
        tiles=[t for t in planner.tiles if t != "panel"] if planner is not None else [],
        tabs=_tabs(_PLANNER_TABS, inputs) if planner is not None else [],
        inputs=inputs,
        live_inputs=row.live_inputs,
        show_technical=spec.show_technical,
        latest_readout=readout,
        ends_at=ends_at,
        layout_version=2,
        preset=spec.preset,
        subject=spec.subject,
        panels_meta=[panel_meta(p) for p in spec.panels],
        scenario_form=scenario_form,
        batch_form=batch_form,
        sections=_sections(inputs),
        upgrade_note=await upgrade_note(session, rev, spec),
    )


# ============================================================================= grants, shares


def grant_dto(row: m.DashboardGrant, member: m.Membership | None) -> DashboardGrant:
    return DashboardGrant(
        user_id=row.user_id,
        email=member.email if member is not None else None,
        display_name=member.display_name if member is not None else None,
        granted_by=row.granted_by,
        created_at=row.created_at,
    )


def share_status(row: m.DashboardShare, at: datetime) -> Literal["active", "expired", "revoked"]:
    if row.revoked_at is not None:
        return "revoked"
    return "expired" if row.expires_at <= at else "active"


def share_dto(row: m.DashboardShare, at: datetime | None = None) -> DashboardShare:
    ts = at or now()
    locked = row.locked_until if row.locked_until is not None and row.locked_until > ts else None
    return DashboardShare(
        id=row.id,
        label=row.label,
        display=row.display,
        status=share_status(row, ts),
        has_passcode=row.passcode_hash is not None,
        allowed_inputs=sorted(row.allowed_inputs or []),
        views=row.views,
        last_viewed_at=row.last_viewed_at,
        locked_until=locked,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_by=row.created_by,
        created_at=row.created_at,
    )
