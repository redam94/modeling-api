"""Continuous readouts of a dashboard (v0.16, plan D10-D12, §3.5).

A readout is an append-only row (``seq`` per dashboard) holding a capped JSON snapshot
(:class:`~mapi_spec.dashboards.ReadoutSnapshot`). Its life:

1. :func:`new_readout` inserts it ``queued`` (the dashboard loop submits its run as the
   publisher), ``running`` (submitted in the request), ``materialising`` (its run already
   finished: a workflow's score node) or ``ready`` (a grid scenario: no run at all);
2. :func:`run_terminal` - called by :func:`mapi_server.services.runs.apply_transition` in the
   run-terminal transaction, also when the project has no webhooks - only flips the rows the
   run's labels name to ``materialising`` (succeeded) or ``failed``: no storage reads there;
3. :func:`materialise` (the dashboard loop) builds the snapshot from the score run's
   ``summary.json``, the pinned version's fit metrics (``roi``, ``roi_p05``, ``roi_p95``,
   ``mroi``, ``contribution_share`` per channel) and its response grid, caps it and sets
   ``ready``. Ready rows never change again (only their visibility, by review).

Refreshes come from a linked workflow's successful run (:func:`on_workflow_succeeded`: the
run of its ``model.score`` / ``model.forecast`` node, no new run) and from an endpoint
promotion (:func:`on_endpoint_promoted`: a new revision when the dashboard follows the
endpoint, else a ``new_version_available`` flag for the analyst).

Visibility (D11): ``published`` rows are the history; ``private`` rows (saved scenarios)
and ``pending_review`` rows (actuals waiting for an editor) are visible to their actor and
to the analysts; ``hidden`` and ``failed`` rows only to the analysts.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

from mapi_spec.api import (
    SCORE_SUMMARY_MAX_BYTES,
    SCORE_SUMMARY_OUTPUT,
    ScoreSummary,
    ScoreSummaryTable,
)
from mapi_spec.audit import AuditAction
from mapi_spec.dashboard_v2 import (
    DashboardSpecV2,
    EffectAccuracy,
    RecipePanel,
    parse_dashboard_spec,
)
from mapi_spec.dashboards import (
    MAX_CHANNELS,
    MAX_SERIES_POINTS,
    ActualsAccuracy,
    ChannelReadout,
    DashboardSpec,
    KpiValue,
    ReadoutKind,
    ReadoutList,
    ReadoutSnapshot,
    ReadoutTrigger,
    ReadoutVisibility,
    Series,
    SeriesPoint,
    TileKey,
    TileUnit,
    WhatIfResult,
    breakeven_of,
    dashboard_spec_hash,
    metrics_of,
    zone_of,
)
from mapi_spec.errors import ERROR_INFO, ErrorCode, MapiError, Problem, Severity
from mapi_spec.run import RunState
from mapi_spec.tenancy import ResourceKind
from mapi_spec.views import ResponseGridView
from mapi_spec.webhooks import WebhookEventType
from mapi_spec.whatif import WhatIfScenario, evaluate_scenario
from mapi_spec.xrjson import MAX_XRJSON_BYTES, XrJsonDataset
from pydantic import JsonValue, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.jsonlimits import loads_limited
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.access import (
    DashboardContext,
    readout_dto,
    readout_header,
    refit_statuses,
)
from mapi_server.services.runs import now
from mapi_server.services.tracking import lttb
from mapi_server.services.webhooks.outbox import emit_event

__all__ = [
    "LABEL_DASHBOARD",
    "LABEL_READOUT",
    "LABEL_SHARE",
    "LABEL_TRIGGER",
    "Actor",
    "ChannelFacts",
    "build_snapshot",
    "get_readout",
    "kpi_series",
    "list_readouts",
    "materialise",
    "new_readout",
    "on_endpoint_promoted",
    "on_workflow_succeeded",
    "publish_ready_event",
    "readout_labels",
    "review",
    "run_terminal",
    "visible_to",
    "whatif_result",
]

log = logging.getLogger(__name__)

LABEL_DASHBOARD: Final = "mapi.dashboard"
LABEL_READOUT: Final = "mapi.readout"
LABEL_TRIGGER: Final = "mapi.trigger"
LABEL_SHARE: Final = "mapi.share"
_SCORE_NODES: Final = frozenset({"model.score", "model.forecast"})
_PENDING: Final = ("queued", "running")


@dataclass(frozen=True, slots=True)
class Actor:
    """Who asked for a readout: a member, a client user, a share link or the platform."""

    kind: Literal["member", "client", "share", "system"]
    id: str | None = None

    @property
    def share_id(self) -> str | None:
        return self.id if self.kind == "share" else None

    @classmethod
    def of(cls, dctx: DashboardContext) -> Actor:
        return cls(dctx.principal_kind, dctx.principal_id)


SYSTEM: Final = Actor("system")


def readout_labels(
    dashboard_id: str, seq: int, trigger: ReadoutTrigger, share_id: str | None = None
) -> dict[str, str]:
    """The labels of a readout's run (``run_terminal`` finds the row by them)."""
    labels = {LABEL_DASHBOARD: dashboard_id, LABEL_READOUT: str(seq), LABEL_TRIGGER: trigger}
    if share_id is not None:
        labels[LABEL_SHARE] = share_id
    return labels


# =================================================================================== writing


async def _lock_dashboard(session: AsyncSession, dashboard_id: str) -> m.Dashboard:
    row = (
        await session.execute(
            select(m.Dashboard).where(m.Dashboard.id == dashboard_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("dashboard", dashboard_id)
    return row


async def new_readout(
    session: AsyncSession,
    dashboard: m.Dashboard,
    revision: m.DashboardRevision,
    *,
    kind: ReadoutKind,
    trigger: ReadoutTrigger,
    visibility: ReadoutVisibility,
    actor: Actor,
    title: str,
    status: Literal["queued", "running", "materialising", "ready"] = "queued",
    model_version_id: str | None = None,
    model_ref: str | None = None,
    run_ids: Sequence[str] = (),
    scenario: Mapping[str, float] | None = None,
    cache_key: str | None = None,
    input_ref: Mapping[str, Any] | None = None,
    input_fingerprint: str | None = None,
    snapshot: ReadoutSnapshot | None = None,
    workflow_run_id: str | None = None,
    expires_at: datetime | None = None,
) -> m.DashboardReadout:
    """Append a readout. The next ``seq`` is taken under ``SELECT … FOR UPDATE`` on the
    dashboard row, so concurrent writers never pick the same one, and from the dashboard's
    ``next_readout_seq`` counter (never lowered), so a pruned readout's seq never comes back.
    """
    locked = await _lock_dashboard(session, dashboard.id)
    top = await session.execute(
        select(func.coalesce(func.max(m.DashboardReadout.seq), 0)).where(
            m.DashboardReadout.dashboard_id == dashboard.id
        )
    )
    seq = max(int(top.scalar_one()) + 1, locked.next_readout_seq or 1)
    locked.next_readout_seq = seq + 1
    ts = now()
    row = m.DashboardReadout(
        tenant_id=dashboard.tenant_id,
        project_id=dashboard.project_id,
        dashboard_id=dashboard.id,
        seq=seq,
        revision_id=revision.id,
        kind=kind,
        trigger=trigger,
        visibility=visibility,
        status=status,
        title=title[:200],
        actor_kind=actor.kind,
        actor_id=actor.id,
        share_id=actor.share_id,
        model_version_id=model_version_id or revision.model_version_id,
        model_ref=model_ref or revision.model_ref,
        endpoint_revision=revision.endpoint_revision,
        spec_hash=revision.spec_hash,
        run_ids=list(run_ids),
        workflow_run_id=workflow_run_id,
        input_ref=dict(input_ref) if input_ref is not None else None,
        input_fingerprint=input_fingerprint,
        scenario=dict(scenario) if scenario is not None else None,
        cache_key=cache_key,
        snapshot=snapshot_json(snapshot) if snapshot is not None else None,
        expires_at=expires_at,
        created_at=ts,
        ready_at=ts if status == "ready" else None,
    )
    session.add(row)
    await session.flush()
    if status == "ready" and visibility == "published":
        await publish_ready_event(session, row)
    return row


async def publish_ready_event(session: AsyncSession, row: m.DashboardReadout) -> None:
    """``dashboard.readout.published`` (outbox, in this transaction): a ready readout entered
    the history."""
    await emit_event(
        session,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        event_type=WebhookEventType.DASHBOARD_READOUT_PUBLISHED,
        data={
            "dashboard_id": row.dashboard_id,
            "seq": row.seq,
            "kind": row.kind,
            "trigger": row.trigger,
            "model_version_id": row.model_version_id,
            "model_ref": row.model_ref,
        },
    )


async def run_terminal(session: AsyncSession, run: m.Run, state: RunState) -> None:
    """A run became terminal (in :func:`~mapi_server.services.runs.apply_transition`'s
    transaction): the readout its labels name goes to ``materialising`` (succeeded) or
    ``failed``. Only rows that list the run and still wait for it change; no storage read."""
    labels = run.labels or {}
    dashboard_id = labels.get(LABEL_DASHBOARD)
    seq_text = labels.get(LABEL_READOUT)
    if not dashboard_id or not seq_text or not str(seq_text).isdigit():
        return
    row = (
        await session.execute(
            select(m.DashboardReadout)
            .where(
                m.DashboardReadout.dashboard_id == str(dashboard_id),
                m.DashboardReadout.seq == int(seq_text),
                m.DashboardReadout.tenant_id == run.tenant_id,
                m.DashboardReadout.status.in_(_PENDING),
                m.DashboardReadout.run_ids.contains([run.id]),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return
    if state is RunState.SUCCEEDED:
        row.status = "materialising"
    else:
        row.status = "failed"
        row.problem = run.problem or Problem.from_code(
            ErrorCode.INTERNAL_ERROR, f"the run ended {state.value}"
        ).model_dump(mode="json", exclude_none=True)
    await session.flush()


# ================================================================================ visibility


def visible_to(dctx: DashboardContext) -> Any:
    """The SQL condition of the readouts ``dctx``'s caller may see (plan D11)."""
    if dctx.is_analyst:
        return m.DashboardReadout.dashboard_id == dctx.dashboard_id
    own = and_(
        m.DashboardReadout.visibility.in_(("private", "pending_review", "published")),
        m.DashboardReadout.actor_kind == dctx.principal_kind,
        m.DashboardReadout.actor_id == dctx.principal_id,
    )
    history = and_(
        m.DashboardReadout.visibility == "published", m.DashboardReadout.status != "failed"
    )
    return and_(m.DashboardReadout.dashboard_id == dctx.dashboard_id, or_(history, own))


async def list_readouts(
    session: AsyncSession,
    dctx: DashboardContext,
    *,
    kind: ReadoutKind | None = None,
    visibility: ReadoutVisibility | None = None,
    before_seq: int | None = None,
    limit: int = 20,
) -> ReadoutList:
    """Readouts newest first that the caller may see (``next_before_seq`` pages back)."""
    limit = max(1, min(limit, 50))
    stmt = select(m.DashboardReadout).where(visible_to(dctx))
    if kind is not None:
        stmt = stmt.where(m.DashboardReadout.kind == kind)
    if visibility is not None:
        stmt = stmt.where(m.DashboardReadout.visibility == visibility)
    if before_seq is not None:
        stmt = stmt.where(m.DashboardReadout.seq < before_seq)
    rows = list(
        (
            await session.execute(stmt.order_by(m.DashboardReadout.seq.desc()).limit(limit + 1))
        ).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    statuses = await refit_statuses(session, dctx.dashboard_id, [r.seq for r in rows])
    return ReadoutList(
        items=[readout_header(r, statuses.get(r.seq)) for r in rows],
        next_before_seq=rows[-1].seq if more and rows else None,
    )


async def load_visible(
    session: AsyncSession, dctx: DashboardContext, seq: int
) -> m.DashboardReadout:
    row = (
        await session.execute(
            select(m.DashboardReadout).where(visible_to(dctx), m.DashboardReadout.seq == seq)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("readout", str(seq))
    return row


async def get_readout(session: AsyncSession, dctx: DashboardContext, seq: int) -> Any:
    """One readout (``404`` when it does not exist or the caller may not see it)."""
    row = await load_visible(session, dctx, seq)
    statuses = await refit_statuses(session, dctx.dashboard_id, [row.seq])
    return readout_dto(row, statuses.get(row.seq), analyst=dctx.is_analyst)


async def review(
    session: AsyncSession,
    ctx: ServerContext,
    dashboard: m.Dashboard,
    seq: int,
    to: Literal["published", "hidden"],
) -> m.DashboardReadout:
    """``…/readouts/{seq}:publish`` / ``:hide`` (editors). Publishing takes a readout that
    waits for review (or was hidden) into the history; hiding removes one from the clients'
    view (the row stays for audit). Saved scenarios are private working state: they are
    neither (``CONFLICT``). Idempotent."""
    await _lock_dashboard(session, dashboard.id)
    row = (
        await session.execute(
            select(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dashboard.id, m.DashboardReadout.seq == seq)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("readout", str(seq))
    if row.visibility == to:
        return row
    if row.kind == "scenario" or row.visibility == "private":
        raise Conflict("a saved scenario is private: it cannot enter the history")
    if to == "published" and row.status == "failed":
        raise Conflict("a failed readout cannot be published")
    before = row.visibility
    row.visibility = to
    row.expires_at = None
    await session.flush()
    if to == "published" and row.status == "ready":
        await publish_ready_event(session, row)
    if ctx.request is not None:
        await write_audit(
            ctx.db,
            [
                ctx.request.audit(
                    ctx,
                    AuditAction.DASHBOARD_READOUT_REVIEW,
                    ResourceKind.DASHBOARD,
                    resource_id=dashboard.id,
                    details={"seq": seq, "from": before, "to": to},
                )
            ],
            session=session,
        )
    return row


# ================================================================================== refreshes


def _period_title(spec: DashboardSpec | DashboardSpecV2, prefix: str) -> str:
    return f"{prefix} on {now():%d/%m/%Y}"


async def _published_with_revision(
    session: AsyncSession, tenant_id: str, project_id: str
) -> list[tuple[m.Dashboard, m.DashboardRevision]]:
    rows = await session.execute(
        select(m.Dashboard, m.DashboardRevision)
        .join(m.DashboardRevision, m.DashboardRevision.id == m.Dashboard.published_revision_id)
        .where(
            m.Dashboard.tenant_id == tenant_id,
            m.Dashboard.project_id == project_id,
            m.Dashboard.status == "published",
        )
    )
    return [(d, r) for d, r in rows.all()]


WORKFLOW_VERSION_REFUSED: Final = "workflow_version_refused"
"""``dashboards.problem`` when a linked workflow scored a version the dashboard may not
show (it ran uploaded code, or it is not a version of the dashboard's model)."""


async def _refused_workflow_version(
    session: AsyncSession, rev: m.DashboardRevision, mv: m.ModelVersion
) -> str | None:
    """Why a linked workflow's score of ``mv`` (not the pinned version) may not reach the
    clients: the checks publishing and endpoint-follow make (no uploaded code), and ``mv``
    must be a version of the pinned version's model (a runner may point the workflow's model
    input at any ready version, e.g. one fitted on another client's data)."""
    from mapi_server.services.sharing import ran_uploaded_code

    if ran_uploaded_code(mv):
        return "it ran uploaded plugin code"
    if mv.status != "ready":
        return "it is not ready"
    pinned = await session.get(m.ModelVersion, rev.model_version_id)
    if pinned is None or pinned.model_id != mv.model_id:
        return "it is not a version of the dashboard's model"
    return None


async def on_workflow_succeeded(session: AsyncSession, wr: m.WorkflowRunRow) -> int:
    """A workflow run succeeded (beside its ``workflow.run.succeeded`` event): every
    published dashboard that links the workflow (``refresh.workflow_id``), or whose approved
    refit request started this run, gets a ``refresh`` readout built from the run of the
    workflow's ``model.score`` / ``model.forecast`` node - no new run. Returns how many."""
    linked = []
    refit_dashboards = set(
        (
            await session.execute(
                select(m.DashboardRefitRequest.dashboard_id).where(
                    m.DashboardRefitRequest.tenant_id == wr.tenant_id,
                    m.DashboardRefitRequest.workflow_run_id == wr.run_id,
                )
            )
        ).scalars()
    )
    for dash, rev in await _published_with_revision(session, wr.tenant_id, wr.project_id):
        refresh = (rev.spec.get("refresh") or {}).get("workflow_id")
        if refresh == wr.workflow_id or dash.id in refit_dashboards:
            linked.append((dash, rev))
    if not linked:
        return 0
    nodes = list(
        (
            await session.execute(
                select(m.WorkflowNodeRunRow)
                .where(m.WorkflowNodeRunRow.run_id == wr.run_id)
                .order_by(m.WorkflowNodeRunRow.position)
            )
        ).scalars()
    )
    child: m.Run | None = None
    for node in nodes:
        if node.use.split("@", 1)[0] not in _SCORE_NODES:
            continue
        run_id = node.child_run_id or node.cached_from
        run = await session.get(m.Run, run_id) if run_id else None
        if run is not None and run.state == RunState.SUCCEEDED.value:
            child = run
    if child is None:
        log.info("workflow run %s has no succeeded score node: no dashboard refresh", wr.run_id)
        return 0
    made = 0
    for dash, rev in linked:
        seen = (
            await session.execute(
                select(m.DashboardReadout.seq).where(
                    m.DashboardReadout.dashboard_id == dash.id,
                    m.DashboardReadout.workflow_run_id == wr.run_id,
                )
            )
        ).first()
        if seen is not None:
            continue  # a workflow run refreshes a dashboard once
        mv_id = child.model_version_id or rev.model_version_id
        mv = await session.get(m.ModelVersion, mv_id)
        ref = rev.model_ref
        if mv is not None and mv.id != rev.model_version_id:
            why = await _refused_workflow_version(session, rev, mv)
            if why is not None:
                log.warning(
                    "workflow run %s scored model version %s: not published to dashboard %s "
                    "(%s)", wr.run_id, mv.id, dash.id, why,
                )  # fmt: skip
                locked = await _lock_dashboard(session, dash.id)
                locked.problem = WORKFLOW_VERSION_REFUSED
                locked.updated_at = now()
                continue
            from mapi_server.services.endpoints import model_ref_of

            ref = await model_ref_of(session, mv)
        spec = parse_dashboard_spec(rev.spec)
        await new_readout(
            session,
            dash,
            rev,
            kind="refresh",
            trigger="workflow",
            visibility="published",
            actor=SYSTEM,
            title=_period_title(spec, "Updated"),
            status="materialising",
            model_version_id=mv_id,
            model_ref=ref,
            run_ids=[child.id],
            workflow_run_id=wr.run_id,
        )
        made += 1
    return made


async def _recent_refresh(
    session: AsyncSession, dashboard_id: str, debounce_s: int
) -> m.DashboardReadout | None:
    """The newest manual or endpoint refresh of the debounce window that did not fail."""
    since = now() - timedelta(seconds=max(0, debounce_s))
    return (
        await session.execute(
            select(m.DashboardReadout)
            .where(
                m.DashboardReadout.dashboard_id == dashboard_id,
                m.DashboardReadout.kind == "refresh",
                m.DashboardReadout.trigger.in_(("manual", "endpoint")),
                m.DashboardReadout.created_at >= since,
                m.DashboardReadout.status != "failed",
            )
            .order_by(m.DashboardReadout.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _retarget(row: m.DashboardReadout, rev: m.DashboardRevision, title: str) -> None:
    """A refresh still ``queued`` (no run yet) now scores ``rev``'s version instead."""
    row.revision_id = rev.id
    row.model_version_id = rev.model_version_id
    row.model_ref = rev.model_ref
    row.endpoint_revision = rev.endpoint_revision
    row.spec_hash = rev.spec_hash
    row.title = title[:200]


NEW_VERSION_INCOMPATIBLE: Final = "new_version_incompatible"
"""``dashboards.problem`` when the endpoint now serves a version the dashboard's spec does
not fit (channels missing from its grid, no KPI variable, no input contract, not ready)."""


async def on_endpoint_promoted(
    session: AsyncSession,
    endpoint: m.Endpoint,
    version: m.ModelVersion,
    *,
    debounce_s: int = 60,
    ctx: ServerContext | None = None,
) -> int:
    """An endpoint now serves ``version`` (beside its ``endpoint.promoted`` event). Each
    published dashboard bound to the alias either follows it (``refresh.follow_endpoint``:
    a new revision pinned to the version and a ``refresh`` readout) or gets ``problem =
    "new_version_available"`` for the analyst. A version that ran uploaded code is never
    followed, and (with ``ctx``) neither is one the spec does not fit: the dashboard gets
    ``new_version_incompatible`` and keeps its pin. Returns how many dashboards changed.

    Debounced by coalescing: a refresh of the window still ``queued`` is retargeted to the
    new revision (so a quick rollback never publishes the rolled-back version's numbers as
    the latest), one that already runs for this very version answers for it, and otherwise
    a new refresh is queued."""
    from mapi_server.services.dashboards.crud import _check_version
    from mapi_server.services.endpoints import model_ref_of
    from mapi_server.services.sharing import ran_uploaded_code

    changed = 0
    for dash, rev in await _published_with_revision(
        session, endpoint.tenant_id, endpoint.project_id
    ):
        if rev.endpoint_alias != endpoint.alias or rev.model_version_id == version.id:
            continue
        spec = parse_dashboard_spec(rev.spec)
        locked = await _lock_dashboard(session, dash.id)
        ts = now()
        if not spec.refresh.follow_endpoint or ran_uploaded_code(version):
            locked.problem = "new_version_available"
            locked.updated_at = ts
            changed += 1
            continue
        if ctx is not None:
            issues: list[Any] = []
            if isinstance(spec, DashboardSpecV2):  # layout 2: the checks against the catalog
                from mapi_server.services.dashboards.sources import check_version_v2

                found = await check_version_v2(ctx, session, spec, version)
                issues = [i for i in found if i.severity is Severity.ERROR]
            else:
                await _check_version(ctx, spec, version, issues)
            if issues:
                log.info(
                    "dashboard %s does not follow %s to %s: %s",
                    dash.id, endpoint.alias, version.id, issues[0].message,
                )  # fmt: skip
                locked.problem = NEW_VERSION_INCOMPATIBLE
                locked.updated_at = ts
                changed += 1
                continue
        top = await session.execute(
            select(func.coalesce(func.max(m.DashboardRevision.seq), 0)).where(
                m.DashboardRevision.dashboard_id == dash.id
            )
        )
        new_rev = m.DashboardRevision(
            id=_new_id("dsr"),
            tenant_id=dash.tenant_id,
            project_id=dash.project_id,
            dashboard_id=dash.id,
            seq=int(top.scalar_one()) + 1,
            spec=spec.model_dump(mode="json"),
            spec_hash=dashboard_spec_hash(spec),
            model_version_id=version.id,
            model_ref=await model_ref_of(session, version),
            endpoint_alias=endpoint.alias,
            endpoint_revision=endpoint.revision,
            created_by=dash.run_as or "system",
            created_at=ts,
        )
        session.add(new_rev)
        await session.flush()
        locked.published_revision_id = new_rev.id
        locked.problem = None
        locked.updated_at = ts
        title = _period_title(spec, "Model updated")
        recent = await _recent_refresh(session, dash.id, debounce_s)
        if recent is not None and recent.status == "queued":
            _retarget(recent, new_rev, title)
        elif recent is None or recent.model_version_id != version.id:
            await new_readout(
                session,
                locked,
                new_rev,
                kind="refresh",
                trigger="endpoint",
                visibility="published",
                actor=SYSTEM,
                title=title,
            )
        changed += 1
    await session.flush()
    return changed


def _new_id(prefix: str) -> str:
    from mapi_spec.api import new_id

    return new_id(prefix)


# ============================================================================ snapshot (pure)


@dataclass(frozen=True, slots=True)
class ChannelFacts:
    """What is known about one channel, per readout period (spend units and KPI units).

    ``spend`` and ``contribution`` are averages per period (the grid's window totals divided
    by ``ResponseGridView.periods``), so they sit next to the KPI tiles, which describe one
    period. ``roi`` and ``mroi`` are ratios and the same either way."""

    spend: float | None = None
    contribution: float | None = None
    share: float | None = None
    roi: float | None = None
    roi_low: float | None = None
    roi_high: float | None = None
    mroi: float | None = None


@dataclass(slots=True)
class _Acc:
    mean: float | None = None
    low: float | None = None
    high: float | None = None
    actual: float | None = None
    parts: int = 0


def _f(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    x = float(v)
    return x if math.isfinite(x) else None


def _cell(row: Sequence[Any], i: int | None) -> float | None:
    """A number of a summary row (``None`` for a ragged row that has no such column)."""
    return _f(row[i]) if i is not None and i < len(row) else None


def finite_json(value: Any) -> Any:
    """``value`` (JSON-shaped) with every non-finite float as ``None``. Sums and ratios of
    finite inputs can still overflow (two cells of 1e308); JSONB refuses ``Infinity``, so a
    snapshot with one would fail its commit on every pass of the loop."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite_json(v) for v in value]
    return value


def snapshot_json(snapshot: ReadoutSnapshot) -> dict[str, Any]:
    """What ``dashboard_readouts.snapshot`` stores: the JSON dump, finite numbers only."""
    out = finite_json(snapshot.model_dump(mode="json"))
    assert isinstance(out, dict)
    return out


def _add(a: float | None, b: float | None) -> float | None:
    if b is None:
        return a
    return b if a is None else a + b


_MEAN_COLS: Final = ("mean", "median", "q50", "p50", "point")
"""``point`` (v0.17, plan §10.4): the centre of a point-prediction model, last, so it only
changes readouts that had no centre."""
_LOW_COLS: Final = ("q5", "q05", "p05", "p5", "lower")
_HIGH_COLS: Final = ("q95", "p95", "upper")


def _stat_columns(t: ScoreSummaryTable) -> tuple[int | None, int | None, int | None, list[int]]:
    """``(mean, low, high, coordinate columns)`` indexes of a summary table."""
    cols = list(t.columns)
    dims = list(t.dims) if t.dims is not None else []
    if not dims:  # older servers: coordinates are the non-statistic columns
        stats = {*_MEAN_COLS, *_LOW_COLS, *_HIGH_COLS}
        dims = [c for c in cols if c not in stats and not c.endswith(("_lower", "_upper", "_mean"))]
    coord = [cols.index(d) for d in dims if d in cols]

    def find(names: Sequence[str], suffix: str | None) -> int | None:
        for n in names:
            if n in cols:
                return cols.index(n)
        if suffix is not None:
            for i, c in enumerate(cols):
                if c.endswith(suffix) and i not in coord:
                    return i
        return None

    return find(_MEAN_COLS, None), find(_LOW_COLS, "_lower"), find(_HIGH_COLS, "_upper"), coord


def _time_col(t: ScoreSummaryTable, coord: Sequence[int]) -> int | None:
    if t.time_dim is not None and t.time_dim in t.columns:
        return t.columns.index(t.time_dim)
    for i in coord:  # the first coordinate that holds ISO dates
        cells = [r[i] for r in t.rows[:3] if i < len(r)]
        if cells and all(isinstance(c, str) and _as_date(c) is not None for c in cells):
            return i
    return coord[0] if coord else None


def _as_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _pick(tables: Sequence[ScoreSummaryTable], variable: str) -> ScoreSummaryTable | None:
    for t in tables:
        if t.variable == variable:
            return t
    return tables[0] if tables else None


def kpi_series(
    summary: ScoreSummary | None,
    variable: str,
    actuals: Mapping[str, float] | None = None,
    *,
    summed_bounds: bool = True,
) -> list[SeriesPoint]:
    """The KPI per period (summed over the other dims), oldest first: the score's mean and
    interval, and the actual where one is known (the summary's ``actuals`` tables, then
    ``actuals`` by period label).

    ``summed_bounds=False`` (layout 2): a period made of several cells keeps its summed
    centre but no interval, since the sum of interval bounds is not the interval of the sum."""
    acc: dict[str, _Acc] = {}
    table = _pick(summary.tables, variable) if summary is not None else None
    if table is not None:
        mean_i, low_i, high_i, coord = _stat_columns(table)
        time_i = _time_col(table, coord)
        for r in table.rows:
            t = str(r[time_i]) if time_i is not None and time_i < len(r) else "all"
            a = acc.setdefault(t, _Acc())
            a.parts += 1
            a.mean = _add(a.mean, _cell(r, mean_i))
            a.low = _add(a.low, _cell(r, low_i))
            a.high = _add(a.high, _cell(r, high_i))
    if summary is not None and summary.actuals:
        at = _pick(summary.actuals, variable)
        if at is not None and "actual" in at.columns:
            ai = at.columns.index("actual")
            _, _, _, coord = _stat_columns(at)
            ti = _time_col(at, [i for i in coord if i != ai])
            for r in at.rows:
                t = str(r[ti]) if ti is not None and ti < len(r) else "all"
                a = acc.setdefault(t, _Acc())
                a.actual = _add(a.actual, _cell(r, ai))
    for t, v in (actuals or {}).items():
        a = acc.setdefault(str(t), _Acc())
        if a.actual is None:
            a.actual = v
    return [
        SeriesPoint(
            t=t[:32],
            mean=a.mean,
            low=a.low if summed_bounds or a.parts <= 1 else None,
            high=a.high if summed_bounds or a.parts <= 1 else None,
            actual=a.actual,
        )
        for t, a in sorted(acc.items())
    ]


def _downsample(points: list[SeriesPoint], n: int) -> list[SeriesPoint]:
    if len(points) <= n:
        return points
    ys = [p.mean if p.mean is not None else (p.actual or 0.0) for p in points]
    keep = lttb([float(i) for i in range(len(points))], ys, n)
    return [points[i] for i in keep]


_MINUS: Final = "−"


def fmt(v: float | None, decimals: int = 0) -> str:
    """A number for plain text: thousands separated, U+2212 for a minus sign."""
    if v is None:
        return "no value"
    text = f"{abs(v):,.{decimals}f}"
    return (_MINUS + text) if v < 0 and text.strip("0.,") else text


def _pct(v: float) -> str:
    return f"{fmt(abs(v) * 100, 0)}%"


def _dir(delta: float | None, *, alert: bool = False) -> Literal["up", "down", "flat", "alert"]:
    if alert:
        return "alert"
    if delta is None or abs(delta) < 0.005:
        return "flat"
    return "up" if delta > 0 else "down"


def kpi_noun(spec: DashboardSpec) -> str:
    label = spec.kpi.label
    return label.lower() if label[1:] == label[1:].lower() else label


def _kpi_unit(spec: DashboardSpec) -> TileUnit:
    return spec.kpi.unit


def _channel_rows(spec: DashboardSpec, facts: Mapping[str, ChannelFacts]) -> list[ChannelReadout]:
    breakeven = breakeven_of(spec.kpi)
    contributions = [facts[c.name].contribution for c in spec.channels if c.name in facts]
    total = math.fsum(x for x in contributions if x is not None) or None
    out: list[ChannelReadout] = []
    for ch in spec.channels[:MAX_CHANNELS]:
        f = facts.get(ch.name, ChannelFacts())
        share = f.share
        if share is None and f.contribution is not None and total:
            share = f.contribution / total
        out.append(
            ChannelReadout(
                name=ch.name,
                label=ch.label,
                spend=f.spend,
                contribution=f.contribution,
                share=share,
                roi=f.roi,
                roi_low=f.roi_low,
                roi_high=f.roi_high,
                mroi=f.mroi,
                zone=zone_of(f.roi, f.mroi, breakeven=breakeven) if breakeven else None,
            )
        )
    return out


def _media_tiles(
    spec: DashboardSpec, channels: Sequence[ChannelReadout]
) -> dict[TileKey, KpiValue]:
    tiles: dict[TileKey, KpiValue] = {}
    spend = math.fsum(c.spend for c in channels if c.spend is not None)
    contrib = math.fsum(
        c.contribution for c in channels if c.contribution is not None and c.spend is not None
    )
    if spend > 0:
        roi = contrib / spend
        unit = spec.kpi.currency or "unit"
        tiles["return_on_media"] = KpiValue(
            key="return_on_media",
            label="Return on media",
            value=roi,
            unit="ratio",
            caption=f"Each 1 {unit} of media brought {fmt(roi, 2)} {kpi_noun(spec)}"[:200],
        )
        zoned = [c for c in channels if c.zone is not None and c.spend is not None]
        if zoned:
            good = math.fsum(c.spend or 0.0 for c in zoned if c.zone in ("headroom", "optimal"))
            share = good / spend * 100
            tiles["in_range_spend"] = KpiValue(
                key="in_range_spend",
                label="Spend in a good range",
                value=share,
                unit="percent",
                caption=f"{fmt(share)}% of media spend is within range or has more room",
            )
    return tiles


def build_snapshot(
    spec: DashboardSpec,
    *,
    kind: ReadoutKind,
    points: Sequence[SeriesPoint],
    facts: Mapping[str, ChannelFacts],
    scenario: Mapping[str, float] | None = None,
    source_run_ids: Sequence[str] = (),
    input_fingerprint: str | None = None,
    conditioning: Any = None,
    technical: Mapping[str, str | float] | None = None,
    notes: Sequence[str] = (),
) -> ReadoutSnapshot:
    """The snapshot of a score-based readout (pure). ``points`` is the KPI series
    (:func:`kpi_series`); the tiles describe its latest period. Raises ``ValueError`` when the
    result is over the snapshot cap after the series is thinned to
    :data:`~mapi_spec.dashboards.MAX_SERIES_POINTS` points."""
    period = spec.period_label
    noun = kpi_noun(spec)
    unit = _kpi_unit(spec)
    d = spec.kpi.decimals
    series = _downsample(list(points), MAX_SERIES_POINTS)
    latest = next((p for p in reversed(series) if p.mean is not None), None)
    prev = None
    if latest is not None:
        idx = series.index(latest)
        prev = next((p for p in reversed(series[:idx]) if p.mean is not None), None)
    with_actual = [p for p in series if p.actual is not None and p.mean is not None]
    accuracy: ActualsAccuracy | None = None
    if with_actual:
        accuracy = metrics_of(
            [p.mean for p in with_actual],
            [p.actual for p in with_actual],
            [p.low for p in with_actual],
            [p.high for p in with_actual],
        )
    last_actual = with_actual[-1] if with_actual else None
    channels = _channel_rows(spec, facts)
    tiles = _media_tiles(spec, channels)
    if latest is not None and latest.mean is not None:
        change = (
            (latest.mean - prev.mean) / prev.mean
            if prev is not None and prev.mean not in (None, 0.0)
            else None
        )
        caption = (
            f"Likely range {fmt(latest.low, d)} to {fmt(latest.high, d)}"
            if latest.low is not None and latest.high is not None
            else f"The model's expectation this {period}"
        )
        tiles["expected"] = KpiValue(
            key="expected",
            label=f"Expected {noun}"[:80],
            value=latest.mean,
            unit=unit,
            delta=change,
            delta_basis=f"the {period} before" if change is not None else None,
            delta_dir=_dir(change),
            low=latest.low,
            high=latest.high,
            caption=caption[:200],
        )
    if last_actual is not None and last_actual.mean and last_actual.actual is not None:
        vs = (last_actual.actual - last_actual.mean) / last_actual.mean
        outside = (
            last_actual.low is not None
            and last_actual.high is not None
            and not last_actual.low <= last_actual.actual <= last_actual.high
        )
        tiles["vs_expected"] = KpiValue(
            key="vs_expected",
            label="Compared with the model",
            value=vs * 100,
            unit="percent",
            delta=vs,
            delta_basis="the model's expectation",
            delta_dir=_dir(vs, alert=outside),
            caption=(
                f"{fmt(last_actual.actual, d)} {noun} against {fmt(last_actual.mean, d)} expected"
            )[:200],
        )
        tiles["index"] = KpiValue(
            key="index",
            label="Index",
            value=100 * last_actual.actual / last_actual.mean,
            unit="index",
            delta_dir=_dir(vs, alert=outside),
            caption="100 is the model's expectation",
        )
    if accuracy is not None and accuracy.mape is not None:
        tiles["accuracy"] = KpiValue(
            key="accuracy",
            label="Prediction error",
            value=accuracy.mape,
            unit="percent",
            caption=f"Average error over {accuracy.n} {period}s"[:200],
        )
    if last_actual is not None and last_actual.mean and last_actual.actual is not None:
        vs = (last_actual.actual - last_actual.mean) / last_actual.mean
        word = "more" if vs > 0 else "less"
        lead = f"The result was {fmt(last_actual.actual, d)} {noun} this {period}. " + (
            "This is the same as the model expects."
            if abs(vs) < 0.005
            else f"This is {_pct(vs)} {word} than the model expects."
        )
    elif latest is not None:
        lead = f"The model expects {fmt(latest.mean, d)} {noun} this {period}."
    else:
        lead = f"This readout has no result for the latest {period}."
    if kind == "scenario" and scenario:
        lead = f"This plan changes the spend on {len(scenario)} channels. " + lead
    return ReadoutSnapshot(
        lead=lead[:400],
        tiles=[tiles[k] for k in spec.tiles if k in tiles],
        channels=channels,
        series=[Series(name=spec.kpi.variable, label=spec.kpi.label, points=series)]
        if series
        else [],
        accuracy=accuracy,
        conditioning=conditioning,
        scenario=dict(scenario) if scenario else None,
        source_run_ids=list(source_run_ids)[:16],
        input_fingerprint=input_fingerprint,
        notes_plain=[n[:400] for n in notes][:20],
        technical=dict(technical or {}),
    )


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _slope(xs: Sequence[float], ys: Sequence[float], spend: Sequence[float], x: float) -> float:
    """The marginal return at multiplier ``x``: the slope of contribution over spend on the
    grid segment that holds ``x`` (the one to its right at a grid point, except at the end)."""
    i = next((k for k in range(1, len(xs)) if x < xs[k]), len(xs) - 1)
    ds = spend[i] - spend[i - 1]
    return (ys[i] - ys[i - 1]) / ds if ds else 0.0


def grid_facts(
    grid: ResponseGridView, multipliers: Mapping[str, float] | None = None
) -> dict[str, ChannelFacts]:
    """Spend, mean contribution, return (5-95 % over draws) and marginal return per channel
    at ``multipliers`` (default: the current plan), from the response grid.

    The grid holds totals over its ``periods``-period fit window; spend and contribution come
    out per period (total / ``periods``), as the readout shows them next to the KPI of one
    period. Return and marginal return are ratios, so the window cancels."""
    xs = grid.multipliers
    per = float(grid.periods)
    out: dict[str, ChannelFacts] = {}
    for c, ch in enumerate(grid.channels):
        x = (multipliers or {}).get(ch, 1.0)
        total_spend = _interp(xs, grid.spend[c], x)
        per_draw = sorted(_interp(xs, grid.contribution[d][c], x) for d in range(grid.draws))
        mean_curve = [
            math.fsum(grid.contribution[d][c][k] for d in range(grid.draws)) / grid.draws
            for k in range(len(xs))
        ]
        total = math.fsum(per_draw) / len(per_draw)
        lo = per_draw[int(0.05 * (len(per_draw) - 1))]
        hi = per_draw[math.ceil(0.95 * (len(per_draw) - 1))]
        out[ch] = ChannelFacts(
            spend=total_spend / per,
            contribution=total / per,
            roi=total / total_spend if total_spend else None,
            roi_low=lo / total_spend if total_spend else None,
            roi_high=hi / total_spend if total_spend else None,
            mroi=_slope(xs, mean_curve, grid.spend[c], x),
        )
    return out


def check_scenario(spec: DashboardSpec, multipliers: Mapping[str, float]) -> None:
    """``SCENARIO_OUT_OF_RANGE`` for a channel the dashboard does not show or a multiplier
    outside its slider range."""
    by_name = {c.name: c for c in spec.channels}
    for name, x in multipliers.items():
        ch = by_name.get(name)
        if ch is None:
            raise MapiError(
                f"the dashboard has no channel {name!r}",
                code=ErrorCode.SCENARIO_OUT_OF_RANGE,
                pointer=f"/multipliers/{name}",
            )
        if not ch.min_mult - 1e-9 <= x <= ch.max_mult + 1e-9:
            raise MapiError(
                f"the multiplier of {name!r} ({x}) is outside the range "
                f"{ch.min_mult} to {ch.max_mult}",
                code=ErrorCode.SCENARIO_OUT_OF_RANGE,
                pointer=f"/multipliers/{name}",
            )


def whatif_result(
    spec: DashboardSpec, grid: ResponseGridView, multipliers: Mapping[str, float]
) -> WhatIfResult:
    """A spend plan evaluated on the (clipped) grid: :func:`mapi_spec.whatif.evaluate_scenario`
    for the KPI, the channel rows at the plan and one plain sentence. No run, no write.

    :func:`~mapi_spec.whatif.evaluate_scenario` answers over the grid's whole fit window;
    every number here is per period (window / ``periods``), like the readout's tiles and
    channel rows, and the words say so."""
    check_scenario(spec, multipliers)
    res = evaluate_scenario(grid, WhatIfScenario(multipliers=dict(multipliers)))
    facts = grid_facts(grid, multipliers)
    channels = _channel_rows(spec, facts)
    tiles = _media_tiles(spec, channels)
    d = spec.kpi.decimals
    noun = kpi_noun(spec)
    period = spec.period_label
    per = float(grid.periods)
    delta = res.kpi_delta.mean / per
    if res.kpi is not None:
        mean = res.kpi.mean / per
        base = mean - delta
        lo, hi = res.kpi.quantiles[0] / per, res.kpi.quantiles[-1] / per
        tiles["expected"] = KpiValue(
            key="expected",
            label=f"Expected {noun}"[:80],
            value=mean,
            unit=spec.kpi.unit,
            delta=delta / base if base else None,
            delta_basis="the current plan",
            delta_dir=_dir(delta / base if base else None),
            low=lo,
            high=hi,
            caption=f"A {period} on average over {grid.periods} {period}s. Likely range "
            f"{fmt(lo, d)} to {fmt(hi, d)}"[:200],
        )
    if abs(delta) < 0.5 * 10**-d:
        lead = "This plan brings the same result as the current plan."
    else:
        word = "more" if delta > 0 else "fewer" if spec.kpi.unit == "count" else "less"
        lead = (
            f"This plan brings about {fmt(abs(delta), d)} {word} {noun} a {period} than the "
            "current plan."
        )
    return WhatIfResult(
        tiles=[tiles[k] for k in spec.tiles if k in tiles],
        channels=channels,
        delta_kpi=delta,
        lead=lead[:400],
    )


def scenario_snapshot(
    spec: DashboardSpec, result: WhatIfResult, multipliers: Mapping[str, float]
) -> ReadoutSnapshot:
    """The snapshot of a saved grid scenario (ready at once)."""
    return ReadoutSnapshot(
        lead=result.lead,
        tiles=list(result.tiles),
        channels=list(result.channels),
        scenario=dict(multipliers),
        notes_plain=["The response grid gives this result. No model run was made."],
        technical={"source": "response_grid", "delta_kpi": result.delta_kpi},
    )


# ============================================================================== materialising


class TransientReadoutError(Exception):
    """A materialisation input could not be read for now (a storage timeout or 5xx): the
    loop retries the readout, up to :data:`MAX_MATERIALISE_ATTEMPTS` passes."""


MAX_MATERIALISE_ATTEMPTS: Final = 5
"""Passes a ``materialising`` readout may fail (transient errors, or anything unexpected)
before it is failed for good; the loop takes the readouts with fewer attempts first."""


def _transient(exc: MapiError) -> bool:
    """A retryable failure (infrastructure, rate limits, timeouts), not a missing, invalid
    or oversized object (those stay as they are)."""
    return ERROR_INFO[exc.code].retryable and exc.code is not ErrorCode.NOT_READY


async def _summary_of(
    ctx: ServerContext, session: AsyncSession, run_id: str
) -> ScoreSummary | None:
    """The score run's ``summary.json`` (size- and depth-capped, validated; ``None`` when
    missing or malformed), as ``GET …:score`` reads it. A transient storage error raises
    :class:`TransientReadoutError` (the readout is retried, never failed for it)."""
    found = await _run_json(
        ctx, session, run_id, SCORE_SUMMARY_OUTPUT, ScoreSummary, SCORE_SUMMARY_MAX_BYTES
    )
    return found if isinstance(found, ScoreSummary) else None


async def _run_json(
    ctx: ServerContext,
    session: AsyncSession,
    run_id: str,
    name: str,
    model: Any,
    max_bytes: int,
) -> Any:
    """The output ``name`` of a succeeded run validated as ``model`` (size- and
    depth-capped JSON; ``None`` when missing, too large or malformed). A transient storage
    error raises :class:`TransientReadoutError`."""
    run = await session.get(m.Run, run_id)
    if run is None or run.state != RunState.SUCCEEDED.value:
        return None
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    meta = (att.outputs or {}).get(name) if att is not None else None
    if not isinstance(meta, dict) or int(meta.get("size") or 0) > max_bytes:
        return None
    try:
        raw = await ctx.store.get(
            meta["bucket"],
            meta["name"],
            generation=int(meta["generation"]),
            max_bytes=max_bytes,
        )
        return model.model_validate(loads_limited(raw))
    except MapiError as exc:
        if _transient(exc):
            raise TransientReadoutError(f"run {run_id} {name}: {exc.code.value}") from exc
        log.warning("%s of run %s could not be read", name, run_id)
        return None
    except (ValidationError, ValueError, KeyError, TypeError):
        log.warning("%s of run %s could not be read", name, run_id)
        return None


async def fit_facts(session: AsyncSession, mv: m.ModelVersion) -> dict[str, ChannelFacts]:
    """Per-channel facts from the version's fit run metrics (``labels = {channel: …}``)."""
    rows = (
        await session.execute(
            select(m.RunMetricSummary).where(
                m.RunMetricSummary.run_id == mv.run_id,
                m.RunMetricSummary.key.in_(
                    ("roi", "roi_p05", "roi_p95", "mroi", "contribution_share")
                ),
            )
        )
    ).scalars()
    found: dict[str, dict[str, float]] = {}
    for r in rows:
        labels = r.labels or {}
        ch = labels.get("channel")
        if not isinstance(ch, str) or set(labels) != {"channel"}:
            continue
        v = _f(r.last)
        if v is not None:
            found.setdefault(ch, {})[r.key] = v
    return {
        ch: ChannelFacts(
            roi=v.get("roi"),
            roi_low=v.get("roi_p05"),
            roi_high=v.get("roi_p95"),
            mroi=v.get("mroi"),
            share=v.get("contribution_share"),
        )
        for ch, v in found.items()
    }


def merge_facts(
    fit: Mapping[str, ChannelFacts], grid: Mapping[str, ChannelFacts]
) -> dict[str, ChannelFacts]:
    """Fit metrics first (the model's own numbers), the grid for spend and the rest."""
    out: dict[str, ChannelFacts] = {}
    for ch in {*fit, *grid}:
        a, b = fit.get(ch, ChannelFacts()), grid.get(ch, ChannelFacts())
        out[ch] = ChannelFacts(
            spend=b.spend,
            contribution=b.contribution
            if b.contribution is not None
            else (a.roi * b.spend if a.roi is not None and b.spend is not None else None),
            share=a.share,
            roi=a.roi if a.roi is not None else b.roi,
            roi_low=a.roi_low if a.roi_low is not None else b.roi_low,
            roi_high=a.roi_high if a.roi_high is not None else b.roi_high,
            mroi=a.mroi if a.mroi is not None else b.mroi,
        )
    return out


def _sum_by_dim(ds: XrJsonDataset, var: str, dim: str) -> dict[str, float]:
    """The values of ``var`` per label of ``dim`` (summed over its other dims)."""
    spec = ds.data_vars.get(var)
    coord = ds.coords.get(dim)
    if spec is None or coord is None or dim not in spec.dims:
        return {}
    labels = coord.data if isinstance(coord.data, list) else []
    axis = spec.dims.index(dim)
    out: dict[str, float] = {}

    def walk(node: JsonValue, depth: int, at: int | None) -> None:
        if depth == len(spec.dims):
            v = _f(node)
            if v is not None and at is not None and at < len(labels):
                key = str(labels[at])
                out[key] = out.get(key, 0.0) + v
            return
        if isinstance(node, list):
            for k, child in enumerate(node):
                walk(child, depth + 1, k if depth == axis else at)

    walk(spec.data, 0, None)
    return out


async def uploaded_actuals(
    ctx: ServerContext, ref: Mapping[str, Any], kpi_column: str, time_dim: str | None
) -> dict[str, float] | None:
    """The KPI actuals of a stored upload, per period (``None`` when the upload cannot be
    read; a transient storage error raises :class:`TransientReadoutError`)."""
    try:
        raw = await ctx.store.get(
            str(ref["bucket"]),
            str(ref["name"]),
            generation=int(ref["generation"]),
            max_bytes=MAX_XRJSON_BYTES,
        )
        ds = XrJsonDataset.model_validate(loads_limited(raw))
    except MapiError as exc:
        if _transient(exc):
            raise TransientReadoutError(f"upload {ref.get('name')}: {exc.code.value}") from exc
        return None
    except (ValidationError, ValueError, KeyError, TypeError):
        return None
    var = ds.data_vars.get(kpi_column)
    if var is None or not var.dims:
        return {}
    dim = time_dim if time_dim in var.dims else var.dims[0]
    return _sum_by_dim(ds, kpi_column, dim)


def _period_of(points: Sequence[SeriesPoint]) -> tuple[date, date] | None:
    dated = [(p, _as_date(p.t)) for p in points if p.mean is not None or p.actual is not None]
    dated = [(p, d) for p, d in dated if d is not None]
    if not dated:
        return None
    end = dated[-1][1]
    assert end is not None
    start = end
    if len(dated) > 1 and dated[-2][1] is not None:
        start = min(end, dated[-2][1] + timedelta(days=1))
    return start, end


@dataclass(slots=True)
class _Material:
    snapshot: ReadoutSnapshot | None = None
    problem: Problem | None = None
    period: tuple[date, date] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


async def _build(ctx: ServerContext, session: AsyncSession, row: m.DashboardReadout) -> _Material:
    rev = await session.get(m.DashboardRevision, row.revision_id)
    if rev is None:  # pragma: no cover - cascade
        return _Material(problem=Problem.from_code(ErrorCode.NOT_FOUND, "the revision is gone"))
    any_spec = parse_dashboard_spec(rev.spec)
    mv = await session.get(m.ModelVersion, row.model_version_id)
    from mapi_server.services.sharing import ran_uploaded_code

    if ran_uploaded_code(mv):  # defence in depth: every path that appends one refuses it
        return _Material(
            problem=Problem.from_code(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "the model version ran uploaded plugin code; dashboards show only versions "
                "fitted with platform plugins",
            )
        )
    if isinstance(any_spec, DashboardSpecV2):
        return await _build_v2(ctx, session, row, rev, any_spec, mv)
    spec = any_spec
    summary = await _summary_of(ctx, session, row.run_ids[0]) if row.run_ids else None
    if row.run_ids and summary is None:
        return _Material(
            problem=Problem.from_code(
                ErrorCode.INTERNAL_ERROR, "the score run wrote no readable summary"
            )
        )
    fit: dict[str, ChannelFacts] = {}
    grid: dict[str, ChannelFacts] = {}
    if mv is not None:
        fit = await fit_facts(session, mv)
        from mapi_server.services.dashboards.crud import read_grid

        try:
            view = await read_grid(ctx, mv)
        except MapiError:
            view = None
        if view is not None:
            grid = grid_facts(view, row.scenario if row.kind == "scenario" else None)
    actuals: dict[str, float] = {}
    if row.input_ref:
        kpi_column = str(row.input_ref.get("kpi_column") or spec.kpi.variable)
        time_dim = row.input_ref.get("time_dim")  # the input contract's, stored at upload
        if not isinstance(time_dim, str) or not time_dim:
            table = (
                _pick(summary.tables, spec.kpi.variable)
                if summary is not None and summary.tables
                else None
            )
            time_dim = table.time_dim if table is not None else None
        found = await uploaded_actuals(ctx, row.input_ref, kpi_column, time_dim)
        if found is None:  # never a ready comparison without the numbers it compares
            return _Material(
                problem=Problem.from_code(
                    ErrorCode.NOT_FOUND, "the uploaded numbers can no longer be read"
                )
            )
        actuals = found
    points = kpi_series(summary, spec.kpi.variable, actuals)
    notes = []
    if row.kind == "actuals":
        notes.append(
            "The model uses your numbers to update its forecast. The model parameters do not "
            "change."
            if summary is not None and summary.conditioning is not None
            else "We compare your numbers with what the model expected. The model does not change."
        )
    try:
        snapshot = build_snapshot(
            spec,
            kind=row.kind,  # type: ignore[arg-type]
            points=points,
            facts=merge_facts(fit, grid),
            scenario=row.scenario,
            source_run_ids=row.run_ids,
            input_fingerprint=row.input_fingerprint,
            conditioning=summary.conditioning if summary is not None else None,
            technical={"model_ref": row.model_ref, "spec_hash": row.spec_hash},
            notes=notes,
        )
    except (ValueError, ValidationError) as exc:
        return _Material(
            problem=Problem.from_code(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "the readout is too large to show",
                untrusted_text=str(exc)[:2000],
            )
        )
    return _Material(snapshot=snapshot, period=_period_of(points))


# ================================================================== materialising (layout 2)

VIEW_MAX_BYTES: Final = 4 * 1024 * 1024
"""Cap on a guided view (forecast, counterfactual) read for a layout 2 readout."""
FIELDS_MAX_BYTES: Final = 8 * 1024 * 1024
"""Cap on the ``ui/fields.json`` of a run read for a layout 2 readout."""
HISTORY_POINTS: Final = 104
_CONDITIONED_NOTE: Final = (
    "The model uses your numbers to update its forecast. The model parameters do not change."
)
_COMPARED_NOTE: Final = (
    "We compare your numbers with what the model expected. The model does not change."
)
_REFIT_NOTE: Final = (
    "The estimate of the effect updates after your analyst fits the model again with your numbers."
)


def _invert(binding: Mapping[str, str] | None) -> dict[str, str]:
    """Dataset name → canonical name (the binding maps canonical → dataset)."""
    return {v: k for k, v in (binding or {}).items()}


def _upload_names(mv: m.ModelVersion | None, ref: Mapping[str, Any]) -> dict[str, str]:
    """How the summary's dims (dataset names) are named in an upload (canonical names):
    the upload's own ``names``, else the version's binding reversed."""
    given = ref.get("names")
    if isinstance(given, dict):
        return {str(k): str(v) for k, v in given.items()}
    if mv is None or mv.score_meta is None:
        return {}
    from mapi_spec.validate import ScoreMetadata

    try:
        meta = ScoreMetadata.model_validate(mv.score_meta)
    except ValidationError:
        return {}
    return _invert(meta.resolved.binding.dims) if meta.resolved.binding is not None else {}


def _upload_column(spec: DashboardSpecV2, ref: Mapping[str, Any], ds: XrJsonDataset) -> str | None:
    """The column of an upload that holds the observed outcome."""
    for name in (
        ref.get("kpi_column"),
        ref.get("label_column"),
        spec.actuals.label_column,
        spec.subject.variable,
        "y",
    ):
        if isinstance(name, str) and name in ds.data_vars:
            return name
    return None


async def _read_upload(ctx: ServerContext, ref: Mapping[str, Any]) -> XrJsonDataset | None:
    """A stored upload (``None`` when it cannot be read; transient errors raise)."""
    try:
        raw = await ctx.store.get(
            str(ref["bucket"]),
            str(ref["name"]),
            generation=int(ref["generation"]),
            max_bytes=MAX_XRJSON_BYTES,
        )
        return XrJsonDataset.model_validate(loads_limited(raw))
    except MapiError as exc:
        if _transient(exc):
            raise TransientReadoutError(f"upload {ref.get('name')}: {exc.code.value}") from exc
        return None
    except (ValidationError, ValueError, KeyError, TypeError):
        return None


async def _metric_values(session: AsyncSession, mv: m.ModelVersion) -> list[Any]:
    from mapi_server.services.dashboards.panels import MetricValue

    rows = (
        await session.execute(
            select(m.RunMetricSummary)
            .where(m.RunMetricSummary.run_id == mv.run_id)
            .order_by(m.RunMetricSummary.key, m.RunMetricSummary.labels_key)
            .limit(1000)
        )
    ).scalars()
    return [
        MetricValue(r.key, {str(k): str(v) for k, v in (r.labels or {}).items()}, _f(r.last))
        for r in rows
    ]


def _scorecard_of(mv: m.ModelVersion) -> Any:
    from mapi_spec.scorecard import Scorecard

    if not mv.scorecard:
        return None
    try:
        return Scorecard.model_validate(mv.scorecard)
    except ValidationError:
        return None


async def _catalog_of(ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion) -> Any:
    """The version's source catalog (``None`` when it cannot be read: the panels then fall
    back on what the summary tables say)."""
    from mapi_server.services.dashboards.sources import source_catalog

    try:
        return await source_catalog(ctx, session, mv)
    except (MapiError, ValidationError, ValueError):
        log.info("model version %s: no source catalog for the readout", mv.id)
        return None


def _dates_of(ts: Sequence[str | None]) -> tuple[date, date] | None:
    days = sorted(d for d in (_as_date(t) for t in ts if t) if d is not None)
    return (days[0], days[-1]) if days else None


async def _history(
    session: AsyncSession, row: m.DashboardReadout, current: Any, period: Any
) -> list[Any]:
    """The accuracy history: the last :data:`HISTORY_POINTS` **published**, ready actuals
    readouts of the dashboard (their stored snapshots, validated), oldest first, then this
    readout when it enters the history itself. Private and pending rows never mix in."""
    from mapi_server.services.dashboards.panels import accuracy_point, history_point

    rows = list(
        (
            await session.execute(
                select(m.DashboardReadout)
                .where(
                    m.DashboardReadout.dashboard_id == row.dashboard_id,
                    m.DashboardReadout.kind == "actuals",
                    m.DashboardReadout.visibility == "published",
                    m.DashboardReadout.status == "ready",
                    m.DashboardReadout.seq < row.seq,
                )
                .order_by(m.DashboardReadout.seq.desc())
                .limit(HISTORY_POINTS)
            )
        ).scalars()
    )
    out: list[Any] = []
    for r in reversed(rows):
        try:
            snap = ReadoutSnapshot.model_validate(r.snapshot)
        except (ValidationError, ValueError):
            continue
        span = (r.period_start, r.period_end) if r.period_start and r.period_end else None
        point = history_point(r.seq, r.created_at, span, snap)
        if point is not None:
            out.append(point)
    if row.kind == "actuals" and row.visibility == "published" and current is not None:
        point = accuracy_point(row.seq, row.created_at, period, current)
        if point is not None:
            out.append(point)
    return out[-HISTORY_POINTS:]


async def _previous(session: AsyncSession, row: m.DashboardReadout) -> dict[str, float]:
    """The KPI panel values of the previous published, ready layout 2 readout."""
    prev = (
        await session.execute(
            select(m.DashboardReadout.snapshot)
            .where(
                m.DashboardReadout.dashboard_id == row.dashboard_id,
                m.DashboardReadout.visibility == "published",
                m.DashboardReadout.status == "ready",
                m.DashboardReadout.seq < row.seq,
            )
            .order_by(m.DashboardReadout.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    out: dict[str, float] = {}
    panels = prev.get("panels") if isinstance(prev, dict) else None
    for block in panels if isinstance(panels, list) else []:
        tile = block.get("tile") if isinstance(block, dict) and block.get("kind") == "kpi" else None
        value = _f(tile.get("value")) if isinstance(tile, dict) else None
        if value is not None and isinstance(block.get("panel_id"), str):
            out[block["panel_id"]] = value
    return out


async def _assets_of(ctx: ServerContext, session: AsyncSession, rev_id: str) -> dict[str, Any]:
    from mapi_server.services.dashboards.assets import revision_assets
    from mapi_server.services.dashboards.panels import AssetInfo

    try:
        found = await revision_assets(ctx, session, rev_id)
    except MapiError as exc:
        if _transient(exc):
            raise TransientReadoutError(f"assets of {rev_id}: {exc.code.value}") from exc
        return {}
    return {
        plot_id: AssetInfo(a.id, a.status, a.width, a.height, a.problem)
        for plot_id, a in found.items()
    }


async def _planner_snapshot(
    ctx: ServerContext,
    row: m.DashboardReadout,
    spec: DashboardSpecV2,
    mv: m.ModelVersion | None,
    session: AsyncSession,
    summary: ScoreSummary | None,
    notes: Sequence[str],
) -> tuple[ReadoutSnapshot, list[SeriesPoint]]:
    """The media planner part of an ``mmm_planner`` readout: exactly the layout 1 snapshot
    (tiles, channel rows, the KPI series and lead), except that a period summed over several
    cells has no interval (the layout 2 rule)."""
    from mapi_spec.dashboard_v2 import kpi_of_subject

    assert spec.mmm is not None
    v1 = DashboardSpec.model_construct(
        title=spec.title,
        period_label=spec.period_label,  # type: ignore[arg-type]  # text only
        source=spec.source,
        kpi=kpi_of_subject(spec.subject),
        channels=list(spec.mmm.channels),
        tiles=list(spec.mmm.tiles),
    )
    fit: dict[str, ChannelFacts] = {}
    grid: dict[str, ChannelFacts] = {}
    if mv is not None:
        fit = await fit_facts(session, mv)
        from mapi_server.services.dashboards.crud import read_grid

        try:
            view = await read_grid(ctx, mv)
        except MapiError:
            view = None
        if view is not None:
            grid = grid_facts(view, row.scenario if row.kind == "scenario" else None)
    actuals: dict[str, float] = {}
    if row.input_ref and row.kind == "actuals":
        kpi_column = str(row.input_ref.get("kpi_column") or spec.subject.variable)
        time_dim = row.input_ref.get("time_dim")
        if not isinstance(time_dim, str) or not time_dim:
            table = _pick(summary.tables, spec.subject.variable) if summary else None
            time_dim = table.time_dim if table is not None else None
        found = await uploaded_actuals(ctx, row.input_ref, kpi_column, time_dim)
        actuals = found or {}
    points = kpi_series(summary, spec.subject.variable, actuals, summed_bounds=False)
    snap = build_snapshot(
        v1,
        kind=row.kind,  # type: ignore[arg-type]
        points=points,
        facts=merge_facts(fit, grid),
        scenario=row.scenario,
        notes=notes,
    )
    return snap, points


async def _build_v2(
    ctx: ServerContext,
    session: AsyncSession,
    row: m.DashboardReadout,
    rev: m.DashboardRevision,
    spec: DashboardSpecV2,
    mv: m.ModelVersion | None,
) -> _Material:
    """The layout 2 snapshot of one readout (plan I-3, I-6): reads the run's summary and
    guided views, the version's fit metrics, scorecard and catalog, the uploaded actuals,
    the accuracy history and the revision's pictures, then resolves every panel
    (:mod:`mapi_server.services.dashboards.panels`)."""
    from mapi_spec.views import (
        COUNTERFACTUAL_VIEW,
        FORECAST_VIEW,
        SCORE_FIELDS_VIEW,
        CounterfactualView,
        FieldsView,
        ForecastView,
    )

    from mapi_server.services.dashboards import panels as pn

    run_id = row.run_ids[0] if row.run_ids else None
    summary = await _summary_of(ctx, session, run_id) if run_id else None
    if run_id and summary is None:
        return _Material(
            problem=Problem.from_code(
                ErrorCode.INTERNAL_ERROR, "the score run wrote no readable summary"
            )
        )
    if row.kind == "batch" and summary is not None and summary.truncated:
        return _Material(
            problem=Problem.from_code(
                ErrorCode.DASHBOARD_BATCH_TOO_LARGE,
                "the file gave more results than a readout can hold; send fewer rows",
            )
        )
    run = await session.get(m.Run, run_id) if run_id else None
    request = (run.payload or {}).get("request") if run is not None else None
    request = request if isinstance(request, dict) else {}
    output = request.get("output") if isinstance(request.get("output"), dict) else None
    material = pn.Material(
        kind=row.kind,
        mode=str(request.get("mode")) if request.get("mode") else None,
        summary=summary,
        band=pn.run_band(output, _representation_of(mv)),
    )
    if run_id:
        material.forecast = await _run_json(
            ctx, session, run_id, FORECAST_VIEW, ForecastView, VIEW_MAX_BYTES
        )
        material.counterfactual = await _run_json(
            ctx, session, run_id, COUNTERFACTUAL_VIEW, CounterfactualView, VIEW_MAX_BYTES
        )
        if any(getattr(p, "intent", None) == "field" for p in spec.panels):
            material.fields = await _run_json(
                ctx, session, run_id, SCORE_FIELDS_VIEW, FieldsView, FIELDS_MAX_BYTES
            )
    if mv is not None:
        material.metrics = await _metric_values(session, mv)
        material.scorecard = _scorecard_of(mv)
        material.catalog = await _catalog_of(ctx, session, mv)
    notes: list[str] = []
    if row.kind == "actuals" and row.input_ref:
        ds = await _read_upload(ctx, row.input_ref)
        column = _upload_column(spec, row.input_ref, ds) if ds is not None else None
        if ds is None or column is None:  # never a ready comparison without its numbers
            return _Material(
                problem=Problem.from_code(
                    ErrorCode.NOT_FOUND, "the uploaded numbers can no longer be read"
                )
            )
        detail, pairs, more = pn.accuracy_of(
            spec, material, pn.actual_cells(ds, column), names=_upload_names(mv, row.input_ref)
        )
        material.accuracy, material.pairs = detail, pairs
        notes += more
        conditioned = summary is not None and summary.conditioning is not None
        notes.append(_CONDITIONED_NOTE if conditioned else _COMPARED_NOTE)
        if spec.preset == "causal_impact" and not isinstance(detail, EffectAccuracy):
            notes.append(_REFIT_NOTE)
    elif summary is not None and summary.actuals:  # a conditioned forecast echoes actuals
        detail, pairs, _ = pn.accuracy_of(spec, material, [])
        material.accuracy, material.pairs = detail, pairs
    period = _dates_of([p.t for p in material.pairs])
    material.history = await _history(session, row, material.accuracy, period)
    material.previous = await _previous(session, row)
    if any(isinstance(p, RecipePanel) and p.is_picture for p in spec.panels):
        material.assets = await _assets_of(ctx, session, rev.id)
    legacy: ReadoutSnapshot | None = None
    points: list[SeriesPoint] = []
    if spec.preset == "mmm_planner" and spec.mmm is not None:
        legacy, points = await _planner_snapshot(ctx, row, spec, mv, session, summary, notes)
        notes = []
    ref = row.input_ref if isinstance(row.input_ref, dict) else {}
    echo = [r for r in ref.get("rows") or [] if isinstance(r, dict)][:20]
    prediction = batch = None
    if row.kind == "prediction":
        labels = [str(x) for x in ref.get("row_labels") or []]
        dim = ref.get("row_dim") if isinstance(ref.get("row_dim"), str) else None
        prediction = pn.prediction_result(spec, material, echo, row_dim=dim, row_labels=labels)
    elif row.kind == "batch":
        n_rows = ref.get("n_rows") if isinstance(ref.get("n_rows"), int) else None
        id_dim = spec.batch.id_column if spec.batch is not None else None
        batch = pn.batch_summary(spec, material, n_rows=n_rows, id_dim=id_dim)
    try:
        snapshot = pn.build_snapshot_v2(
            spec,
            material,
            legacy=legacy,
            prediction=prediction,
            batch=batch,
            inputs_echo=echo,
            source_run_ids=row.run_ids,
            input_fingerprint=row.input_fingerprint,
            technical={"model_ref": row.model_ref, "spec_hash": row.spec_hash},
            notes=notes,
        )
    except (ValueError, ValidationError) as exc:
        return _Material(
            problem=Problem.from_code(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "the readout is too large to show",
                untrusted_text=str(exc)[:2000],
            )
        )
    if not points and snapshot.series:
        points = list(snapshot.series[0].points)
    return _Material(snapshot=snapshot, period=_period_of(points) or period)


def _representation_of(mv: m.ModelVersion | None) -> str | None:
    rep = (mv.capabilities or {}).get("representation") if mv is not None else None
    return rep if isinstance(rep, str) else None


async def record_failed_attempt(
    ctx: ServerContext, dashboard_id: str, seq: int, exc: BaseException
) -> str | None:
    """A materialisation pass of the readout raised (a transient storage error, or anything
    unexpected such as a commit the database refused): count it, and fail the readout once
    it reached :data:`MAX_MATERIALISE_ATTEMPTS` so it can neither loop nor starve the others.
    Returns ``"failed"`` when it did."""
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.DashboardReadout)
                .where(
                    m.DashboardReadout.dashboard_id == dashboard_id,
                    m.DashboardReadout.seq == seq,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.status != "materialising":
            return None
        row.attempts = (row.attempts or 0) + 1
        if row.attempts < MAX_MATERIALISE_ATTEMPTS:
            return None
        detail = (
            "the readout's inputs could not be read; refresh to try again"
            if isinstance(exc, TransientReadoutError)
            else "the readout could not be built"
        )
        row.status = "failed"
        row.problem = Problem.from_code(
            ErrorCode.INFRA_ERROR if isinstance(exc, TransientReadoutError)
            else ErrorCode.INTERNAL_ERROR,
            detail,
        ).model_dump(mode="json", exclude_none=True)  # fmt: skip
        return "failed"


async def materialise(ctx: ServerContext, dashboard_id: str, seq: int) -> str | None:
    """Build and store the snapshot of one ``materialising`` readout (the dashboard loop).

    The inputs are read first, outside any row lock; the row is then locked and written only
    if it still waits (another worker may have done it). Returns the new status, or ``None``
    when nothing was done."""
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.DashboardReadout).where(
                    m.DashboardReadout.dashboard_id == dashboard_id,
                    m.DashboardReadout.seq == seq,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.status != "materialising":
            return None
        built = await _build(ctx, s, row)
    async with ctx.db.session() as s:
        row = (
            await s.execute(
                select(m.DashboardReadout)
                .where(
                    m.DashboardReadout.dashboard_id == dashboard_id,
                    m.DashboardReadout.seq == seq,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.status != "materialising":
            return None
        if built.snapshot is None:
            row.status = "failed"
            row.problem = (
                built.problem or Problem.from_code(ErrorCode.INTERNAL_ERROR, "the readout failed")
            ).model_dump(mode="json", exclude_none=True)
            return "failed"
        row.snapshot = snapshot_json(built.snapshot)
        row.status = "ready"
        row.ready_at = now()
        if built.period is not None:
            row.period_start, row.period_end = built.period
            if row.kind in ("refresh", "actuals"):
                spec_rev = await s.get(m.DashboardRevision, row.revision_id)
                label = (spec_rev.spec.get("period_label") if spec_rev else None) or "period"
                row.title = f"{str(label).capitalize()} ending {built.period[1]:%d/%m/%Y}"
        await s.flush()
        if row.visibility == "published":
            await publish_ready_event(s, row)
        return "ready"
