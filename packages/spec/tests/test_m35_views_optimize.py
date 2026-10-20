"""M3.5 explorer views, explore requests, statistical test results, budget optimisation and
the new run jobs."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RUN_KIND_MAX_LEN, RunKind
from mapi_spec.explore import DataExploreRequest, ExploreJob, ModelExploreRequest
from mapi_spec.jobs import parse_job
from mapi_spec.optimize import (
    ChannelBounds,
    OptimizeJob,
    OptimizeRequest,
    OptimizeResult,
    SpendGroup,
    check_feasible,
)
from mapi_spec.stattests import (
    TEST_DEFINITIONS,
    StatTestResult,
    decide,
    make_test_result,
)
from mapi_spec.views import (
    DATA_SECTIONS,
    MODEL_SECTIONS,
    CorrelationView,
    ExplorerIndex,
    FevdView,
    Histogram,
    IrfView,
    LagProfile,
    ResponseCurvesView,
    SeriesView,
    StationarityRow,
    StlComponents,
    explore_view_name,
)
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
from pydantic import ValidationError

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------------------- test results


@pytest.mark.parametrize(
    ("test", "p", "reject", "decision"),
    [
        ("test.adf", 0.01, True, "stationary"),
        ("test.adf", 0.57, False, "unit_root"),
        ("test.kpss", 0.01, True, "unit_root"),  # KPSS's null is the stable case
        ("test.kpss", 0.10, False, "stationary"),
        ("test.ljung_box", 0.30, False, "white_noise"),
        ("test.granger", 0.001, True, "predictive"),
        ("test.adf", 0.05, False, "unit_root"),  # p == alpha does not reject
    ],
)
def test_decisions(test: str, p: float, reject: bool, decision: str) -> None:
    got = decide(test, p, 0.05)
    assert got[:2] == (reject, decision) and got[2]
    result = make_test_result(test, statistic=-1.0, p_value=p, var="y", lags=3)
    assert result.reject is reject and result.decision == decision and result.null_plain


def test_every_test_node_has_a_definition_and_bad_inputs_are_refused() -> None:
    tests = {i for i in NODE_IMPLEMENTATIONS if i.startswith("test.")}
    assert tests == set(TEST_DEFINITIONS)
    for bad in ((-0.1, 0.05), (1.2, 0.05), (float("nan"), 0.05), (0.5, 0.0), (0.5, 1.0)):
        with pytest.raises(ValueError):
            decide("test.adf", *bad)
    with pytest.raises(KeyError):
        decide("test.nope", 0.5, 0.05)
    with pytest.raises(ValidationError, match="does not match"):
        StatTestResult(
            test="test.adf", reject=True, decision="unit_root", verdict_plain="x", null_plain="y"
        )
    with pytest.raises(ValidationError, match="unknown decision"):
        StatTestResult(
            test="test.adf", reject=True, decision="maybe", verdict_plain="x", null_plain="y"
        )
    skipped = StatTestResult(
        test="test.adf",
        reject=False,
        decision="n/a",
        verdict_plain="-",
        null_plain="-",
        status="skipped",
        message_plain="too short",
    )
    assert skipped.status == "skipped"


# ------------------------------------------------------------------------------------- views


def test_view_shapes_are_checked() -> None:
    with pytest.raises(ValidationError, match="one more edge"):
        Histogram(edges=[0, 1], counts=[1, 2])
    with pytest.raises(ValidationError, match="ascend"):
        Histogram(edges=[1, 0], counts=[1])
    with pytest.raises(ValidationError, match="same length"):
        SeriesView(var="y", coords=[1, 2], values=[1.0])
    with pytest.raises(ValidationError, match="same length"):
        LagProfile(var="y", lags=[0, 1], values=[1.0])
    with pytest.raises(ValidationError, match="square"):
        CorrelationView(vars=["a", "b"], matrix=[[1.0, 0.2]])
    with pytest.raises(ValidationError, match="one value per coord"):
        StlComponents(
            var="y",
            period=4,
            coords=[1, 2],
            observed=[1, 2],
            trend=[1, 2],
            seasonal=[0],
            resid=[0, 0],
        )
    with pytest.raises(ValidationError, match="response"):
        IrfView(variables=["a", "b"], horizons=1, values=[[[1.0, 0.5]]])
    irf = IrfView(variables=["a"], horizons=2, values=[[[1.0, 0.5, 0.2]]])
    assert irf.orthogonalized
    with pytest.raises(ValidationError, match="sums to 1"):
        FevdView(variables=["a", "b"], horizons=1, shares=[[[0.5, 0.2]], [[0.5, 0.5]]])
    FevdView(variables=["a", "b"], horizons=1, shares=[[[0.8, 0.2]], [[0.3, 0.7]]])
    with pytest.raises(ValidationError, match="channel"):
        ResponseCurvesView(channels=["tv"], spend=[[0, 1], [0, 1]], mean=[[0, 1]])
    row = StationarityRow(
        var="y",
        verdict="difference",
        verdict_plain="Difference once.",
        adf=make_test_result("test.adf", statistic=-1.0, p_value=0.6),
    )
    assert row.adf is not None and row.adf.decision == "unit_root"


def test_sections_and_index() -> None:
    assert len(DATA_SECTIONS) == 21 and len(MODEL_SECTIONS) == 12
    assert explore_view_name("stl") == "ui/explore/stl.json"
    with pytest.raises(ValueError):
        explore_view_name("../../secrets")
    idx = ExplorerIndex.model_validate(ExplorerIndex.model_json_schema()["examples"][0])
    assert idx.sections[1].status == "skipped"


def test_explore_requests_and_jobs() -> None:
    assert DataExploreRequest().sections == list(DATA_SECTIONS)
    assert ModelExploreRequest().sections == list(MODEL_SECTIONS)
    with pytest.raises(ValidationError, match="unique"):
        DataExploreRequest(sections=["stl", "stl"])
    with pytest.raises(ValidationError):
        DataExploreRequest(sections=["telepathy"])  # type: ignore[list-item]
    with pytest.raises(ValidationError, match="model ref"):
        ModelExploreRequest(compare_with=["dataset://x/y"])
    plan = ModelExploreRequest.model_validate(
        {"backtest": {"kind": "expanding_window", "horizons": [4]}}
    )
    assert plan.backtest is not None
    job = parse_job(
        RunKind.EXPLORE, {"target": "dataset", "ref": "dataset://g/s@v1", "data": {"kpi": "y"}}
    )
    assert isinstance(job, ExploreJob) and job.data is not None and job.data.kpi == "y"
    with pytest.raises(MapiError) as exc:
        parse_job(RunKind.EXPLORE, {"target": "model", "ref": "model://g/m@v1"})
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


# ----------------------------------------------------------------------------- optimisation


def test_optimize_requests() -> None:
    for ex in OptimizeRequest.model_json_schema()["examples"]:
        OptimizeRequest.model_validate(ex)
    for bad, msg in (
        ({"total_budget": 1, "budget_change_pct": 5}, "not both"),
        ({"mode": "free", "total_budget": 10}, "drop the budget"),
        ({"objective": "median"}, "objective"),
        ({"objective": "p99"}, "objective"),
        ({"channels": {"tv": {"min_multiplier": 2, "max_multiplier": 1}}}, "must not exceed"),
        ({"groups": [{"name": "g", "channels": ["a"]}]}, "at least one bound"),
        ({"groups": [{"name": "g", "channels": ["a"], "min_share": 0.6, "max_share": 0.5}]},
         "min_share"),
        ({"groups": [{"name": "g", "channels": ["a"], "min_share": 0.1},
                     {"name": "g", "channels": ["b"], "min_share": 0.1}]}, "unique"),
    ):  # fmt: skip
        with pytest.raises(ValidationError, match=msg):
            OptimizeRequest.model_validate(bad)
    assert OptimizeRequest(objective="cvar5").objective == "cvar5"


@pytest.mark.parametrize(
    ("req", "message"),
    [
        ({"channels": {"radio": {}}}, "unknown channels"),
        (
            {
                "total_budget": 100,
                "channels": {"tv": {"min_spend": 90}, "search": {"min_spend": 20}},
            },
            "minimums add up",
        ),
        ({"total_budget": 1000}, "maximums add up"),
        (
            {
                "total_budget": 300,
                "groups": [
                    {"name": "a", "channels": ["tv"], "min_share": 0.6},
                    {"name": "b", "channels": ["search"], "min_share": 0.6},
                ],
            },
            "more than 100%",
        ),
    ],
)
def test_infeasible_requests(req: dict[str, Any], message: str) -> None:
    current = {"tv": 200.0, "search": 100.0}
    with pytest.raises(MapiError) as exc:
        check_feasible(OptimizeRequest.model_validate(req), current)
    assert exc.value.code is ErrorCode.OPTIMIZE_INFEASIBLE and message in exc.value.detail


def test_feasible_requests_pass() -> None:
    current = {"tv": 200.0, "search": 100.0}
    check_feasible(OptimizeRequest(total_budget=300), current)
    check_feasible(OptimizeRequest(budget_change_pct=-10), current)
    check_feasible(OptimizeRequest(mode="free"), current)
    locked = OptimizeRequest(
        total_budget=300,
        channels={"tv": ChannelBounds(locked=True)},
        groups=[SpendGroup(name="d", channels=["search"], min_share=0.2)],
    )
    check_feasible(locked, current)


def test_optimize_results_and_jobs() -> None:
    ex = OptimizeResult.model_json_schema()["examples"][0]
    res = OptimizeResult.model_validate(ex)
    assert sum(a.optimal_spend for a in res.allocation) == res.total_budget
    off = dict(ex, total_budget=2_000_000)
    with pytest.raises(ValidationError, match="add up"):
        OptimizeResult.model_validate(off)
    job = parse_job(RunKind.OPTIMIZE, {"request": {"total_budget": 10}, "model": "model://g/m@v1"})
    assert isinstance(job, OptimizeJob) and job.request.total_budget == 10


def test_new_run_kinds_fit_the_column_and_parents_never_execute() -> None:
    assert all(len(k.value) <= RUN_KIND_MAX_LEN for k in RunKind)
    assert RunKind.WORKFLOW.is_parent and not RunKind.WORKFLOW_NODE.is_parent
    with pytest.raises(NotImplementedError, match="fan-out parents"):
        parse_job(RunKind.WORKFLOW, {})
    node = parse_job(
        RunKind.WORKFLOW_NODE,
        {
            "workflow_run_id": "run_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "node_id": "adf",
            "use": "test.adf@1.0.0",
        },
    )
    assert node.node_id == "adf"  # type: ignore[union-attr]
