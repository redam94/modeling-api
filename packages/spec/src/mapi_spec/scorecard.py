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
data            missing values, history length, spend that never varies
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
            "message_plain": "All chains converged to the same answer.",
            "metric": "r_hat_max",
            "value": 1.004,
            "threshold": 1.01,
        },
        {
            "code": "HOLDOUT_MAPE",
            "status": "warn",
            "title": "Holdout error",
            "message_plain": "Forecasts were off by 14% on the held-out periods.",
            "metric": "mape",
            "value": 0.14,
            "threshold": 0.1,
            "suggested_action": "Check the last weeks of data for gaps or one-off events.",
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
                    "message_plain": "All chains converged to the same answer.",
                    "metric": "r_hat_max",
                    "value": 1.004,
                    "threshold": 1.01,
                },
                {
                    "code": "HOLDOUT_MAPE",
                    "status": "warn",
                    "title": "Holdout error",
                    "message_plain": "Forecasts were off by 14% on the last 8 weeks.",
                    "metric": "mape",
                    "value": 0.14,
                    "threshold": 0.1,
                    "suggested_action": "Add holidays or check data for the last weeks.",
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


ValueFormat = Literal["pct", "num", "int"]


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
               "All chains converged to the same answer (R-hat {value}).",
               "The chains mostly agree, but not fully (R-hat {value}).",
               "The chains disagree (R-hat {value}): the results are not reliable.",
               "Run more tuning and draws, or simplify the model.",
           )),
        _c("convergence.ess", "convergence", "ESS", "Enough independent draws", "ess_bulk_min",
           _MIN, 400, 100, (
               "There are enough independent draws ({value}) for stable numbers.",
               "Only {value} independent draws: numbers may move a little between runs.",
               "Only {value} independent draws: the numbers are not stable.",
               "Increase the number of draws.",
           ), "int"),
        _c("convergence.divergences", "convergence", "DIVERGENCES", "Sampler stayed on track",
           "divergence_rate", _MAX, 0.0, 0.01, (
               "The sampler explored the model without problems.",
               "The sampler struggled in a few places ({value} of draws).",
               "The sampler struggled often ({value} of draws): the results may be biased.",
               "Raise target_accept, or use more informative priors.",
           ), "pct"),
        _c("holdout.mape", "holdout", "HOLDOUT_MAPE", "Holdout error", "mape", _MAX,
           0.10, 0.25, (
               "Forecasts were off by {value} on the held-out periods.",
               "Forecasts were off by {value} on the held-out periods.",
               "Forecasts were off by {value} on the held-out periods: too far to trust.",
               "Check the last periods of data for gaps or one-off events, and add holidays "
               "or controls that explain them.",
           ), "pct"),
        _c("holdout.coverage_90", "holdout", "HOLDOUT_COVERAGE", "Uncertainty is honest",
           "coverage_90", _BAND, 0.10, 0.25, (
               "{value} of held-out values fell inside the 90% range, as expected.",
               "{value} of held-out values fell inside the 90% range (expected 90%).",
               "Only {value} of held-out values fell inside the 90% range: the uncertainty "
               "is misleading.",
               "Look for trends or shifts the model does not capture.",
           ), "pct", target=0.90),
        _c("holdout.hit_rate", "holdout", "HOLDOUT_HIT_RATE", "Beats a naive guess",
           "accuracy_lift", _MIN, 0.02, -0.10, (
               "On held-out trips the model's top pick beat always guessing the most popular "
               "product by {value}.",
               "On held-out trips the model's top pick was about as good as always guessing "
               "the most popular product ({value} difference).",
               "On held-out trips the model's top pick did worse than always guessing the "
               "most popular product ({value} difference): its predicted choices are not "
               "reliable.",
               "Add what drives the choice for every product (price, promotions, displays), "
               "or collect more trips per customer.",
           ), "pct"),
        _c("plausibility.negative_roi", "plausibility", "NEGATIVE_ROI", "No channel loses money",
           "negative_roi_share", _MAX, 0.10, 0.50, (
               "No channel is likely to have a negative return.",
               "A channel has a {value} chance of a negative return.",
               "A channel more likely loses than makes money ({value} chance).",
               "Check the channel's spend data, or state an ROI range in business priors.",
           ), "pct"),
        _c("plausibility.roi_absurd", "plausibility", "ROI_ABSURD", "Returns are believable",
           "roi_max", _MAX, 20.0, 100.0, (
               "The highest channel return ({value} per unit spent) is believable.",
               "A channel returns {value} per unit spent, which is unusually high.",
               "A channel returns {value} per unit spent, which is not believable.",
               "Check spend units and gaps, or state an ROI range in business priors.",
           )),
        _c("plausibility.share_outlier", "plausibility", "SHARE_OUTLIER",
           "No channel dominates", "max_channel_share", _MAX, 0.60, 0.90, (
               "No single channel explains most of the KPI.",
               "One channel explains {value} of the KPI, more than usual.",
               "One channel explains {value} of the KPI, which is not plausible.",
               "Check for a missing control (price, promotions, seasonality).",
           ), "pct"),
        _c("plausibility.baseline_share", "plausibility", "BASELINE_SHARE",
           "Baseline is plausible", "baseline_share", _BAND, 0.40, 0.48, (
               "The baseline (KPI without media) is {value}, a plausible share.",
               "The baseline (KPI without media) is {value}, an unusual share.",
               "The baseline (KPI without media) is {value}, which is not plausible.",
               "State a baseline share in business priors, or review the controls.",
           ), "pct", target=0.50),
        _c("plausibility.prior_overlap", "plausibility", "PRIOR_OVERLAP",
           "The data informed the model", "prior_overlap_max", _MAX, 0.90, 0.99, (
               "The data moved every parameter away from its prior.",
               "A parameter barely moved from its prior ({value} overlap): the data says "
               "little about it.",
               "A parameter did not move from its prior at all ({value} overlap): its value "
               "comes from the assumptions, not the data.",
               "Add data that varies the driver (or a lift test), or state a business prior "
               "you trust for it.",
           ), "pct"),
        _c("data.missing", "data", "DATA_MISSING", "Few missing values", "missing_fraction",
           _MAX, 0.05, 0.30, (
               "Only {value} of the values are missing.",
               "{value} of the values are missing.",
               "{value} of the values are missing: too many to model reliably.",
               "Fill the gaps in the source data, or drop the affected series.",
           ), "pct"),
        _c("data.history", "data", "DATA_HISTORY", "Enough history", "history_periods", _MIN,
           104, 52, (
               "There are {value} periods of history, enough to learn seasonality.",
               "Only {value} periods of history: seasonal effects are uncertain.",
               "Only {value} periods of history: too short to separate media from seasonality.",
               "Add more history if you have it.",
           ), "int"),
        _c("data.spend_variation", "data", "SPEND_VARIATION", "Spend varies enough",
           "spend_cv_min", _MIN, 0.10, 0.02, (
               "Every channel's spend varies enough to measure its effect.",
               "A channel's spend barely varies ({value}): its effect is uncertain.",
               "A channel's spend never really changes ({value}): its effect cannot be "
               "measured from this data.",
               "Add a lift test for the channel, or state its ROI in business priors.",
           ), "pct"),
    )
}  # fmt: skip
"""The scorecard check catalog (stable ids; thresholds are the platform defaults)."""


def _fmt(value: float, fmt: ValueFormat) -> str:
    if fmt == "pct":
        return f"{value:.0%}" if abs(value) >= 0.01 or value == 0 else f"{value:.1%}"
    if fmt == "int":
        return f"{round(value):,}"
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
                    "message_plain": "Holdout error: forecasts were off by 14%.",
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
) -> PromotionGateDecision:
    """Decide whether a model version may be promoted (pure).

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
        add("PARTIAL_MODEL", GateOutcome.BLOCKED, "The fit was interrupted before it finished.")
    if fit_mode == "train_only" and not allow_train_only:
        add(
            "TRAIN_ONLY",
            GateOutcome.BLOCKED,
            "The model was fitted without its holdout data; endpoints need a refit on all data.",
        )
    if schema_breaks and schema_change != "allow":
        add(
            "SCHEMA_CHANGE",
            GateOutcome.BLOCKED if schema_change == "block" else GateOutcome.NEEDS_OVERRIDE,
            "The model expects different input than the endpoint's callers send: "
            + "; ".join(schema_breaks),
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
                "The model was never scored on held-out data, so its forecast error is "
                f"unknown ({why}). Add a holdout split (data.split) and holdout metrics "
                "(evaluate.metrics), then refit.",
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
                f"{title}: this check could not be run ({why}).",
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
