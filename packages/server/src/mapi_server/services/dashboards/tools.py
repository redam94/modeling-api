"""The read-only dashboard tool surface, served (v0.16, plan D18 and I-3).

:func:`call_tool` runs one tool of :data:`mapi_spec.dashboard_tools.DASHBOARD_TOOLS` for a
:class:`~mapi_server.services.dashboards.access.DashboardContext` (a member, a client user or
a share-link session). No tool submits a run or writes a row; private readouts are seen only
by their actor and by analysts. The results are the DTOs the HTTP routes return - the
member, client and share read routes call these functions, so the surface v0.17's chat and
MCP adapters map onto is the one the portal uses now.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mapi_spec._dto import ResponseModel
from mapi_spec.dashboard_tools import (
    ChannelArgs,
    ChannelDelta,
    ChannelHistory,
    ChannelPoint,
    CompareArgs,
    ExplainArgs,
    ListReadoutsArgs,
    NoArgs,
    ReadoutComparison,
    ReadoutRef,
    TileDelta,
    explain,
    tool,
)
from mapi_spec.dashboards import (
    ChannelReadout,
    ClientDashboard,
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
    DashboardContext,
    client_dashboard,
    readout_dto,
    readout_header,
    refit_statuses,
    revision_spec,
)

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


async def _dashboard(session: AsyncSession, dctx: DashboardContext) -> ClientDashboard:
    row, rev = await _parts(session, dctx)
    return await client_dashboard(session, row, rev, dctx.inputs)


async def _latest(session: AsyncSession, dctx: DashboardContext) -> Readout:
    row = (
        await session.execute(
            select(m.DashboardReadout)
            .where(
                m.DashboardReadout.dashboard_id == dctx.dashboard_id,
                m.DashboardReadout.visibility == "published",
                m.DashboardReadout.status == "ready",
            )
            .order_by(m.DashboardReadout.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("readout", "latest")
    statuses = await refit_statuses(session, dctx.dashboard_id, [row.seq])
    return readout_dto(row, statuses.get(row.seq), analyst=dctx.is_analyst)


def _snap(row: m.DashboardReadout) -> ReadoutSnapshot | None:
    if row.status != "ready" or row.snapshot is None:
        return None
    return ReadoutSnapshot.model_validate(row.snapshot)


def _delta(a: float | None, b: float | None) -> float | None:
    return b - a if a is not None and b is not None else None


async def _compare(
    session: AsyncSession, dctx: DashboardContext, args: CompareArgs
) -> ReadoutComparison:
    _, rev = await _parts(session, dctx)
    spec = revision_spec(rev)
    ra = await readouts.load_visible(session, dctx, args.a_seq)
    rb = await readouts.load_visible(session, dctx, args.b_seq)
    sa, sb = _snap(ra), _snap(rb)
    ta: dict[str, KpiValue] = {t.key: t for t in (sa.tiles if sa else [])}
    tb: dict[str, KpiValue] = {t.key: t for t in (sb.tiles if sb else [])}
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
    for ch in spec.channels:
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
    expected = next((t for t in tiles if t.key == "expected" and t.delta is not None), None)
    noun = readouts.kpi_noun(spec)
    if expected is not None and expected.delta is not None:
        word = "more" if expected.delta > 0 else "fewer" if spec.kpi.unit == "count" else "less"
        lead = (
            f"Readout {args.b_seq} expects {readouts.fmt(abs(expected.delta), spec.kpi.decimals)} "
            f"{word} {noun} than readout {args.a_seq}."
            if abs(expected.delta) > 0
            else f"Readouts {args.a_seq} and {args.b_seq} expect the same result."
        )
    else:
        lead = f"Readout {args.b_seq} compared with readout {args.a_seq}."
    if ra.model_version_id != rb.model_version_id:
        lead += " The model changed between the two readouts."
    statuses = await refit_statuses(session, dctx.dashboard_id, [ra.seq, rb.seq])
    return ReadoutComparison(
        a=readout_header(ra, statuses.get(ra.seq)),
        b=readout_header(rb, statuses.get(rb.seq)),
        same_model=ra.model_version_id == rb.model_version_id,
        tiles=tiles,
        channels=channels[:40],
        lead=lead[:400],
    )


async def _channel(
    session: AsyncSession, dctx: DashboardContext, args: ChannelArgs
) -> ChannelHistory:
    _, rev = await _parts(session, dctx)
    spec = revision_spec(rev)
    ch = next((c for c in spec.channels if c.name == args.name), None)
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
