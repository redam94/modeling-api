"""Operator controls: kill switches and executor health (plan §9 denial of wallet, §11 Admin).

:class:`KillSwitches` is a single platform-wide document. :func:`kill_switch_decision` is the
pure check the API (at submit) and the dispatcher (at lease) both apply: when
``untrusted_execution_enabled`` is off, no run of uploaded (tenant) code is accepted or
leased; when the global spend in the current window reaches ``global_spend_cap_micros``,
no new compute is accepted for anyone except ``internal`` tenants. Both denials use the
``QUOTA_EXCEEDED`` problem code at the ``global`` scope.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, dto_examples
from mapi_spec.conformance_executor import ConformanceReport, ConformanceTarget
from mapi_spec.cost import format_micros
from mapi_spec.errors import ErrorCode
from mapi_spec.executor import ExecutorCaps
from mapi_spec.quotas import (
    GLOBAL_SUBJECT,
    LimitName,
    QuotaDecision,
    QuotaScope,
    window_reset,
)
from mapi_spec.tenancy import TrustTier

__all__ = [
    "DEFAULT_KILL_SWITCHES",
    "NETWORK_PROBES",
    "ExecutorAttestation",
    "ExecutorEvidence",
    "ExecutorEvidenceUpdate",
    "ExecutorHealth",
    "ExecutorStatus",
    "KillSwitchUpdate",
    "KillSwitches",
    "SecurityProbeRow",
    "SecuritySuiteReport",
    "kill_switch_decision",
    "shared_tiers_problem",
]


class KillSwitches(ResponseModel):
    """Platform-wide emergency controls (operators only; every change is audited)."""

    untrusted_execution_enabled: bool = Field(
        True, description="Off: no run of tenant-uploaded plugin code is accepted or leased"
    )
    global_spend_cap_micros: int | None = Field(
        None, ge=0, description="Stop new compute once the platform spent this much"
    )
    spend_cap_currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    spend_cap_period: Literal["day", "month"] = "day"
    reason: str | None = Field(None, max_length=500)
    updated_at: AwareDatetime | None = None
    updated_by: Id | None = None

    model_config = dto_examples(
        {
            "untrusted_execution_enabled": False,
            "global_spend_cap_micros": 500_000_000,
            "spend_cap_currency": "USD",
            "spend_cap_period": "day",
            "reason": "incident 2026-10-15: suspected sandbox probe; code runs paused",
            "updated_at": EXAMPLE_TS,
            "updated_by": "usr_01jaf3a0b0c0d0e0f0g0h0j0k0",
        }
    )


DEFAULT_KILL_SWITCHES: Final = KillSwitches()


class KillSwitchUpdate(RequestModel):
    """``PUT /v1/admin/kill-switches``: fields left out keep their value; ``reason`` is
    required so the audit log says why."""

    untrusted_execution_enabled: bool | None = None
    global_spend_cap_micros: int | None = Field(None, ge=0)
    clear_spend_cap: bool = Field(False, description="Remove the spend cap")
    spend_cap_period: Literal["day", "month"] | None = None
    reason: str = Field(..., min_length=3, max_length=500)

    model_config = dto_examples(
        {"untrusted_execution_enabled": False, "reason": "incident 2026-10-15"},
        {"global_spend_cap_micros": 500_000_000, "reason": "daily cap for the beta"},
    )

    @model_validator(mode="after")
    def _cap(self) -> KillSwitchUpdate:
        if self.clear_spend_cap and self.global_spend_cap_micros is not None:
            raise ValueError("give either global_spend_cap_micros or clear_spend_cap")
        return self

    def apply(self, current: KillSwitches, *, now: datetime, by: str | None) -> KillSwitches:
        """The switches after this update."""
        data = current.model_dump()
        if self.untrusted_execution_enabled is not None:
            data["untrusted_execution_enabled"] = self.untrusted_execution_enabled
        if self.global_spend_cap_micros is not None:
            data["global_spend_cap_micros"] = self.global_spend_cap_micros
        if self.clear_spend_cap:
            data["global_spend_cap_micros"] = None
        if self.spend_cap_period is not None:
            data["spend_cap_period"] = self.spend_cap_period
        data.update(reason=self.reason, updated_at=now, updated_by=by)
        return KillSwitches.model_validate(data)


_ADJ: Final = {"day": "daily", "month": "monthly"}


def kill_switch_decision(
    switches: KillSwitches,
    *,
    untrusted_code: bool,
    tier: TrustTier,
    spent_micros: int,
    now: datetime,
) -> QuotaDecision:
    """May a new run start? ``untrusted_code``: the run executes tenant-uploaded plugin code
    (T2/T3). ``spent_micros``: platform spend in the current cap window (same currency)."""
    if untrusted_code and not switches.untrusted_execution_enabled:
        return QuotaDecision(
            allowed=False,
            code=ErrorCode.QUOTA_EXCEEDED,
            scope=QuotaScope.GLOBAL,
            subject_id=GLOBAL_SUBJECT,
            limit_name=LimitName.UNTRUSTED_EXECUTION,
            message="running uploaded plugin code is paused platform-wide",
        )
    cap = switches.global_spend_cap_micros
    if cap is not None and tier is not TrustTier.INTERNAL and spent_micros >= cap:
        retry = window_reset(switches.spend_cap_period, now) - now
        cur = switches.spend_cap_currency
        return QuotaDecision(
            allowed=False,
            code=ErrorCode.QUOTA_EXCEEDED,
            scope=QuotaScope.GLOBAL,
            subject_id=GLOBAL_SUBJECT,
            limit_name=LimitName.GLOBAL_SPEND,
            limit=cap,
            used=spent_micros,
            retry_after_seconds=max(1, int(retry.total_seconds())),
            message=(
                f"the platform's {_ADJ[switches.spend_cap_period]} spend cap "
                f"({format_micros(cap, cur)}) is reached"
            ),
        )
    return QuotaDecision.allow()


class ExecutorStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    DISABLED = "disabled"
    """Configured but not enabled (for example: conformance not passed)."""


# ======================================================================= executor evidence

NETWORK_PROBES: Final = ("http_egress", "dns_egress", "metadata_token")
"""Security-suite probes whose ``blocked`` outcome is the evidence behind ``egress_enforced``
and ``dns_enforced``: a declared cap only counts once the probes saw it hold."""

_EXAMPLE_CAPS: Final = {
    "isolation": 3,
    "egress_enforced": True,
    "dns_enforced": True,
    "max_runtime": "1h",
    "classes": ["S", "M", "L"],
    "max_tasks": 10000,
    "regions": ["europe-west1"],
    "checkpoint_storage": True,
}


class SecurityProbeRow(ResponseModel):
    """One probe of a security-suite run (``mapi-security --json``)."""

    probe_id: str = Field(..., max_length=64)
    category: str | None = Field(None, max_length=32)
    outcome: Literal["allowed", "blocked", "error", "skipped"]
    acceptable: list[str] = Field(default_factory=list)
    unexpected: bool = False
    detail: str | None = Field(None, max_length=2000)

    model_config = dto_examples(
        {"probe_id": "dns_egress", "category": "isolation", "outcome": "blocked"}
    )


class SecuritySuiteReport(ResponseModel):
    """A security-suite run against one executor: the JSON ``mapi-security --json`` prints."""

    executor: str = Field(..., pattern=r"^[a-z][a-z0-9-]{0,63}$")
    isolation: Literal["none", "process", "gvisor", "microvm", "dedicated_vm"]
    ok: bool
    unexpected: list[str] = Field(default_factory=list)
    rows: list[SecurityProbeRow] = Field(default_factory=list, max_length=500)
    started_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "executor": "cloudrun-jobs",
            "isolation": "microvm",
            "ok": True,
            "unexpected": [],
            "started_at": "2026-10-15T02:30:00Z",
            "rows": [
                {"probe_id": p, "category": "isolation", "outcome": "blocked"}
                for p in NETWORK_PROBES
            ],
        }
    )

    def blocked(self, probe_id: str) -> bool:
        """Did the probe run and get blocked (``skipped`` is no evidence)?"""
        return any(r.probe_id == probe_id and r.outcome == "blocked" for r in self.rows)

    def passed_for(self, executor: str, caps: ExecutorCaps) -> bool:
        """No unexpected outcome, judged at ``caps.isolation``, and every network probe was
        blocked when the caps claim enforced egress or DNS."""
        if not self.ok or self.unexpected or any(r.unexpected for r in self.rows):
            return False
        if self.executor != executor or self.isolation != caps.isolation.name.lower():
            return False
        if caps.egress_enforced or caps.dns_enforced:
            return all(self.blocked(p) for p in NETWORK_PROBES)
        return True


class ExecutorAttestation(ResponseModel):
    """One recorded suite result, bound to the caps it was measured against."""

    caps: ExecutorCaps
    passed: bool
    recorded_at: AwareDatetime
    expires_at: AwareDatetime
    recorded_by: Id | None = None

    model_config = dto_examples(
        {
            "caps": _EXAMPLE_CAPS,
            "passed": True,
            "recorded_at": EXAMPLE_TS,
            "expires_at": "2026-10-18T09:30:00Z",
        }
    )

    def problem(self, what: str, caps: ExecutorCaps, *, now: datetime) -> str | None:
        if not self.passed:
            return f"the last {what} report did not pass"
        if self.expires_at <= now:
            return f"the {what} report expired at {self.expires_at.isoformat()}"
        if self.caps != caps:
            return f"the {what} report was recorded for other caps than the executor reports"
        return None


class ExecutorEvidence(ResponseModel):
    """The conformance and security-suite evidence recorded for one executor."""

    executor: str = Field(..., pattern=r"^[a-z][a-z0-9-]{0,63}$")
    conformance: ConformanceReport | None = None
    conformance_attestation: ExecutorAttestation | None = None
    security: SecuritySuiteReport | None = None
    security_attestation: ExecutorAttestation | None = None

    model_config = dto_examples(
        {
            "executor": "cloudrun-jobs",
            "conformance": ConformanceReport.model_json_schema()["examples"][0],
            "conformance_attestation": ExecutorAttestation.model_json_schema()["examples"][0],
            "security": SecuritySuiteReport.model_json_schema()["examples"][0],
            "security_attestation": ExecutorAttestation.model_json_schema()["examples"][0],
        }
    )

    def shared_tiers_problem(self, caps: ExecutorCaps, *, now: datetime) -> str | None:
        """Why an executor reporting ``caps`` may not take ``verified``/``public`` tenants
        on this evidence (``None``: it may)."""
        return shared_tiers_problem(self, caps, now=now)


def shared_tiers_problem(
    evidence: ExecutorEvidence | None, caps: ExecutorCaps, *, now: datetime
) -> str | None:
    """Plan §8.4: an executor takes shared tiers (``verified``, ``public``) only after it
    passed the executor conformance suite *and* the security suite for its current caps, and
    neither result has expired. ``None`` means the evidence is in order."""
    if evidence is None:
        return "no executor conformance or security-suite report has been recorded"
    for what, att in (
        ("executor conformance", evidence.conformance_attestation),
        ("security-suite", evidence.security_attestation),
    ):
        if att is None:
            return f"no {what} report has been recorded"
        problem = att.problem(what, caps, now=now)
        if problem is not None:
            return problem
    return None


class ExecutorEvidenceUpdate(RequestModel):
    """Record suite results for an executor (``PUT /v1/admin/executors/{name}/evidence``).

    ``target`` is what the suites ran against (the executor's caps plus the facts its caps do
    not express); the evidence only counts while the executor still reports those caps.
    Each report given replaces the stored one of its kind.
    """

    target: ConformanceTarget
    conformance: ConformanceReport | None = None
    security: SecuritySuiteReport | None = None

    model_config = dto_examples(
        {
            "target": {"executor": "cloudrun-jobs", "caps": _EXAMPLE_CAPS},
            "conformance": ConformanceReport.model_json_schema()["examples"][0],
            "security": SecuritySuiteReport.model_json_schema()["examples"][0],
        }
    )

    @model_validator(mode="after")
    def _one_report(self) -> ExecutorEvidenceUpdate:
        if self.conformance is None and self.security is None:
            raise ValueError("give a conformance report, a security-suite report, or both")
        for report in (self.conformance, self.security):
            if report is not None and report.executor != self.target.executor:
                raise ValueError("every report must be for target.executor")
        return self


class ExecutorHealth(ResponseModel):
    """One executor as the admin console shows it."""

    name: str = Field(..., pattern=r"^[a-z][a-z0-9-]{0,63}$")
    enabled: bool
    status: ExecutorStatus
    caps: ExecutorCaps
    regions: list[str] = Field(default_factory=list)
    allows_public_tenants: bool
    conformance: ConformanceReport | None = None
    conformance_passed: bool = Field(
        False, description="A passing, unexpired conformance report for the current caps"
    )
    security: SecuritySuiteReport | None = None
    security_passed: bool = Field(
        False, description="A passing, unexpired security-suite report for the current caps"
    )
    shared_tiers_enabled: bool = Field(
        False,
        description="May take verified and public tenants: the caps allow it and both "
        "suites passed for these caps (plan §8.4)",
    )
    running_executions: int = Field(0, ge=0)
    queued_runs: int = Field(0, ge=0)
    submits_last_minute: int = Field(0, ge=0)
    submit_rate_limit_per_minute: int | None = Field(None, ge=1)
    throttled_last_hour: int = Field(0, ge=0, description="429 answers from the backend")
    failures_last_hour: int = Field(0, ge=0, description="Attempts failed as infra errors")
    last_success_at: AwareDatetime | None = None
    checked_at: AwareDatetime
    message: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "name": "cloudrun-jobs",
            "enabled": True,
            "status": "ok",
            "caps": {
                "isolation": 3,
                "egress_enforced": True,
                "dns_enforced": True,
                "max_runtime": "1h",
                "classes": ["S", "M", "L"],
                "max_tasks": 10000,
                "regions": ["europe-west1"],
                "checkpoint_storage": True,
            },
            "regions": ["europe-west1"],
            "allows_public_tenants": True,
            "conformance": ConformanceReport.model_json_schema()["examples"][0],
            "conformance_passed": True,
            "security": SecuritySuiteReport.model_json_schema()["examples"][0],
            "security_passed": True,
            "shared_tiers_enabled": True,
            "running_executions": 37,
            "queued_runs": 4,
            "submits_last_minute": 52,
            "submit_rate_limit_per_minute": 180,
            "throttled_last_hour": 0,
            "failures_last_hour": 1,
            "last_success_at": EXAMPLE_TS,
            "checked_at": EXAMPLE_TS,
        }
    )
