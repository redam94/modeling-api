"""Executor drivers behind the :class:`mapi_spec.executor.Executor` protocol (plan §8.4).

* :class:`LocalProcessExecutor` (``local-process``, PROCESS isolation; dev loop and tests;
  refuses multi-tenant profiles);
* :class:`DockerExecutor` (``docker`` / ``docker-gvisor``).

Placement is enforced in code (:func:`check_placement`), never by tenant configuration.
"""

from __future__ import annotations

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import Executor, ExecutorCaps
from mapi_spec.resources import ResourceClass

from mapi_server.executors.docker import DockerExecutor, docker_available
from mapi_server.executors.local_process import ExecutorRefused, LocalProcessExecutor
from mapi_server.settings import ExecutorName, Settings

__all__ = [
    "DockerExecutor",
    "Executor",
    "ExecutorRefused",
    "LocalProcessExecutor",
    "PlacementRefused",
    "build_executor",
    "check_placement",
    "docker_available",
]


class PlacementRefused(MapiError):
    """The executor's isolation is not allowed for this tenant tier or resource class."""

    default_code = ErrorCode.FORBIDDEN


def check_placement(caps: ExecutorCaps, *, trust_tier: str, resource_class: ResourceClass) -> None:
    """Public tenants need isolation >= gVisor with egress and DNS enforced (plan §8.4)."""
    if trust_tier == "public" and not caps.allows_public_tenants():
        raise PlacementRefused(
            "public tenants require an executor with isolation >= gVisor and enforced egress/DNS"
        )
    if resource_class not in caps.classes:
        raise PlacementRefused(f"the executor does not offer resource class {resource_class.value}")


def build_executor(settings: Settings) -> Executor:
    """The executor selected by ``MAPI_EXECUTOR``."""
    template = settings.bootstrap_url_template()
    if settings.executor is ExecutorName.LOCAL_PROCESS:
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
