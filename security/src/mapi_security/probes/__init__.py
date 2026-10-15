"""The probe registry.

Each probe is a :class:`Probe`: an id, a category, a one-line description, and a callable that
*attempts* a hostile action and returns a :class:`~mapi_security.outcomes.ProbeResult`. Probes
are deliberately safe to run — fork/memory/disk bombs are bounded, network probes use a short
timeout and reserved addresses, and nothing is left behind.

The corpus is the plan's §13 security-suite list: metadata-server token fetch, HTTP and DNS
egress, writes outside signed objects, reuse of another run's URL, reading ``/proc/1`` and the
supervisor's memory, ``.pth``/``sitecustomize`` injection, fork/memory/disk bombs, log flood,
forged stdout events, a tampered manifest, a ReDoS JSON schema, and plot XSS/exfil specs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mapi_security.outcomes import Category, ProbeResult

__all__ = ["ALL_PROBES", "Probe", "get_probe"]


@dataclass(frozen=True, slots=True)
class Probe:
    probe_id: str
    category: Category
    description: str
    run: Callable[[], ProbeResult]


def _collect() -> tuple[Probe, ...]:
    from mapi_security.probes import isolation, platform

    probes = (*isolation.PROBES, *platform.PROBES)
    seen: set[str] = set()
    for p in probes:
        if p.probe_id in seen:
            raise ValueError(f"duplicate probe id {p.probe_id!r}")
        seen.add(p.probe_id)
    return probes


ALL_PROBES: tuple[Probe, ...] = _collect()


def get_probe(probe_id: str) -> Probe:
    for p in ALL_PROBES:
        if p.probe_id == probe_id:
            return p
    raise KeyError(probe_id)
