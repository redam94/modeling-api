"""Render a probe run: a human table, a JSON document, and a Prometheus textfile.

The Prometheus textfile exports ``mapi_security_probe_unexpected{executor=...}`` — the gauge
the ops alert ``MapiSecurityProbeFailure`` pages on. A value above zero means at least one
probe's outcome was not acceptable for that executor's isolation level.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mapi_spec.executor import Isolation

from mapi_security.matrix import acceptable, is_unexpected
from mapi_security.outcomes import Outcome, ProbeResult
from mapi_security.probes import get_probe

__all__ = ["Report", "Row", "build_report"]


@dataclass(frozen=True, slots=True)
class Row:
    probe_id: str
    category: str
    description: str
    outcome: Outcome
    acceptable: frozenset[Outcome]
    unexpected: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Report:
    executor: str
    isolation: Isolation
    rows: list[Row]

    @property
    def unexpected(self) -> list[Row]:
        return [r for r in self.rows if r.unexpected]

    @property
    def ok(self) -> bool:
        return not self.unexpected

    def to_json(self) -> dict[str, object]:
        return {
            "executor": self.executor,
            "isolation": self.isolation.name.lower(),
            "ok": self.ok,
            "unexpected": [r.probe_id for r in self.unexpected],
            "rows": [
                {
                    "probe_id": r.probe_id,
                    "category": r.category,
                    "outcome": r.outcome.value,
                    "acceptable": sorted(o.value for o in r.acceptable),
                    "unexpected": r.unexpected,
                    "detail": r.detail,
                }
                for r in self.rows
            ],
        }

    def table(self) -> str:
        head = f"Security suite — executor={self.executor} isolation={self.isolation.name.lower()}"
        lines = [head, "=" * len(head), ""]
        w = max((len(r.probe_id) for r in self.rows), default=10)
        lines.append(f"  {'PROBE':<{w}}  {'CATEGORY':<9}  {'OUTCOME':<8}  {'OK?':<3}  DETAIL")
        for r in self.rows:
            flag = "!!" if r.unexpected else "ok"
            lines.append(
                f"  {r.probe_id:<{w}}  {r.category:<9}  {r.outcome.value:<8}  {flag:<3}  {r.detail}"
            )
        lines.append("")
        if self.ok:
            lines.append(f"PASS: all {len(self.rows)} probe outcomes acceptable for this executor.")
        else:
            lines.append(
                f"FAIL: {len(self.unexpected)} unexpected outcome(s): "
                + ", ".join(r.probe_id for r in self.unexpected)
            )
        return "\n".join(lines)

    def write_metrics(self, path: Path) -> None:
        """Write the Prometheus textfile the ops alert scrapes."""
        text = (
            "# HELP mapi_security_probe_unexpected Probes whose outcome was unacceptable.\n"
            "# TYPE mapi_security_probe_unexpected gauge\n"
            f'mapi_security_probe_unexpected{{executor="{self.executor}"}} '
            f"{len(self.unexpected)}\n"
            "# HELP mapi_security_probe_total Probes evaluated.\n"
            "# TYPE mapi_security_probe_total gauge\n"
            f'mapi_security_probe_total{{executor="{self.executor}"}} {len(self.rows)}\n'
        )
        path.write_text(text)

    def write_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n")


def build_report(executor: str, isolation: Isolation, results: list[ProbeResult]) -> Report:
    rows: list[Row] = []
    for res in results:
        ok_set = acceptable(res.probe_id, res.category, isolation)
        rows.append(
            Row(
                probe_id=res.probe_id,
                category=res.category.value,
                description=get_probe(res.probe_id).description,
                outcome=res.outcome,
                acceptable=ok_set,
                unexpected=is_unexpected(res.probe_id, res.category, isolation, res.outcome),
                detail=res.detail,
            )
        )
    return Report(executor=executor, isolation=isolation, rows=rows)
