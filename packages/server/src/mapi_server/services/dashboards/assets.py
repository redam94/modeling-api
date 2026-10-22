"""Pictures of a published layout 2 revision (v0.17, plan D4, §3.5).

A ``recipe`` panel whose recipe has no native chart intent
(:data:`~mapi_spec.dashboard_v2.RECIPE_INTENTS`) shows a **picture**: a plot of the pinned
model version (fit-time ``m.Plot``), rendered to PNG once per revision. Clients and share
links never see the plot document or a signed link, only the PNG bytes served same-origin.

* :func:`queue_pictures` (at publish, and after the automatic upgrade) submits one ``render``
  run per picture as the dashboard's publisher (the caller's context must be the owner's),
  at most :data:`MAX_PICTURES_PER_REVISION` per revision, each counted against the
  dashboard's ``limits.exact_runs_per_day``. Only embeddable plots (platform recipe
  provenance of a version that ran no uploaded code) are rendered; anything else is stored
  as a ``failed`` asset with a plain reason, so the readout shows "unavailable".
* :func:`finish_asset` turns a ``queued`` asset whose run finished into ``ready``: the PNG
  is copied (server side, write-once) from the run's scratch to the bundles bucket, checked
  (a PNG, at most :data:`~mapi_server.db.models.DASHBOARD_ASSET_MAX_BYTES`), and its size
  read from the PNG header. It runs lazily on read (:func:`revision_assets`,
  :func:`read_asset`), so no loop or run hook is needed.
* :func:`read_asset` returns the bytes of a ready asset of a dashboard the caller may read;
  an asset of another dashboard, project or tenant answers ``404`` like a missing one.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.dashboard_v2 import RECIPE_INTENTS, DashboardSpecV2, RecipePanel
from mapi_spec.errors import MapiError
from mapi_spec.jobs import render_output
from mapi_spec.run import RunState
from mapi_spec.sharing import ModelPlotRef
from mapi_spec.storage import NotFound, PreconditionFailed, check_segment
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.runs import now
from mapi_server.services.sharing import ran_uploaded_code, resolve_model_plot, submit_render

__all__ = [
    "ASSET_CONTENT_TYPE",
    "MAX_PICTURES_PER_REVISION",
    "PICTURE_SCALE",
    "PICTURE_WIDTH",
    "AssetBytes",
    "asset_key",
    "finish_asset",
    "picture_panels",
    "queue_pictures",
    "read_asset",
    "recipe_name",
    "revision_assets",
]

log = logging.getLogger(__name__)

MAX_PICTURES_PER_REVISION: Final = 4
ASSET_CONTENT_TYPE: Final = "image/png"
PICTURE_WIDTH: Final = 800
PICTURE_SCALE: Final = 2.0
"""A picture is rendered 800 CSS pixels wide at scale 2: at most 1600 pixels wide."""
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PROBLEMS: Final = {
    "provenance": "Only plots of platform recipes can be shown to clients.",
    "uploaded": "The model version ran uploaded code: its plots are not shown to clients.",
    "missing": "The model version has no such plot.",
    "limit": "The daily run limit of the dashboard was reached before this picture.",
    "failed": "The picture could not be drawn.",
    "large": "The picture is larger than 512 KiB.",
    "gone": "The drawn picture is no longer available.",
}


def asset_key(tenant_id: str, dashboard_id: str, asset_id: str) -> str:
    """``t/{tenant}/dashboards/{dashboard}/assets/{asset}.png`` in the bundles bucket."""
    return (
        f"t/{check_segment(tenant_id, 'tenant_id')}/dashboards/"
        f"{check_segment(dashboard_id, 'dashboard_id')}/assets/"
        f"{check_segment(asset_id, 'asset_id')}.png"
    )


def picture_panels(spec: DashboardSpecV2) -> list[RecipePanel]:
    """The recipe panels of ``spec`` that are pictures (no native intent)."""
    return [p for p in spec.panels if isinstance(p, RecipePanel) and p.recipe not in RECIPE_INTENTS]


def png_size(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG's IHDR chunk, ``None`` when it is not a PNG."""
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    if not (1 <= width <= 1 << 16 and 1 <= height <= 1 << 16):
        return None
    return int(width), int(height)


# ===================================================================================== queue


async def _runs_today(session: AsyncSession, dashboard_id: str) -> int:
    """Runs the dashboard spent in the last day: readouts that spent one (as the input caps
    count them) and pictures."""
    since = now() - timedelta(days=1)
    readouts = (
        await session.execute(
            select(func.count()).where(
                m.DashboardReadout.dashboard_id == dashboard_id,
                m.DashboardReadout.created_at > since,
                (m.DashboardReadout.cache_key.is_not(None))
                | (m.DashboardReadout.trigger == "manual"),
            )
        )
    ).scalar_one()
    pictures = (
        await session.execute(
            select(func.count()).where(
                m.DashboardAsset.dashboard_id == dashboard_id,
                m.DashboardAsset.created_at > since,
                m.DashboardAsset.run_id.is_not(None),
            )
        )
    ).scalar_one()
    return int(readouts) + int(pictures)


def recipe_name(recipe: str | None) -> str | None:
    """``core.fan@1.0.0`` → ``core.fan`` (panels name recipes without a version)."""
    return recipe.split("@", 1)[0] if recipe else None


async def _plot_of(session: AsyncSession, mv: m.ModelVersion, panel: RecipePanel) -> m.Plot | None:
    """The version's plot a picture panel shows: its ``plot_id`` (whose recipe must be the
    panel's), else the first plot of the panel's recipe."""
    if panel.plot_id is not None:
        row = await session.get(m.Plot, (mv.id, panel.plot_id))
        ok = row is not None and recipe_name(row.recipe) in (None, panel.recipe)
        return row if ok else None
    rows = (
        await session.execute(
            select(m.Plot)
            .where(
                m.Plot.model_version_id == mv.id,
                (m.Plot.recipe == panel.recipe)
                | m.Plot.recipe.startswith(panel.recipe + "@", autoescape=True),
            )
            .order_by(m.Plot.plot_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return rows


def _asset(
    rev: m.DashboardRevision, plot_id: str, recipe: str | None, **kw: object
) -> m.DashboardAsset:
    return m.DashboardAsset(
        id=new_id("dsa"),
        tenant_id=rev.tenant_id,
        project_id=rev.project_id,
        dashboard_id=rev.dashboard_id,
        revision_id=rev.id,
        plot_id=plot_id[:128],
        recipe=recipe,
        content_type=ASSET_CONTENT_TYPE,
        created_at=now(),
        **kw,
    )


async def queue_pictures(
    ctx: ServerContext,
    session: AsyncSession,
    revision: m.DashboardRevision,
    spec: DashboardSpecV2 | None = None,
) -> list[m.DashboardAsset]:
    """Queue the render runs of the revision's pictures (idempotent per revision and plot).

    ``ctx`` acts as the dashboard's publisher (the request of the publisher, or
    :func:`~mapi_server.services.dashboards.runas.owner_context`). A refusal (admission,
    quota, a plot that is not embeddable) never fails the publish: the asset is stored
    ``failed`` with a plain reason."""
    if spec is None:
        from mapi_spec.dashboard_v2 import parse_dashboard_spec

        parsed = parse_dashboard_spec(revision.spec)
        if not isinstance(parsed, DashboardSpecV2):
            return []
        spec = parsed
    panels = picture_panels(spec)
    if not panels:
        return []
    dash = await session.get(m.Dashboard, revision.dashboard_id)
    mv = await session.get(m.ModelVersion, revision.model_version_id)
    if dash is None or mv is None:  # pragma: no cover - written together
        return []
    existing = {
        a.plot_id
        for a in (
            await session.execute(
                select(m.DashboardAsset).where(m.DashboardAsset.revision_id == revision.id)
            )
        ).scalars()
    }
    spent = await _runs_today(session, dash.id)
    cap = spec.limits.exact_runs_per_day
    out: list[m.DashboardAsset] = []
    seen: set[str] = set()
    for panel in panels:
        if len(seen) >= MAX_PICTURES_PER_REVISION:
            break
        plot = await _plot_of(session, mv, panel)
        plot_id = plot.plot_id if plot is not None else (panel.plot_id or panel.recipe)
        if plot_id in seen or plot_id in existing:
            continue
        seen.add(plot_id)
        if plot is None:
            asset = _asset(revision, plot_id, panel.recipe, status="failed",
                           problem=_PROBLEMS["missing"])  # fmt: skip
        elif ran_uploaded_code(mv):
            asset = _asset(revision, plot_id, plot.recipe, status="failed",
                           problem=_PROBLEMS["uploaded"])  # fmt: skip
        elif spent >= cap:
            asset = _asset(revision, plot_id, plot.recipe, status="failed",
                           problem=_PROBLEMS["limit"])  # fmt: skip
        else:
            asset = await _render(ctx, session, revision, mv, plot)
            if asset.run_id is not None:
                spent += 1
        session.add(asset)
        out.append(asset)
    await session.flush()
    return out


async def _render(
    ctx: ServerContext,
    session: AsyncSession,
    revision: m.DashboardRevision,
    mv: m.ModelVersion,
    plot: m.Plot,
) -> m.DashboardAsset:
    ref = ModelPlotRef(model_id=mv.model_id, version=mv.version, plot_id=plot.plot_id)
    try:
        async with session.begin_nested():
            resolved = await resolve_model_plot(session, ctx, ref)
            if not resolved.embeddable:
                return _asset(revision, plot.plot_id, plot.recipe, status="failed",
                              problem=_PROBLEMS["provenance"])  # fmt: skip
            run = await submit_render(
                session, ctx, resolved, fmt="png", scale=PICTURE_SCALE, width=PICTURE_WIDTH
            )
    except MapiError as exc:
        log.info("dashboard %s: picture %s not queued: %s", revision.dashboard_id,
                 plot.plot_id, exc)  # fmt: skip
        return _asset(revision, plot.plot_id, plot.recipe, status="failed",
                      problem=str(exc)[:200] or _PROBLEMS["failed"])  # fmt: skip
    return _asset(revision, plot.plot_id, plot.recipe, status="queued", run_id=run.id)


# ==================================================================================== finish


def _fail(asset: m.DashboardAsset, why: str) -> None:
    asset.status = "failed"
    asset.problem = _PROBLEMS[why]


async def finish_asset(
    ctx: ServerContext, session: AsyncSession, asset: m.DashboardAsset
) -> m.DashboardAsset:
    """Settle a ``queued`` asset whose render run finished (no-op otherwise), and give the
    ready readouts of its revision that froze it as "being prepared" the settled block."""
    if asset.status != "queued" or asset.run_id is None:
        return asset
    await _settle(ctx, session, asset)
    if asset.status != "queued":
        from mapi_server.services.dashboards.readouts import patch_picture_readouts

        await patch_picture_readouts(session, asset)
    return asset


async def _settle(
    ctx: ServerContext, session: AsyncSession, asset: m.DashboardAsset
) -> m.DashboardAsset:
    run = await session.get(m.Run, asset.run_id)
    if run is None:
        _fail(asset, "failed")
        return asset
    try:
        state = RunState(run.state)
    except ValueError:  # pragma: no cover - a CHECK constraint holds the states
        return asset
    if state is not RunState.SUCCEEDED:
        if state.terminal:  # failed, cancelled, rejected, partial: no picture
            _fail(asset, "failed")
            await session.flush()
        return asset
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    outputs = (att.outputs if att is not None else None) or {}
    meta = outputs.get(render_output(asset.plot_id, "png"))
    if not isinstance(meta, dict):
        wanted = (run.payload or {}).get("plots") or []
        meta = outputs.get(render_output(str(wanted[0]), "png")) if wanted else None
    if not isinstance(meta, dict):
        _fail(asset, "failed")
        await session.flush()
        return asset
    if int(meta.get("size") or 0) > m.DASHBOARD_ASSET_MAX_BYTES:
        _fail(asset, "large")
        await session.flush()
        return asset
    bucket = ctx.buckets.bundles
    key = asset_key(asset.tenant_id, asset.dashboard_id, asset.id)
    try:
        copied = await ctx.store.copy(
            str(meta["bucket"]),
            str(meta["name"]),
            bucket,
            key,
            source_generation=int(meta["generation"]),
            if_generation_match=0,
        )
    except PreconditionFailed:
        copied = await ctx.store.stat(bucket, key)
    except NotFound:  # the run's scratch expired before anyone asked
        _fail(asset, "gone")
        await session.flush()
        return asset
    if copied.size > m.DASHBOARD_ASSET_MAX_BYTES or copied.size < 1:
        _fail(asset, "large")
        await session.flush()
        return asset
    head = await ctx.store.get(
        copied.bucket, copied.name, generation=copied.generation, byte_range=(0, 23)
    )
    size = png_size(head)
    if size is None:
        _fail(asset, "failed")
        await session.flush()
        return asset
    asset.width, asset.height = size
    asset.bucket, asset.name, asset.generation = copied.bucket, copied.name, copied.generation
    asset.bytes = copied.size
    asset.status = "ready"
    asset.ready_at = now()
    await session.flush()
    return asset


async def revision_assets(
    ctx: ServerContext, session: AsyncSession, revision_id: str
) -> dict[str, m.DashboardAsset]:
    """The assets of a revision by ``plot_id`` (queued ones settled first): what the readout
    builder turns into picture blocks (``ready``) or unavailable blocks."""
    rows = list(
        (
            await session.execute(
                select(m.DashboardAsset).where(m.DashboardAsset.revision_id == revision_id)
            )
        ).scalars()
    )
    for row in rows:
        await finish_asset(ctx, session, row)
    return {r.plot_id: r for r in rows}


@dataclass(frozen=True, slots=True)
class AssetBytes:
    """A ready picture: its bytes and the PNG size."""

    data: bytes
    width: int
    height: int
    content_type: str = ASSET_CONTENT_TYPE


async def read_asset(
    ctx: ServerContext,
    session: AsyncSession,
    dashboard_id: str,
    asset_id: str,
    *,
    revision_ids: Sequence[str] | None = None,
) -> AssetBytes:
    """The bytes of a ready asset of ``dashboard_id`` (``404`` otherwise, also while it is
    being drawn). The caller has already decided that it may read the dashboard;
    ``revision_ids`` narrows to these revisions (a share link: the published one)."""
    row = await session.get(m.DashboardAsset, asset_id)
    if row is None or row.dashboard_id != dashboard_id:
        raise NotFoundError("asset", asset_id)
    if revision_ids is not None and row.revision_id not in revision_ids:
        raise NotFoundError("asset", asset_id)
    await finish_asset(ctx, session, row)
    if (
        row.status != "ready"
        or row.bucket is None
        or row.name is None
        or row.generation is None
        or row.width is None
        or row.height is None
    ):
        raise NotFoundError("asset", asset_id)
    try:
        data = await ctx.store.get(
            row.bucket, row.name, generation=row.generation,
            max_bytes=m.DASHBOARD_ASSET_MAX_BYTES,
        )  # fmt: skip
    except NotFound:
        raise NotFoundError("asset", asset_id) from None
    return AssetBytes(data=data, width=row.width, height=row.height)
