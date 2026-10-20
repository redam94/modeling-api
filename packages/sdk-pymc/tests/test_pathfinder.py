"""The ``pathfinder`` engine (extra ``mapi-pymc[pathfinder]``)."""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
from mapi_pymc import engines
from mapi_pymc.engines import EngineHooks, EnginePlan, engine_available, resolve_engine, run_engine
from mapi_spec.errors import ErrorCode, MapiError


def test_pathfinder_engine_is_available() -> None:
    # The workspace installs pymc-extras (mapi-plugin-ts declares it; mapi-pymc has the extra).
    assert engine_available("pathfinder")
    assert resolve_engine("pathfinder") == "pathfinder"


def test_missing_pathfinder_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines, "engine_available", lambda name: name != "pathfinder")
    with pytest.raises(MapiError) as err:
        resolve_engine("pathfinder")
    assert err.value.code == ErrorCode.CAPABILITY_UNSUPPORTED
    assert "mapi-pymc[pathfinder]" in str(err.value)


@pytest.mark.slow
def test_pathfinder_fit_is_one_quiet_chain(capfd: pytest.CaptureFixture[str]) -> None:
    rng = np.random.default_rng(3)
    y = rng.normal(1.5, 0.7, size=40)
    with pm.Model() as model:
        mu = pm.Normal("mu", 0.0, 5.0)
        sigma = pm.HalfNormal("sigma", 2.0)
        pm.Normal("y", mu, sigma, observed=y)
    messages: list[str] = []
    hooks = EngineHooks(progress=lambda _f, msg: messages.append(msg))
    plan = EnginePlan("pathfinder", tune=0, draws=100, chains=1, seed=11)
    capfd.readouterr()
    result = run_engine(model, plan, hooks)
    out = capfd.readouterr().out
    posterior = result.idata["posterior"].to_dataset()
    assert posterior.sizes["chain"] == 1
    assert posterior.sizes["draw"] == 100
    assert set(posterior.data_vars) >= {"mu", "sigma"}
    assert abs(float(posterior["mu"].mean()) - 1.5) < 0.5
    assert result.plan.engine == "pathfinder"
    assert "Pathfinder finished" in messages
    # progressbar=False: no rich progress table on the worker's stdout.
    assert "Pathfinder" not in out
    assert "━" not in out
