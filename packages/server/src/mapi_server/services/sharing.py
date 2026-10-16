"""Saved plots, static rendering and embed links (plan §10.3 items 4-5; M3).

Everything here is JSON in the control plane: plots are re-sanitized with the pure-JSON
sanitizer (:func:`mapi_plots.sanitize.sanitize_plot`) whenever they are saved, rendered or
served. Arrow datasets are never decoded here:

* a **render** run gets the sanitized plot as a ``plots.json`` input written by the API and
  each ``data_ref`` dataset as its own generation-pinned input (``plotdata.<n>``), read from
  the model version's bundle only after its signed manifest verifies; the sandbox does the
  decoding and the drawing (vl-convert, no network);
* an **embed** view inlines ``data_ref`` datasets as base64 of the verified bundle object's
  bytes (no parsing), capped so the result is a valid :class:`~mapi_spec.plots.InlineArrow`.

Only plots of platform recipes saved from a model version are embeddable
(:func:`mapi_spec.sharing.embeddable`); user-authored specs are saved with ``author``
provenance whatever they claim, so they can never pass as recipes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.jobs import INPUT_PLOTS, RenderJob, plot_data_input, render_output
from mapi_spec.loader import spec_hash
from mapi_spec.plots import MAX_INLINE_ARROW_B64, DataRef, InlineArrow, PlotSpec
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.sharing import (
    EMBED_TOKEN_RE,
    EmbedView,
    ModelPlotRef,
    PlotEmbed,
    PlotEmbedCreate,
    RenderFormat,
    SavedPlot,
    SavedPlotCreate,
    check_render_pixels,
    embeddable,
    generate_embed_token,
)
from mapi_spec.storage import NotFound, PreconditionFailed, check_segment
from mapi_spec.tenancy import Action, ResourceKind, TenantStatus
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import write_audit
from mapi_server.services.bundles import verified_bundle_object, verified_manifest
from mapi_server.services.quotas import charge_egress, count_requests
from mapi_server.services.runs import apply_transition, create_run, now
from mapi_server.services.runspec import StoredRef
from mapi_server.services.submit import admit

__all__ = [
    "EMBED_DATASET_MAX_BYTES",
    "EMBED_IMAGE_NAME",
    "ResolvedPlot",
    "create_embed",
    "create_saved_plot",
    "delete_saved_plot",
    "embed_dto",
    "embed_image_key",
    "embed_status",
    "embed_token_hash",
    "embed_view",
    "load_saved_plot",
    "render_input_key",
    "resolve_render_request",
    "revoke_embed",
    "saved_plot_dto",
    "submit_render",
]

log = logging.getLogger(__name__)

EMBED_DATASET_MAX_BYTES: Final = (MAX_INLINE_ARROW_B64 // 4) * 3
"""The largest dataset an embed can inline (its base64 must fit an ``InlineArrow``)."""
EMBED_IMAGE_NAME: Final = "plot.png"
_TOKEN_PURPOSE: Final = b"mapi/embed-token/v1:"
_RENDER_CLASS: Final = ResourceClass.S
_FALLBACK_SCALE: Final = 2.0


def _sanitized(doc: Any, pointer: str) -> PlotSpec:
    """Sanitize a plot document; ``PLOT_UNSAFE`` (422) with the sanitizer's issues."""
    try:
        return sanitize_plot(doc).plot
    except SanitizeError as exc:
        raise MapiError(
            exc.detail, code=ErrorCode.PLOT_UNSAFE, pointer=pointer, issues=exc.issues
        ) from None
    except MapiError as exc:  # envelope errors (not a PlotSpec)
        raise MapiError(
            exc.detail, code=ErrorCode.PLOT_UNSAFE, pointer=pointer, issues=exc.issues
        ) from None
    except ValidationError as exc:
        raise MapiError(
            "not a valid PlotSpec",
            code=ErrorCode.PLOT_UNSAFE,
            pointer=pointer,
            untrusted_text=str(exc)[:4000],
        ) from None


def _dump(plot: PlotSpec) -> dict[str, Any]:
    return plot.model_dump(mode="json", by_alias=True, exclude_none=True)


# ----------------------------------------------------------------------------- resolving


def ran_uploaded_code(mv: m.ModelVersion | None) -> bool:
    """Was ``mv`` fitted with uploaded plugin code (an overlay)? Its worker wrote the plot
    documents, provenance included, so none of them can count as a platform recipe (plan
    §10.3 item 4): the server decides from the version's lineage, never from the plot."""
    return mv is not None and bool((mv.lineage or {}).get("overlay_hash"))


async def uploaded_code_versions(session: AsyncSession, ids: Iterable[str | None]) -> set[str]:
    """Which of these model versions ran uploaded code (see :func:`ran_uploaded_code`)."""
    wanted = {i for i in ids if i}
    if not wanted:
        return set()
    rows = await session.execute(
        select(m.ModelVersion.id, m.ModelVersion.lineage).where(m.ModelVersion.id.in_(wanted))
    )
    return {mv_id for mv_id, lineage in rows if (lineage or {}).get("overlay_hash")}


@dataclass(frozen=True, slots=True)
class ResolvedPlot:
    """A sanitized plot and where its ``data_ref`` datasets live (a verified model version)."""

    plot: PlotSpec
    model_version: m.ModelVersion | None
    source_kind: str
    provenance: str
    recipe: str | None
    source: ModelPlotRef | None = None
    saved_plot_id: str | None = None

    @property
    def embeddable(self) -> bool:
        return embeddable(self.source_kind, self.provenance, self.recipe) and not (
            ran_uploaded_code(self.model_version)
        )


async def _model_version(
    session: AsyncSession, ctx: ServerContext, ref: ModelPlotRef
) -> m.ModelVersion:
    sc = ctx.scope
    model = await session.get(m.Model, ref.model_id)
    if model is None or model.tenant_id != sc.tenant_id or model.project_id != sc.project_id:
        raise NotFoundError("model", ref.model_id)
    mv = (
        await session.execute(
            select(m.ModelVersion).where(
                m.ModelVersion.model_id == model.id, m.ModelVersion.version == ref.version
            )
        )
    ).scalar_one_or_none()
    if mv is None:
        raise NotFoundError("model version", f"{ref.model_id}@v{ref.version}")
    return mv


async def resolve_model_plot(
    session: AsyncSession, ctx: ServerContext, ref: ModelPlotRef
) -> ResolvedPlot:
    """A model version's plot, after its signed manifest verifies (re-sanitized)."""
    mv = await _model_version(session, ctx, ref)
    verified_manifest(ctx.signer, mv)
    row = await session.get(m.Plot, (mv.id, ref.plot_id))
    if row is None:
        raise NotFoundError("plot", ref.plot_id)
    plot = _sanitized(row.spec, "/source")
    provenance = plot.provenance.source
    if provenance == "recipe" and ran_uploaded_code(mv):
        provenance = "plugin"  # what the plot document claims cannot be trusted
    return ResolvedPlot(
        plot=plot,
        model_version=mv,
        source_kind="model_plot",
        provenance=provenance,
        recipe=plot.provenance.recipe,
        source=ref,
    )


def resolve_authored(plot: PlotSpec, author: str | None, pointer: str) -> ResolvedPlot:
    """A user-authored plot: sanitized, inline data only, ``author`` provenance whatever the
    document claims (so a hand-written spec never passes as a platform recipe)."""
    clean = _sanitized(_dump(plot), pointer)
    refs = [n for n, d in clean.datasets.items() if isinstance(d, DataRef)]
    if refs:
        raise MapiError(
            "user-authored plots carry their data inline (data_ref datasets belong to a "
            "model version: save that plot with 'source' instead)",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"{pointer}/datasets/{refs[0]}",
        )
    provenance = clean.provenance.model_copy(
        update={
            "recipe": None,
            "plugin": None,
            "bundle_id": None,
            "author": (author or "unknown")[:128],
        }
    )
    clean = clean.model_copy(update={"provenance": provenance})
    return ResolvedPlot(
        plot=clean, model_version=None, source_kind="spec", provenance="author", recipe=None
    )


async def resolve_saved(
    session: AsyncSession, ctx: ServerContext, row: m.SavedPlot
) -> ResolvedPlot:
    plot = _sanitized(row.spec, "/saved_plot_id")
    mv: m.ModelVersion | None = None
    if row.model_version_id is not None:
        mv = await session.get(m.ModelVersion, row.model_version_id)
        if mv is None:  # pragma: no cover - the foreign key cascades
            raise NotFoundError("model version", row.model_version_id)
        verified_manifest(ctx.signer, mv)
    return ResolvedPlot(
        plot=plot,
        model_version=mv,
        source_kind=row.source_kind,
        provenance=row.provenance,
        recipe=row.recipe,
        source=ModelPlotRef.model_validate(row.source) if row.source else None,
        saved_plot_id=row.id,
    )


# ----------------------------------------------------------------------------- saved plots


def saved_plot_dto(row: m.SavedPlot, *, with_spec: bool, uploaded_code: bool = False) -> SavedPlot:
    """``uploaded_code``: the plot's model version ran uploaded code (never embeddable)."""
    return SavedPlot(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        title=row.title,
        description=row.description,
        kind=row.kind,
        source_kind=row.source_kind,
        source=ModelPlotRef.model_validate(row.source) if row.source else None,
        recipe=row.recipe,
        provenance=row.provenance,
        embeddable=embeddable(row.source_kind, row.provenance, row.recipe) and not uploaded_code,
        spec=sanitize_plot(row.spec).plot if with_spec else None,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def load_saved_plot(
    session: AsyncSession, ctx: ServerContext, plot_id: str, *, for_update: bool = False
) -> m.SavedPlot:
    sc = ctx.scope
    stmt = select(m.SavedPlot).where(
        m.SavedPlot.id == plot_id,
        m.SavedPlot.tenant_id == sc.tenant_id,
        m.SavedPlot.project_id == sc.project_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("plot", plot_id)
    return row


async def create_saved_plot(
    session: AsyncSession, ctx: ServerContext, body: SavedPlotCreate
) -> m.SavedPlot:
    """``POST /v1/plots``: sanitize (and for a model plot verify) before anything is stored."""
    user = ctx.request.user_id if ctx.request is not None else None
    if body.source is not None:
        resolved = await resolve_model_plot(session, ctx, body.source)
    else:
        assert body.spec is not None
        resolved = resolve_authored(body.spec, user, "/spec")
    ts = now()
    sc = ctx.scope
    row = m.SavedPlot(
        id=new_id("plt"),
        tenant_id=sc.tenant_id,
        project_id=sc.project_id,
        title=body.title,
        description=body.description,
        kind=resolved.plot.kind,
        source_kind=resolved.source_kind,
        model_version_id=resolved.model_version.id if resolved.model_version else None,
        source=resolved.source.model_dump(mode="json") if resolved.source else None,
        recipe=resolved.recipe,
        provenance=resolved.provenance,
        spec=_dump(resolved.plot),
        created_by=user,
        created_at=ts,
        updated_at=ts,
    )
    session.add(row)
    await session.flush()
    return row


async def delete_saved_plot(session: AsyncSession, ctx: ServerContext, plot_id: str) -> None:
    """Delete a saved plot; its embeds go with it (their links answer 404 from now on)."""
    row = await load_saved_plot(session, ctx, plot_id, for_update=True)
    embeds = list(
        (await session.execute(select(m.PlotEmbed).where(m.PlotEmbed.plot_id == row.id))).scalars()
    )
    await session.delete(row)
    await session.flush()
    for embed in embeds:
        await _drop_image(ctx, embed)


# ----------------------------------------------------------------------------- rendering


def render_input_key(tenant_id: str, run_id: str) -> str:
    """Where the API writes a render run's ``plots.json`` (runs bucket, outside every attempt
    prefix, so no attempt's create-only upload policy can touch it)."""
    return (
        f"t/{check_segment(tenant_id, 'tenant_id')}/runs/"
        f"{check_segment(run_id, 'run_id')}/inputs/plots.json"
    )


async def resolve_render_request(
    session: AsyncSession,
    ctx: ServerContext,
    *,
    plot: PlotSpec | None,
    saved_plot_id: str | None,
    source: ModelPlotRef | None,
) -> ResolvedPlot:
    if saved_plot_id is not None:
        return await resolve_saved(session, ctx, await load_saved_plot(session, ctx, saved_plot_id))
    if source is not None:
        return await resolve_model_plot(session, ctx, source)
    assert plot is not None
    user = ctx.request.user_id if ctx.request is not None else None
    return resolve_authored(plot, user, "/plot")


_DEFAULT_SIZE: Final = {"vega-lite": (200, 200), "plotly": (700, 500)}
"""The renderers' default sizes (CSS pixels) of a plot that names none: a lower bound."""


def _check_pixels(
    plot: PlotSpec, fmt: str, scale: float, width: int | None, height: int | None
) -> None:
    """Refuse up front a raster over ``MAX_RENDER_PIXELS``: the overrides, else the plot's own
    top-level size, else the renderer's default (the runner measures the laid-out size
    again before it draws)."""
    if fmt == "svg":
        return
    spec: Any = plot.spec
    box = spec.get("layout") if plot.kind == "plotly" and isinstance(spec, dict) else spec
    own = box if isinstance(box, dict) else {}
    dw, dh = _DEFAULT_SIZE.get(plot.kind, (200, 200))

    def size(override: int | None, key: str, default: int) -> float:
        if override is not None:
            return float(override)
        v = own.get(key)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else default

    check_render_pixels(size(width, "width", dw), size(height, "height", dh), scale)


async def submit_render(
    session: AsyncSession,
    ctx: ServerContext,
    resolved: ResolvedPlot,
    *,
    fmt: RenderFormat,
    scale: float,
    width: int | None = None,
    height: int | None = None,
) -> m.Run:
    """Queue a ``render`` run of one sanitized plot (admission: kill switches and quotas).

    The run gets no ``model_version_id``: its inputs are not the bundle's own ``plots.json``
    but a document the API wrote, plus the bundle's data objects, each pinned here after the
    signed manifest verified.
    """
    if resolved.plot.kind == "plotly" and not ctx.settings.render_plotly_enabled:
        raise MapiError(
            "static rendering of Plotly plots is not available on this platform (it needs "
            "kaleido and a Chrome/Chromium in the runner image); render Vega-Lite plots, or "
            "export the Plotly plot from the plot viewer",
            code=ErrorCode.MODE_NOT_SUPPORTED,
            pointer="/plot/kind",
        )
    _check_pixels(resolved.plot, fmt, scale, width, height)
    doc = _dump(resolved.plot)
    inputs: dict[str, Any] = {}
    datasets: dict[str, Any] = dict(doc.get("datasets") or {})
    refs = [(n, d) for n, d in resolved.plot.datasets.items() if isinstance(d, DataRef)]
    for i, (name, ds) in enumerate(refs):
        if resolved.model_version is None:
            raise MapiError(
                f"dataset {name!r} refers to a bundle object, but the plot has no model version",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/plot/datasets/{name}",
            )
        obj = await verified_bundle_object(ctx.store, ctx.signer, resolved.model_version, ds.path)
        input_name = plot_data_input(i)
        inputs[input_name] = StoredRef(obj.bucket, obj.key, obj.generation)
        datasets[name] = {
            "kind": "data_ref",
            "path": input_name,
            "format": ds.format,
            "size": obj.size,
            "crc32c": obj.crc32c,
        }
    doc["datasets"] = datasets
    job = RenderJob(plots=[resolved.plot.id], format=fmt, scale=scale, width=width, height=height)
    payload = job.model_dump(mode="json", exclude_none=True)
    labels = {"mapi.plot": resolved.plot.id[:63].replace("/", "-")}
    if resolved.saved_plot_id is not None:
        labels["mapi.saved_plot"] = resolved.saved_plot_id
    if resolved.model_version is not None:
        labels["mapi.model_version"] = resolved.model_version.id[:63]
    sc = ctx.scope
    run = await create_run(
        session,
        sc,
        kind=RunKind.RENDER,
        resource_class=_RENDER_CLASS,
        timeout_s=ctx.settings.render_timeout_s,
        payload=payload,
        inputs=inputs,
        labels=labels,
        spec_hash=spec_hash({"job": payload, "plot": doc}),
    )
    body = json.dumps([doc], separators=(",", ":"), ensure_ascii=False).encode()
    meta = await ctx.store.put(
        ctx.buckets.runs,
        render_input_key(sc.tenant_id, run.id),
        body,
        content_type="application/json",
        if_generation_match=0,
    )
    run.inputs = {**inputs, INPUT_PLOTS: StoredRef(meta.bucket, meta.name, meta.generation)}
    await session.flush()
    await admit(session, ctx, run)
    await apply_transition(session, run, RunTrigger.ACCEPT, Actor.API)
    return run


# ----------------------------------------------------------------------------- embeds


def embed_token_hash(pepper: bytes, token: str) -> str:
    """The stored form of an embed token (keyed, so a database dump allows no guessing)."""
    digest = hmac.new(pepper, _TOKEN_PURPOSE + token.encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def embed_status(row: m.PlotEmbed, at: datetime) -> str:
    if row.revoked_at is not None:
        return "revoked"
    return "expired" if row.expires_at <= at else "active"


def embed_dto(row: m.PlotEmbed, at: datetime | None = None) -> PlotEmbed:
    return PlotEmbed(
        id=row.id,
        plot_id=row.plot_id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        label=row.label,
        display=row.display,
        allowed_origins=list(row.allowed_origins or []),
        status=embed_status(row, at or now()),
        views=row.views,
        last_viewed_at=row.last_viewed_at,
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def embed_image_key(tenant_id: str, embed_id: str) -> str:
    """``t/{tenant}/embeds/{embed}/plot.png`` in the bundles bucket (write-once)."""
    return (
        f"t/{check_segment(tenant_id, 'tenant_id')}/embeds/"
        f"{check_segment(embed_id, 'embed_id')}/{EMBED_IMAGE_NAME}"
    )


def _check_embeddable(resolved: ResolvedPlot) -> None:
    if not resolved.embeddable:
        why = (
            "it is a user-authored spec"
            if resolved.source_kind == "spec"
            else f"it was drawn by {resolved.provenance} code, not a platform recipe"
        )
        raise MapiError(
            f"only plots of platform recipes can be embedded; {why}. Share it inside the "
            "project instead, or render it as an image (POST /v1/plots:render)",
            code=ErrorCode.EMBED_NOT_ALLOWED,
        )
    issues: list[Issue] = []
    for name, ds in resolved.plot.datasets.items():
        if isinstance(ds, DataRef) and (ds.size or 0) > EMBED_DATASET_MAX_BYTES:
            issues.append(
                Issue(
                    code=ErrorCode.EMBED_NOT_ALLOWED,
                    pointer=f"/datasets/{name}",
                    message=f"dataset {name!r} is larger than an embed can carry "
                    f"({EMBED_DATASET_MAX_BYTES} bytes)",
                )
            )
    if issues:
        raise MapiError(issues[0].message, code=ErrorCode.EMBED_NOT_ALLOWED, issues=issues)


async def create_embed(
    session: AsyncSession, ctx: ServerContext, plot_id: str, body: PlotEmbedCreate
) -> tuple[m.PlotEmbed, str]:
    """Mint an embed link (returns the row and the token, shown once). A PNG fallback is
    rendered in the sandbox when the caller may start render runs; without it (or when the
    quota refuses it) the embed still works, with no image link."""
    row = await load_saved_plot(session, ctx, plot_id)
    resolved = await resolve_saved(session, ctx, row)
    _check_embeddable(resolved)
    for name, ds in resolved.plot.datasets.items():
        if isinstance(ds, DataRef):
            assert resolved.model_version is not None
            obj = await verified_bundle_object(
                ctx.store, ctx.signer, resolved.model_version, ds.path
            )
            if obj.size > EMBED_DATASET_MAX_BYTES:
                raise MapiError(
                    f"dataset {name!r} is larger than an embed can carry",
                    code=ErrorCode.EMBED_NOT_ALLOWED,
                    pointer=f"/datasets/{name}",
                )
    token = generate_embed_token()
    ts = now()
    auth = ctx.request
    embed = m.PlotEmbed(
        id=new_id("emb"),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        plot_id=row.id,
        label=body.label,
        token_hash=embed_token_hash(ctx.secrets.token_pepper, token),
        display=token[:10] + "…",
        allowed_origins=list(body.allowed_origins),
        views=0,
        created_by=auth.user_id if auth is not None else None,
        created_at=ts,
        expires_at=ts + timedelta(days=body.expires_in_days),
    )
    session.add(embed)
    await session.flush()
    if auth is not None and auth.allows(Action.RUN, ResourceKind.PLOT):
        try:
            async with session.begin_nested():
                run = await submit_render(session, ctx, resolved, fmt="png", scale=_FALLBACK_SCALE)
            embed.render_run_id = run.id
        except MapiError as exc:
            log.info("embed %s has no PNG fallback: %s (%s)", embed.id, exc.detail, exc.code)
    if auth is not None:
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.PLOT_EMBED_CREATE,
                    ResourceKind.EMBED,
                    resource_id=embed.id,
                    details={
                        "plot_id": row.id,
                        "expires_at": embed.expires_at.isoformat(),
                        "allowed_origins": list(body.allowed_origins),
                        "render_run_id": embed.render_run_id,
                    },
                )
            ],
            session=session,
        )
    return embed, token


async def revoke_embed(
    session: AsyncSession, ctx: ServerContext, plot_id: str, embed_id: str
) -> None:
    """Revoke (idempotent: a second revoke changes nothing and writes no audit row)."""
    await load_saved_plot(session, ctx, plot_id)
    row = (
        await session.execute(
            select(m.PlotEmbed)
            .where(m.PlotEmbed.id == embed_id, m.PlotEmbed.plot_id == plot_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("embed", embed_id)
    if row.revoked_at is not None:
        return
    row.revoked_at = now()
    await _drop_image(ctx, row)
    row.image = None
    if ctx.request is not None:
        await write_audit(
            ctx.db,
            [
                ctx.request.audit(
                    ctx,
                    AuditAction.PLOT_EMBED_REVOKE,
                    ResourceKind.EMBED,
                    resource_id=row.id,
                    details={"plot_id": plot_id},
                )
            ],
            session=session,
        )


async def _drop_image(ctx: ServerContext, row: m.PlotEmbed) -> None:
    image = row.image
    if not image:
        return
    try:
        await ctx.store.delete(str(image["bucket"]), str(image["name"]))
    except (NotFound, MapiError) as exc:  # retention lock or already gone: best effort
        log.info("could not delete the image of embed %s: %s", row.id, exc)


# ----------------------------------------------------------------------------- public view


@dataclass(frozen=True, slots=True)
class _Cached:
    view: EmbedView
    stamp: float


_CACHE: OrderedDict[tuple[str, str], _Cached] = OrderedDict()
_CACHE_MAX: Final = 512


def _cache_get(key: tuple[str, str], ttl: float) -> EmbedView | None:
    hit = _CACHE.get(key)
    if hit is None or time.monotonic() - hit.stamp > ttl:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return hit.view


def _cache_put(key: tuple[str, str], view: EmbedView) -> None:
    _CACHE[key] = _Cached(view, time.monotonic())
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def clear_embed_cache() -> None:
    """Forget every resolved embed (tests)."""
    _CACHE.clear()


def _retry_after(at: datetime) -> dict[str, str]:
    return {"Retry-After": str(max(1, 60 - at.second))}


async def _limit(s: AsyncSession, subject: str, limit: int, at: datetime) -> None:
    before = await count_requests(s, subject, now=at)
    if before >= limit:
        raise ProblemWithHeaders(
            "too many requests for this embed; try again shortly",
            code=ErrorCode.RATE_LIMITED,
            headers=_retry_after(at),
        )


def _expired() -> MapiError:
    return MapiError("this embed link has expired or was revoked", code=ErrorCode.LINK_EXPIRED)


async def embed_view(ctx: ServerContext, token: str, client_ip: str | None) -> EmbedView:
    """``GET /v1/embeds/{token}`` (public): rate-limit, find the embed by its token hash as
    the gateway role, then read the plot in a session pinned to the embed's tenant."""
    at = now()
    settings = ctx.settings
    async with ctx.db.session() as s:  # the gateway role inside a request
        if settings.rate_limits_enabled:
            who = hmac.new(
                ctx.secrets.audit_key, (client_ip or "-").encode(), hashlib.sha256
            ).hexdigest()[:32]
            await _limit(s, f"embedip:{who}", settings.embed_ip_rate_per_minute, at)
    if not EMBED_TOKEN_RE.match(token):
        raise NotFoundError("embed", "link")
    digest = embed_token_hash(ctx.secrets.token_pepper, token)
    async with ctx.db.session() as s:
        found = (
            await s.execute(
                select(
                    m.PlotEmbed.id,
                    m.PlotEmbed.tenant_id,
                    m.PlotEmbed.expires_at,
                    m.PlotEmbed.revoked_at,
                ).where(m.PlotEmbed.token_hash == digest)
            )
        ).one_or_none()
        if found is None:
            raise NotFoundError("embed", "link")
        embed_id, tenant_id, expires_at, revoked_at = found
        if revoked_at is not None or expires_at <= at:
            raise _expired()
        tenant = await s.get(m.Tenant, tenant_id)
        if tenant is None or tenant.status != TenantStatus.ACTIVE.value:
            raise _expired()
        if settings.rate_limits_enabled:
            await _limit(s, f"embed:{embed_id}", settings.embed_rate_per_minute, at)
    async with ctx.db.tenant_session(tenant_id) as session:
        row = await session.get(m.PlotEmbed, embed_id)
        if row is None or row.revoked_at is not None or row.expires_at <= at:
            raise _expired()
        saved = await session.get(m.SavedPlot, row.plot_id)
        if saved is None or ran_uploaded_code(
            await session.get(m.ModelVersion, saved.model_version_id or "")
        ):  # checked before the cache: plugin-written plots are never served
            raise NotFoundError("embed", "link")
        key = (row.id, saved.updated_at.isoformat())
        view = _cache_get(key, settings.embed_cache_s)
        if view is None:
            view = await _build_view(ctx, session, row, saved)
            _cache_put(key, view)
        image_url, image_bytes = await _image_url(ctx, session, row)
        # a public view is the tenant's download egress: the inline data and the PNG link
        await charge_egress(ctx, tenant_id, _view_bytes(view) + image_bytes, now=at)
        await session.execute(
            update(m.PlotEmbed)
            .where(m.PlotEmbed.id == row.id)
            .values(views=m.PlotEmbed.views + 1, last_viewed_at=at)
            .execution_options(synchronize_session=False)
        )
    return view.model_copy(update={"image_url": image_url})


async def _build_view(
    ctx: ServerContext, session: AsyncSession, row: m.PlotEmbed, saved: m.SavedPlot
) -> EmbedView:
    if not embeddable(saved.source_kind, saved.provenance, saved.recipe):
        raise NotFoundError("embed", "link")  # defence in depth: never serve non-recipes
    mv = await session.get(m.ModelVersion, saved.model_version_id or "")
    if mv is None or ran_uploaded_code(mv):
        raise NotFoundError("embed", "link")
    plot = sanitize_plot(saved.spec).plot
    datasets: dict[str, Any] = {}
    for name, ds in plot.datasets.items():
        if not isinstance(ds, DataRef):
            datasets[name] = ds.model_dump(mode="json")
            continue
        obj = await verified_bundle_object(ctx.store, ctx.signer, mv, ds.path)
        if obj.size > EMBED_DATASET_MAX_BYTES:
            raise MapiError(
                "this plot's data is too large to embed", code=ErrorCode.PAYLOAD_TOO_LARGE
            )
        raw = await ctx.store.get(
            obj.bucket, obj.key, generation=obj.generation, max_bytes=EMBED_DATASET_MAX_BYTES
        )
        if ds.format != "arrow":
            raise MapiError(
                f"dataset {name!r} is not Arrow; embeds carry Arrow datasets only",
                code=ErrorCode.EMBED_NOT_ALLOWED,
            )
        datasets[name] = {"kind": "inline_arrow", "data": base64.b64encode(raw).decode("ascii")}
    inline = plot.model_copy(update={"datasets": {}}).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    inline["datasets"] = datasets
    return EmbedView(
        title=saved.title,
        caption_plain=plot.caption_plain,
        plot=PlotSpec.model_validate(inline),
        generated_at=plot.provenance.generated_at,
        expires_at=row.expires_at,
        allowed_origins=list(row.allowed_origins or []),
    )


def _view_bytes(view: EmbedView) -> int:
    """The inline data an embed view carries (its Arrow datasets, base64)."""
    return sum(len(ds.data) for ds in view.plot.datasets.values() if isinstance(ds, InlineArrow))


async def _image_url(
    ctx: ServerContext, session: AsyncSession, row: m.PlotEmbed
) -> tuple[str | None, int]:
    """A signed link to the PNG fallback: copied (server side, write-once) out of the render
    run's scratch into the bundles bucket the first time it is asked for after the run
    succeeded."""
    ttl = timedelta(seconds=ctx.settings.embed_image_url_ttl_s)
    if row.image is None and row.render_run_id is not None:
        row.image = await _copy_fallback(ctx, session, row)
    if not row.image:
        return None, 0
    try:
        signed = await ctx.store.sign_get(
            str(row.image["bucket"]),
            str(row.image["name"]),
            expires_in=ttl,
            generation=int(row.image["generation"]),
        )
    except NotFound:
        row.image = None
        return None, 0
    return signed.url, int(signed.size or 0)


async def _copy_fallback(
    ctx: ServerContext, session: AsyncSession, row: m.PlotEmbed
) -> dict[str, Any] | None:
    run = await session.get(m.Run, row.render_run_id or "")
    if run is None or run.state != RunState.SUCCEEDED.value:
        return None
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    wanted = (run.payload or {}).get("plots") or []
    plot_id = str(wanted[0]) if wanted else ""
    outputs = (att.outputs if att is not None else None) or {}
    meta = outputs.get(render_output(plot_id, "png")) if plot_id else None
    if not isinstance(meta, dict):
        return None
    key = embed_image_key(row.tenant_id, row.id)
    bucket = ctx.buckets.bundles
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
    except NotFound:  # the run's scratch expired
        return None
    return {"bucket": copied.bucket, "name": copied.name, "generation": copied.generation}
