"""Run tracking of search.grid and model.backtest nodes (v0.11): trial params, scores, tags."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_sdk.tracking import MetricRecorder
from mapi_workflows.runtime.models import backtest_node, grid_node, trial_params
from wf_ctx import node_ctx, run_node
from wf_models import registry, weekly_series

pytestmark = pytest.mark.filterwarnings("ignore")

ORDERS = [[0, 0, 0], [1, 0, 0], [2, 0, 0]]


def _params(**extra: Any) -> dict[str, Any]:
    return {
        "model": "ts.sarimax@^1",
        "settings": {"order": [1, 0, 0], "exog": ["price"]},
        "grid": {"/order": ORDERS},
        "metric": "rmse",
        **extra,
    }


def _plan(ds: Any) -> dict[str, Any]:
    out = run_node("split.expanding_window", {"horizons": [1, 2], "n_folds": 2, "step": 8}, data=ds)
    plan = out.outputs["plan"]
    assert isinstance(plan, dict)
    return plan


def test_grid_logs_trial_params_scores_best_metric_and_tag() -> None:
    ds = weekly_series(80)
    rec = MetricRecorder()
    ctx = node_ctx("search.grid", _params(), data=ds, split=_plan(ds), registry=registry())
    ctx.services["sandbox"] = True
    ctx.tracker = rec
    res = grid_node(ctx).result

    assert rec.params["grid.metric"] == "rmse"
    assert rec.params["grid.direction"] == "min"
    assert rec.params["grid.points"] == 3
    assert rec.params["grid.nested"] is False
    assert rec.params["trial.0.order"] == "[0,0,0]"
    assert rec.params["trial.2.order"] == "[2,0,0]"
    series = rec.series()
    rmse = series[("rmse", "")]
    assert [s for s, _ in rmse] == [0, 1, 2]
    assert ("mae", "") in series
    best = series[("best_rmse", "")]
    assert best == [(None, pytest.approx(res["best_score"]))]
    point = int(rec.tags["grid.best_point"])
    assert rmse[point][1] == pytest.approx(res["best_score"])
    assert not rec.dropped


def test_nested_grid_labels_inner_and_outer_scores() -> None:
    ds = weekly_series(120, beta=3.0)
    plan = run_node(
        "split.nested",
        {
            "outer": {"kind": "expanding_window", "horizons": [1, 2], "n_folds": 2, "step": 8},
            "inner": {"kind": "expanding_window", "horizons": [1, 2], "n_folds": 2, "step": 4},
        },
        data=ds,
    ).outputs["plan"]
    rec = MetricRecorder()
    ctx = node_ctx("search.grid", _params(), data=ds, split=plan, registry=registry())
    ctx.services["sandbox"] = True
    ctx.tracker = rec
    res = grid_node(ctx).result

    series = rec.series()
    assert rec.params["grid.nested"] is True
    for j in range(2):
        inner = series[("rmse", f"fold={j},stage=inner")]
        assert [s for s, _ in inner] == [0, 1, 2]
    outer = series[("rmse", "stage=outer")]
    assert [s for s, _ in outer] == [0, 1]
    assert [v for _, v in outer] == pytest.approx(res["outer_scores"])
    assert series[("best_rmse", "")] == [(None, pytest.approx(res["best_score"]))]
    assert "grid.best_point" in rec.tags


def test_backtest_logs_one_scalar_per_fold() -> None:
    ds = weekly_series(80)
    rec = MetricRecorder()
    ctx = node_ctx(
        "model.backtest",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=ds,
        split=_plan(ds),
        registry=registry(),
    )
    ctx.tracker = rec
    res = backtest_node(ctx).result
    series = rec.series()
    assert res["n_folds"] == 2
    for j in range(2):
        mae = series[("mae", f"fold={j}")]
        assert len(mae) == 1 and mae[0][0] is None


def test_grid_without_tracker_still_validates_and_runs() -> None:
    ds = weekly_series(80)
    ctx = node_ctx("search.grid", _params(), data=ds, split=_plan(ds), registry=registry())
    ctx.services["sandbox"] = True
    assert ctx.tracker is None and not ctx.tracking
    assert grid_node(ctx).result["n_points"] == 3


def test_trial_params_decode_pointers_and_skip_bad_keys() -> None:
    points: list[tuple[dict[str, Any], dict[str, Any]]] = [
        ({}, {"/adstock/l_max": 4, "/a~1b": True, "/prior": {"mu": 0}}),
        ({}, {"/bad key": 1}),
    ]
    params, notes = trial_params(points)
    assert params == {
        "trial.0.adstock/l_max": 4,
        "trial.0.a/b": True,
        "trial.0.prior": '{"mu":0}',
    }
    assert len(notes) == 1 and "trial.1.bad key" in notes[0]
