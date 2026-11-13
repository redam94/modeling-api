"""Who may mount which uploaded plugin version (v0.19 D9, D11, D13, D15).

This module is the one place that decides it. :func:`pins_problem` answers for the overlay
pins of a run (admission in ``services.submit`` and the dispatcher's re-check before every
pick); :func:`visible_versions` lists the versions whose exports the caller's tenant catalog
view shows. Both read only the database: never the wheel.

A version may be mounted when it belongs to the tenant, its stored hash is the pinned one,
and its state is in :data:`~mapi_spec.plugins.MOUNTABLE_STATES` and it is neither yanked nor
purged; then either it is ``approved`` (the whole tenant) or the run's creator uploaded it
and the tenant allows private use (``tenant_plugin_settings.private_use``, on by default).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.plugins import MOUNTABLE_STATES, OverlayPin, PluginExports
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m

__all__ = [
    "VisibleVersion",
    "describe_for",
    "pins_problem",
    "private_use_enabled",
    "visible_versions",
]


@dataclass(frozen=True)
class VisibleVersion:
    """One version whose exports a caller's catalog view shows (``scope`` says why: approved
    for the tenant, or the caller's own version in private use)."""

    version_id: str
    plugin_id: str
    namespace: str
    sha256: str
    describe_digest: str
    scope: Literal["tenant", "private"]
    uploaded_by: str
    exports: PluginExports
    plot_examples: Mapping[str, Any]


async def private_use_enabled(session: AsyncSession, tenant_id: str) -> bool:
    """The tenant's private-use setting (on when the tenant has no settings row yet)."""
    row = await session.get(m.TenantPluginSettings, tenant_id)
    return True if row is None else bool(row.private_use)


def _label(version: m.PluginVersion, namespace: str) -> str:
    return f"{namespace} {version.version}"


def _unavailable(detail: str) -> Problem:
    return Problem.from_code(ErrorCode.PLUGIN_UNAVAILABLE, detail)


async def pins_problem(
    session: AsyncSession,
    *,
    tenant_id: str,
    created_by: str | None,
    pins: Sequence[OverlayPin],
) -> Problem | None:
    """Why the run of ``created_by`` may not mount ``pins`` (``PLUGIN_UNAVAILABLE`` with a
    plain reason naming the plugin), or ``None`` when every pin may be mounted.

    ``created_by`` is the person the run acts for (``None``: nobody, so private versions are
    refused). The caller holds a session of ``tenant_id``; the tenant filter is repeated
    here so a platform session cannot leak another tenant's versions.
    """
    if not pins:
        return None
    ids = sorted({p.plugin_version_id for p in pins})
    rows = (
        await session.execute(
            select(m.PluginVersion, m.Plugin.namespace)
            .join(
                m.Plugin,
                (m.Plugin.id == m.PluginVersion.plugin_id)
                & (m.Plugin.tenant_id == m.PluginVersion.tenant_id),
            )
            .where(m.PluginVersion.tenant_id == tenant_id, m.PluginVersion.id.in_(ids))
        )
    ).all()
    found: dict[str, tuple[m.PluginVersion, str]] = {v.id: (v, ns) for v, ns in rows}
    private_use: bool | None = None
    for pin in pins:
        hit = found.get(pin.plugin_version_id)
        if hit is None:
            return _unavailable(
                f"The uploaded plugin {pin.namespace} (version {pin.plugin_version_id}) does "
                "not exist in this workspace."
            )
        version, namespace = hit
        label = _label(version, namespace)
        if namespace != pin.namespace or version.upload_sha256 != pin.sha256:
            return _unavailable(
                f"The uploaded plugin {label} is not the file that this item was made with."
            )
        if version.purged_at is not None or version.state == "purged":
            return _unavailable(f"The uploaded plugin {label} was removed.")
        if version.yanked_at is not None or version.state == "yanked":
            return _unavailable(f"The uploaded plugin {label} was withdrawn.")
        if version.state not in MOUNTABLE_STATES:
            return _unavailable(
                f"The uploaded plugin {label} cannot be used: it did not pass its checks."
            )
        if version.state == "approved":
            continue
        if created_by is None or created_by != version.uploaded_by:
            return _unavailable(
                f"The uploaded plugin {label} is not approved for the workspace yet. Only "
                "the person who uploaded it can use it until a tenant admin approves it."
            )
        if private_use is None:
            private_use = await private_use_enabled(session, tenant_id)
        if not private_use:
            return _unavailable(
                f"The uploaded plugin {label} is not approved for the workspace yet, and a "
                "tenant admin turned off the use of uploads before approval."
            )
    return None


async def describe_for(
    session: AsyncSession, version_id: str, base_image_digest: str | None
) -> m.PluginDescribe | None:
    """The describe result of a version for ``base_image_digest``, else its newest one that
    passed (D12: until a rekit lands, the view keeps the newest describe), else its newest."""
    if base_image_digest is not None:
        row = await session.get(m.PluginDescribe, (version_id, base_image_digest))
        if row is not None:
            return row
    rows = (
        await session.execute(
            select(m.PluginDescribe)
            .where(m.PluginDescribe.version_id == version_id)
            .order_by(m.PluginDescribe.ok.desc(), m.PluginDescribe.created_at.desc())
            .limit(1)
        )
    ).scalars()
    return next(iter(rows), None)


def _newest_ok(rows: Iterable[m.PluginDescribe], base_image_digest: str) -> m.PluginDescribe | None:
    exact = None
    newest = None
    for row in rows:
        if not row.ok:
            continue
        if row.base_image_digest == base_image_digest:
            exact = row
        if newest is None or row.created_at > newest.created_at:
            newest = row
    return exact or newest


async def visible_versions(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str | None,
    base_image_digest: str,
) -> list[VisibleVersion]:
    """The versions whose exports ``user_id``'s catalog view of ``tenant_id`` shows (D11):
    every ``approved`` version, plus the caller's own ``kit_passed``/``awaiting_approval``
    versions while the tenant allows private use. Exports come from the describe result for
    ``base_image_digest`` (else the version's newest passing describe). Ordered by namespace
    and version id."""
    private = user_id is not None and await private_use_enabled(session, tenant_id)
    cond = m.PluginVersion.state == "approved"
    if private:
        cond = cond | (
            m.PluginVersion.state.in_(("kit_passed", "awaiting_approval"))
            & (m.PluginVersion.uploaded_by == user_id)
        )
    rows = (
        await session.execute(
            select(m.PluginVersion, m.Plugin.namespace)
            .join(
                m.Plugin,
                (m.Plugin.id == m.PluginVersion.plugin_id)
                & (m.Plugin.tenant_id == m.PluginVersion.tenant_id),
            )
            .where(
                m.PluginVersion.tenant_id == tenant_id,
                m.PluginVersion.yanked_at.is_(None),
                m.PluginVersion.purged_at.is_(None),
                cond,
            )
            .order_by(m.Plugin.namespace, m.PluginVersion.id)
        )
    ).all()
    if not rows:
        return []
    describes = (
        await session.execute(
            select(m.PluginDescribe).where(m.PluginDescribe.version_id.in_([v.id for v, _ in rows]))
        )
    ).scalars()
    by_version: dict[str, list[m.PluginDescribe]] = {}
    for row in describes:
        by_version.setdefault(row.version_id, []).append(row)
    out: list[VisibleVersion] = []
    for version, namespace in rows:
        d = _newest_ok(by_version.get(version.id, ()), base_image_digest)
        if d is None:  # a mountable version always has one; a missing row shows nothing
            continue
        out.append(
            VisibleVersion(
                version_id=version.id,
                plugin_id=version.plugin_id,
                namespace=namespace,
                sha256=version.upload_sha256,
                describe_digest=d.describe_digest,
                scope="tenant" if version.state == "approved" else "private",
                uploaded_by=version.uploaded_by,
                exports=PluginExports.model_validate(d.exports),
                plot_examples=dict(d.plot_examples or {}),
            )
        )
    return out
