"""Live recipe previews: the recipe playground of the plot catalog (v0.18 D21).

``POST /v1/catalog/plots/{plot_id}/{version}:preview`` draws a plot recipe on its own
synthetic catalog example with the settings the user is trying. The server never draws
anything: it checks the request, then queues a ``render`` run whose job is
``RenderJob{recipe: RecipePreview}``; the runner merges the settings over the example's
options, sanitizes the result and writes :data:`~mapi_spec.jobs.PREVIEW_OUTPUT`.

Before any run (cheapest refusal first):

1. the kill switch ``plot_preview_enabled`` (``PREVIEW_DISABLED``, 409);
2. the recipe must be in the caller's tenant catalog view (v0.19: the base catalog plus the
   uploads they may use; ``NOT_FOUND``) and have an example (``MODE_NOT_SUPPORTED``); an
   uploaded recipe's run mounts its version (``runs.overlays``, untrusted);
3. the settings are validated with ``jsonschema`` against the recipe's sanitized catalog
   ``settings_schema`` (``SCHEMA_INVALID`` with pointers ``/settings/…``; settings for a
   recipe without a schema are ``MODE_NOT_SUPPORTED``). Empty settings are the catalog
   example itself, served without a run;
4. the cache: a render run of the same project with the same :func:`preview_key` (recipe,
   settings, catalog digest) from the last :data:`CACHE_TTL` is reused: its plot when it
   succeeded, its problem when it failed for a reason the same settings would hit again,
   the run itself while it is still going. A cache hit starts no run and is not counted;
5. the rate limits: ``plot_preview_per_minute`` new runs per user (a fixed one-minute
   window) and ``plot_preview_per_day`` new runs per tenant in the last 24 hours
   (``RATE_LIMITED``, 429 with ``Retry-After``);
6. admission (kill switches, placement, the tenant's run quotas: ``QUOTA_EXCEEDED``).

The run is labelled ``mapi.preview=<key>`` (indexed for render runs) and
``mapi.plot=<recipe id>``; it reads no input. :func:`preview_result` reads a run back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.api import parse_prefer_wait
from mapi_spec.catalog import PlotCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import RunKind
from mapi_spec.jobs import (
    PREVIEW_LABEL,
    PREVIEW_OUTPUT,
    RecipePreview,
    RecipePreviewRequest,
    RecipePreviewResult,
    RenderJob,
)
from mapi_spec.loader import spec_hash
from mapi_spec.plots import PlotSpec
from mapi_spec.plugins import OverlayPin
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.validate import validate_settings
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.plugins.view import catalog_view
from mapi_server.services.quotas import count_requests
from mapi_server.services.runs import apply_transition, create_run, load_run, now
from mapi_server.services.submit import admit

__all__ = [
    "CACHE_TTL",
    "PREVIEW_MAX_BYTES",
    "preview_key",
    "preview_result",
    "preview_wait",
    "request_preview",
]

log = logging.getLogger(__name__)

CACHE_TTL: Final = timedelta(days=7)
"""How long a preview run answers the same request again."""
PREVIEW_MAX_BYTES: Final = 8 * 1024 * 1024
"""The largest preview the API reads back (a PlotSpec with inline example data)."""

_KEY_PREFIX: Final = "pv1-"
_KEY_HEX: Final = 56
"""The key is a run label value (≤ 63 characters): the prefix and 56 hex digits of sha256."""
_RENDER_CLASS: Final = ResourceClass.S
_DAY: Final = timedelta(days=1)
_SETTINGS_POINTER: Final = "/settings"
_STICKY_FAILURES: Final = frozenset(
    {
        ErrorCode.SCHEMA_INVALID,
        ErrorCode.MODE_NOT_SUPPORTED,
        ErrorCode.PLOT_UNSAFE,
        # what the supervisor records when the worker refuses the settings ("the run failed
        # with SCHEMA_INVALID (as reported by the worker)"): a preview runs only a platform
        # recipe on its own example, so the same settings fail the same way again
        ErrorCode.USER_CODE_ERROR,
    }
)
"""Failures the same recipe, settings and catalog would hit again: cached like a success
(anything else, a timeout or an infrastructure error, is tried again)."""


def preview_key(ref: str, settings: Mapping[str, JsonValue], catalog_digest: str) -> str:
    """The cache key of a preview: recipe ``id@version``, settings (canonical JSON: key order
    does not matter) and the catalog digest (a new image draws anew)."""
    canonical = json.dumps(
        {"catalog": catalog_digest, "ref": ref, "settings": settings},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _KEY_PREFIX + hashlib.sha256(canonical.encode()).hexdigest()[:_KEY_HEX]


def preview_wait(ctx: ServerContext, prefer: str | None) -> float:
    """Seconds a request may wait for its preview: ``Prefer: wait=N`` capped at
    ``plot_preview_wait_s`` (0 without the header)."""
    asked = parse_prefer_wait(prefer)
    return float(min(asked, ctx.settings.plot_preview_wait_s)) if asked else 0.0


# ------------------------------------------------------------------------------- checks


def _disabled() -> MapiError:
    return MapiError(
        "live chart previews are turned off on this platform; change the settings and use "
        "Apply on a real model instead",
        code=ErrorCode.PREVIEW_DISABLED,
    )


def _check_settings(entry: PlotCatalogEntry, settings: Mapping[str, JsonValue]) -> None:
    ref = f"{entry.id}@{entry.version}"
    if not settings:
        return
    if not entry.settings_schema:
        raise MapiError(
            f"the plot recipe {ref} has no settings to try",
            code=ErrorCode.MODE_NOT_SUPPORTED,
            pointer=_SETTINGS_POINTER,
        )
    issues = validate_settings(
        entry.settings_schema, dict(settings), _SETTINGS_POINTER, code=ErrorCode.SCHEMA_INVALID
    )
    if issues:
        raise MapiError(
            f"the settings are not valid for {ref}: {issues[0].message}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=issues[0].pointer,
            issues=issues,
        )


def _retry_after(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, int(seconds + 0.999)))}


async def _check_rate(ctx: ServerContext, session: AsyncSession, at: datetime) -> None:
    """The per-user minute and the per-tenant day (counted when a run would start)."""
    settings = ctx.settings
    auth = ctx.request
    who = (auth.user_id if auth is not None else None) or "anonymous"
    scope = ctx.scope
    async with ctx.db.session() as system:
        before = await count_requests(system, f"preview:{scope.tenant_id}:{who}"[:96], now=at)
    if before >= settings.plot_preview_per_minute:
        raise ProblemWithHeaders(
            f"you can start at most {settings.plot_preview_per_minute} chart previews a "
            "minute; try again shortly",
            code=ErrorCode.RATE_LIMITED,
            headers=_retry_after(60 - at.second),
        )
    since = at - _DAY
    count, oldest = (
        await session.execute(
            select(func.count(), func.min(m.Run.created_at)).where(
                m.Run.tenant_id == scope.tenant_id,
                m.Run.kind == RunKind.RENDER.value,
                m.Run.labels[PREVIEW_LABEL].astext.is_not(None),
                m.Run.created_at >= since,
            )
        )
    ).one()
    if int(count) >= settings.plot_preview_per_day:
        wait = (oldest + _DAY - at).total_seconds() if oldest is not None else 3600.0
        raise ProblemWithHeaders(
            f"this tenant started {settings.plot_preview_per_day} chart previews in the last "
            "24 hours, the most allowed; try again later",
            code=ErrorCode.RATE_LIMITED,
            headers=_retry_after(wait),
        )


# ------------------------------------------------------------------------------ reading


async def _read_plot(ctx: ServerContext, session: AsyncSession, run: m.Run) -> PlotSpec | None:
    """The preview a succeeded run wrote, sanitized again on the way out (``None`` when it
    is missing, too large or not a safe plot)."""
    att = await session.get(m.RunAttempt, (run.id, run.attempt))
    meta = ((att.outputs if att is not None else None) or {}).get(PREVIEW_OUTPUT)
    if not isinstance(meta, dict) or int(meta.get("size") or 0) > PREVIEW_MAX_BYTES:
        return None
    try:
        raw = await ctx.store.get(
            str(meta["bucket"]),
            str(meta["name"]),
            generation=int(meta["generation"]),
            max_bytes=PREVIEW_MAX_BYTES,
        )
        plot = PlotSpec.model_validate(loads_limited(raw))
        return sanitize_plot(plot).plot
    except (JsonTooDeep, ValueError, ValidationError, KeyError, SanitizeError, MapiError):
        log.warning("unreadable recipe preview of run %s", run.id)
        return None


def _problem(run: m.Run) -> Problem:
    if run.problem:
        try:
            return Problem.model_validate(run.problem)
        except ValidationError:
            pass
    return Problem.from_code(ErrorCode.INTERNAL_ERROR, "the preview run failed")


async def _result(
    ctx: ServerContext, session: AsyncSession, run: m.Run, *, cached: bool
) -> RecipePreviewResult | None:
    """The result of a preview run; ``None`` when a succeeded run's preview is unreadable."""
    state = RunState(run.state)
    if not state.terminal:
        return RecipePreviewResult(run_id=run.id, cached=cached, done=False)
    if state is RunState.SUCCEEDED:
        plot = await _read_plot(ctx, session, run)
        if plot is None:
            return None
        return RecipePreviewResult(run_id=run.id, cached=cached, done=True, plot=plot)
    return RecipePreviewResult(run_id=run.id, cached=cached, done=True, problem=_problem(run))


async def _cached(
    ctx: ServerContext, session: AsyncSession, key: str, at: datetime
) -> RecipePreviewResult | None:
    """The newest usable run of this project with the preview ``key``."""
    scope = ctx.scope
    rows = (
        await session.execute(
            select(m.Run)
            .where(
                m.Run.labels[PREVIEW_LABEL].astext == key,
                m.Run.kind == RunKind.RENDER.value,
                m.Run.tenant_id == scope.tenant_id,
                m.Run.project_id == scope.project_id,
                m.Run.created_at >= at - CACHE_TTL,
            )
            .order_by(m.Run.created_at.desc())
            .limit(5)
        )
    ).scalars()
    for run in rows:
        state = RunState(run.state)
        if state is RunState.FAILED and _problem(run).code not in _STICKY_FAILURES:
            continue
        if state.terminal and state not in (RunState.SUCCEEDED, RunState.FAILED):
            continue  # cancelled, expired: draw again
        found = await _result(ctx, session, run, cached=True)
        if found is not None:
            return found
    return None


async def _wait(
    ctx: ServerContext,
    run_id: str,
    timeout: float,
    disconnected: Callable[[], Awaitable[bool]] | None,
) -> None:
    """Wait (notifications, with polling) until the run is terminal or ``timeout``."""
    deadline = time.monotonic() + timeout
    async with ctx.hub.subscribe(run_id) as sub:
        while True:
            async with ctx.data_session() as session:
                run = await load_run(session, None, run_id)
            remaining = deadline - time.monotonic()
            if RunState(run.state).terminal or remaining <= 0:
                return
            if disconnected is not None and await disconnected():
                return
            await sub.wait(min(remaining, ctx.settings.sse_poll_s))
            await asyncio.sleep(0)


# --------------------------------------------------------------------------------- API


@dataclass(frozen=True, slots=True)
class _Recipe:
    """A recipe as the caller's tenant catalog view shows it (v0.19)."""

    entry: PlotCatalogEntry
    example: PlotSpec
    digest: str
    """The cache digest: the base catalog digest, plus the wheel's hash for an upload."""
    pins: list[OverlayPin]


async def _entry(ctx: ServerContext, plot_id: str, version: str) -> _Recipe:
    """The recipe in the caller's tenant catalog view, its example and its overlay pins
    (an uploaded recipe runs its own code: its version is mounted)."""
    async with ctx.db.session() as system:
        view = await catalog_view(
            ctx,
            system,
            tenant_id=ctx.scope.tenant_id,
            user_id=ctx.request.user_id if ctx.request is not None else None,
        )
        catalogs = view.service(ctx.catalogs)
        for entry in view.catalog.plots:
            if entry.id == plot_id and entry.version == version:
                example = await catalogs.example(system, f"{plot_id}@{version}")
                if example is None:
                    raise MapiError(
                        f"the plot recipe {plot_id}@{version} has no example to preview",
                        code=ErrorCode.MODE_NOT_SUPPORTED,
                    )
                digest = view.base.digest
                if entry.overlay_hash is not None:
                    digest = f"{digest}+{entry.overlay_hash}"
                return _Recipe(entry, example, digest, view.pins_for([plot_id]))
    raise NotFoundError("catalog plot", f"{plot_id}@{version}")


async def request_preview(
    ctx: ServerContext,
    plot_id: str,
    version: str,
    body: RecipePreviewRequest,
    *,
    wait: float = 0.0,
    disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> RecipePreviewResult:
    """Preview recipe ``plot_id@version`` with ``body.settings`` in the request's project
    (the checks and the order are in the module docstring). With ``wait`` > 0 a new or
    running preview is waited for up to that many seconds. ``done`` says whether the
    result is final (the route answers 200) or still running (202, poll
    :func:`preview_result`)."""
    if not ctx.settings.plot_preview_enabled:
        raise _disabled()
    recipe = await _entry(ctx, plot_id, version)
    ref = f"{plot_id}@{version}"
    settings = dict(body.settings)
    # off the event loop: the schema comes from an image catalog (schema_safety bounds it)
    await asyncio.to_thread(_check_settings, recipe.entry, settings)
    if not settings:
        return RecipePreviewResult(run_id=None, cached=True, done=True, plot=recipe.example)
    key = preview_key(ref, settings, recipe.digest)
    at = now()
    async with ctx.data_session() as session:
        found = await _cached(ctx, session, key, at)
        if found is None:
            await _check_rate(ctx, session, at)
            run = await _start(ctx, session, ref, settings, key, recipe)
            found = RecipePreviewResult(run_id=run.id, cached=False, done=False)
    if found.done or wait <= 0 or found.run_id is None:
        return found
    await _wait(ctx, found.run_id, wait, disconnected)
    result = await preview_result(ctx, found.run_id)
    return result.model_copy(update={"cached": found.cached})


async def _start(
    ctx: ServerContext,
    session: AsyncSession,
    ref: str,
    settings: dict[str, JsonValue],
    key: str,
    recipe: _Recipe,
) -> m.Run:
    """Queue the render run of a preview (admission: kill switches, placement, quotas and,
    for an uploaded recipe, its overlay pin)."""
    job = RenderJob(recipe=RecipePreview(ref=ref, settings=settings))
    payload = job.model_dump(mode="json", exclude_none=True)
    run = await create_run(
        session,
        ctx.scope,
        kind=RunKind.RENDER,
        resource_class=_RENDER_CLASS,
        timeout_s=ctx.settings.render_timeout_s,
        payload=payload,
        labels={PREVIEW_LABEL: key, "mapi.plot": ref.split("@", 1)[0][:63]},
        spec_hash=spec_hash({"job": payload, "catalog": recipe.digest}),
        catalog_digest=recipe.digest.split("+", 1)[0],
    )
    await session.flush()
    await admit(session, ctx, run, overlays=recipe.pins)
    await apply_transition(session, run, RunTrigger.ACCEPT, Actor.API)
    return run


async def preview_result(ctx: ServerContext, run_id: str) -> RecipePreviewResult:
    """``GET /v1/catalog/plots/previews/{run_id}``: a preview run of the request's project
    (``NOT_FOUND`` for any other run, including the render runs of saved plots)."""
    async with ctx.data_session() as session:
        run = await load_run(session, ctx.scope, run_id)
        if run.kind != RunKind.RENDER.value or not (run.labels or {}).get(PREVIEW_LABEL):
            raise NotFoundError("recipe preview", run_id)
        result = await _result(ctx, session, run, cached=False)
    if result is None:
        return RecipePreviewResult(
            run_id=run_id,
            done=True,
            problem=Problem.from_code(
                ErrorCode.INTERNAL_ERROR, "the preview run finished but its plot could not be read"
            ),
        )
    return result
