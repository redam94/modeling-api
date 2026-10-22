"""Onboarding progress: the first-run checklist and the guided tours (v0.18 D19).

Progress is per user and per tenant, kept server-side (``onboarding_progress``) so it follows
the user from browser to browser. It is UX state, not a security control: it gates nothing.

* :class:`OnboardingState` is what ``GET /v1/tenants/{t}/me/onboarding`` returns; its
  ``version`` is the ETag and the ``If-Match`` precondition of a PATCH.
* :class:`OnboardingPatch` is the PATCH body. :func:`apply_patch` merges it (pure, no I/O):
  a completed checklist step **never** goes back to not done, and only :func:`reset_state`
  (``POST …:reset``) starts the checklist and the tours again.
* The step, tour and status names are closed sets (:data:`CHECKLIST_STEPS`,
  :data:`TOUR_IDS`, :data:`TOUR_STATUSES`): a request with any other name is refused.
  :func:`load_state` reads a stored document leniently, so a step that a later release
  removes does not break the row.

Everything here is plain pydantic: no I/O, no numpy.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal, get_args

from pydantic import AwareDatetime, Field, JsonValue, ValidationError

from mapi_spec._dto import EXAMPLE_TS, RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Slug, StrictModel
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "CHECKLIST_STEPS",
    "MAX_HIDDEN_SAMPLES",
    "MAX_ONBOARDING_BYTES",
    "MAX_ONBOARDING_PATCH_BYTES",
    "MAX_TOUR_STEP",
    "TOUR_IDS",
    "TOUR_STATUSES",
    "ChecklistStep",
    "OnboardingPatch",
    "OnboardingState",
    "TourId",
    "TourProgress",
    "TourStatus",
    "apply_patch",
    "checklist_done",
    "load_state",
    "reset_state",
    "state_bytes",
    "state_document",
]

ChecklistStep = Literal[
    "took_tour",
    "open_sample",
    "fit_model",
    "read_results",
    "try_whatif",
    "make_dashboard",
    "own_data",
    "make_your_own",
]
"""The first-run checklist, in the order the Start-here hub shows it."""

TourId = Literal["welcome", "data", "fit", "results", "whatif", "dashboard", "make_your_own"]
"""The guided tours (the web holds their steps and anchors)."""

TourStatus = Literal["in_progress", "completed", "dismissed"]

CHECKLIST_STEPS: Final[tuple[ChecklistStep, ...]] = get_args(ChecklistStep)
TOUR_IDS: Final[tuple[TourId, ...]] = get_args(TourId)
TOUR_STATUSES: Final[tuple[TourStatus, ...]] = get_args(TourStatus)

MAX_ONBOARDING_BYTES: Final = 8192
"""Cap of a stored state (compact JSON; the table has the same CHECK)."""
MAX_ONBOARDING_PATCH_BYTES: Final = 2048
"""Cap of a PATCH body."""
MAX_TOUR_STEP: Final = 50
MAX_HIDDEN_SAMPLES: Final = 8

_TOUR_DONE_STEP: Final[ChecklistStep] = "took_tour"
"""Completing any tour completes this checklist step."""


class TourProgress(StrictModel):
    """Where the user is in one tour. ``updated_at`` is set by the server on merge."""

    status: TourStatus
    step: int = Field(0, ge=0, le=MAX_TOUR_STEP, description="Index of the current step")
    updated_at: AwareDatetime


class OnboardingState(ResponseModel):
    """The checklist and tour progress of the calling user in one tenant."""

    version: int = Field(0, ge=0, description="Bumped on every change; the ETag of the state")
    checklist: dict[ChecklistStep, AwareDatetime] = Field(
        default_factory=dict, description="Completed steps and when they were completed"
    )
    tours: dict[TourId, TourProgress] = Field(default_factory=dict)
    checklist_dismissed: bool = False
    tips_off: bool = Field(False, description="The user turned the welcome dialog and tips off")
    hidden_samples: list[Slug] = Field(
        default_factory=list,
        max_length=MAX_HIDDEN_SAMPLES,
        description="Sample projects the user hid from the Start-here hub",
    )
    welcomed_at: AwareDatetime | None = Field(
        None, description="When the user first saw (and closed) the welcome dialog"
    )

    model_config = dto_examples(
        {"version": 0},
        {
            "version": 4,
            "checklist": {"took_tour": EXAMPLE_TS, "open_sample": EXAMPLE_TS},
            "tours": {"welcome": {"status": "completed", "step": 5, "updated_at": EXAMPLE_TS}},
            "checklist_dismissed": False,
            "tips_off": False,
            "hidden_samples": [],
            "welcomed_at": EXAMPLE_TS,
        },
    )


class OnboardingPatch(RequestModel):
    """``PATCH /v1/tenants/{t}/me/onboarding`` (at most 2 KiB). Every field is optional;
    an absent field leaves the state as it is."""

    complete: list[ChecklistStep] = Field(
        default_factory=list, max_length=len(CHECKLIST_STEPS), description="Steps now done"
    )
    tours: dict[TourId, TourProgress] | None = Field(
        None, description="Tours to set (other tours are kept)"
    )
    checklist_dismissed: bool | None = None
    tips_off: bool | None = None
    hide_samples: list[Slug] = Field(default_factory=list, max_length=MAX_HIDDEN_SAMPLES)
    unhide_samples: list[Slug] = Field(default_factory=list, max_length=MAX_HIDDEN_SAMPLES)
    welcomed: bool | None = Field(
        None, description="true records the first welcome; only a reset clears it"
    )

    model_config = dto_examples(
        {"complete": ["took_tour"]},
        {
            "tours": {"fit": {"status": "in_progress", "step": 2, "updated_at": EXAMPLE_TS}},
            "welcomed": True,
        },
        {"checklist_dismissed": True, "hide_samples": ["geo_mmm"]},
    )


def state_document(state: OnboardingState) -> dict[str, JsonValue]:
    """The JSON stored in ``onboarding_progress.state`` (``version`` has its own column;
    fields at their defaults are left out)."""
    doc: dict[str, JsonValue] = state.model_dump(
        mode="json", exclude={"version"}, exclude_defaults=True
    )
    return doc


def state_bytes(state: OnboardingState) -> int:
    """Size of the stored document in bytes (compact JSON), checked against
    :data:`MAX_ONBOARDING_BYTES`."""
    text = json.dumps(state_document(state), separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8"))


def load_state(doc: Mapping[str, Any] | None, *, version: int = 0) -> OnboardingState:
    """Read a stored state leniently: steps, tours and fields this release does not know are
    dropped (a later release may remove a step), and a broken tour entry is forgotten.
    An unreadable document reads as a fresh state at ``version``."""
    if not doc:
        return OnboardingState(version=version)
    data: dict[str, Any] = {}
    checklist = doc.get("checklist")
    if isinstance(checklist, Mapping):
        data["checklist"] = {k: v for k, v in checklist.items() if k in CHECKLIST_STEPS}
    tours = doc.get("tours")
    if isinstance(tours, Mapping):
        kept: dict[str, Any] = {}
        for k, v in tours.items():
            if k not in TOUR_IDS:
                continue
            try:
                kept[k] = TourProgress.model_validate(v)
            except ValidationError:
                continue
        data["tours"] = kept
    for key in ("checklist_dismissed", "tips_off"):
        if isinstance(doc.get(key), bool):
            data[key] = doc[key]
    hidden = doc.get("hidden_samples")
    if isinstance(hidden, list):
        data["hidden_samples"] = [s for s in hidden if isinstance(s, str)][:MAX_HIDDEN_SAMPLES]
    if doc.get("welcomed_at") is not None:
        data["welcomed_at"] = doc["welcomed_at"]
    try:
        return OnboardingState.model_validate({**data, "version": version})
    except ValidationError:
        return OnboardingState(version=version)


def apply_patch(
    state: OnboardingState, patch: OnboardingPatch, *, now: datetime
) -> OnboardingState:
    """Merge ``patch`` into ``state`` (pure).

    * ``complete`` adds steps with the time ``now``; a step already done keeps its first
      time, and no patch removes a step.
    * ``tours`` replaces the named tours (``updated_at`` becomes ``now``); a tour that
      reaches ``completed`` also completes ``took_tour``. A tour already at the same status
      and step is left as it is (its ``updated_at`` too), and a ``completed`` tour stays
      completed (a stale tab cannot reopen it; only ``:reset`` starts again).
    * ``hide_samples`` / ``unhide_samples`` edit the hidden list (unhide wins over hide).
    * ``welcomed: true`` records ``welcomed_at`` once; ``false`` changes nothing.

    ``version`` goes up by one when anything changed and stays the same otherwise, so a
    repeated PATCH is harmless. Raises ``SCHEMA_INVALID`` when more than
    :data:`MAX_HIDDEN_SAMPLES` samples would be hidden and ``PAYLOAD_TOO_LARGE`` when the
    result exceeds :data:`MAX_ONBOARDING_BYTES`.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    checklist = dict(state.checklist)
    for step in patch.complete:
        checklist.setdefault(step, now)
    tours = dict(state.tours)
    for tour_id, progress in (patch.tours or {}).items():
        stored = tours.get(tour_id)
        if stored is not None and (
            (stored.status, stored.step) == (progress.status, progress.step)
            or (stored.status == "completed" and progress.status != "completed")
        ):
            continue
        tours[tour_id] = progress.model_copy(update={"updated_at": now})
        if progress.status == "completed":
            checklist.setdefault(_TOUR_DONE_STEP, now)
    hidden = list(state.hidden_samples)
    for sample in patch.hide_samples:
        if sample not in hidden:
            hidden.append(sample)
    unhide = set(patch.unhide_samples)
    hidden = [s for s in hidden if s not in unhide]
    if len(hidden) > MAX_HIDDEN_SAMPLES:
        raise MapiError(
            f"you can hide at most {MAX_HIDDEN_SAMPLES} sample projects",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/hide_samples",
        )
    welcomed_at = state.welcomed_at
    if patch.welcomed and welcomed_at is None:
        welcomed_at = now
    merged = state.model_copy(
        update={
            "checklist": {s: checklist[s] for s in CHECKLIST_STEPS if s in checklist},
            "tours": tours,
            "checklist_dismissed": (
                state.checklist_dismissed
                if patch.checklist_dismissed is None
                else patch.checklist_dismissed
            ),
            "tips_off": state.tips_off if patch.tips_off is None else patch.tips_off,
            "hidden_samples": hidden,
            "welcomed_at": welcomed_at,
        }
    )
    if state_document(merged) == state_document(state):
        return state
    out = OnboardingState.model_validate(
        {**merged.model_dump(mode="json"), "version": state.version + 1}
    )
    if state_bytes(out) > MAX_ONBOARDING_BYTES:
        raise MapiError(
            f"the onboarding state would exceed {MAX_ONBOARDING_BYTES} bytes",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
        )
    return out


def reset_state(state: OnboardingState) -> OnboardingState:
    """``POST …:reset``: the checklist and every tour start again and tips come back on.
    The hidden samples and the first welcome are kept (the user asked for the reset, so the
    welcome dialog does not show again). ``version`` always goes up by one."""
    return OnboardingState(
        version=state.version + 1,
        hidden_samples=list(state.hidden_samples),
        welcomed_at=state.welcomed_at,
    )


def checklist_done(state: OnboardingState) -> bool:
    """True when every step is done or the user dismissed the checklist (the Start-here hub
    then collapses to a one-line banner)."""
    return state.checklist_dismissed or all(s in state.checklist for s in CHECKLIST_STEPS)
