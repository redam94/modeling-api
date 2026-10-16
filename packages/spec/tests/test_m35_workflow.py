"""M3.5 workflows: the document, static validation with JSON pointers (types, references,
parameters, cycles, unreachable nodes), the node catalog, run parameters, cache keys,
schedules and triggers, and the template gallery."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.refs import WorkflowRef
from mapi_spec.workflow import (
    NodeRunStatus,
    ScheduleCreate,
    TriggerCreate,
    WorkflowCreate,
    WorkflowNodeJob,
    WorkflowRunStatus,
    WorkflowSpec,
    check_run_params,
    load_workflow,
    node_cache_key,
    node_dependencies,
    topological_order,
    validate_workflow,
    workflow_hash,
)
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    NODE_KIND_ABOUTS,
    NodeKind,
    PortType,
    get_node,
)
from mapi_spec.workflow_templates import (
    WORKFLOW_TEMPLATES,
    instantiate_template,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _doc(nodes: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "wf", "project": "growth"},
        "params": {"alpha": {"type": "number", "default": 0.05}},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": nodes,
        **extra,
    }


ADF = {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}
KPSS = {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}}
BRANCH = {"id": "wanders", "use": "branch.if",
          "with": {"condition": "not nodes.adf.reject or nodes.kpss.reject"}}  # fmt: skip
DIFF = {"id": "diff", "use": "transform.difference", "inputs": {"data": "inputs.data"},
        "when": "wanders.then"}  # fmt: skip
ORDER = {"id": "order", "use": "search.arma_order",
         "inputs": {"data": ["diff.data", "inputs.data"]}}  # fmt: skip
BASE = [ADF, KPSS, BRANCH, DIFF, ORDER]


def _issues(doc: dict[str, Any]) -> list[tuple[str, str, str]]:
    v = validate_workflow(doc)
    return [(i.severity.value, i.pointer, i.message) for i in v.issues]


def _errors(doc: dict[str, Any]) -> dict[str, str]:
    return {p: m for s, p, m in _issues(doc) if s == "error"}


def test_a_valid_workflow_has_an_order_and_resolved_nodes() -> None:
    v = validate_workflow(_doc(copy.deepcopy(BASE), outputs={"orders": "order.table"}))
    assert v.valid, v.issues
    assert v.order == ["adf", "kpss", "wanders", "diff", "order"]
    assert v.resolved["adf"] == "test.adf@1.0.0"
    spec = WorkflowSpec.model_validate(_doc(copy.deepcopy(BASE)))
    assert node_dependencies(spec)["wanders"] == {"adf", "kpss"}  # read by the condition
    assert node_dependencies(spec)["order"] == {"diff"}
    assert topological_order(spec) == v.order


@pytest.mark.parametrize(
    ("change", "pointer", "needle"),
    [
        (lambda d: d["nodes"][0].update(use="test.nope"), "/nodes/0/use", "not in the node"),
        (lambda d: d["nodes"][0].update(kind="model"), "/nodes/0/kind", "is a test node"),
        (lambda d: d["nodes"][0]["inputs"].pop("data"), "/nodes/0/inputs", "needs input 'data'"),
        (
            lambda d: d["nodes"][0]["inputs"].update(series="inputs.data"),
            "/nodes/0/inputs/series",
            "has no input 'series'",
        ),
        (
            lambda d: d["nodes"][0]["inputs"].update(data="inputs.other"),
            "/nodes/0/inputs/data",
            "unknown workflow input 'other'",
        ),
        (
            lambda d: d["nodes"][4]["inputs"].update(data=["diff.series", "inputs.data"]),
            "/nodes/4/inputs/data/0",
            "has no output 'series'",
        ),
        (
            lambda d: d["nodes"][4]["inputs"].update(data="adf.result"),
            "/nodes/4/inputs/data",
            "expects dataset but adf.result is test_result",
        ),
        (
            lambda d: d["nodes"][4]["inputs"].update(data="ghost.data"),
            "/nodes/4/inputs/data",
            "unknown node 'ghost'",
        ),
        (
            lambda d: d["nodes"][0].update(**{"with": {"alpha": 2}}),
            "/nodes/0/with/alpha",
            "maximum",
        ),
        (
            lambda d: d["nodes"][0].update(**{"with": {"regression": "zz"}}),
            "/nodes/0/with/regression",
            "is not one of",
        ),
        (
            lambda d: d["nodes"][0].update(**{"with": {"bogus": 1}}),
            "/nodes/0/with/bogus",
            "unknown parameter 'bogus'",
        ),
        (
            lambda d: d["nodes"][0].update(**{"with": {"alpha": {"$param": "nope"}}}),
            "/nodes/0/with/alpha",
            "unknown workflow parameter 'nope'",
        ),
        (
            lambda d: d["nodes"][0].update(**{"with": {"bogus": {"$param": "alpha"}}}),
            "/nodes/0/with/bogus",
            "unknown parameter 'bogus'",
        ),
        (
            lambda d: d["nodes"][2]["with"].update(condition="nodes.adf.p_valu < 1"),
            "/nodes/2/with/condition",
            "no field 'p_valu'",
        ),
        (
            lambda d: d["nodes"][2]["with"].update(condition="nodes.wanders.value"),
            "/nodes/2/with/condition",
            "does not run before",
        ),
        (
            lambda d: d["nodes"][2]["with"].update(condition="nodes.diff.vars == []"),
            "/nodes",
            "cycle",
        ),
        (
            lambda d: d["nodes"][2]["with"].update(condition="nodes.adf.reject <"),
            "/nodes/2/with/condition",
            "expected",
        ),
        (
            lambda d: d["nodes"][2]["with"].update(condition="params.beta > 1"),
            "/nodes/2/with/condition",
            "unknown workflow parameter",
        ),
        (
            lambda d: d["nodes"][2]["with"].update(condition="nodes.ghost.x"),
            "/nodes/2/with/condition",
            "unknown node 'ghost'",
        ),
        (lambda d: d["nodes"][3].update(when="adf.then"), "/nodes/3/when", "not a branch"),
        (lambda d: d["nodes"][2].pop("with"), "/nodes/2/with", "condition"),
    ],
)
def test_static_errors_carry_json_pointers(change: Any, pointer: str, needle: str) -> None:
    doc = _doc(copy.deepcopy(BASE))
    change(doc)
    errors = _errors(doc)
    assert pointer in errors, errors
    assert needle in errors[pointer], errors[pointer]


def test_cycles_are_reported_with_the_loop() -> None:
    nodes = [
        {"id": "a", "use": "transform.log", "inputs": {"data": "b.data"}},
        {"id": "b", "use": "transform.log", "inputs": {"data": "a.data"}},
    ]
    errors = _errors(_doc(nodes, outputs={"x": "b.data"}))
    assert "a -> b -> a" in errors["/nodes"] or "b -> a -> b" in errors["/nodes"]
    with pytest.raises(MapiError) as exc:
        topological_order(WorkflowSpec.model_validate(_doc(nodes)))
    assert exc.value.code is ErrorCode.WORKFLOW_INVALID
    loop = [{"id": "c", "use": "branch.if", "with": {"condition": "nodes.c.value"}},
            {"id": "d", "use": "transform.log", "inputs": {"data": "inputs.data"},
             "when": "c.then"}]  # fmt: skip
    assert "/nodes/0/with/condition" in _errors(_doc(loop))


def test_nodes_needing_both_arms_of_a_branch_can_never_run() -> None:
    other = {"id": "other", "use": "transform.log", "inputs": {"data": "inputs.data"},
             "when": "wanders.else"}  # fmt: skip
    both = {"id": "both", "use": "compare.models",
            "inputs": {"candidates": ["x.metrics"]}}  # fmt: skip
    del both
    joined = {"id": "joined", "use": "transform.select", "inputs": {"data": "diff.data"},
              "when": "wanders.else"}  # fmt: skip
    errors = _errors(_doc([*copy.deepcopy(BASE), other, joined]))
    assert "/nodes/6" in errors and "can never run" in errors["/nodes/6"]
    # an alternative input makes it runnable again
    ok = dict(joined, inputs={"data": ["diff.data", "other.data"]})
    ok.pop("when")
    assert "/nodes/6" not in _errors(_doc([*copy.deepcopy(BASE), other, ok]))


def test_unused_results_and_idle_branches_are_warnings() -> None:
    lone = {"id": "lone", "use": "test.jarque_bera", "inputs": {"data": "inputs.data"}}
    idle = {"id": "idle", "use": "branch.if", "with": {"condition": "nodes.adf.reject"}}
    issues = _issues(_doc([*copy.deepcopy(BASE), lone, idle]))
    warnings = {p: m for s, p, m in issues if s == "warning"}
    assert "not used" in warnings["/nodes/5"] and "gates no node" in warnings["/nodes/6"]
    assert validate_workflow(_doc([*copy.deepcopy(BASE), lone])).valid  # warnings only


def test_document_level_rules() -> None:
    with pytest.raises(ValidationError, match="unique"):
        WorkflowSpec.model_validate(_doc([ADF, ADF]))
    with pytest.raises(ValidationError, match="reserved"):
        WorkflowSpec.model_validate(_doc([dict(ADF, id="params")]))
    with pytest.raises(ValidationError, match="when"):
        WorkflowSpec.model_validate(_doc([dict(ADF, when="x.maybe")]))
    with pytest.raises(ValidationError, match="inputs"):
        WorkflowSpec.model_validate(_doc([dict(ADF, inputs={"data": "data"})]))
    with pytest.raises(ValidationError, match="default"):
        WorkflowSpec.model_validate(_doc([ADF], params={"a": {"type": "integer", "default": "x"}}))
    bad = _issues({"kind": "Workflow"})
    assert bad and all(s == "error" for s, _, _ in bad)
    only_branch = _errors(_doc([BRANCH]))
    assert "/nodes" in only_branch


def test_outputs_and_expression_parameters_are_checked() -> None:
    errors = _errors(_doc(copy.deepcopy(BASE), outputs={"a": "inputs.data", "b": "order.nope"}))
    assert "/outputs/a" in errors and "no output 'nope'" in errors["/outputs/b"]
    fit = {
        "id": "fit",
        "use": "model.fit",
        "inputs": {"data": "inputs.data"},
        "with": {
            "model": "ts.sarimax@^1",
            "settings": {"order": {"$expr": "[nodes.order.best_p, 0, nodes.order.q]"}},
        },
    }
    errors = _errors(_doc([*copy.deepcopy(BASE), fit]))
    assert "no field 'q'" in errors["/nodes/5/with/settings/order"]
    ok = copy.deepcopy(fit)
    ok["with"]["settings"]["order"] = {"$expr": "[nodes.order.best_p, 0, nodes.order.best_q]"}
    v = validate_workflow(_doc([*copy.deepcopy(BASE), ok]))
    assert v.valid and v.order.index("order") < v.order.index("fit")


def test_model_refs_resolve_against_a_catalog(catalog: Any) -> None:
    fit = {"id": "fit", "use": "model.fit", "inputs": {"data": "inputs.data"},
           "with": {"model": "ts.sarimax@^1"}}  # fmt: skip
    v = validate_workflow(_doc([fit]), catalog=catalog)
    assert [i.code for i in v.issues] == [ErrorCode.REF_UNRESOLVED]
    fit["with"] = {"model": "mmm.bayesian@^1"}
    assert validate_workflow(_doc([fit]), catalog=catalog).valid


def test_subworkflows_are_checked_against_the_resolved_revision() -> None:
    child = WorkflowSpec.model_validate(_doc([ADF], outputs={"adf": "adf.result"}))
    sub = {"id": "ready", "use": "subworkflow.run", "inputs": {"data": "inputs.data"},
           "with": {"workflow": "workflow://growth/readiness@r2"}}  # fmt: skip
    rep = {"id": "rep", "use": "report.summary", "inputs": {"tests": ["ready.adf"]}}
    seen: list[WorkflowRef] = []

    def resolve(ref: WorkflowRef) -> WorkflowSpec | None:
        seen.append(ref)
        return child if ref.revision == "r2" else None

    v = validate_workflow(_doc([sub, rep]), resolve_subworkflow=resolve)
    assert v.valid, v.issues
    assert seen[0].revision_number == 2
    bad = copy.deepcopy(rep)
    bad["inputs"] = {"tests": ["ready.nope"]}
    v = validate_workflow(_doc([sub, bad]), resolve_subworkflow=resolve)
    assert "no output 'nope'" in {i.pointer: i.message for i in v.issues}["/nodes/1/inputs/tests/0"]
    unresolved = _errors(_doc([sub, rep]))  # without a resolver the ports cannot be checked
    assert "not resolved" in unresolved["/nodes/1/inputs/tests/0"]
    missing = copy.deepcopy(sub)
    missing["with"]["workflow"] = "workflow://growth/readiness@r3"
    v = validate_workflow(_doc([missing, rep]), resolve_subworkflow=resolve)
    assert any(i.code is ErrorCode.REF_UNRESOLVED for i in v.issues)


def test_run_parameters() -> None:
    spec = WorkflowSpec.model_validate(
        _doc(
            [ADF],
            params={
                "alpha": {"type": "number", "default": 0.05, "maximum": 0.5},
                "series": {"type": "array", "required": True},
                "mode": {"type": "string", "default": "a", "enum": ["a", "b"]},
            },
        )
    )
    assert check_run_params(spec, {"series": ["y"]}) == {
        "alpha": 0.05, "series": ["y"], "mode": "a"}  # fmt: skip
    with pytest.raises(MapiError) as exc:
        check_run_params(spec, {"alpha": 0.9, "mode": "c", "extra": 1})
    pointers = {i.pointer for i in exc.value.issues}
    assert pointers == {"/params/alpha", "/params/mode", "/params/extra", "/params/series"}


def test_cache_keys_are_canonical() -> None:
    a = node_cache_key(
        "test.adf@1.0.0", {"alpha": 0.05, "regression": "c"}, {"data": ["sha256:" + "1" * 64]}
    )
    b = node_cache_key(
        "test.adf@1.0.0", {"regression": "c", "alpha": 0.05}, {"data": ["sha256:" + "1" * 64]}
    )
    assert a == b and a.startswith("sha256:")
    assert a != node_cache_key(
        "test.adf@1.0.0", {"alpha": 0.01, "regression": "c"}, {"data": ["sha256:" + "1" * 64]}
    )
    assert a != node_cache_key(
        "test.adf@1.0.0",
        {"alpha": 0.05, "regression": "c"},
        {"data": ["sha256:" + "1" * 64]},
        salt="image-2",
    )
    spec = WorkflowSpec.model_validate(_doc([ADF]))
    assert workflow_hash(spec) == workflow_hash(WorkflowSpec.model_validate(_doc([ADF])))
    assert workflow_hash(spec) != workflow_hash(WorkflowSpec.model_validate(_doc([KPSS])))


def test_yaml_documents_load_strictly() -> None:
    text = """
apiVersion: mapi/v1
kind: Workflow
metadata: {name: wf, project: growth}
inputs: {data: {type: dataset}}
nodes:
  - id: adf
    use: test.adf
    inputs: {data: inputs.data}
    with: {alpha: 0.01}
"""
    spec = load_workflow(text)
    assert spec.nodes[0].with_ == {"alpha": 0.01}
    with pytest.raises(MapiError) as exc:
        load_workflow(text.replace("kind: Workflow", "kind: Workflow\nextra: 1"))
    assert exc.value.code is ErrorCode.WORKFLOW_INVALID and exc.value.issues
    with pytest.raises(MapiError) as exc:
        load_workflow("a: &x 1\nb: *x\n")  # aliases are refused by the strict loader
    assert exc.value.code is not ErrorCode.WORKFLOW_INVALID


def test_the_node_catalog() -> None:
    kinds = {n.kind for n in NODE_IMPLEMENTATIONS.values()}
    assert kinds == set(NodeKind)
    assert set(NODE_KIND_ABOUTS) == set(NodeKind)
    expected = {
        "test.adf", "test.kpss", "test.pp", "test.zivot_andrews", "test.johansen",
        "test.engle_granger", "test.ljung_box", "test.jarque_bera", "test.breusch_pagan",
        "test.white", "test.arch_lm", "test.granger", "test.acf_pacf",
        "test.seasonal_strength", "test.vif", "test.cusum", "test.chow",
        "transform.difference", "transform.seasonal_difference", "transform.log",
        "transform.box_cox", "transform.steps", "transform.select", "split.holdout",
        "split.rolling_origin", "split.expanding_window", "split.blocked_kfold",
        "split.purged_kfold", "split.geo_holdout", "split.nested", "search.arma_order",
        "search.sarima_order", "search.var_lag_order", "search.coint_rank", "search.grid",
        "model.fit", "model.backtest", "model.forecast", "evaluate.metrics",
        "evaluate.baseline", "evaluate.residuals", "compare.models",
        "compare.diebold_mariano", "select.best", "select.threshold", "governance.policy",
        "governance.check", "report.summary", "report.evidence_pack", "branch.if",
        "subworkflow.run",
    }  # fmt: skip
    assert set(NODE_IMPLEMENTATIONS) == expected
    for impl in NODE_IMPLEMENTATIONS.values():
        assert impl.about.title and impl.params_schema.get("type") == "object", impl.id
        if impl.kind is NodeKind.TEST:
            assert impl.outputs == {"result": PortType.TEST_RESULT}
            assert {"p_value", "reject", "decision"} <= set(impl.result_fields)
        if impl.kind in (NodeKind.BRANCH, NodeKind.SELECT, NodeKind.COMPARE):
            assert impl.runs_in == "orchestrator" or impl.kind is NodeKind.COMPARE
    assert get_node("test.adf@1.0.0") is NODE_IMPLEMENTATIONS["test.adf"]
    assert get_node("nope") is None


@pytest.mark.parametrize("template", WORKFLOW_TEMPLATES.values(), ids=lambda t: t.id)
def test_gallery_templates_validate_and_instantiate(template: Any) -> None:
    v = validate_workflow(template.document)
    assert v.valid, [i.message for i in v.issues]
    assert not [i for i in v.issues if i.severity is Severity.WARNING]
    spec = instantiate_template(template, name="mine", project="growth")
    assert spec.metadata.name == "mine" and spec.metadata.labels["workflow-template"]
    assert template.about.title


def test_the_canonical_readiness_to_arimax_workflow() -> None:
    t = WORKFLOW_TEMPLATES["ts_readiness_arimax"]
    spec = t.spec
    uses = {n.id: n.impl_id for n in spec.nodes}
    for needed in ("test.adf", "test.kpss", "test.pp", "test.zivot_andrews",
                   "transform.difference", "transform.seasonal_difference", "test.acf_pacf",
                   "search.arma_order", "split.expanding_window", "model.backtest",
                   "evaluate.baseline", "compare.models", "select.best", "governance.policy",
                   "report.summary"):  # fmt: skip
        assert needed in uses.values(), needed
    order = validate_workflow(t.document).order
    assert order.index("wanders") < order.index("diff") < order.index("acf")
    assert order.index("order") < order.index("backtest") < order.index("compare")
    assert order.index("select") < order.index("governance")
    folds = next(n for n in spec.nodes if n.id == "folds")
    assert folds.with_["gap"] == {"$param": "gap"}


def test_run_dtos_schedules_and_triggers() -> None:
    assert WorkflowRunStatus.SUCCEEDED.terminal and not WorkflowRunStatus.RUNNING.terminal
    assert NodeRunStatus.CACHED.produced and not NodeRunStatus.SKIPPED.produced
    assert NodeRunStatus.SKIPPED.done and not NodeRunStatus.QUEUED.done
    with pytest.raises(ValidationError, match="every 15 minutes"):
        ScheduleCreate(cron="*/5 * * * *")
    with pytest.raises(ValidationError, match="time zone"):
        ScheduleCreate(cron="0 6 * * 1", timezone="Mars/Olympus")
    assert ScheduleCreate(cron="@daily", timezone="Europe/Berlin").cron == "@daily"
    with pytest.raises(ValidationError, match="not one of its versions"):
        TriggerCreate.model_validate({"dataset": "dataset://growth/sales@v3", "input": "data"})
    with pytest.raises(ValidationError, match="exactly one"):
        WorkflowCreate.model_validate({"template": "x", "text": "y"})
    job = WorkflowNodeJob.model_validate(WorkflowNodeJob.model_json_schema()["examples"][0])
    assert job.input_types["in.data.0"] is PortType.DATASET
    _ = datetime.now(UTC)
