"""The expected-outcome matrix: what each probe may do at each isolation level (plan §9, §13).

The platform is honest that **local-process is not a security boundary** (§8.4: a startup
check refuses it in any multi-tenant profile). So at ``PROCESS`` isolation most OS-level
probes are expected to be ALLOWED in development, and that is documented here rather than
pretended away. Network probes at ``PROCESS`` are environment-dependent (a dev laptop has no
metadata server and no egress firewall, so the connection may simply fail), so both outcomes
are accepted.

At ``GVISOR`` and above (docker-gvisor, Cloud Run microVM) every isolation probe **must** be
BLOCKED — that is the gate an executor passes before it may run public tenants.

Platform probes exercise control-plane defences (event HMAC, manifest integrity, the plot and
schema sanitizers). Those do not depend on the executor, so they must be BLOCKED at every
level, including local-process.
"""

from __future__ import annotations

from mapi_spec.executor import Isolation

from mapi_security.outcomes import Category, Outcome
from mapi_security.probes import ALL_PROBES

__all__ = ["acceptable", "expected_outcomes", "is_unexpected"]

# Accepted outcome sets, by (isolation-level-is-enforcing, category).
_ENFORCED = frozenset({Outcome.BLOCKED})
_DEV_ISOLATION = frozenset({Outcome.ALLOWED, Outcome.BLOCKED})
# Probes whose PROCESS-level outcome is environment-dependent even in dev (network reach).
_NETWORK_PROBES = frozenset({"metadata_token", "http_egress", "dns_egress", "reuse_foreign_url"})


def _enforces(isolation: Isolation) -> bool:
    """Whether this isolation level must block every sandbox probe (GVISOR and above)."""
    return isolation >= Isolation.GVISOR


def acceptable(probe_id: str, category: Category, isolation: Isolation) -> frozenset[Outcome]:
    """The outcomes that are acceptable for ``probe_id`` at ``isolation``.

    SKIPPED is always acceptable (a probe that could not run here is not a finding); ERROR is
    never acceptable (the suite must be able to run its own probes).
    """
    base: frozenset[Outcome]
    if category is Category.PLATFORM or _enforces(isolation):
        base = _ENFORCED
    elif probe_id in _NETWORK_PROBES:
        base = _DEV_ISOLATION
    else:
        # dev OS-level probe: ALLOWED is expected (no boundary), BLOCKED is fine too.
        base = _DEV_ISOLATION
    return base | {Outcome.SKIPPED}


def is_unexpected(probe_id: str, category: Category, isolation: Isolation, got: Outcome) -> bool:
    """True if ``got`` is a finding at ``isolation`` (what the alert and exit code key on)."""
    return got not in acceptable(probe_id, category, isolation)


def expected_outcomes(isolation: Isolation) -> dict[str, frozenset[Outcome]]:
    """The acceptable-outcome set for every registered probe at ``isolation`` (for display)."""
    return {p.probe_id: acceptable(p.probe_id, p.category, isolation) for p in ALL_PROBES}
