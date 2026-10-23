"""What uses an uploaded plugin version (v0.19 D15): shown before a yank.

Runs record their pins in ``runs.overlays`` and model versions in ``lineage.overlays``
(both from the control plane), so the counts come from JSONB containment queries on the GIN
index ``ix_runs_overlays``; endpoints and dashboards are found through the model versions.
Each list holds at most :data:`LIST_MAX` ids; ``truncated`` says that more exist.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from mapi_spec.plugins import PluginUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.services.runs import now

__all__ = ["LIST_MAX", "RUNS_WINDOW", "plugin_usage"]

LIST_MAX: Final = 50
RUNS_WINDOW: Final = timedelta(days=30)


async def plugin_usage(session: AsyncSession, version: m.PluginVersion) -> PluginUsage:
    """Runs in the last 30 days, model versions, endpoints serving one of those model
    versions and dashboards with a readout of one, for ``version`` (the caller's tenant
    session; the tenant filter is repeated)."""
    tenant_id = version.tenant_id
    pin = [{"plugin_version_id": version.id}]
    runs = (
        await session.execute(
            select(func.count()).where(
                m.Run.tenant_id == tenant_id,
                m.Run.overlays.contains(pin),
                m.Run.created_at > now() - RUNS_WINDOW,
            )
        )
    ).scalar_one()
    mv_ids = list(
        (
            await session.execute(
                select(m.ModelVersion.id)
                .where(
                    m.ModelVersion.tenant_id == tenant_id,
                    m.ModelVersion.lineage.contains({"overlays": pin}),
                )
                .order_by(m.ModelVersion.created_at.desc(), m.ModelVersion.id)
                .limit(LIST_MAX + 1)
            )
        ).scalars()
    )
    truncated = len(mv_ids) > LIST_MAX
    mv_ids = mv_ids[:LIST_MAX]
    endpoints: list[str] = []
    dashboards: list[str] = []
    if mv_ids:
        endpoints = list(
            (
                await session.execute(
                    select(m.Endpoint.id)
                    .where(
                        m.Endpoint.tenant_id == tenant_id,
                        m.Endpoint.model_version_id.in_(mv_ids),
                    )
                    .order_by(m.Endpoint.id)
                    .limit(LIST_MAX + 1)
                )
            ).scalars()
        )
        dashboards = list(
            (
                await session.execute(
                    select(m.DashboardReadout.dashboard_id)
                    .join(m.Dashboard, m.Dashboard.id == m.DashboardReadout.dashboard_id)
                    .where(
                        m.DashboardReadout.tenant_id == tenant_id,
                        m.DashboardReadout.model_version_id.in_(mv_ids),
                        m.Dashboard.archived_at.is_(None),
                    )
                    .distinct()
                    .order_by(m.DashboardReadout.dashboard_id)
                    .limit(LIST_MAX + 1)
                )
            ).scalars()
        )
    truncated = truncated or len(endpoints) > LIST_MAX or len(dashboards) > LIST_MAX
    return PluginUsage(
        runs_30d=int(runs),
        model_versions=mv_ids,
        endpoints=endpoints[:LIST_MAX],
        dashboards=dashboards[:LIST_MAX],
        truncated=truncated,
    )
