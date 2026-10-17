"""The canonical example of the scope, end to end in-process: unit-root tests -> branches
-> differencing -> ACF/PACF and an AIC search of AR/MA orders -> an expanding-window
backtest with a gap and several horizons -> ARIMAX vs naive benchmarks -> select ->
governance -> report (the ``ts_readiness_arimax`` gallery template)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
from mapi_spec.governance import (
    CheckResult,
    CheckStatus,
    GovernancePolicy,
    PolicyCheck,
)
from mapi_spec.workflow import NodeRunStatus, WorkflowEventType, WorkflowRunStatus
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX, instantiate_template
from mapi_workflows.payloads import MetricsPayload, ReportPayload
from mapi_workflows.runtime.governance import CheckContext, GovernanceServices
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.runtime.models import model_residuals
from mapi_workflows.runtime.values import ModelValue
from wf_models import registry, weekly_series

POLICY = GovernancePolicy(
    id="pol_01jaf3h1j2k3m4n5p6q7r8s9t0",
    tenant_id="acme",
    project_id=None,
    scope="tenant",
    version=1,
    name="Forecast models",
    checks=[PolicyCheck(check="residuals.whiteness"), PolicyCheck(check="convergence")],
    created_at=datetime(2026, 10, 1, tzinfo=UTC),
)


def _whiteness(ctx: CheckContext) -> CheckResult:
    from statsmodels.stats.diagnostic import acorr_ljungbox

    res = model_residuals(ctx.model, ctx.registry)
    p = float(acorr_ljungbox(res.resid, lags=[10], return_df=True)["lb_pvalue"].iloc[0])
    return CheckResult(
        check="residuals.whiteness",
        status=CheckStatus.PASS if p > 0.01 else CheckStatus.FAIL,
        severity=ctx.check.severity,
        metric="ljung_box_p",
        value=p,
        message_plain=f"Ljung-Box p = {p:.3f}",
    )


def _convergence(ctx: CheckContext) -> CheckResult:
    return CheckResult(
        check="convergence",
        status=CheckStatus.PASS,
        severity=ctx.check.severity,
        message_plain="The fit finished.",
    )


def _runner(**kw: Any) -> LocalRunner:
    seen: dict[str, Any] = {}

    def resolver(model: ModelValue, policy_id: str | None) -> list[GovernancePolicy]:
        seen["model"] = model
        return [POLICY]

    gov = GovernanceServices(
        resolver=resolver,
        runners={"residuals.whiteness": _whiteness, "convergence": _convergence},
    )
    return LocalRunner(registry=registry(), governance=gov, **kw)


PARAMS = {
    "drivers": ["price"],
    "n_folds": 4,
    "gap": 1,
    "horizons": [1, 4, 8],
    "max_p": 2,
    "max_q": 2,
}


def test_readiness_to_arimax_end_to_end() -> None:
    spec = instantiate_template(TS_READINESS_ARIMAX, name="weekly-readiness", project="growth")
    data = weekly_series(156, random_walk=True, beta=3.0)
    events: list[Any] = []
    runner = _runner(on_event=events.append)
    res = runner.run(spec, inputs={"data": data}, params=PARAMS)
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}

    # the random walk wanders: ADF keeps the unit root, KPSS rejects stationarity
    assert runs["adf"].result is not None and runs["adf"].result["decision"] == "unit_root"
    assert runs["kpss"].result is not None and runs["kpss"].result["decision"] == "unit_root"
    assert runs["wanders"].result == {"value": True, "arm": "then"}
    # no yearly season in the data: no seasonal differencing
    assert runs["seasonal"].result == {"value": False, "arm": "else"}
    assert runs["sdiff"].status is NodeRunStatus.SKIPPED
    # the same difference as the over-differencing guard's trial: reused from it
    assert runs["diff"].status in (NodeRunStatus.SUCCEEDED, NodeRunStatus.CACHED)
    assert runs["overdiff"].result is not None and runs["overdiff"].result["acf"][1] > -0.5
    assert runs["diff"].result is not None and runs["diff"].result["lost_periods"] == 1
    # the ACF/PACF ran on the differenced series (the first available alternative)
    acf = runs["acf"].result
    assert acf is not None and acf["n_obs"] == 155

    # the order search fitted d=1 models and the backtest used its orders
    order = runs["order"].result
    assert order is not None and order["best_order"][1] == 1
    settings = runs["backtest"].params["settings"]
    assert settings["order"] == [order["best_p"], 1, order["best_q"]]
    assert settings["exog"] == ["price"]

    # the backtest: 4 expanding folds with a 1-week gap scored at horizons 1, 4 and 8
    bt = runs["backtest"].result
    assert bt is not None and bt["n_folds"] == 4
    assert set(bt["by_horizon"]) == {"1", "4", "8"}
    folds = runs["folds"].result
    assert folds is not None and folds["n_folds"] == 4
    plan = res.node_values["folds"]["plan"]
    assert isinstance(plan, dict)
    for f in plan["folds"]:
        assert f["test_start"] - f["train_stop"] == 1  # the gap
        assert f["test_stop"] - f["test_start"] == 8
    metrics = MetricsPayload.model_validate(res.node_values["backtest"]["metrics"])
    cand = metrics.candidates[0]
    assert cand.model == "ts.sarimax@^1"
    assert len(cand.errors or []) == 4 and all(len(e) == 8 for e in cand.errors or [])
    # with the price driver known, ARIMAX beats the naive benchmarks
    comp = runs["compare"].result
    assert comp is not None and comp["best"] == "backtest"
    sel = runs["select"].result
    assert sel is not None and sel["beats_baselines"] is True
    assert "model" in runs["select"].outputs

    # governance ran the policy's checks on the selected model
    gov = runs["governance"].result
    assert gov is not None and gov["passed"] is True and gov["n_checks"] == 2
    ev = res.values["evidence"]
    assert isinstance(ev, dict) and ev["policies"] == [f"{POLICY.id}@1"]

    report = ReportPayload.model_validate(res.values["report"])
    assert report.title == "Time-series readiness and ARIMAX"
    kinds = {s.kind for s in report.sections}
    assert {"test", "decision", "table", "metrics", "model"} <= kinds
    assert isinstance(res.values["model"], ModelValue)

    types = [e.type for e in events]
    assert types[0] is WorkflowEventType.RUN_STARTED
    assert types[-1] is WorkflowEventType.RUN_SUCCEEDED

    # a second run on the same data reuses every cacheable node
    again = runner.run(spec, inputs={"data": data}, params=PARAMS)
    statuses = {n.node_id: n.status for n in again.nodes}
    assert statuses["adf"] is NodeRunStatus.CACHED
    assert statuses["backtest"] is NodeRunStatus.CACHED
    assert statuses["governance"] is NodeRunStatus.SUCCEEDED  # never cached
    assert again.status is WorkflowRunStatus.SUCCEEDED


def test_stationary_series_goes_straight_to_the_search() -> None:
    spec = instantiate_template(TS_READINESS_ARIMAX, name="weekly-readiness", project="growth")
    data = weekly_series(156, phi=0.3, beta=3.0, seed=11)
    res = _runner().run(spec, inputs={"data": data}, params=PARAMS)
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}
    assert runs["wanders"].result == {"value": False, "arm": "else"}
    assert runs["diff"].status is NodeRunStatus.SKIPPED
    assert runs["backtest"].params["settings"]["order"][1] == 0
    assert np.isfinite(runs["backtest"].result["metrics"]["mae"])  # type: ignore[index]


def _decisions() -> Any:
    """The template's differencing decisions only (its own nodes and rules)."""
    import copy

    from mapi_spec.workflow import WorkflowSpec

    keep = {"seas", "seasonal", "sdiff", "adf", "kpss", "pp", "za", "cand", "overdiff",
            "wanders", "diff"}  # fmt: skip
    doc = copy.deepcopy(TS_READINESS_ARIMAX.document)
    doc["metadata"] = {"name": "decide", "project": "growth"}
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] in keep]  # type: ignore[index,union-attr]
    doc["outputs"] = {}
    return WorkflowSpec.model_validate(doc)


def _monthly(y: np.ndarray) -> Any:
    from wf_ctx import series_ds

    return series_ds(100 + y, name="sales", freq="ME")


def test_seasonal_random_walks_are_not_over_differenced() -> None:
    """Regression (simulation): the unit-root tests ran on the levels, so a seasonal random
    walk (true d = 0, D = 1) was also first-differenced in 93% of draws. They now run on
    the seasonally differenced series, with an over-differencing guard; plain random walks
    are still differenced."""
    spec, runner = _decisions(), LocalRunner()
    n, s, reps = 144, 12, 20
    over = rw_diff = 0
    for seed in range(reps):
        rng = np.random.default_rng(seed)
        y = np.zeros(n)
        for t in range(n):
            y[t] = (y[t - s] if t >= s else 10 * np.sin(2 * np.pi * t / s)) + rng.normal()
        res = runner.run(spec, inputs={"data": _monthly(y)}, params={"season": s})
        assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
        runs = {r.node_id: r for r in res.nodes}
        assert runs["seasonal"].result == {"value": True, "arm": "then"}
        over += bool(runs["wanders"].result["value"])  # type: ignore[index]
        walk = runner.run(
            spec, inputs={"data": _monthly(np.cumsum(rng.normal(size=n)))}, params={"season": s}
        )
        rw_diff += bool(next(r for r in walk.nodes if r.node_id == "wanders").result["value"])  # type: ignore[index]
    assert over <= 3, f"seasonal random walk first-differenced in {over}/{reps} draws"
    assert rw_diff >= reps - 3, f"random walk differenced in only {rw_diff}/{reps} draws"
