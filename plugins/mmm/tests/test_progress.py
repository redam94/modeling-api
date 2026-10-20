"""``mmm.bayesian`` live sampling: progress, chains, step metrics and cancellation (slow)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
import xarray as xr
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_pymc.engines import engine_available
from mapi_sdk.errors import RunCancelled
from mapi_sdk.models import FitContext
from mapi_sdk.steps import CancelToken
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.run import ChainProgress
from mapi_spec.spec import FitConfig

pytestmark = pytest.mark.slow

SETTINGS = {"adstock": {"kind": "geometric", "l_max": 3}, "trend": {"kind": "none"}}


@pytest.fixture(scope="module")
def stage(geo_stage: xr.Dataset) -> xr.Dataset:
    """One geo of the shared geo panel: a national model."""
    return geo_stage.isel(geo=0, drop=True)


def _cfg(engine: str) -> FitConfig:
    return FitConfig(engine=engine, chains=2, tune=30, draws=30, seed=1)


class _CancelOnChains(MetricRecorder):
    """Cancels ``token`` on the first chains report."""

    def __init__(self, token: CancelToken) -> None:
        super().__init__()
        self.token = token

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        super().chains(chains)
        self.token.cancel("test")


@pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed")
def test_nutpie_fit_streams_progress_chains_and_step_metrics(stage: xr.Dataset) -> None:
    plugin = MMMBayesian()
    rec = MetricRecorder()
    events: list[tuple[float, str]] = []
    ctx = FitContext(seed=1, tracker=rec, on_progress=lambda f, m: events.append((f, m)))
    out = plugin.fit(stage, plugin.parse_settings(SETTINGS), _cfg("nutpie"), ctx)
    assert "posterior" in out.groups
    series = rec.series()
    assert series[("step_size", "chain=0")] and series[("divergences", "chain=1")]
    assert len(rec.last_chains) == 2
    live = [f for f, m in events if m.startswith("chain ")]
    assert live and all(0.1 <= f <= 0.9 for f in live)


@pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed")
def test_nutpie_fit_cancels_while_sampling(stage: xr.Dataset) -> None:
    plugin = MMMBayesian()
    token = CancelToken()
    rec = _CancelOnChains(token)
    ctx = FitContext(seed=1, tracker=rec, cancel=token)
    cfg = FitConfig(engine="nutpie", chains=2, tune=2000, draws=2000, seed=1)
    with pytest.raises(RunCancelled):
        plugin.fit(stage, plugin.parse_settings(SETTINGS), cfg, ctx)
    # stopped while sampling, not by the check after a complete fit
    assert max(c.draws_done for c in rec.last_chains) < 4000


def test_pymc_sampler_fit_reports_chains_and_cancels(stage: xr.Dataset) -> None:
    plugin = MMMBayesian()
    rec = MetricRecorder()
    plugin.fit(
        stage, plugin.parse_settings(SETTINGS), _cfg("pymc"), FitContext(seed=1, tracker=rec)
    )
    assert [c.chain for c in rec.last_chains] == [0, 1]
    series = rec.series()
    for key in ("divergences", "step_size", "num_steps", "tree_depth"):
        assert (key, "chain=0") in series, key
    assert all(c.step_size is not None for c in rec.last_chains)

    token = CancelToken()
    ctx: Any = FitContext(seed=1, tracker=_CancelOnChains(token), cancel=token)
    with pytest.raises(RunCancelled):
        plugin.fit(stage, plugin.parse_settings(SETTINGS), _cfg("pymc"), ctx)
