"""Executor drivers behind the :class:`mapi_spec.executor.Executor` protocol (plan §8.4).

* :class:`LocalProcessExecutor` (``local-process``, PROCESS isolation; dev loop and tests;
  refuses multi-tenant profiles);
* :class:`DockerExecutor` (``docker`` / ``docker-gvisor``);
* :class:`CloudRunJobsExecutor` (``cloudrun-jobs``, MICROVM; the primary cloud backend, over
  the Cloud Run Admin API — :class:`FakeCloudRun` stands in for it in tests).

Placement is enforced in code (:func:`check_placement`), never by tenant configuration.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import Executor, ExecutorCaps
from mapi_spec.resources import ResourceClass
from mapi_spec.tenancy import TIER_PLACEMENT, TrustTier, placement_allows
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.executors.cloudrun import (
    Base,
    CloudRunApi,
    CloudRunJobsExecutor,
    GoogleCloudRunApi,
    JobInventory,
)
from mapi_server.executors.docker import DockerExecutor, docker_available
from mapi_server.executors.local_process import ExecutorRefused, LocalProcessExecutor
from mapi_server.settings import ExecutorName, Profile, Settings

__all__ = [
    "CloudRunJobsExecutor",
    "DockerExecutor",
    "Executor",
    "ExecutorRefused",
    "LocalProcessExecutor",
    "PlacementRefused",
    "build_cloudrun_executor",
    "build_executor",
    "check_placement",
    "cloudrun_inventory",
    "docker_available",
    "placement_refusal",
    "placement_warnings",
    "reported_caps",
]


log = logging.getLogger(__name__)


class PlacementRefused(MapiError):
    """The executor's isolation is not allowed for this tenant tier or resource class."""

    default_code = ErrorCode.FORBIDDEN


def check_placement(
    caps: ExecutorCaps,
    *,
    trust_tier: str,
    resource_class: ResourceClass,
    residency: str | None = None,
) -> None:
    """Enforce :data:`mapi_spec.tenancy.TIER_PLACEMENT` in code (plan §8.4): shared tiers
    (``verified``, ``public``) need isolation >= gVisor with egress and DNS enforced. Data
    residency is checked against executors that declare regions (local executors have none).
    """
    try:
        tier = TrustTier(trust_tier)
    except ValueError:
        tier = TrustTier.PUBLIC  # unknown tiers get the strictest placement
    region = caps.regions[0] if caps.regions else None
    if not placement_allows(
        tier, caps, residency=residency if caps.regions else None, region=region
    ):
        req = TIER_PLACEMENT[tier]
        if caps.isolation < req.min_isolation or (
            req.egress_enforced and not (caps.egress_enforced and caps.dns_enforced)
        ):
            raise PlacementRefused(
                f"{tier.value} tenants require an executor with isolation >= "
                f"{req.min_isolation.name.lower()}"
                + (" and enforced egress/DNS" if req.egress_enforced else "")
            )
        raise PlacementRefused(f"the executor's regions do not satisfy residency {residency}")
    if resource_class not in caps.classes:
        raise PlacementRefused(f"the executor does not offer resource class {resource_class.value}")


def placement_refusal(
    offered: Sequence[ExecutorCaps],
    *,
    trust_tier: str,
    resource_class: ResourceClass,
    residency: str | None,
) -> str | None:
    """Why none of the ``offered`` executors may run this work (``None``: one may, or
    nothing is known about the executors)."""
    reasons: list[str] = []
    for caps in offered:
        try:
            check_placement(
                caps, trust_tier=trust_tier, resource_class=resource_class, residency=residency
            )
        except PlacementRefused as exc:
            reasons.append(exc.detail)
        else:
            return None
    return reasons[0] if reasons else None


async def reported_caps(s: AsyncSession, *, max_age: timedelta) -> list[ExecutorCaps]:
    """Capabilities of the executors whose dispatcher reported recently (``executor_reports``):
    what the API can check placement against at admission (it runs no executor itself)."""
    rows = (
        await s.execute(
            select(m.ExecutorReport.caps).where(
                m.ExecutorReport.enabled.is_(True),
                m.ExecutorReport.checked_at >= datetime.now(UTC) - max_age,
            )
        )
    ).scalars()
    out: list[ExecutorCaps] = []
    for raw in rows:
        try:
            out.append(ExecutorCaps.model_validate(raw))
        except ValueError:  # a report from another version: ignore it
            continue
    return out


async def placement_warnings(s: AsyncSession, caps: ExecutorCaps) -> list[str]:
    """One line per active tenant this executor cannot place (tier or residency): logged by
    the worker at start-up, because every run of such a tenant would be refused."""
    tenants = (
        await s.execute(select(m.Tenant).where(m.Tenant.status == "active").order_by(m.Tenant.id))
    ).scalars()
    smallest = caps.classes[0] if caps.classes else ResourceClass.S
    out: list[str] = []
    for t in tenants:
        why = placement_refusal(
            [caps], trust_tier=t.trust_tier, resource_class=smallest, residency=t.data_residency
        )
        if why is not None:
            out.append(
                f"tenant {t.id} ({t.trust_tier}, residency {t.data_residency}) cannot run on "
                f"this executor: {why}"
            )
    return out


def cloudrun_inventory(settings: Settings) -> JobInventory:
    """The Job inventory the Cloud Run settings describe."""
    if not settings.cloudrun_project:
        raise ExecutorRefused("MAPI_EXECUTOR=cloudrun-jobs needs MAPI_CLOUDRUN_PROJECT")
    return JobInventory(
        project=settings.cloudrun_project,
        region=settings.cloudrun_region,
        env=settings.env,
        template=settings.cloudrun_job_template,
        plugin_bases={k: Base(v) for k, v in settings.cloudrun_plugin_bases.items()},
        default_base=Base(settings.cloudrun_default_base),
    )


def build_cloudrun_executor(
    settings: Settings, api: CloudRunApi | None = None
) -> CloudRunJobsExecutor:
    """``cloudrun-jobs`` from settings (``api`` defaults to the real google-cloud-run client)."""
    inventory = cloudrun_inventory(settings)
    if settings.profile is Profile.MULTI_TENANT and settings.pubsub_push_audience is None:
        log.warning(
            "cloudrun-jobs in a multi-tenant profile without MAPI_PUBSUB_PUSH_AUDIENCE: "
            "POST /internal/events will refuse every push until it is configured"
        )
    return CloudRunJobsExecutor(
        api if api is not None else GoogleCloudRunApi(),
        inventory,
        submits_per_minute=settings.cloudrun_submits_per_minute,
        burst=settings.cloudrun_submit_burst,
        max_rate_limit_retries=settings.cloudrun_max_429_retries,
        backoff_base_s=settings.cloudrun_backoff_base_s,
        backoff_cap_s=settings.cloudrun_backoff_cap_s,
        timeout_margin_s=settings.cloudrun_timeout_margin_s,
        egress_enforced=settings.cloudrun_egress_enforced,
        dns_enforced=settings.cloudrun_dns_enforced,
        adopt_lookback_s=settings.cloudrun_adopt_lookback_s,
    )


def build_executor(settings: Settings) -> Executor:
    """The executor selected by ``MAPI_EXECUTOR``."""
    if settings.executor is ExecutorName.CLOUDRUN_JOBS:
        return build_cloudrun_executor(settings)
    template = settings.bootstrap_url_template()
    if settings.executor is ExecutorName.LOCAL_PROCESS:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        log.warning(
            "local-process executor: runs execute plugin code under the same uid (%s) and "
            "filesystem as this orchestrator. Run code can read the object store's control "
            "key (%s), the bundle-signing key (%s) and the executions directory (%s), so "
            "bundle signatures and finalizer checks do not hold against it. Use it only with "
            "trusted plugin code; the docker executor runs each attempt in its own container",
            uid,
            settings.gcs_key_path("control"),
            settings.key_path,
            settings.workdir,
        )
        return LocalProcessExecutor(
            command=settings.runner_command,
            bootstrap_url_template=template,
            workdir=settings.workdir,
            exec_token=settings.resolved_exec_token(),
            profile=settings.profile,
            cancel_grace_s=settings.cancel_grace_s,
        )
    if settings.docker_network == "none" and template.startswith(("http://", "https://")):
        raise ExecutorRefused(
            "MAPI_DOCKER_NETWORK=none: run containers could not fetch the bootstrap object "
            f"from {template.split('/storage/')[0]}; set MAPI_DOCKER_NETWORK to "
            "container:<gcs-shim container> or to an internal network that holds the shim"
        )
    if settings.docker_network_isolated and settings.docker_network in (
        "host",
        "bridge",
        "default",
    ):
        raise ExecutorRefused(
            f"MAPI_DOCKER_NETWORK_ISOLATED: {settings.docker_network!r} reaches the outside; "
            "use an internal network (docker network create --internal) that holds only the "
            "GCS shim"
        )
    return DockerExecutor(
        image=settings.docker_image,
        images=settings.docker_images,
        bootstrap_url_template=template,
        exec_token=settings.resolved_exec_token(),
        network=settings.docker_network,
        network_isolated=settings.docker_network_isolated,
        gvisor=settings.executor is ExecutorName.DOCKER_GVISOR,
        gvisor_runtime=settings.docker_runtime_gvisor,
        cancel_grace_s=settings.cancel_grace_s,
    )
