"""The DAG engine: gating, alternatives, branches, caching, failures, retries, cancel."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.workflow import (
    NodeOutputRef,
    NodeRunStatus,
    WorkflowEventType,
    WorkflowRunStatus,
    WorkflowSpec,
    validate_workflow,
)
from mapi_spec.workflow_nodes import PortType
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX
from mapi_workflows.engine import CacheHit, WorkflowEngine
from wf_fakes import (
    baselines,
    dataset,
    drive,
    evidence,
    fails,
    metrics,
    model,
    order,
    plan,
    stat_result,
)

DATA = NodeOutputRef(type=PortType.DATASET, ref="dataset://growth/sales@v1")


def _spec(nodes: list[dict[str, Any]], **extra: Any) -> WorkflowSpec:
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "t", "project": "growth"},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": nodes,
        **extra,
    }
    return WorkflowSpec.model_validate(doc)


def _canonical_fakes(
    *, adf_p: float, kpss_p: float, seasonal: bool, overdiff_acf1: float = -0.1
) -> dict[str, Any]:
    return {
        "adf": stat_result("test.adf", adf_p),
        "kpss": stat_result("test.kpss", kpss_p),
        "pp": stat_result("test.pp", 0.5),
        "za": stat_result("test.zivot_andrews", 0.5),
        "seas": lambda a: (
            {"strength": 0.9 if seasonal else 0.1, "seasonal": seasonal},
            stat_result("test.seasonal_strength", 0.5)(a)[1],
        ),
        "sdiff": dataset("run://sdiff"),
        "cand": dataset("run://cand"),
        "overdiff": lambda a: (
            {"acf": [1.0, overdiff_acf1]},
            stat_result("test.acf_pacf", 0.5)(a)[1],
        ),
        "diff": dataset("run://diff"),
        "acf": stat_result("test.acf_pacf", 0.01, suggested_p=1),
        "order": order(2, 1),
        "folds": plan(),
        "backtest": metrics("ts.sarimax@^1", 3.0),
        "naive": baselines(naive=5.0, seasonal_naive=4.0),
        "fit": model("model://growth/arimax@v1"),
        "governance": evidence(True),
    }


def test_canonical_template_runs_through_the_engine() -> None:
    spec = TS_READINESS_ARIMAX.spec
    assert validate_workflow(spec).valid
    eng = WorkflowEngine(
        spec, params={k: p.default for k, p in spec.params.items()}, inputs={"data": DATA}
    )
    drive(eng, _canonical_fakes(adf_p=0.6, kpss_p=0.01, seasonal=False))
    assert eng.status is WorkflowRunStatus.SUCCEEDED
    runs = {n.node_id: n for n in eng.node_runs()}
    # the series wanders: difference; no seasonal difference (strength 0.1)
    assert runs["wanders"].result == {"value": True, "arm": "then"}
    assert runs["seasonal"].result == {"value": False, "arm": "else"}
    assert runs["sdiff"].status is NodeRunStatus.SKIPPED
    assert "else" in (runs["sdiff"].skipped_reason or "")
    assert runs["diff"].status is NodeRunStatus.SUCCEEDED
    # diff's input fell back to inputs.data; acf read the differenced data
    # $expr resolved the differencing order and the AR/MA orders into the model settings
    assert runs["order"].params["d"] == 1
    assert runs["order"].params["D"] == 0 and runs["order"].params["period"] == 52
    settings = runs["backtest"].params["settings"]
    assert settings["order"] == [2, 1, 1]
    assert settings["seasonal_order"] == [0, 0, 0, 52]
    # comparison, selection and report are computed inline
    assert runs["compare"].result is not None
    assert runs["compare"].result["best"] == "backtest"
    sel = runs["select"].result
    assert sel is not None and sel["beats_baselines"] is True
    assert runs["select"].outputs["model"].ref == "model://growth/arimax@v1"
    assert runs["governance"].status is NodeRunStatus.SUCCEEDED
    report = runs["report"].outputs["report"].inline
    assert isinstance(report, dict) and report["format"] == "mapi.report/v1"
    texts = " ".join(s["text"] for s in report["sections"])
    assert "wanders" in texts
    outs = eng.outputs()
    assert outs["model"].ref == "model://growth/arimax@v1"
    assert {d.node_id: d.arm for d in eng.decisions.values()} == {
        "wanders": "then",
        "seasonal": "else",
    }
    decision = eng.decisions["wanders"]
    assert decision.inputs["nodes.adf.reject"] is False
    assert decision.inputs["nodes.kpss.reject"] is True
    types = [e.type for e in eng.drain_events()]
    assert types[0] is WorkflowEventType.RUN_STARTED
    assert types[-1] is WorkflowEventType.RUN_SUCCEEDED
    assert WorkflowEventType.BRANCH_DECIDED in types


def test_stationary_series_skips_differencing() -> None:
    spec = TS_READINESS_ARIMAX.spec
    eng = WorkflowEngine(
        spec, params={k: p.default for k, p in spec.params.items()}, inputs={"data": DATA}
    )
    drive(eng, _canonical_fakes(adf_p=0.01, kpss_p=0.5, seasonal=True))
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["wanders"].result == {"value": False, "arm": "else"}
    assert runs["diff"].status is NodeRunStatus.SKIPPED
    assert runs["sdiff"].status is NodeRunStatus.SUCCEEDED
    assert runs["backtest"].params["settings"]["order"] == [2, 0, 1]
    assert runs["backtest"].params["settings"]["seasonal_order"] == [0, 1, 0, 52]
    assert eng.status is WorkflowRunStatus.SUCCEEDED


def test_the_over_differencing_guard_keeps_d_at_zero() -> None:
    """The unit-root tests say "wanders", but one more difference would leave a lag-1
    autocorrelation below -0.5 (a unit root that is not there): no difference."""
    spec = TS_READINESS_ARIMAX.spec
    eng = WorkflowEngine(
        spec, params={k: p.default for k, p in spec.params.items()}, inputs={"data": DATA}
    )
    drive(eng, _canonical_fakes(adf_p=0.6, kpss_p=0.01, seasonal=True, overdiff_acf1=-0.62))
    assert eng.status is WorkflowRunStatus.SUCCEEDED
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["wanders"].result == {"value": False, "arm": "else"}
    assert runs["diff"].status is NodeRunStatus.SKIPPED
    assert runs["backtest"].params["settings"]["order"] == [2, 0, 1]
    assert runs["order"].params["D"] == 1


def test_select_refuses_a_model_that_loses_to_the_benchmarks() -> None:
    spec = TS_READINESS_ARIMAX.spec
    eng = WorkflowEngine(
        spec, params={k: p.default for k, p in spec.params.items()}, inputs={"data": DATA}
    )
    fakes = _canonical_fakes(adf_p=0.6, kpss_p=0.01, seasonal=False)
    fakes["backtest"] = metrics("ts.sarimax@^1", 4.5)
    drive(eng, fakes)
    runs = {n.node_id: n for n in eng.node_runs()}
    sel = runs["select"]
    assert sel.result is not None and sel.result["beats_baselines"] is False
    assert "does not beat" in str(sel.result["reason_plain"])
    assert "model" not in sel.outputs
    # governance needs the selected model: skipped, and the report still runs
    assert runs["governance"].status is NodeRunStatus.SKIPPED
    assert runs["report"].status is NodeRunStatus.SUCCEEDED
    assert "model" not in eng.outputs()
    assert eng.status is WorkflowRunStatus.SUCCEEDED


def test_alternatives_take_the_first_available_source() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "b", "use": "branch.if", "with": {"condition": "not nodes.adf.reject"}},
            {
                "id": "diff",
                "use": "transform.difference",
                "inputs": {"data": "inputs.data"},
                "when": "b.then",
            },
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": ["diff.data", "inputs.data"]}},
        ]
    )
    seen: dict[str, Any] = {}

    def kpss(a: Any) -> Any:
        seen["data"] = a.inputs["data"][0].ref
        return stat_result("test.kpss", 0.5)(a)

    for adf_p, expected in ((0.01, DATA.ref), (0.9, "run://diff")):
        eng = WorkflowEngine(spec, inputs={"data": DATA})
        drive(
            eng,
            {"adf": stat_result("test.adf", adf_p), "diff": dataset("run://diff"), "kpss": kpss},
        )
        assert seen["data"] == expected
        assert eng.status is WorkflowRunStatus.SUCCEEDED


def test_required_input_without_any_source_is_skipped() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "b", "use": "branch.if", "with": {"condition": "nodes.adf.reject"}},
            {
                "id": "diff",
                "use": "transform.difference",
                "inputs": {"data": "inputs.data"},
                "when": "b.then",
            },
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": "diff.data"}},
        ]
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(eng, {"adf": stat_result("test.adf", 0.9)})
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["diff"].status is NodeRunStatus.SKIPPED
    assert runs["kpss"].status is NodeRunStatus.SKIPPED
    assert "skipped" in (runs["kpss"].skipped_reason or "")
    assert eng.status is WorkflowRunStatus.SUCCEEDED


def test_cache_hits_reuse_results_without_running() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}, "cache": False},
        ]
    )
    store: dict[str, CacheHit] = {}
    first = WorkflowEngine(spec, inputs={"data": DATA}, cache=store.get)
    drive(first, {"adf": stat_result("test.adf", 0.3), "kpss": stat_result("test.kpss", 0.3)})
    for n in first.node_runs():
        assert n.cache_key is not None
        store[n.cache_key] = CacheHit(
            result=n.result or {}, outputs=n.outputs, workflow_run_id="run_first"
        )
    second = WorkflowEngine(spec, inputs={"data": DATA}, cache=store.get)
    rounds = drive(second, {"kpss": stat_result("test.kpss", 0.3)})
    runs = {n.node_id: n for n in second.node_runs()}
    assert runs["adf"].status is NodeRunStatus.CACHED
    assert runs["adf"].cached_from == "run_first"
    assert runs["kpss"].status is NodeRunStatus.SUCCEEDED  # cache: false
    assert rounds == [["kpss"]]
    # another dataset version gives other keys
    other = WorkflowEngine(
        spec,
        inputs={"data": DATA},
        cache=store.get,
        input_identities={"data": "sha256:" + "1" * 64},
    )
    rounds = drive(
        other, {"adf": stat_result("test.adf", 0.3), "kpss": stat_result("test.kpss", 0.3)}
    )
    assert sorted(rounds[0]) == ["adf", "kpss"]
    no_cache = WorkflowEngine(spec, inputs={"data": DATA}, cache=store.get, use_cache=False)
    rounds = drive(
        no_cache, {"adf": stat_result("test.adf", 0.3), "kpss": stat_result("test.kpss", 0.3)}
    )
    assert sorted(rounds[0]) == ["adf", "kpss"]


def test_fail_fast_cancels_the_rest() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}},
            {
                "id": "diff",
                "use": "transform.difference",
                "inputs": {"data": "inputs.data"},
                "with": {"order": {"$expr": "if(nodes.adf.reject, 1, 2)"}},
            },
        ],
        execution={"max_parallel": 1},
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(eng, {"adf": fails(ErrorCode.SCHEMA_INVALID, "no target")})
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["adf"].status is NodeRunStatus.FAILED
    assert runs["kpss"].status is NodeRunStatus.CANCELLED
    assert runs["diff"].status is NodeRunStatus.CANCELLED
    assert eng.status is WorkflowRunStatus.FAILED
    assert eng.error is not None and "adf" in eng.error.message
    assert eng.drain_events()[-1].type is WorkflowEventType.RUN_FAILED


def test_continue_counts_as_skipped_for_dependants() -> None:
    spec = _spec(
        [
            {
                "id": "adf",
                "use": "test.adf",
                "inputs": {"data": "inputs.data"},
                "on_failure": "continue",
            },
            {"id": "diff", "use": "transform.difference", "inputs": {"data": "inputs.data"}},
            {
                "id": "b",
                "use": "branch.if",
                "with": {"condition": "coalesce(nodes.adf.reject, true)"},
            },
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": "diff.data"}, "when": "b.then"},
        ]
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(
        eng,
        {
            "adf": fails(ErrorCode.USER_CODE_ERROR),
            "diff": dataset("run://d"),
            "kpss": stat_result("test.kpss", 0.2),
        },
    )
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["adf"].status is NodeRunStatus.FAILED
    assert runs["b"].result == {"value": True, "arm": "then"}
    assert runs["kpss"].status is NodeRunStatus.SUCCEEDED
    assert eng.status is WorkflowRunStatus.SUCCEEDED


def test_retries_requeue_retryable_failures_only() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}, "retries": 2},
            {
                "id": "kpss",
                "use": "test.kpss",
                "inputs": {"data": "inputs.data"},
                "retries": 2,
                "on_failure": "continue",
            },
        ]
    )
    calls = {"adf": 0}

    def flaky(a: Any) -> Any:
        calls["adf"] += 1
        if calls["adf"] < 3:
            return fails(ErrorCode.INFRA_ERROR, "lost")(a)
        return stat_result("test.adf", 0.2)(a)

    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(eng, {"adf": flaky, "kpss": fails(ErrorCode.SCHEMA_INVALID, "bad data")})
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["adf"].attempt == 3
    assert runs["adf"].status is NodeRunStatus.SUCCEEDED
    assert runs["kpss"].attempt == 1  # not retryable
    assert runs["kpss"].status is NodeRunStatus.FAILED
    retrying = [e for e in eng.drain_events() if e.type is WorkflowEventType.NODE_RETRYING]
    assert len(retrying) == 2


def test_cancel_stops_pending_and_lists_running() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "b", "use": "branch.if", "with": {"condition": "nodes.adf.reject"}},
            {
                "id": "diff",
                "use": "transform.difference",
                "inputs": {"data": "inputs.data"},
                "when": "b.then",
            },
        ]
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    actions = eng.step()
    assert [a.node_id for a in actions] == ["adf"]
    eng.started_node("adf", run_id="run_x")
    eng.cancel()
    assert eng.to_cancel == {"adf"}
    assert eng.status is WorkflowRunStatus.CANCELLING
    assert eng.step() == []
    eng.cancelled("adf")
    assert eng.finished and eng.status is WorkflowRunStatus.CANCELLED
    assert {n.status for n in eng.node_runs()} == {NodeRunStatus.CANCELLED}


def test_max_parallel_limits_what_starts() -> None:
    nodes = [
        {"id": f"t{i}", "use": "test.adf", "inputs": {"data": "inputs.data"}} for i in range(5)
    ]
    nodes.append(
        {
            "id": "rep",
            "use": "report.summary",
            "inputs": {"tests": [f"t{i}.result" for i in range(5)]},
        }
    )
    spec = _spec(nodes, execution={"max_parallel": 2})
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    rounds = drive(eng, {"test.adf": stat_result("test.adf", 0.2)})
    assert [len(r) for r in rounds] == [2, 2, 1]
    assert eng.status is WorkflowRunStatus.SUCCEEDED


def test_expression_errors_fail_the_node_with_a_plain_message() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {
                "id": "diff",
                "use": "transform.difference",
                "inputs": {"data": "inputs.data"},
                "with": {"order": {"$expr": "nodes.adf.statistic + 'x'"}},
            },
        ]
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(eng, {"adf": stat_result("test.adf", 0.2)})
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["diff"].status is NodeRunStatus.FAILED
    assert runs["diff"].error is not None
    assert runs["diff"].error.code is ErrorCode.EXPRESSION_ERROR


def test_resolved_params_are_checked_against_the_schema() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {
                "id": "diff",
                "use": "transform.difference",
                "inputs": {"data": "inputs.data"},
                "with": {"order": {"$expr": "nodes.adf.n_obs"}},
            },
        ]
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(eng, {"adf": stat_result("test.adf", 0.2, n_obs=40)})
    runs = {n.node_id: n for n in eng.node_runs()}
    assert runs["diff"].status is NodeRunStatus.FAILED
    assert runs["diff"].error is not None
    assert runs["diff"].error.code is ErrorCode.WORKFLOW_INVALID
    assert "maximum" in runs["diff"].error.message
    # defaults from the schema are applied and recorded
    assert runs["adf"].params["regression"] == "c"
    assert runs["adf"].params["autolag"] == "AIC"


def test_restore_rebuilds_the_same_state() -> None:
    spec = TS_READINESS_ARIMAX.spec
    params = {k: p.default for k, p in spec.params.items()}
    eng = WorkflowEngine(spec, params=params, inputs={"data": DATA})
    fakes = _canonical_fakes(adf_p=0.6, kpss_p=0.01, seasonal=False)
    # run the first round only, then rebuild from records and finish
    actions = eng.step()
    for a in actions:
        eng.started_node(a.node_id, run_id=f"run_{a.node_id}")
        result, outputs = fakes[a.node_id](a)
        eng.succeeded(a.node_id, result, outputs)
    restored = WorkflowEngine.restore(
        spec,
        copy.deepcopy(eng.node_runs()),
        list(eng.decisions.values()),
        params=params,
        inputs={"data": DATA},
    )
    drive(restored, fakes)
    assert restored.status is WorkflowRunStatus.SUCCEEDED
    assert restored.outputs()["model"].ref == "model://growth/arimax@v1"


def test_unknown_implementations_are_refused() -> None:
    spec = _spec([{"id": "x", "use": "test.adf", "inputs": {"data": "inputs.data"}}])
    with pytest.raises(MapiError) as exc:
        WorkflowEngine(spec, registry={})
    assert exc.value.code is ErrorCode.NODE_UNKNOWN


def test_outputs_must_match_declared_ports() -> None:
    spec = _spec([{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}])
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    (_a,) = eng.step()
    eng.started_node("adf")
    eng.succeeded("adf", {}, {"result": NodeOutputRef(type=PortType.DATASET, ref="x")})
    run = eng.node_run("adf")
    assert run.status is NodeRunStatus.FAILED
    assert run.error is not None and run.error.code is ErrorCode.INTEGRITY_FAILURE


def test_unknown_cache_keys_wait_for_the_driver() -> None:
    from mapi_workflows.engine import UNKNOWN

    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}},
        ]
    )
    known: dict[str, Any] = {}
    eng = WorkflowEngine(spec, inputs={"data": DATA}, cache=lambda k: known.get(k, UNKNOWN))
    assert eng.step() == []
    keys = eng.take_lookups()
    assert len(keys) == 2 and eng.take_lookups() == set()
    adf_key = eng.node_run("adf").cache_key
    assert adf_key is not None
    for k in keys:
        known[k] = None
    known[adf_key] = CacheHit(result={"reject": True}, outputs={}, workflow_run_id="run_x")
    actions = eng.step()
    assert [a.node_id for a in actions] == ["kpss"]
    assert eng.node_run("adf").status is NodeRunStatus.CACHED


def test_a_restored_failed_run_still_lists_its_running_nodes_to_cancel() -> None:
    """``to_cancel`` is not persisted: restoring a failed run lists the nodes still running."""
    from mapi_spec.workflow import NodeError

    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}},
        ]
    )
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    for a in eng.step():
        eng.attach_run(a.node_id, f"run_{a.node_id}")
    eng.failed("kpss", NodeError(code=ErrorCode.SETTINGS_INVALID, message="bad"), retryable=False)
    assert eng.to_cancel == {"adf"} and eng.error is not None
    restored = WorkflowEngine.restore(
        spec, copy.deepcopy(eng.node_runs()), error=eng.error, inputs={"data": DATA}
    )
    assert restored.to_cancel == {"adf"}
    restored.cancelled("adf")
    assert restored.status is WorkflowRunStatus.FAILED


def test_decisions_and_selections_are_explained_in_plain_words() -> None:
    """A branch's ``explain_then`` / ``explain_else`` say what its path means; the report and
    the selection name steps by their labels, never by "then"/"else" or node ids."""
    spec = TS_READINESS_ARIMAX.spec
    eng = WorkflowEngine(
        spec, params={k: p.default for k, p in spec.params.items()}, inputs={"data": DATA}
    )
    drive(eng, _canonical_fakes(adf_p=0.6, kpss_p=0.01, seasonal=False))
    wanders, seasonal = eng.decisions["wanders"], eng.decisions["seasonal"]
    assert wanders.explain_plain is not None
    assert wanders.explain_plain.startswith("The tests found that the series wanders")
    assert seasonal.explain_plain is not None
    assert "no strong seasonal pattern" in seasonal.explain_plain
    runs = {n.node_id: n for n in eng.node_runs()}
    # the branch result (what conditions read) is unchanged
    assert runs["wanders"].result == {"value": True, "arm": "then"}
    sel = runs["select"].result
    assert sel is not None and sel["selected"] == "backtest"  # ids stay machine-readable
    assert str(sel["reason_plain"]).startswith("ARIMAX has the best average error")
    assert "the strongest, Naive forecast" in str(sel["reason_plain"])
    report = runs["report"].outputs["report"].inline
    assert isinstance(report, dict)
    summary = str(report["summary_plain"])
    # the seasonal decision comes first, so this one continues the sentence
    assert wanders.explain_plain.rstrip(".").lower() in summary.lower()
    assert "-> then" not in summary and "-> else" not in summary
    assert "ARIMAX has the best" in summary
    section = next(s for s in report["sections"] if s["node_id"] == "wanders")
    assert section["text"].startswith(wanders.explain_plain)


def test_a_branch_without_explanations_reads_yes_or_no() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "wanders", "use": "branch.if", "label": "Difference?",
             "with": {"condition": "not nodes.adf.reject"}},
            {"id": "diff", "use": "transform.difference", "inputs": {"data": "inputs.data"},
             "when": "wanders.then"},
            {"id": "report", "use": "report.summary", "inputs": {"tests": ["adf.result"]}},
        ]
    )  # fmt: skip
    eng = WorkflowEngine(spec, inputs={"data": DATA})
    drive(eng, {"adf": stat_result("test.adf", 0.6), "diff": dataset("run://diff")})
    assert eng.decisions["wanders"].explain_plain is None
    runs = {n.node_id: n for n in eng.node_runs()}
    report = runs["report"].outputs["report"].inline
    assert isinstance(report, dict)
    assert "Decisions: Difference? yes." in str(report["summary_plain"])


def test_branch_explanations_are_checked() -> None:
    spec = _spec(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "b", "use": "branch.if",
             "with": {"condition": "nodes.adf.reject", "explain_then": "x" * 301}},
        ]
    )  # fmt: skip
    report = validate_workflow(spec)
    assert not report.valid
    assert any("explain_then" in (i.pointer or "") for i in report.issues)
