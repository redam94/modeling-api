"""The read-only dashboard tool surface, served (v0.16, plan D18 and I-3).

:func:`call_tool` runs one tool of :data:`mapi_spec.dashboard_tools.DASHBOARD_TOOLS` for a
:class:`~mapi_server.services.dashboards.access.DashboardContext` (a member, a client user or
a share-link session). No tool submits a run or writes a row; private readouts are seen only
by their actor and by analysts. The results are the DTOs the HTTP routes return - the
member, client and share read routes call these functions, so the surface v0.17's chat and
MCP adapters map onto is the one the portal uses now.

v0.17 (plan I-5, D19) serves either layout: ``compare_readouts`` adds the change of each KPI
panel (``panel_deltas``); ``list_panels``, ``get_panel``, ``get_accuracy``, ``list_inputs``,
``get_prediction`` and ``get_batch_summary`` read layout 2 readouts (the last two only the
caller's own); the media tools (``get_channel``, ``evaluate_whatif``) answer
``DASHBOARD_INPUT_DISABLED`` on a dashboard without the media planner. These are what the
v0.18 adapters (chat and MCP) map onto, 1:1.

v0.19 (plan D16): for a client user or a share link every tool first checks that nothing
the dashboard shows was made with uploaded code (``403 UPLOADED_CODE_NOT_SHAREABLE``), so a
caller that reaches :func:`run_tool` without the HTTP routes' checks (the v0.20 chat and MCP
adapters) is refused the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mapi_spec._dto import ResponseModel
from mapi_spec.dashboard_tools import (
    AccuracyReport,
    BatchReport,
    ChannelArgs,
    ChannelDelta,
    ChannelHistory,
    ChannelPoint,
    CompareArgs,
    ExplainArgs,
    InputsReport,
    ListReadoutsArgs,
    NoArgs,
    OptionalReadoutRef,
    PanelArgs,
    PanelDelta,
    PanelList,
    PanelReport,
    PredictionReport,
    ReadoutComparison,
    ReadoutRef,
    TileDelta,
    explain,
    tool,
)
from mapi_spec.dashboard_v2 import (
    MAX_PANELS,
    DashboardSpecV2,
    ErrorAccuracy,
    KpiBlock,
    panel_meta,
)
from mapi_spec.dashboards import (
    ChannelDef,
    ChannelReadout,
    ClientDashboard,
    DashboardSpec,
    KpiValue,
    Readout,
    ReadoutSnapshot,
    WhatIfRequest,
)
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.dashboards import inputs, readouts
from mapi_server.services.dashboards.access import (
    OVERVIEW_KINDS,
    AnySpec,
    DashboardContext,
    any_revision_spec,
    client_dashboard,
    is_mine,
    readout_dto,
    readout_header,
    refit_statuses,
)
from mapi_server.services.sharing import require_shareable

__all__ = ["call_tool", "run_tool"]

_HISTORY_SCAN: Final = 200
"""Published readouts scanned for a channel's history (newest first)."""


async def call_tool(
    ctx: ServerContext, dctx: DashboardContext, name: str, args: Mapping[str, JsonValue]
) -> ResponseModel:
    """Run the tool ``name`` with ``args`` (validated against its declaration) for ``dctx``
    in a session pinned to the dashboard's tenant. Unknown tools and invalid arguments are
    ``SCHEMA_INVALID``."""
    try:
        decl = tool(name)
    except KeyError:
        raise MapiError(
            f"no dashboard tool named {name!r}", code=ErrorCode.SCHEMA_INVALID
        ) from None
    try:
        parsed = decl.args.model_validate(dict(args))
    except ValidationError as exc:
        raise MapiError(
            f"the arguments of {name!r} are not valid",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=str(exc)[:2000],
        ) from None
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        return await run_tool(ctx, session, dctx, name, parsed)


async def run_tool(
    ctx: ServerContext,
    session: AsyncSession,
    dctx: DashboardContext,
    name: str,
    args: object,
) -> ResponseModel:
    """:func:`call_tool` in the caller's session, with already-validated arguments."""
    if dctx.principal_kind != "member":
        row = await session.get(m.Dashboard, dctx.dashboard_id)
        if row is None:
            raise NotFoundError("dashboard", dctx.dashboard_id)
        await require_shareable(session, dashboard=row)
    if name == "get_dashboard" and isinstance(args, NoArgs):
        return await _dashboard(session, dctx)
    if name == "get_latest_readout" and isinstance(args, NoArgs):
        return await _latest(session, dctx)
    if name == "list_readouts" and isinstance(args, ListReadoutsArgs):
        return await readouts.list_readouts(
            session, dctx, kind=args.kind, before_seq=args.before_seq, limit=args.limit
        )
    if name == "get_readout" and isinstance(args, ReadoutRef):
        result: Readout = await readouts.get_readout(session, dctx, args.seq)
        return result
    if name == "compare_readouts" and isinstance(args, CompareArgs):
        return await _compare(session, dctx, args)
    if name == "get_channel" and isinstance(args, ChannelArgs):
        return await _channel(session, dctx, args)
    if name == "explain_metric" and isinstance(args, ExplainArgs):
        return explain(args.metric)
    if name == "evaluate_whatif" and isinstance(args, WhatIfRequest):
        return await inputs.evaluate_whatif(ctx, session, dctx, args)
    if name == "list_panels" and isinstance(args, NoArgs):
        return await _panels(session, dctx)
    if name == "get_panel" and isinstance(args, PanelArgs):
        return await _panel(session, dctx, args)
    if name == "get_accuracy" and isinstance(args, OptionalReadoutRef):
        return await _accuracy(session, dctx, args)
    if name == "list_inputs" and isinstance(args, NoArgs):
        return await _inputs(session, dctx)
    if name == "get_prediction" and isinstance(args, ReadoutRef):
        return await _prediction(session, dctx, args)
    if name == "get_batch_summary" and isinstance(args, ReadoutRef):
        return await _batch(session, dctx, args)
    raise MapiError(f"the arguments do not fit the tool {name!r}", code=ErrorCode.SCHEMA_INVALID)


# ===================================================================================== tools


async def _parts(
    session: AsyncSession, dctx: DashboardContext
) -> tuple[m.Dashboard, m.DashboardRevision]:
    row = await session.get(m.Dashboard, dctx.dashboard_id)
    rev = await session.get(m.DashboardRevision, dctx.revision_id)
    if row is None or rev is None or row.status != "published":
        raise NotFoundError("dashboard", dctx.dashboard_id)
    return row, rev


async def _spec(session: AsyncSession, dctx: DashboardContext) -> AnySpec:
    _, rev = await _parts(session, dctx)
    return any_revision_spec(rev)


def _channels_of(spec: AnySpec) -> list[ChannelDef]:
    """The media planner's channels (layout 1, or the ``mmm_planner`` preset; else none)."""
    if isinstance(spec, DashboardSpec):
        return list(spec.channels)
    return list(spec.mmm.channels) if spec.mmm is not None else []


def _no_planner() -> MapiError:
    return MapiError(
        "this dashboard has no media planner: channels and spend plans are not part of it",
        code=ErrorCode.DASHBOARD_INPUT_DISABLED,
    )


async def _dashboard(session: AsyncSession, dctx: DashboardContext) -> ClientDashboard:
    row, rev = await _parts(session, dctx)
    return await client_dashboard(session, row, rev, dctx.inputs)


async def _latest_row(
    session: AsyncSession, dctx: DashboardContext, kind: str | None = None
) -> m.DashboardReadout | None:
    """The newest ready, published readout (of ``kind``)."""
    stmt = select(m.DashboardReadout).where(
        m.DashboardReadout.dashboard_id == dctx.dashboard_id,
        m.DashboardReadout.visibility == "published",
        m.DashboardReadout.status == "ready",
    )
    if kind is not None:
        stmt = stmt.where(m.DashboardReadout.kind == kind)
    else:  # the Overview's readout (a published kept case stays in the history)
        stmt = stmt.where(m.DashboardReadout.kind.in_(OVERVIEW_KINDS))
    return (
        await session.execute(stmt.order_by(m.DashboardReadout.seq.desc()).limit(1))
    ).scalar_one_or_none()


async def _latest(session: AsyncSession, dctx: DashboardContext) -> Readout:
    row = await _latest_row(session, dctx)
    if row is None:
        raise NotFoundError("readout", "latest")
    statuses = await refit_statuses(session, dctx.dashboard_id, [row.seq])
    return readout_dto(row, statuses.get(row.seq), analyst=dctx.is_analyst, mine=is_mine(row, dctx))


def _snap(row: m.DashboardReadout) -> ReadoutSnapshot | None:
    if row.status != "ready" or row.snapshot is None:
        return None
    return ReadoutSnapshot.model_validate(row.snapshot)


def _delta(a: float | None, b: float | None) -> float | None:
    return b - a if a is not None and b is not None else None


def _kpi_blocks(snap: ReadoutSnapshot | None) -> dict[str, KpiBlock]:
    return {b.panel_id: b for b in (snap.panels if snap else []) if isinstance(b, KpiBlock)}


def _panel_deltas(sa: ReadoutSnapshot | None, sb: ReadoutSnapshot | None) -> list[PanelDelta]:
    """The change of each KPI panel of either readout (``b``'s order, then ``a``'s)."""
    pa, pb = _kpi_blocks(sa), _kpi_blocks(sb)
    out: list[PanelDelta] = []
    for pid in [*pb, *(k for k in pa if k not in pb)]:
        a, b = pa.get(pid), pb.get(pid)
        va = a.tile.value if a is not None else None
        vb = b.tile.value if b is not None else None
        named = b if b is not None else a
        out.append(
            PanelDelta(
                panel_id=pid,
                label=named.title if named is not None else pid,
                a=va,
                b=vb,
                delta=_delta(va, vb),
            )
        )
    return out[:MAX_PANELS]


def _noun(label: str) -> str:
    return label.lower() if label[1:] == label[1:].lower() else label


def _compare_lead(
    spec: AnySpec, args: CompareArgs, tiles: list[TileDelta], panels: list[PanelDelta]
) -> str:
    kpi = spec.kpi if isinstance(spec, DashboardSpec) else spec.subject
    expected = next((t for t in tiles if t.key == "expected" and t.delta is not None), None)
    if expected is not None and expected.delta is not None:
        noun = readouts.kpi_noun(spec) if isinstance(spec, DashboardSpec) else _noun(kpi.label)
        word = "more" if expected.delta > 0 else "fewer" if kpi.unit == "count" else "less"
        if abs(expected.delta) > 0:
            return (
                f"Readout {args.b_seq} expects {readouts.fmt(abs(expected.delta), kpi.decimals)} "
                f"{word} {noun} than readout {args.a_seq}."
            )
        return f"Readouts {args.a_seq} and {args.b_seq} expect the same result."
    first = next((p for p in panels if p.delta is not None and p.b is not None), None)
    if first is not None and first.delta is not None and first.b is not None:
        decimals = 0 if float(first.b).is_integer() and float(first.delta).is_integer() else 2
        if abs(first.delta) > 0:
            way = "up" if first.delta > 0 else "down"
            return (
                f"{first.label} went {way} by {readouts.fmt(abs(first.delta), decimals)} "
                f"from readout {args.a_seq} to readout {args.b_seq}."
            )
        return f"{first.label} is the same in readouts {args.a_seq} and {args.b_seq}."
    return f"Readout {args.b_seq} compared with readout {args.a_seq}."


async def _compare(
    session: AsyncSession, dctx: DashboardContext, args: CompareArgs
) -> ReadoutComparison:
    spec = await _spec(session, dctx)
    ra = await readouts.load_visible(session, dctx, args.a_seq)
    rb = await readouts.load_visible(session, dctx, args.b_seq)
    sa, sb = _snap(ra), _snap(rb)
    # layout 2 KPI panels copied into the legacy tiles are compared as panels
    ta: dict[str, KpiValue] = {t.key: t for t in (sa.tiles if sa else []) if t.key != "panel"}
    tb: dict[str, KpiValue] = {t.key: t for t in (sb.tiles if sb else []) if t.key != "panel"}
    tiles = [
        TileDelta(
            key=k,
            label=(tb.get(k) or ta[k]).label,
            a=ta[k].value if k in ta else None,
            b=tb[k].value if k in tb else None,
            delta=_delta(ta[k].value if k in ta else None, tb[k].value if k in tb else None),
        )
        for k in [*ta, *(k for k in tb if k not in ta)]
    ][:6]
    ca: dict[str, ChannelReadout] = {c.name: c for c in (sa.channels if sa else [])}
    cb: dict[str, ChannelReadout] = {c.name: c for c in (sb.channels if sb else [])}
    channels = []
    for ch in _channels_of(spec):
        a, b = ca.get(ch.name), cb.get(ch.name)
        if a is None and b is None:
            continue
        channels.append(
            ChannelDelta(
                name=ch.name,
                label=ch.label,
                spend_a=a.spend if a else None,
                spend_b=b.spend if b else None,
                contribution_a=a.contribution if a else None,
                contribution_b=b.contribution if b else None,
                roi_a=a.roi if a else None,
                roi_b=b.roi if b else None,
                zone_a=a.zone if a else None,
                zone_b=b.zone if b else None,
            )
        )
    panel_deltas = _panel_deltas(sa, sb)
    lead = _compare_lead(spec, args, tiles, panel_deltas)
    if ra.model_version_id != rb.model_version_id:
        lead += " The model changed between the two readouts."
    statuses = await refit_statuses(session, dctx.dashboard_id, [ra.seq, rb.seq])
    return ReadoutComparison(
        a=readout_header(ra, statuses.get(ra.seq), mine=is_mine(ra, dctx)),
        b=readout_header(rb, statuses.get(rb.seq), mine=is_mine(rb, dctx)),
        same_model=ra.model_version_id == rb.model_version_id,
        tiles=tiles,
        channels=channels[:40],
        lead=lead[:400],
        panel_deltas=panel_deltas,
    )


async def _channel(
    session: AsyncSession, dctx: DashboardContext, args: ChannelArgs
) -> ChannelHistory:
    spec = await _spec(session, dctx)
    channels = _channels_of(spec)
    if not channels:
        raise _no_planner()
    ch = next((c for c in channels if c.name == args.name), None)
    if ch is None:
        raise NotFoundError("channel", args.name)
    rows = (
        await session.execute(
            select(m.DashboardReadout)
            .where(
                m.DashboardReadout.dashboard_id == dctx.dashboard_id,
                m.DashboardReadout.visibility == "published",
                m.DashboardReadout.status == "ready",
            )
            .order_by(m.DashboardReadout.seq.desc())
            .limit(_HISTORY_SCAN)
        )
    ).scalars()
    points: list[ChannelPoint] = []
    for row in rows:
        snap = _snap(row)
        found = next((c for c in (snap.channels if snap else []) if c.name == args.name), None)
        if found is None:
            continue
        points.append(
            ChannelPoint(
                seq=row.seq,
                created_at=row.created_at,
                model_version_id=row.model_version_id,
                spend=found.spend,
                contribution=found.contribution,
                roi=found.roi,
                mroi=found.mroi,
                zone=found.zone,
            )
        )
        if len(points) >= args.last_n:
            break
    return ChannelHistory(name=ch.name, label=ch.label, points=list(reversed(points)))


# ============================================================================ layout 2 tools


async def _panels(session: AsyncSession, dctx: DashboardContext) -> PanelList:
    spec = await _spec(session, dctx)
    if not isinstance(spec, DashboardSpecV2):
        return PanelList()
    return PanelList(panels=[panel_meta(p) for p in spec.panels])


async def _readout_or_latest(
    session: AsyncSession, dctx: DashboardContext, seq: int | None, kind: str | None = None
) -> m.DashboardReadout | None:
    if seq is not None:
        return await readouts.load_visible(session, dctx, seq)
    return await _latest_row(session, dctx, kind)


def _not_ready(row: m.DashboardReadout) -> MapiError | None:
    if row.status in ("queued", "running", "materialising"):
        return MapiError(
            f"readout {row.seq} is not ready yet: try again in a moment",
            code=ErrorCode.NOT_READY,
        )
    return None


async def _panel(session: AsyncSession, dctx: DashboardContext, args: PanelArgs) -> PanelReport:
    row = await _readout_or_latest(session, dctx, args.seq)
    if row is None:
        raise NotFoundError("readout", "latest")
    waiting = _not_ready(row)
    if waiting is not None:
        raise waiting
    snap = _snap(row)
    block = next((b for b in (snap.panels if snap else []) if b.panel_id == args.panel_id), None)
    if block is None:
        raise NotFoundError("panel", args.panel_id)
    return PanelReport(seq=row.seq, panel=block)


async def _accuracy(
    session: AsyncSession, dctx: DashboardContext, args: OptionalReadoutRef
) -> AccuracyReport:
    """A readout's accuracy as it shows it (without ``seq``: the latest published actuals
    readout). A layout 1 readout's accuracy reads as the error metrics."""
    row = await _readout_or_latest(session, dctx, args.seq, "actuals")
    if row is None:
        return AccuracyReport()
    snap = _snap(row)
    if snap is None:
        return AccuracyReport(seq=row.seq)
    detail = snap.accuracy_detail
    if detail is None and snap.accuracy is not None:
        detail = ErrorAccuracy.model_validate(snap.accuracy.model_dump())
    return AccuracyReport(seq=row.seq, detail=detail, history=list(snap.accuracy_history))


async def _inputs(session: AsyncSession, dctx: DashboardContext) -> InputsReport:
    """The caller's inputs and forms exactly as the dashboard view gives them."""
    view = await _dashboard(session, dctx)
    spec = await _spec(session, dctx)
    batch_cap = (
        spec.limits.batch_runs_per_day
        if isinstance(spec, DashboardSpecV2) and dctx.inputs.batch
        else None
    )
    return InputsReport(
        inputs=dctx.inputs,
        scenario_form=view.scenario_form,
        batch_form=view.batch_form,
        exact_runs_per_day=spec.limits.exact_runs_per_day,
        batch_runs_per_day=batch_cap,
    )


async def _own(
    session: AsyncSession, dctx: DashboardContext, seq: int, kind: str
) -> m.DashboardReadout:
    """The caller's own readout ``seq`` of ``kind`` (``404`` for anyone else's, analysts
    included: these tools read the caller's own rows only)."""
    row = await readouts.load_visible(session, dctx, seq)
    if (
        row.kind != kind
        or row.actor_kind != dctx.principal_kind
        or row.actor_id != dctx.principal_id
    ):
        raise NotFoundError(kind, str(seq))
    return row


async def _prediction(
    session: AsyncSession, dctx: DashboardContext, args: ReadoutRef
) -> PredictionReport:
    row = await _own(session, dctx, args.seq, "prediction")
    statuses = await refit_statuses(session, dctx.dashboard_id, [row.seq])
    snap = _snap(row)
    return PredictionReport(
        readout=readout_header(row, statuses.get(row.seq), mine=True),
        prediction=snap.prediction if snap else None,
    )


async def _batch(session: AsyncSession, dctx: DashboardContext, args: ReadoutRef) -> BatchReport:
    row = await _own(session, dctx, args.seq, "batch")
    statuses = await refit_statuses(session, dctx.dashboard_id, [row.seq])
    snap = _snap(row)
    return BatchReport(
        readout=readout_header(row, statuses.get(row.seq), mine=True),
        summary=snap.batch if snap else None,
    )
