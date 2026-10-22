"""Onboarding progress (v0.18 D19): closed names, the merge never un-completes, caps."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.onboarding import (
    CHECKLIST_STEPS,
    MAX_HIDDEN_SAMPLES,
    MAX_ONBOARDING_BYTES,
    MAX_ONBOARDING_PATCH_BYTES,
    TOUR_IDS,
    OnboardingPatch,
    OnboardingState,
    TourProgress,
    apply_patch,
    checklist_done,
    load_state,
    reset_state,
    state_bytes,
    state_document,
)
from pydantic import ValidationError

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(days=1)


def _patch(**kw: Any) -> OnboardingPatch:
    return OnboardingPatch.model_validate(kw)


def _tour(status: str = "in_progress", step: int = 0) -> dict[str, Any]:
    return {"status": status, "step": step, "updated_at": T0.isoformat()}


# ======================================================================== closed names


def test_names_match_the_plan() -> None:
    assert CHECKLIST_STEPS == (
        "took_tour", "open_sample", "fit_model", "read_results", "try_whatif",
        "make_dashboard", "own_data", "make_your_own",
    )  # fmt: skip
    assert TOUR_IDS == (
        "welcome", "data", "fit", "results", "whatif", "dashboard", "make_your_own",
    )  # fmt: skip


@pytest.mark.parametrize(
    "body",
    [
        {"complete": ["took_the_tour"]},
        {"tours": {"tutorial": _tour()}},
        {"tours": {"welcome": {**_tour(), "status": "paused"}}},
        {"tours": {"welcome": {**_tour(), "step": 51}}},
        {"tours": {"welcome": {**_tour(), "step": -1}}},
        {"tours": {"welcome": {"status": "completed", "step": 1, "updated_at": "2026-10-21"}}},
        {"hide_samples": ["Not A Slug"]},
        {"surprise": True},
    ],
)
def test_unknown_names_and_values_are_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OnboardingPatch.model_validate(body)


def test_patch_lists_are_capped() -> None:
    with pytest.raises(ValidationError):
        _patch(complete=[*CHECKLIST_STEPS, "took_tour"])
    with pytest.raises(ValidationError):
        _patch(hide_samples=[f"s{i}" for i in range(MAX_HIDDEN_SAMPLES + 1)])


def test_the_largest_patch_fits_the_body_limit() -> None:
    body = {
        "complete": list(CHECKLIST_STEPS),
        "tours": {t: _tour("completed", 50) for t in TOUR_IDS},
        "checklist_dismissed": False,
        "tips_off": False,
        "hide_samples": [f"sample_{i:02d}" for i in range(MAX_HIDDEN_SAMPLES)],
        "unhide_samples": [],
        "welcomed": True,
    }
    OnboardingPatch.model_validate(body)
    assert len(json.dumps(body, separators=(",", ":"))) <= MAX_ONBOARDING_PATCH_BYTES


def test_examples_validate() -> None:
    for model in (OnboardingState, OnboardingPatch):
        for ex in model.model_json_schema()["examples"]:
            obj = model.model_validate(ex)
            assert model.model_validate(obj.model_dump(mode="json")) == obj


# ======================================================================== merge


def test_empty_state_is_version_zero() -> None:
    s = OnboardingState()
    assert s.version == 0 and s.checklist == {} and s.tours == {} and not s.tips_off
    assert state_document(s) == {}


def test_complete_adds_steps_and_bumps_the_version() -> None:
    s = apply_patch(OnboardingState(), _patch(complete=["open_sample", "fit_model"]), now=T0)
    assert s.version == 1
    assert s.checklist == {"open_sample": T0, "fit_model": T0}


def test_a_completed_step_never_reverts_or_moves() -> None:
    s = apply_patch(OnboardingState(), _patch(complete=["fit_model"]), now=T0)
    again = apply_patch(s, _patch(complete=["fit_model"]), now=T1)
    assert again is s  # nothing changed: same state, same version
    other = apply_patch(s, _patch(complete=["own_data"], checklist_dismissed=False), now=T1)
    assert other.checklist == {"fit_model": T0, "own_data": T1}
    # no patch field removes a step
    body: dict[str, Any]
    for body in ({}, {"tours": {}}, {"checklist_dismissed": True}, {"tips_off": True}):
        assert "fit_model" in apply_patch(other, _patch(**body), now=T2).checklist


def test_checklist_keeps_its_display_order() -> None:
    s = apply_patch(OnboardingState(), _patch(complete=["make_your_own", "took_tour"]), now=T0)
    assert list(s.checklist) == ["took_tour", "make_your_own"]


def test_tours_merge_per_tour_and_take_the_server_time() -> None:
    s = apply_patch(OnboardingState(), _patch(tours={"welcome": _tour(step=2)}), now=T1)
    assert s.tours["welcome"].step == 2 and s.tours["welcome"].updated_at == T1
    s = apply_patch(s, _patch(tours={"fit": _tour(step=1)}), now=T2)
    assert set(s.tours) == {"welcome", "fit"} and s.tours["welcome"].updated_at == T1
    assert "took_tour" not in s.checklist


def test_a_completed_tour_completes_took_tour() -> None:
    s = apply_patch(OnboardingState(), _patch(tours={"whatif": _tour("completed", 4)}), now=T1)
    assert s.checklist == {"took_tour": T1}
    # a dismissed tour does not
    d = apply_patch(OnboardingState(), _patch(tours={"welcome": _tour("dismissed")}), now=T1)
    assert d.checklist == {}


def test_flags_and_welcome() -> None:
    s = apply_patch(OnboardingState(), _patch(welcomed=True, tips_off=True), now=T0)
    assert s.welcomed_at == T0 and s.tips_off
    s2 = apply_patch(s, _patch(welcomed=True, tips_off=False), now=T1)
    assert s2.welcomed_at == T0 and not s2.tips_off and s2.version == s.version + 1
    assert apply_patch(s2, _patch(welcomed=False), now=T2) is s2  # only a reset clears it
    s3 = apply_patch(s2, _patch(checklist_dismissed=True), now=T2)
    assert s3.checklist_dismissed and checklist_done(s3)


def test_hide_and_unhide_samples() -> None:
    s = apply_patch(OnboardingState(), _patch(hide_samples=["geo_mmm", "geo_mmm"]), now=T0)
    assert s.hidden_samples == ["geo_mmm"]
    s = apply_patch(s, _patch(hide_samples=["other"], unhide_samples=["geo_mmm"]), now=T1)
    assert s.hidden_samples == ["other"]
    # unhide wins over hide in one patch
    s = apply_patch(s, _patch(hide_samples=["x"], unhide_samples=["x"]), now=T1)
    assert s.hidden_samples == ["other"]


def test_hiding_too_many_samples_is_refused() -> None:
    full = OnboardingState(hidden_samples=[f"s{i}" for i in range(MAX_HIDDEN_SAMPLES)])
    with pytest.raises(MapiError) as err:
        apply_patch(full, _patch(hide_samples=["one-more"]), now=T0)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/hide_samples"


def test_now_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone"):
        apply_patch(OnboardingState(), _patch(complete=["own_data"]), now=datetime(2026, 1, 1))


def test_checklist_done_needs_every_step() -> None:
    s = apply_patch(OnboardingState(), _patch(complete=list(CHECKLIST_STEPS[:-1])), now=T0)
    assert not checklist_done(s)
    assert checklist_done(apply_patch(s, _patch(complete=["make_your_own"]), now=T0))


# ======================================================================== reset, storage, cap


def test_reset_starts_again_but_keeps_hidden_samples_and_the_welcome() -> None:
    s = apply_patch(
        OnboardingState(),
        _patch(
            complete=list(CHECKLIST_STEPS),
            tours={"welcome": _tour("completed", 5)},
            checklist_dismissed=True,
            tips_off=True,
            hide_samples=["geo_mmm"],
            welcomed=True,
        ),
        now=T0,
    )
    r = reset_state(s)
    assert r.version == s.version + 1
    assert r.checklist == {} and r.tours == {} and not r.checklist_dismissed and not r.tips_off
    assert r.hidden_samples == ["geo_mmm"] and r.welcomed_at == T0


def test_the_largest_state_fits_the_cap() -> None:
    s = OnboardingState()
    s = apply_patch(
        s,
        _patch(
            complete=list(CHECKLIST_STEPS),
            tours={t: _tour("completed", 50) for t in TOUR_IDS},
            checklist_dismissed=True,
            tips_off=True,
            hide_samples=[f"{'s' * 120}{i}" for i in range(MAX_HIDDEN_SAMPLES)],
            welcomed=True,
        ),
        now=T0,
    )
    assert state_bytes(s) <= MAX_ONBOARDING_BYTES


def test_a_state_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import mapi_spec.onboarding as onboarding

    monkeypatch.setattr(onboarding, "MAX_ONBOARDING_BYTES", 64)
    with pytest.raises(MapiError) as err:
        apply_patch(OnboardingState(), _patch(complete=list(CHECKLIST_STEPS)), now=T0)
    assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_stored_document_round_trips() -> None:
    s = apply_patch(
        OnboardingState(),
        _patch(complete=["own_data"], tours={"data": _tour(step=3)}, welcomed=True),
        now=T0,
    )
    doc = state_document(s)
    assert "version" not in doc
    assert load_state(json.loads(json.dumps(doc)), version=s.version) == s


def test_load_state_is_lenient() -> None:
    doc = {
        "checklist": {"own_data": T0.isoformat(), "retired_step": T0.isoformat()},
        "tours": {"welcome": _tour(step=1), "old_tour": _tour(), "fit": {"status": "??"}},
        "tips_off": "yes",
        "hidden_samples": ["geo_mmm", 3],
        "future_field": {"x": 1},
    }
    s = load_state(doc, version=7)
    assert s.version == 7
    assert list(s.checklist) == ["own_data"] and list(s.tours) == ["welcome"]
    assert not s.tips_off and s.hidden_samples == ["geo_mmm"]
    assert load_state(None, version=2) == OnboardingState(version=2)
    assert load_state({"welcomed_at": "not a date"}, version=1) == OnboardingState(version=1)


def test_tour_progress_requires_aware_time() -> None:
    with pytest.raises(ValidationError):
        TourProgress.model_validate({"status": "completed", "updated_at": "2026-10-21T09:00:00"})
