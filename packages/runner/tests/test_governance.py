"""Governance checks in the sandbox (M3.5): every catalog check is implemented, they
discriminate a model with real signal from one fitted on noise, they are model-family aware
(machine learning, Bayesian), the refit budget is honoured, and a governance run writes a
valid ``governance.json``."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from gov_fixtures import (
    SCHEMA,
    FakeCtx,
    bayes_registry,
    bayes_spec,
    fit,
    linear_data,
    linear_spec,
)
from mapi_runner.worker.governance import CHECK_IMPLEMENTATIONS, GovernanceEnv, measure
from mapi_runner.worker.refit import Refitter
from mapi_sdk.registry import default_registry
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    CheckStatus,
    PolicyCheck,
    RefitBudget,
)
from mapi_spec.schema import DataSchema
from mapi_spec.scorecard import CheckThresholds

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore::FutureWarning")]

FAST = {
    "null.permutation_target": {"n_permutations": 19},
    "null.noise_regressors": {"n_noise": 3},
    "recovery.simulated": {"n_sims": 5},
}


def _env(signal: bool, *, budget: RefitBudget | None = None) -> GovernanceEnv:
    reg = default_registry()
    data = linear_data(signal=signal)
    b = fit(linear_spec(), data, reg)
    return GovernanceEnv(b, data, reg, Refitter(b, reg, budget or RefitBudget(max_refits=200)))


def _run(env: GovernanceEnv, check: str, **params: object) -> tuple[CheckStatus, float | None, str]:
    r = measure(env, PolicyCheck(check=check, params={**FAST.get(check, {}), **params}))  # type: ignore[arg-type]
    assert r.message_plain and r.check == check and r.metric == GOVERNANCE_CHECKS[check].metric
    if r.status is not CheckStatus.PASS:
        assert r.status is CheckStatus.SKIPPED or r.suggested_action, check
    return r.status, r.value, r.message_plain


def test_every_catalog_check_is_implemented() -> None:
    assert set(CHECK_IMPLEMENTATIONS) == set(GOVERNANCE_CHECKS)


@pytest.fixture(scope="module")
def signal() -> GovernanceEnv:
    return _env(True)


@pytest.fixture(scope="module")
def noise() -> GovernanceEnv:
    return _env(False)


def test_the_model_family_is_read_from_the_bundle(signal: GovernanceEnv) -> None:
    assert signal.kind == "ml" and signal.family == "sklearn"
    assert signal.has() == {"holdout", "time_series", "residuals"}
    assert signal.drivers() == ["spend"]


def test_null_tests_tell_signal_from_noise(signal: GovernanceEnv, noise: GovernanceEnv) -> None:
    st, p, msg = _run(signal, "null.permutation_target")
    assert st is CheckStatus.PASS and p == pytest.approx(1 / 20) and "shuffled" in msg
    st, p, _ = _run(noise, "null.permutation_target")
    assert st is CheckStatus.FAIL and p is not None and p > 0.1
    st, share, _ = _run(signal, "null.noise_regressors")
    assert st is CheckStatus.PASS and share is not None and share < 0.02
    st, ratio, _ = _run(signal, "null.placebo_time_shift")
    assert st is CheckStatus.PASS and ratio is not None and ratio < 0.25
    st, ratio, _ = _run(noise, "null.placebo_time_shift")
    assert st is not CheckStatus.PASS


def test_benchmarks_residuals_accuracy_and_data(
    signal: GovernanceEnv, noise: GovernanceEnv
) -> None:
    st, skill, msg = _run(signal, "baseline.beats_naive")
    assert st is CheckStatus.PASS and skill is not None and skill > 0.5 and "holdout" in msg
    st, p, _ = _run(signal, "residuals.whiteness")
    assert st is CheckStatus.PASS and p is not None and p > 0.05
    st, mape, _ = _run(signal, "holdout.accuracy")
    assert st is CheckStatus.PASS and mape is not None and mape < 0.05
    st, mape, _ = _run(noise, "holdout.accuracy")
    assert st is CheckStatus.FAIL
    r = measure(
        signal,
        PolicyCheck(
            check="holdout.accuracy",
            params={"metric": "rmse"},
            thresholds=CheckThresholds(warn=0.1, fail=0.2),
        ),
    )
    assert r.status is CheckStatus.FAIL and r.value == pytest.approx(
        signal.bundle.metrics.values["rmse"]
    )
    st, issues, _ = _run(signal, "data.quality")
    assert st is CheckStatus.PASS and issues == 0
    st, _v, _m = _run(signal, "convergence")
    assert st is CheckStatus.PASS


def test_stability_and_signs(signal: GovernanceEnv, noise: GovernanceEnv) -> None:
    st, var, _ = _run(signal, "stability.rolling_params")
    assert st is CheckStatus.PASS and var is not None and var < 0.25
    st, _var, _ = _run(noise, "stability.rolling_params")
    assert st is CheckStatus.FAIL
    st, wrong, _ = _run(signal, "plausibility.signs")
    assert st is CheckStatus.PASS and wrong == 0.0
    st, wrong, msg = _run(signal, "plausibility.signs", expected={"price": "positive"})
    assert st is CheckStatus.FAIL and wrong == 1.0 and "price" in msg


def test_checks_that_do_not_apply_are_skipped(signal: GovernanceEnv) -> None:
    st, _v, msg = _run(signal, "sensitivity.prior")
    assert st is CheckStatus.SKIPPED and "Bayesian" in msg
    st, _v, msg = _run(signal, "recovery.simulated")
    assert st is CheckStatus.SKIPPED
    reg = default_registry()
    data = linear_data()
    b = fit(linear_spec(split=False), data, reg)
    env = GovernanceEnv(b, data, reg, Refitter(b, reg))
    for check in ("null.permutation_target", "baseline.beats_naive", "holdout.accuracy"):
        st, _v, msg = _run(env, check)
        assert st is CheckStatus.SKIPPED and "test period" in msg


def test_the_refit_budget_turns_into_an_error_result() -> None:
    env = _env(True, budget=RefitBudget(max_refits=3))
    r = measure(env, PolicyCheck(check="null.permutation_target", params={"n_permutations": 20}))
    assert r.status is CheckStatus.ERROR and "budget" in r.message_plain
    assert env.refitter.used <= 3


def test_noise_regressors_extend_a_channel_dim() -> None:
    import pandas as pd

    rng = np.random.default_rng(0)
    n = 80
    t = pd.date_range("2022-01-02", periods=n, freq="W-SUN")
    spend = np.abs(rng.normal(size=(n, 2))) * 10
    y = 5 + spend @ np.array([1.0, 0.5]) + rng.normal(scale=0.5, size=n)
    data = xr.Dataset(
        {"y": ("time", y), "spend": (("time", "channel"), spend)},
        coords={"time": t, "channel": ["tv", "search"]},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {
                "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "channel": {"kind": "categorical"},
            },
            "vars": {
                "y": {"dims": ["time"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["time", "channel"], "dtype": "float64", "role": "spend"},
            },
        }
    )
    reg = default_registry()
    b = fit(linear_spec(), data, reg, schema)
    env = GovernanceEnv(b, data, reg, Refitter(b, reg))
    assert env.channel_dim() == ("channel", ["spend"]) and "channels" in env.has()
    r = measure(env, PolicyCheck(check="null.noise_regressors", params={"n_noise": 2}))
    assert r.status is CheckStatus.PASS, r.message_plain
    assert {e.name for e in r.evidence} >= {"noise_effect_share", "placebo"}
    placebo = next(e for e in r.evidence if e.name == "placebo")
    assert placebo.inline["how"] == "channels on 'channel'"  # type: ignore[index]
    r = measure(env, PolicyCheck(check="plausibility.signs"))
    assert r.status is CheckStatus.PASS
    shares = next(e for e in r.evidence if e.name == "wrong_sign_share").inline
    assert set(shares) == {"channel=tv", "channel=search"}  # type: ignore[arg-type]


def test_run_governance_writes_a_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mapi_runner.worker import data as data_mod
    from mapi_runner.worker import governance as gov
    from mapi_spec.governance import GOVERNANCE_OUTPUT, GovernanceJob, GovernanceReport

    reg = default_registry()
    data = linear_data()
    b = fit(linear_spec(), data, reg)
    monkeypatch.setattr(data_mod, "load_bundle", lambda ctx, *a: b)
    monkeypatch.setattr(data_mod, "load_dataset", lambda ctx, *a: data)
    monkeypatch.setattr(data_mod, "load_side_tables", lambda ctx: {})
    ctx = FakeCtx(tmp_path, reg)
    job = GovernanceJob(
        model_ref="model://p/lin@v1",
        checks=[
            PolicyCheck(check="data.quality"),
            PolicyCheck(check="convergence"),
            PolicyCheck(check="sensitivity.prior"),
            PolicyCheck(check="null.permutation_target", params={"n_permutations": 19}),
        ],
        budget=RefitBudget(max_refits=40),
    )
    report = gov.run_governance(ctx, job)
    stored = GovernanceReport.model_validate(ctx.written[GOVERNANCE_OUTPUT])
    assert stored == report
    assert [r.check for r in report.results] == [c.check for c in job.checks]
    assert [r.status for r in report.results] == [
        CheckStatus.PASS,
        CheckStatus.PASS,
        CheckStatus.SKIPPED,
        CheckStatus.PASS,
    ]
    assert report.model_kind == "ml" and report.refits == 20
    assert SCHEMA.vars["y"].role.value == "target"


@pytest.mark.slow
def test_bayesian_checks_use_draws_priors_and_simulations() -> None:
    reg = bayes_registry()
    data = linear_data()
    b = fit(bayes_spec(draws=60, tune=60), data, reg)
    env = GovernanceEnv(
        b, data, reg, Refitter(b, reg, RefitBudget(max_refits=40, draws=40, tune=40))
    )
    assert env.kind == "bayesian"
    r = measure(env, PolicyCheck(check="sensitivity.prior", params={"scales": [0.5, 2.0]}))
    assert r.status is CheckStatus.PASS, r.message_plain
    assert next(e for e in r.evidence if e.name == "prior_parameters_scaled").value == 2.0
    r = measure(env, PolicyCheck(check="recovery.simulated", params={"n_sims": 5}))
    assert r.status in (CheckStatus.PASS, CheckStatus.WARN) and r.value is not None
    r = measure(env, PolicyCheck(check="convergence"))
    assert r.metric == "r_hat_max" and r.value is not None and r.value >= 1.0
    r = measure(
        env, PolicyCheck(check="plausibility.signs", params={"expected": {"b_spend": "positive"}})
    )
    assert r.status is CheckStatus.PASS and r.value == 0.0
    r = measure(env, PolicyCheck(check="null.noise_regressors"))
    assert r.status is CheckStatus.SKIPPED and "fixed set of drivers" in r.message_plain
    refit = env.refitter.spec(split=None)
    assert refit.model.fit.draws == 40 and refit.model.fit.tune == 40
    assert refit.model.fit.mode.value == "train_only"
