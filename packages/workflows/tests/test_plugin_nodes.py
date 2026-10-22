"""Plugin workflow nodes (v0.18): ``NodeExport`` entry points, their checks and diagnostics,
and running them in the sandbox (``execute_job``) and in-process (``LocalRunner``).

A fake distribution is installed by monkeypatching :func:`importlib.metadata.entry_points`;
the acme node below is reused by ``test_plugin_templates`` and ``test_check_node``.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest
import xarray as xr
from mapi_spec.catalog import CatalogDiagnostic
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.node_authoring import about, num, schema, string
from mapi_spec.workflow import WorkflowNodeJob, WorkflowRunStatus
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation, NodeKind
from mapi_workflows.runtime import governance, nodes
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.runtime.nodes import (
    ENTRY_POINT_GROUP,
    MAX_DIAGNOSTICS_PER_DISTRIBUTION,
    NodeExport,
    all_node_contracts,
    builtin_nodes,
    load_node_exports,
    node_registry,
    plugin_node_contracts,
)
from mapi_workflows.runtime.sandbox import execute_job, output_files, resolve_contract
from mapi_workflows.runtime.values import NodeContext, NodeOutcome
from mapi_workflows.testing import weekly_series

pytestmark = pytest.mark.filterwarnings("ignore")

ACME_DIST = "acme-nodes"


# ========================================================================= the acme node

ACME_ABOUT = about(
    "Share of zeros",
    "Counts how many values of the target are zero.",
    use=["Before a count model"],
    avoid=["The data has no zeros by design"],
    needs=["A numeric target"],
    assumes=["Zeros are real values, not missing values"],
    outputs=["The share of zeros and a verdict"],
    difficulty="beginner",
    runtime="instant",
    example="In weekly store sales, 12% of the weeks have zero sales.",
)


def zero_share(ctx: NodeContext) -> NodeOutcome:
    """The acme node: the share of zeros of ``var`` (a ``test_result``)."""
    dv = ctx.dataset("data")
    assert dv is not None
    var = str(ctx.params.get("var") or "sales")
    threshold = float(ctx.params.get("threshold", 0.2))  # type: ignore[arg-type]
    values = dv.data[var].values
    share = float((values == 0).mean())
    reject = share > threshold
    result: dict[str, Any] = {
        "test": "test.acme_zero_share",
        "var": var,
        "statistic": share,
        "alpha": 0.05,
        "reject": reject,
        "decision": "too_many_zeros" if reject else "ok",
        "verdict_plain": "Too many values are zero." if reject else "Few values are zero.",
        "null_plain": "The share of zeros is at most the threshold.",
        "n_obs": int(values.size),
    }
    return NodeOutcome(
        result={"share": share, "reject": reject, "verdict_plain": result["verdict_plain"]},
        outputs={"result": result},
    )


ZERO_SHARE_CONTRACT = NodeImplementation(
    id="test.acme_zero_share",
    version="1.2.0",
    kind=NodeKind.TEST,
    plugin="acme",
    inputs={"data": {"type": "dataset", "description": "The data to test"}},
    outputs={"result": "test_result"},
    params_schema=schema(
        {
            "threshold": num("The largest share of zeros that passes", 0.2, 0, 1),
            "var": string("The variable to test", "sales", max_length=128),
        }
    ),
    result_fields={
        "share": {"type": "number", "description": "The share of zeros"},
        "reject": {"type": "boolean", "description": "True if there are too many zeros"},
        "verdict_plain": {"type": "string", "description": "The verdict in plain words"},
    },
    about=ACME_ABOUT,
)
ZERO_SHARE = NodeExport(contract=ZERO_SHARE_CONTRACT, fn=zero_share)


# ====================================================================== fake entry points


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeEP:
    """An entry point of a fake distribution (``obj`` an exception: loading raises it)."""

    def __init__(
        self, name: str, obj: Any, dist: str | None = ACME_DIST, value: str = "acme:NODES"
    ) -> None:
        self.name, self._obj, self.value = name, obj, value
        self.dist = None if dist is None else _Dist(dist)

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def install(monkeypatch: pytest.MonkeyPatch, eps: dict[str, list[FakeEP]]) -> None:
    """Make ``eps`` the installed entry points (every other group is empty)."""
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps.get(group, []))


def _codes(diags: list[CatalogDiagnostic]) -> list[str]:
    return [d.code for d in diags]


def _variant(**update: Any) -> NodeExport:
    return NodeExport(contract=ZERO_SHARE_CONTRACT.model_copy(update=update), fn=zero_share)


def _job(use: str = "test.acme_zero_share@1.2.0", **params: Any) -> WorkflowNodeJob:
    return WorkflowNodeJob(
        workflow_run_id="run_01jaf3q2x8m9v7k5h4g3f2e1d0",
        node_id="zeros",
        use=use,
        params=params,
        input_types={"in.data.0": "dataset"},
    )


@pytest.fixture
def weekly() -> xr.Dataset:
    ds = weekly_series(60)
    ds["sales"][:9] = 0.0
    return ds


@pytest.fixture
def acme(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("acme", [ZERO_SHARE])]})


# ============================================================================== loading


def test_a_valid_export_is_loaded_with_no_diagnostic(acme: None) -> None:
    exports, diags = load_node_exports()
    assert diags == []
    assert exports == {"test.acme_zero_share": ZERO_SHARE}
    assert plugin_node_contracts() == [ZERO_SHARE_CONTRACT]
    reg = node_registry()
    assert reg["test.acme_zero_share"] is zero_share
    assert set(builtin_nodes()) < set(reg)
    merged = all_node_contracts()
    assert merged["test.acme_zero_share"] is ZERO_SHARE_CONTRACT
    assert set(NODE_IMPLEMENTATIONS) < set(merged)


def test_a_factory_and_a_single_export_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    other = _variant(id="test.acme_other")
    install(
        monkeypatch,
        {ENTRY_POINT_GROUP: [FakeEP("a", lambda: [ZERO_SHARE]), FakeEP("b", other)]},
    )
    exports, diags = load_node_exports()
    assert diags == []
    assert sorted(exports) == ["test.acme_other", "test.acme_zero_share"]


def test_the_builtin_table_entry_point_is_not_a_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [
                FakeEP(
                    "core",
                    builtin_nodes,
                    dist="mapi-workflows",
                    value="mapi_workflows.runtime.nodes:builtin_nodes",
                )
            ]
        },
    )
    assert load_node_exports() == ({}, [])


def test_nothing_installed_keeps_the_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {})
    assert node_registry() == builtin_nodes()
    assert plugin_node_contracts() == []
    assert all_node_contracts() == dict(NODE_IMPLEMENTATIONS)


@pytest.mark.parametrize(
    ("export", "code"),
    [
        pytest.param(_variant(runs_in="orchestrator"), "node_contract_invalid", id="orchestrator"),
        pytest.param(_variant(runs_in="child_run"), "node_contract_invalid", id="child_run"),
        pytest.param(
            _variant(id="branch.acme_if", kind=NodeKind.BRANCH),
            "node_contract_invalid",
            id="branch",
        ),
        pytest.param(
            _variant(id="model.acme_fit", kind=NodeKind.MODEL), "node_contract_invalid", id="model"
        ),
        pytest.param(
            _variant(id="test.sci_zero_share", plugin="sci"), "namespace_reserved", id="builtin-ns"
        ),
        pytest.param(
            _variant(id="test.t_acme_zero", plugin="t_acme"), "namespace_reserved", id="upload-ns"
        ),
        pytest.param(_variant(plugin=None), "node_contract_invalid", id="no-plugin"),
        pytest.param(
            _variant(outputs={"Result Port": "test_result"}), "node_contract_invalid", id="bad-port"
        ),
        pytest.param(_variant(outputs={}), "node_contract_invalid", id="no-output"),
        pytest.param(
            _variant(
                params_schema={
                    "type": "object",
                    "properties": {"code": {"type": "string", "pattern": "(a+)+$"}},
                }
            ),
            "schema_unsafe",
            id="unsafe-schema",
        ),
        pytest.param(
            _variant(about=ACME_ABOUT.model_copy(update={"summary_plain": "zeros"})),
            "about_incomplete",
            id="no-about",
        ),
    ],
)
def test_an_invalid_contract_is_a_diagnostic(
    monkeypatch: pytest.MonkeyPatch, export: NodeExport, code: str
) -> None:
    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("acme", [export, ZERO_SHARE])]})
    exports, diags = load_node_exports()
    assert list(exports) == ["test.acme_zero_share"]  # the valid one still loads
    assert _codes(diags) == [code]
    d = diags[0]
    assert d.group == ENTRY_POINT_GROUP and d.entry_point == "acme"
    assert d.distribution == ACME_DIST and d.id == export.contract.id
    assert d.untrusted_text  # the reasons, as plugin-originated text
    assert d.message and export.contract.id not in d.message  # trusted text only
    assert node_registry()["test.acme_zero_share"] is zero_share


def test_a_builtin_id_is_a_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    shadow = _variant(id="test.adf", plugin=None)
    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("acme", [shadow])]})
    exports, diags = load_node_exports()
    assert exports == {}
    assert diags[0].id == "test.adf"
    assert node_registry()["test.adf"] is builtin_nodes()["test.adf"]


def test_the_first_distribution_wins_an_id(monkeypatch: pytest.MonkeyPatch) -> None:
    twin = NodeExport(contract=ZERO_SHARE_CONTRACT, fn=lambda ctx: zero_share(ctx))
    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [
                FakeEP("acme", [ZERO_SHARE]),
                FakeEP("copy", [twin], dist="acme-copy"),
            ]
        },
    )
    exports, diags = load_node_exports()
    assert exports["test.acme_zero_share"] is ZERO_SHARE
    assert _codes(diags) == ["id_collision"]
    assert diags[0].distribution == "acme-copy"


def test_a_broken_entry_point_is_a_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [
                FakeEP("broken", ImportError("no module named acme_lab")),
                FakeEP("acme", [ZERO_SHARE]),
            ]
        },
    )
    exports, diags = load_node_exports()
    assert list(exports) == ["test.acme_zero_share"]
    assert _codes(diags) == ["load_error"]
    assert diags[0].untrusted_text == "ImportError: no module named acme_lab"
    assert "acme_lab" not in diags[0].message  # trusted text never carries plugin text


def test_exports_that_are_not_node_exports_are_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [
                FakeEP("number", 42),
                FakeEP("list", [ZERO_SHARE, "test.acme_x"]),
                FakeEP("fn", [NodeExport(contract=ZERO_SHARE_CONTRACT, fn=None)]),  # type: ignore[arg-type]
            ]
        },
    )
    exports, diags = load_node_exports()
    assert list(exports) == ["test.acme_zero_share"]
    assert _codes(diags) == ["node_contract_invalid"] * 3


def test_a_legacy_mapping_export_is_deprecated_and_never_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_adf(ctx: NodeContext) -> NodeOutcome:  # pragma: no cover - never chosen
        raise AssertionError

    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [
                FakeEP("legacy", lambda: {"test.adf": fake_adf, "test.acme_nope": fake_adf})
            ]
        },
    )
    exports, diags = load_node_exports()
    assert exports == {}
    assert _codes(diags) == ["deprecated", "id_collision", "node_contract_invalid"]
    assert [d.id for d in diags] == [None, "test.adf", "test.acme_nope"]
    assert node_registry()["test.adf"] is builtin_nodes()["test.adf"]


def test_a_legacy_mapping_still_fills_a_contract_id_with_no_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free = next(
        (i for i in NODE_IMPLEMENTATIONS if i not in builtin_nodes() and i != "subworkflow.run"),
        None,
    )
    if free is None:  # pragma: no cover - every contract id has a built-in implementation
        pytest.skip("every node contract has a built-in implementation")

    def impl(ctx: NodeContext) -> NodeOutcome:
        return NodeOutcome(result={})

    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("legacy", {free: impl})]})
    _, diags = load_node_exports()
    assert _codes(diags) == ["deprecated"]
    assert node_registry()[free] is impl


def test_diagnostics_are_capped_per_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = [_variant(id=f"test.acme_n{i}", runs_in="orchestrator") for i in range(30)]
    install(
        monkeypatch,
        {
            ENTRY_POINT_GROUP: [
                FakeEP("acme", bad),
                FakeEP("other", ImportError("x"), dist="other-dist"),
            ]
        },
    )
    _, diags = load_node_exports()
    acme_rows = [d for d in diags if d.distribution == ACME_DIST]
    assert len(acme_rows) == MAX_DIAGNOSTICS_PER_DISTRIBUTION
    assert [d.distribution for d in diags if d.distribution != ACME_DIST] == ["other-dist"]


def test_extra_implementations_still_win(acme: None) -> None:
    def mine(ctx: NodeContext) -> NodeOutcome:
        return NodeOutcome(result={})

    assert node_registry({"test.acme_zero_share": mine})["test.acme_zero_share"] is mine


# =============================================================================== sandbox


def test_a_plugin_node_runs_in_the_sandbox(acme: None, weekly: xr.Dataset) -> None:
    from mapi_workflows.runtime.sandbox import dataset_value

    job = _job(threshold=0.1)
    outcome = execute_job(job, {"data": [dataset_value(weekly)]})
    assert outcome.result["reject"] is True
    assert outcome.result["share"] == pytest.approx(9 / 60)
    sidecar, files = output_files(job, outcome)
    assert files == []
    assert sidecar.use == "test.acme_zero_share@1.2.0"
    out = sidecar.outputs["result"]
    assert out.type == "test_result" and out.inline is not None
    assert out.inline["decision"] == "too_many_zeros"  # type: ignore[index,call-overload]


def test_the_sandbox_refuses_another_contract_version(acme: None, weekly: xr.Dataset) -> None:
    from mapi_workflows.runtime.sandbox import dataset_value

    with pytest.raises(MapiError) as err:
        execute_job(_job("test.acme_zero_share@1.1.0"), {"data": [dataset_value(weekly)]})
    assert err.value.code is ErrorCode.NODE_UNKNOWN
    assert "version 1.1.0" in err.value.detail and "version 1.2.0" in err.value.detail
    assert resolve_contract("test.acme_zero_share") is ZERO_SHARE_CONTRACT
    assert resolve_contract("test.acme_zero_share@1.2.0") is ZERO_SHARE_CONTRACT


def test_the_sandbox_refuses_an_uninstalled_node(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {})
    with pytest.raises(MapiError) as err:
        execute_job(_job(), {})
    assert err.value.code is ErrorCode.NODE_UNKNOWN


def test_the_sandbox_refuses_an_invalid_export(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _variant(id="test.sci_zero_share", plugin="sci")
    install(monkeypatch, {ENTRY_POINT_GROUP: [FakeEP("acme", [bad])]})
    with pytest.raises(MapiError) as err:
        execute_job(_job("test.sci_zero_share@1.2.0"), {})
    assert err.value.code is ErrorCode.NODE_UNKNOWN


# ============================================================================ local runner


_WORKFLOW: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "zeros", "project": "demo"},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        {
            "id": "zeros",
            "use": "test.acme_zero_share",
            "with": {"threshold": 0.1},
            "inputs": {"data": "inputs.data"},
        },
        {
            "id": "adf",
            "use": "test.adf",
            "with": {"var": "sales"},
            "inputs": {"data": "inputs.data"},
        },
    ],
    "outputs": {"zeros": "zeros.result", "adf": "adf.result"},
}


def test_a_plugin_node_runs_in_the_local_runner(acme: None, weekly: xr.Dataset) -> None:
    res = LocalRunner().run(_WORKFLOW, inputs={"data": weekly})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    zeros = res.values["zeros"]
    assert isinstance(zeros, dict) and zeros["decision"] == "too_many_zeros"


def test_the_workflow_validates_against_the_merged_contracts(acme: None) -> None:
    from mapi_spec.workflow import validate_workflow

    assert not validate_workflow(_WORKFLOW).valid  # the static built-ins do not know it
    result = validate_workflow(_WORKFLOW, nodes=all_node_contracts())
    assert result.valid, result.issues
    assert result.resolved["zeros"] == "test.acme_zero_share@1.2.0"


# ============================================================================ governance


def test_governance_check_loading_records_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    def runner(ctx: Any) -> Any:  # pragma: no cover - never run
        raise AssertionError

    eps = [
        FakeEP("good", {"recovery": runner}, value="acme:CHECKS"),
        FakeEP("broken", RuntimeError("boom"), value="acme:BROKEN"),
        FakeEP("odd", 3, value="acme:ODD"),
        FakeEP("twin", {"recovery": runner, 7: runner}, dist="acme-twin", value="acme:TWIN"),
    ]
    install(monkeypatch, {governance.CHECK_ENTRY_POINT_GROUP: eps})
    diags: list[CatalogDiagnostic] = []
    runners = governance.load_check_runners(diagnostics=diags)
    assert runners == {"recovery": runner}
    assert _codes(diags) == ["load_error", "load_error", "id_collision", "load_error"]
    assert all(d.group == governance.CHECK_ENTRY_POINT_GROUP for d in diags)
    assert diags[0].untrusted_text == "RuntimeError: boom"
    # without a list the behaviour is the v0.17 one
    assert governance.load_check_runners() == {"recovery": runner}


def test_the_module_exports_the_fixed_names() -> None:
    for name in (
        "NodeExport",
        "TEMPLATE_ENTRY_POINT_GROUP",
        "load_node_exports",
        "node_registry",
        "plugin_node_contracts",
        "load_plugin_templates",
    ):
        assert name in nodes.__all__
    assert nodes.TEMPLATE_ENTRY_POINT_GROUP == "mapi.workflow_templates"
