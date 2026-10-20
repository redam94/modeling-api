"""Engine tracking: live chains, step metrics, the ADVI loss stream, the PyMC nutpie patch."""

from __future__ import annotations

import sys
import types
from typing import Any

import pymc as pm
import pytest
from mapi_pymc import engines
from mapi_pymc.engines import (
    MAX_HISTORY_POINTS,
    ChainProgress,
    EngineHooks,
    EnginePlan,
    engine_available,
    nutpie_progress,
    pymc_draw_callback,
    pymc_nutpie_progress,
    run_engine,
)
from mapi_sdk.models import FitContext
from mapi_sdk.tracking import MetricRecorder
from mapi_spec import run as spec_run


def _ctx(recorder: MetricRecorder | None) -> FitContext:
    return FitContext(seed=0, tracker=recorder)


def _series(rec: MetricRecorder) -> dict[tuple[str, str], list[tuple[int | None, float]]]:
    return rec.series()


def test_for_context_wires_the_tracker() -> None:
    rec = MetricRecorder()
    ctx = _ctx(rec)
    hooks = EngineHooks.for_context(ctx, progress=None)
    assert hooks.tracker is not None and hooks.cancel is ctx.cancel
    hooks.metrics({"step_size": 0.5, "divergences": float("nan")}, step=3, labels={"chain": "0"})
    hooks.chains([ChainProgress(0, 1, 3, 10, True, 0, 0.5, 7)])
    assert _series(rec) == {("step_size", "chain=0"): [(3, 0.5)]}  # NaN dropped by the mixin
    (chain,) = rec.last_chains
    assert isinstance(chain, spec_run.ChainProgress)
    assert (chain.draws_done, chain.draws_total, chain.step_size, chain.num_steps) == (
        3,
        10,
        0.5,
        7,
    )
    hooks.tracker.flush()
    with pytest.raises(ValueError):  # keys are validated through the context
        hooks.metrics({"Bad-Key": 1.0}, step=1)

    off = EngineHooks.for_context(_ctx(None), progress=None)
    assert off.tracker is None
    off.metrics({"loss": 1.0}, step=1)  # no-op
    off.chains([ChainProgress(0, 1, 1, 2, False)])


def test_to_spec_clamps_bad_step_size() -> None:
    spec = ChainProgress(1, 2, 5, 10, False, 1, float("inf"), None).to_spec()
    assert spec.step_size is None and spec.chain == 1 and spec.divergences == 1


def test_pymc_engine_streams_step_metrics_and_chains(linear_model: tuple) -> None:
    rec = MetricRecorder()
    hooks = EngineHooks.for_context(_ctx(rec), progress=None)
    plan = EnginePlan("pymc", tune=50, draws=50, chains=2, seed=3)
    run_engine(linear_model[0], plan, hooks)
    series = _series(rec)
    for c in ("0", "1"):
        depth = series[("tree_depth", f"chain={c}")]
        assert len(depth) == 100 and depth[0][0] == 1 and depth[-1][0] == 100
        assert {("step_size", f"chain={c}"), ("num_steps", f"chain={c}")} <= set(series)
        divs = [v for _, v in series[("divergences", f"chain={c}")]]
        assert divs == sorted(divs)  # cumulative
    assert len(rec.last_chains) == 2
    assert all(c.draws_done == 100 and not c.tuning for c in rec.last_chains)


def test_pymc_engine_thins_long_chains_under_the_series_budget(
    linear_model: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain with more draws than MAX_HISTORY_POINTS logs every k-th draw and the last
    one, so a long fit stays under the per-series budget and keeps the end of sampling."""
    monkeypatch.setattr(engines, "MAX_HISTORY_POINTS", 30)
    rec = MetricRecorder()
    hooks = EngineHooks.for_context(_ctx(rec), progress=None)
    run_engine(linear_model[0], EnginePlan("pymc", tune=50, draws=50, chains=1, seed=3), hooks)
    steps = [s for s, _ in _series(rec)[("tree_depth", "chain=0")]]
    assert steps[:3] == [4, 8, 12] and steps[-1] == 100 and len(steps) == 25
    assert rec.last_chains[0].draws_done == 100


class _Draw:
    def __init__(self, chain: int, step_size: float, diverging: bool = False) -> None:
        self.chain = chain
        self.tuning = False
        self.stats = [
            {"diverging": diverging, "step_size": step_size, "tree_size": 7.0, "depth": 3}
        ]


def test_pymc_draw_callback_logs_the_engine_step_metrics() -> None:
    """The ``pm.sample`` callback a library such as mmm-framework uses gives the same
    chains and step metrics as the ``pymc`` engine (step size, steps, depth, divergences)."""
    rec = MetricRecorder()
    reports: list[str] = []
    hooks = EngineHooks.for_context(_ctx(rec), progress=lambda f, m: reports.append(m))
    callback = pymc_draw_callback(hooks, EnginePlan("pymc", tune=1, draws=2, chains=2, seed=0))
    for i in range(3):
        for c in range(2):
            callback(None, _Draw(c, 0.5 + c, diverging=i == 1))
    callback(None, _Draw(5, 0.1))  # not a chain of the plan: ignored
    series = _series(rec)
    assert series[("step_size", "chain=1")] == [(1, 1.5), (2, 1.5), (3, 1.5)]
    assert series[("num_steps", "chain=0")][-1] == (3, 7.0)
    assert series[("tree_depth", "chain=0")][-1] == (3, 3.0)
    assert [v for _, v in series[("divergences", "chain=0")]] == [0.0, 1.0, 1.0]
    assert rec.last_chains and rec.last_chains[0].step_size == 0.5  # shown in the lanes
    assert reports and reports[-1].startswith("chain ")


def test_compound_pymc_reports_chains() -> None:
    with pm.Model() as model:
        k = pm.Poisson("k", 3.0)
        pm.Normal("x", k, 1.0, observed=[2.0, 3.0])
    rec = MetricRecorder()
    hooks = EngineHooks.for_context(_ctx(rec), progress=None)
    run_engine(model, EnginePlan("pymc", tune=20, draws=20, chains=2, seed=1), hooks)
    assert [c.draws_done for c in rec.last_chains] == [40, 40]


def test_advi_streams_a_thinned_loss() -> None:
    with pm.Model() as model:
        mu = pm.Normal("mu", 0.0, 1.0)
        pm.Normal("y", mu, 1.0, observed=[0.1, -0.2, 0.3])
    rec = MetricRecorder()
    hooks = EngineHooks.for_context(_ctx(rec), progress=None)
    run_engine(model, EnginePlan("advi", tune=2000, draws=20, chains=1, seed=0), hooks)
    loss = _series(rec)[("loss", "")]
    assert 0 < len(loss) <= MAX_HISTORY_POINTS and loss[-1][0] == 2000 and loss[0][0] == 1


def test_advi_every_kth_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines, "MAX_HISTORY_POINTS", 300)
    with pm.Model() as model:
        mu = pm.Normal("mu", 0.0, 1.0)
        pm.Normal("y", mu, 1.0, observed=[0.1])
    rec = MetricRecorder()
    hooks = EngineHooks.for_context(_ctx(rec), progress=None)
    run_engine(model, EnginePlan("advi", tune=1000, draws=5, chains=1, seed=0), hooks)
    steps = [s for s, _ in _series(rec)[("loss", "")]]
    assert steps[:3] == [4, 8, 12] and steps[-1] == 1000 and len(steps) == 250


def test_no_tracker_builds_no_metrics(linear_model: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    def spy(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("step metrics built without a tracker")

    monkeypatch.setattr(engines._LiveChains, "draw", spy)
    monkeypatch.setattr(EngineHooks, "metrics", spy)
    monkeypatch.setattr(ChainProgress, "to_spec", spy)
    hooks = EngineHooks.for_context(_ctx(None), progress=None)
    run_engine(linear_model[0], EnginePlan("pymc", tune=10, draws=10, chains=1, seed=0), hooks)
    with pm.Model() as model:
        mu = pm.Normal("mu", 0.0, 1.0)
        pm.Normal("y", mu, 1.0, observed=[0.1])
    run_engine(model, EnginePlan("advi", tune=100, draws=5, chains=1, seed=0), hooks)


class _Progress:
    def __init__(self, done: int, total: int = 20, step_size: float = 0.4) -> None:
        self.finished_draws = done
        self.total_draws = total
        self.tuning = done < 10
        self.divergences = 0
        self.step_size = step_size
        self.latest_num_steps = 3
        self.started = True


def test_nutpie_progress_dedupes_on_finished_draws() -> None:
    rec = MetricRecorder()
    events: list[tuple[float, str]] = []
    hooks = EngineHooks.for_context(_ctx(rec), progress=lambda f, m: events.append((f, m)))
    hooks.min_interval = 0.0
    handle = nutpie_progress(hooks)
    handle([_Progress(2), _Progress(1)])
    handle([_Progress(2, step_size=9.0), _Progress(3)])  # chain 0 did not move
    series = _series(rec)
    assert series[("step_size", "chain=0")] == [(2, 0.4)]
    assert series[("step_size", "chain=1")] == [(1, 0.4), (3, 0.4)]
    assert series[("num_steps", "chain=1")][-1] == (3, 3.0)
    assert [c.draws_done for c in rec.last_chains] == [2, 3]
    assert events[-1] == (5 / 40, "chain 1/2: tuning 2/20, 0 divergences")
    handle([object()])  # malformed: logged, never raised


def test_pymc_nutpie_progress_patches_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    class Base:
        def __init__(self) -> None:
            self.updates: list[Any] = []

        def update(self, chain_progresses: Any) -> None:
            self.updates.append(chain_progresses)

    fake = types.ModuleType("pymc.sampling.mcmc")
    fake.NutpieProgressBarManager = Base  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymc.sampling.mcmc", fake)
    seen: list[list[Any]] = []

    def boom(progress: list[Any]) -> None:
        seen.append(progress)
        raise RuntimeError("callback errors are logged, not raised")

    with pymc_nutpie_progress(boom):
        manager = fake.NutpieProgressBarManager()  # type: ignore[attr-defined]
        assert manager is not None and isinstance(manager, Base)
        manager.update((1, 2))
        assert seen == [[1, 2]] and manager.updates == [(1, 2)]
    assert fake.NutpieProgressBarManager is Base  # type: ignore[attr-defined]

    with pytest.raises(KeyError), pymc_nutpie_progress(seen.append):
        raise KeyError("restored on error too")
    assert fake.NutpieProgressBarManager is Base  # type: ignore[attr-defined]

    del fake.NutpieProgressBarManager  # type: ignore[attr-defined]
    with pymc_nutpie_progress(seen.append):
        assert not hasattr(fake, "NutpieProgressBarManager")


def test_pymc_nutpie_progress_real_module() -> None:
    import pymc.sampling.mcmc as mcmc

    original = mcmc.NutpieProgressBarManager
    with pymc_nutpie_progress(lambda p: None):
        assert mcmc.NutpieProgressBarManager is not original
        assert issubclass(mcmc.NutpieProgressBarManager, original)
    assert mcmc.NutpieProgressBarManager is original


@pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed")
def test_nutpie_engine_streams_chains_and_step_metrics(linear_model: tuple) -> None:
    rec = MetricRecorder()
    hooks = EngineHooks.for_context(_ctx(rec), progress=None)
    run_engine(linear_model[0], EnginePlan("nutpie", tune=100, draws=100, chains=2, seed=1), hooks)
    series = _series(rec)
    for c in ("0", "1"):
        assert series[("step_size", f"chain={c}")]
        assert series[("divergences", f"chain={c}")]
        steps = [s for s, _ in series[("step_size", f"chain={c}")]]
        assert steps == sorted(set(steps)) and steps[-1] <= 200
    assert len(rec.last_chains) == 2
