"""The Model Explorer and the governance checks on every plugin family (M3.5 integration).

Each conformance fixture is fitted at size ``tiny`` and explored with every Model Explorer
section (as an ``explore`` run computes them), then measured by the governance checks that
need no refits. No section and no check may fail with an error: each one either produces
its view or result, or says plainly why it does not apply to the model. The families:
statsmodels (forecast, ts_multivariate), mmm-framework (Bayesian and frequentist), PyMC time
series, tabular (GLM, gradient boosting, scikit-learn) and choice models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from gov_fixtures import FakeCtx
from mapi_conformance.runner import load_fixture
from mapi_runner.worker.explore import write_sections
from mapi_runner.worker.explore_model import MODEL_SECTION_FUNCS, ModelContext
from mapi_runner.worker.governance import GovernanceEnv, measure
from mapi_runner.worker.refit import Refitter
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import default_registry
from mapi_spec.explore import ModelExploreRequest
from mapi_spec.governance import CheckStatus, PolicyCheck, RefitBudget

pytestmark = pytest.mark.filterwarnings("ignore")

_SLOW = pytest.mark.slow
BASE = {"params", "diagnostics", "fit", "residuals"}
TIME = BASE | {"forecast", "backtest"}
EXPECTED: dict[str, tuple[str, set[str], tuple[Any, ...]]] = {
    # fixture: (model family, sections that must produce a view, pytest marks)
    "sm_arimax": ("forecast", TIME, ()),
    "sm_vecm_macro": ("ts_multivariate", TIME | {"irf", "fevd"}, ()),
    "mmm_frequentist": ("mmm", TIME | {"decomposition"}, ()),
    "xgb_store_demand": ("tabular", TIME - {"params"}, ()),
    "sklearn_store_demand": ("sklearn", TIME - {"params"}, ()),
    "e84_lite": ("choice", {"params", "diagnostics"}, ()),
    "e02": ("tabular", BASE | {"backtest"}, (_SLOW,)),
    "c08": ("ts", TIME, (_SLOW,)),
    "e24": ("ts", TIME, (_SLOW,)),
    "e33": ("mmm", TIME | {"decomposition", "response_curves"}, (_SLOW,)),
    "guided_mmm": ("mmm", TIME | {"decomposition", "response_curves"}, (_SLOW,)),
    "e07_lgcp": ("sci", {"params", "diagnostics", "fields"}, (_SLOW,)),
    "e50_kpp": ("sci", {"params", "diagnostics", "fields"}, (_SLOW,)),
    "e51_heat_plate": ("sci", {"params", "diagnostics", "fields"}, (_SLOW,)),
    "e60_traction": ("sci", {"params", "diagnostics", "fields"}, ()),
    "e80_lens": ("sci", {"params", "diagnostics", "fields"}, (_SLOW,)),
}
CHEAP_CHECKS = (
    "convergence",
    "data.quality",
    "residuals.whiteness",
    "holdout.accuracy",
    "baseline.beats_naive",
    "plausibility.signs",
)


def test_every_conformance_fixture_is_covered() -> None:
    from mapi_conformance.fixtures import FIXTURES

    assert set(EXPECTED) == set(FIXTURES)


@pytest.mark.parametrize(
    "fixture_id",
    [pytest.param(k, marks=v[2]) for k, v in EXPECTED.items()],
)
def test_explorer_and_governance_on_every_family(fixture_id: str, tmp_path: Path) -> None:
    family, must, _marks = EXPECTED[fixture_id]
    fx = load_fixture(fixture_id)
    data, tables = fx.generate(0, "tiny")
    spec = fx.spec("tiny")
    reg = default_registry().copy()
    hook = fx.hook("registry")
    if callable(hook):
        reg = hook(reg)
    bundle = fit_spec(spec, data, registry=reg, raw_schema=fx.raw_schema(data), side_tables=tables)
    assert bundle.model.manifest.model_family == family

    budget = RefitBudget(max_refits=6, draws=50, tune=50, chains=1)
    req = ModelExploreRequest.model_validate(
        {
            "backtest": {"kind": "expanding_window", "horizons": [1, 2], "n_folds": 2,
                         "min_train": 20},
            "horizon": 4,
        }
    )  # fmt: skip
    context = ModelContext(
        bundle,
        data,
        reg,
        req,
        ref=f"model://conformance/{fixture_id}@v1",
        side_tables=tables,
        refitter=Refitter(bundle, reg, budget, side_tables=tables),
    )
    ctx = FakeCtx(tmp_path, reg)
    sections = [s for s in MODEL_SECTION_FUNCS if s != "comparison"]
    index = write_sections(
        ctx,  # type: ignore[arg-type]
        MODEL_SECTION_FUNCS,
        sections,
        context,
        target="model",
        ref=context.ref,
    )
    status = {s.section: s.status for s in index.sections}
    assert not [s for s, st in status.items() if st == "error"], (status, ctx.warnings)
    ok = {s for s, st in status.items() if st == "ok"}
    assert must <= ok, (must - ok, status)
    for s in index.sections:
        if s.status == "skipped":
            assert s.message_plain  # a plain reason the section does not apply

    env = GovernanceEnv(
        bundle,
        data,
        reg,
        Refitter(bundle, reg, budget, side_tables=tables),
        side_tables=tables,
    )
    results = {c: measure(env, PolicyCheck(check=c)) for c in CHEAP_CHECKS}
    errors = {c: r.message_plain for c, r in results.items() if r.status is CheckStatus.ERROR}
    assert not errors, errors
    assert results["convergence"].status is not CheckStatus.SKIPPED
    assert results["data.quality"].status is not CheckStatus.SKIPPED


def test_statsmodels_views_come_from_the_plugin(tmp_path: Path) -> None:
    """The statsmodels plugin computes its own parameter and IRF/FEVD views
    (``explorer_views``); the explorer prefers them over the generic sections."""
    fx = load_fixture("sm_vecm_macro")
    data, _tables = fx.generate(0, "tiny")
    reg = default_registry()
    bundle = fit_spec(fx.spec("tiny"), data, registry=reg, raw_schema=fx.raw_schema(data))
    context = ModelContext(bundle, data, reg)
    own = context.plugin_views()
    assert {"params", "irf", "fevd"} <= set(own), context.plugin_error
    assert MODEL_SECTION_FUNCS["irf"](context) is own["irf"]
    assert MODEL_SECTION_FUNCS["params"](context) is own["params"]


@pytest.mark.slow
@pytest.mark.parametrize("fixture_id", ["mmm_frequentist", "sm_arimax", "xgb_store_demand"])
def test_null_tests_run_on_mmm_framework_statsmodels_and_tabular(fixture_id: str) -> None:
    """The null tests (fake noise drivers, time-shifted drivers, shuffled targets) refit the
    model and measure it: they produce a result on every family, never an error."""
    fx = load_fixture(fixture_id)
    data, tables = fx.generate(0, "tiny")
    reg = default_registry()
    bundle = fit_spec(
        fx.spec("tiny"), data, registry=reg, raw_schema=fx.raw_schema(data), side_tables=tables
    )
    env = GovernanceEnv(
        bundle,
        data,
        reg,
        Refitter(bundle, reg, RefitBudget(max_refits=40), side_tables=tables),
        side_tables=tables,
        seed=3,
    )
    checks = [
        PolicyCheck(check="null.noise_regressors", params={"n_noise": 2}),
        PolicyCheck(check="null.placebo_time_shift", params={"shifts": [-8, 8]}),
        PolicyCheck(check="null.permutation_target", params={"n_permutations": 9}),
    ]
    for check in checks:
        r = measure(env, check)
        assert r.status is not CheckStatus.ERROR, (check.check, r.message_plain, r.message_expert)
        assert r.message_plain
