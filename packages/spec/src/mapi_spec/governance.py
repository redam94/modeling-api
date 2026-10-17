"""Model governance: policies, the check catalog, evidence, the validation state machine,
approvals and the evidence pack (M3.5).

A :class:`GovernancePolicy` (tenant- or project-scoped, versioned) lists the checks a model
of a family must pass before it is **validated**, with thresholds and severities, and
whether a second person must approve. Checks run as governance workflows in the sandbox
(``governance.policy`` nodes); their :class:`CheckResult` s with :class:`EvidenceItem` s are
stored on a :class:`ModelValidation`.

Validation status (:func:`transition`, pure and exhaustive)::

    draft             --submit-->           checks_running
    checks_running    --checks_passed-->    checks_passed
    checks_running    --checks_failed-->    checks_failed
    checks_running    --cancel-->           draft
    checks_passed     --validate-->         validated          (no approval required)
    checks_passed     --request_approval--> awaiting_approval  (approval required)
    awaiting_approval --approve-->          validated
    awaiting_approval --reject-->           rejected
    checks_failed     --resubmit-->         checks_running
    checks_failed     --reject-->           rejected
    rejected          --resubmit-->         checks_running
    validated         --deprecate-->        deprecated

Approvals need a reviewer who is not the submitter (:func:`check_approver`). When a policy
says ``require_validated_for_promotion``, promoting a model version to an endpoint needs
status ``validated`` (:func:`mapi_spec.scorecard.promotion_gate`, reason ``NOT_VALIDATED``).
Every transition is audited and emitted as a webhook event.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from mapi_spec._dto import (
    EXAMPLE_TS,
    Id,
    RequestModel,
    ResponseModel,
    Sha256,
    TenantId,
    dto_examples,
)
from mapi_spec._types import StrictModel
from mapi_spec.about import About, AboutReference, Difficulty, TypicalRuntime
from mapi_spec.capabilities import check_json_schema
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.scorecard import CheckDirection, CheckThresholds

__all__ = [
    "DEFAULT_POLICIES",
    "EVIDENCE_PACK_FORMAT",
    "GOVERNANCE_CHECKS",
    "GOVERNANCE_OUTPUT",
    "GOVERNANCE_REPORT_FORMAT",
    "SYSTEM_ACTOR",
    "TRANSITIONS",
    "Approval",
    "ApprovalCreate",
    "CheckCategory",
    "CheckResult",
    "CheckSeverity",
    "CheckStatus",
    "EvidenceItem",
    "EvidencePack",
    "EvidencePackRequest",
    "GovernanceCheckDef",
    "GovernanceJob",
    "GovernancePolicy",
    "GovernancePolicyCreate",
    "GovernancePolicyUpdate",
    "GovernanceReport",
    "ModelKind",
    "ModelValidation",
    "PolicyCheck",
    "PolicyMatch",
    "PolicyScope",
    "RefitBudget",
    "ValidationEvent",
    "ValidationNote",
    "ValidationRequest",
    "ValidationStatus",
    "ValidationTransitionRecord",
    "after_checks",
    "check_applies",
    "check_approver",
    "check_status",
    "effective_checks",
    "model_kind",
    "policies_for",
    "requires_validation",
    "rollup_results",
    "transition",
]

EVIDENCE_PACK_FORMAT: Final = "mapi.evidence-pack/v1"
GOVERNANCE_REPORT_FORMAT: Final = "mapi.governance-report/v1"
GOVERNANCE_OUTPUT: Final = "governance.json"
"""What a ``governance`` run writes: a :class:`GovernanceReport` (read by the control plane
as size-capped JSON and stored on the validation)."""
SYSTEM_ACTOR: Final = "system"
"""``created_by`` of the platform's default policies (:data:`DEFAULT_POLICIES`)."""


class CheckSeverity(StrEnum):
    """What a failing check does: ``blocking`` stops validation, ``warning`` is recorded and
    shown to the approver, ``info`` is evidence only."""

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "blocking": 2}[self.value]


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    ERROR = "error"
    """The check could not be computed (counts as a failure for blocking checks)."""
    SKIPPED = "skipped"
    """Not applicable to this model (e.g. prior sensitivity of a frequentist model)."""


CheckCategory = Literal[
    "null_test", "baseline", "residuals", "stability", "plausibility", "sensitivity",
    "recovery", "convergence", "holdout", "data",
]  # fmt: skip
Requirement = Literal["bayesian", "holdout", "time_series", "channels", "residuals"]


class GovernanceCheckDef(ResponseModel):
    """One check of the governance catalog (``GET /v1/catalog/governance-checks``)."""

    id: str = Field(..., pattern=r"^[a-z]+(?:\.[a-z0-9_]+)?$")
    version: str = "1.0.0"
    title: str
    category: CheckCategory
    metric: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,63}$")
    direction: CheckDirection
    warn: float
    fail: float
    target: float | None = None
    requires: list[Requirement] = Field(default_factory=list)
    families: list[str] | None = Field(
        None, description="Model families it applies to (None: every family)"
    )
    params_schema: dict[str, JsonValue] = Field(default_factory=dict)
    default_severity: CheckSeverity = CheckSeverity.BLOCKING
    about: About

    model_config = dto_examples(
        {
            "id": "baseline.beats_naive",
            "title": "Beats simple benchmarks",
            "category": "baseline",
            "metric": "relative_skill",
            "direction": "min",
            "warn": 0.05,
            "fail": 0.0,
            "requires": ["holdout", "time_series"],
            "about": About.model_json_schema()["examples"][0],
        }
    )

    @field_validator("params_schema")
    @classmethod
    def _schema(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return check_json_schema(v)

    @model_validator(mode="after")
    def _dir(self) -> GovernanceCheckDef:
        if (self.direction is CheckDirection.BAND) != (self.target is not None):
            raise ValueError("band checks (and only they) have a target")
        lenient = self.fail >= self.warn
        if (self.direction is CheckDirection.MIN) == lenient and self.fail != self.warn:
            raise ValueError(f"{self.id}: fail must be at least as strict as warn")
        return self


def _a(title: str, summary: str, use: Sequence[str], avoid: Sequence[str], needs: Sequence[str],
       assumes: Sequence[str], outputs: Sequence[str], difficulty: Difficulty,
       runtime: TypicalRuntime, example: str, *, related: Sequence[str] = (),
       expert: str | None = None,
       refs: Sequence[tuple[str, str | None]] = ()) -> About:  # fmt: skip
    return About(
        title=title, summary_plain=summary, use_when=list(use), avoid_when=list(avoid),
        data_needs=list(needs), assumptions=list(assumes), outputs=list(outputs),
        difficulty=difficulty, typical_runtime=runtime, example=example, related=list(related),
        expert_notes=expert, references=[AboutReference(title=t, url=u) for t, u in refs],
        fits={"goal": ["validate_model"]},
    )  # fmt: skip


def _obj(props: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": dict(props)}


_MAX, _MIN, _BAND = CheckDirection.MAX, CheckDirection.MIN, CheckDirection.BAND
_B, _I, _X = Difficulty.BEGINNER, Difficulty.INTERMEDIATE, Difficulty.EXPERT
_SEC, _MINS, _LONG = TypicalRuntime.SECONDS, TypicalRuntime.MINUTES, TypicalRuntime.LONG

_CHECKS: list[GovernanceCheckDef] = [
    GovernanceCheckDef(
        id="null.permutation_target", title="Beats a shuffled result", category="null_test",
        metric="permutation_p_value", direction=_MAX, warn=0.05, fail=0.10,
        requires=["holdout"],
        params_schema=_obj({
            "n_permutations": {"type": "integer", "minimum": 5, "maximum": 200, "default": 20},
            "metric": {"type": "string", "default": "rmse",
                       "enum": ["rmse", "mae", "mape", "smape", "crps"]},
            "block": {"type": "integer", "minimum": 1, "maximum": 520,
                      "description": "Periods of the drivers shuffled together (default: a "
                      "twentieth of the series)"},
        }),
        about=_a(
            "Beats a shuffled result",
            "Refits the model on copies of the data where the drivers are shuffled in time "
            "(for a model without drivers, the result's ups and downs); a real model must do "
            "clearly better on the true data than on the shuffled copies.",
            ["Every model whose accuracy is used to make decisions"],
            ["Models without a test period (there is nothing to compare)"],
            ["The model's data, its settings and a test period"],
            ["Shuffling the drivers breaks any real link between them and the result, while "
             "trend, season and the result itself stay as they are"],
            ["The share of shuffled fits that did as well as the real one (a p-value)",
             "The distribution of shuffled scores"],
            _I, _LONG,
            "The real model forecasts better than all 19 shuffled fits: p = 0.05, pass.",
            related=["null.noise_regressors", "baseline.beats_naive"],
            expert="Permutation (randomisation) test on a scale-free statistic: holdout skill "
            "1 - L(model) / L(null forecast) on each dataset (the null forecast is the last "
            "training value of an integrated series, else its mean). Null: models with drivers "
            "keep the result and get their drivers block-permuted jointly (so trend, season "
            "and controls cannot pass on their own; a model that ignores its drivers is "
            "tested as one without); models without drivers get the d-th differences of the "
            "result permuted i.i.d. and re-integrated from the original start (d from the "
            "ARIMA order or ADF/KPSS), which keeps the integration order and the increments' "
            "distribution but destroys predictable dynamics. Without a time order the result "
            "is permuted across rows and the statistic is the holdout error. "
            "p = (1 + #{perm >= real}) / (K + 1).",
            refs=[("Ojala & Garriga (2010), Permutation tests for studying classifier "
                   "performance", None)],
        ),
    ),
    GovernanceCheckDef(
        id="null.noise_regressors", title="Ignores fake drivers", category="null_test",
        metric="noise_effect_share", direction=_MAX, warn=0.02, fail=0.05,
        params_schema=_obj({
            "n_noise": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            "kind": {"type": "string", "enum": ["gaussian", "shuffled_driver"],
                     "default": "shuffled_driver"},
            "seed": {"type": "integer", "minimum": 0, "default": 0},
        }),
        about=_a(
            "Ignores fake drivers",
            "Adds a few made-up drivers that are pure noise and refits; a trustworthy model "
            "gives them almost no effect.",
            ["Marketing mix and other models that split a result between drivers"],
            ["Pure forecasting models without drivers"],
            ["The model's data and settings"],
            ["The fake drivers look like real ones but carry no signal"],
            ["The largest share of the real drivers' effect given to a fake driver",
             "Whether any fake driver came out clearly above the limit"],
            _I, _LONG,
            "Three fake channels get 0.4%, 0.1% and 0.9% of the media effect: pass.",
            related=["null.permutation_target", "plausibility.signs"],
            expert="Placebo regressors drawn as Gaussian noise or as a driver shuffled in "
            "blocks, with the model's default prior (never a real channel's prior or "
            "calibration); the statistic is the largest placebo share of the real drivers' "
            "total effect (by ablation through the score path). Bayesian: flagged when "
            "P(share > warn limit) >= 0.9 over the posterior (practical equivalence, valid "
            "for sign-constrained effects); frequentist: placebo p-values with Holm's "
            "correction.",
        ),
    ),
    GovernanceCheckDef(
        id="null.placebo_time_shift", title="Effects vanish when shifted in time",
        category="null_test", metric="placebo_effect_ratio", direction=_MAX, warn=0.25,
        fail=0.5, requires=["time_series"],
        params_schema=_obj({
            "shifts": {"type": "array", "items": {"type": "integer"}, "minItems": 1,
                       "maxItems": 6, "default": [-26, 26]},
        }),
        about=_a(
            "Effects vanish when shifted in time",
            "Moves the drivers months away from when they really happened and refits; the "
            "effects should mostly disappear, otherwise the model picks up timing patterns "
            "rather than real effects.",
            ["Models that credit results to drivers over time (marketing mix, regressions)"],
            ["Very short series (the shift leaves too little overlap)"],
            ["The model's data and settings over time"],
            ["A real effect is tied to when the driver happened"],
            ["Placebo effect as a share of the real effect, per shift"],
            _X, _LONG,
            "Shifting spend by half a year keeps only 8% of the effect: pass.",
            related=["null.noise_regressors"],
            expert="Time-shift placebo (leads/lags beyond any plausible carry-over); ratio of "
            "the placebo total effect to the real one, maximised over shifts.",
        ),
    ),
    GovernanceCheckDef(
        id="baseline.beats_naive", title="Beats simple benchmarks", category="baseline",
        metric="relative_skill", direction=_MIN, warn=0.05, fail=0.0,
        requires=["holdout", "time_series"],
        params_schema=_obj({
            "methods": {"type": "array", "items": {"type": "string",
                        "enum": ["naive", "seasonal_naive", "mean", "drift"]},
                        "default": ["naive", "seasonal_naive"]},
            "metric": {"type": "string", "default": "mae"},
        }),
        about=_a(
            "Beats simple benchmarks",
            "Compares the model's error on the test periods with very simple forecasts such "
            "as 'same as last week' or 'same as last year'; the model must be better.",
            ["Every forecasting model"], ["Models that do not forecast over time"],
            ["The model's test periods and the history before them"],
            ["Benchmarks are scored on exactly the same periods"],
            ["Skill against the strongest benchmark (1 - model error / benchmark error)"],
            _B, _SEC,
            "The model's error is 30% lower than 'same as last year': skill 0.30, pass.",
            related=["evaluate.baseline", "holdout.accuracy"],
            expert="Relative skill 1 - L(model)/min_b L(b) on the holdout or backtest folds; "
            "MASE-style scaling by the in-sample seasonal-naive error is reported as well.",
            refs=[("Hyndman & Koehler (2006), Another look at measures of forecast accuracy",
                   None)],
        ),
    ),
    GovernanceCheckDef(
        id="residuals.whiteness", title="No pattern left in the errors", category="residuals",
        metric="ljung_box_p_value", direction=_MIN, warn=0.05, fail=0.01,
        requires=["residuals", "time_series"],
        params_schema=_obj({"lags": {"type": "integer", "minimum": 1, "maximum": 200,
                                     "default": 10}}),
        about=_a(
            "No pattern left in the errors",
            "Checks that the model's errors look like random noise over time; a leftover "
            "pattern means the model misses something it could have learned.",
            ["Time-series and regression models"], ["Models without residuals"],
            ["The model's in-sample errors"], ["Regular time steps"],
            ["The Ljung-Box p-value and the errors' autocorrelations"],
            _I, _SEC, "Ljung-Box p = 0.38 at 10 lags: pass.",
            related=["test.ljung_box", "evaluate.residuals"],
            expert="Ljung-Box Q on residuals (model_df adjusted for ARMA terms); the minimum "
            "p-value over series for panels.",
        ),
    ),
    GovernanceCheckDef(
        id="stability.rolling_params", title="Estimates are stable over time",
        category="stability", metric="param_variation", direction=_MAX, warn=0.25, fail=0.5,
        requires=["time_series"],
        params_schema=_obj({
            "window": {"type": "integer", "minimum": 8, "description": "Periods per window"},
            "step": {"type": "integer", "minimum": 1, "default": 4},
            "params": {"type": "array", "items": {"type": "string"},
                       "description": "Parameters to watch (default: the key effects)"},
        }),
        about=_a(
            "Estimates are stable over time",
            "Refits the model on rolling windows of history and checks that its key "
            "estimates do not swing wildly from one window to the next.",
            ["Models whose estimates drive decisions (effects, returns)"],
            ["Short histories (windows would be too small)"],
            ["The model's data over time and settings"],
            ["The true effects change slowly"],
            ["How much each key estimate varies across windows"],
            _X, _LONG, "TV's return varies by 12% across windows: pass.",
            related=["test.cusum"],
            expert="Coefficient of variation (or max relative deviation from the full-sample "
            "estimate) of each watched parameter across rolling-window refits; maximum over "
            "parameters.",
        ),
    ),
    GovernanceCheckDef(
        id="plausibility.signs", title="Effects point the right way", category="plausibility",
        metric="wrong_sign_share", direction=_MAX, warn=0.1, fail=0.25,
        params_schema=_obj({
            "expected": {"type": "object", "additionalProperties": {
                "type": "string", "enum": ["positive", "negative"]},
                "description": "Parameter -> expected sign (default: media positive)"},
        }),
        about=_a(
            "Effects point the right way",
            "Checks that effects have the direction business sense expects, for example "
            "that advertising does not reduce sales and that higher prices do not raise "
            "demand.",
            ["Any model with interpretable effects"], ["Pure forecasting models"],
            ["The model's estimates and the expected directions"],
            ["The expected directions are right"],
            ["The share of the estimate (or of the uncertainty) on the wrong side, per effect"],
            _B, _SEC, "Radio's effect is negative with 40% probability: fail.",
            related=["null.noise_regressors", "test.vif"],
        ),
    ),
    GovernanceCheckDef(
        id="sensitivity.prior", title="Not driven by assumptions", category="sensitivity",
        metric="prior_sensitivity", direction=_MAX, warn=0.2, fail=0.5,
        requires=["bayesian"],
        params_schema=_obj({
            "scales": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0},
                       "default": [0.5, 2.0], "description": "Prior widths tried"},
        }),
        about=_a(
            "Not driven by assumptions",
            "Refits a Bayesian model with wider and narrower starting assumptions and "
            "checks the key answers barely move, so they come from the data.",
            ["Bayesian models, especially with business priors"],
            ["Models that are not Bayesian"],
            ["The model's data, settings and priors"],
            ["Reasonable prior changes are covered by the scales tried"],
            ["The largest relative change of a key estimate"],
            _X, _LONG, "Doubling prior widths moves TV's return by 6%: pass.",
            related=["plausibility.signs", "recovery.simulated"],
            expert="Power-scaling or refit-based prior sensitivity: relative change of "
            "posterior means of key quantities under scaled prior widths.",
            refs=[("Kallioinen et al. (2023), Detecting and diagnosing prior and likelihood "
                   "sensitivity with power-scaling", "https://arxiv.org/abs/2107.14054")],
        ),
    ),
    GovernanceCheckDef(
        id="recovery.simulated", title="Finds known answers", category="recovery",
        metric="recovery_coverage", direction=_BAND, warn=0.1, fail=0.25, target=0.9,
        params_schema=_obj({
            "n_sims": {"type": "integer", "minimum": 5, "maximum": 100, "default": 20},
            "params": {"type": "array", "items": {"type": "string"}},
        }),
        about=_a(
            "Finds known answers",
            "Simulates data from the model with known true values, refits, and checks that "
            "the model's ranges contain the true values about as often as they should.",
            ["New or heavily customised models", "Before trusting a complex Bayesian model"],
            ["Models that cannot simulate data"],
            ["The model's settings and data shape"],
            ["The simulated data resembles the real data"],
            ["How often the 90% ranges contained the truth"],
            _X, _LONG, "True values fell inside the 90% ranges 88% of the time: pass.",
            related=["sensitivity.prior", "convergence"],
            expert="Simulation-based calibration lite: N simulate-fit cycles; coverage of "
            "central 90% intervals and rank uniformity per parameter.",
            refs=[("Talts et al. (2018), Validating Bayesian inference algorithms with "
                   "simulation-based calibration", "https://arxiv.org/abs/1804.06788")],
        ),
    ),
    GovernanceCheckDef(
        id="convergence", title="The fit finished properly", category="convergence",
        metric="r_hat_max", direction=_MAX, warn=1.01, fail=1.05,
        params_schema=_obj({}),
        about=_a(
            "The fit finished properly",
            "Checks that the fitting procedure settled on a stable answer, so running it "
            "again would give the same result.",
            ["Every model"], ["Models fitted outside the platform without diagnostics"],
            ["The fit's diagnostics"], ["Diagnostics were recorded by the fit"],
            ["R-hat, effective draws and divergences (Bayesian) or the optimiser status"],
            _B, _SEC, "All chains agree (R-hat 1.003): pass.",
            related=["recovery.simulated"],
            expert="Bayesian: max split R-hat (rank-normalised), with ESS and divergences as "
            "evidence; frequentist: optimiser convergence flag (value 1.0 when converged, "
            "inf otherwise).",
        ),
    ),
    GovernanceCheckDef(
        id="holdout.accuracy", title="Accurate on unseen data", category="holdout",
        metric="holdout_error", direction=_MAX, warn=0.15, fail=0.30, requires=["holdout"],
        params_schema=_obj({"metric": {"type": "string", "default": "mape",
                                       "enum": ["mape", "smape", "mase", "rmse", "mae"]}}),
        about=_a(
            "Accurate on unseen data",
            "Checks the model's error on periods it was not fitted on stays below the "
            "limit your organisation sets.",
            ["Every model used for forecasts or what-ifs"],
            ["Models without a test period"],
            ["A test period or backtest"],
            ["The test periods resemble future use"],
            ["The chosen error metric on the test data"],
            _B, _SEC, "MAPE 9% against a limit of 15%: pass.",
            related=["baseline.beats_naive", "evaluate.metrics"],
        ),
    ),
    GovernanceCheckDef(
        id="data.quality", title="Data is good enough", category="data",
        metric="data_issue_count", direction=_MAX, warn=0, fail=2,
        params_schema=_obj({
            "max_missing": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.05},
            "min_history_periods": {"type": "integer", "minimum": 1, "default": 52},
        }),
        about=_a(
            "Data is good enough",
            "Checks the data before trusting the model: few gaps, enough history, no "
            "constant columns and no impossible values.",
            ["Every model"], ["Synthetic data you generated yourself"],
            ["The dataset the model was fitted on"], ["The limits fit your data"],
            ["Data problems found, each in plain words"],
            _B, _SEC, "No gaps, 3 years of history, every driver varies: pass.",
            related=["convergence"],
        ),
        default_severity=CheckSeverity.BLOCKING,
    ),
]  # fmt: skip

GOVERNANCE_CHECKS: Final[dict[str, GovernanceCheckDef]] = {c.id: c for c in _CHECKS}
"""The governance check catalog by id."""


def check_status(
    check: GovernanceCheckDef | str, value: float, thresholds: CheckThresholds | None = None
) -> CheckStatus:
    """``pass``/``warn``/``fail`` of a measured value (a non-finite value is ``error``)."""
    c = GOVERNANCE_CHECKS[check] if isinstance(check, str) else check
    if not math.isfinite(value):
        return CheckStatus.ERROR
    warn, fail = (thresholds.warn, thresholds.fail) if thresholds else (c.warn, c.fail)
    if c.direction is CheckDirection.MIN:
        return (
            CheckStatus.FAIL
            if value < fail
            else CheckStatus.WARN
            if value < warn
            else CheckStatus.PASS
        )
    v = abs(value - c.target) if c.target is not None else value
    return CheckStatus.FAIL if v > fail else CheckStatus.WARN if v > warn else CheckStatus.PASS


# ================================================================================== policies


class PolicyCheck(StrictModel):
    """A required check in a policy: thresholds, severity and parameters."""

    check: str
    severity: CheckSeverity = CheckSeverity.BLOCKING
    thresholds: CheckThresholds | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known(self) -> PolicyCheck:
        c = GOVERNANCE_CHECKS.get(self.check)
        if c is None:
            raise ValueError(f"unknown governance check {self.check!r}")
        from jsonschema import Draft202012Validator

        errs = list(Draft202012Validator(c.params_schema).iter_errors(self.params))
        if errs:
            raise ValueError(f"{self.check}: {errs[0].message[:200]}")
        if self.thresholds is not None:
            t = self.thresholds
            lenient = t.fail >= t.warn
            if (c.direction is CheckDirection.MIN) == lenient and t.fail != t.warn:
                raise ValueError(f"{self.check}: fail must be at least as strict as warn")
        return self


class PolicyMatch(StrictModel):
    """Which models a policy covers; an empty list matches everything on that axis.

    A family matches a model's declared family (``forecast`` for the statsmodels
    forecasters, ``ts_multivariate``, ``mmm``) *or* the namespace of its id (``ts`` for
    ``ts.sarimax``), so ``families: [ts]`` covers every ``ts.*`` model whatever family its
    plugin declares."""

    families: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Model families or model-id namespaces (ts covers ts.sarimax)",
    )
    models: list[str] = Field(default_factory=list, max_length=64, description="Model ids")
    templates: list[str] = Field(default_factory=list, max_length=32)

    def matches(self, *, family: str, model_id: str, template: str | None) -> bool:
        namespace = model_id.split(".", 1)[0]
        if self.families and family not in self.families and namespace not in self.families:
            return False
        if self.models and model_id not in self.models:
            return False
        return not self.templates or (template is not None and template in self.templates)


PolicyScope = Literal["tenant", "project"]


class GovernancePolicyCreate(RequestModel):
    """``POST /v1/governance/policies`` (project) or ``/v1/tenants/{id}/governance/policies``."""

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=4000)
    applies_to: PolicyMatch = Field(default_factory=PolicyMatch)
    checks: list[PolicyCheck] = Field(..., min_length=1, max_length=32)
    require_approval: bool = False
    approvals_required: int = Field(1, ge=1, le=3)
    require_validated_for_promotion: bool = True
    active: bool = True

    model_config = dto_examples(
        {
            "name": "Forecast models",
            "applies_to": {"families": ["ts"]},
            "checks": [
                {"check": "baseline.beats_naive", "severity": "blocking"},
                {"check": "residuals.whiteness", "severity": "warning"},
                {"check": "null.permutation_target", "severity": "blocking",
                 "params": {"n_permutations": 20}},
                {"check": "holdout.accuracy", "thresholds": {"warn": 0.1, "fail": 0.2}},
            ],
            "require_approval": True,
        }
    )  # fmt: skip

    @model_validator(mode="after")
    def _unique(self) -> GovernancePolicyCreate:
        ids = [c.check for c in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("a policy lists each check once")
        return self


class GovernancePolicyUpdate(GovernancePolicyCreate):
    """``PUT …/policies/{policy_id}``: a new version (the old one stays for audit)."""

    expected_version: int | None = Field(None, ge=1, description="409 when it changed")


class GovernancePolicy(ResponseModel):
    """A versioned policy. ``project_id`` is ``None`` for tenant-wide policies."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId | None = None
    scope: PolicyScope
    version: int = Field(..., ge=1)
    name: str
    description: str | None = None
    applies_to: PolicyMatch = Field(default_factory=PolicyMatch)
    checks: list[PolicyCheck]
    require_approval: bool = False
    approvals_required: int = 1
    require_validated_for_promotion: bool = True
    active: bool = True
    created_at: AwareDatetime
    created_by: str | None = None

    model_config = dto_examples(
        {
            "id": "pol_01jaf3h1j2k3m4n5p6q7r8s9t0",
            "tenant_id": "acme",
            "project_id": None,
            "scope": "tenant",
            "version": 3,
            "name": "All models",
            "checks": [{"check": "convergence"}, {"check": "data.quality"},
                       {"check": "null.permutation_target"}],
            "require_approval": True,
            "created_at": EXAMPLE_TS,
        }
    )  # fmt: skip

    @model_validator(mode="after")
    def _scope(self) -> GovernancePolicy:
        if (self.scope == "tenant") != (self.project_id is None):
            raise ValueError("tenant policies have no project; project policies have one")
        return self

    def applies(self, *, family: str, model_id: str, template: str | None = None) -> bool:
        return self.active and self.applies_to.matches(
            family=family, model_id=model_id, template=template
        )


def policies_for(
    policies: Iterable[GovernancePolicy],
    *,
    project_id: str,
    family: str,
    model_id: str,
    template: str | None = None,
) -> list[GovernancePolicy]:
    """The active policies covering a model in ``project_id``: tenant-wide ones and the
    project's own (tenant first)."""
    out = [
        p
        for p in policies
        if (p.project_id is None or p.project_id == project_id)
        and p.applies(family=family, model_id=model_id, template=template)
    ]
    return sorted(out, key=lambda p: (p.project_id is not None, p.id))


_ParamRule = Literal["max", "min", "union", "union_map"]
_PARAM_RULES: Final[dict[str, dict[str, _ParamRule]]] = {
    "null.permutation_target": {"n_permutations": "max"},
    "null.noise_regressors": {"n_noise": "max"},
    "null.placebo_time_shift": {"shifts": "union"},
    "baseline.beats_naive": {"methods": "union"},
    "residuals.whiteness": {"lags": "max"},
    "stability.rolling_params": {"params": "union"},
    "plausibility.signs": {"expected": "union_map"},
    "sensitivity.prior": {"scales": "union"},
    "recovery.simulated": {"n_sims": "max", "params": "union"},
    "data.quality": {"max_missing": "min", "min_history_periods": "max"},
}
"""How a check parameter set by two policies merges: ``max``/``min`` keep the stricter
number, ``union`` keeps every list item (more benchmarks, shifts, watched parameters) and
``union_map`` keeps every key with the earlier policy winning a conflict. A parameter
without a rule (a metric, a block length, a seed) keeps the earlier policy's value; a
later policy may only add parameters the earlier ones left unset."""


def _merge_params(check: str, first: Mapping[str, Any], later: Mapping[str, Any]) -> dict[str, Any]:
    rules = _PARAM_RULES.get(check, {})
    out = dict(first)
    for key, value in later.items():
        if key not in out:
            out[key] = value
            continue
        rule, prev = rules.get(key), out[key]
        if rule == "max":
            out[key] = max(prev, value)
        elif rule == "min":
            out[key] = min(prev, value)
        elif rule == "union" and isinstance(prev, list) and isinstance(value, list):
            out[key] = [*prev, *(v for v in value if v not in prev)]
        elif rule == "union_map" and isinstance(prev, dict) and isinstance(value, dict):
            out[key] = {**value, **prev}
    return out


def effective_checks(policies: Sequence[GovernancePolicy]) -> list[PolicyCheck]:
    """The union of the policies' checks: for a check named twice, the stricter severity,
    the stricter thresholds and the stricter parameters win (tenant policies are a floor
    projects cannot lower; see :data:`_PARAM_RULES`). Tenant policies are merged first
    whatever order ``policies`` comes in."""
    merged: dict[str, PolicyCheck] = {}
    for p in sorted(policies, key=lambda p: p.project_id is not None):
        for c in p.checks:
            prev = merged.get(c.check)
            if prev is None:
                merged[c.check] = c
                continue
            sev = c.severity if c.severity.rank > prev.severity.rank else prev.severity
            merged[c.check] = prev.model_copy(
                update={
                    "severity": sev,
                    "thresholds": _stricter(c.check, prev.thresholds, c.thresholds),
                    "params": _merge_params(c.check, prev.params, c.params),
                }
            )
    return list(merged.values())


def _stricter(
    check: str, a: CheckThresholds | None, b: CheckThresholds | None
) -> CheckThresholds | None:
    if a is None or b is None:
        return a or b
    if GOVERNANCE_CHECKS[check].direction is CheckDirection.MIN:
        return CheckThresholds(warn=max(a.warn, b.warn), fail=max(a.fail, b.fail))
    return CheckThresholds(warn=min(a.warn, b.warn), fail=min(a.fail, b.fail))


def requires_validation(policies: Iterable[GovernancePolicy]) -> bool:
    """Whether promotion to an endpoint needs status ``validated`` under ``policies``."""
    return any(p.active and p.require_validated_for_promotion for p in policies)


# ================================================================================== evidence


class EvidenceItem(ResponseModel):
    """One piece of evidence behind a check: a number, a small table, a plot or a stored
    artifact (``ref`` is a run artifact name or a ``model://`` reference)."""

    kind: Literal["metric", "table", "plot", "artifact", "text"]
    name: str = Field(..., max_length=200)
    value: float | None = None
    inline: JsonValue = None
    ref: str | None = Field(None, max_length=512)
    run_id: Id | None = None
    caption_plain: str | None = Field(None, max_length=500)


class CheckResult(ResponseModel):
    """The outcome of one check on one model version."""

    check: str
    status: CheckStatus
    severity: CheckSeverity
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None
    message_plain: str = Field(..., max_length=2000)
    message_expert: str | None = Field(None, max_length=2000)
    suggested_action: str | None = Field(
        None, max_length=1000, description="What to do about a warning or failure (plain words)"
    )
    params: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=50)
    run_id: Id | None = Field(None, description="The run that computed it")
    computed_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "check": "null.permutation_target",
            "status": "pass",
            "severity": "blocking",
            "metric": "permutation_p_value",
            "value": 0.048,
            "threshold": 0.05,
            "message_plain": "The model did better than all but 1 of 20 fits on shuffled "
            "results.",
            "evidence": [{"kind": "table", "name": "permutation_scores",
                          "inline": {"real": 0.081, "shuffled_min": 0.079}}],
            "computed_at": EXAMPLE_TS,
        }
    )  # fmt: skip

    @property
    def blocks(self) -> bool:
        """A blocking check that failed or errored stops validation."""
        return self.severity is CheckSeverity.BLOCKING and self.status in (
            CheckStatus.FAIL,
            CheckStatus.ERROR,
        )


def rollup_results(
    results: Sequence[CheckResult], required: Sequence[PolicyCheck]
) -> tuple[bool, list[str]]:
    """``(passed, failing check ids)``: every required blocking check has a result that
    does not block (a missing result counts as a failure)."""
    by_id = {r.check: r for r in results}
    failing: list[str] = []
    for c in required:
        r = by_id.get(c.check)
        if r is None:
            if c.severity is CheckSeverity.BLOCKING:
                failing.append(c.check)
            continue
        if r.blocks:
            failing.append(c.check)
    return (not failing, failing)


# ========================================================================== state machine


class ValidationStatus(StrEnum):
    DRAFT = "draft"
    CHECKS_RUNNING = "checks_running"
    CHECKS_PASSED = "checks_passed"
    CHECKS_FAILED = "checks_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    VALIDATED = "validated"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"

    @property
    def terminal(self) -> bool:
        return self is ValidationStatus.DEPRECATED


class ValidationEvent(StrEnum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    CHECKS_PASSED = "checks_passed"
    CHECKS_FAILED = "checks_failed"
    REQUEST_APPROVAL = "request_approval"
    VALIDATE = "validate"
    APPROVE = "approve"
    REJECT = "reject"
    RESUBMIT = "resubmit"
    DEPRECATE = "deprecate"


_S, _EV = ValidationStatus, ValidationEvent
TRANSITIONS: Final[dict[tuple[ValidationStatus, ValidationEvent], ValidationStatus]] = {
    (_S.DRAFT, _EV.SUBMIT): _S.CHECKS_RUNNING,
    (_S.CHECKS_RUNNING, _EV.CHECKS_PASSED): _S.CHECKS_PASSED,
    (_S.CHECKS_RUNNING, _EV.CHECKS_FAILED): _S.CHECKS_FAILED,
    (_S.CHECKS_RUNNING, _EV.CANCEL): _S.DRAFT,
    (_S.CHECKS_PASSED, _EV.REQUEST_APPROVAL): _S.AWAITING_APPROVAL,
    (_S.CHECKS_PASSED, _EV.VALIDATE): _S.VALIDATED,
    (_S.CHECKS_FAILED, _EV.RESUBMIT): _S.CHECKS_RUNNING,
    (_S.CHECKS_FAILED, _EV.REJECT): _S.REJECTED,
    (_S.AWAITING_APPROVAL, _EV.APPROVE): _S.VALIDATED,
    (_S.AWAITING_APPROVAL, _EV.REJECT): _S.REJECTED,
    (_S.REJECTED, _EV.RESUBMIT): _S.CHECKS_RUNNING,
    (_S.VALIDATED, _EV.DEPRECATE): _S.DEPRECATED,
}
"""Every allowed ``(status, event) -> status``; anything else is refused."""


def transition(
    status: ValidationStatus | str,
    event: ValidationEvent | str,
    *,
    require_approval: bool = False,
) -> ValidationStatus:
    """The status after ``event`` (pure). ``VALIDATE`` straight from ``checks_passed`` is
    refused when the policy requires approval (use ``REQUEST_APPROVAL``), and
    ``REQUEST_APPROVAL`` when it does not. Raises ``VALIDATION_TRANSITION_INVALID``."""
    s, e = ValidationStatus(status), ValidationEvent(event)
    nxt = TRANSITIONS.get((s, e))
    if nxt is None:
        raise MapiError(
            f"a model version in status {s.value!r} cannot {e.value.replace('_', ' ')}",
            code=ErrorCode.VALIDATION_TRANSITION_INVALID,
        )
    if s is ValidationStatus.CHECKS_PASSED:
        if e is ValidationEvent.VALIDATE and require_approval:
            raise MapiError(
                "the policy requires a reviewer's approval before validation",
                code=ErrorCode.VALIDATION_TRANSITION_INVALID,
            )
        if e is ValidationEvent.REQUEST_APPROVAL and not require_approval:
            raise MapiError(
                "the policy does not require approval: validate directly",
                code=ErrorCode.VALIDATION_TRANSITION_INVALID,
            )
    return nxt


def after_checks(passed: bool, *, require_approval: bool) -> list[ValidationEvent]:
    """The events the orchestrator applies when a check run finishes: ``checks_passed``
    then ``validate`` or ``request_approval``; or ``checks_failed``."""
    if not passed:
        return [ValidationEvent.CHECKS_FAILED]
    return [
        ValidationEvent.CHECKS_PASSED,
        ValidationEvent.REQUEST_APPROVAL if require_approval else ValidationEvent.VALIDATE,
    ]


# =============================================================================== approvals


class ApprovalCreate(RequestModel):
    """``POST /v1/validations/{validation_id}/approvals``: a reviewer's decision."""

    decision: Literal["approve", "reject"]
    comment: str | None = Field(None, max_length=4000)

    model_config = dto_examples(
        {"decision": "approve", "comment": "Evidence reviewed; null tests are convincing."}
    )

    @model_validator(mode="after")
    def _reason(self) -> ApprovalCreate:
        if self.decision == "reject" and not (self.comment and self.comment.strip()):
            raise ValueError("a rejection needs a comment")
        return self


class Approval(ResponseModel):
    id: Id
    validation_id: Id
    approver_id: str
    decision: Literal["approve", "reject"]
    comment: str | None = None
    decided_at: AwareDatetime

    model_config = dto_examples(
        {
            "id": "apv_01jaf3j1k2m3n4p5q6r7s8t9v0",
            "validation_id": "val_01jaf3k1m2n3p4q5r6s7t8v9w0",
            "approver_id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "decision": "approve",
            "decided_at": EXAMPLE_TS,
        }
    )


def check_approver(
    *, submitter_id: str, approver_id: str, is_reviewer: bool, already_approved: Iterable[str] = ()
) -> None:
    """Refuse an approval by the submitter (``SELF_APPROVAL_FORBIDDEN``), by someone without
    the reviewer capability (``FORBIDDEN``) or twice by the same person (``CONFLICT``)."""
    if approver_id == submitter_id:
        raise MapiError(
            "the person who submitted a model for validation cannot approve it",
            code=ErrorCode.SELF_APPROVAL_FORBIDDEN,
        )
    if not is_reviewer:
        raise MapiError("approving needs the reviewer capability", code=ErrorCode.FORBIDDEN)
    if approver_id in set(already_approved):
        raise MapiError("you already decided on this validation", code=ErrorCode.CONFLICT)


# ============================================================================ validation DTO


class ValidationRequest(RequestModel):
    """``POST /v1/models/{model_id}/versions/{version}/validations``: run the checks."""

    policy_ids: list[Id] = Field(
        default_factory=list, max_length=8, description="Default: every policy that applies"
    )
    note: str | None = Field(None, max_length=2000)
    dataset: str | None = Field(
        None, max_length=512, description="dataset:// for data checks (default: the fit's)"
    )

    model_config = dto_examples({"note": "Q4 refresh of the geo MMM"})


class ValidationTransitionRecord(ResponseModel):
    """One step of a validation's history (also an audit event)."""

    from_status: ValidationStatus
    to_status: ValidationStatus
    event: ValidationEvent
    actor_id: str | None = None
    at: AwareDatetime
    note: str | None = Field(None, max_length=2000)


class ModelValidation(ResponseModel):
    """A validation of one model version: status, policies, results, approvals, history."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId
    model_id: Id
    version: int = Field(..., ge=1)
    status: ValidationStatus
    policies: list[str] = Field(default_factory=list, description="<policy id>@<version>")
    required_checks: list[PolicyCheck] = Field(default_factory=list)
    results: list[CheckResult] = Field(default_factory=list)
    failing_checks: list[str] = Field(default_factory=list)
    require_approval: bool = False
    approvals_required: int = 1
    approvals: list[Approval] = Field(default_factory=list)
    history: list[ValidationTransitionRecord] = Field(default_factory=list)
    workflow_run_id: Id | None = Field(None, description="The governance workflow run")
    submitted_by: str | None = None
    submitted_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    model_config = dto_examples(
        {
            "id": "val_01jaf3k1m2n3p4q5r6s7t8v9w0",
            "tenant_id": "acme",
            "project_id": "growth",
            "model_id": "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0",
            "version": 7,
            "status": "awaiting_approval",
            "policies": ["pol_01jaf3h1j2k3m4n5p6q7r8s9t0@3"],
            "results": [CheckResult.model_json_schema()["examples"][0]],
            "require_approval": True,
            "submitted_by": "usr_01jaf3a0b0c0d0e0f0g0h0j0k0",
            "submitted_at": EXAMPLE_TS,
            "updated_at": EXAMPLE_TS,
        }
    )


class ValidationNote(RequestModel):
    """``POST /v1/validations/{validation_id}:cancel|:resubmit|:deprecate``: why (a
    deprecation needs a reason; it is audited)."""

    note: str | None = Field(None, max_length=2000)

    model_config = dto_examples({"note": "Superseded by v8 after the Q1 data refresh."})


class EvidencePackRequest(RequestModel):
    """``POST /v1/validations/{validation_id}/evidence-pack``."""

    format: Literal["html", "json"] = "html"
    include_plots: bool = True

    model_config = dto_examples({"format": "html", "include_plots": True})


class EvidencePack(ResponseModel):
    """The audit export of a validation (``format`` ``mapi.evidence-pack/v1``; the HTML
    rendering is a static, script-free document with the same content)."""

    format: Literal["mapi.evidence-pack/v1"] = EVIDENCE_PACK_FORMAT
    validation: ModelValidation
    model_ref: str = Field(..., description="model://project/name@vN")
    lineage: dict[str, JsonValue] = Field(default_factory=dict)
    policies: list[GovernancePolicy] = Field(default_factory=list)
    scorecard: dict[str, JsonValue] | None = None
    generated_at: AwareDatetime
    generated_by: str | None = None
    digest: Sha256 | None = Field(None, description="sha256 of the canonical JSON content")
    download: dict[str, JsonValue] | None = Field(
        None, description="Signed link to the HTML rendering (ArtifactLink)"
    )

    model_config = dto_examples(
        {
            "validation": ModelValidation.model_json_schema()["examples"][0],
            "model_ref": "model://growth/geo-mmm@v7",
            "lineage": {"dataset": "dataset://growth/weekly_sales@v4",
                        "plugin": "mmm.bayesian@1.0.0"},
            "policies": [GovernancePolicy.model_json_schema()["examples"][0]],
            "generated_at": EXAMPLE_TS,
        }
    )  # fmt: skip


# ========================================================================== governance runs

ModelKind = Literal["bayesian", "frequentist", "ml"]
"""How a model is estimated, which decides how checks refit it and read its uncertainty:
``bayesian`` (posterior draws: credible intervals, R-hat, prior sensitivity),
``frequentist`` (point estimates with standard errors: confidence intervals, p-values,
optimiser convergence) or ``ml`` (machine-learning estimators: predictions only)."""

_BAYESIAN_INFERENCE: Final = frozenset({"mcmc", "vi", "smc"})
_ML_NAMESPACES: Final = frozenset({"tabular", "sklearn", "xgboost", "ml"})


def model_kind(
    inference: str, *, representation: str | None = None, model_id: str = ""
) -> ModelKind:
    """The :data:`ModelKind` of a model from its capabilities (pure; the runner and the
    control plane agree on it). ``mcmc``/``vi``/``smc`` inference is Bayesian; bootstrap
    ensembles, quantile or conformal-interval models and the ``tabular``/``sklearn``
    namespaces are machine learning; everything else (``map``, ``kalman``, ``point``,
    ``custom``) is frequentist."""
    if inference in _BAYESIAN_INFERENCE:
        return "bayesian"
    if (
        inference == "bootstrap"
        or representation in ("quantiles", "interval")
        or model_id.split(".", 1)[0] in _ML_NAMESPACES
    ):
        return "ml"
    return "frequentist"


_REQUIREMENT_PLAIN: Final[dict[str, str]] = {
    "bayesian": "only Bayesian models have priors to vary",
    "holdout": "the model has no test period (add a data split and refit)",
    "time_series": "the model's data has no time dimension",
    "channels": "the model has no marketing channels",
    "residuals": "the model has no residuals",
}


def check_applies(
    check: GovernanceCheckDef | str,
    *,
    kind: ModelKind,
    family: str,
    has: Iterable[str] = (),
) -> str | None:
    """``None`` when ``check`` applies to a model, else the plain-language reason it is
    skipped. ``has`` lists the model's requirements that hold (``holdout``,
    ``time_series``, ``channels``, ``residuals``); ``bayesian`` comes from ``kind``."""
    c = GOVERNANCE_CHECKS[check] if isinstance(check, str) else check
    if c.families is not None and family not in c.families:
        return f"this check is for {', '.join(c.families)} models, not {family}"
    held = set(has) | ({"bayesian"} if kind == "bayesian" else set())
    for req in c.requires:
        if req not in held:
            return _REQUIREMENT_PLAIN[req]
    return None


class RefitBudget(StrictModel):
    """How much refitting a governance run may do. Null tests, stability, sensitivity and
    recovery refit the model many times: Bayesian refits use at most ``draws``/``tune`` per
    chain on ``chains`` chains (and ``engine``, e.g. ``advi``, when set); frequentist and
    machine-learning models refit as they are. ``max_refits`` caps the whole run; a check
    that would exceed it stops early and says so in its evidence."""

    max_refits: int = Field(120, ge=1, le=1000)
    draws: int = Field(200, ge=10, le=5000)
    tune: int = Field(200, ge=0, le=5000)
    chains: int = Field(2, ge=1, le=8)
    engine: str | None = Field(None, pattern=r"^[a-z][a-z0-9_]{0,31}$")


class GovernanceJob(StrictModel):
    """``RunSpec.spec`` of a ``governance`` run: measure ``checks`` on one model version.

    Inputs: ``bundle`` (+ ``bundle.manifest``), ``dataset`` (the data it was fitted on) and,
    when the fit kept one, ``evaluation`` (+ ``evaluation.manifest``): the train-only
    evaluation bundle benchmarks are scored with. Output: :data:`GOVERNANCE_OUTPUT`.
    """

    validation_id: str | None = Field(None, max_length=64)
    model_ref: str = Field(..., max_length=512, description="model://project/name@vN")
    checks: list[PolicyCheck] = Field(..., min_length=1, max_length=32)
    budget: RefitBudget = Field(default_factory=RefitBudget)
    seed: int = Field(0, ge=0, lt=2**32)

    model_config = dto_examples(
        {
            "validation_id": "val_01jaf3k1m2n3p4q5r6s7t8v9w0",
            "model_ref": "model://growth/geo-mmm@v7",
            "checks": [{"check": "null.permutation_target", "params": {"n_permutations": 20}},
                       {"check": "convergence"}],
            "budget": {"max_refits": 60, "draws": 200, "tune": 200},
        }
    )  # fmt: skip

    @model_validator(mode="after")
    def _unique(self) -> GovernanceJob:
        ids = [c.check for c in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("a governance job lists each check once")
        return self


class GovernanceReport(ResponseModel):
    """:data:`GOVERNANCE_OUTPUT`: every requested check's :class:`CheckResult` (in request
    order; a check that does not apply is ``skipped`` with the reason)."""

    format: Literal["mapi.governance-report/v1"] = GOVERNANCE_REPORT_FORMAT
    model_ref: str = Field(..., max_length=512)
    model_id: str = Field(..., max_length=128, description="The model plugin id")
    family: str = Field(..., max_length=64)
    model_kind: ModelKind
    results: list[CheckResult] = Field(default_factory=list, max_length=32)
    refits: int = Field(0, ge=0)
    warnings_plain: list[str] = Field(default_factory=list, max_length=50)

    model_config = dto_examples(
        {
            "model_ref": "model://growth/geo-mmm@v7",
            "model_id": "mmm.bayesian",
            "family": "mmm",
            "model_kind": "bayesian",
            "results": [CheckResult.model_json_schema()["examples"][0]],
            "refits": 21,
        }
    )


# ========================================================================= default policies


def _pc(
    check: str, severity: CheckSeverity = CheckSeverity.BLOCKING, **params: JsonValue
) -> PolicyCheck:
    return PolicyCheck(check=check, severity=severity, params=dict(params))


_W, _INFO = CheckSeverity.WARNING, CheckSeverity.INFO

DEFAULT_POLICIES: Final[tuple[GovernancePolicyCreate, ...]] = (
    GovernancePolicyCreate(
        name="Marketing mix models (platform default)",
        description="Marketing mix models split results between channels, so they must "
        "beat shuffled results, give fake channels no credit and point effects the right "
        "way. Seeded for every tenant; edit it or add stricter project policies.",
        applies_to=PolicyMatch(families=["mmm"]),
        checks=[
            _pc("data.quality"),
            _pc("convergence"),
            _pc("holdout.accuracy", metric="mape"),
            _pc("null.permutation_target", n_permutations=20),
            _pc("null.noise_regressors", n_noise=3),
            _pc("plausibility.signs"),
            _pc("baseline.beats_naive", _W),
            _pc("null.placebo_time_shift", _W),
            _pc("sensitivity.prior", _W),
            _pc("residuals.whiteness", _INFO),
        ],
        require_approval=False,
        require_validated_for_promotion=False,
    ),
    GovernancePolicyCreate(
        name="Forecasting models (platform default)",
        description="Forecasts must beat simple benchmarks out of sample, stay accurate on "
        "unseen periods and leave no pattern in their errors. Seeded for every tenant.",
        applies_to=PolicyMatch(families=["ts", "forecast", "ts_multivariate", "regime"]),
        checks=[
            _pc("data.quality"),
            _pc("convergence"),
            _pc("baseline.beats_naive"),
            _pc("holdout.accuracy", metric="mape"),
            _pc("residuals.whiteness", _W),
            _pc("null.permutation_target", _W, n_permutations=20),
            _pc("stability.rolling_params", _INFO),
        ],
        require_approval=False,
        require_validated_for_promotion=False,
    ),
    GovernancePolicyCreate(
        name="Regression and machine-learning models (platform default)",
        description="Tabular and choice models must beat shuffled results and stay accurate "
        "on unseen data; fake drivers should get no credit. Seeded for every tenant.",
        applies_to=PolicyMatch(families=["tabular", "choice", "sklearn"]),
        checks=[
            _pc("data.quality"),
            _pc("holdout.accuracy", metric="mape"),
            _pc("null.permutation_target", n_permutations=20),
            _pc("null.noise_regressors", _W, n_noise=3),
            _pc("convergence", _INFO),
        ],
        require_approval=False,
        require_validated_for_promotion=False,
    ),
)
"""The policies seeded (as tenant policies, version 1, ``created_by`` :data:`SYSTEM_ACTOR`)
the first time a tenant uses governance. They define what validation runs per family but
do **not** gate promotion (``require_validated_for_promotion`` is off) or require approval;
an admin turns those on. Thresholds are the catalog defaults (:data:`GOVERNANCE_CHECKS`)."""
