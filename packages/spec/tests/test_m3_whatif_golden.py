"""Golden what-if values shared with the web app's TypeScript port of the slider interpolation.

``snapshots/whatif_golden.json`` holds response grids, scenarios and the results
:func:`mapi_spec.whatif.evaluate_scenario` gives for them (or the refusal it raises).
``apps/web/src/features/guided/whatif.test.ts`` replays every case against the TypeScript port,
so the browser and the reference implementation cannot drift apart. Regenerate after a
deliberate change with ``MAPI_UPDATE_SNAPSHOTS=1 uv run pytest
packages/spec/tests/test_m3_whatif_golden.py`` (``make snapshots``).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.errors import MapiError
from mapi_spec.views import RESPONSE_GRID_FORMAT, ResponseGridView
from mapi_spec.whatif import WhatIfScenario, evaluate_scenario

pytestmark = pytest.mark.unit

SNAPSHOT = Path(__file__).parent / "snapshots" / "whatif_golden.json"


def _hand_grid() -> dict[str, Any]:
    """The grid of ``test_m3_whatif.py`` (golden values there are computed by hand)."""
    return {
        "format": RESPONSE_GRID_FORMAT,
        "kpi": "revenue",
        "kpi_units": "EUR",
        "spend_units": "EUR",
        "periods": 52,
        "channels": ["tv", "search"],
        "geos": ["de", "fr"],
        "multipliers": [0.0, 1.0, 2.0],
        "draws": 2,
        "contribution": [[[0, 10, 15], [0, 4, 6]], [[0, 20, 30], [0, 2, 3]]],
        "spend": [[0, 100, 200], [0, 50, 100]],
        "current_spend": [100, 50],
        "baseline": [100, 200],
        "contribution_by_geo": [
            [[[0, 0], [6, 4], [9, 6]], [[0, 0], [1, 3], [2, 4]]],
            [[[0, 0], [12, 8], [18, 12]], [[0, 0], [1, 1], [2, 1]]],
        ],
        "spend_by_geo": [[[0, 60, 120], [0, 20, 40]], [[0, 40, 80], [0, 30, 60]]],
    }


def _saturating_grid() -> dict[str, Any]:
    """A deterministic grid shaped like a real fit: 25 draws, three channels with
    diminishing returns on a 9-point multiplier grid and a baseline per draw."""
    mults = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
    channels = ["tv", "search", "social"]
    current = [120_000.0, 80_000.0, 40_000.0]
    draws = 25
    contribution: list[list[list[float]]] = []
    baseline: list[float] = []
    for d in range(draws):
        wobble = 1 + 0.15 * math.sin(1.7 * d + 0.3)
        per_c: list[list[float]] = []
        for c, spend in enumerate(current):
            roi = (2.4, 3.1, 1.6)[c] * (1 + 0.1 * math.cos(d + c))
            half = (0.9, 1.3, 0.7)[c]
            per_c.append(
                [round(roi * spend * wobble * m / (m + half) * (1 + half), 6) for m in mults]
            )
        contribution.append(per_c)
        baseline.append(round(2_000_000 * (1 + 0.05 * math.sin(3.1 * d)), 6))
    return {
        "format": RESPONSE_GRID_FORMAT,
        "kpi": "revenue",
        "kpi_units": "EUR",
        "spend_units": "EUR",
        "periods": 104,
        "channels": channels,
        "multipliers": mults,
        "draws": draws,
        "contribution": contribution,
        "spend": [[s * m for m in mults] for s in current],
        "current_spend": current,
        "baseline": baseline,
    }


CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("hand: tv halved, search up half", "hand", {"multipliers": {"tv": 0.5, "search": 1.5}}),
    ("hand: identity", "hand", {}),
    ("hand: exact grid point", "hand", {"multipliers": {"tv": 2.0}}),
    ("hand: per geo", "hand", {"multipliers": {"tv": 2.0}, "geo": "fr"}),
    ("hand: above the grid", "hand", {"multipliers": {"tv": 2.5}}),
    ("hand: unknown channel", "hand", {"multipliers": {"radio": 1.0}}),
    ("hand: unknown geo", "hand", {"geo": "it"}),
    ("saturating: shift tv to search", "saturating", {"multipliers": {"tv": 0.8, "search": 1.3}}),
    (
        "saturating: every channel between grid points",
        "saturating",
        {"multipliers": {"tv": 1.13, "search": 0.37, "social": 1.92}},
    ),
    ("saturating: turn social off", "saturating", {"multipliers": {"social": 0.0}}),
]
LEVELS = [0.05, 0.5, 0.95]


def _golden() -> dict[str, Any]:
    grids = {"hand": _hand_grid(), "saturating": _saturating_grid()}
    cases = []
    for name, grid, scenario in CASES:
        view = ResponseGridView.model_validate(grids[grid])
        case: dict[str, Any] = {"name": name, "grid": grid, "scenario": scenario}
        try:
            result = evaluate_scenario(view, WhatIfScenario.model_validate(scenario), levels=LEVELS)
        except MapiError as exc:
            case["error"] = {"code": exc.code.value, "pointer": exc.pointer}
        else:
            case["result"] = result.model_dump(mode="json")
        cases.append(case)
    return {
        "about": "Golden cases of mapi_spec.whatif.evaluate_scenario (test_m3_whatif_golden.py)",
        "levels": LEVELS,
        "grids": grids,
        "cases": cases,
    }


def test_whatif_golden_snapshot_is_current() -> None:
    current = _golden()
    text = json.dumps(current, indent=1, sort_keys=True) + "\n"
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(text, "utf-8")
    assert SNAPSHOT.exists(), "regenerate with MAPI_UPDATE_SNAPSHOTS=1"
    assert json.loads(SNAPSHOT.read_text("utf-8")) == json.loads(text), (
        "the what-if golden cases changed: regenerate with MAPI_UPDATE_SNAPSHOTS=1 and make "
        "sure apps/web/src/features/guided/whatif.test.ts still passes"
    )


def test_golden_cases_cover_results_and_refusals() -> None:
    cases = _golden()["cases"]
    assert sum("result" in c for c in cases) >= 6
    assert {c["error"]["code"] for c in cases if "error" in c} == {"SCENARIO_OUT_OF_RANGE"}
