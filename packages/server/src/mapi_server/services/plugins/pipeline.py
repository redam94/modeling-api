"""The two sandboxed pipeline runs of an uploaded plugin version (v0.19, D8).

* :func:`start_inspect` queues a ``plugin_inspect`` run that reads the uploaded wheel (run
  input ``plugin.whl``, the generation-pinned quarantine object) as an archive. It imports
  nothing and extracts nothing; its result is ``plugin/inspection.json``.
* :func:`start_describe` queues a ``plugin_describe`` run that mounts the inspected copy in
  the ``plugins`` bucket as an overlay (``runs.overlays`` = exactly this version's pin) and
  exports its catalog entries, kit report and recipe examples to ``plugin/describe.json``.

Both runs are untrusted (the kill switch, cut line A and the untrusted placement rule apply)
and are billed to the version's ``project_id``. The control plane never opens the wheel: it
only names objects and signs URLs to them. :mod:`.finalize` reads the size-capped JSON
results back as data.

Run labels tie a pipeline run to its version: :data:`LABEL_VERSION` (the version id),
:data:`LABEL_REASON` (``upload`` or ``rekit``) and, for describe runs,
:data:`LABEL_BASE_IMAGE` (the control plane's image digest the describe is recorded under,
``Settings.resolve_image_digest()``; never a docker image id).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import PLUGIN_KINDS, RunKind
from mapi_spec.jobs import PLUGIN_INPUT, PluginDescribeJob, PluginInspectJob
from mapi_spec.loader import spec_hash
from mapi_spec.plugins import MOUNTABLE_STATES, ArchiveLimits, OverlayPin, upload_dist_name
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.runs import Scope, create_run, now
from mapi_server.services.runspec import StoredRef

__all__ = [
    "LABEL_BASE_IMAGE",
    "LABEL_REASON",
    "LABEL_VERSION",
    "PIPELINE_LABELS",
    "DescribeReason",
    "archive_limits",
    "describe_pending",
    "describe_pin_problem",
    "is_pipeline_run",
    "plugin_of",
    "start_describe",
    "start_inspect",
    "version_pin",
]

LABEL_VERSION: Final = "mapi.plugin_version"
"""Run label: the plugin version a pipeline run checks."""
LABEL_REASON: Final = "mapi.plugin_reason"
"""Run label: why a describe run was queued (``upload`` or ``rekit``)."""
LABEL_BASE_IMAGE: Final = "mapi.plugin_base_image"
"""Run label: the base image digest a describe run's result is recorded under."""
PIPELINE_LABELS: Final = frozenset({LABEL_VERSION, LABEL_REASON, LABEL_BASE_IMAGE})
"""Platform-set labels of pipeline runs (never handed to an executor)."""

DescribeReason = Literal["upload", "rekit"]

_LIVE_STATES: Final = tuple(s.value for s in RunState if not s.terminal)


def archive_limits(ctx: ServerContext) -> ArchiveLimits:
    """The effective archive limits of an inspect run (the settings, not the job defaults)."""
    s = ctx.settings
    return ArchiveLimits(
        max_bytes=s.plugin_upload_max_bytes,
        max_uncompressed=s.plugin_uncompressed_max_bytes,
        max_member=s.plugin_member_max_bytes,
        max_members=s.plugin_max_members,
        max_ratio=s.plugin_max_ratio,
    )


async def plugin_of(session: AsyncSession, version: m.PluginVersion) -> m.Plugin:
    """The plugin of ``version`` (same tenant; a mismatch is treated as missing)."""
    plugin = await session.get(m.Plugin, version.plugin_id)
    if plugin is None or plugin.tenant_id != version.tenant_id:
        raise NotFoundError("plugin", version.plugin_id)
    return plugin


def version_pin(version: m.PluginVersion, namespace: str) -> OverlayPin:
    """The overlay pin of ``version`` (the sha256 the control plane computed at upload)."""
    return OverlayPin(
        plugin_version_id=version.id, namespace=namespace, sha256=version.upload_sha256
    )


async def _queue(
    session: AsyncSession,
    ctx: ServerContext,
    run: m.Run,
    *,
    created_by: str | None,
) -> m.Run:
    # imported here: submit imports services that import this package's siblings
    from mapi_server.services.submit import _accept

    # pipeline runs act for the uploader even when queued by a sweep or the finalizer
    # (no request): the gate and the audit see who the run belongs to
    run.created_by = created_by
    return await _accept(session, ctx, run, untrusted=True)


async def start_inspect(
    session: AsyncSession,
    ctx: ServerContext,
    version: m.PluginVersion,
    *,
    created_by: str | None,
) -> m.Run:
    """Queue the ``plugin_inspect`` run of an ``uploaded`` version and move the version to
    ``inspecting`` (the caller holds the version ``FOR UPDATE``).

    The run reads the quarantine object at the generation recorded at ``:complete``
    (``quarantine_key``/``quarantine_generation``) and checks its sha256 against
    ``upload_sha256`` before anything else. Admission refuses it like any untrusted run
    (``UNTRUSTED_PLACEMENT_UNAVAILABLE``, cut line A); the kill switch only holds it.
    """
    from mapi_server.services.plugins.store import set_state

    plugin = await plugin_of(session, version)
    job = PluginInspectJob(
        plugin_version_id=version.id,
        namespace=plugin.namespace,
        dist_name=upload_dist_name(plugin.namespace),
        version=version.version,
        expected_sha256=version.upload_sha256,
        limits=archive_limits(ctx),
    )
    payload = job.model_dump(mode="json", exclude_none=True)
    run = await create_run(
        session,
        Scope(version.tenant_id, version.project_id),
        kind=RunKind.PLUGIN_INSPECT,
        resource_class=ResourceClass.S,
        timeout_s=ctx.settings.plugin_inspect_timeout_s,
        payload=payload,
        inputs={
            PLUGIN_INPUT: StoredRef(
                ctx.buckets.quarantine, version.quarantine_key, version.quarantine_generation
            )
        },
        labels={LABEL_VERSION: version.id, LABEL_REASON: "upload"},
        spec_hash=spec_hash(payload),
    )
    run = await _queue(session, ctx, run, created_by=created_by)
    version.inspect_run_id = run.id
    if version.state == "uploaded":
        set_state(version, "inspecting", now=now())
    version.updated_at = now()
    return run


async def describe_pending(
    session: AsyncSession, version_id: str, base_image_digest: str
) -> m.Run | None:
    """A ``plugin_describe`` run of ``version_id`` for ``base_image_digest`` that has not
    ended yet (one describe per version and digest is enough)."""
    return (
        await session.execute(
            select(m.Run)
            .where(
                m.Run.kind == RunKind.PLUGIN_DESCRIBE.value,
                m.Run.labels[LABEL_VERSION].astext == version_id,
                m.Run.labels[LABEL_BASE_IMAGE].astext == base_image_digest,
                m.Run.state.in_(_LIVE_STATES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def start_describe(
    session: AsyncSession,
    ctx: ServerContext,
    version: m.PluginVersion,
    *,
    created_by: str | None,
    reason: DescribeReason,
) -> m.Run:
    """Queue the ``plugin_describe`` run of ``version`` for the current base image digest.

    ``reason="upload"``: the version passed inspect and its overlay copy exists; it moves
    ``inspecting → describing``. ``reason="rekit"`` (D12): a mountable version is described
    again for a new base image and keeps its state until the result lands. The caller holds
    the version ``FOR UPDATE``. The run mounts exactly this version
    (``runs.overlays = [pin]``), so it is untrusted by construction.
    """
    from mapi_server.services.plugins.store import set_state

    if version.overlay_key is None or version.overlay_generation is None:
        raise MapiError(
            f"The plugin version {version.id} has no checked copy to test yet.",
            code=ErrorCode.PLUGIN_STATE_CONFLICT,
        )
    plugin = await plugin_of(session, version)
    pin = version_pin(version, plugin.namespace)
    base = ctx.catalogs.image_digest
    job = PluginDescribeJob(
        plugin_version_id=version.id,
        namespace=plugin.namespace,
        overlay_sha256=version.upload_sha256,
        data_kits=True,
        data_check_timeout_s=ctx.settings.plugin_data_check_timeout_s,
    )
    payload = job.model_dump(mode="json", exclude_none=True)
    run = await create_run(
        session,
        Scope(version.tenant_id, version.project_id),
        kind=RunKind.PLUGIN_DESCRIBE,
        resource_class=ResourceClass.M,
        timeout_s=ctx.settings.plugin_describe_timeout_s,
        payload=payload,
        labels={LABEL_VERSION: version.id, LABEL_REASON: reason, LABEL_BASE_IMAGE: base},
        spec_hash=spec_hash(payload),
    )
    # set before admission: the DB requires overlays => untrusted_code, and admission keeps
    # what the row holds (the describe of a not-yet-mountable version is the one run that
    # may mount it; the dispatcher checks that pin against the version itself)
    run.overlays = [pin.model_dump(mode="json")]
    run.untrusted_code = True
    if reason == "upload" and version.state == "inspecting":
        # before admission, so its pin check sees the state the run is for
        set_state(version, "describing", now=now())
    run = await _queue(session, ctx, run, created_by=created_by)
    version.updated_at = now()
    return run


def is_pipeline_run(run: m.Run) -> bool:
    """Is ``run`` one of the two pipeline runs of a plugin version?"""
    return run.kind in {k.value for k in PLUGIN_KINDS}


async def describe_pin_problem(
    session: AsyncSession, run: m.Run, pins: Sequence[OverlayPin]
) -> str | None:
    """Why a ``plugin_describe`` run may not mount its one pin (plain text), else ``None``.

    A describe run mounts the version it checks, which is not mountable for anyone else yet
    (``describing``), or a mountable version described again for a new base image (rekit).
    The version must be the run's own (the ``plugin_version_id`` of its job), in the run's
    tenant, with the same sha256 and an inspected copy.

    Admission (``submit.admit``) and the dispatcher use this instead of
    :func:`~mapi_server.services.plugins.gate.pins_problem` for ``plugin_describe`` runs.
    """
    want = (run.payload or {}).get("plugin_version_id")
    if len(pins) != 1 or pins[0].plugin_version_id != want:
        return "A plugin check run must mount exactly the version it checks."
    version = await session.get(m.PluginVersion, pins[0].plugin_version_id)
    if (
        version is None
        or version.tenant_id != run.tenant_id
        or version.upload_sha256 != pins[0].sha256
        or version.overlay_key is None
    ):
        return "The plugin version to check is not available."
    if version.state != "describing" and version.state not in MOUNTABLE_STATES:
        return f"The plugin version to check is {version.state.replace('_', ' ')}."
    return None
