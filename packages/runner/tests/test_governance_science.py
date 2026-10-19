"""``recovery.simulated`` on science models with real tiny fits (M5): count targets
(``sci.lgcp_grid``, ``sci.areal_bym2``) and a 0/1 graph (``sci.network_latent_space``).

The check simulates each dataset from the model's own predictive draws, so the refits see
whole counts and 0/1 ties. Before, it added residuals to the fitted values: the refits of the
count models failed (``error``), and the network model gave no prediction (``skipped``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_runner.worker.governance import GovernanceEnv, measure
from mapi_runner.worker.modelview import bundle_kind as env_kind
from mapi_runner.worker.refit import Refitter
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import default_registry
from mapi_spec.governance import CheckStatus, PolicyCheck, RefitBudget

pytest.importorskip("mapi_plugin_science")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::UserWarning"),
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
]


def _lgcp() -> tuple[Any, Any, str, xr.Dataset]:
    from mapi_plugin_science.lgcp import LGCPGridModel
    from mapi_plugin_science.lgcp_data import LGCP_SCHEMA, LGCP_SPEC_YAML, lgcp_dataset

    return LGCPGridModel, LGCP_SCHEMA, LGCP_SPEC_YAML, lgcp_dataset(seed=0)


def _areal() -> tuple[Any, Any, str, xr.Dataset]:
    from mapi_plugin_science.areal import ArealBYM2Model
    from mapi_plugin_science.areal_data import AREAL_SCHEMA, AREAL_SPEC_YAML, areal_dataset

    return ArealBYM2Model, AREAL_SCHEMA, AREAL_SPEC_YAML, areal_dataset(seed=0)


def _network() -> tuple[Any, Any, str, xr.Dataset]:
    from mapi_plugin_science.network import NetworkLatentSpaceModel
    from mapi_plugin_science.network_data import (
        NETWORK_SCHEMA,
        NETWORK_SPEC_YAML,
        network_dataset,
    )

    return NetworkLatentSpaceModel, NETWORK_SCHEMA, NETWORK_SPEC_YAML, network_dataset(seed=0)


CASES: dict[str, Callable[[], tuple[Any, Any, str, xr.Dataset]]] = {
    "sci.lgcp_grid": _lgcp,
    "sci.areal_bym2": _areal,
    "sci.network_latent_space": _network,
}


def _env(model_id: str) -> tuple[GovernanceEnv, Bundle]:
    model, schema, spec, data = CASES[model_id]()
    reg = default_registry().copy()
    reg.register_model(model, replace=True)
    b = fit_spec(spec, data, registry=reg, raw_schema=schema)
    assert env_kind(b) == "bayesian"
    budget = RefitBudget(max_refits=10, draws=60, tune=60)
    return GovernanceEnv(b, data, reg, Refitter(b, reg, budget)), b


@pytest.mark.parametrize("model_id", sorted(CASES))
def test_recovery_refits_whole_simulated_data(
    model_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _b = _env(model_id)
    y = env.targets[0]
    preds = env.predictions()  # the network's output ``tie`` comes back as ``cowork``
    assert [p.target for p in preds][:1] == [y]
    seen: list[np.ndarray] = []
    fit = env.refitter.fit

    def spy(data: xr.Dataset, spec: Any) -> Bundle:
        seen.append(np.asarray(data[y].values, dtype=float))
        return fit(data, spec)

    monkeypatch.setattr(env.refitter, "fit", spy)
    r = measure(env, PolicyCheck(check="recovery.simulated", params={"n_sims": 5}))
    assert r.status in (CheckStatus.PASS, CheckStatus.WARN, CheckStatus.FAIL), (
        r.message_plain,
        r.message_expert,
    )
    assert r.value is not None and 0.0 <= r.value <= 1.0
    assert r.message_expert is not None and "posterior predictive" in r.message_expert
    assert next(e for e in r.evidence if e.name == "simulations").value == 5.0
    assert not any(e.name == "failed_simulations" for e in r.evidence)
    assert len(seen) == 5
    for sim in seen:
        finite = sim[np.isfinite(sim)]
        assert finite.size and np.all(finite == np.rint(finite)) and finite.min() >= 0
        if model_id == "sci.network_latent_space":
            assert set(np.unique(finite)) <= {0.0, 1.0}
