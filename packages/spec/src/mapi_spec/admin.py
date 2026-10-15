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
from mapi_spec.conformance_executor import ConformanceReport
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
    "ExecutorHealth",
    "ExecutorStatus",
    "ExecutorUpdate",
    "KillSwitchUpdate",
    "KillSwitches",
    "kill_switch_decision",
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
    cancel_untrusted_runs: bool = Field(
        False,
        description=(
            "Also cancel every unfinished run of tenant-uploaded code, queued or already "
            "placed on an executor. Requires untrusted_execution_enabled=false in the same "
            "update. Without it, pausing only holds new work and in-flight runs continue"
        ),
    )
    reason: str = Field(..., min_length=3, max_length=500)

    model_config = dto_examples(
        {"untrusted_execution_enabled": False, "reason": "incident 2026-10-15"},
        {
            "untrusted_execution_enabled": False,
            "cancel_untrusted_runs": True,
            "reason": "incident 2026-10-15: confirmed sandbox escape",
        },
        {"global_spend_cap_micros": 500_000_000, "reason": "daily cap for the beta"},
    )

    @model_validator(mode="after")
    def _cap(self) -> KillSwitchUpdate:
        if self.clear_spend_cap and self.global_spend_cap_micros is not None:
            raise ValueError("give either global_spend_cap_micros or clear_spend_cap")
        if self.cancel_untrusted_runs and self.untrusted_execution_enabled is not False:
            raise ValueError("cancel_untrusted_runs requires untrusted_execution_enabled=false")
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
    """Disabled by an operator (``PUT /v1/admin/executors/{name}``): nothing is leased."""


class ExecutorUpdate(RequestModel):
    """``PUT /v1/admin/executors/{name}``: enable or disable an executor (audited).

    Orchestrators lease no new run onto a disabled executor. Runs already placed on it keep
    running: cancel them with ``cancel_untrusted_runs`` on the kill switches, or one by one.
    """

    enabled: bool
    reason: str = Field(..., min_length=3, max_length=500)

    model_config = dto_examples(
        {"enabled": False, "reason": "incident 2026-10-15: suspected sandbox escape"},
    )


class ExecutorHealth(ResponseModel):
    """One executor as the admin console shows it."""

    name: str = Field(..., pattern=r"^[a-z][a-z0-9-]{0,63}$")
    enabled: bool
    status: ExecutorStatus
    caps: ExecutorCaps
    regions: list[str] = Field(default_factory=list)
    allows_public_tenants: bool
    conformance: ConformanceReport | None = None
    conformance_passed: bool = False
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
