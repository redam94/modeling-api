"""Engine dispatch, per-chain progress, segmented sampling with exact resume."""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_pymc.checkpoint import MemoryCheckpointSink, SamplingCheckpoint
from mapi_pymc.engines import (
    ENGINES,
    ChainProgress,
    EngineHooks,
    EnginePlan,
    engine_available,
    resolve_engine,
    run_engine,
)
from mapi_sdk.errors import RunCancelled
from mapi_sdk.steps import CancelToken
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig

KEY = "feedfacefeedfacefeedfacefeedface"


class CountingToken(CancelToken):
    """Cancels itself on the ``after``-th cooperative check."""

    __slots__ = ("after", "calls")

    def __init__(self, after: int) -> None:
        super().__init__()
        self.after = after
        self.calls = 0

    def check(self) -> None:
        self.calls += 1
        if self.calls >= self.after:
            self.cancel("test")
        super().check()


def _plan(**kw: object) -> EnginePlan:
    base: dict[str, object] = {
        "engine": "pymc",
        "tune": 60,
        "draws": 40,
        "chains": 2,
        "seed": 7,
        "checkpoint_every": 10,
    }
    base.update(kw)
    return EnginePlan(**base)  # type: ignore[arg-type]


def _values(idata: xr.DataTree) -> dict[str, np.ndarray]:
    post = idata["posterior"].to_dataset()
    return {str(k): np.asarray(v.values) for k, v in post.data_vars.items()}


def test_engine_resolution_and_plans() -> None:
    assert set(ENGINES) >= {"nutpie", "pymc", "map", "advi", "pathfinder"}
    assert engine_available("pymc") and engine_available("map")
    assert not engine_available("nope")
    assert resolve_engine(None) == ("nutpie" if engine_available("nutpie") else "pymc")
    assert resolve_engine(None, default="map") == "map"
    with pytest.raises(MapiError) as err:
        resolve_engine("hmc2000")
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    missing = next(e for e in ("pathfinder", "numpyro", "blackjax") if not engine_available(e))
    with pytest.raises(MapiError) as err2:
        resolve_engine(missing)
    assert err2.value.code is ErrorCode.CAPABILITY_UNSUPPORTED

    plan = EnginePlan.from_config(
        FitConfig.model_validate({"engine": "pymc", "draws": 30, "checkpoint": {"every_draws": 5}}),
        seed=4,
    )
    assert (plan.tune, plan.draws, plan.chains, plan.seed, plan.checkpoint_every) == (
        1000,
        30,
        4,
        4,
        5,
    )
    assert plan.mcmc
    advi = EnginePlan.from_config(FitConfig.model_validate({"engine": "advi", "seed": 1}))
    assert advi.tune == 10_000 and not advi.mcmc and advi.seed == 1
    msg = ChainProgress(1, 4, 150, 500, False, 2).message()
    assert msg == "chain 2/4: sampling 150/500, 2 divergences"


def test_segmented_pymc_sampling_reports_per_chain_progress(linear_model: tuple) -> None:
    model = linear_model[0]
    events: list[tuple[float, str]] = []
    sink = MemoryCheckpointSink()
    hooks = EngineHooks(
        progress=lambda f, m: events.append((f, m)), sink=sink, key=KEY, min_interval=0.0
    )
    result = run_engine(model, _plan(), hooks)
    post = result.idata["posterior"].to_dataset()
    assert dict(post.sizes) == {"chain": 2, "draw": 40, "obs": 20}
    assert {"alpha", "beta", "sigma", "mu"} <= set(post.data_vars)
    stats = result.idata["sample_stats"].to_dataset()
    assert "diverging" in stats and stats.sizes["draw"] == 40
    assert result.checkpoints == 3 and [c for _, c in sink.history] == [10, 20, 30]
    assert sink.load(KEY) is None  # cleared once the fit completed
    fractions = [f for f, _ in events]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    messages = [m for _, m in events]
    assert "chain 1/2: tuning 1/60, 0 divergences" in messages
    assert any(m.startswith("chain 2/2: sampling 40/40") for m in messages)
    assert abs(float(post["beta"].mean()) - 2.0) < 0.5


def test_cancelled_fit_resumes_exactly_from_its_checkpoint(linear_model: tuple) -> None:
    model = linear_model[0]
    reference = run_engine(model, _plan(checkpoint_every=None))
    sink = MemoryCheckpointSink()
    logs: list[str] = []
    # checks: tuning 4 per chain (60 draws, every 16), then 1 per chain per 10-draw chunk
    token = CountingToken(after=8 + 2 * 2 + 2)  # cancel during the third chunk's 2nd chain
    with pytest.raises(RunCancelled):
        run_engine(model, _plan(), EngineHooks(cancel=token, sink=sink, key=KEY, log=logs.append))
    saved = sink.load(KEY)
    assert saved is not None and saved.resumable and saved.completed == 20
    assert saved.data["value__beta"].shape == (2, 20)
    resumed = run_engine(model, _plan(), EngineHooks(sink=sink, key=KEY, log=logs.append))
    assert resumed.resumed_from == 20 and "resumed after 20" in resumed.notes[0]
    assert any("resuming from a checkpoint" in line for line in logs)
    ref, got = _values(reference.idata), _values(resumed.idata)
    assert ref.keys() == got.keys()
    for name in ref:
        np.testing.assert_array_equal(ref[name], got[name], err_msg=name)


def test_incompatible_checkpoints_are_ignored(linear_model: tuple) -> None:
    model = linear_model[0]
    sink = MemoryCheckpointSink()
    sink.save(
        SamplingCheckpoint(
            key=KEY, engine="pymc", resumable=True, chains=3, tune=1, draws=5, completed=1
        )
    )
    logs: list[str] = []
    result = run_engine(
        model, _plan(checkpoint_every=None), EngineHooks(sink=sink, key=KEY, log=logs.append)
    )
    assert result.resumed_from == 0
    assert any("incompatible checkpoint" in line for line in logs)


def test_cancel_before_start_and_without_sink(linear_model: tuple) -> None:
    token = CancelToken()
    token.cancel("stop")
    with pytest.raises(RunCancelled):
        run_engine(linear_model[0], _plan(), EngineHooks(cancel=token))
    with pytest.raises(RunCancelled):
        run_engine(linear_model[0], _plan(), EngineHooks(cancel=CountingToken(after=10)))


def test_map_and_advi_engines(linear_model: tuple) -> None:
    model = linear_model[0]
    events: list[str] = []
    m = run_engine(model, _plan(engine="map"), EngineHooks(progress=lambda f, s: events.append(s)))
    post = m.idata["posterior"].to_dataset()
    assert (post.sizes["chain"], post.sizes["draw"]) == (1, 1)
    assert abs(float(post["beta"].squeeze()) - 2.0) < 0.5
    assert m.plan.engine == "map" and not m.plan.mcmc
    a = run_engine(
        model,
        _plan(engine="advi", tune=10_000, draws=30),
        EngineHooks(progress=lambda f, s: events.append(s), min_interval=0.0),
    )
    post_a = a.idata["posterior"].to_dataset()
    assert (post_a.sizes["chain"], post_a.sizes["draw"]) == (1, 30)
    assert abs(float(post_a["beta"].mean()) - 2.0) < 0.7
    assert any(s.startswith("ADVI step") for s in events)
    assert "found the MAP estimate" in events and "ADVI finished" in events


def test_discrete_models_fall_back_to_pm_sample() -> None:
    rng = np.random.default_rng(0)
    with pm.Model() as model:
        z = pm.Bernoulli("z", 0.5)
        mu = pm.Normal("mu", 0, 1)
        pm.Normal("y", mu + z, 1.0, observed=rng.standard_normal(10))
    events: list[str] = []
    result = run_engine(
        model,
        _plan(tune=20, draws=20),
        EngineHooks(progress=lambda f, s: events.append(s), min_interval=0.0),
    )
    post = result.idata["posterior"].to_dataset()
    assert post.sizes["draw"] == 20 and "z" in post
    assert "without segments" in result.notes[0]
    assert any("tuning" in s for s in events)


@pytest.mark.skipif(not engine_available("nutpie"), reason="nutpie is not installed")
def test_nutpie_progress_snapshots_and_cancel(linear_model: tuple) -> None:
    model = linear_model[0]
    events: list[tuple[float, str]] = []
    plain = run_engine(
        model,
        _plan(engine="nutpie", tune=100, draws=100, checkpoint_every=None),
        EngineHooks(progress=lambda f, s: events.append((f, s)), min_interval=0.0),
    )
    post = plain.idata["posterior"].to_dataset()
    assert dict(post.sizes)["draw"] == 100 and set(plain.idata.children) <= {
        "posterior",
        "sample_stats",
    }
    assert events[-1][0] == 1.0

    token = CancelToken()

    class CancelOnSave(MemoryCheckpointSink):
        def save(self, checkpoint: SamplingCheckpoint) -> None:
            super().save(checkpoint)
            token.cancel("after a snapshot")

    sink = CancelOnSave()
    with pytest.raises(RunCancelled):
        run_engine(
            model,
            _plan(engine="nutpie", tune=100, draws=400_000, checkpoint_every=500),
            EngineHooks(cancel=token, sink=sink, key=KEY),
        )
    snap = sink.load(KEY)
    assert snap is not None and snap.engine == "nutpie" and not snap.resumable
    assert snap.completed >= 500 and snap.data["posterior__beta"].sizes["draw"] == snap.completed
    retry = run_engine(
        model, _plan(engine="nutpie", tune=50, draws=50), EngineHooks(sink=sink, key=KEY)
    )
    assert "cannot restore its adaptation" in retry.notes[0]
    assert sink.load(KEY) is None
