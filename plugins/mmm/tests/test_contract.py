"""The SDK contract kit (``check_model``) on ``mmm.hierarchical_geo`` with the real steps."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import xarray as xr
from mapi_plugin_mmm.examples import E33Truth
from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import DataSchema

EXPECTED = {
    "manifest",
    "fit",
    "bundle_roundtrip_no_pickle",
    "shuffled_scoring",
    "subset_scoring",
    "new_level_policies",
    "extend_typed_errors",
    "context_window_equivalence",
    "evaluate_path_equals_score_path",
    "output_space_and_units",
    "representation_aggregation",
    "additive_contributions",
}


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_hierarchical_geo_passes_the_contract_kit(
    registry: Registry,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
    raw_schema: DataSchema,
    spec_text: Callable[..., str],
) -> None:
    # the kit's score requests do not supply side tables: pin fx; no derived quantities
    text = spec_text(
        **{
            "score_policy: supplied": "score_policy: pinned",
            "  derived: [contributions, response_curves, marginal_cost]\n": "",
        }
    )
    fixture = ModelFixture(
        spec=text,
        data=small[0],
        side_tables=tables,
        raw_schema=raw_schema,
        registry=registry,
        window=28,  # four whole W-SUN weeks of daily data
    )
    report = check_model(HierarchicalGeoMMM, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert ran >= EXPECTED, sorted(EXPECTED - ran)
    detail = report.get("new_level_policies").detail
    assert "market: new level scored (hyperprior)" in detail
    assert "channel" in detail and "UNSEEN_LEVELS" in detail
    # nutpie (the default engine) is not seed-deterministic, so the kit skips that check
    assert report.get("seed_determinism").skipped
