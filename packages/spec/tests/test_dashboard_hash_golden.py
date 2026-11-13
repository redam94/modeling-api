"""v0.17 hash guard: stored layout 1 dashboard specs keep their ``spec_hash``.

``golden/dashboard_v1_hashes.json`` was written with the v0.16 ``DashboardSpec`` *before* the
v0.17 edits (the spec example, the MMM and dummy-channel forecast mocks, a draft and six
permutations: key order, explicit defaults, no ``layout_version``, float spelling). Never
regenerate it: a change here means a stored revision would get a new identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.dashboard_v2 import DashboardSpecV2
from mapi_spec.dashboards import (
    Dashboard,
    DashboardCreate,
    DashboardRevision,
    DashboardSpec,
    RefreshPolicy,
    dashboard_spec_hash,
    parse_dashboard_spec,
)
from mapi_spec.loader import canonical_json

pytestmark = pytest.mark.unit

GOLDEN: dict[str, Any] = json.loads(
    (Path(__file__).parent / "golden" / "dashboard_v1_hashes.json").read_text("utf-8")
)
CASES: list[dict[str, Any]] = GOLDEN["cases"]


def test_golden_covers_the_fixtures() -> None:
    names = {c["name"] for c in CASES}
    assert {"spec_example", "mmm_mock", "forecast_dummy_channel_mock"} <= names
    assert sum(1 for n in names if n.startswith("perm_")) == 6


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_v1_hash_is_unchanged(case: dict[str, Any]) -> None:
    spec = parse_dashboard_spec(case["spec"])
    assert type(spec) is DashboardSpec
    assert canonical_json(spec) == case["canonical"]
    assert dashboard_spec_hash(spec) == case["hash"]
    # the old entry point and a JSON round trip agree
    assert dashboard_spec_hash(DashboardSpec.model_validate(case["spec"])) == case["hash"]
    again = parse_dashboard_spec(spec.model_dump_json())
    assert again == spec and dashboard_spec_hash(again) == case["hash"]
    # layout 1 canonical JSON never names the new optional field
    assert "horizon" not in case["canonical"] and "layout_version" not in case["canonical"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_v1_specs_inside_dtos_keep_their_hash(case: dict[str, Any]) -> None:
    rev = DashboardRevision.model_validate(
        {**DashboardRevision.model_json_schema()["examples"][0], "spec": case["spec"]}
    )
    assert isinstance(rev.spec, DashboardSpec)
    assert dashboard_spec_hash(rev.spec) == case["hash"]
    dash = Dashboard.model_validate(
        {**Dashboard.model_json_schema()["examples"][0], "draft_spec": case["spec"]}
    )
    assert isinstance(dash.draft_spec, DashboardSpec)
    assert dashboard_spec_hash(dash.draft_spec) == case["hash"]
    create = DashboardCreate.model_validate({"name": "acme", "spec": case["spec"]})
    assert isinstance(create.spec, DashboardSpec)


def test_refresh_horizon_is_excluded_when_unset() -> None:
    assert canonical_json(RefreshPolicy()) == "{}"
    assert json.loads(canonical_json(RefreshPolicy(horizon=8))) == {"horizon": 8}


def test_v2_hash_always_names_its_layout() -> None:
    example = DashboardSpecV2.model_json_schema()["examples"][0]
    spec = parse_dashboard_spec(example)
    assert isinstance(spec, DashboardSpecV2)
    canonical = json.loads(canonical_json(spec))
    assert canonical["layout_version"] == 2
    v1_hashes = {c["hash"] for c in CASES}
    assert dashboard_spec_hash(spec) not in v1_hashes
