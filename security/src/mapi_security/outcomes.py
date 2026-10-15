"""Outcomes and the per-probe result record for the security suite.

A probe *attempts* a hostile action and reports what the environment permitted:

* ``ALLOWED``  — the action succeeded. On an enforcing executor this is a finding.
* ``BLOCKED``  — a control refused the action (permission error, firewall, NXDOMAIN, a
  sanitizer rejection, ...). This is the desired outcome under isolation.
* ``ERROR``    — the probe could not run (a bug in the probe or an unexpected environment).
* ``SKIPPED``  — the probe did not run here (e.g. a Docker executor with no daemon).

A probe never decides whether its outcome is acceptable; that is the expected-outcome matrix
(:mod:`mapi_security.matrix`), which depends on the executor's isolation level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Category", "Outcome", "ProbeResult"]


class Outcome(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    ERROR = "error"
    SKIPPED = "skipped"


class Category(StrEnum):
    """What kind of control a probe exercises."""

    ISOLATION = "isolation"
    """An OS/sandbox control: egress, DNS, filesystem, /proc, fork, memory, disk."""
    PLATFORM = "platform"
    """A control-plane defence: event HMAC, manifest integrity, plot/schema sanitizers.

    Platform controls hold regardless of the executor, so these must be BLOCKED everywhere.
    """


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: str
    category: Category
    outcome: Outcome
    detail: str
    evidence: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "probe_id": self.probe_id,
            "category": self.category.value,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "evidence": self.evidence,
        }
