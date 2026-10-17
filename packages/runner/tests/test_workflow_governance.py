"""Governance checks inside workflows (M3.5 integration): the runner's check
implementations are the check runners of ``governance.policy`` / ``governance.check`` nodes
(entry point ``mapi.governance_checks``), measured on the real statsmodels ARIMAX of the
canonical readiness workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from mapi_runner.worker.governance import CHECK_IMPLEMENTATIONS
from mapi_runner.worker.governance_checks import check_runners
from mapi_spec.governance import GOVERNANCE_CHECKS, GovernancePolicy, PolicyCheck
from mapi_spec.workflow import WorkflowRunStatus, WorkflowSpec
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX, instantiate_template
from mapi_workflows.runtime.governance import GovernanceServices, load_check_runners
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.testing import test_registry, weekly_series

pytestmark = pytest.mark.filterwarnings("ignore")

PARAMS = {"drivers": ["price"], "n_folds": 4, "gap": 1, "horizons": [1, 4, 8], "max_p": 2,
          "max_q": 2}  # fmt: skip


def _policy(checks: list[PolicyCheck]) -> GovernancePolicy:
    return GovernancePolicy(
        id="pol_01jaf3h1j2k3m4n5p6q7r8s9t0",
        tenant_id="acme",
        project_id=None,
        scope="tenant",
        version=1,
        name="Every check",
        checks=checks,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_the_entry_point_exports_every_check() -> None:
    assert set(check_runners()) == set(CHECK_IMPLEMENTATIONS) == set(GOVERNANCE_CHECKS)
    assert set(GOVERNANCE_CHECKS) <= set(load_check_runners())


def test_the_canonical_workflow_runs_every_check_on_the_real_arimax() -> None:
    policy = _policy([PolicyCheck(check=c) for c in GOVERNANCE_CHECKS])
    gov = GovernanceServices(resolver=lambda _m, _p: [policy])  # runners from the entry point
    spec = instantiate_template(TS_READINESS_ARIMAX, name="weekly", project="growth")
    data = weekly_series(156, random_walk=True, beta=3.0)
    res = LocalRunner(registry=test_registry(), governance=gov).run(
        spec, inputs={"data": data}, params=PARAMS
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    model = res.values["model"]
    assert model.bundle.model.manifest.id == "ts.sarimax"  # type: ignore[union-attr]
    ev = res.values["evidence"]
    assert isinstance(ev, dict)
    results = {r["check"]: r for r in ev["results"]}
    assert set(results) == set(GOVERNANCE_CHECKS)
    # every check either measured the model or said plainly why it does not apply
    assert not [c for c, r in results.items() if r["status"] == "error"], results
    assert results["residuals.whiteness"]["status"] == "pass"
    assert results["convergence"]["status"] == "pass"
    assert results["null.placebo_time_shift"]["status"] == "pass"  # the price effect is real
    assert results["sensitivity.prior"]["status"] == "skipped"  # not Bayesian
    for r in results.values():
        assert r["message_plain"]


def _null_test_workflow() -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "null-test", "project": "growth"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [
                {"id": "holdout", "use": "split.holdout", "inputs": {"data": "inputs.data"},
                 "with": {"split": {"kind": "time_holdout", "dim": "time", "last": 16}}},
                {"id": "fit", "use": "model.fit",
                 "inputs": {"data": "inputs.data", "split": "holdout.plan"},
                 "with": {"model": "ts.sarimax@^1",
                          "settings": {"order": [1, 0, 0], "exog": ["price"]}}},
                {"id": "null", "use": "governance.check",
                 "inputs": {"model": "fit.model", "data": "inputs.data"},
                 "with": {"check": "null.permutation_target",
                          "params": {"n_permutations": 19, "metric": "mae"}}},
            ],
            "outputs": {"evidence": "null.evidence"},
        }
    )  # fmt: skip


def _null_result(data: Any) -> dict[str, Any]:
    res = LocalRunner(registry=test_registry()).run(_null_test_workflow(), inputs={"data": data})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    ev = res.values["evidence"]
    assert isinstance(ev, dict)
    (r,) = ev["results"]
    return dict(r)


@pytest.mark.slow
def test_the_permutation_null_test_tells_signal_from_noise() -> None:
    """A permutation null test inside a workflow: the ARIMAX with a real price effect beats
    its own fits with the price shuffled in time (the sales stay as they are); fitted to
    pure noise it does not. The price varies enough from week to week for its effect to
    matter on the held-out weeks (a slowly drifting price adds little forecast skill
    beyond the sales' own dynamics, and the test then rightly finds no difference)."""
    signal = weekly_series(160, phi=0.5, beta=6.0, seed=3)
    price = 10 + np.random.default_rng(4).normal(0, 1.0, signal.sizes["time"])
    signal["sales"] = signal["sales"] - 6.0 * (price - signal["price"])
    signal["price"] = ("time", price)
    real = _null_result(signal)
    assert real["status"] == "pass", real
    assert real["value"] is not None and real["value"] <= 0.05  # p = 1/(K+1) at best

    noise = signal.copy()
    rng = np.random.default_rng(5)
    noise["sales"] = ("time", rng.normal(100, 1, signal.sizes["time"]))
    null = _null_result(noise)
    assert null["status"] == "fail", null
    assert null["value"] is not None and null["value"] > 0.1
