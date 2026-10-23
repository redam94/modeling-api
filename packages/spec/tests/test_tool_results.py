"""v0.20 tool-result caps (:mod:`mapi_spec.tool_results`): fit, determinism and goldens.

The realistic inputs in ``data/tool_results/`` are ``get_readout`` results whose snapshot is
built from the v0.17 golden fixtures (``panels.build_snapshot_v2`` over ``compile_preset``,
the WP0 (d) measurement); ``causal_impact`` is the largest real readout (about 25 KB).
``golden/tool_results.json`` pins the capped output of each input at each budget. Regenerate
it only on purpose: ``MAPI_UPDATE_SNAPSHOTS=1 pytest packages/spec/tests/test_tool_results.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_spec.chat import CHAT_TOOL_RESULT_MAX_BYTES, MCP_TOOL_RESULT_MAX_BYTES
from mapi_spec.tool_results import (
    MIN_CAP_BYTES,
    TRIM_ORDER,
    TRUNCATED_KEY,
    CappedResult,
    canonical_tool_json,
    cap_tool_result,
)
from pydantic import JsonValue

pytestmark = pytest.mark.unit

DATA = Path(__file__).parent / "data" / "tool_results"
GOLDEN = Path(__file__).parent / "golden" / "tool_results.json"
BUDGETS = (MCP_TOOL_RESULT_MAX_BYTES, CHAT_TOOL_RESULT_MAX_BYTES, 8192, 2048, MIN_CAP_BYTES)
INPUTS: dict[str, Any] = {
    p.stem: json.loads(p.read_text("utf-8")) for p in sorted(DATA.glob("*.json"))
}


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _record(name: str, result: Any, budget: int) -> dict[str, Any]:
    tool = name.split("_", 2)[0] + "_" + name.split("_", 2)[1]  # get_readout_<case>
    capped = cap_tool_result(tool, result, budget)
    return {
        "bytes": _size(capped.text),
        "original_bytes": capped.original_bytes,
        "sha256": hashlib.sha256(capped.text.encode("utf-8")).hexdigest(),
        "truncated": capped.truncated,
    }


def _current() -> dict[str, dict[str, Any]]:
    return {
        name: {str(b): _record(name, result, b) for b in BUDGETS} for name, result in INPUTS.items()
    }


# =================================================================================== goldens


def test_inputs_are_the_wp0_fixtures() -> None:
    assert set(INPUTS) == {
        "get_readout_causal_impact",
        "get_readout_classifier_binary",
        "get_readout_field_lgcp",
        "get_readout_forecast_bsts",
    }
    sizes = {n: _size(canonical_tool_json(r)) for n, r in INPUTS.items()}
    # the largest real readout is over the chat budget and under the MCP budget (WP0 (d))
    assert sizes["get_readout_causal_impact"] > CHAT_TOOL_RESULT_MAX_BYTES
    assert sizes["get_readout_causal_impact"] < MCP_TOOL_RESULT_MAX_BYTES
    assert sizes["get_readout_classifier_binary"] < CHAT_TOOL_RESULT_MAX_BYTES


def test_capped_outputs_match_the_golden() -> None:
    current = _current()
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", "utf-8")
    assert current == json.loads(GOLDEN.read_text("utf-8")), (
        "cap_tool_result output changed; regenerate golden/tool_results.json on purpose"
    )


@pytest.mark.parametrize("name", sorted(INPUTS))
@pytest.mark.parametrize("budget", BUDGETS)
def test_every_budget_is_kept(name: str, budget: int) -> None:
    result = INPUTS[name]
    before = copy.deepcopy(result)
    capped = cap_tool_result("get_readout", result, budget)
    assert _size(capped.text) <= budget
    assert capped.text == canonical_tool_json(capped.payload)
    assert json.loads(capped.text) == capped.payload
    assert capped.original_bytes == _size(canonical_tool_json(result))
    assert capped.truncated is (capped.original_bytes > budget)
    assert result == before, "the input must not be changed"
    if not capped.truncated:
        assert capped.payload is result


def test_chat_budget_trims_points_of_the_largest_readout_first() -> None:
    result = INPUTS["get_readout_causal_impact"]
    capped = cap_tool_result("get_readout", result, CHAT_TOOL_RESULT_MAX_BYTES)
    assert capped.truncated
    payload = capped.payload
    assert isinstance(payload, dict)
    # trimming kept the readout: the header and the tiles are still there
    assert payload["seq"] == result["seq"]
    assert payload["snapshot"]["tiles"] == result["snapshot"]["tiles"]
    assert f'"{TRUNCATED_KEY}":' in capped.text
    # the MCP budget passes the same readout untrimmed
    assert not cap_tool_result("get_readout", result, MCP_TOOL_RESULT_MAX_BYTES).truncated


# ================================================================================= behaviour


def _series(n: int) -> list[JsonValue]:
    return [{"x": i, "y": i * 1.5} for i in range(n)]


def test_small_results_pass_unchanged() -> None:
    result: dict[str, JsonValue] = {"b": 1, "a": "é"}
    capped = cap_tool_result("explain_metric", result, 1024)
    assert capped == CappedResult(result, '{"a":"é","b":1}', False, 16)  # é is 2 bytes
    assert capped.payload is result


def test_lists_keep_their_first_and_last_items_with_a_marker() -> None:
    result: dict[str, JsonValue] = {"title": "t", "points": _series(200)}
    capped = cap_tool_result("get_channel", result, 1500)
    assert capped.truncated
    payload = capped.payload
    assert isinstance(payload, dict)
    points = payload["points"]
    assert isinstance(points, list)
    marker = [p for p in points if isinstance(p, dict) and TRUNCATED_KEY in p]
    assert len(marker) == 1
    n = points.index(marker[0])
    assert points[:n] == _series(200)[:n] and points[n + 1 :] == _series(200)[-n:]
    assert marker[0][TRUNCATED_KEY] == 200 - 2 * n
    assert payload["title"] == "t"


def test_trim_order_is_followed_and_deep_lists_are_trimmed() -> None:
    assert TRIM_ORDER[0] == "points" and TRIM_ORDER[-1] == "items"
    result: dict[str, JsonValue] = {
        "panels": [{"id": "p1", "series": [{"name": "a", "points": _series(300)}]}],
        "tiles": [{"key": f"t{i}", "value": i} for i in range(20)],
    }
    capped = cap_tool_result("get_panel", result, 2000)
    payload = capped.payload
    assert isinstance(payload, dict)
    # points (first in the order) went first; tiles (later) were not needed
    assert payload["tiles"] == result["tiles"]
    panels = payload["panels"]
    assert isinstance(panels, list) and isinstance(panels[0], dict)
    series = panels[0]["series"]
    assert isinstance(series, list) and isinstance(series[0], dict)
    points = series[0]["points"]
    assert isinstance(points, list) and len(points) < 300


def test_list_readouts_trims_its_items_first() -> None:
    items: list[JsonValue] = [{"seq": i, "kind": "baseline", "points": [1, 2]} for i in range(60)]
    result: dict[str, JsonValue] = {"items": items, "next_before_seq": None}
    capped = cap_tool_result("list_readouts", result, 1200)
    payload = capped.payload
    assert isinstance(payload, dict) and isinstance(payload["items"], list)
    kept = [i for i in payload["items"] if isinstance(i, dict) and "seq" in i]
    assert kept and all(i["points"] == [1, 2] for i in kept)  # points untouched


def test_last_resort_keeps_top_level_scalars_only() -> None:
    result: dict[str, JsonValue] = {
        "seq": 12,
        "title": "x" * 500,
        "ok": True,
        "blob": {"deep": "y" * 5000},
        "rows": ["z" * 5000],
    }
    capped = cap_tool_result("get_readout", result, 400)
    assert capped.truncated and _size(capped.text) <= 400
    assert capped.payload == {
        TRUNCATED_KEY: True,
        "summary": {"ok": True, "seq": 12, "title": "x" * 200 + "…"},
    }


def test_last_resort_drops_keys_until_it_fits() -> None:
    result: dict[str, JsonValue] = {f"k{i:02d}": "v" * 150 for i in range(20)}
    capped = cap_tool_result("get_dashboard", result, MIN_CAP_BYTES)
    assert _size(capped.text) <= MIN_CAP_BYTES
    assert capped.payload == {TRUNCATED_KEY: True, "summary": {}}
    assert cap_tool_result("x", ["a" * 100], MIN_CAP_BYTES).payload == {
        TRUNCATED_KEY: True,
        "summary": {"length": 1},
    }


def test_a_tiny_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        cap_tool_result("get_dashboard", {}, MIN_CAP_BYTES - 1)


def test_a_maximum_size_readout_fits_both_budgets() -> None:
    # a stored readout can be up to MAX_READOUT_BYTES (256 KiB)
    result: dict[str, JsonValue] = {
        "seq": 3,
        "snapshot": {
            "series": [{"name": f"s{j}", "points": _series(520)} for j in range(20)],
            "frames": [{"codes_b64": "A" * 8192} for _ in range(8)],
            "tiles": [{"key": "revenue", "value": 1.0}],
        },
    }
    assert _size(canonical_tool_json(result)) > 256 * 1024
    for budget in (CHAT_TOOL_RESULT_MAX_BYTES, MCP_TOOL_RESULT_MAX_BYTES):
        capped = cap_tool_result("get_readout", result, budget)
        assert capped.truncated and _size(capped.text) <= budget
        assert capped == cap_tool_result("get_readout", copy.deepcopy(result), budget)


_json = st.recursive(
    st.none() | st.booleans() | st.integers(-(10**9), 10**9) | st.text(max_size=40),
    lambda inner: (
        st.lists(inner, max_size=12)
        | st.dictionaries(st.sampled_from([*TRIM_ORDER, "a", "title", "seq"]), inner, max_size=6)
    ),
    max_leaves=60,
)


@settings(max_examples=200, deadline=None)
@given(result=_json, budget=st.integers(MIN_CAP_BYTES, 3000))
def test_any_result_fits_and_is_deterministic(result: JsonValue, budget: int) -> None:
    a = cap_tool_result("get_readout", result, budget)
    b = cap_tool_result("get_readout", copy.deepcopy(result), budget)
    assert a.text == b.text and a.truncated == b.truncated
    assert _size(a.text) <= budget
    assert json.loads(a.text) == a.payload
