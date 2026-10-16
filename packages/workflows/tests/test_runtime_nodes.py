"""Transforms, split plans, searches, benchmarks, metrics, Diebold-Mariano, governance."""

from __future__ import annotations

import math
import warnings
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import (
    CheckResult,
    CheckSeverity,
    CheckStatus,
    GovernancePolicy,
    PolicyCheck,
)
from mapi_spec.splitplan import ExpandingWindowPlan, plan_folds
from mapi_workflows.payloads import EvidencePayload, MetricsPayload, SplitPlanPayload
from mapi_workflows.runtime.evaluate import baseline_forecast, diebold_mariano
from mapi_workflows.runtime.governance import GovernanceServices
from mapi_workflows.runtime.models import FoldForecast, fold_metrics
from mapi_workflows.runtime.search import arma_grid, sarimax_trend
from mapi_workflows.runtime.values import DatasetValue, ModelValue
from wf_ctx import ar1, panel_ds, random_walk, run_node, series_ds
from wf_models import registry, weekly_series

pytestmark = pytest.mark.filterwarnings("ignore")


# ============================================================================ transforms


def test_difference_and_seasonal_difference() -> None:
    y = np.array([100.0, 104, 103, 110, 108, 115, 120, 118, 125, 130])
    out = run_node("transform.difference", data=series_ds(y))
    dv = out.outputs["data"]
    assert isinstance(dv, DatasetValue)
    assert list(dv.data["y"].values) == pytest.approx(list(np.diff(y)))
    assert dv.data.sizes["time"] == 9
    assert out.result["last_levels"] == {"y": [130.0]}
    assert dv.replayable is False and "difference" in dv.notes[0]
    two = run_node("transform.difference", {"order": 2}, data=series_ds(y))
    assert list(two.outputs["data"].data["y"].values) == pytest.approx(list(np.diff(y, n=2)))  # type: ignore[union-attr]
    sd = run_node("transform.seasonal_difference", {"period": 4}, data=series_ds(y))
    assert list(sd.outputs["data"].data["y"].values) == pytest.approx(list(y[4:] - y[:-4]))  # type: ignore[union-attr]


def test_log_and_box_cox() -> None:
    from scipy import stats as sps

    y = np.exp(np.linspace(1, 3, 40)) + np.random.default_rng(0).uniform(0, 1, 40)
    lg = run_node("transform.log", data=series_ds(y)).outputs["data"]
    assert np.allclose(lg.data["y"].values, np.log(y))  # type: ignore[union-attr]
    bc = run_node("transform.box_cox", data=series_ds(y))
    xt, lam = sps.boxcox(y)
    assert bc.result["lmbda"] == pytest.approx(lam)
    assert np.allclose(bc.outputs["data"].data["y"].values, xt)  # type: ignore[union-attr]
    with pytest.raises(MapiError):
        run_node("transform.log", data=series_ds(y - 100))


def test_steps_record_a_replayable_pipeline() -> None:
    ds = weekly_series(60)
    out = run_node(
        "transform.steps",
        {"steps": [{"id": "lp", "use": "core.log1p@^1", "with": {"vars": ["price"]}}]},
        data=ds, registry=registry(),
    )  # fmt: skip
    dv = out.outputs["data"]
    assert isinstance(dv, DatasetValue)
    assert np.allclose(dv.data["price"].values, np.log1p(ds["price"].values))
    assert dv.replayable and dv.pipeline == [
        {"id": "lp", "use": "core.log1p@^1", "with": {"vars": ["price"]}}
    ]
    assert dv.root is not None and dv.root.data is not None
    sel = run_node("transform.select", {"start": "2022-03-06"},
                   data=DatasetValue(data=ds))  # fmt: skip
    sdv = sel.outputs["data"]
    assert isinstance(sdv, DatasetValue)
    assert str(sdv.data["time"].values[0])[:10] == "2022-03-06"
    assert sdv.pipeline[0]["use"] == "core.filter_coords@^1"


def test_a_model_fitted_on_selected_data_replays_the_selection() -> None:
    reg = registry()
    ds = weekly_series(120)
    sel = run_node("transform.select", {"start": "2022-07-03"}, data=DatasetValue(data=ds))
    dv = sel.outputs["data"]
    out = run_node("model.fit", {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
                   data=dv, registry=reg)  # fmt: skip
    mv = out.outputs["model"]
    assert isinstance(mv, ModelValue)
    steps = [s.id for s in mv.bundle.spec.pipeline]
    assert steps == ["transform_select_range"]
    # fitted on the original data: the selection is part of the bundle
    assert mv.data is not None and mv.data.sizes["time"] == 120


# ================================================================================ splits


def test_expanding_window_folds_match_the_pure_plan_and_carry_labels() -> None:
    ds = weekly_series(100)
    out = run_node("split.expanding_window", {"horizons": [1, 4], "gap": 2, "n_folds": 5,
                                              "step": 3}, data=ds)  # fmt: skip
    payload = SplitPlanPayload.model_validate(out.outputs["plan"])
    plan = ExpandingWindowPlan(kind="expanding_window", horizons=[1, 4], gap=2, n_folds=5, step=3)
    expect = plan_folds(plan, 100)
    assert [f["test_start"] for f in payload.folds] == [f.test_start for f in expect]
    assert payload.folds[-1]["test_stop"] == 100
    assert payload.folds[0]["test_labels"][0] == str(ds["time"].values[expect[0].test_start])[:10]
    assert out.result["n_folds"] == 5


def test_holdout_and_kfold_and_geo_plans() -> None:
    ds = weekly_series(80)
    hold = run_node("split.holdout", {"split": {"kind": "time_holdout", "dim": "time",
                                                "last": 8}}, data=ds)  # fmt: skip
    f = hold.outputs["plan"]["folds"][0]
    assert (f["train_stop"], f["test_start"], f["test_stop"]) == (72, 72, 80)
    kf = run_node("split.purged_kfold", {"k": 4, "purge": 2, "embargo": 1}, data=ds)
    folds = kf.outputs["plan"]["folds"]
    assert len(folds) == 4 and folds[1]["excluded"][1] == [18, 20]
    panel = panel_ds(np.ones((5, 30)), ["a", "b", "c", "d", "e"])
    geo = run_node("split.geo_holdout", {"dim": "geo", "n": 2, "n_folds": 3, "seed": 1},
                   data=panel)  # fmt: skip
    gf = geo.outputs["plan"]["folds"]
    assert len(gf) == 3 and all(len(x["test_groups"]) == 2 for x in gf)
    with pytest.raises(MapiError) as exc:
        run_node("split.expanding_window", {"horizons": [8], "n_folds": 50}, data=ds)
    assert exc.value.code is ErrorCode.SPLIT_INVALID


# ================================================================================ search


def test_arma_search_recovers_an_ar2_like_statsmodels() -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    from statsmodels.tsa.stattools import arma_order_select_ic

    rng = np.random.default_rng(3)
    e = rng.normal(size=400)
    y = np.zeros(400)
    for t in range(2, 400):
        y[t] = 0.6 * y[t - 1] - 0.3 * y[t - 2] + e[t]
    rows, failed = arma_grid(y, None, ps=range(4), qs=range(3), criterion="aic")
    assert failed == 0
    best = rows[0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        aic = SARIMAX(y, order=(best["p"], 0, best["q"]), trend="c").fit(disp=False).aic
        ref = arma_order_select_ic(y, max_ar=3, max_ma=2, ic="aic", trend="c")
    assert best["aic"] == pytest.approx(aic, rel=1e-6)
    assert (best["p"], best["q"]) == tuple(ref.aic_min_order)
    assert best["p"] == 2
    out = run_node("search.arma_order", {"max_p": 3, "max_q": 2}, data=series_ds(y))
    assert out.result["best_order"] == [best["p"], 0, best["q"]]
    assert out.result["suggested_p"] == 2  # the PACF hint agrees
    assert sarimax_trend("c", 1) is None and sarimax_trend("ct", 1) == "t"


def test_var_lag_and_coint_rank_match_statsmodels() -> None:
    from statsmodels.tsa.api import VAR
    from statsmodels.tsa.vector_ar.vecm import select_coint_rank

    rng = np.random.default_rng(5)
    x = np.cumsum(rng.normal(size=300))
    y = 0.5 * x + rng.normal(size=300)
    ds = series_ds(np.diff(y, prepend=0), extra={"x": np.diff(x, prepend=0)})
    lag = run_node("search.var_lag_order", {"vars": ["y", "x"], "maxlags": 6}, data=ds)
    mat = np.column_stack([ds["y"].values, ds["x"].values])
    assert lag.result["best_lag"] == VAR(mat).select_order(6).selected_orders["aic"]
    lvl = series_ds(y, extra={"x": x})
    rank = run_node("search.coint_rank", {"vars": ["y", "x"]}, data=lvl)
    assert rank.result["rank"] == select_coint_rank(np.column_stack([y, x]), 0, 1).rank


# ============================================================================ benchmarks


def test_baseline_forecasts_by_hand() -> None:
    train = np.array([[1.0], [2.0], [3.0], [4.0], [5.0], [6.0]])
    assert baseline_forecast(train, 3, "naive", 4)[:, 0].tolist() == [6, 6, 6]
    assert baseline_forecast(train, 3, "mean", 4)[:, 0].tolist() == [3.5] * 3
    assert baseline_forecast(train, 2, "drift", 4)[:, 0].tolist() == [7, 8]
    assert baseline_forecast(train, 6, "seasonal_naive", 4)[:, 0].tolist() == [3, 4, 5, 6, 3, 4]


def test_metrics_by_hand() -> None:
    fc = FoldForecast(actual=np.array([[10.0], [20.0]]), point=np.array([[12.0], [16.0]]),
                      lower=np.array([[9.0], [17.0]]), upper=np.array([[11.0], [25.0]]),
                      scale=2.0)  # fmt: skip
    m = fold_metrics([fc])
    assert m["mae"] == pytest.approx(3.0)
    assert m["rmse"] == pytest.approx(math.sqrt((4 + 16) / 2))
    assert m["mape"] == pytest.approx((0.2 + 0.2) / 2)
    assert m["mase"] == pytest.approx(1.5)
    assert m["coverage_90"] == pytest.approx(1.0)
    assert fold_metrics([fc], [1])["mae"] == pytest.approx(4.0)


def test_baseline_node_scores_the_same_folds_as_the_backtest() -> None:
    ds = weekly_series(120, beta=3.0)
    plan = run_node("split.expanding_window", {"horizons": [1, 4], "n_folds": 3}, data=ds)
    out = run_node("evaluate.baseline", {"methods": ["naive", "seasonal_naive", "mean"],
                                         "season": 52}, data=ds,
                   split=plan.outputs["plan"])  # fmt: skip
    mp = MetricsPayload.model_validate(out.outputs["metrics"])
    assert [c.name for c in mp.candidates] == ["naive", "seasonal_naive", "mean"]
    assert all(c.kind == "baseline" and c.n_folds == 3 for c in mp.candidates)
    # the naive forecast at horizon 1 of fold 0, by hand
    f0 = plan.outputs["plan"]["folds"][0]
    y = ds["sales"].values
    err = y[f0["test_start"]] - y[f0["train_stop"] - 1]
    assert mp.candidates[0].errors is not None
    assert mp.candidates[0].errors[0][0] == pytest.approx(err)
    bt = run_node("model.backtest", {"model": "ts.sarimax@^1",
                                     "settings": {"order": [1, 0, 0], "exog": ["price"]}},
                  data=ds, split=plan.outputs["plan"], registry=registry())  # fmt: skip
    cand = MetricsPayload.model_validate(bt.outputs["metrics"]).candidates[0]
    assert cand.n_folds == 3 and set(cand.by_horizon) == {"1", "4"}
    assert cand.metrics["coverage_90"] is not None
    assert bt.result["metrics"]["mae"] < mp.candidates[0].metrics["mae"]  # type: ignore[index,operator]


def test_kfold_backtest_masks_and_predicts_the_block() -> None:
    ds = weekly_series(80)
    plan = run_node("split.blocked_kfold", {"k": 4, "gap": 1}, data=ds)
    bt = run_node("model.backtest", {"model": "ts.sarimax@^1",
                                     "settings": {"order": [1, 0, 0], "exog": ["price"]}},
                  data=ds, split=plan.outputs["plan"], registry=registry())  # fmt: skip
    assert bt.result["n_folds"] == 4
    assert bt.result["metrics"]["mae"] < 3  # type: ignore[index,operator]


def test_metrics_node_reads_a_backtest_or_a_model() -> None:
    payload = MetricsPayload.model_validate({"candidates": [
        {"name": "bt", "group": "bt", "metrics": {"mae": 1.0, "rmse": 2.0},
         "by_horizon": {"1": {"mae": 0.5}}}]}).model_dump(mode="json")  # fmt: skip
    out = run_node("evaluate.metrics", {"metrics": ["mae"]}, backtest=payload)
    assert out.result["mae"] == 1.0 and "rmse" not in out.result
    reg = registry()
    fit = run_node("model.fit", {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]},
                                 "evaluate": {"metrics": ["mae", "rmse"]}},
                   data=weekly_series(100), registry=reg,
                   split={"plan": {"kind": "holdout",
                                   "split": {"kind": "time_holdout", "dim": "time",
                                             "last": 8}}})  # fmt: skip
    assert fit.result["metrics"]["mae"] is not None  # type: ignore[index]
    m = run_node("evaluate.metrics", model=fit.outputs["model"], registry=reg)
    assert m.result["mae"] == fit.result["metrics"]["mae"]  # type: ignore[index]
    assert m.result["in_sample"] is False
    res = run_node("evaluate.residuals", model=fit.outputs["model"], registry=reg)
    assert res.result["white_noise"] is True
    # the data's drivers are there: the spread check is Breusch-Pagan on them
    assert {r["check"] for r in res.outputs["table"]["rows"]} == {  # type: ignore[index]
        "Ljung-Box",
        "Jarque-Bera",
        "Breusch-Pagan",
    }


# ======================================================================= Diebold-Mariano


def test_diebold_mariano_by_hand_and_by_node() -> None:
    rng = np.random.default_rng(1)
    e1 = rng.normal(scale=1.0, size=60)
    e2 = rng.normal(scale=2.0, size=60)
    stat, p, n = diebold_mariano(e1, e2, h=1)
    d = e1**2 - e2**2
    dm = d.mean() / math.sqrt(np.mean((d - d.mean()) ** 2) / 60)
    hln = math.sqrt((60 + 1 - 2 + 0) / 60)
    assert stat == pytest.approx(dm * hln)
    from scipy import stats as sps

    assert p == pytest.approx(2 * sps.t.sf(abs(stat), 59))
    assert stat < 0 and p < 0.01 and n == 60

    def payload(errs: np.ndarray) -> dict[str, Any]:
        return MetricsPayload.model_validate({"candidates": [{
            "name": "m", "group": "m", "metrics": {"mae": float(np.mean(np.abs(errs)))},
            "errors": [[float(x)] for x in errs]}]}).model_dump(mode="json")  # fmt: skip

    out = run_node("compare.diebold_mariano", {"horizon": 1}, a=payload(e1), b=payload(e2))
    assert out.result["decision"] == "a_better"
    assert out.result["test"] == "test.diebold_mariano"


# ============================================================================ governance

_POLICY = GovernancePolicy(
    id="pol_1", tenant_id="acme", project_id="growth", scope="project", version=2,
    name="ts", checks=[PolicyCheck(check="convergence"),
                       PolicyCheck(check="data.quality", severity=CheckSeverity.WARNING)],
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)  # fmt: skip


def _mv() -> ModelValue:
    out = run_node("model.fit", {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
                   data=weekly_series(80), registry=registry())  # fmt: skip
    mv = out.outputs["model"]
    assert isinstance(mv, ModelValue)
    return mv


def test_policy_without_a_store_requires_nothing() -> None:
    out = run_node("governance.policy", model=_mv())
    assert out.result["passed"] is True and out.result["n_checks"] == 0
    assert "No governance policy" in out.result["message_plain"]  # type: ignore[operator]
    with pytest.raises(MapiError) as exc:
        run_node("governance.policy", {"policy": "pol_x"}, model=_mv())
    assert exc.value.code is ErrorCode.POLICY_INVALID


def test_policy_runs_checks_and_missing_runners_block() -> None:
    def ok(ctx: Any) -> CheckResult:
        return CheckResult(check=ctx.check.check, status=CheckStatus.PASS,
                           severity=CheckSeverity.INFO, message_plain="ok")  # fmt: skip

    submitted: list[Any] = []
    svc = GovernanceServices(resolver=lambda m, p: [_POLICY], runners={"convergence": ok},
                             submit=lambda m, ev: submitted.append(ev))  # fmt: skip
    out = run_node("governance.policy", {"submit": True}, model=_mv(),
                   services={"governance": svc})  # fmt: skip
    ev = EvidencePayload.model_validate(out.outputs["evidence"])
    # the runner's severity is replaced by the policy's
    assert ev.results[0]["severity"] == "blocking"
    # data.quality has no runner: an error, but only a warning in this policy
    assert ev.results[1]["status"] == "error"
    assert ev.passed and submitted and ev.policies == ["pol_1@2"]
    svc.runners = {}
    failed = run_node("governance.policy", model=_mv(), services={"governance": svc})
    assert failed.result["passed"] is False
    assert failed.result["failed_checks"] == ["convergence"]


def test_one_check_and_the_evidence_pack() -> None:
    def crash(ctx: Any) -> CheckResult:
        raise RuntimeError("boom")

    svc = GovernanceServices(runners={"null.permutation_target": crash})
    mv = _mv()
    out = run_node("governance.check", {"check": "null.permutation_target"}, model=mv,
                   services={"governance": svc})  # fmt: skip
    assert out.result["status"] == "error"
    ev = out.outputs["evidence"]
    pack = run_node("report.evidence_pack", model=mv, evidence=ev)
    assert pack.result["passed"] is False
    assert pack.outputs["report"]["sections"][0]["kind"] == "evidence"  # type: ignore[index]


def test_random_walk_data_helpers() -> None:
    assert len(random_walk(10)) == 10 and len(ar1(10)) == 10
