"""M3 scorecard: the check catalog, item evaluation, roll-up, and the promotion gate with
its override rules (pass -> allowed, warn -> needs an audited admin override, fail ->
blocked)."""

from __future__ import annotations

import itertools

import pytest
from mapi_spec import api
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.scorecard import (
    SCORECARD_CHECKS,
    CheckDirection,
    CheckThresholds,
    GateOutcome,
    Scorecard,
    ScorecardCheck,
    ScorecardItem,
    authorize_promotion,
    evaluate_check,
    promotion_gate,
    rollup,
    worst_status,
)

pytestmark = pytest.mark.unit


def test_the_api_module_re_exports_the_moved_scorecard_dtos() -> None:
    assert api.Scorecard is Scorecard and api.ScorecardItem is ScorecardItem
    legacy = ScorecardItem.model_validate(ScorecardItem.model_json_schema()["examples"][0])
    assert legacy.check is None and legacy.category is None  # v0.3 items still parse


def test_catalog_ids_codes_and_anchors_are_unique_and_well_formed() -> None:
    checks = list(SCORECARD_CHECKS.values())
    assert {c.category for c in checks} == {"convergence", "holdout", "plausibility", "data"}
    for field in ("id", "code", "docs_anchor"):
        values = [getattr(c, field) for c in checks]
        assert len(values) == len(set(values)), field
    for c in checks:
        assert c.id.split(".")[0] == c.category
        # every check renders a valid item at its thresholds
        for v in (c.warn, c.fail):
            ScorecardItem.model_validate(evaluate_check(c, (c.target or 0) + v).model_dump())


@pytest.mark.parametrize(
    ("check", "value", "status"),
    [
        ("convergence.rhat", 1.004, "pass"),
        ("convergence.rhat", 1.01, "pass"),  # at the threshold: still pass
        ("convergence.rhat", 1.02, "warn"),
        ("convergence.rhat", 1.2, "fail"),
        ("convergence.ess", 1000, "pass"),
        ("convergence.ess", 400, "pass"),
        ("convergence.ess", 250, "warn"),
        ("convergence.ess", 50, "fail"),
        ("convergence.divergences", 0.0, "pass"),
        ("convergence.divergences", 0.001, "warn"),
        ("convergence.divergences", 0.05, "fail"),
        ("holdout.coverage_90", 0.9, "pass"),
        ("holdout.coverage_90", 0.82, "pass"),
        ("holdout.coverage_90", 0.75, "warn"),
        ("holdout.coverage_90", 0.99, "pass"),
        ("holdout.coverage_90", 0.5, "fail"),
        ("holdout.hit_rate", 0.15, "pass"),
        ("holdout.hit_rate", 0.02, "pass"),
        ("holdout.hit_rate", 0.0, "warn"),
        ("holdout.hit_rate", -0.08, "warn"),
        ("holdout.hit_rate", -0.2, "fail"),
        ("plausibility.baseline_share", 0.5, "pass"),
        ("plausibility.baseline_share", 0.95, "warn"),
        ("plausibility.baseline_share", 0.01, "fail"),
    ],
)
def test_directions_and_bands(check: str, value: float, status: str) -> None:
    item = evaluate_check(check, value)
    assert item.status == status
    assert item.check == check and item.category == SCORECARD_CHECKS[check].category
    assert (item.suggested_action is None) == (status == "pass")
    assert item.docs_anchor == "check-" + check.replace(".", "-").replace("_", "-")


def test_messages_are_plain_and_formatted() -> None:
    item = evaluate_check("holdout.mape", 0.14)
    assert item.message_plain == "Forecasts were off by 14% on the held-out periods."
    assert item.threshold == 0.10 and item.metric == "mape"
    assert "4" in evaluate_check("convergence.ess", 3.6).message_plain
    assert "1.2" in evaluate_check("convergence.rhat", 1.2).message_plain
    hit = evaluate_check("holdout.hit_rate", 0.12)
    assert hit.message_plain == (
        "On held-out trips the model's top pick beat always guessing the most popular "
        "product by 12%."
    )
    assert hit.metric == "accuracy_lift" and hit.category == "holdout"
    assert "-15%" in evaluate_check("holdout.hit_rate", -0.15).message_plain


def test_template_thresholds_override_the_defaults() -> None:
    tight = CheckThresholds(warn=0.05, fail=0.1)
    assert evaluate_check("holdout.mape", 0.07).status == "pass"
    assert evaluate_check("holdout.mape", 0.07, thresholds=tight).status == "warn"
    assert evaluate_check("holdout.mape", 0.2, thresholds=tight).threshold == 0.1


def test_bad_values_and_definitions_are_refused() -> None:
    with pytest.raises(ValueError):
        evaluate_check("holdout.mape", float("nan"))
    with pytest.raises(KeyError):
        evaluate_check("holdout.nope", 1.0)
    args = ("x.y", "data", "X", "t", "m")
    texts = ("a", "b", "c", "d")
    with pytest.raises(ValueError, match="strict"):
        ScorecardCheck(*args, CheckDirection.MAX, 0.5, 0.1, *texts)
    with pytest.raises(ValueError, match="strict"):
        ScorecardCheck(*args, CheckDirection.MIN, 10, 20, *texts)
    with pytest.raises(ValueError, match="target"):
        ScorecardCheck(*args, CheckDirection.BAND, 0.1, 0.2, *texts)


def test_rollup_takes_the_worst_item() -> None:
    items = [evaluate_check("convergence.rhat", 1.0), evaluate_check("holdout.mape", 0.14)]
    assert rollup(items).overall == "warn"
    assert rollup([*items, evaluate_check("data.missing", 0.5)]).overall == "fail"
    assert rollup([items[0]]).overall == "pass"
    empty = rollup([], {"holdout.mape": "no holdout split"})
    assert empty.overall == "warn" and empty.skipped == {"holdout.mape": "no holdout split"}
    for combo in itertools.product(["pass", "warn", "fail"], repeat=3):
        expected = "fail" if "fail" in combo else "warn" if "warn" in combo else "pass"
        assert worst_status(combo) == expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- the gate


def _card(*statuses: str) -> Scorecard:
    values = {"pass": 0.05, "warn": 0.14, "fail": 0.5}
    return rollup([evaluate_check("holdout.mape", values[s]) for s in statuses])


@pytest.mark.parametrize(
    ("card", "kw", "outcome"),
    [
        (_card("pass"), {}, GateOutcome.ALLOWED),
        (_card("pass", "warn"), {}, GateOutcome.NEEDS_OVERRIDE),
        (_card("warn", "fail"), {}, GateOutcome.BLOCKED),
        (None, {}, GateOutcome.NEEDS_OVERRIDE),
        (_card("pass"), {"partial": True}, GateOutcome.BLOCKED),
        (_card("pass"), {"fit_mode": "train_only"}, GateOutcome.BLOCKED),
        (_card("pass"), {"fit_mode": "train_only", "allow_train_only": True}, GateOutcome.ALLOWED),
        (_card("pass"), {"schema_breaks": ["dim 'geo' is gone"]}, GateOutcome.BLOCKED),
        (
            _card("pass"),
            {"schema_breaks": ["x"], "schema_change": "warn"},
            GateOutcome.NEEDS_OVERRIDE,
        ),
        (_card("pass"), {"schema_breaks": ["x"], "schema_change": "allow"}, GateOutcome.ALLOWED),
        (Scorecard(overall="warn"), {}, GateOutcome.NEEDS_OVERRIDE),
        (Scorecard(overall="fail"), {}, GateOutcome.BLOCKED),
    ],
)
def test_gate_outcomes(card: Scorecard | None, kw: dict[str, object], outcome: GateOutcome) -> None:
    decision = promotion_gate(card, **kw)  # type: ignore[arg-type]
    assert decision.outcome is outcome
    assert (decision.reasons == []) == (outcome is GateOutcome.ALLOWED)
    assert decision.scorecard_overall == (card.overall if card else None)


def test_gate_lists_failing_checks_and_plain_reasons() -> None:
    d = promotion_gate(_card("pass", "warn"))
    assert d.failing_checks == ["holdout.mape"]
    [reason] = d.reasons
    assert reason.code == "SCORECARD_WARN" and reason.check == "holdout.mape"
    assert reason.message_plain.startswith("Holdout error:")


def test_override_rules() -> None:
    allowed = promotion_gate(_card("pass"))
    assert authorize_promotion(allowed) is False
    assert authorize_promotion(allowed, override=True, reason="x", can_override=True) is False
    warn = promotion_gate(_card("warn"))
    cases = [
        ({}, ErrorCode.OVERRIDE_REQUIRED),
        ({"override": True, "reason": "ok"}, ErrorCode.FORBIDDEN),
        ({"override": True, "reason": "  ", "can_override": True}, ErrorCode.SCHEMA_INVALID),
        ({"override": True, "can_override": True}, ErrorCode.SCHEMA_INVALID),
    ]
    for kw, code in cases:
        with pytest.raises(MapiError) as exc:
            authorize_promotion(warn, **kw)  # type: ignore[arg-type]
        assert exc.value.code is code, kw
    assert authorize_promotion(warn, override=True, reason="CMO sign-off", can_override=True)
    blocked = promotion_gate(_card("fail"))
    with pytest.raises(MapiError) as exc:
        authorize_promotion(blocked, override=True, reason="please", can_override=True)
    assert exc.value.code is ErrorCode.PROMOTION_BLOCKED
    assert "Holdout error" in exc.value.detail


def test_gate_needs_an_override_for_an_evaluate_then_refit_fit_never_evaluated() -> None:
    """Q13: an ``evaluate_then_refit`` version must have been scored on a holdout. A spec
    without ``data.split`` fits on all the data and every holdout check is skipped."""
    no_split = rollup(
        [evaluate_check("convergence.rhat", 1.001)],
        {"holdout.mape": "no split", "holdout.coverage_90": "no split"},
    )
    assert no_split.overall == "pass"
    d = promotion_gate(no_split, fit_mode="evaluate_then_refit")
    assert d.outcome is GateOutcome.NEEDS_OVERRIDE
    [reason] = d.reasons
    assert reason.code == "NOT_EVALUATED" and reason.check == "holdout.mape"
    assert "no split" in reason.message_plain
    assert "holdout.mape" in d.failing_checks
    # an exploration fit allowed by the endpoint's policy needs no holdout
    lenient = promotion_gate(no_split, fit_mode="train_only", allow_train_only=True)
    assert lenient.outcome is GateOutcome.ALLOWED
    # any measured holdout check is enough
    scored = rollup([evaluate_check("holdout.hit_rate", 0.1)], {"holdout.mape": "not choice"})
    assert promotion_gate(scored).outcome is GateOutcome.ALLOWED


def test_gate_needs_an_override_for_a_required_check_that_was_skipped() -> None:
    card = rollup(
        [evaluate_check("holdout.mape", 0.05)],
        {"plausibility.negative_roi": "the model has no response curves"},
    )
    assert promotion_gate(card).outcome is GateOutcome.ALLOWED
    d = promotion_gate(
        card, required_checks=["holdout.mape", "plausibility.negative_roi", "data.history"]
    )
    assert d.outcome is GateOutcome.NEEDS_OVERRIDE
    assert [(r.code, r.check) for r in d.reasons] == [
        ("NOT_EVALUATED", "plausibility.negative_roi"),
        ("NOT_EVALUATED", "data.history"),
    ]
    assert "no response curves" in d.reasons[0].message_plain
    assert d.failing_checks == ["plausibility.negative_roi", "data.history"]
