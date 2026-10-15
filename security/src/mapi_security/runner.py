"""Execute the probe corpus against an executor's isolation level.

``run_probes`` runs the probes *in this process*, which is exactly the right environment for
the ``local-process`` executor: the suite process stands in for the dev-mode execution, with
no uid split, no egress firewall and no seccomp — so it measures what untrusted code could do
there. For the ``docker`` / ``docker-gvisor`` executors the isolation probes must run *inside*
the container to be meaningful; :class:`DockerProbeRunner` does that when a Docker daemon is
available and auto-skips the isolation probes otherwise (platform probes always run in
process, since they are control-plane checks independent of the sandbox).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass

from mapi_spec.executor import Isolation

from mapi_security.outcomes import Category, Outcome, ProbeResult
from mapi_security.probes import ALL_PROBES, get_probe

__all__ = [
    "EXECUTORS",
    "ExecutorProfile",
    "evaluate",
    "isolation_for",
    "run_probes",
    "run_probes_in_docker",
]


@dataclass(frozen=True, slots=True)
class ExecutorProfile:
    name: str
    isolation: Isolation
    docker_runtime: str | None = None  # 'runsc' for gVisor; None = in-process
    docker: bool = False


EXECUTORS: dict[str, ExecutorProfile] = {
    "local-process": ExecutorProfile("local-process", Isolation.PROCESS),
    "docker": ExecutorProfile("docker", Isolation.PROCESS, docker_runtime=None, docker=True),
    "docker-gvisor": ExecutorProfile(
        "docker-gvisor", Isolation.GVISOR, docker_runtime="runsc", docker=True
    ),
    "cloud": ExecutorProfile("cloud", Isolation.MICROVM),
}


def isolation_for(executor: str) -> Isolation:
    return EXECUTORS[executor].isolation


def run_probes(*, only: list[str] | None = None) -> list[ProbeResult]:
    """Run every probe (or only ``only``) in this process and collect the results."""
    probes = [get_probe(pid) for pid in only] if only else list(ALL_PROBES)
    results: list[ProbeResult] = []
    for probe in probes:
        try:
            results.append(probe.run())
        except Exception as exc:  # a probe must never crash the suite
            results.append(
                ProbeResult(
                    probe.probe_id,
                    probe.category,
                    Outcome.ERROR,
                    f"probe raised {type(exc).__name__}: {exc}",
                )
            )
    return results


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def run_probes_in_docker(profile: ExecutorProfile, *, image: str) -> list[ProbeResult]:
    """Run the isolation probes inside a container; platform probes run in process.

    Returns SKIPPED for the isolation probes when Docker (or the gVisor runtime) is not
    available, so the suite is still useful on a laptop without a daemon.
    """
    platform_results = run_probes(
        only=[p.probe_id for p in ALL_PROBES if p.category is Category.PLATFORM]
    )
    isolation_ids = [p.probe_id for p in ALL_PROBES if p.category is Category.ISOLATION]
    if not docker_available():
        skipped = [
            ProbeResult(pid, Category.ISOLATION, Outcome.SKIPPED, "Docker daemon not available")
            for pid in isolation_ids
        ]
        return _ordered(skipped + platform_results)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ]
    if profile.docker_runtime:
        cmd += ["--runtime", profile.docker_runtime]
    cmd += [image, "python", "-m", "mapi_security", "probe", "--category", "isolation", "--json"]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, timeout=300, text=True)
        parsed = json.loads(proc.stdout)
        iso_results = [
            ProbeResult(
                r["probe_id"],
                Category(r["category"]),
                Outcome(r["outcome"]),
                r["detail"],
                r.get("evidence", {}),
            )
            for r in parsed["results"]
        ]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        iso_results = [
            ProbeResult(pid, Category.ISOLATION, Outcome.ERROR, f"docker probe run failed: {exc}")
            for pid in isolation_ids
        ]
    return _ordered(iso_results + platform_results)


def _ordered(results: list[ProbeResult]) -> list[ProbeResult]:
    order = {p.probe_id: i for i, p in enumerate(ALL_PROBES)}
    return sorted(results, key=lambda r: order.get(r.probe_id, 1_000))


def evaluate(executor: str, *, image: str | None = None) -> list[ProbeResult]:
    """Run the corpus for ``executor`` (in process, or in a container for docker executors)."""
    profile = EXECUTORS[executor]
    if profile.docker:
        return run_probes_in_docker(profile, image=image or "mapi/worker:latest")
    return run_probes()


def _main_json(category: str | None) -> int:  # pragma: no cover - invoked inside containers
    only = None
    if category == "isolation":
        only = [p.probe_id for p in ALL_PROBES if p.category is Category.ISOLATION]
    elif category == "platform":
        only = [p.probe_id for p in ALL_PROBES if p.category is Category.PLATFORM]
    results = run_probes(only=only)
    json.dump({"results": [r.to_json() for r in results]}, sys.stdout)
    return 0
