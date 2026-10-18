"""The sanity scorecard and the promotion gate (plan §2 Diagnostic, §11 Guided task 5; M3).

A fitted model version carries a :class:`Scorecard`: plain-language checks in four
categories, each ``pass``, ``warn`` or ``fail``:

==============  =========================================================================
category        checks (:data:`SCORECARD_CHECKS`)
==============  =========================================================================
convergence     chains agree (R-hat), enough effective draws, no divergences
holdout         holdout error (MAPE) and interval coverage on the evaluation split; for
                choice models, the holdout hit rate against always guessing the most
                popular product
plausibility    negative or absurd ROI, a channel taking most of the KPI, the baseline share,
                parameters the data did not inform (prior-posterior overlap)
data            missing values, years of history, spend that never varies
==============  =========================================================================

:func:`evaluate_check` turns a metric value into a :class:`ScorecardItem` with the check's
message and suggested action; :func:`rollup` makes the scorecard (``overall`` is the worst
item). Templates pick checks and may tighten thresholds (:class:`CheckThresholds`).

Promoting a model version to an endpoint passes the **promotion gate**
(:func:`promotion_gate`): ``pass`` → allowed, ``warn`` → needs an administrator override
with a reason (audited), ``fail`` → blocked. :func:`authorize_promotion` applies an override
request to a gate decision.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from pydantic import Field, model_validator

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import MetricName, StrictModel
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "SCORECARD_CHECKS",
    "SCORECARD_DOCS_PAGE",
    "SCORECARD_OUTPUT",
    "CheckDirection",
    "CheckThresholds",
    "GateOutcome",
    "GateReason",
    "PromotionGateDecision",
    "Scorecard",
    "ScorecardCategory",
    "ScorecardCheck",
    "ScorecardItem",
    "ScorecardStatus",
    "authorize_promotion",
    "evaluate_check",
    "promotion_gate",
    "rollup",
    "worst_status",
]

ScorecardStatus = Literal["pass", "warn", "fail"]
ScorecardCategory = Literal["convergence", "holdout", "plausibility", "data"]
SCORECARD_DOCS_PAGE: Final = "concepts/guided-mode.md"
SCORECARD_OUTPUT: Final = "scorecard.json"
"""The fit sidecar holding the runner-computed :class:`Scorecard` (M3); the finalizer
stores it on the model version."""
"""The docs page whose anchors (``ScorecardItem.docs_anchor``) explain each check."""

_RANK: Final[dict[str, int]] = {"pass": 0, "warn": 1, "fail": 2}


def worst_status(statuses: Iterable[ScorecardStatus]) -> ScorecardStatus:
    """The worst of ``statuses`` (``pass`` for none)."""
    worst: ScorecardStatus = "pass"
    for s in statuses:
        if _RANK[s] > _RANK[worst]:
            worst = s
    return worst


class ScorecardItem(ResponseModel):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    status: ScorecardStatus
    title: str = Field(..., max_length=200)
    message_plain: str = Field(..., max_length=2000)
    message_expert: str | None = Field(None, max_length=2000)
    metric: MetricName | None = None
    value: float | None = None
    threshold: float | None = None
    suggested_action: str | None = Field(None, max_length=2000)
    check: str | None = Field(
        None,
        pattern=r"^[a-z]+\.[a-z0-9_]{1,63}$",
        description="M3: the stable check id (SCORECARD_CHECKS), e.g. holdout.mape",
    )
    category: ScorecardCategory | None = Field(None, description="M3: the check's category")
    docs_anchor: str | None = Field(
        None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,99}$",
        description="M3: anchor on the scorecard docs page that explains the check",
    )

    model_config = dto_examples(
        {
            "code": "RHAT_OK",
            "status": "pass",
            "title": "Chains agree",
            "message_plain": "All chains got the same answer.",
            "metric": "r_hat_max",
            "value": 1.004,
            "threshold": 1.01,
        },
        {
            "code": "HOLDOUT_MAPE",
            "status": "warn",
            "title": "Holdout error",
            "message_plain": "The forecast error on the holdout periods was 14%.",
            "metric": "mape",
            "value": 0.14,
            "threshold": 0.1,
            "suggested_action": "Look for gaps or one-time events in the last weeks of data.",
            "check": "holdout.mape",
            "category": "holdout",
            "docs_anchor": "check-holdout-mape",
        },
    )


class Scorecard(ResponseModel):
    """Sanity scorecard: convergence, holdout error and plausibility checks rolled up."""

    overall: ScorecardStatus
    items: list[ScorecardItem] = Field(default_factory=list)
    skipped: dict[str, str] = Field(
        default_factory=dict,
        max_length=64,
        description="M3: checks that could not run, with the reason (check id -> text)",
    )

    model_config = dto_examples(
        {
            "overall": "warn",
            "items": [
                {
                    "code": "RHAT_OK",
                    "status": "pass",
                    "title": "Chains agree",
                    "message_plain": "All chains got the same answer.",
                    "metric": "r_hat_max",
                    "value": 1.004,
                    "threshold": 1.01,
                },
                {
                    "code": "HOLDOUT_MAPE",
                    "status": "warn",
                    "title": "Holdout error",
                    "message_plain": "The forecast error on the last 8 weeks was 14%.",
                    "metric": "mape",
                    "value": 0.14,
                    "threshold": 0.1,
                    "suggested_action": "Add holidays, or look at the data for the last weeks.",
                },
            ],
        }
    )


# ====================================================================================== checks


class CheckDirection(StrEnum):
    MAX = "max"
    """The value must stay at or below the thresholds (errors, R-hat)."""
    MIN = "min"
    """The value must stay at or above the thresholds (ESS, history length)."""
    BAND = "band"
    """``|value - target|`` must stay at or below the thresholds (coverage)."""


class CheckThresholds(StrictModel):
    """Warn and fail thresholds of a check (in the check's direction)."""

    warn: float
    fail: float

    @model_validator(mode="after")
    def _order(self) -> CheckThresholds:
        if not (math.isfinite(self.warn) and math.isfinite(self.fail)):
            raise ValueError("thresholds must be finite")
        return self


ValueFormat = Literal["pct", "num", "int", "years"]


@dataclass(frozen=True, slots=True)
class ScorecardCheck:
    """One check of the catalog. Messages are templates over ``{value}`` (formatted per
    ``value_format``) and ``{threshold}``."""

    id: str
    category: ScorecardCategory
    code: str
    title: str
    metric: str
    direction: CheckDirection
    warn: float
    fail: float
    pass_plain: str
    warn_plain: str
    fail_plain: str
    suggested_action: str
    value_format: ValueFormat = "num"
    target: float | None = None

    def __post_init__(self) -> None:
        if (self.direction is CheckDirection.BAND) != (self.target is not None):
            raise ValueError("band checks (and only they) have a target")
        lenient_fail = self.fail >= self.warn
        if (self.direction is CheckDirection.MIN) == lenient_fail and self.fail != self.warn:
            raise ValueError(f"{self.id}: fail must be at least as strict as warn")

    @property
    def docs_anchor(self) -> str:
        return "check-" + self.id.replace(".", "-").replace("_", "-")

    def thresholds(self, override: CheckThresholds | None = None) -> tuple[float, float]:
        return (override.warn, override.fail) if override is not None else (self.warn, self.fail)


def _c(
    id: str,
    category: ScorecardCategory,
    code: str,
    title: str,
    metric: str,
    direction: CheckDirection,
    warn: float,
    fail: float,
    texts: tuple[str, str, str, str],
    value_format: ValueFormat = "num",
    target: float | None = None,
) -> ScorecardCheck:
    return ScorecardCheck(
        id, category, code, title, metric, direction, warn, fail, *texts, value_format, target
    )


_MAX, _MIN, _BAND = CheckDirection.MAX, CheckDirection.MIN, CheckDirection.BAND

SCORECARD_CHECKS: Final[dict[str, ScorecardCheck]] = {
    c.id: c
    for c in (
        _c("convergence.rhat", "convergence", "RHAT", "Chains agree", "r_hat_max", _MAX,
           1.01, 1.05, (
               "All chains got the same answer (R-hat {value}).",
               "The chains agree, but not fully (R-hat {value}).",
               "The chains do not agree (R-hat {value}). The results are not reliable.",
               "Increase the tuning steps and the draws, or make the model simpler.",
           )),
        _c("convergence.ess", "convergence", "ESS", "Enough independent draws", "ess_bulk_min",
           _MIN, 400, 100, (
               "There are enough independent draws ({value}) for stable results.",
               "There are only {value} independent draws. The results can change a little "
               "from one run to the next.",
               "There are only {value} independent draws. The results are not stable.",
               "Increase the number of draws.",
           ), "int"),
        _c("convergence.divergences", "convergence", "DIVERGENCES", "The sampler had no problems",
           "divergence_rate", _MAX, 0.0, 0.01, (
               "The sampler had no problems with the model.",
               "The sampler had problems in a small part of the draws ({value}).",
               "The sampler had problems in many draws ({value}). The results can be biased.",
               "Increase target_accept, or use priors that give more information.",
           ), "pct"),
        _c("holdout.mape", "holdout", "HOLDOUT_MAPE", "Holdout error", "mape", _MAX,
           0.10, 0.25, (
               "The forecast error on the holdout periods was {value}.",
               "The forecast error on the holdout periods was {value}.",
               "The forecast error on the holdout periods was {value}. This error is too large.",
               "Look for gaps or one-time events in the last periods of data. Add holidays "
               "or controls that explain them.",
           ), "pct"),
        _c("holdout.coverage_90", "holdout", "HOLDOUT_COVERAGE", "The uncertainty is correct",
           "coverage_90", _BAND, 0.10, 0.25, (
               "{value} of the holdout values were in the 90% range. This is correct.",
               "{value} of the holdout values were in the 90% range. The target is 90%.",
               "Only {value} of the holdout values were in the 90% range. The uncertainty "
               "is not correct.",
               "Look for trends or shifts that the model does not show.",
           ), "pct", target=0.90),
        _c("holdout.hit_rate", "holdout", "HOLDOUT_HIT_RATE", "Better than a simple guess",
           "accuracy_lift", _MIN, 0.02, -0.10, (
               "On the holdout trips, the top choice of the model was correct {value} more "
               "often than a guess of the most popular product.",
               "On the holdout trips, the top choice of the model was correct about as often "
               "as a guess of the most popular product ({value} difference).",
               "On the holdout trips, the top choice of the model was correct less often than "
               "a guess of the most popular product ({value} difference). Its predicted "
               "choices are not reliable.",
               "Add the drivers of the choice for each product (price, promotions, displays). "
               "Or get more trips for each customer.",
           ), "pct"),
        _c("plausibility.negative_roi", "plausibility", "NEGATIVE_ROI", "No channel loses money",
           "negative_roi_share", _MAX, 0.10, 0.50, (
               "No channel has a high probability of a negative return.",
               "A channel has a {value} probability of a negative return.",
               "A channel probably loses money ({value} probability).",
               "Do a check of the spend data of the channel. Or give an ROI range in the "
               "business priors.",
           ), "pct"),
        _c("plausibility.roi_absurd", "plausibility", "ROI_ABSURD", "Returns are not too high",
           "roi_max", _MAX, 20.0, 100.0, (
               "The highest return of a channel ({value} for each unit of spend) is in the "
               "usual range.",
               "A channel returns {value} for each unit of spend. This is higher than usual.",
               "A channel returns {value} for each unit of spend. This is too high to be "
               "correct.",
               "Do a check of the spend units and the gaps. Or give an ROI range in the "
               "business priors.",
           )),
        _c("plausibility.share_outlier", "plausibility", "SHARE_OUTLIER",
           "No channel is too large", "max_channel_share", _MAX, 0.60, 0.90, (
               "No single channel explains most of the KPI.",
               "One channel explains {value} of the KPI. This is more than usual.",
               "One channel explains {value} of the KPI. This is too high to be correct.",
               "Look for a missing control (price, promotions, seasonality).",
           ), "pct"),
        _c("plausibility.baseline_share", "plausibility", "BASELINE_SHARE",
           "The baseline is in the usual range", "baseline_share", _BAND, 0.40, 0.48, (
               "The baseline (the KPI without media) is {value}. This share is in the usual "
               "range.",
               "The baseline (the KPI without media) is {value}. This share is unusual.",
               "The baseline (the KPI without media) is {value}. This share is too far from "
               "the usual range.",
               "Give a baseline share in the business priors. Or do a check of the controls.",
           ), "pct", target=0.50),
        _c("plausibility.prior_overlap", "plausibility", "PRIOR_OVERLAP",
           "The data changed the model", "prior_overlap_max", _MAX, 0.90, 0.99, (
               "The data moved each parameter away from its prior.",
               "A parameter moved only a small distance from its prior ({value} overlap). "
               "The data gives little information about it.",
               "A parameter did not move from its prior ({value} overlap). Its value comes "
               "from the assumptions, not from the data.",
               "Add data where the driver changes, or add a lift test. Or give a business "
               "prior that you trust for the driver.",
           ), "pct"),
        _c("data.missing", "data", "DATA_MISSING", "Few missing values", "missing_fraction",
           _MAX, 0.05, 0.30, (
               "Only {value} of the values are missing.",
               "{value} of the values are missing.",
               "{value} of the values are missing. This is too many for a reliable model.",
               "Fill the gaps in the source data, or remove the series that have gaps.",
           ), "pct"),
        _c("data.history", "data", "DATA_HISTORY", "Enough history", "history_years", _MIN,
           2.0, 1.0, (
               "There are {value} years of history. This is enough to find the seasonality.",
               "There are only {value} years of history. The seasonal effects are not "
               "certain.",
               "There are only {value} years of history. This is not sufficient to separate "
               "media from seasonality.",
               "If you have more history, add it.",
           ), "years"),
        _c("data.spend_variation", "data", "SPEND_VARIATION", "Spend changes enough",
           "spend_cv_min", _MIN, 0.10, 0.02, (
               "The spend of each channel changes sufficiently to measure its effect.",
               "The spend of a channel changes very little ({value}). Its effect is not "
               "certain.",
               "The spend of a channel almost never changes ({value}). You cannot measure "
               "its effect from this data.",
               "Add a lift test for the channel, or give its ROI in the business priors.",
           ), "pct"),
    )
}  # fmt: skip
"""The scorecard check catalog (stable ids; thresholds are the platform defaults)."""


def _fmt(value: float, fmt: ValueFormat) -> str:
    if fmt == "pct":
        return f"{value:.0%}" if abs(value) >= 0.01 or value == 0 else f"{value:.1%}"
    if fmt == "int":
        return f"{round(value):,}"
    if fmt == "years":
        return f"{value:.1f}"
    return f"{value:.3g}"


def _status(check: ScorecardCheck, value: float, warn: float, fail: float) -> ScorecardStatus:
    if check.direction is CheckDirection.MIN:
        return "fail" if value < fail else "warn" if value < warn else "pass"
    v = abs(value - check.target) if check.target is not None else value
    return "fail" if v > fail else "warn" if v > warn else "pass"


def evaluate_check(
    check: str | ScorecardCheck,
    value: float,
    *,
    thresholds: CheckThresholds | None = None,
    message_expert: str | None = None,
) -> ScorecardItem:
    """The :class:`ScorecardItem` of one check at ``value`` (``KeyError`` for unknown ids,
    ``ValueError`` for a non-finite value)."""
    c = SCORECARD_CHECKS[check] if isinstance(check, str) else check
    if not math.isfinite(value):
        raise ValueError(f"{c.id}: the metric value must be finite")
    warn, fail = c.thresholds(thresholds)
    status = _status(c, value, warn, fail)
    template = {"pass": c.pass_plain, "warn": c.warn_plain, "fail": c.fail_plain}[status]
    return ScorecardItem(
        code=c.code,
        status=status,
        title=c.title,
        message_plain=template.format(value=_fmt(value, c.value_format)),
        message_expert=message_expert,
        metric=c.metric,
        value=value,
        threshold=fail if status == "fail" else warn,
        suggested_action=None if status == "pass" else c.suggested_action,
        check=c.id,
        category=c.category,
        docs_anchor=c.docs_anchor,
    )


def rollup(items: Sequence[ScorecardItem], skipped: Mapping[str, str] | None = None) -> Scorecard:
    """A scorecard whose ``overall`` is the worst item; with no item at all it is ``warn``
    (nothing was checked)."""
    overall: ScorecardStatus = worst_status(i.status for i in items) if items else "warn"
    return Scorecard(overall=overall, items=list(items), skipped=dict(skipped or {}))


# ============================================================================ promotion gate


class GateOutcome(StrEnum):
    ALLOWED = "allowed"
    NEEDS_OVERRIDE = "needs_override"
    BLOCKED = "blocked"


_GATE_RANK: Final = {GateOutcome.ALLOWED: 0, GateOutcome.NEEDS_OVERRIDE: 1, GateOutcome.BLOCKED: 2}


class GateReason(ResponseModel):
    """Why the gate is not plainly ``allowed``: a stable code, plain text, and the check."""

    code: Literal[
        "SCORECARD_WARN",
        "SCORECARD_FAIL",
        "NO_SCORECARD",
        "TRAIN_ONLY",
        "PARTIAL_MODEL",
        "SCHEMA_CHANGE",
        "NOT_EVALUATED",
        "NOT_VALIDATED",
    ]
    outcome: GateOutcome
    message_plain: str = Field(..., max_length=500)
    check: str | None = None


class PromotionGateDecision(ResponseModel):
    """The gate's decision for promoting one model version to an endpoint."""

    outcome: GateOutcome
    scorecard_overall: ScorecardStatus | None = None
    reasons: list[GateReason] = Field(default_factory=list)
    failing_checks: list[str] = Field(default_factory=list)

    model_config = dto_examples(
        {
            "outcome": "needs_override",
            "scorecard_overall": "warn",
            "reasons": [
                {
                    "code": "SCORECARD_WARN",
                    "outcome": "needs_override",
                    "message_plain": "Holdout error: the forecast error was 14%.",
                    "check": "holdout.mape",
                }
            ],
            "failing_checks": ["holdout.mape"],
        }
    )


def promotion_gate(
    scorecard: Scorecard | None,
    *,
    fit_mode: str = "evaluate_then_refit",
    allow_train_only: bool = False,
    partial: bool = False,
    schema_breaks: Sequence[str] = (),
    schema_change: Literal["block", "warn", "allow"] = "block",
    required_checks: Sequence[str] = (),
    require_validated: bool = False,
    validation_status: str | None = None,
) -> PromotionGateDecision:
    """Decide whether a model version may be promoted (pure).

    M3.5: when a governance policy requires validation (``require_validated``, see
    :func:`mapi_spec.governance.requires_validation`), a version whose ``validation_status``
    is not ``validated`` is blocked (``NOT_VALIDATED``): no override promotes it.

    * a ``partial`` version (interrupted fit) is blocked;
    * a ``train_only`` fit is blocked unless the endpoint's policy allows it (Q13:
      ``evaluate_then_refit`` is the default for endpoints, so context and terminal state
      come from all the data);
    * breaking input-schema changes (``schema_breaks``) follow ``schema_change``;
    * no scorecard needs an override; a ``fail`` scorecard blocks, a ``warn`` one needs an
      override, a ``pass`` one is allowed;
    * an ``evaluate_then_refit`` version whose scorecard measured no holdout check (no
      ``data.split``, or no ``evaluate.metrics``) was never evaluated and needs an override
      (``NOT_EVALUATED``), as does every check of ``required_checks`` (a guided template's
      ``scorecard_checks``) that the scorecard skipped or lacks.
    """
    reasons: list[GateReason] = []

    def add(code: str, outcome: GateOutcome, text: str, check: str | None = None) -> None:
        reasons.append(
            GateReason.model_validate(
                {"code": code, "outcome": outcome, "message_plain": text[:500], "check": check}
            )
        )

    if partial:
        add("PARTIAL_MODEL", GateOutcome.BLOCKED, "The fit stopped before it was complete.")
    if require_validated and validation_status != "validated":
        state = (validation_status or "not submitted").replace("_", " ")
        add(
            "NOT_VALIDATED",
            GateOutcome.BLOCKED,
            "The governance policy accepts only a validated model. This version is "
            f"{state}. Submit it for validation first.",
        )
    if fit_mode == "train_only" and not allow_train_only:
        add(
            "TRAIN_ONLY",
            GateOutcome.BLOCKED,
            "The fit did not use the holdout data. For an endpoint, fit the model again on all "
            "the data.",
        )
    if schema_breaks and schema_change != "allow":
        add(
            "SCHEMA_CHANGE",
            GateOutcome.BLOCKED if schema_change == "block" else GateOutcome.NEEDS_OVERRIDE,
            "The model needs inputs that are different from the inputs that the callers of "
            "the endpoint send: " + "; ".join(schema_breaks),
        )
    failing: list[str] = []
    if scorecard is None:
        add("NO_SCORECARD", GateOutcome.NEEDS_OVERRIDE, "The model has no sanity scorecard.")
    else:
        for item in scorecard.items:
            if item.status == "pass":
                continue
            failing.append(item.check or item.code)
            outcome = GateOutcome.BLOCKED if item.status == "fail" else GateOutcome.NEEDS_OVERRIDE
            add(
                "SCORECARD_FAIL" if item.status == "fail" else "SCORECARD_WARN",
                outcome,
                f"{item.title}: {item.message_plain}",
                item.check,
            )
        scored = {i.check for i in scorecard.items if i.check}
        if fit_mode == "evaluate_then_refit" and not _has_holdout(scorecard):
            skipped = scorecard.skipped
            check = next(
                (c for c in SCORECARD_CHECKS if c.startswith("holdout.") and c in skipped), None
            )
            why = scorecard.skipped.get(check or "", "the scorecard has no holdout check")
            failing.append(check or "holdout")
            add(
                "NOT_EVALUATED",
                GateOutcome.NEEDS_OVERRIDE,
                "The model has no score on holdout data, so its forecast error is not "
                f"known ({why}). Add a holdout split (data.split) and holdout metrics "
                "(evaluate.metrics). Then fit the model again.",
                check,
            )
        for check in dict.fromkeys(required_checks):
            if check in scored or check in failing:
                continue
            failing.append(check)
            title = SCORECARD_CHECKS[check].title if check in SCORECARD_CHECKS else check
            why = scorecard.skipped.get(check, "it is not on the scorecard")
            add(
                "NOT_EVALUATED",
                GateOutcome.NEEDS_OVERRIDE,
                f"{title}: the check did not run ({why}).",
                check,
            )
        if scorecard.overall != "pass" and not any(
            r.code in ("SCORECARD_WARN", "SCORECARD_FAIL") for r in reasons
        ):
            outcome = (
                GateOutcome.BLOCKED if scorecard.overall == "fail" else GateOutcome.NEEDS_OVERRIDE
            )
            add(
                "SCORECARD_FAIL" if scorecard.overall == "fail" else "SCORECARD_WARN",
                outcome,
                "The scorecard did not pass.",
            )
    outcome = GateOutcome.ALLOWED
    for r in reasons:
        if _GATE_RANK[r.outcome] > _GATE_RANK[outcome]:
            outcome = r.outcome
    return PromotionGateDecision(
        outcome=outcome,
        scorecard_overall=scorecard.overall if scorecard is not None else None,
        reasons=reasons,
        failing_checks=failing,
    )


def _has_holdout(scorecard: Scorecard) -> bool:
    """Whether the scorecard measured a holdout check (v0.3 items carry only a code)."""
    return any(
        i.category == "holdout"
        or (i.check or "").startswith("holdout.")
        or i.code.startswith("HOLDOUT")
        for i in scorecard.items
    )


def authorize_promotion(
    decision: PromotionGateDecision,
    *,
    override: bool = False,
    reason: str | None = None,
    can_override: bool = False,
) -> bool:
    """Apply an override request to a gate decision; return whether it was *overridden*.

    Raises ``PROMOTION_BLOCKED`` (blocked: no override possible), ``OVERRIDE_REQUIRED``
    (needs an override that was not requested), ``FORBIDDEN`` (the caller may not override:
    ``manage`` on the endpoint, a signed-in admin) or ``SCHEMA_INVALID`` (an override
    without a reason). The caller audits an override as ``endpoint.promote_override``.
    """
    if decision.outcome is GateOutcome.ALLOWED:
        return False
    if decision.outcome is GateOutcome.BLOCKED:
        if any(r.code == "NOT_VALIDATED" for r in decision.reasons):
            raise MapiError(
                next(r.message_plain for r in decision.reasons if r.code == "NOT_VALIDATED"),
                code=ErrorCode.NOT_VALIDATED,
            )
        raise MapiError(
            "the promotion gate blocks this model version: "
            + "; ".join(r.message_plain for r in decision.reasons if r.outcome == "blocked"),
            code=ErrorCode.PROMOTION_BLOCKED,
        )
    if not override:
        raise MapiError(
            "this model version needs an administrator override (send override=true with a reason)",
            code=ErrorCode.OVERRIDE_REQUIRED,
        )
    if not can_override:
        raise MapiError(
            "overriding the promotion gate needs a signed-in project admin",
            code=ErrorCode.FORBIDDEN,
        )
    if reason is None or not reason.strip():
        raise MapiError(
            "an override needs a reason", code=ErrorCode.SCHEMA_INVALID, pointer="/reason"
        )
    return True
