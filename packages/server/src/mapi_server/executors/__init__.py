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

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import Executor, ExecutorCaps
from mapi_spec.resources import ResourceClass
from mapi_spec.tenancy import TIER_PLACEMENT, TrustTier, placement_allows

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
    return DockerExecutor(
        image=settings.docker_image,
        images=settings.docker_images,
        bootstrap_url_template=template,
        exec_token=settings.resolved_exec_token(),
        network=settings.docker_network,
        gvisor=settings.executor is ExecutorName.DOCKER_GVISOR,
        gvisor_runtime=settings.docker_runtime_gvisor,
        cancel_grace_s=settings.cancel_grace_s,
    )
