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
from mapi_spec.errors import ErrorCode, MapiError, Problem
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
    dashboard row, so concurrent writers never pick the same one."""
    await _lock_dashboard(session, dashboard.id)
    top = await session.execute(
        select(func.coalesce(func.max(m.DashboardReadout.seq), 0)).where(
            m.DashboardReadout.dashboard_id == dashboard.id
        )
    )
    seq = int(top.scalar_one()) + 1
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
        snapshot=snapshot.model_dump(mode="json") if snapshot is not None else None,
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


def _period_title(spec: DashboardSpec, prefix: str) -> str:
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
        spec = DashboardSpec.model_validate(rev.spec)
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
        spec = DashboardSpec.model_validate(rev.spec)
        locked = await _lock_dashboard(session, dash.id)
        ts = now()
        if not spec.refresh.follow_endpoint or ran_uploaded_code(version):
            locked.problem = "new_version_available"
            locked.updated_at = ts
            changed += 1
            continue
        if ctx is not None:
            issues: list[Any] = []
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
    """What is known about one channel over the fit window (spend units and KPI units)."""

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


def _add(a: float | None, b: float | None) -> float | None:
    if b is None:
        return a
    return b if a is None else a + b


_MEAN_COLS: Final = ("mean", "median", "q50", "p50")
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
) -> list[SeriesPoint]:
    """The KPI per period (summed over the other dims), oldest first: the score's mean and
    interval, and the actual where one is known (the summary's ``actuals`` tables, then
    ``actuals`` by period label)."""
    acc: dict[str, _Acc] = {}
    table = _pick(summary.tables, variable) if summary is not None else None
    if table is not None:
        mean_i, low_i, high_i, coord = _stat_columns(table)
        time_i = _time_col(table, coord)
        for r in table.rows:
            t = str(r[time_i]) if time_i is not None and time_i < len(r) else "all"
            a = acc.setdefault(t, _Acc())
            a.parts += 1
            if mean_i is not None:
                a.mean = _add(a.mean, _f(r[mean_i]))
            if low_i is not None:
                a.low = _add(a.low, _f(r[low_i]))
            if high_i is not None:
                a.high = _add(a.high, _f(r[high_i]))
    if summary is not None and summary.actuals:
        at = _pick(summary.actuals, variable)
        if at is not None and "actual" in at.columns:
            ai = at.columns.index("actual")
            _, _, _, coord = _stat_columns(at)
            ti = _time_col(at, [i for i in coord if i != ai])
            for r in at.rows:
                t = str(r[ti]) if ti is not None and ti < len(r) else "all"
                a = acc.setdefault(t, _Acc())
                a.actual = _add(a.actual, _f(r[ai]))
    for t, v in (actuals or {}).items():
        a = acc.setdefault(str(t), _Acc())
        if a.actual is None:
            a.actual = v
    return [
        SeriesPoint(t=t[:32], mean=a.mean, low=a.low, high=a.high, actual=a.actual)
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
    at ``multipliers`` (default: the current plan), from the response grid."""
    xs = grid.multipliers
    out: dict[str, ChannelFacts] = {}
    for c, ch in enumerate(grid.channels):
        x = (multipliers or {}).get(ch, 1.0)
        spend = _interp(xs, grid.spend[c], x)
        per_draw = sorted(_interp(xs, grid.contribution[d][c], x) for d in range(grid.draws))
        mean_curve = [
            math.fsum(grid.contribution[d][c][k] for d in range(grid.draws)) / grid.draws
            for k in range(len(xs))
        ]
        contrib = math.fsum(per_draw) / len(per_draw)
        lo = per_draw[int(0.05 * (len(per_draw) - 1))]
        hi = per_draw[math.ceil(0.95 * (len(per_draw) - 1))]
        out[ch] = ChannelFacts(
            spend=spend,
            contribution=contrib,
            roi=contrib / spend if spend else None,
            roi_low=lo / spend if spend else None,
            roi_high=hi / spend if spend else None,
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
    for the KPI, the channel rows at the plan and one plain sentence. No run, no write."""
    check_scenario(spec, multipliers)
    res = evaluate_scenario(grid, WhatIfScenario(multipliers=dict(multipliers)))
    facts = grid_facts(grid, multipliers)
    channels = _channel_rows(spec, facts)
    tiles = _media_tiles(spec, channels)
    d = spec.kpi.decimals
    noun = kpi_noun(spec)
    delta = res.kpi_delta.mean
    if res.kpi is not None:
        base = res.kpi.mean - delta
        lo, hi = res.kpi.quantiles[0], res.kpi.quantiles[-1]
        tiles["expected"] = KpiValue(
            key="expected",
            label=f"Expected {noun}"[:80],
            value=res.kpi.mean,
            unit=spec.kpi.unit,
            delta=delta / base if base else None,
            delta_basis="the current plan",
            delta_dir=_dir(delta / base if base else None),
            low=lo,
            high=hi,
            caption=f"Over {grid.periods} {spec.period_label}s. Likely range {fmt(lo, d)} to "
            f"{fmt(hi, d)}"[:200],
        )
    if abs(delta) < 0.5 * 10**-d:
        lead = "This plan brings the same result as the current plan."
    else:
        word = "more" if delta > 0 else "fewer" if spec.kpi.unit == "count" else "less"
        lead = f"This plan brings about {fmt(abs(delta), d)} {word} {noun} than the current plan."
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


async def _summary_of(
    ctx: ServerContext, session: AsyncSession, run_id: str
) -> ScoreSummary | None:
    """The score run's ``summary.json`` (size- and depth-capped, validated; ``None`` when
    missing or malformed), as ``GET …:score`` reads it."""
    run = await session.get(m.Run, run_id)
    if run is None or run.state != RunState.SUCCEEDED.value:
        return None
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    meta = (att.outputs or {}).get(SCORE_SUMMARY_OUTPUT) if att is not None else None
    if not isinstance(meta, dict) or int(meta.get("size") or 0) > SCORE_SUMMARY_MAX_BYTES:
        return None
    try:
        raw = await ctx.store.get(
            meta["bucket"],
            meta["name"],
            generation=int(meta["generation"]),
            max_bytes=SCORE_SUMMARY_MAX_BYTES,
        )
        return ScoreSummary.model_validate(loads_limited(raw))
    except (MapiError, ValidationError, ValueError, KeyError, TypeError):
        log.warning("the summary of score run %s could not be read", run_id)
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
) -> dict[str, float]:
    """The KPI actuals of a stored upload, per period (empty when unreadable)."""
    try:
        raw = await ctx.store.get(
            str(ref["bucket"]),
            str(ref["name"]),
            generation=int(ref["generation"]),
            max_bytes=MAX_XRJSON_BYTES,
        )
        ds = XrJsonDataset.model_validate(loads_limited(raw))
    except (MapiError, ValidationError, ValueError, KeyError, TypeError):
        return {}
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
    spec = DashboardSpec.model_validate(rev.spec)
    mv = await session.get(m.ModelVersion, row.model_version_id)
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
        table = summary.tables[0] if summary is not None and summary.tables else None
        actuals = await uploaded_actuals(
            ctx,
            row.input_ref,
            str(row.input_ref.get("kpi_column") or spec.kpi.variable),
            table.time_dim if table is not None else None,
        )
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
        row.snapshot = built.snapshot.model_dump(mode="json")
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
