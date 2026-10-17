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
        data=ds,
        registry=registry(),
    )
    dv = out.outputs["data"]
    assert isinstance(dv, DatasetValue)
    assert np.allclose(dv.data["price"].values, np.log1p(ds["price"].values))
    assert dv.replayable and dv.pipeline == [
        {"id": "lp", "use": "core.log1p@^1", "with": {"vars": ["price"]}}
    ]
    assert dv.root is not None and dv.root.data is not None
    sel = run_node("transform.select", {"start": "2022-03-06"}, data=DatasetValue(data=ds))
    sdv = sel.outputs["data"]
    assert isinstance(sdv, DatasetValue)
    assert str(sdv.data["time"].values[0])[:10] == "2022-03-06"
    assert sdv.pipeline[0]["use"] == "core.filter_coords@^1"


def test_a_model_fitted_on_selected_data_replays_the_selection() -> None:
    reg = registry()
    ds = weekly_series(120)
    sel = run_node("transform.select", {"start": "2022-07-03"}, data=DatasetValue(data=ds))
    dv = sel.outputs["data"]
    out = run_node(
        "model.fit",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
        data=dv,
        registry=reg,
    )
    mv = out.outputs["model"]
    assert isinstance(mv, ModelValue)
    steps = [s.id for s in mv.bundle.spec.pipeline]
    assert steps == ["transform_select_range"]
    # fitted on the original data: the selection is part of the bundle
    assert mv.data is not None and mv.data.sizes["time"] == 120


# ================================================================================ splits


def test_expanding_window_folds_match_the_pure_plan_and_carry_labels() -> None:
    ds = weekly_series(100)
    out = run_node(
        "split.expanding_window", {"horizons": [1, 4], "gap": 2, "n_folds": 5, "step": 3}, data=ds
    )
    payload = SplitPlanPayload.model_validate(out.outputs["plan"])
    plan = ExpandingWindowPlan(kind="expanding_window", horizons=[1, 4], gap=2, n_folds=5, step=3)
    expect = plan_folds(plan, 100)
    assert [f["test_start"] for f in payload.folds] == [f.test_start for f in expect]
    assert payload.folds[-1]["test_stop"] == 100
    assert payload.folds[0]["test_labels"][0] == str(ds["time"].values[expect[0].test_start])[:10]
    assert out.result["n_folds"] == 5


def test_holdout_and_kfold_and_geo_plans() -> None:
    ds = weekly_series(80)
    hold = run_node(
        "split.holdout", {"split": {"kind": "time_holdout", "dim": "time", "last": 8}}, data=ds
    )
    f = hold.outputs["plan"]["folds"][0]
    assert (f["train_stop"], f["test_start"], f["test_stop"]) == (72, 72, 80)
    kf = run_node("split.purged_kfold", {"k": 4, "purge": 2, "embargo": 1}, data=ds)
    folds = kf.outputs["plan"]["folds"]
    assert len(folds) == 4 and folds[1]["excluded"][1] == [18, 20]
    panel = panel_ds(np.ones((5, 30)), ["a", "b", "c", "d", "e"])
    geo = run_node("split.geo_holdout", {"dim": "geo", "n": 2, "n_folds": 3, "seed": 1}, data=panel)
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


def test_arma_search_uses_the_seasonal_difference_and_stays_before_the_tests() -> None:
    """Regression: the order search fitted ARIMA(p,d,q) without the seasonal difference
    the final model uses (AR terms absorbed the season) and searched every period,
    including the backtest's test windows."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    rng = np.random.default_rng(0)
    n, s = 156, 12
    y = np.zeros(n)
    for t in range(n):  # a seasonal random walk: (1 - L^12) y = e
        y[t] = (y[t - s] if t >= s else 5 * np.sin(2 * np.pi * t / s)) + rng.normal()
    ds = series_ds(100 + y)
    out = run_node("search.arma_order", {"max_p": 2, "max_q": 1, "D": 1, "period": s}, data=ds)
    best = out.result
    assert best["best_order"] == [0, 0, 0]  # white noise once seasonally differenced
    meta = out.outputs["table"]["meta"]  # type: ignore[index]
    assert meta["D"] == 1 and meta["period"] == s
    row = out.outputs["table"]["rows"][0]  # type: ignore[index]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ref = SARIMAX(100 + y, order=(0, 0, 0), seasonal_order=(0, 1, 0, s), trend=None).fit(
            disp=False
        )
    assert row["aic"] == pytest.approx(ref.aic, rel=1e-6)  # the model that will be fitted
    # without the seasonal difference, AR terms soak up the season
    plain = run_node("search.arma_order", {"max_p": 2, "max_q": 1}, data=ds)
    assert plain.result["best_p"] > 0

    # with a split plan, only the periods before the first test window are searched
    plan = run_node(
        "split.expanding_window",
        {"horizons": [1, 4], "n_folds": 3, "step": 4, "gap": 1},
        data=ds,
    ).outputs["plan"]
    first = min(f["test_start"] for f in plan["folds"])  # type: ignore[index]
    held = run_node(
        "search.arma_order",
        {"max_p": 1, "max_q": 1, "D": 1, "period": s},
        data=ds,
        split=plan,
    )
    assert held.outputs["table"]["meta"]["n_obs"] == first - 1  # type: ignore[index]
    kfold = run_node("split.blocked_kfold", {"k": 4}, data=ds).outputs["plan"]
    with pytest.raises(MapiError, match="before its first test window"):
        run_node("search.arma_order", {"max_p": 1}, data=ds, split=kfold)
    with pytest.raises(MapiError, match="season length"):
        run_node("search.arma_order", {"max_p": 1, "D": 1}, data=ds)


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
    fc = FoldForecast(
        actual=np.array([[10.0], [20.0]]),
        point=np.array([[12.0], [16.0]]),
        lower=np.array([[9.0], [17.0]]),
        upper=np.array([[11.0], [25.0]]),
        scale=2.0,
    )
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
    out = run_node(
        "evaluate.baseline",
        {"methods": ["naive", "seasonal_naive", "mean"], "season": 52},
        data=ds,
        split=plan.outputs["plan"],
    )
    mp = MetricsPayload.model_validate(out.outputs["metrics"])
    assert [c.name for c in mp.candidates] == ["naive", "seasonal_naive", "mean"]
    assert all(c.kind == "baseline" and c.n_folds == 3 for c in mp.candidates)
    # the naive forecast at horizon 1 of fold 0, by hand
    f0 = plan.outputs["plan"]["folds"][0]
    y = ds["sales"].values
    err = y[f0["test_start"]] - y[f0["train_stop"] - 1]
    assert mp.candidates[0].errors is not None
    assert mp.candidates[0].errors[0][0] == pytest.approx(err)
    bt = run_node(
        "model.backtest",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=ds,
        split=plan.outputs["plan"],
        registry=registry(),
    )
    cand = MetricsPayload.model_validate(bt.outputs["metrics"]).candidates[0]
    assert cand.n_folds == 3 and set(cand.by_horizon) == {"1", "4"}
    assert cand.metrics["coverage_90"] is not None
    assert bt.result["metrics"]["mae"] < mp.candidates[0].metrics["mae"]  # type: ignore[index,operator]


def test_kfold_backtest_masks_and_predicts_the_block() -> None:
    ds = weekly_series(80)
    plan = run_node("split.blocked_kfold", {"k": 4, "gap": 1}, data=ds)
    bt = run_node(
        "model.backtest",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=ds,
        split=plan.outputs["plan"],
        registry=registry(),
    )
    assert bt.result["n_folds"] == 4
    assert bt.result["metrics"]["mae"] < 3  # type: ignore[index,operator]


def _nested_plan(ds: Any, outer: dict[str, Any]) -> dict[str, Any]:
    plan = run_node(
        "split.nested",
        {
            "outer": outer,
            "inner": {"kind": "expanding_window", "horizons": [1, 2], "n_folds": 2, "step": 4},
        },
        data=ds,
    )
    out = plan.outputs["plan"]
    assert isinstance(out, dict)
    return out


def test_grid_search_on_a_nested_plan_tunes_inside_and_scores_outside() -> None:
    """Regression: search.grid ignored the inner folds of a nested plan and picked its
    winner by the outer folds' score (the optimistic selection bias nested CV removes),
    and a k-fold outer plan crashed (``horizon.periods ... -90``). Now each outer fold
    tunes on its own inner folds and scores the inner winner on its outer test window."""
    from mapi_workflows.runtime.models import backtest_model, fold_metrics, grid_node
    from wf_ctx import node_ctx

    ds = weekly_series(120, beta=3.0)
    params = {
        "model": "ts.sarimax@^1",
        "settings": {"order": [1, 0, 0], "exog": ["price"]},
        "grid": {"/order": [[0, 0, 0], [1, 0, 0], [2, 0, 0]]},
        "metric": "rmse",
    }
    for outer in (
        {"kind": "expanding_window", "horizons": [1, 2], "n_folds": 3, "step": 8},
        {"kind": "blocked_kfold", "k": 3},
    ):
        plan = _nested_plan(ds, outer)
        assert all(f["inner"] for f in plan["folds"])
        ctx = node_ctx("search.grid", params, data=ds, split=plan, registry=registry())
        ctx.services["sandbox"] = True  # no final refit needed for the test
        res = grid_node(ctx).result
        assert res["nested"] is True
        n_outer = len(plan["folds"])
        assert len(res["outer_scores"]) == n_outer  # type: ignore[arg-type]
        assert len(res["selected_per_fold"]) == n_outer  # type: ignore[arg-type]
        # every outer fold's winner is the best point on that fold's inner folds
        for f, chosen in zip(plan["folds"], res["selected_per_fold"], strict=True):  # type: ignore[arg-type]
            inner = {**plan, "plan": plan["plan"]["inner"], "folds": f["inner"]}
            scores = []
            for order in params["grid"]["/order"]:  # type: ignore[index]
                fcs, _, _ = backtest_model(
                    node_ctx("model.backtest", params, data=ds, registry=registry()),
                    DatasetValue(data=ds),
                    inner,
                    settings={**params["settings"], "order": order},  # type: ignore[dict-item]
                )
                scores.append(fold_metrics(fcs)["rmse"])
            assert chosen["/order"] == params["grid"]["/order"][int(np.argmin(scores))]  # type: ignore[index]
        # the honest estimate pools the outer test errors of the per-fold winners
        lo, hi = min(res["outer_scores"]), max(res["outer_scores"])  # type: ignore[type-var]
        assert lo - 1e-9 <= res["best_score"] <= hi + 1e-9  # type: ignore[operator]


def test_backtest_on_a_kfold_nested_plan_scores_its_outer_blocks() -> None:
    ds = weekly_series(120, beta=3.0)
    plan = _nested_plan(ds, {"kind": "blocked_kfold", "k": 3})
    bt = run_node(
        "model.backtest",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=ds,
        split=plan,
        registry=registry(),
    )
    assert bt.result["n_folds"] == 3


def test_metrics_node_reads_a_backtest_or_a_model() -> None:
    payload = MetricsPayload.model_validate(
        {
            "candidates": [
                {
                    "name": "bt",
                    "group": "bt",
                    "metrics": {"mae": 1.0, "rmse": 2.0},
                    "by_horizon": {"1": {"mae": 0.5}},
                }
            ]
        }
    ).model_dump(mode="json")
    out = run_node("evaluate.metrics", {"metrics": ["mae"]}, backtest=payload)
    assert out.result["mae"] == 1.0 and "rmse" not in out.result
    reg = registry()
    fit = run_node(
        "model.fit",
        {
            "model": "ts.sarimax@^1",
            "settings": {"order": [1, 0, 0]},
            "evaluate": {"metrics": ["mae", "rmse"]},
        },
        data=weekly_series(100),
        registry=reg,
        split={
            "plan": {"kind": "holdout", "split": {"kind": "time_holdout", "dim": "time", "last": 8}}
        },
    )
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
        return MetricsPayload.model_validate(
            {
                "candidates": [
                    {
                        "name": "m",
                        "group": "m",
                        "metrics": {"mae": float(np.mean(np.abs(errs)))},
                        "errors": [[float(x)] for x in errs],
                    }
                ]
            }
        ).model_dump(mode="json")

    out = run_node("compare.diebold_mariano", {"horizon": 1}, a=payload(e1), b=payload(e2))
    assert out.result["decision"] == "a_better"
    assert out.result["test"] == "test.diebold_mariano"


def test_diebold_mariano_refuses_horizons_its_folds_cannot_support() -> None:
    """Three folds cannot test horizon 4 (the long-run variance needs lags up to h - 1): a
    plain data error, not a NaN p-value that fails the result's validation."""
    rng = np.random.default_rng(2)
    e1, e2 = rng.normal(size=3), rng.normal(size=3)
    with pytest.raises(ValueError, match="more than 4 paired errors"):
        diebold_mariano(e1, e2, h=4)
    with pytest.raises(ValueError, match="more than 3 paired errors"):
        diebold_mariano(e1, e2, h=3)
    stat, p, n = diebold_mariano(rng.normal(size=40), rng.normal(size=40), h=4)
    assert math.isfinite(stat) and 0 <= p <= 1 and n == 40

    def payload(errs: np.ndarray) -> dict[str, Any]:
        return MetricsPayload.model_validate(
            {
                "candidates": [
                    {
                        "name": "m",
                        "group": "m",
                        "metrics": {"mae": float(np.mean(np.abs(errs)))},
                        "errors": [[0.0, 0.0, 0.0, float(x)] for x in errs],
                    }
                ]
            }
        ).model_dump(mode="json")

    with pytest.raises(MapiError, match="add folds"):
        run_node("compare.diebold_mariano", {"horizon": 4}, a=payload(e1), b=payload(e2))


def _fold_payload(errs: np.ndarray, h: int, step: int) -> dict[str, Any]:
    """One h-step error per fold, folds ``step`` periods apart (as model.backtest)."""
    return MetricsPayload.model_validate(
        {
            "candidates": [
                {
                    "name": "m",
                    "group": "m",
                    "metrics": {"mae": float(np.mean(np.abs(errs)))},
                    "errors": [[0.0] * (h - 1) + [float(x)] for x in errs],
                    "origins": [100 + step * i for i in range(len(errs))],
                }
            ]
        }
    ).model_dump(mode="json")


def test_diebold_mariano_on_folds_uses_the_fold_spacing() -> None:
    """Regression: per-fold h-step errors were treated as consecutive-origin errors (h - 1
    autocovariances in fold units and the HLN factor of h), which for folds 4 periods
    apart at h = 4 shrank the statistic by 0.41 and refused h = 8 on 6 folds. Folds `step`
    apart overlap only up to ceil(h / step) - 1 folds."""
    rng = np.random.default_rng(3)
    e1, e2 = rng.normal(size=6), rng.normal(scale=3.0, size=6)
    out = run_node(
        "compare.diebold_mariano",
        {"horizon": 4},
        a=_fold_payload(e1, 4, 4),
        b=_fold_payload(e2, 4, 4),
    )
    ref, p_ref, _ = diebold_mariano(e1, e2, h=1)  # no overlap: the h = 1 formula
    assert out.result["statistic"] == pytest.approx(ref)
    assert out.result["p_value"] == pytest.approx(p_ref)
    assert out.result["details"]["variance_lags"] == 0
    assert out.result["details"]["fold_step"] == 4
    eight = run_node(
        "compare.diebold_mariano",
        {"horizon": 8},
        a=_fold_payload(e1, 8, 4),
        b=_fold_payload(e2, 8, 4),
    )
    assert eight.result["details"]["variance_lags"] == 1  # neighbouring folds overlap
    assert math.isfinite(eight.result["statistic"])


def test_diebold_mariano_on_spaced_folds_keeps_size_and_gains_power() -> None:
    """Simulation: a random walk scored by expanding-window folds 4 periods apart at h = 4
    (non-overlapping test windows). Two equally good forecasts: rejection near alpha. A
    forecast with a bias of 2: rejected far more often than with the old h - 1 lags."""

    reps, n_folds, h, step = 300, 12, 4, 4
    size = power = old_power = 0
    for seed in range(reps):
        rng = np.random.default_rng(seed)
        y = np.cumsum(rng.normal(size=200))
        origins = [120 + step * i for i in range(n_folds)]
        actual = np.array([y[o + h - 1] for o in origins])
        last = np.array([y[o - 1] for o in origins])
        ea = actual - (last + 0.5)
        eb = actual - (last - 0.5)
        ec = actual - (last + 2.0)
        _, p, _ = diebold_mariano(ea, eb, h=h, lags=0)
        size += p < 0.05
        _, p, _ = diebold_mariano(actual - last, ec, h=h, lags=0)
        power += p < 0.05
        try:  # the old rule (h - 1 lags in fold units) often has no variance at all
            _, p, _ = diebold_mariano(actual - last, ec, h=h)
            old_power += p < 0.05
        except ValueError:
            pass
    assert size / reps <= 0.10, size / reps
    assert power > old_power, (power, old_power)


# ============================================================================ governance

_POLICY = GovernancePolicy(
    id="pol_1",
    tenant_id="acme",
    project_id="growth",
    scope="project",
    version=2,
    name="ts",
    checks=[
        PolicyCheck(check="convergence"),
        PolicyCheck(check="data.quality", severity=CheckSeverity.WARNING),
    ],
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)


def _mv() -> ModelValue:
    out = run_node(
        "model.fit",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
        data=weekly_series(80),
        registry=registry(),
    )
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
        return CheckResult(
            check=ctx.check.check,
            status=CheckStatus.PASS,
            severity=CheckSeverity.INFO,
            message_plain="ok",
        )

    submitted: list[Any] = []
    svc = GovernanceServices(
        resolver=lambda m, p: [_POLICY],
        runners={"convergence": ok},
        submit=lambda m, ev: submitted.append(ev),
        installed=False,  # only the runners given here (data.quality has none)
    )
    out = run_node("governance.policy", {"submit": True}, model=_mv(), services={"governance": svc})
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
    out = run_node(
        "governance.check",
        {"check": "null.permutation_target"},
        model=mv,
        services={"governance": svc},
    )
    assert out.result["status"] == "error"
    ev = out.outputs["evidence"]
    pack = run_node("report.evidence_pack", model=mv, evidence=ev)
    assert pack.result["passed"] is False
    assert pack.outputs["report"]["sections"][0]["kind"] == "evidence"  # type: ignore[index]


def test_random_walk_data_helpers() -> None:
    assert len(random_walk(10)) == 10 and len(ar1(10)) == 10


# ================================================================================ forecast


def test_forecast_reads_only_the_rows_after_the_fit_as_future_data() -> None:
    """``model.forecast``'s ``data`` is the future drivers: a workflow that wires the fit's own
    dataset in forecasts from the end of the fit (its targets there are no actuals), and a
    target value after the fit is an actual - used or refused, never ignored."""
    import xarray as xr

    reg = registry()
    ds = weekly_series(100)
    fit = ds.isel(time=slice(0, 96))
    out = run_node(
        "model.fit",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0]}},
        data=DatasetValue(data=fit),
        registry=reg,
    )
    mv = out.outputs["model"]
    assert isinstance(mv, ModelValue)
    # the fit's weeks (with their sales: history, not actuals) and 4 weeks of future prices
    future = ds.copy()
    future["sales"] = future["sales"].where(future["time"] <= fit["time"][-1])
    res = run_node("model.forecast", {"horizon": 4}, model=mv, data=DatasetValue(data=future),
                   registry=reg)  # fmt: skip
    fc = res.outputs["forecast"]
    assert isinstance(fc, DatasetValue)
    np.testing.assert_array_equal(fc.data["time"].values, ds["time"].values[96:])
    # a new actual after the fit: the model uses it, or refuses it if it cannot
    actual = ds.isel(time=slice(96, 100)).copy()
    actual["sales"] = xr.where(actual["time"] == actual["time"][0], actual["sales"], np.nan)
    capable = reg.model("ts.sarimax@^1").manifest().capabilities.conditions_on_actuals
    if capable:
        res = run_node("model.forecast", {"horizon": 4}, model=mv,
                       data=DatasetValue(data=actual), registry=reg)  # fmt: skip
        assert isinstance(res.outputs["forecast"], DatasetValue)
    else:
        with pytest.raises(MapiError) as err:
            run_node("model.forecast", {"horizon": 4}, model=mv,
                     data=DatasetValue(data=actual), registry=reg)  # fmt: skip
        assert err.value.code is ErrorCode.ACTUALS_NOT_SUPPORTED
