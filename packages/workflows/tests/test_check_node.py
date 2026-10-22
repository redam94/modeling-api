"""The workflow extension contract kits (v0.18): :func:`mapi_workflows.kit.check_node` and
:func:`mapi_workflows.kit.check_template`."""

from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest
import xarray as xr
from mapi_spec.errors import ErrorCode, Severity
from mapi_workflows import testing
from mapi_workflows.kit import ContractViolation, NodeExport, check_node, check_template
from mapi_workflows.runtime.nodes import ENTRY_POINT_GROUP, all_node_contracts
from mapi_workflows.runtime.values import NodeContext, NodeOutcome
from mapi_workflows.testing import weekly_series
from test_plugin_nodes import ZERO_SHARE, ZERO_SHARE_CONTRACT, FakeEP, install, zero_share
from test_plugin_templates import WEEKLY_CHECK

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def weekly() -> xr.Dataset:
    ds = weekly_series(60)
    ds["sales"][:9] = 0.0
    return ds


def _export(fn: Any, **update: Any) -> NodeExport:
    return NodeExport(contract=ZERO_SHARE_CONTRACT.model_copy(update=update), fn=fn)


def _failed(report: Any) -> list[str]:
    return [r.name for r in report.failures]


# ================================================================================== nodes


def test_a_good_node_passes_every_check(weekly: xr.Dataset) -> None:
    report = check_node(ZERO_SHARE, inputs={"data": weekly}, params={"threshold": 0.1})
    assert report.ok, report.summary()
    assert [r.name for r in report.results] == [
        "contract",
        "params",
        "inputs",
        "runs",
        "outputs",
        "result",
        "sidecar",
        "deterministic",
    ]


def test_the_kits_are_reexported_by_testing() -> None:
    assert testing.check_node is check_node
    assert testing.check_template is check_template


def test_a_wrong_output_port_type_fails(weekly: xr.Dataset) -> None:
    def returns_data(ctx: NodeContext) -> NodeOutcome:
        dv = ctx.dataset("data")
        return NodeOutcome(result={"share": 0.0}, outputs={"result": dv})  # type: ignore[dict-item]

    report = check_node(_export(returns_data), inputs={"data": weekly}, raise_on_failure=False)
    assert _failed(report) == ["outputs"]
    assert "'result' is a test_result port" in report.get("outputs").detail


def test_an_invalid_json_payload_fails(weekly: xr.Dataset) -> None:
    def bad_payload(ctx: NodeContext) -> NodeOutcome:
        return NodeOutcome(result={}, outputs={"result": {"test": "test.acme_zero_share"}})

    report = check_node(_export(bad_payload), inputs={"data": weekly}, raise_on_failure=False)
    assert _failed(report) == ["outputs"]
    assert "not a valid test_result payload" in report.get("outputs").detail


def test_undeclared_and_missing_outputs_fail(weekly: xr.Dataset) -> None:
    def extra(ctx: NodeContext) -> NodeOutcome:
        return NodeOutcome(result={}, outputs={"other": {}})

    detail = check_node(_export(extra), inputs={"data": weekly}, raise_on_failure=False).get(
        "outputs"
    )
    assert not detail.passed
    assert "'other' is not an output port" in detail.detail
    assert "did not return the declared output 'result'" in detail.detail


def test_a_non_deterministic_cacheable_node_fails(weekly: xr.Dataset) -> None:
    counter = itertools.count()

    def drifting(ctx: NodeContext) -> NodeOutcome:
        out = zero_share(ctx)
        out.result["share"] = float(next(counter))
        return out

    with pytest.raises(ContractViolation) as err:
        check_node(_export(drifting), inputs={"data": weekly})
    assert _failed(err.value.report) == ["deterministic"]
    assert "cacheable=False" in err.value.report.get("deterministic").detail
    # the same node declared not cacheable is not held to it
    report = check_node(_export(drifting, cacheable=False), inputs={"data": weekly})
    assert report.get("deterministic").skipped


def test_a_node_that_changes_its_input_cannot_fake_determinism(weekly: xr.Dataset) -> None:
    def mutating(ctx: NodeContext) -> NodeOutcome:
        dv = ctx.dataset("data")
        assert dv is not None
        out = zero_share(ctx)
        dv.data["sales"][:] = 0.0  # a second run on the same object would differ
        return out

    report = check_node(_export(mutating), inputs={"data": weekly})
    assert report.ok, report.summary()


def test_params_and_inputs_are_checked_against_the_contract(weekly: xr.Dataset) -> None:
    report = check_node(
        ZERO_SHARE,
        inputs={"data": weekly, "model": weekly},
        params={"threshold": 3, "colour": "red"},
        raise_on_failure=False,
    )
    assert _failed(report)[:2] == ["params", "inputs"]
    assert "'model' is not an input port" in report.get("inputs").detail
    missing = check_node(ZERO_SHARE, inputs={}, raise_on_failure=False)
    assert "the required port 'data' has no input" in missing.get("inputs").detail
    wrong = check_node(ZERO_SHARE, inputs={"data": [{"rows": []}]}, raise_on_failure=False)
    assert "takes a dataset value" in wrong.get("inputs").detail


def test_a_crashing_node_fails_runs_and_skips_the_rest(weekly: xr.Dataset) -> None:
    def crash(ctx: NodeContext) -> NodeOutcome:
        raise ValueError("no sales column")

    report = check_node(_export(crash), inputs={"data": weekly}, raise_on_failure=False)
    assert _failed(report) == ["runs"]
    assert "ValueError: no sales column" in report.get("runs").detail
    assert all(report.get(n).skipped for n in ("outputs", "result", "sidecar", "deterministic"))


def test_result_fields_must_have_their_types(weekly: xr.Dataset) -> None:
    def typed(ctx: NodeContext) -> NodeOutcome:
        out = zero_share(ctx)
        out.result["share"] = "a lot"
        out.result["nan"] = float("nan")
        return out

    report = check_node(_export(typed), inputs={"data": weekly}, raise_on_failure=False)
    assert "result" in _failed(report)
    detail = report.get("result").detail
    assert "'share' must be number" in detail and "not JSON" in detail


def test_the_contract_rules_are_checked(weekly: xr.Dataset) -> None:
    report = check_node(
        _export(zero_share, runs_in="orchestrator"), inputs={"data": weekly}, raise_on_failure=False
    )
    assert _failed(report) == ["contract"]
    assert "must run in the sandbox" in report.get("contract").detail
    # a built-in namespace is refused unless the exporter is a built-in distribution
    sci = _export(zero_share, id="test.sci_zero_share", plugin="sci")
    assert not check_node(sci, inputs={"data": weekly}, raise_on_failure=False).ok
    assert check_node(sci, inputs={"data": weekly}, distribution="mapi-plugin-science").ok


# ============================================================================== templates


def test_a_good_template_has_no_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("acme", [ZERO_SHARE])]})
    assert check_template(WEEKLY_CHECK) == []  # installed plugin nodes are known
    assert check_template(WEEKLY_CHECK.model_dump(mode="json")) == []


def test_a_template_with_an_unknown_node_points_into_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, {})
    issues = check_template(WEEKLY_CHECK)
    assert issues and all(i.severity is Severity.ERROR for i in issues)
    assert issues[0].pointer.startswith("/document/nodes/0")
    # an explicit contract map wins over the installed nodes
    assert check_template(WEEKLY_CHECK, nodes=all_node_contracts([ZERO_SHARE_CONTRACT])) == []


def test_template_entry_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("acme", [ZERO_SHARE])]})
    raw = WEEKLY_CHECK.model_dump(mode="json")
    bad_id = check_template({**raw, "id": "weekly"})
    assert [(i.code, i.pointer) for i in bad_id] == [(ErrorCode.WORKFLOW_INVALID, "/id")]
    builtin_ns = check_template({**raw, "id": "mmm_weekly"})
    assert [i.pointer for i in builtin_ns] == ["/id"]
    about = copy.deepcopy(raw)
    about["about"]["summary_plain"] = "Checks zeros. Checks the level. Checks both at once."
    assert [i.code for i in check_template(about)] == [ErrorCode.ABOUT_INCOMPLETE]
