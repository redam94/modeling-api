"""Statistical test results: the ``test_result`` payload of workflow ``test`` nodes and the
rows of the Data Explorer's stationarity table (M3.5).

The runner computes tests with statsmodels/scipy in the sandbox; this module fixes what a
result looks like and how a p-value becomes a decision, so the control plane (branch
conditions), the web app and the docs agree. Every test states its null hypothesis and
both possible verdicts in plain words (:data:`TEST_DEFINITIONS`); :func:`decide` applies
the significance level, including for tests whose null is the "good" case (KPSS: the
series is stationary).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import Field, JsonValue, model_validator

from mapi_spec._dto import ResponseModel, dto_examples

__all__ = [
    "TEST_DEFINITIONS",
    "SeriesTestResult",
    "StatTestResult",
    "TestDefinition",
    "decide",
    "make_test_result",
]


@dataclass(frozen=True, slots=True)
class TestDefinition:
    """What a test checks and what each outcome means, in plain words.

    ``reject_decision``/``keep_decision`` are stable machine labels for the decision when
    the null is rejected or kept (branch conditions compare ``nodes.<id>.decision`` with
    them); ``*_plain`` are the sentences the UI shows.
    """

    id: str
    name: str
    null_plain: str
    reject_decision: str
    keep_decision: str
    reject_plain: str
    keep_plain: str
    p_value: bool = True
    """False for tests decided on critical values only (Johansen's rank, CUSUM bounds)."""

    __test__ = False  # not a pytest class


def _t(
    id: str,
    name: str,
    null_plain: str,
    reject_decision: str,
    keep_decision: str,
    reject_plain: str,
    keep_plain: str,
    *,
    p_value: bool = True,
) -> TestDefinition:
    return TestDefinition(
        id, name, null_plain, reject_decision, keep_decision, reject_plain, keep_plain, p_value
    )


TEST_DEFINITIONS: Final[dict[str, TestDefinition]] = {
    d.id: d
    for d in (
        _t("test.adf", "Augmented Dickey-Fuller",
           "The series has a unit root (it moves away from a stable level and does not return).",
           "stationary", "unit_root",
           "The series returns to a stable level. You do not need its differences.",
           "The series does not return to a stable level. Use the differences of the series."),
        _t("test.kpss", "KPSS",
           "The series is stationary (it changes around a stable level or trend).",
           "unit_root", "stationary",
           "The series does not return to a stable level. Use the differences of the series.",
           "The series changes around a stable level. You do not need its differences."),
        _t("test.pp", "Phillips-Perron",
           "The series has a unit root (it moves away from a stable level and does not return).",
           "stationary", "unit_root",
           "The series returns to a stable level. You do not need its differences.",
           "The series does not return to a stable level. Use the differences of the series."),
        _t("test.zivot_andrews", "Zivot-Andrews",
           "The series has a unit root and no one-time break.",
           "stationary_with_break", "unit_root",
           "The series is stable, but it has one break in level or trend.",
           "The series does not return to a stable level, also with one break. Use the "
           "differences of the series."),
        _t("test.johansen", "Johansen cointegration",
           "The series have no long-run relationship.",
           "cointegrated", "not_cointegrated",
           "The series have one or more long-run relationships.",
           "The test found no long-run relationship between the series.", p_value=False),
        _t("test.engle_granger", "Engle-Granger cointegration",
           "The two series have no long-run relationship.",
           "cointegrated", "not_cointegrated",
           "The series have a long-run relationship.",
           "The test found no long-run relationship between the series."),
        _t("test.ljung_box", "Ljung-Box",
           "The values have no relation to their own past values (no pattern is left).",
           "autocorrelated", "white_noise",
           "A pattern over time is left. The model does not find all of the structure.",
           "No pattern over time is left."),
        _t("test.jarque_bera", "Jarque-Bera",
           "The values have a bell curve (a normal distribution).",
           "not_normal", "normal",
           "The values do not have a bell curve (they are skewed or have heavy tails).",
           "The values agree with a bell curve."),
        _t("test.breusch_pagan", "Breusch-Pagan",
           "The spread of the errors is the same at all points.",
           "unequal_spread", "equal_spread",
           "The errors are larger at some points than at others. The ranges can be wrong.",
           "The errors have a constant spread."),
        _t("test.white", "White",
           "The spread of the errors is the same at all points.",
           "unequal_spread", "equal_spread",
           "The errors are larger at some points than at others. The ranges can be wrong.",
           "The errors have a constant spread."),
        _t("test.arch_lm", "ARCH-LM",
           "Calm periods and volatile periods do not occur in groups.",
           "volatility_clusters", "no_volatility_clusters",
           "Volatile periods occur in groups. The ranges must be wider after shocks.",
           "The volatility does not occur in groups."),
        _t("test.granger", "Granger causality",
           "Past values of the driver do not help to predict the result.",
           "predictive", "not_predictive",
           "Past values of the driver help to predict the result.",
           "Past values of the driver do not help to predict the result."),
        _t("test.acf_pacf", "Autocorrelation significance",
           "No lag has a relation to the present that is more than chance.",
           "autocorrelated", "white_noise",
           "Some past periods have a relation to the present (see the suggested orders).",
           "No past period has a relation to the present that is more than chance."),
        _t("test.seasonal_strength", "Seasonal strength",
           "There is no important pattern that repeats.",
           "seasonal", "not_seasonal",
           "There is a clear pattern that repeats. Add the season to the model.",
           "The test found no clear pattern that repeats.", p_value=False),
        _t("test.vif", "Variance inflation (VIF)",
           "The drivers are not too similar to each other.",
           "collinear", "not_collinear",
           "Some drivers move almost together. It is difficult to separate their effects.",
           "The drivers are sufficiently different to separate their effects.",
           p_value=False),
        _t("test.cusum", "CUSUM stability",
           "The relationship stays the same over time.",
           "unstable", "stable",
           "The relationship changed at one point. Add a break, or use a shorter history.",
           "The relationship is stable over time."),
        _t("test.chow", "Chow break test",
           "There is no break at the selected date.",
           "break", "no_break",
           "The relationship changed at the selected date.",
           "The test found no change at the selected date."),
    )
}  # fmt: skip
"""The statistical tests workflow ``test`` nodes run (ids match the node implementations)."""


def decide(test_id: str, p_value: float, alpha: float) -> tuple[bool, str, str]:
    """``(reject, decision, verdict_plain)`` of a p-value at significance ``alpha``.

    ``reject`` is ``p_value < alpha``; the decision label depends on what the test's null
    means (see :data:`TEST_DEFINITIONS`). Raises ``KeyError`` for unknown tests and
    ``ValueError`` for a p-value or alpha outside ``[0, 1]``.
    """
    d = TEST_DEFINITIONS[test_id]
    if not (0.0 <= p_value <= 1.0) or math.isnan(p_value):
        raise ValueError("p_value must be within [0, 1]")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    reject = p_value < alpha
    if reject:
        return True, d.reject_decision, d.reject_plain
    return False, d.keep_decision, d.keep_plain


class SeriesTestResult(ResponseModel):
    """The test on one series of a panel (``coords`` names it, e.g. ``{"geo": "DE"}``)."""

    coords: dict[str, str | int | float | bool] = Field(default_factory=dict)
    statistic: float | None = None
    p_value: float | None = Field(None, ge=0, le=1)
    reject: bool | None = None
    decision: str | None = Field(None, max_length=64)
    details: dict[str, JsonValue] = Field(default_factory=dict)


class StatTestResult(ResponseModel):
    """The ``test_result`` of one test node (and one stationarity-table row).

    For panels the test runs per series: ``per_series`` holds each one and the headline
    fields summarise them (``reject`` when ``reject_share >= 0.5``; ``p_value`` is the
    median). Branch conditions read these fields, e.g. ``nodes.adf.reject``.
    """

    test: str = Field(..., pattern=r"^test\.[a-z0-9_]+$")
    var: str | None = Field(None, max_length=256)
    statistic: float | None = None
    p_value: float | None = Field(None, ge=0, le=1)
    alpha: float = Field(0.05, gt=0, lt=1)
    reject: bool
    decision: str = Field(..., max_length=64)
    verdict_plain: str = Field(..., max_length=500)
    null_plain: str = Field(..., max_length=500)
    critical_values: dict[str, float] = Field(default_factory=dict, max_length=16)
    n_obs: int | None = Field(None, ge=0)
    lags: int | None = Field(None, ge=0)
    details: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Test-specific fields (rank, break_at, suggested_p, max_vif, ...)",
    )
    per_series: list[SeriesTestResult] = Field(default_factory=list, max_length=10_000)
    reject_share: float | None = Field(None, ge=0, le=1)
    status: Literal["ok", "skipped", "error"] = "ok"
    message_plain: str | None = Field(None, max_length=500, description="Why skipped/error")

    model_config = dto_examples(
        {
            "test": "test.adf",
            "var": "y",
            "statistic": -1.42,
            "p_value": 0.57,
            "alpha": 0.05,
            "reject": False,
            "decision": "unit_root",
            "verdict_plain": "The series does not return to a stable level. Use the "
            "differences of the series.",
            "null_plain": "The series has a unit root (it moves away from a stable level and "
            "does not return).",
            "critical_values": {"1%": -3.46, "5%": -2.87, "10%": -2.57},
            "n_obs": 156,
            "lags": 3,
        }
    )

    @model_validator(mode="after")
    def _known(self) -> StatTestResult:
        if self.test in TEST_DEFINITIONS and self.status == "ok":
            d = TEST_DEFINITIONS[self.test]
            if self.decision not in (d.reject_decision, d.keep_decision):
                raise ValueError(f"{self.test}: unknown decision {self.decision!r}")
            if (self.decision == d.reject_decision) != self.reject:
                raise ValueError("decision does not match reject")
        return self


def make_test_result(
    test_id: str,
    *,
    statistic: float | None,
    p_value: float,
    alpha: float = 0.05,
    var: str | None = None,
    **extra: JsonValue,
) -> StatTestResult:
    """A :class:`StatTestResult` with the decision and texts filled from ``p_value``."""
    reject, decision, verdict = decide(test_id, p_value, alpha)
    d = TEST_DEFINITIONS[test_id]
    return StatTestResult.model_validate(
        {
            "test": test_id,
            "var": var,
            "statistic": statistic,
            "p_value": p_value,
            "alpha": alpha,
            "reject": reject,
            "decision": decision,
            "verdict_plain": verdict,
            "null_plain": d.null_plain,
            **extra,
        }
    )
