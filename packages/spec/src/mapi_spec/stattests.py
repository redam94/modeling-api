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
           "The series has a unit root (it wanders without returning to a stable level).",
           "stationary", "unit_root",
           "The series returns to a stable level: no differencing needed.",
           "The series wanders: consider differencing it."),
        _t("test.kpss", "KPSS",
           "The series is stationary (it varies around a stable level or trend).",
           "unit_root", "stationary",
           "The series wanders: consider differencing it.",
           "The series varies around a stable level: no differencing needed."),
        _t("test.pp", "Phillips-Perron",
           "The series has a unit root (it wanders without returning to a stable level).",
           "stationary", "unit_root",
           "The series returns to a stable level: no differencing needed.",
           "The series wanders: consider differencing it."),
        _t("test.zivot_andrews", "Zivot-Andrews",
           "The series has a unit root, with no one-off break.",
           "stationary_with_break", "unit_root",
           "The series is stable apart from one break in level or trend.",
           "The series wanders even allowing for one break: consider differencing it."),
        _t("test.johansen", "Johansen cointegration",
           "The series are not tied together in the long run.",
           "cointegrated", "not_cointegrated",
           "The series share at least one long-run relationship.",
           "No long-run relationship was found between the series.", p_value=False),
        _t("test.engle_granger", "Engle-Granger cointegration",
           "The two series are not tied together in the long run.",
           "cointegrated", "not_cointegrated",
           "The series share a long-run relationship.",
           "No long-run relationship was found between the series."),
        _t("test.ljung_box", "Ljung-Box",
           "The values are not related to their own past (no leftover pattern).",
           "autocorrelated", "white_noise",
           "A pattern over time is left: the model misses some structure.",
           "No pattern over time is left."),
        _t("test.jarque_bera", "Jarque-Bera",
           "The values follow a bell curve (normal distribution).",
           "not_normal", "normal",
           "The values do not follow a bell curve (skewed or heavy tails).",
           "The values are consistent with a bell curve."),
        _t("test.breusch_pagan", "Breusch-Pagan",
           "The spread of the errors is the same everywhere.",
           "unequal_spread", "equal_spread",
           "Errors are larger in some places than others: ranges may be off.",
           "The errors have a steady spread."),
        _t("test.white", "White",
           "The spread of the errors is the same everywhere.",
           "unequal_spread", "equal_spread",
           "Errors are larger in some places than others: ranges may be off.",
           "The errors have a steady spread."),
        _t("test.arch_lm", "ARCH-LM",
           "Calm and volatile periods do not cluster.",
           "volatility_clusters", "no_volatility_clusters",
           "Volatile periods come in clusters: ranges should widen after shocks.",
           "Volatility does not cluster."),
        _t("test.granger", "Granger causality",
           "Past values of the driver do not help predict the result.",
           "predictive", "not_predictive",
           "Past values of the driver help predict the result.",
           "Past values of the driver add nothing to predicting the result."),
        _t("test.acf_pacf", "Autocorrelation significance",
           "No lag is related to the present beyond chance.",
           "autocorrelated", "white_noise",
           "Some past periods are related to the present (see the suggested orders).",
           "No past period is related to the present beyond chance."),
        _t("test.seasonal_strength", "Seasonal strength",
           "There is no meaningful repeating pattern.",
           "seasonal", "not_seasonal",
           "There is a clear repeating pattern: model the season.",
           "No clear repeating pattern was found.", p_value=False),
        _t("test.vif", "Variance inflation (VIF)",
           "The drivers are not too similar to each other.",
           "collinear", "not_collinear",
           "Some drivers move almost together: their separate effects are hard to tell apart.",
           "The drivers are distinct enough to tell their effects apart.", p_value=False),
        _t("test.cusum", "CUSUM stability",
           "The relationship stays the same over time.",
           "unstable", "stable",
           "The relationship changed at some point: consider a break or shorter history.",
           "The relationship looks stable over time."),
        _t("test.chow", "Chow break test",
           "There is no break at the chosen date.",
           "break", "no_break",
           "The relationship changed at the chosen date.",
           "No change was found at the chosen date."),
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
            "verdict_plain": "The series wanders: consider differencing it.",
            "null_plain": "The series has a unit root (it wanders without returning to a "
            "stable level).",
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
