"""The other gallery templates in-process, and runner features (sub-workflows, timeouts,
cancellation, invalid documents)."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

import pytest
from mapi_sdk.steps import CancelToken
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.refs import WorkflowRef
from mapi_spec.workflow import NodeRunStatus, WorkflowRunStatus, WorkflowSpec
from mapi_spec.workflow_templates import FORECAST_BAKEOFF, VAR_VECM, instantiate_template
from mapi_workflows.payloads import MetricsPayload, TablePayload
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.runtime.values import ModelValue, NodeContext, NodeOutcome
from wf_models import multi_series, registry, weekly_series

pytestmark = pytest.mark.filterwarnings("ignore")


def test_var_vecm_template_takes_the_vecm_path_for_tied_series() -> None:
    spec = instantiate_template(VAR_VECM, name="prices", project="growth")
    res = LocalRunner(registry=registry()).run(
        spec,
        inputs={"data": multi_series(220)},
        params={"target": "a", "driver": "b", "maxlags": 4},
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}
    assert runs["tied"].result == {"value": True, "arm": "then"}
    assert runs["rank"].result is not None and runs["rank"].result["rank"] >= 1
    assert runs["vecm"].status is NodeRunStatus.SUCCEEDED
    assert runs["vecm"].params["settings"]["coint_rank"] == runs["rank"].result["rank"]
    # the VAR path was not taken: differences, lag search and VAR are skipped
    for nid in ("diff", "lags", "var"):
        assert runs[nid].status is NodeRunStatus.SKIPPED
    # Granger ran on the levels (its first alternative, the differences, was skipped)
    assert runs["granger"].status is NodeRunStatus.SUCCEEDED
    assert res.values["report"]["format"] == "mapi.report/v1"  # type: ignore[index]


def test_var_vecm_template_takes_the_var_path_for_independent_series() -> None:
    spec = instantiate_template(VAR_VECM, name="prices", project="growth")
    res = LocalRunner(registry=registry()).run(
        spec,
        inputs={"data": multi_series(220, tied=False, seed=9)},
        params={"target": "a", "driver": "c", "maxlags": 4},
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}
    assert runs["tied"].result == {"value": False, "arm": "else"}
    assert runs["vecm"].status is NodeRunStatus.SKIPPED
    assert runs["var"].status is NodeRunStatus.SUCCEEDED
    assert runs["var"].params["settings"]["lags"] == runs["lags"].result["best_lag"]  # type: ignore[index]


def test_var_vecm_template_fits_a_var_on_the_levels_of_stationary_series() -> None:
    """Regression: for stationary series Johansen rejects every null (rank = number of
    series); the template sent that full rank to ts.vecm, which refused it, and the run
    failed. Stationary series are modelled by a VAR on their levels."""
    spec = instantiate_template(VAR_VECM, name="rates", project="growth")
    res = LocalRunner(registry=registry()).run(
        spec,
        inputs={"data": multi_series(220, stationary=True, seed=5)},
        params={"target": "a", "driver": "b", "maxlags": 4},
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}
    assert runs["johansen"].result is not None and runs["johansen"].result["rank"] == 3
    assert runs["tied"].result == {"value": False, "arm": "else"}
    assert runs["stationary"].result == {"value": True, "arm": "then"}
    for nid in ("rank", "vecm", "diff"):
        assert runs[nid].status is NodeRunStatus.SKIPPED
    assert runs["lags"].status is NodeRunStatus.SUCCEEDED  # on the levels (no diff)
    assert runs["var"].status is NodeRunStatus.SUCCEEDED
    assert runs["granger"].status is NodeRunStatus.SUCCEEDED
    assert runs["report"].status is NodeRunStatus.SUCCEEDED


def _bakeoff_without_the_bayesian_model() -> WorkflowSpec:
    doc = copy.deepcopy(FORECAST_BAKEOFF.document)
    nodes = [n for n in doc["nodes"] if n["id"] != "smooth"]  # type: ignore[union-attr]
    for n in nodes:
        if n["id"] == "compare":  # type: ignore[index]
            n["inputs"]["candidates"].remove("smooth.metrics")  # type: ignore[index]
        if n["id"] == "sarimax":  # type: ignore[index]
            n["with"]["settings"]["seasonal_order"] = [0, 0, 0, 52]  # type: ignore[index]
    doc["nodes"] = nodes
    return WorkflowSpec.model_validate(doc)


def test_forecast_bakeoff_compares_and_refits_the_winner() -> None:
    spec = _bakeoff_without_the_bayesian_model()
    res = LocalRunner(registry=registry()).run(
        spec,
        inputs={"data": weekly_series(160, drift=0.3, seed=21)},
        params={"horizons": [1, 4], "n_folds": 3},
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}
    table = TablePayload.model_validate(res.values["comparison"])
    assert {r["node"] for r in table.rows} == {"sarimax", "ets", "theta", "naive"}
    best = runs["best"].result
    assert best is not None and best["selected"] in ("sarimax", "ets", "theta")
    assert runs["refit"].params["model"] == best["selected_model"]
    model = res.values["model"]
    assert isinstance(model, ModelValue)
    assert model.model is not None and model.model.startswith(
        str(best["selected_model"]).split("@")[0]
    )


def test_subworkflows_run_recursively() -> None:
    inner = WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "checks", "project": "growth"},
            "inputs": {"series": {"type": "dataset"}},
            "nodes": [
                {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.series"}},
                {"id": "acf", "use": "test.acf_pacf", "inputs": {"data": "inputs.series"}},
            ],
            "outputs": {"adf": "adf.result", "acf": "acf.result"},
        }
    )
    outer = WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "outer", "project": "growth"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [
                {
                    "id": "sub",
                    "use": "subworkflow.run",
                    "inputs": {"series": "inputs.data"},
                    "with": {"workflow": "workflow://growth/checks@r1"},
                },
                {
                    "id": "report",
                    "use": "report.summary",
                    "inputs": {"tests": ["sub.adf", "sub.acf"]},
                },
            ],
        }
    )
    seen: list[WorkflowRef] = []

    def resolve(ref: WorkflowRef) -> WorkflowSpec | None:
        seen.append(ref)
        return inner if ref.name == "checks" else None

    res = LocalRunner(registry=registry(), subworkflows=resolve).run(
        outer, inputs={"data": weekly_series(120)}
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    runs = {n.node_id: n for n in res.nodes}
    assert sorted(runs["sub"].outputs) == ["acf", "adf"]
    report = res.node_values["report"]["report"]
    assert isinstance(report, dict) and len(report["sections"]) == 2
    assert seen and seen[0].name == "checks"


def test_a_workflow_that_runs_itself_stops() -> None:
    loop = WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "loop", "project": "growth"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [
                {
                    "id": "again",
                    "use": "subworkflow.run",
                    "inputs": {"data": "inputs.data"},
                    "with": {"workflow": "workflow://growth/loop@r1"},
                }
            ],
        }
    )
    res = LocalRunner(registry=registry(), subworkflows=lambda ref: loop).run(
        loop, inputs={"data": weekly_series(40)}
    )
    assert res.status is WorkflowRunStatus.FAILED
    assert res.error is not None and "nested more than" in res.error.message


def _slow(ctx: NodeContext) -> NodeOutcome:
    for _ in range(200):
        ctx.check()
        time.sleep(0.02)
    return NodeOutcome(result={})


def _spec(**node: Any) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "t", "project": "growth"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [
                {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}, **node},
                {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}},
            ],
        }
    )


def test_node_timeouts_are_enforced() -> None:
    runner = LocalRunner(nodes={"test.adf": _slow})
    t0 = time.monotonic()
    res = runner.run(_spec(timeout="1s"), inputs={"data": weekly_series(60)})
    assert time.monotonic() - t0 < 4
    adf = res.node("adf")
    assert adf.status is NodeRunStatus.FAILED
    assert adf.error is not None and adf.error.code is ErrorCode.TIMEOUT
    assert res.status is WorkflowRunStatus.FAILED


def test_cancelling_a_run_stops_the_running_node() -> None:
    runner = LocalRunner(nodes={"test.adf": _slow})
    token = CancelToken()
    threading.Timer(0.2, token.cancel).start()
    res = runner.run(_spec(), inputs={"data": weekly_series(60)}, cancel=token)
    assert res.status is WorkflowRunStatus.CANCELLED
    assert res.node("adf").status is NodeRunStatus.CANCELLED


def test_invalid_workflows_and_params_are_refused() -> None:
    runner = LocalRunner()
    bad = _spec().model_dump(by_alias=True)
    bad["nodes"][1]["inputs"]["data"] = "nowhere.data"
    with pytest.raises(MapiError) as exc:
        runner.run(bad, inputs={"data": weekly_series(60)})
    assert exc.value.code is ErrorCode.WORKFLOW_INVALID
    assert any("nowhere" in i.message for i in exc.value.issues)
    with pytest.raises(MapiError):
        runner.run(_spec(), inputs={})
    yaml_text = """
apiVersion: mapi/v1
kind: Workflow
metadata: {name: y, project: growth}
inputs: {data: {type: dataset}}
params: {alpha: {type: number, default: 0.05}}
nodes:
  - {id: adf, use: test.adf, inputs: {data: inputs.data}, with: {alpha: {$param: alpha}}}
"""
    res = runner.run(yaml_text, inputs={"data": weekly_series(60)}, params={"alpha": 0.1})
    assert res.node("adf").params["alpha"] == 0.1
    with pytest.raises(MapiError):
        runner.run(yaml_text, inputs={"data": weekly_series(60)}, params={"alpha": "x"})


def test_backtest_metrics_carry_the_candidate_model() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "bt", "project": "growth"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [
                {
                    "id": "folds",
                    "use": "split.rolling_origin",
                    "inputs": {"data": "inputs.data"},
                    "with": {"horizons": [1, 2], "window": 60, "n_folds": 3},
                },
                {
                    "id": "bt",
                    "use": "model.backtest",
                    "inputs": {"data": "inputs.data", "split": "folds.plan"},
                    "with": {"model": "ts.ets@^1", "settings": {"trend": "add"}},
                },
            ],
            "outputs": {"m": "bt.metrics"},
        }
    )
    res = LocalRunner(registry=registry()).run(spec, inputs={"data": weekly_series(100)})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    cand = MetricsPayload.model_validate(res.values["m"]).candidates[0]
    assert cand.model == "ts.ets@^1" and cand.settings == {"trend": "add"}
    cov = cand.metrics["coverage_90"]  # the real ETS gives 90% prediction intervals
    assert cov is not None and 0.0 <= cov <= 1.0


@pytest.mark.slow
def test_mmm_governed_template_fits_mmm_framework_and_runs_governance() -> None:
    """The gallery's marketing-mix workflow on a geo panel: the VIF test, a holdout, the
    real mmm.bayesian fit (mmm-framework, tiny sampler settings), the policy's checks with
    the runner's implementations, a report and an evidence pack. The model's ``geo``,
    ``time`` and ``y`` names are bound to the data's ``region``, ``week`` and ``revenue``."""
    from datetime import UTC, datetime

    from mapi_conformance.runner import load_fixture
    from mapi_sdk.registry import default_registry
    from mapi_spec.governance import GovernancePolicy, PolicyCheck
    from mapi_spec.workflow_templates import MMM_GOVERNED
    from mapi_workflows.runtime.governance import GovernanceServices

    fx = load_fixture("guided_mmm")
    data, _tables = fx.generate(0, "tiny")
    data = data.copy()
    data.attrs["mapi_schema"] = fx.raw_schema(data).model_dump_json(exclude_none=True)
    doc = copy.deepcopy(MMM_GOVERNED.document)
    for n in doc["nodes"]:  # type: ignore[union-attr]
        if n["id"] == "fit":  # type: ignore[index]
            n["with"]["fit"] = {"engine": "pymc", "tune": 60, "draws": 40, "chains": 2}  # type: ignore[index]
    spec = WorkflowSpec.model_validate(doc)
    policy = GovernancePolicy(
        id="pol_01jaf3h1j2k3m4n5p6q7r8s9t0",
        tenant_id="acme",
        project_id=None,
        scope="tenant",
        version=1,
        name="MMM",
        checks=[
            PolicyCheck(check="data.quality", severity="warning"),
            PolicyCheck(check="holdout.accuracy", severity="warning"),
            PolicyCheck(check="plausibility.signs"),
            PolicyCheck(check="residuals.whiteness", severity="info"),
        ],
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    submitted: list[Any] = []
    gov = GovernanceServices(
        resolver=lambda _m, _p: [policy], submit=lambda m, e: submitted.append(e)
    )
    res = LocalRunner(registry=default_registry(), governance=gov).run(
        spec, inputs={"data": data}, params={"drivers": ["spend"], "holdout_weeks": 6}
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    model = res.values["model"]
    assert isinstance(model, ModelValue)
    assert model.bundle.model.manifest.id == "mmm.bayesian"
    b = model.bundle.binding
    assert (b.to_dataset("geo"), b.to_dataset("time"), b.to_dataset("y")) == (
        "region",
        "week",
        "revenue",
    )
    ev = res.values["evidence"]
    assert isinstance(ev, dict)
    statuses = {r["check"]: r["status"] for r in ev["results"]}
    assert set(statuses) == {c.check for c in policy.checks}
    assert "error" not in statuses.values(), ev["results"]
    assert statuses["plausibility.signs"] == "pass"
    assert ev["passed"] is True and submitted
    pack = res.values["pack"]
    assert isinstance(pack, dict) and pack["format"] == "mapi.report/v1"
