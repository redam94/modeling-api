"""Real ``mmm.multivariate`` fits (tiny settings; ``slow``)."""

from __future__ import annotations

import numpy as np
import pytest
from mapi_plugin_mmm.examples_multivariate import (
    MULTIVARIATE_SPEC_YAML,
    multivariate_dataset,
    multivariate_raw_schema,
)
from mapi_plugin_mmm.multivariate import MMMMultivariate
from mapi_pymc.engines import engine_available
from mapi_sdk.models import FitContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.spec import FitConfig

pytestmark = pytest.mark.slow


@pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed")
def test_nutpie_fit_streams_chains_and_logs_the_summary() -> None:
    ds, _ = multivariate_dataset(weeks=60, seed=1)
    stage = ds.rename({"product": "outcome", "week": "time", "sales": "y"})
    plugin = MMMMultivariate()
    rec = MetricRecorder()
    s = plugin.parse_settings(
        {
            "cross_effects": [{"source": "premium", "target": "basic", "kind": "halo"}],
            "trend": {"kind": "linear"},
            "seasonality": {"yearly_order": 1},
        }
    )
    cfg = FitConfig(engine="nutpie", chains=2, tune=30, draws=30, seed=1)
    out = plugin.fit(stage, s, cfg, FitContext(seed=1, tracker=rec))
    post = out.groups["posterior"]
    assert {"beta_media", "psi_matrix", "Y_obs_correlation"} <= set(map(str, post.data_vars))
    assert post.sizes["chain"] == 2 and post.sizes["draw"] == 30
    assert {"prior", "sample_stats", "fit_data", "observed_data"} <= set(out.groups)
    assert "energy" in out.groups["sample_stats"]
    series = rec.series()
    assert series[("step_size", "chain=0")] and len(rec.last_chains) == 2
    assert ("roi", "channel=tv,outcome=basic") in series
    assert ("roi", "channel=tv") in series
    assert rec.params["mmm.outcomes"] == "premium,basic"
    assert rec.params["mmm.cross_effects"] == 1
    assert out.learned["primary_outcome"] == "premium"
    assert np.isfinite(post["psi_matrix"].values).all()


@pytest.mark.mapi_contract
def test_multivariate_passes_the_contract_kit(registry: Registry) -> None:
    ds, _ = multivariate_dataset(weeks=60, seed=2)
    text = MULTIVARIATE_SPEC_YAML.replace("tune: 100, draws: 100", "tune: 30, draws: 30").replace(
        "ui_draws: 100", "ui_draws: 30"
    )
    reg = registry.copy()
    reg.register_model(MMMMultivariate, replace=True)
    fixture = ModelFixture(spec=text, data=ds, raw_schema=multivariate_raw_schema(), registry=reg)
    report = check_model(MMMMultivariate, fixture, raise_on_failure=False, require_about=True)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit", "bundle_roundtrip_no_pickle", "subset_scoring", "context_window_equivalence",
        "evaluate_path_equals_score_path", "outputs_match_manifest", "additive_contributions",
        "plots_sanitize",
    } <= ran  # fmt: skip
