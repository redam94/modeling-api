"""Governance nodes (``governance.policy``, ``governance.check``) and the evidence pack.

The workflow side of governance is a contract: this module runs *check runners* and rolls
their :class:`~mapi_spec.governance.CheckResult` s up against the policy; the governance
component implements the checks (null tests on shuffled targets and noise drivers, placebo
time shifts, recovery on simulated data, ...) and the policy store.

* **Check runners** are callables ``(CheckContext) -> CheckResult`` registered by check id,
  from the ``mapi.governance_checks`` entry point group (each entry point returns a mapping
  ``check id -> runner``) or given by the driver (``services["governance"].runners``). A
  required check without a runner gives an ``error`` result (which blocks when the check is
  blocking): a model is never validated by a check that did not run.
* **Policies** come from the driver's :class:`PolicyResolver`
  (``services["governance"].resolver``): the control plane resolves the policies in force
  for the model's family before it starts the node. Without a resolver no policy applies,
  and the node says so.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    CheckResult,
    CheckSeverity,
    CheckStatus,
    GovernancePolicy,
    PolicyCheck,
    effective_checks,
    rollup_results,
)
from mapi_spec.scorecard import CheckThresholds
from mapi_workflows.orchestrator import BoundValue, evidence_pack
from mapi_workflows.payloads import EvidencePayload, clean_number
from mapi_workflows.runtime.values import DatasetValue, ModelValue, NodeContext, NodeOutcome
from pydantic import JsonValue

__all__ = [
    "CHECK_ENTRY_POINT_GROUP",
    "GOVERNANCE_NODES",
    "CheckContext",
    "CheckRunner",
    "GovernanceServices",
    "PolicyResolver",
    "load_check_runners",
    "run_checks",
]

CHECK_ENTRY_POINT_GROUP: Final = "mapi.governance_checks"


@dataclass(slots=True)
class CheckContext:
    """What a check runner gets: the model, its data and the check's configuration."""

    check: PolicyCheck
    model: ModelValue
    data: DatasetValue | None
    registry: Any = None
    seed: int = 0
    cancel: Any = None
    log: Callable[[str], None] | None = None

    @property
    def thresholds(self) -> CheckThresholds | None:
        return self.check.thresholds

    @property
    def params(self) -> dict[str, JsonValue]:
        return dict(self.check.params)


class CheckRunner(Protocol):
    def __call__(self, ctx: CheckContext) -> CheckResult: ...


class PolicyResolver(Protocol):
    def __call__(self, model: ModelValue, policy_id: str | None) -> Sequence[GovernancePolicy]: ...


@dataclass(slots=True)
class GovernanceServices:
    """Driver-provided governance hooks (``NodeContext.services["governance"]``)."""

    resolver: PolicyResolver | None = None
    runners: dict[str, CheckRunner] = field(default_factory=dict)
    submit: Callable[[ModelValue, EvidencePayload], None] | None = None
    installed: bool = True
    """Also use the installed check runners (``mapi.governance_checks``); ``runners`` win."""


def load_check_runners() -> dict[str, CheckRunner]:
    """Check runners exported by installed distributions (``mapi.governance_checks``)."""
    out: dict[str, CheckRunner] = {}
    for ep in importlib.metadata.entry_points(group=CHECK_ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            mapping = obj() if callable(obj) and not isinstance(obj, Mapping) else obj
        except Exception:
            continue
        if isinstance(mapping, Mapping):
            for k, v in mapping.items():
                if isinstance(k, str) and callable(v) and k not in out:
                    out[k] = v
    return out


def _services(ctx: NodeContext) -> GovernanceServices:
    svc = ctx.services.get("governance")
    if isinstance(svc, GovernanceServices):
        return svc
    return GovernanceServices()


def _now() -> datetime:
    return datetime.now(UTC)


def run_checks(
    checks: Sequence[PolicyCheck],
    model: ModelValue,
    data: DatasetValue | None,
    runners: Mapping[str, CheckRunner],
    *,
    registry: Any = None,
    seed: int = 0,
    cancel: Any = None,
    log: Callable[[str], None] | None = None,
) -> list[CheckResult]:
    """Run each check with its runner; a missing runner or a crash is an ``error`` result."""
    out: list[CheckResult] = []
    for c in checks:
        if cancel is not None:
            cancel.check()
        definition = GOVERNANCE_CHECKS.get(c.check)
        runner = runners.get(c.check)
        if runner is None:
            out.append(
                CheckResult(
                    check=c.check,
                    status=CheckStatus.ERROR,
                    severity=c.severity,
                    metric=definition.metric if definition else None,
                    message_plain=f"The check {c.check!r} is not installed on this platform, so it "
                    "could not run.",
                    params=dict(c.params),
                    computed_at=_now(),
                )
            )
            continue
        try:
            res = runner(
                CheckContext(
                    check=c,
                    model=model,
                    data=data,
                    registry=registry,
                    seed=seed,
                    cancel=cancel,
                    log=log,
                )
            )
            if not isinstance(res, CheckResult):
                raise TypeError("a check runner must return a CheckResult")
            out.append(res.model_copy(update={"severity": c.severity}))
        except MapiError as exc:
            out.append(
                CheckResult(
                    check=c.check,
                    status=CheckStatus.ERROR,
                    severity=c.severity,
                    metric=definition.metric if definition else None,
                    message_plain=f"The check could not be computed: {exc.detail[:500]}",
                    params=dict(c.params),
                    computed_at=_now(),
                )
            )
        except Exception as exc:
            out.append(
                CheckResult(
                    check=c.check,
                    status=CheckStatus.ERROR,
                    severity=c.severity,
                    metric=definition.metric if definition else None,
                    message_plain="The check could not be computed.",
                    message_expert=f"{type(exc).__name__}: {str(exc)[:500]}",
                    params=dict(c.params),
                    computed_at=_now(),
                )
            )
    return out


def _evidence(
    model: ModelValue,
    results: Sequence[CheckResult],
    required: Sequence[PolicyCheck],
    policies: Sequence[str],
    *,
    none_apply: bool = False,
) -> EvidencePayload:
    passed, failing = rollup_results(results, required)
    if none_apply:
        msg = "No governance policy covers this model, so no check was required."
    elif passed:
        warn = [r.check for r in results if r.status in (CheckStatus.WARN, CheckStatus.FAIL)]
        msg = f"Every blocking check passed ({len(results)} check(s) ran)" + (
            f"; warnings on {', '.join(warn)}." if warn else "."
        )
    else:
        msg = f"{len(failing)} blocking check(s) did not pass: {', '.join(failing)}."
    return EvidencePayload(
        model=model.ref,
        status="checks_passed" if passed else "checks_failed",
        passed=passed,
        failed_checks=failing,
        results=[r.model_dump(mode="json") for r in results],
        policies=list(policies),
        message_plain=msg,
    )


def _policy(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    data = ctx.dataset("data", required=False)
    svc = _services(ctx)
    policy_id = ctx.params.get("policy")
    pid = policy_id if isinstance(policy_id, str) and policy_id else None
    if svc.resolver is None:
        if pid is not None:
            raise MapiError(
                f"policy {pid!r} cannot be resolved: no governance policy store is available",
                code=ErrorCode.POLICY_INVALID,
            )
        policies: Sequence[GovernancePolicy] = []
    else:
        policies = svc.resolver(mv, pid)
        if pid is not None and not policies:
            raise MapiError(
                f"policy {pid!r} was not found or does not apply to this model",
                code=ErrorCode.POLICY_INVALID,
            )
    checks = effective_checks(policies)
    runners = {**(load_check_runners() if svc.installed else {}), **svc.runners}
    results = run_checks(
        checks,
        mv,
        data,
        runners,
        registry=ctx.registry,
        seed=ctx.seed,
        cancel=ctx.cancel,
        log=ctx.log,
    )
    ev = _evidence(
        mv, results, checks, [f"{p.id}@{p.version}" for p in policies], none_apply=not policies
    )
    warns: list[str] = []
    if bool(ctx.params.get("submit")):
        if ev.passed and svc.submit is not None:
            svc.submit(mv, ev)
        elif ev.passed:
            warns.append("submit was requested but no validation service is available")
    return NodeOutcome(
        result={
            "passed": ev.passed,
            "failed_checks": list(ev.failed_checks),
            "status": ev.status,
            "n_checks": len(results),
            "message_plain": ev.message_plain,
        },
        outputs={"evidence": ev.model_dump(mode="json")},
        warnings_plain=warns,
    )


def _check(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    data = ctx.dataset("data", required=False)
    check_id = str(ctx.params.get("check") or "")
    if check_id not in GOVERNANCE_CHECKS:
        raise MapiError(f"unknown governance check {check_id!r}", code=ErrorCode.POLICY_INVALID)
    warn, fail = clean_number(ctx.params.get("warn")), clean_number(ctx.params.get("fail"))
    params = ctx.params.get("params")
    try:
        pc = PolicyCheck(
            check=check_id,
            severity=CheckSeverity.BLOCKING,
            thresholds=CheckThresholds(warn=warn, fail=fail)
            if warn is not None and fail is not None
            else None,
            params=dict(params) if isinstance(params, dict) else {},
        )
    except ValueError as exc:
        raise MapiError(
            f"invalid check configuration: {str(exc)[:300]}", code=ErrorCode.POLICY_INVALID
        ) from None
    svc = _services(ctx)
    runners = {**(load_check_runners() if svc.installed else {}), **svc.runners}
    (res,) = run_checks(
        [pc],
        mv,
        data,
        runners,
        registry=ctx.registry,
        seed=ctx.seed,
        cancel=ctx.cancel,
        log=ctx.log,
    )
    ev = _evidence(mv, [res], [pc], [])
    return NodeOutcome(
        result={"status": res.status.value, "value": res.value, "message_plain": res.message_plain},
        outputs={"evidence": ev.model_dump(mode="json")},
    )


def _pack(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    ev = [BoundValue(ref=r) for r in ctx.input_refs.get("evidence", [])]
    if not ev:
        from mapi_spec.workflow import NodeOutputRef
        from mapi_spec.workflow_nodes import PortType

        ev = [
            BoundValue(ref=NodeOutputRef(type=PortType.EVIDENCE, inline=v))
            for v in ctx.json("evidence")
        ]
    manifest = mv.bundle.manifest
    lineage: dict[str, JsonValue] = {
        "bundle_id": manifest.bundle_id,
        "model": mv.model,
        "spec_hash": getattr(manifest, "spec_hash", None),
        "data_hash": getattr(manifest, "data_hash", None),
    }
    result, outputs = evidence_pack(
        ctx.params, mv.ref, ev, lineage={k: v for k, v in lineage.items() if v is not None}
    )
    report = outputs["report"].inline
    return NodeOutcome(
        result=result, outputs={"report": report if isinstance(report, dict) else {}}
    )


GOVERNANCE_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "governance.policy": _policy,
    "governance.check": _check,
    "report.evidence_pack": _pack,
}
