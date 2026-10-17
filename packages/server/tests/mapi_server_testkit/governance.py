"""Governance test helpers: play a sandboxed ``governance`` run to its report."""

from __future__ import annotations

from typing import Any

from mapi_server.context import ServerContext
from mapi_server.services.finalizer import Finalizer
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    GOVERNANCE_OUTPUT,
    CheckResult,
    CheckStatus,
    GovernanceJob,
    GovernanceReport,
)
from mapi_spec.run import RunState

from .builders import simulate_outputs
from .plots import _to_finalizing

__all__ = ["finish_governance", "governance_report"]


def governance_report(job: dict[str, Any], *, fail: str | None = None) -> bytes:
    """A ``governance.json`` answering every check of ``job`` (``fail`` fails one)."""
    j = GovernanceJob.model_validate(job)
    results = [
        CheckResult(
            check=c.check,
            severity=c.severity,
            metric=GOVERNANCE_CHECKS[c.check].metric,
            status=CheckStatus.FAIL if c.check == fail else CheckStatus.PASS,
            value=0.5 if c.check == fail else 0.01,
            message_plain=f"{c.check} measured",
            suggested_action="do something" if c.check == fail else None,
        )
        for c in j.checks
    ]
    report = GovernanceReport(
        model_ref=j.model_ref,
        model_id="ts.hsgp_trend",
        family="ts",
        model_kind="bayesian",
        results=results,
        refits=21,
    )
    return report.model_dump_json().encode()


async def finish_governance(ctx: ServerContext, run_id: str, *, fail: str | None = None) -> None:
    """Run the governance run ``run_id`` to ``succeeded`` with a report."""
    run, att = await _to_finalizing(ctx, run_id)
    body = governance_report(run.payload, fail=fail)
    await simulate_outputs(ctx, run, att, add={GOVERNANCE_OUTPUT: (body, "application/json")})
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
