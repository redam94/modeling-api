"""v0.17 ``mapi_spec.dashboard_metrics``: golden values (checked against the SDK's
``compute_metrics`` when generated) and the edge cases: zero actuals, one class, ties,
probabilities of 0 and 1, refused NaN, too many rows, classes or characters."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.calibration import DistributionSummary
from mapi_spec.dashboard_metrics import (
    LOG_FLOOR,
    MAX_CLASSES,
    MAX_ROWS,
    auc_of,
    classification_metrics_of,
    effect_of,
    error_metrics_of,
    match_label,
    multiclass_metrics_of,
    normalise_label,
)

GOLDEN = Path(__file__).parent / "golden" / "dashboard_metrics.json"
CASES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())["cases"]


def _same(got: dict[str, Any], want: dict[str, Any]) -> None:
    assert set(got) == set(want)
    for k, w in want.items():
        g = got[k]
        if isinstance(w, float):
            assert g == pytest.approx(w, rel=1e-12, abs=1e-12), k
        elif isinstance(w, dict):
            _same(g, w)
        elif isinstance(w, list) and w and isinstance(w[0], dict):
            assert len(g) == len(w), k
            for a, b in zip(g, w, strict=True):
                _same(a, b)
        else:
            assert g == w, k


@pytest.mark.parametrize("case", CASES, ids=[f"{c['kind']}{i}" for i, c in enumerate(CASES)])
def test_golden(case: dict[str, Any]) -> None:
    kind = case["kind"]
    if kind == "error":
        got = error_metrics_of([tuple(p) for p in case["pairs"]], [tuple(b) for b in case["bands"]])
    elif kind == "binary":
        got = classification_metrics_of(case["p"], case["y"], case["positive"], case["threshold"])
    elif kind == "multiclass":
        got = multiclass_metrics_of(case["probs"], case["y"])
    else:
        cf = DistributionSummary.model_validate(case["cf"])
        got = effect_of(case["actual_total"], cf, n_periods=case["n_periods"])
    _same(got.model_dump(mode="json"), case["expected"])


# ===================================================================================== errors


def test_error_metrics_by_hand() -> None:
    acc = error_metrics_of([(12.0, 10.0), (8.0, 10.0), (5.0, 0.0)], [(9, 13), (11, 13), (None, 6)])
    assert acc.n == 3
    assert acc.mae == pytest.approx(3.0)
    assert acc.bias == pytest.approx((2 - 2 + 5) / 3)
    assert acc.rmse == pytest.approx(math.sqrt((4 + 4 + 25) / 3))
    assert acc.mape == pytest.approx(20.0)  # the actual of 0 is left out
    assert acc.wape == pytest.approx(9 / 20 * 100)
    assert acc.smape == pytest.approx((2 / 11 + 2 / 9 + 5 / 2.5) / 3 * 100)
    assert acc.band_hit_rate == pytest.approx(50.0)  # the row without a low bound is out
    assert acc.r2 == pytest.approx(1 - 33 / (2 * (10 - 20 / 3) ** 2 + (20 / 3) ** 2))


def test_error_metrics_with_zero_actuals() -> None:
    acc = error_metrics_of([(1.0, 0.0), (2.0, 0.0)])
    assert acc.mape is None and acc.wape is None and acc.r2 is None
    assert acc.mae == pytest.approx(1.5) and acc.smape == pytest.approx(200.0)
    assert error_metrics_of([(0.0, 0.0)]).smape is None
    assert error_metrics_of([]).n == 0 and error_metrics_of([]).mae is None
    assert error_metrics_of([(1.0, 1.0)], None).band_hit_rate is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_error_metrics_refuse_nan_and_infinity(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        error_metrics_of([(1.0, bad)])
    with pytest.raises(ValueError, match="finite"):
        error_metrics_of([(bad, 1.0)])
    with pytest.raises(ValueError, match="finite"):
        error_metrics_of([(1.0, 1.0)], [(bad, 2.0)])


def test_error_metrics_refuse_bad_shapes() -> None:
    with pytest.raises(ValueError, match="rows"):
        error_metrics_of([(1.0, 1.0)] * (MAX_ROWS + 1))
    with pytest.raises(ValueError, match="one"):
        error_metrics_of([(1.0, 1.0)], [])
    with pytest.raises(ValueError, match="number"):
        error_metrics_of([(True, 1.0)])  # type: ignore[list-item]


# ============================================================================ classification


def test_auc_ties_count_one_half() -> None:
    assert auc_of([0.5, 0.5], [True, False]) == pytest.approx(0.5)
    assert auc_of([0.2, 0.8, 0.8], [False, True, False]) == pytest.approx(0.75)
    assert auc_of([0.1, 0.9], [False, True]) == 1.0
    assert auc_of([0.1, 0.9], [True, True]) is None


def test_binary_by_hand() -> None:
    acc = classification_metrics_of([0.9, 0.2, 0.6, 0.4], ["yes", "no", "no", "Yes"], "yes", 0.5)
    assert acc.n == 4 and acc.positive == "yes" and acc.threshold == 0.5
    assert acc.accuracy == pytest.approx(0.5)  # "Yes" matches "yes" (case folded)
    assert acc.brier == pytest.approx((0.01 + 0.04 + 0.36 + 0.36) / 4)
    assert acc.rate_pred == pytest.approx(0.5) and acc.rate_obs == pytest.approx(0.5)
    assert acc.confusion is not None
    assert acc.confusion.labels == ["no", "yes"]
    assert acc.confusion.counts == [[1, 1], [1, 1]]
    assert sum(b.n for b in acc.bins) == 4
    # the analyst's threshold moves accuracy and the predicted rate only
    high = classification_metrics_of([0.9, 0.2, 0.6, 0.4], ["yes", "no", "no", "yes"], "yes", 0.7)
    assert high.accuracy == pytest.approx(0.75) and high.rate_pred == pytest.approx(0.25)
    assert high.brier == acc.brier


def test_binary_one_class_has_no_auc() -> None:
    acc = classification_metrics_of([0.3, 0.7], ["yes", "yes"], "yes", 0.5)
    assert acc.auc is None and acc.accuracy == pytest.approx(0.5)
    assert acc.confusion is not None and acc.confusion.labels == ["other", "yes"]


def test_log_score_of_certain_predictions_is_floored() -> None:
    right = classification_metrics_of([1.0, 0.0], ["yes", "no"], "yes", 0.5)
    assert right.log_score == 0.0
    wrong = classification_metrics_of([0.0], ["yes"], "yes", 0.5)
    assert wrong.log_score == pytest.approx(math.log(LOG_FLOOR))
    assert math.isfinite(wrong.log_score)


def test_binary_refusals() -> None:
    with pytest.raises(ValueError, match="finite"):
        classification_metrics_of([math.nan], ["yes"], "yes", 0.5)
    with pytest.raises(ValueError, match="probability"):
        classification_metrics_of([1.5], ["yes"], "yes", 0.5)
    with pytest.raises(ValueError, match="threshold"):
        classification_metrics_of([0.5], ["yes"], "yes", 1.0)
    with pytest.raises(ValueError, match="different labels"):
        classification_metrics_of([0.5] * 51, [f"l{i}" for i in range(51)], "l0", 0.5)
    with pytest.raises(ValueError, match="longer"):
        classification_metrics_of([0.5], ["x" * 65], "yes", 0.5)
    with pytest.raises(ValueError, match="same length"):
        classification_metrics_of([0.5], [], "yes", 0.5)
    with pytest.raises(ValueError, match="rows"):
        classification_metrics_of([0.5] * (MAX_ROWS + 1), ["a"] * (MAX_ROWS + 1), "a", 0.5)


def test_multiclass_by_hand() -> None:
    probs = [{"a": 0.7, "b": 0.2, "c": 0.1}, {"a": 0.1, "b": 0.3, "c": 0.6}, {"a": 0.5, "b": 0.5}]
    acc = multiclass_metrics_of(probs, ["a", "b", "B"])
    assert acc.n == 3
    # the tie of the third case goes to the first class (as numpy.argmax)
    assert acc.accuracy == pytest.approx(1 / 3)
    assert acc.brier == pytest.approx(((0.09 + 0.04 + 0.01) + (0.01 + 0.49 + 0.36) + 0.5) / 3)
    assert acc.log_score == pytest.approx((math.log(0.7) + math.log(0.3) + math.log(0.5)) / 3)
    assert acc.confusion is not None and acc.confusion.labels == ["a", "b", "c"]
    assert acc.confusion.counts == [[1, 0, 0], [1, 0, 1], [0, 0, 0]]


def test_multiclass_skips_empty_cases_and_misses_unknown_labels() -> None:
    acc = multiclass_metrics_of([{"a": 0.0, "b": 0.0}, {"a": 1.0}, {"a": 0.5, "b": 0.5}],
                                ["a", "", "zzz"])  # fmt: skip
    assert acc.n == 1  # the all-zero case does not count; the empty label is skipped
    assert acc.accuracy == 0.0 and acc.log_score == pytest.approx(math.log(LOG_FLOOR))
    assert acc.auc is None


def test_multiclass_refusals() -> None:
    with pytest.raises(ValueError, match="classes"):
        multiclass_metrics_of([{f"c{i}": 0.01 for i in range(MAX_CLASSES + 1)}], ["c0"])
    with pytest.raises(ValueError, match="finite"):
        multiclass_metrics_of([{"a": math.nan}], ["a"])
    with pytest.raises(ValueError, match="longer"):
        multiclass_metrics_of([{"a" * 65: 1.0}], ["a"])
    assert multiclass_metrics_of([], []).n == 0


def test_multiclass_big_confusion_is_left_out() -> None:
    probs = [{f"c{i}": 1.0 / 21 for i in range(21)}]
    assert multiclass_metrics_of(probs, ["c0"]).confusion is None


def test_labels() -> None:
    assert normalise_label(" Coke Zero ") == "coke_zero"
    assert normalise_label("1.0") == "1" and normalise_label("3") == "3"
    assert normalise_label("Straße") == normalise_label("STRASSE")  # casefold
    assert normalise_label("Café") == normalise_label("Café")  # NFC
    assert match_label(1.0, ["0", "1"]) == 1
    assert match_label("coke zero", ["Coke Zero", "coke_zero"]) == 0
    assert match_label("coke_zero", ["Coke Zero", "coke_zero"]) == 1  # exact text wins
    assert match_label("x", ["a"]) == -1


# ===================================================================================== effect


def test_effect_interval_orientation() -> None:
    cf = DistributionSummary(mean=100.0, sd=10.0, quantiles={"0.05": 84.0, "0.95": 117.0})
    eff = effect_of(130.0, cf, n_periods=4)
    assert eff.total == pytest.approx(30.0)
    assert eff.low == pytest.approx(130 - 117) and eff.high == pytest.approx(130 - 84)
    assert eff.low <= eff.total <= eff.high
    assert eff.relative == pytest.approx(30.0) and eff.n_periods == 4


def test_effect_without_quantiles_or_mean() -> None:
    eff = effect_of(5.0, DistributionSummary(mean=0.0, sd=1.0))
    assert eff.total == 5.0 and eff.low is None and eff.high is None and eff.relative is None
    outer = effect_of(10.0, DistributionSummary(mean=8.0, sd=1.0,
                                               quantiles={"0.025": 6.0, "0.975": 9.5}))  # fmt: skip
    assert (outer.low, outer.high) == (pytest.approx(0.5), pytest.approx(4.0))
    with pytest.raises(ValueError, match="finite"):
        effect_of(math.nan, DistributionSummary(mean=0.0, sd=1.0))
