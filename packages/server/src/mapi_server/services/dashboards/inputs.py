"""The no-refit inputs of a dashboard (v0.16, plan D8, D9, D12, D14, §3.4).

* **What-ifs.** :func:`grid_for` serves the pinned version's response grid (read only after
  the signed manifest verifies, size-capped, validated JSON; never unpickled), clipped to the
  dashboard's channels and slider ranges and cached per revision. :func:`evaluate_whatif`
  answers a spend plan on it server-side (no run, no write; the tool surface uses it).
  :func:`save_scenario` keeps a plan as a *private* readout: the grid result at once, or with
  ``exact`` a sandboxed score run (``counterfactual``) submitted as the publisher.
* **Actuals.** :func:`actuals_template` is the table an upload fills in;
  :func:`submit_actuals` checks the inline XrJSON the browser parsed (caps, the version's
  input contract, the KPI column, contiguous periods: ``ACTUALS_INVALID`` with pointers),
  stores it in object storage (never in Postgres) and scores it as the publisher:
  ``conditioned`` (a forecast with actuals) when the model can, else ``compared`` (a
  prediction over the uploaded periods; the model does not change). It may also *ask* for a
  refit (:mod:`.refits`): that never starts compute by itself.
* **Refresh.** :func:`refresh` queues a ``refresh`` readout (an editor's "Refresh now");
  the dashboard loop submits it as the publisher (debounced per dashboard).

Every run is a score of the pinned version, submitted through ``submit_model_run`` on
:func:`~mapi_server.services.dashboards.runas.owner_context` (quotas, kill switches,
placement and audit as for the owner), labelled with the readout. Cost guardrails (D14):
per dashboard and day (``limits.exact_runs_per_day``), per share link and hour, per client
user and hour - all counted from the readouts that spent a run, no new counter - and a
result cache that answers a repeated plan or upload with the earlier result.

v0.17: a layout 2 revision dispatches its actuals and template to :mod:`.actuals_v2` and its
queued baselines and refreshes to :func:`.cases.submit_baseline_v2`; the media planner inputs
here (grid, what-ifs, scenarios) answer ``DASHBOARD_INPUT_DISABLED`` for it.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.capabilities import OutputSelector, Representation, summary_allowed
from mapi_spec.dashboards import (
    MAX_ACTUALS_COLS,
    MAX_ACTUALS_ROWS,
    ActualsSubmit,
    ActualsTemplate,
    DashboardGrid,
    DashboardSpec,
    ScenarioSave,
    WhatIfRequest,
    WhatIfResult,
    readout_cache_key,
)
from mapi_spec.endpoints import (
    INPUT_INLINE_DATA,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    input_table,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.executor import RunKind
from mapi_spec.loader import canonical_json
from mapi_spec.schema import Role
from mapi_spec.spec import API_VERSION, FutureCovariates, Horizon, ScoreRequest
from mapi_spec.tenancy import ResourceKind
from mapi_spec.views import ResponseGridView
from mapi_spec.whatif import DEFAULT_GUIDED_OUTPUT, WhatIfScenario, scenario_score_request
from mapi_spec.xrjson import XrJsonDataset, check_xrjson_size, observed_labels
from pydantic import JsonValue
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.access import (
    AnySpec,
    DashboardContext,
    any_revision_spec,
    revision_spec,
)
from mapi_server.services.dashboards.crud import read_grid
from mapi_server.services.dashboards.readouts import (
    Actor,
    _lock_dashboard,
    new_readout,
    readout_labels,
    scenario_snapshot,
    whatif_result,
)
from mapi_server.services.dashboards.runas import owner_context, paused_error
from mapi_server.services.endpoints import (
    endpoint_schema_issues,
    live_capabilities_snapshot,
    store_inline_inputs,
)
from mapi_server.services.runs import now
from mapi_server.services.sharing import ran_uploaded_code
from mapi_server.services.submit import fit_dataset_ref, submit_model_run

__all__ = [
    "RUN_LIMIT_HOUR_MESSAGE",
    "RUN_LIMIT_MESSAGE",
    "actuals_template",
    "clear_grid_cache",
    "clip_grid",
    "evaluate_whatif",
    "grid_for",
    "refresh",
    "save_scenario",
    "submit_actuals",
    "submit_queued",
]

RUN_LIMIT_MESSAGE: Final = (
    "You reached today's limit for new results. Try again tomorrow or ask your analyst."
)
RUN_LIMIT_HOUR_MESSAGE: Final = (
    "You reached this hour's limit for new results. Try again later or ask your analyst."
)
_GRID_CACHE_MAX: Final = 64
_GRID_CACHE: dict[str, tuple[float, DashboardGrid]] = {}


def clear_grid_cache() -> None:
    """Forget the cached grids (tests; a revision's grid never changes otherwise)."""
    _GRID_CACHE.clear()


# ==================================================================================== loading


async def _parts(
    session: AsyncSession, dctx: DashboardContext
) -> tuple[m.Dashboard, m.DashboardRevision, DashboardSpec, m.ModelVersion]:
    dash = await session.get(m.Dashboard, dctx.dashboard_id)
    rev = await session.get(m.DashboardRevision, dctx.revision_id)
    if dash is None or rev is None or dash.status != "published":
        raise NotFoundError("dashboard", dctx.dashboard_id)
    mv = await session.get(m.ModelVersion, rev.model_version_id)
    if mv is None:  # pragma: no cover - versions are never deleted under a revision
        raise NotFoundError("model version", rev.model_version_id)
    return dash, rev, _planner_spec(rev), mv


def _planner_spec(rev: m.DashboardRevision) -> DashboardSpec:
    """The layout 1 media planner spec of a revision: the spec itself on layout 1, the v1
    shape of the planner part of an ``mmm_planner`` layout 2 spec (its channels, what-ifs and
    tiles, as ``readouts._planner_snapshot`` builds it), else ``DASHBOARD_INPUT_DISABLED``."""
    spec = any_revision_spec(rev)
    if isinstance(spec, DashboardSpec):
        return spec
    if spec.mmm is None:
        return revision_spec(rev)  # raises DASHBOARD_INPUT_DISABLED
    from mapi_spec.dashboard_v2 import kpi_of_subject

    return DashboardSpec.model_construct(
        title=spec.title,
        client_name=spec.client_name,
        summary=spec.summary,
        period_label=spec.period_label,  # type: ignore[arg-type]  # text only
        source=spec.source,
        kpi=kpi_of_subject(spec.subject),
        channels=list(spec.mmm.channels),
        tiles=list(spec.mmm.tiles),
        whatif=spec.mmm.whatif,
        actuals=spec.actuals,
        refresh=spec.refresh,
        limits=spec.limits,
        show_technical=spec.show_technical,
        note=spec.note,
    )


async def _is_v2(session: AsyncSession, dctx: DashboardContext) -> bool:
    """Is the caller's published revision a layout 2 spec (v0.17: its inputs dispatch to
    :mod:`.cases` and :mod:`.actuals_v2`)?"""
    rev = await session.get(m.DashboardRevision, dctx.revision_id)
    return rev is not None and not isinstance(any_revision_spec(rev), DashboardSpec)


def _disabled(what: str) -> MapiError:
    return MapiError(
        f"{what} is not available on this dashboard", code=ErrorCode.DASHBOARD_INPUT_DISABLED
    )


def _refuse_unsafe(mv: m.ModelVersion) -> None:
    if mv.status != "ready":
        raise MapiError(f"model version {mv.id} is not ready", code=ErrorCode.NOT_READY)
    if ran_uploaded_code(mv):
        raise MapiError(
            "the model version ran uploaded plugin code; dashboards do not run it",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )


# ======================================================================================= grid


def _keep(xs: list[float], lo: float, hi: float) -> list[int]:
    """Grid points inside ``[lo, hi]`` plus the nearest one beyond each end (interpolation
    needs the segment around every reachable multiplier)."""
    inside = [i for i, x in enumerate(xs) if lo - 1e-9 <= x <= hi + 1e-9]
    below = [i for i, x in enumerate(xs) if x < lo - 1e-9]
    above = [i for i, x in enumerate(xs) if x > hi + 1e-9]
    keep = set(inside)
    if below and (not inside or xs[inside[0]] > lo + 1e-9):
        keep.add(below[-1])
    if above and (not inside or xs[inside[-1]] < hi - 1e-9):
        keep.add(above[0])
    return sorted(keep)


def clip_grid(grid: ResponseGridView, spec: DashboardSpec) -> DashboardGrid:
    """The grid restricted to the dashboard's channels (its order) and slider ranges. The
    contribution at the current plan of the channels left out moves into ``baseline``, so the
    expected KPI stays the model's."""
    xs = list(grid.multipliers)
    lo = min(c.min_mult for c in spec.channels)
    hi = max(c.max_mult for c in spec.channels)
    ks = _keep(xs, lo, hi)
    one = next(i for i, x in enumerate(xs) if abs(x - 1.0) < 1e-9)
    names = [c.name for c in spec.channels if c.name in grid.channels]
    if not names:
        raise MapiError(
            "none of the dashboard's channels is in the model's response grid",
            code=ErrorCode.DASHBOARD_INPUT_DISABLED,
        )
    cs = [grid.channels.index(n) for n in names]
    dropped = [c for c in range(len(grid.channels)) if c not in cs]
    body: dict[str, Any] = {
        "format": grid.format,
        "kpi": grid.kpi,
        "kpi_units": grid.kpi_units,
        "spend_units": grid.spend_units,
        "periods": grid.periods,
        "channels": names,
        "multipliers": [xs[k] for k in ks],
        "draws": grid.draws,
        "contribution": [[[grid.contribution[d][c][k] for k in ks] for c in cs]
                         for d in range(grid.draws)],
        "spend": [[grid.spend[c][k] for k in ks] for c in cs],
        "current_spend": [grid.current_spend[c] for c in cs],
    }  # fmt: skip
    if grid.baseline is not None:
        body["baseline"] = [
            grid.baseline[d] + math.fsum(grid.contribution[d][c][one] for c in dropped)
            for d in range(grid.draws)
        ]
    if grid.geos and grid.contribution_by_geo is not None:
        body["geos"] = list(grid.geos)
        body["contribution_by_geo"] = [
            [[grid.contribution_by_geo[d][c][k] for k in ks] for c in cs] for d in range(grid.draws)
        ]
        if grid.spend_by_geo is not None:
            body["spend_by_geo"] = [[[g[c][k] for k in ks] for c in cs] for g in grid.spend_by_geo]
    return DashboardGrid.model_validate(body)


async def _grid(ctx: ServerContext, rev: m.DashboardRevision, spec: DashboardSpec,
                mv: m.ModelVersion) -> DashboardGrid:  # fmt: skip
    hit = _GRID_CACHE.get(rev.id)
    ttl = float(ctx.settings.embed_cache_s)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    raw = await read_grid(ctx, mv)
    if raw is None:
        raise _disabled("scenario planning (the model has no response grid)")
    clipped = clip_grid(raw, spec)
    if ttl > 0:
        if len(_GRID_CACHE) >= _GRID_CACHE_MAX:
            _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
        _GRID_CACHE[rev.id] = (time.monotonic() + ttl, clipped)
    return clipped


async def grid_for(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext
) -> DashboardGrid:
    """``GET …/grid``: the clipped grid (``DASHBOARD_INPUT_DISABLED`` without what-ifs or a
    grid). No run is made."""
    if not dctx.inputs.whatif:
        raise _disabled("scenario planning")
    _, rev, spec, mv = await _parts(session, dctx)
    _refuse_unsafe(mv)
    return await _grid(ctx, rev, spec, mv)


async def evaluate_whatif(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, body: WhatIfRequest
) -> WhatIfResult:
    """``POST …/whatif:evaluate``: a spend plan on the grid (``SCENARIO_OUT_OF_RANGE``
    outside the sliders' ranges). No run, nothing saved."""
    grid = await grid_for(ctx, session, dctx)
    _, _, spec, _ = await _parts(session, dctx)
    return whatif_result(spec, grid, body.multipliers)


# ===================================================================================== limits


def _spent() -> Any:
    """Readouts that spent (or will spend) a run: exact results and manual refreshes."""
    return or_(
        m.DashboardReadout.cache_key.is_not(None),
        m.DashboardReadout.trigger == "manual",
    )


async def _count(session: AsyncSession, *where: Any) -> tuple[int, datetime | None]:
    row = (
        await session.execute(
            select(func.count(), func.min(m.DashboardReadout.created_at)).where(_spent(), *where)
        )
    ).one()
    return int(row[0]), row[1]


def _limit(oldest: datetime | None, window: timedelta) -> ProblemWithHeaders:
    wait = (oldest + window - now()).total_seconds() if oldest is not None else 60.0
    return ProblemWithHeaders(
        RUN_LIMIT_MESSAGE if window >= timedelta(days=1) else RUN_LIMIT_HOUR_MESSAGE,
        code=ErrorCode.DASHBOARD_RUN_LIMIT,
        headers={"Retry-After": str(max(1, int(wait + 0.999)))},
    )


async def _limits_check(
    ctx: ServerContext, session: AsyncSession, dash: m.Dashboard, spec: AnySpec,
    actor: Actor,
) -> None:  # fmt: skip
    """``DASHBOARD_RUN_LIMIT`` (429, ``Retry-After``) over a cap of plan D14.

    The caller holds the dashboard row lock (``_lock_dashboard``) until the readout is
    inserted, so concurrent requests count each other's runs (a plain count under READ
    COMMITTED would let every one of them pass)."""
    at = now()
    day = timedelta(days=1)
    n, oldest = await _count(
        session,
        m.DashboardReadout.dashboard_id == dash.id,
        m.DashboardReadout.created_at > at - day,
    )
    # v0.17: picture renders of layout 2 panels spend runs of the same daily cap
    pictures, first_picture = (
        await session.execute(
            select(func.count(), func.min(m.DashboardAsset.created_at)).where(
                m.DashboardAsset.dashboard_id == dash.id,
                m.DashboardAsset.created_at > at - day,
                m.DashboardAsset.run_id.is_not(None),
            )
        )
    ).one()
    n += int(pictures)
    if first_picture is not None and (oldest is None or first_picture < oldest):
        oldest = first_picture
    if n >= spec.limits.exact_runs_per_day:
        raise _limit(oldest, day)
    hour = timedelta(hours=1)
    if actor.kind == "share":
        n, oldest = await _count(
            session,
            m.DashboardReadout.share_id == actor.id,
            m.DashboardReadout.created_at > at - hour,
        )
        if n >= ctx.settings.share_run_rate_per_hour:
            raise _limit(oldest, hour)
    elif actor.kind == "client":
        n, oldest = await _count(
            session,
            m.DashboardReadout.dashboard_id == dash.id,
            m.DashboardReadout.actor_kind == "client",
            m.DashboardReadout.actor_id == actor.id,
            m.DashboardReadout.created_at > at - hour,
        )
        if n >= ctx.settings.client_run_rate_per_hour:
            raise _limit(oldest, hour)


async def _cache_lookup(
    ctx: ServerContext, session: AsyncSession, dash: m.Dashboard, key: str, actor: Actor
) -> m.DashboardReadout | None:
    """A ready readout of the same revision and input from the last
    ``dashboard_cache_ttl_s``: the caller's own one, or another one whose result is copied
    into a new row of the caller's (so nobody sees anybody's private readout)."""
    since = now() - timedelta(seconds=ctx.settings.dashboard_cache_ttl_s)
    rows = list(
        (
            await session.execute(
                select(m.DashboardReadout)
                .where(
                    m.DashboardReadout.dashboard_id == dash.id,
                    m.DashboardReadout.cache_key == key,
                    m.DashboardReadout.status == "ready",
                    m.DashboardReadout.ready_at >= since,
                )
                .order_by(m.DashboardReadout.seq.desc())
                .limit(20)
            )
        ).scalars()
    )
    for row in rows:
        if row.actor_kind == actor.kind and row.actor_id == actor.id:
            return row
    return rows[0] if rows else None


async def _copy_for(
    ctx: ServerContext,
    session: AsyncSession,
    dash: m.Dashboard,
    rev: m.DashboardRevision,
    src: m.DashboardReadout,
    actor: Actor,
    visibility: str,
    *,
    title: str,
) -> m.DashboardReadout:
    """A cache hit for another actor: the same result in a row of the caller's own (no
    ``cache_key``: it spent no run). ``title`` is the caller's own (their label or the
    default text): the cache key never covers the label, so the source row's title (another
    person's private words) is never copied."""
    from mapi_spec.dashboards import ReadoutSnapshot

    snap = ReadoutSnapshot.model_validate(src.snapshot) if src.snapshot else None
    return await new_readout(
        session,
        dash,
        rev,
        kind=src.kind,  # type: ignore[arg-type]
        trigger=src.trigger,  # type: ignore[arg-type]
        visibility=visibility,  # type: ignore[arg-type]
        actor=actor,
        title=title,
        status="ready",
        model_version_id=src.model_version_id,
        model_ref=src.model_ref,
        run_ids=src.run_ids,
        scenario=src.scenario,
        input_ref=src.input_ref,
        input_fingerprint=src.input_fingerprint,
        snapshot=snap,
        expires_at=_private_expiry(ctx) if visibility == "private" else None,
    )


def _private_expiry(ctx: ServerContext) -> datetime:
    return now() + timedelta(days=ctx.settings.dashboard_private_ttl_days)


# ================================================================================ submission


async def _submit(
    session: AsyncSession,
    octx: ServerContext,
    row: m.DashboardReadout,
    request: ScoreRequest,
    *,
    extra_inputs: dict[str, Any] | None = None,
    inline_summary: Any = None,
) -> m.Run:
    """Submit the readout's score run as the owner (``octx``); the row becomes ``running``."""
    run = await submit_model_run(
        session,
        octx,
        kind=RunKind.SCORE,
        model_version_id=row.model_version_id,
        request=request,
        labels=readout_labels(row.dashboard_id, row.seq, row.trigger, row.share_id),  # type: ignore[arg-type]
        extra_inputs=extra_inputs,
        inline_summary=inline_summary,
    )
    row.run_ids = [run.id]
    if row.status == "queued":
        row.status = "running"
    await session.flush()
    return run


def readout_output(representation: Representation | str | None) -> OutputSelector:
    """The summaries a readout asks for: the mean and 90 % HDI of joint draws, else the
    centre and band the model's representation allows (a ``quantiles`` forecast, e.g.
    statsmodels: median and 90 % equal-tailed interval). The readout reads either."""
    try:
        rep = Representation(representation) if representation else Representation.DRAWS
    except ValueError:
        rep = Representation.DRAWS
    if rep is Representation.DRAWS:
        return DEFAULT_GUIDED_OUTPUT
    centre = next((s for s in ("mean", "median", "point") if summary_allowed(rep, s)), None)
    band = next((s for s in ("hdi_90", "eti_90", "interval") if summary_allowed(rep, s)), None)
    return DEFAULT_GUIDED_OUTPUT.model_copy(
        update={"summary": [s for s in (centre, band) if s is not None]}
    )


def _representation(mv: m.ModelVersion) -> str | None:
    rep = (mv.capabilities or {}).get("representation")
    return rep if isinstance(rep, str) else None


def baseline_request(
    model_ref: str, data: str, representation: Representation | str | None = None
) -> ScoreRequest:
    """The standing request of a baseline or refresh readout: the expected KPI over the
    fitted data (centre and 90 % interval, no draws; see :func:`readout_output`)."""
    return ScoreRequest.model_validate(
        {
            "apiVersion": API_VERSION,
            "kind": "ScoreRequest",
            "model": model_ref,
            "mode": "predict",
            "data": data,
            "output": readout_output(representation).model_dump(exclude_defaults=True),
        }
    )


async def exact_request(
    ctx: ServerContext,
    session: AsyncSession,
    rev: m.DashboardRevision,
    mv: m.ModelVersion,
    multipliers: Mapping[str, float],
) -> ScoreRequest:
    """The "Get exact result" request: the plan as a ``counterfactual`` over the fitted data, one
    ``multiply`` per changed channel on the model's spend input (the input variable with
    the ``spend`` role, along its categorical dim). ``DASHBOARD_INPUT_DISABLED`` for a
    model without one."""
    try:
        snapshot = await live_capabilities_snapshot(session, ctx.catalogs, mv)
    except MapiError:
        raise _disabled("an exact result (the model has no input contract)") from None
    schema = snapshot.input_schema
    spend = next((n for n, v in schema.vars.items() if v.role is Role.SPEND), None)
    channel = (
        next((d for d in schema.vars[spend].dims if schema.dims[d].kind == "categorical"), None)
        if spend is not None
        else None
    )
    if spend is None or channel is None:
        raise _disabled("an exact result (the model has no spend input per channel)")
    req = scenario_score_request(
        WhatIfScenario(multipliers=dict(multipliers)),
        rev.model_ref,
        spend_var=spend,
        channel_dim=channel,
    )
    body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    body["data"] = await fit_dataset_ref(session, mv)
    return ScoreRequest.model_validate(body)


async def submit_queued(ctx: ServerContext, session: AsyncSession, row: m.DashboardReadout) -> None:
    """The dashboard loop: submit a ``queued`` baseline or refresh readout as the owner.
    A refusal (paused owner, quota, an unsafe version) fails the readout with its problem."""
    dash = await session.get(m.Dashboard, row.dashboard_id)
    if dash is None:  # pragma: no cover - cascade
        return
    mv = await session.get(m.ModelVersion, row.model_version_id)
    try:
        if dash.status != "published":  # archived (or unpublished) after it was queued
            raise MapiError(
                "the dashboard is not published", code=ErrorCode.DASHBOARD_NOT_PUBLISHED
            )
        if mv is None:
            raise NotFoundError("model version", row.model_version_id)
        _refuse_unsafe(mv)
        rev = await session.get(m.DashboardRevision, row.revision_id)
        spec = any_revision_spec(rev) if rev is not None else None
        if spec is not None and not isinstance(spec, DashboardSpec):  # layout 2 (v0.17, D17)
            from mapi_server.services.dashboards.cases import submit_baseline_v2

            await submit_baseline_v2(ctx, session, row, dash, spec, mv)
            return
        octx = await owner_context(ctx, session, dash)
        async with session.begin_nested():
            data = await fit_dataset_ref(session, mv)
            request = baseline_request(row.model_ref, data, _representation(mv))
            await _submit(session, octx, row, request)
    except MapiError as exc:
        row.status = "failed"
        row.problem = exc.to_problem().model_dump(mode="json", exclude_none=True)
        await session.flush()


# ================================================================================== scenarios


async def save_scenario(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, body: ScenarioSave
) -> m.DashboardReadout:
    """``POST …/scenarios``: a private ``scenario`` readout. Without ``exact``: the grid
    result, ready at once (no run). With ``exact`` (the dashboard and link must allow it): a
    cached result, or a counterfactual score run as the publisher (``running``)."""
    if not dctx.inputs.whatif:
        raise _disabled("scenario planning")
    if body.exact and not dctx.inputs.exact:
        raise _disabled("an exact result")
    dash, rev, spec, mv = await _parts(session, dctx)
    _refuse_unsafe(mv)
    grid = await _grid(ctx, rev, spec, mv)
    result = whatif_result(spec, grid, body.multipliers)  # range checks for both kinds
    actor = Actor.of(dctx)
    title = body.label or _scenario_title(spec, body.multipliers)
    multipliers = {k: v for k, v in sorted(body.multipliers.items())}
    if not body.exact:
        return await new_readout(
            session,
            dash,
            rev,
            kind="scenario",
            trigger="whatif",
            visibility="private",
            actor=actor,
            title=title,
            status="ready",
            scenario=multipliers,
            snapshot=scenario_snapshot(spec, result, multipliers),
            expires_at=_private_expiry(ctx),
        )
    if dash.live_inputs == "paused":
        raise paused_error()
    request = await exact_request(ctx, session, rev, mv, multipliers)
    key = readout_cache_key(rev.id, {"scenario": multipliers})
    hit = await _cache_lookup(ctx, session, dash, key, actor)
    if hit is not None:
        if hit.actor_kind == actor.kind and hit.actor_id == actor.id:
            return hit
        return await _copy_for(ctx, session, dash, rev, hit, actor, "private", title=title)
    octx = await owner_context(ctx, session, dash)  # before the lock (it may pause)
    await _lock_dashboard(session, dash.id)  # count-and-insert under the row lock
    await _limits_check(ctx, session, dash, spec, actor)
    row = await new_readout(
        session,
        dash,
        rev,
        kind="scenario",
        trigger="whatif",
        visibility="private",
        actor=actor,
        title=title,
        status="queued",
        scenario=multipliers,
        cache_key=key,
        expires_at=_private_expiry(ctx),
    )
    await _submit(session, octx, row, request)
    return row


def _scenario_title(spec: DashboardSpec, multipliers: Mapping[str, float]) -> str:
    labels = {c.name: c.label for c in spec.channels}
    parts = []
    for name, x in sorted(multipliers.items()):
        if abs(x - 1.0) < 1e-9:
            continue
        pct = round((x - 1.0) * 100)
        sign = "+" if pct > 0 else "−"
        parts.append(f"{labels.get(name, name)} {sign}{abs(pct)} %")
    return ", ".join(parts)[:200] or "Current plan"


# ==================================================================================== actuals


def _kpi_column(spec: DashboardSpec, snapshot: EndpointSchemaSnapshot) -> str:
    """The input column that holds the KPI actuals: the KPI variable when it is a target of
    the input contract, else its first target."""
    targets = [n for n, v in snapshot.input_schema.vars.items() if v.role is Role.TARGET]
    if spec.kpi.variable in targets or not targets:
        return spec.kpi.variable
    return targets[0]


def _time_dim(snapshot: EndpointSchemaSnapshot) -> str | None:
    schema = snapshot.input_schema
    if snapshot.horizon_dim in schema.dims:
        return snapshot.horizon_dim
    return next((n for n, d in schema.dims.items() if d.kind == "time"), None)


async def actuals_template(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext
) -> ActualsTemplate:
    """``GET …/actuals/template``: the pinned version's input table, the KPI column and an
    example CSV (``DASHBOARD_INPUT_DISABLED`` without uploads or an input contract)."""
    if not dctx.inputs.actuals:
        raise _disabled("uploading numbers")
    if await _is_v2(session, dctx):
        from mapi_server.services.dashboards.actuals_v2 import actuals_template_v2

        return await actuals_template_v2(ctx, session, dctx)
    _, _, spec, mv = await _parts(session, dctx)
    _refuse_unsafe(mv)
    try:
        snapshot = await live_capabilities_snapshot(session, ctx.catalogs, mv)
    except MapiError:
        raise _disabled("uploading numbers (the model has no input contract)") from None
    table = input_table(snapshot, periods=8)
    if table is None:
        raise _disabled("uploading numbers (the model has no input table)")
    head = table.header
    lines = [",".join(head)] + [
        ",".join("" if v is None else str(v) for v in row) for row in table.rows
    ]
    return ActualsTemplate(
        mode=dctx.inputs.actuals_mode or "compared",
        input_table=table,
        kpi_column=_kpi_column(spec, snapshot),
        example_csv=("\n".join(lines) + "\n")[:65_536],
        max_rows=MAX_ACTUALS_ROWS,
    )


def _invalid(message: str, pointer: str, issues: list[Issue] | None = None) -> MapiError:
    return MapiError(
        message,
        code=ErrorCode.ACTUALS_INVALID,
        pointer=pointer,
        issues=issues or [Issue(code=ErrorCode.ACTUALS_INVALID, pointer=pointer, message=message)],
    )


def _actuals_call(
    payload: XrJsonDataset, snapshot: EndpointSchemaSnapshot, mode: str, kpi: str,
    hdim: str | None,
) -> EndpointScoreCall:  # fmt: skip
    output = readout_output(snapshot.representation)
    if mode == "conditioned" and hdim is not None:
        labels = payload.coords[hdim].data if hdim in payload.coords else []
        n = len(labels) if isinstance(labels, list) else 0
        seen = set(map(str, observed_labels(payload, kpi, hdim) or []))
        last = max((k for k, v in enumerate(labels or []) if str(v) in seen), default=-1)  # type: ignore[arg-type]
        return EndpointScoreCall(
            mode="forecast",
            horizon=Horizon(dim=hdim, periods=max(1, n - 1 - last)),
            data=payload,
            future_covariates=FutureCovariates.CARRY_LAST,
            output=output,
        )
    return EndpointScoreCall(mode="predict", data=payload, output=output)


def _check_actuals(
    payload: XrJsonDataset, snapshot: EndpointSchemaSnapshot, kpi: str, hdim: str | None
) -> None:
    check_xrjson_size(payload.model_dump(mode="json"))
    if len(payload.data_vars) > MAX_ACTUALS_COLS:
        raise _invalid(
            f"the upload has {len(payload.data_vars)} columns (max {MAX_ACTUALS_COLS})",
            "/inputs/data_vars",
        )
    if kpi not in payload.data_vars:
        raise _invalid(
            f"the upload has no column {kpi!r} (the result to compare)",
            f"/inputs/data_vars/{kpi}",
        )
    if hdim is not None:
        coord = payload.coords.get(hdim)
        if coord is None:
            raise _invalid(
                f"the upload has no column {hdim!r} (the periods)", f"/inputs/coords/{hdim}"
            )
        rows = len(coord.data) if isinstance(coord.data, list) else 1
        if rows > MAX_ACTUALS_ROWS:
            raise _invalid(
                f"the upload has {rows} periods (max {MAX_ACTUALS_ROWS})", f"/inputs/coords/{hdim}"
            )


async def submit_actuals(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, body: ActualsSubmit
) -> m.DashboardReadout:
    """``POST …/actuals``: validate, store and score new actuals as the publisher. The
    readout is ``published`` when the dashboard publishes uploads automatically, else
    ``pending_review`` (the uploader sees it at once; an editor publishes it). With
    ``request_refit`` (when allowed) an open refit request is recorded too."""
    if not dctx.inputs.actuals:
        raise _disabled("uploading numbers")
    if body.request_refit and not dctx.inputs.refit_requests:
        raise _disabled("asking for a refit")
    if await _is_v2(session, dctx):
        from mapi_server.services.dashboards.actuals_v2 import submit_actuals_v2

        return await submit_actuals_v2(ctx, session, dctx, body)
    dash, rev, spec, mv = await _parts(session, dctx)
    _refuse_unsafe(mv)
    if dash.live_inputs == "paused":
        raise paused_error()
    snapshot = await live_capabilities_snapshot(session, ctx.catalogs, mv)
    kpi = _kpi_column(spec, snapshot)
    hdim = _time_dim(snapshot)
    _check_actuals(body.inputs, snapshot, kpi, hdim)
    mode = dctx.inputs.actuals_mode or "compared"
    call = _actuals_call(body.inputs, snapshot, mode, kpi, hdim)
    issues, summary = endpoint_schema_issues(call, snapshot)
    if hdim is not None and summary is not None:
        cs = summary.coords.get(hdim)
        if cs is not None and not cs.contiguous:
            issues.append(
                Issue(
                    code=ErrorCode.ACTUALS_INVALID,
                    pointer=f"/data/coords/{hdim}",
                    message="the periods must follow each other without gaps",
                )
            )
    errors = [i for i in issues if i.severity is Severity.ERROR]
    if errors:
        moved = [
            i.model_copy(update={"pointer": "/inputs" + i.pointer.removeprefix("/data")})
            for i in errors
        ]
        raise _invalid(f"the numbers cannot be used: {moved[0].message}", moved[0].pointer, moved)
    fingerprint = (
        "sha256:"
        + hashlib.sha256(canonical_json(body.inputs.model_dump(mode="json")).encode()).hexdigest()
    )
    key = readout_cache_key(rev.id, fingerprint)
    actor = Actor.of(dctx)
    visibility = "published" if spec.actuals.publish == "auto" else "pending_review"
    title = body.label or f"Numbers sent on {now():%d/%m/%Y}"
    hit = await _cache_lookup(ctx, session, dash, key, actor)
    if hit is not None and hit.kind == "actuals":
        row = (
            hit
            if hit.actor_kind == actor.kind and hit.actor_id == actor.id
            else await _copy_for(ctx, session, dash, rev, hit, actor, visibility, title=title)
        )
    else:
        octx = await owner_context(ctx, session, dash)  # before the lock (it may pause)
        await _lock_dashboard(session, dash.id)  # count-and-insert under the row lock
        await _limits_check(ctx, session, dash, spec, actor)
        stored = await store_inline_inputs(ctx, octx.scope, call)
        ref = dict(stored[INPUT_INLINE_DATA])
        row = await new_readout(
            session,
            dash,
            rev,
            kind="actuals",
            trigger="actuals",
            visibility=visibility,  # type: ignore[arg-type]
            actor=actor,
            title=title,
            cache_key=key,
            input_ref={**ref, "kpi_column": kpi, "time_dim": hdim},
            input_fingerprint=fingerprint,
        )
        request = call.to_score_request(rev.model_ref)
        await _submit(session, octx, row, request, extra_inputs=stored, inline_summary=summary)
    await _audit(
        ctx,
        session,
        dctx,
        AuditAction.DASHBOARD_ACTUALS_SUBMIT,
        {"seq": row.seq, "mode": mode, "fingerprint": fingerprint},
    )
    if body.request_refit:
        from mapi_server.services.dashboards.refits import request_refit

        await request_refit(ctx, session, dctx, dash, row)
    return row


async def _audit(
    ctx: ServerContext,
    session: AsyncSession,
    dctx: DashboardContext,
    action: AuditAction,
    details: dict[str, JsonValue],
) -> None:
    """Audit a dashboard input: a member's or client's as their request, a share link's
    with the link as the actor."""
    auth = ctx.request
    if dctx.principal_kind != "share" and auth is not None and auth.context is not None:
        ev = auth.audit(ctx, action, ResourceKind.DASHBOARD, resource_id=dctx.dashboard_id,
                        details=details)  # fmt: skip
    else:
        ev = make_audit_event(
            action=action,
            resource_kind=ResourceKind.DASHBOARD,
            outcome=AuditOutcome.SUCCESS,
            ts=now(),
            fingerprint_key=ctx.secrets.audit_key,
            tenant_id=dctx.tenant_id,
            project_id=dctx.project_id,
            actor_id=dctx.principal_id,
            resource_id=dctx.dashboard_id,
            client_ip=auth.client_ip if auth is not None else None,
            details={**details, "share_id": dctx.principal_id},
        )
    await write_audit(ctx.db, [ev], session=session)


# ==================================================================================== refresh


async def refresh(
    ctx: ServerContext, session: AsyncSession, dashboard: m.Dashboard, user: str | None
) -> m.DashboardReadout:
    """``POST …:refresh`` (editors): queue a ``refresh`` readout of the published revision
    (the loop submits it as the publisher). A refresh asked for within
    ``dashboard_refresh_debounce_s`` of the last one answers with that one."""
    if dashboard.status != "published" or dashboard.published_revision_id is None:
        raise MapiError("publish the dashboard first", code=ErrorCode.DASHBOARD_NOT_PUBLISHED)
    if dashboard.live_inputs == "paused":
        raise paused_error()
    rev = await session.get(m.DashboardRevision, dashboard.published_revision_id)
    assert rev is not None
    spec = any_revision_spec(rev)  # either layout (v0.17)
    await _lock_dashboard(session, dashboard.id)  # debounce, count and insert serialised
    since = now() - timedelta(seconds=ctx.settings.dashboard_refresh_debounce_s)
    recent = (
        await session.execute(
            select(m.DashboardReadout)
            .where(
                m.DashboardReadout.dashboard_id == dashboard.id,
                m.DashboardReadout.kind == "refresh",
                m.DashboardReadout.revision_id == rev.id,
                m.DashboardReadout.created_at >= since,
                m.DashboardReadout.status != "failed",
            )
            .order_by(m.DashboardReadout.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent is not None:
        return recent
    actor = Actor("member", user)
    await _limits_check(ctx, session, dashboard, spec, actor)
    return await new_readout(
        session,
        dashboard,
        rev,
        kind="refresh",
        trigger="manual",
        visibility="published",
        actor=actor,
        title=f"Refreshed on {now():%d/%m/%Y}",
    )
