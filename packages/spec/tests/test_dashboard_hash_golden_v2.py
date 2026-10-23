"""v0.20 hash guard: stored layout 2 dashboard specs keep their ``spec_hash``.

``golden/dashboard_v2_hashes.json`` holds ``compile_preset`` specs of the v0.17 golden
fixtures (forecast, classifier, field map, causal impact), frozen on the v0.19 tree before the
v0.20 edits (plan WP0 (g)). v0.20 keeps chat settings in a table, so no spec change may move
these hashes. Never regenerate the file: a change here means every stored layout 2 revision
would get a new identity, and every readout's ``spec_hash`` would stop matching.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.dashboard_v2 import DashboardSpecV2
from mapi_spec.dashboards import DashboardRevision, dashboard_spec_hash, parse_dashboard_spec
from mapi_spec.loader import canonical_json

pytestmark = pytest.mark.unit

GOLDEN: dict[str, Any] = json.loads(
    (Path(__file__).parent / "golden" / "dashboard_v2_hashes.json").read_text("utf-8")
)
CASES: list[dict[str, Any]] = GOLDEN["cases"]

# the hashes recorded by the WP0 probe (plans/v020-probe.md (g)), independent of the file
PROBE_HASHES = {
    "forecast_bsts": "sha256:e72d4034530388b474706f143b317c73ad21a5edbd2c14ffb798f4de224fc715",
    "classifier_binary": "sha256:a3292f831bd62725a731056f0ac4ed5fce7a7583cd67e55d8d1661c8ac80fdd8",
    "field_lgcp": "sha256:8cae6c98bcd1d7452f487e2a3a888e76e74975656984660916001e61d24b3e25",
    "causal_impact": "sha256:d627a5abe7e8ef4674cb9921d62b92a11ffbdcbe00ea8fd00a385c640c9e3ab8",
}


def test_golden_is_the_probe_record() -> None:
    assert {c["name"]: c["hash"] for c in CASES} == PROBE_HASHES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_v2_hash_is_unchanged(case: dict[str, Any]) -> None:
    spec = parse_dashboard_spec(case["spec"])
    assert type(spec) is DashboardSpecV2
    assert canonical_json(spec) == case["canonical"]
    assert dashboard_spec_hash(spec) == case["hash"]
    assert json.loads(case["canonical"])["layout_version"] == 2
    # a JSON round trip and the strict model agree
    again = parse_dashboard_spec(spec.model_dump_json())
    assert again == spec and dashboard_spec_hash(again) == case["hash"]
    assert dashboard_spec_hash(DashboardSpecV2.model_validate(case["spec"])) == case["hash"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_v2_specs_inside_a_revision_keep_their_hash(case: dict[str, Any]) -> None:
    rev = DashboardRevision.model_validate(
        {**DashboardRevision.model_json_schema()["examples"][0], "spec": case["spec"]}
    )
    assert isinstance(rev.spec, DashboardSpecV2)
    assert dashboard_spec_hash(rev.spec) == case["hash"]


def test_v2_canonical_json_names_no_chat_setting() -> None:
    # chat settings live in dashboard_chat_settings (D4), never in the spec
    for case in CASES:
        assert "chat" not in case["canonical"].lower(), case["name"]
    assert not {f for f in DashboardSpecV2.model_fields if "chat" in f}
