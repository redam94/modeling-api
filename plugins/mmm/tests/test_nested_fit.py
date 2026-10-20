"""Real ``mmm.nested`` fits (tiny settings; ``slow``): live sampling metrics, run tracking,
cancellation and the SDK contract kit."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
import xarray as xr
from mapi_plugin_mmm.examples_nested import NESTED_SPEC_YAML, nested_dataset, nested_raw_schema
from mapi_plugin_mmm.nested import MMMNested
from mapi_pymc.engines import engine_available
from mapi_sdk.errors import RunCancelled
from mapi_sdk.models import FitContext
from mapi_sdk.registry import Registry
from mapi_sdk.steps import CancelToken
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.run import ChainProgress
from mapi_spec.spec import FitConfig

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed"),
]

SETTINGS = {
    "mediators": [{"name": "visits"}, {"name": "awareness", "channels": ["tv"]}],
    "trend": {"kind": "linear"},
    "seasonality": {"yearly_order": 1},
}


@pytest.fixture(scope="module")
def stage() -> xr.Dataset:
    ds, _ = nested_dataset(weeks=60)
    return ds.rename({"sales": "y", "week": "time"})


class _CancelOnChains(MetricRecorder):
    """Cancels ``token`` on the first chains report."""

    def __init__(self, token: CancelToken) -> None:
        super().__init__()
        self.token = token

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        super().chains(chains)
        self.token.cancel("test")


def test_nutpie_fit_streams_chains_step_metrics_and_the_summary(stage: xr.Dataset) -> None:
    plugin = MMMNested()
    rec = MetricRecorder()
    events: list[tuple[float, str]] = []
    ctx = FitContext(seed=1, tracker=rec, on_progress=lambda f, m: events.append((f, m)))
    cfg = FitConfig(engine="nutpie", chains=2, tune=30, draws=30, seed=1)
    out = plugin.fit(stage, plugin.parse_settings(SETTINGS), cfg, ctx)
    assert {"posterior", "sample_stats", "prior", "fit_data", "observed_data"} <= set(out.groups)
    post = out.groups["posterior"]
    assert (post.sizes["chain"], post.sizes["draw"]) == (2, 30)
    assert "energy" in out.groups["sample_stats"]
    series = rec.series()
    assert series[("step_size", "chain=0")] and series[("divergences", "chain=1")]
    assert len(rec.last_chains) == 2
    live = [f for f, m in events if m.startswith("chain ")]
    assert live and all(0.1 <= f <= 0.9 for f in live)
    assert ("roi", "channel=tv") in series and ("mroi", "channel=search") in series
    assert rec.params["mmm.mediators"] == "visits,awareness"
    assert out.learned["method"] == "nuts" and out.learned["chains"] == 2


def test_nutpie_fit_cancels_while_sampling(stage: xr.Dataset) -> None:
    plugin = MMMNested()
    token = CancelToken()
    rec = _CancelOnChains(token)
    ctx = FitContext(seed=1, tracker=rec, cancel=token)
    cfg = FitConfig(engine="nutpie", chains=2, tune=2000, draws=2000, seed=1)
    with pytest.raises(RunCancelled):
        plugin.fit(stage, plugin.parse_settings(SETTINGS), cfg, ctx)
    # stopped while sampling, not by the check after a complete fit
    assert max(c.draws_done for c in rec.last_chains) < 4000


NATIONAL_CHECKS = (
    "manifest", "about", "fit", "bundle_roundtrip_no_pickle", "shuffled_scoring",
    "new_level_policies", "extend_typed_errors", "horizon_exceeds_fit",
    "missing_future_covariate", "recursive_unsupported", "unsupported_mode",
    "context_window_equivalence", "evaluate_path_equals_score_path", "output_space_and_units",
    "outputs_match_manifest", "representation_aggregation", "additive_contributions",
    "seed_determinism", "fast_score_matches_exact", "plots_sanitize",
)  # fmt: skip
"""Every check but ``subset_scoring``: the model is national, and scoring a subset of the
channels leaves the others without spend (see ``test_fit.NATIONAL_CHECKS``)."""


@pytest.mark.mapi_contract
def test_the_example_spec_passes_the_contract_kit(registry: Registry) -> None:
    ds, _ = nested_dataset(weeks=60)
    text = NESTED_SPEC_YAML.replace("tune: 100, draws: 100", "tune: 30, draws: 30")
    fixture = ModelFixture(spec=text, data=ds, raw_schema=nested_raw_schema(), registry=registry)
    report = check_model(
        MMMNested, fixture, raise_on_failure=False, require_about=True, checks=NATIONAL_CHECKS
    )
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {"fit", "additive_contributions", "outputs_match_manifest", "plots_sanitize"} <= ran
