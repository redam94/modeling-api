"""What the control plane does with the result of a plugin check run (v0.19, D8, D10, D12).

Both functions run inside the finalizer's transaction (the run row is locked) and take the
verified output objects of the run, or ``None`` when the run ended without a result (failed,
timed out, cancelled, lost, refused at dispatch, integrity failure). They are called for
**every** terminal state of a ``plugin_inspect``/``plugin_describe`` run, so a version never
stays ``inspecting``/``describing`` once its run is over (:func:`plugin_run_ended` is the
no-result entry point; the housekeeping sweep repeats it for any run whose end was missed).

The results are data from a sandbox that ran hostile input. They are read with a size cap
**before** JSON parsing (:data:`~mapi_spec.plugins.INSPECTION_MAX_BYTES`,
:data:`~mapi_spec.plugins.DESCRIBE_MAX_BYTES`) and the depth cap of
:mod:`mapi_server.jsonlimits`, validated with the spec models, and the describe result is
re-checked as data (:func:`mapi_spec.plugins.check_describe`); every recipe example is
re-sanitized (:func:`mapi_plots.sanitize.sanitize_plot`). A bad result fails the version's
check with a plain problem; it never raises out of here (which would fail the finalizer).

Security decisions are made on inspect facts and the re-checked describe JSON, never on the
kit report: plugin code runs in the same process as its kit and can fake it (T19.25).

The describe result goes to ``plugin_describes`` only — never to the platform-wide
``catalogs`` table (G4).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.errors import ErrorCode, Issue, MapiError, Problem, has_errors
from mapi_spec.jobs import PLUGIN_DESCRIBE_OUTPUT, PLUGIN_INSPECTION_OUTPUT
from mapi_spec.plugins import (
    DESCRIBE_MAX_BYTES,
    INSPECTION_MAX_BYTES,
    MOUNTABLE_STATES,
    PluginDescribe,
    PluginInspection,
    PluginVersionState,
    check_describe,
    describe_digest,
    upload_dist_name,
)
from mapi_spec.run import RunState
from mapi_spec.storage import NotFound, ObjectMeta, PreconditionFailed, plugin_key
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.services.plugins.notify import PluginEvent, Recipients, notify_plugin_event
from mapi_server.services.plugins.pipeline import (
    LABEL_BASE_IMAGE,
    LABEL_REASON,
    LABEL_VERSION,
    plugin_of,
    start_describe,
)
from mapi_server.services.plugins.store import set_state
from mapi_server.services.runs import now

__all__ = [
    "HASH_CHUNK_BYTES",
    "builtin_titles",
    "fail_check",
    "finalize_plugin_describe",
    "finalize_plugin_inspect",
    "plugin_run_ended",
    "run_problem",
    "sha256_of_object",
]

log = logging.getLogger(__name__)

HASH_CHUNK_BYTES: Final = 4 * 1024 * 1024
"""The overlay copy is re-hashed in ranged reads of this size (never parsed)."""

_UPLOAD_AGAIN: Final = "Raise the version number and upload it again."
"""A version string is never uploaded twice (``uq_plugin_versions_plugin_id_version``), so
even a check the platform lost needs a new version number."""


# ================================================================================= helpers


async def _version_for(session: AsyncSession, run: m.Run) -> m.PluginVersion | None:
    """The version a pipeline run checks, locked, in the run's tenant only."""
    version_id = (run.labels or {}).get(LABEL_VERSION) or (run.payload or {}).get(
        "plugin_version_id"
    )
    if not isinstance(version_id, str):
        return None
    return (
        await session.execute(
            select(m.PluginVersion)
            .where(m.PluginVersion.id == version_id, m.PluginVersion.tenant_id == run.tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


def run_problem(run: m.Run) -> Problem:
    """The plain problem of a check run that ended without a result."""
    stored: Problem | None = None
    if isinstance(run.problem, dict):
        try:
            stored = Problem.model_validate(run.problem)
        except ValidationError:
            stored = None
    if run.state == RunState.CANCELLED.value:
        return Problem.from_code(
            ErrorCode.CANCELLED, f"The check run was cancelled. {_UPLOAD_AGAIN}"
        )
    if run.failure_class == "timeout":
        return Problem.from_code(
            ErrorCode.TIMEOUT,
            "The check run took too long and was stopped. Make the package load faster, "
            "then upload a new version.",
        )
    if stored is not None and stored.code is not None:
        if stored.code in (ErrorCode.PLUGIN_UNAVAILABLE, ErrorCode.UNTRUSTED_PLACEMENT_UNAVAILABLE):
            return Problem.from_code(stored.code, stored.detail)
        if stored.code is ErrorCode.INTEGRITY_FAILURE:
            return Problem.from_code(
                ErrorCode.INTEGRITY_FAILURE,
                "The result of the check run could not be verified. " + _UPLOAD_AGAIN,
            )
        if stored.code is ErrorCode.INFRA_ERROR:
            return Problem.from_code(
                ErrorCode.INFRA_ERROR,
                "The platform could not run the check. " + _UPLOAD_AGAIN,
            )
    return Problem.from_code(
        ErrorCode.PLUGIN_REFUSED,
        "The check run failed before it produced a result. Look at the run's log, fix the "
        "package and upload a new version.",
    )


def _refused(stage: str, detail: str, issues: list[Issue]) -> Problem:
    """``PLUGIN_REFUSED`` with its issues, each naming ``stage`` (``inspect``/``describe``)
    and its check in ``context`` (the page groups them)."""
    staged = [i.model_copy(update={"context": {**i.context, "stage": stage}}) for i in issues[:100]]
    return Problem.from_code(ErrorCode.PLUGIN_REFUSED, detail, errors=staged)


def _issue(check: str, message: str, *, untrusted: str | None = None) -> Issue:
    return Issue(
        code=ErrorCode.PLUGIN_REFUSED,
        message=message,
        untrusted_text=untrusted[:8000] if untrusted else None,
        context={"check": check},
    )


async def _read_json(
    ctx: ServerContext, meta: ObjectMeta | None, cap: int, what: str
) -> tuple[Any, Issue | None]:
    """Read one result object as JSON with ``cap`` checked before parsing."""
    if meta is None:
        return None, _issue("missing", f"The check run did not write {what}.")
    if meta.size > cap:
        return None, _issue(
            "size", f"{what} is larger than the limit of {cap} bytes. Export fewer entries."
        )
    try:
        raw = await ctx.store.get(meta.bucket, meta.name, generation=meta.generation, max_bytes=cap)
    except NotFound:
        return None, _issue("missing", f"The check run did not write {what}.")
    if len(raw) > cap:
        return None, _issue("size", f"{what} is larger than the limit of {cap} bytes.")
    try:
        return loads_limited(raw), None
    except JsonTooDeep:
        return None, _issue("depth", f"{what} is nested too deeply.")
    except ValueError:
        return None, _issue("json", f"{what} is not valid JSON.")


async def sha256_of_object(
    ctx: ServerContext, bucket: str, name: str, *, generation: int, size: int
) -> str:
    """``sha256:<hex>`` of a stored object, read in ranges (bytes only, never parsed)."""
    digest = hashlib.sha256()
    start = 0
    while start < size:
        end = min(start + HASH_CHUNK_BYTES, size) - 1
        chunk = await ctx.store.get(bucket, name, generation=generation, byte_range=(start, end))
        if not chunk:
            break
        digest.update(chunk)
        start += len(chunk)
    return "sha256:" + digest.hexdigest()


async def builtin_titles(session: AsyncSession, ctx: ServerContext) -> list[str]:
    """Titles of the built-in entries (an upload's title may not look like one): the base
    catalog's models, steps, recipes, plugin nodes and templates, plus the static built-in
    workflow nodes and templates."""
    from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    out: list[str] = []

    def add(title: object) -> None:
        if isinstance(title, str) and title:
            out.append(title)

    snapshot = await ctx.catalogs.get(session)
    if snapshot is not None:
        cat = snapshot.catalog
        for model in cat.models:
            if model.manifest.about is not None:
                add(model.manifest.about.title)
        for step in cat.steps:
            if step.about is not None:
                add(step.about.title)
        for plot in cat.plots:
            add(plot.title)
            if plot.about is not None:
                add(plot.about.title)
        for node in cat.workflow_nodes:
            add(node.about.title)
        for template in cat.workflow_templates:
            add(template.about.title)
    for impl in NODE_IMPLEMENTATIONS.values():
        if impl.about is not None:
            add(impl.about.title)
    for wt in WORKFLOW_TEMPLATES.values():
        about = getattr(wt, "about", None)
        if about is not None:
            add(about.title)
    return sorted(set(out))


async def _notify(
    session: AsyncSession,
    ctx: ServerContext,
    version: m.PluginVersion,
    event: PluginEvent,
    *,
    to: Recipients,
    exclude: Sequence[str] = (),
) -> None:
    """Mail + webhook through the one notifier (never uploaded text); ``exclude`` are people
    not mailed (the uploader, for a review request: D14)."""
    await notify_plugin_event(session, ctx, version, event, to=to, exclude=exclude)


def _move(
    ctx: ServerContext, version: m.PluginVersion, to: PluginVersionState, *, at: datetime
) -> None:
    """The one state writer, with the configured retention (``MAPI_PLUGIN_RETENTION_DAYS``)."""
    set_state(version, to, now=at, retention=timedelta(days=ctx.settings.plugin_retention_days))


def _fail(
    ctx: ServerContext,
    version: m.PluginVersion,
    to: PluginVersionState,
    problem: Problem,
    *,
    at: datetime,
) -> None:
    version.problem = problem.model_dump(mode="json", exclude_none=True)
    _move(ctx, version, to, at=at)


async def fail_check(
    session: AsyncSession, ctx: ServerContext, version: m.PluginVersion, problem: Problem
) -> None:
    """End the check of a version that is still ``inspecting``/``describing`` with
    ``problem`` (``inspect_failed``/``kit_failed``; the uploader is told). The caller holds
    the version ``FOR UPDATE``; a version in any other state is left alone."""
    to: PluginVersionState
    if version.state == "inspecting":
        to = "inspect_failed"
    elif version.state == "describing":
        to = "kit_failed"
    else:
        return
    _fail(ctx, version, to, problem, at=now())
    await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")


async def plugin_run_ended(session: AsyncSession, ctx: ServerContext, run: m.Run) -> None:
    """A plugin check run ended without a verified result: end its version's check.

    The caller holds the run's transaction. Safe to call more than once (a version that has
    moved on is left alone).
    """
    if run.kind == "plugin_inspect":
        await finalize_plugin_inspect(session, ctx, run, None)
    elif run.kind == "plugin_describe":
        await finalize_plugin_describe(session, ctx, run, None)


# ================================================================================= inspect


async def finalize_plugin_inspect(
    session: AsyncSession,
    ctx: ServerContext,
    run: m.Run,
    outputs: Mapping[str, ObjectMeta] | None,
) -> None:
    """Record an inspect result: refuse the version (``inspect_failed``) with plain problems,
    or copy the checked wheel into the ``plugins`` bucket (write-once, re-hashed) and queue
    its describe run (``describing``)."""
    version = await _version_for(session, run)
    if version is None:
        log.warning("plugin_inspect run %s names no plugin version of its tenant", run.id)
        return
    if version.state != "inspecting" or version.inspect_run_id != run.id:
        return  # an older run, or the version moved on
    at = now()
    if outputs is None:
        _fail(ctx, version, "inspect_failed", run_problem(run), at=at)
        await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")
        return
    raw, bad = await _read_json(
        ctx, outputs.get(PLUGIN_INSPECTION_OUTPUT), INSPECTION_MAX_BYTES, "inspection.json"
    )
    inspection: PluginInspection | None = None
    if bad is None:
        try:
            inspection = PluginInspection.model_validate(raw)
        except ValidationError as exc:
            bad = _issue(
                "schema",
                "inspection.json does not match the inspection schema.",
                untrusted=str(exc),
            )
    if inspection is None:
        assert bad is not None
        await _refuse_inspect(
            session,
            ctx,
            version,
            "The check of the file did not give a usable result. " + _UPLOAD_AGAIN,
            [bad],
            at=at,
        )
        return
    plugin = await plugin_of(session, version)
    version.inspection = inspection.model_dump(mode="json", exclude_none=True)
    version.sbom = inspection.sbom.model_dump(mode="json") if inspection.sbom else None
    issues = [_issue(p.check, p.message, untrusted=p.path) for p in inspection.problems]
    # the facts the rest of the pipeline relies on, checked again here (data, not trust)
    if inspection.archive_sha256 != version.upload_sha256:
        issues.append(_issue("sha_mismatch", "The checked file is not the file that was uploaded."))
    if inspection.ok and not issues:
        if inspection.dist is None or _normal(inspection.dist) != upload_dist_name(
            plugin.namespace
        ):
            issues.append(
                _issue(
                    "dist_name",
                    f"The distribution name must be {upload_dist_name(plugin.namespace)}.",
                )
            )
        if inspection.version != version.version:
            issues.append(
                _issue("dist_info", "The version in the wheel is not the uploaded version.")
            )
        if inspection.top_level != [plugin.namespace]:
            issues.append(
                _issue(
                    "top_level_name",
                    f"The wheel must hold exactly one top-level package, {plugin.namespace}.",
                )
            )
    if not inspection.ok or issues:
        if not issues:
            issues.append(_issue("inspect", "The check of the file found a problem."))
        await _refuse_inspect(
            session,
            ctx,
            version,
            f"The upload was refused: {len(issues)} problem(s) found in the file. Fix them "
            "and upload a new version.",
            issues,
            at=at,
        )
        return
    copied = await _copy_overlay(ctx, version)
    if copied is None:
        await _refuse_inspect(
            session,
            ctx,
            version,
            "The stored copy of the file is not the file that was uploaded. " + _UPLOAD_AGAIN,
            [_issue("sha_mismatch", "The copy in the plugins bucket has another sha256.")],
            at=at,
        )
        return
    version.overlay_key, version.overlay_generation = copied
    version.problem = None
    try:
        async with session.begin_nested():
            await start_describe(
                session, ctx, version, created_by=version.uploaded_by, reason="upload"
            )
    except MapiError as exc:
        # the describe run could not be admitted (placement, cut line A): the check ends
        await session.refresh(version)
        if version.state == "inspecting":
            _move(ctx, version, "describing", at=at)
        _fail(ctx, version, "kit_failed", Problem.from_code(exc.code, exc.detail), at=at)
        await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")


def _normal(name: str) -> str:
    """PEP 503 normalization of a distribution name."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


async def _refuse_inspect(
    session: AsyncSession,
    ctx: ServerContext,
    version: m.PluginVersion,
    detail: str,
    issues: list[Issue],
    *,
    at: datetime,
) -> None:
    problem = _refused("inspect", detail, issues)
    _fail(ctx, version, "inspect_failed", problem, at=at)
    await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")


async def _copy_overlay(ctx: ServerContext, version: m.PluginVersion) -> tuple[str, int] | None:
    """Copy the quarantine generation into the ``plugins`` bucket (write-once) and re-hash
    the copy. ``None`` when the copy's sha256 is not the uploaded one (the copy is deleted)."""
    store = ctx.store
    bucket = ctx.buckets.plugins
    key = plugin_key(version.tenant_id, version.plugin_id, version.id)
    try:
        meta = await store.copy(
            ctx.buckets.quarantine,
            version.quarantine_key,
            bucket,
            key,
            source_generation=version.quarantine_generation,
            if_generation_match=0,
        )
    except PreconditionFailed:
        meta = await store.stat(bucket, key)  # a retried finalization: re-hash what is there
    sha = await sha256_of_object(ctx, bucket, key, generation=meta.generation, size=meta.size)
    if sha != version.upload_sha256 or meta.size != version.size_bytes:
        log.error(
            "SECURITY ALERT: plugin version %s: the plugins-bucket copy hashes to %s, not %s",
            version.id,
            sha,
            version.upload_sha256,
            extra={"security_alert": True, "tenant_id": version.tenant_id},
        )
        with contextlib.suppress(NotFound):
            await store.delete(bucket, key, generation=meta.generation)
        return None
    return key, int(meta.generation)


# ================================================================================ describe


async def finalize_plugin_describe(
    session: AsyncSession,
    ctx: ServerContext,
    run: m.Run,
    outputs: Mapping[str, ObjectMeta] | None,
) -> None:
    """Record a describe result for the run's base image digest and move the version.

    Upload: ``describing → kit_passed`` (or ``kit_failed``); ``kit_passed → awaiting_approval``
    when the uploader asked for approval at ``:complete`` (``requested_by`` already set).
    Rekit (D12): kits fail ⇒ ``yanked`` (``rekit_failed``); exports changed on an approved
    version ⇒ ``awaiting_approval``; otherwise the state is kept.
    """
    version = await _version_for(session, run)
    if version is None:
        log.warning("plugin_describe run %s names no plugin version of its tenant", run.id)
        return
    labels = run.labels or {}
    reason = labels.get(LABEL_REASON, "upload")
    base = str(labels.get(LABEL_BASE_IMAGE) or ctx.catalogs.image_digest)
    if reason == "upload" and version.state != "describing":
        return
    if reason == "rekit" and version.state not in MOUNTABLE_STATES:
        return
    at = now()
    if outputs is None:
        if reason == "upload":
            _fail(ctx, version, "kit_failed", run_problem(run), at=at)
            await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")
        elif run.state == RunState.CANCELLED.value or run.failure_class == "infra":
            # not the package's fault: the rekit sweep queues another describe later
            log.info("rekit of %s ended without a result (%s): will retry", version.id, run.state)
        else:
            await _rekit_failed(session, ctx, version, run_problem(run), at=at)
        return
    raw, bad = await _read_json(
        ctx, outputs.get(PLUGIN_DESCRIBE_OUTPUT), DESCRIBE_MAX_BYTES, "describe.json"
    )
    d: PluginDescribe | None = None
    if bad is None:
        try:
            d = PluginDescribe.model_validate(raw)
        except ValidationError as exc:
            bad = _issue(
                "schema", "describe.json does not match the describe schema.", untrusted=str(exc)
            )
    if d is not None and (
        d.plugin_version_id != version.id or d.overlay_sha256 != version.upload_sha256
    ):
        bad = _issue("identity", "describe.json describes another version or file.")
        d = None
    if d is None:
        assert bad is not None
        problem = _refused(
            "describe",
            "The tests did not give a usable result. Fix the package and upload a new version.",
            [bad],
        )
        if reason == "upload":
            _fail(ctx, version, "kit_failed", problem, at=at)
            await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")
        else:
            await _rekit_failed(session, ctx, version, problem, at=at)
        return
    plugin = await plugin_of(session, version)
    examples, example_issues = _sanitize_examples(d)
    issues = check_describe(
        d, namespace=plugin.namespace, builtin_titles=await builtin_titles(session, ctx)
    )
    issues = [*issues, *example_issues]
    ok = d.kit.ok and not has_errors(issues)
    digest = describe_digest(d.exports)
    values = {
        "version_id": version.id,
        "base_image_digest": base,
        "tenant_id": version.tenant_id,
        "run_id": run.id,
        "describe_digest": digest,
        "exports": d.exports.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        "plot_examples": examples,
        "kit": d.kit.model_dump(mode="json"),
        "issues": [i.model_dump(mode="json", exclude_none=True) for i in issues[:200]],
        "ok": ok,
        "created_at": at,
    }
    await session.execute(
        pg_insert(m.PluginDescribe)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["version_id", "base_image_digest"],
            set_={k: v for k, v in values.items() if k not in ("version_id", "base_image_digest")},
        )
    )
    if reason == "upload":
        if not ok:
            failed = [i for i in issues if i.is_error]
            failed_kits = [c for c in d.kit.checks if c.status == "fail"]
            detail = (
                f"The tests found {len(failed) + len(failed_kits)} problem(s). Look at the "
                "kit report, fix the package and upload a new version."
            )
            _fail(ctx, version, "kit_failed", _refused("describe", detail, failed), at=at)
            await _notify(session, ctx, version, "plugin.version.kit_failed", to="uploader")
            return
        version.problem = None
        _move(ctx, version, "kit_passed", at=at)
        version.updated_at = at
        await _notify(session, ctx, version, "plugin.version.kit_passed", to="uploader")
        if version.requested_by is not None:
            # the uploader asked for approval at :complete (D14)
            _move(ctx, version, "awaiting_approval", at=at)
            version.requested_at = version.requested_at or at
            await _notify(
                session,
                ctx,
                version,
                "plugin.version.awaiting_approval",
                to="admins",
                exclude=(version.uploaded_by,),
            )
        return
    # rekit (D12)
    if not ok:
        failed = [i for i in issues if i.is_error]
        await _rekit_failed(
            session,
            ctx,
            version,
            _refused(
                "describe",
                "The tests of this version failed on the new platform image.",
                failed,
            ),
            at=at,
        )
        return
    if version.state == "approved" and digest != await _approved_digest(session, version.id):
        _move(ctx, version, "awaiting_approval", at=at)
        version.updated_at = at
        await _notify(
            session,
            ctx,
            version,
            "plugin.version.awaiting_approval",
            to="admins",
            exclude=(version.uploaded_by,),
        )


async def _approved_digest(session: AsyncSession, version_id: str) -> str | None:
    """The ``describe_digest`` of the newest approval of a version."""
    return (
        await session.execute(
            select(m.PluginApproval.describe_digest)
            .where(
                m.PluginApproval.version_id == version_id,
                m.PluginApproval.decision == "approve",
            )
            .order_by(m.PluginApproval.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _rekit_failed(
    session: AsyncSession,
    ctx: ServerContext,
    version: m.PluginVersion,
    problem: Problem,
    *,
    at: datetime,
) -> None:
    """D12: a version whose tests fail on a new base image is withdrawn (mail + webhook to
    the uploader and the tenant admins)."""
    version.problem = problem.model_dump(mode="json", exclude_none=True)
    _move(ctx, version, "yanked", at=at)
    version.yank_reason = "rekit_failed"
    version.yanked_at = version.yanked_at or at
    version.updated_at = at
    await _notify(session, ctx, version, "plugin.version.yanked", to="admins_and_uploader")


def _sanitize_examples(d: PluginDescribe) -> tuple[dict[str, Any], list[Issue]]:
    """Every recipe example through the control plane's plot sanitizer; an unsafe one is
    dropped with an ``example_unsafe`` error issue."""
    kept: dict[str, Any] = {}
    issues: list[Issue] = []
    for plot_id, doc in d.plot_examples.items():
        try:
            spec = sanitize_plot(doc).plot
        except (SanitizeError, ValidationError, ValueError) as exc:
            detail = exc.detail if isinstance(exc, SanitizeError) else str(exc)
            issues.append(
                Issue(
                    code=ErrorCode.PLUGIN_REFUSED,
                    pointer=f"/plot_examples/{_escape(plot_id)}",
                    message="A recipe example is not a safe plot and was dropped.",
                    untrusted_text=f"{plot_id}: {detail}"[:8000],
                    context={"check": "example_unsafe"},
                )
            )
            continue
        kept[plot_id] = spec.model_dump(mode="json", by_alias=True)
    return kept, issues


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")[:200]
