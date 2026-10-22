"""Contract kits for workflow extensions: plugin nodes and plugin templates (v0.18).

A plugin node is a :class:`~mapi_workflows.runtime.nodes.NodeExport` (a JSON contract built
with :mod:`mapi_spec.node_authoring` and its implementation) exported from the
``mapi.workflow_nodes`` entry-point group; a plugin template is a
:class:`~mapi_spec.catalog.TemplateCatalogEntry` exported from ``mapi.workflow_templates``.
Run the kits in the plugin's tests (``mapi plugin init --kind node|template`` writes them)::

    from mapi_workflows.kit import check_node, check_template

    def test_zero_share(weekly):
        check_node(ZERO_SHARE, inputs={"data": weekly}, params={"threshold": 0.2})

    def test_weekly_check():
        assert check_template(WEEKLY_CHECK) == []

:func:`check_node` checks what the platform relies on: the contract passes the plugin rules
(:func:`~mapi_spec.workflow_nodes.plugin_contract_issues`), the parameters and inputs fit
it, the node runs, every output has its declared port type (JSON payloads validated by
type), the result is JSON, the run's sidecar fits its size cap and a ``cacheable`` node
gives the same result twice. :func:`check_template` validates the entry, its About card and
its document against the node contracts (built-ins and installed plugins).

Needs the ``runtime`` extra only (``mapi_workflows.testing`` re-exports both kits but also
needs the ``testing`` extra).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import traceback
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import xarray as xr
from jsonschema import Draft202012Validator
from mapi_sdk.registry import Registry
from mapi_sdk.steps import CancelToken
from mapi_sdk.testing.checks import CheckResult, ContractReport, ContractViolation
from mapi_spec.about import about_issues
from mapi_spec.catalog import TemplateCatalogEntry
from mapi_spec.errors import ErrorCode, Issue, Severity, json_pointer
from mapi_spec.namespaces import check_namespace
from mapi_spec.plots import PlotSpec
from mapi_spec.stattests import StatTestResult
from mapi_spec.workflow import NODE_RESULT_MAX_BYTES, WorkflowNodeJob, validate_workflow
from mapi_spec.workflow_nodes import NodeImplementation, PortType, plugin_contract_issues
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES, WorkflowTemplate
from pydantic import BaseModel, JsonValue, ValidationError

from mapi_workflows.payloads import (
    EvidencePayload,
    MetricsPayload,
    ReportPayload,
    SplitPlanPayload,
    TablePayload,
)
from mapi_workflows.runtime.governance import GovernanceServices
from mapi_workflows.runtime.nodes import NodeExport, all_node_contracts
from mapi_workflows.runtime.sandbox import dataset_value, output_files
from mapi_workflows.runtime.values import (
    DatasetValue,
    ModelValue,
    NodeContext,
    NodeOutcome,
    Value,
)

__all__ = [
    "CheckResult",
    "ContractReport",
    "ContractViolation",
    "NodeExport",
    "check_node",
    "check_template",
]

_PAYLOADS: Final[dict[PortType, type[BaseModel]]] = {
    PortType.TEST_RESULT: StatTestResult,
    PortType.SPLIT_PLAN: SplitPlanPayload,
    PortType.METRICS: MetricsPayload,
    PortType.TABLE: TablePayload,
    PortType.REPORT: ReportPayload,
    PortType.PLOT: PlotSpec,
    PortType.EVIDENCE: EvidencePayload,
}
"""The JSON payload model of each inline port type (:mod:`mapi_workflows.payloads`)."""

_RESULT_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "string": (str,),
    "object": (dict,),
    "array": (list,),
}

InputArg = Value | xr.Dataset | Sequence[Value | xr.Dataset]
"""One port's input for :func:`check_node`: a value, an ``xr.Dataset`` or a list of them."""


# ================================================================================== nodes


def _values(raw: InputArg) -> list[Value]:
    items: Sequence[Any] = raw if isinstance(raw, list | tuple) else [raw]
    return [dataset_value(v) if isinstance(v, xr.Dataset) else v for v in items]


def _fresh(values: Mapping[str, list[Value]]) -> dict[str, list[Value]]:
    """Copies of the inputs, so a node that changes its inputs cannot fake determinism."""
    out: dict[str, list[Value]] = {}
    for port, vals in values.items():
        out[port] = [
            dataclasses.replace(v, data=v.data.copy(deep=True))
            if isinstance(v, DatasetValue)
            else v
            if isinstance(v, ModelValue)
            else copy.deepcopy(v)
            for v in vals
        ]
    return out


def _input_problems(c: NodeImplementation, inputs: Mapping[str, list[Value]]) -> list[str]:
    problems: list[str] = []
    for port in inputs:
        if port not in c.inputs:
            problems.append(f"{port!r} is not an input port of the contract")
    for port, spec in c.inputs.items():
        vals = inputs.get(port, [])
        if spec.required and not vals:
            problems.append(f"the required port {port!r} has no input")
        if len(vals) > 1 and not spec.multiple:
            problems.append(f"the port {port!r} takes one value, not {len(vals)}")
        for v in vals:
            got = (
                PortType.DATASET
                if isinstance(v, DatasetValue)
                else PortType.MODEL_VERSION
                if isinstance(v, ModelValue)
                else None
            )
            if spec.type in (PortType.DATASET, PortType.MODEL_VERSION):
                if got is not spec.type:
                    problems.append(f"the port {port!r} takes a {spec.type.value} value")
            elif not isinstance(v, dict):
                problems.append(f"the port {port!r} takes a JSON object ({spec.type.value})")
    return problems


def _output_problems(c: NodeImplementation, outcome: NodeOutcome) -> list[str]:
    problems: list[str] = []
    for port, value in outcome.outputs.items():
        want = c.outputs.get(port)
        if want is None:
            problems.append(f"{port!r} is not an output port of the contract")
            continue
        if want is PortType.MODEL_VERSION or isinstance(value, ModelValue):
            problems.append(
                f"{port!r}: a plugin node cannot output a model version (fits run as fit runs)"
            )
        elif want is PortType.DATASET:
            if not isinstance(value, DatasetValue):
                problems.append(
                    f"{port!r} is a dataset port, but the node returned {type(value).__name__}"
                )
        elif not isinstance(value, dict):
            problems.append(
                f"{port!r} is a {want.value} port (a JSON object), but the node returned "
                f"{type(value).__name__}"
            )
        else:
            try:
                _PAYLOADS[want].model_validate(value)
            except ValidationError as exc:
                problems.append(f"{port!r} is not a valid {want.value} payload: {exc}"[:600])
    for port in c.outputs:
        if port not in outcome.outputs:
            problems.append(f"the node did not return the declared output {port!r}")
    return problems


def _result_problems(c: NodeImplementation, result: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        problems.append(f"the result is not JSON (no NaN or infinity, plain types only): {exc}")
    for name, f in c.result_fields.items():
        v = result.get(name)
        if v is None:
            continue
        ok = isinstance(v, _RESULT_TYPES[f.type]) and not (
            f.type in ("number", "integer") and isinstance(v, bool)
        )
        if not ok:
            problems.append(f"the result field {name!r} must be {f.type}, not {type(v).__name__}")
    return problems


def _fingerprint(outcome: NodeOutcome) -> tuple[str, dict[str, Any]]:
    result = json.dumps(outcome.result, sort_keys=True, default=str)
    outputs: dict[str, Any] = {}
    for port, v in outcome.outputs.items():
        if isinstance(v, DatasetValue):
            outputs[port] = v.data
        elif isinstance(v, dict):
            outputs[port] = json.dumps(v, sort_keys=True, default=str)
    return result, outputs


def _same(a: NodeOutcome, b: NodeOutcome) -> str | None:
    """Where two outcomes differ (``None``: they are the same)."""
    ra, oa = _fingerprint(a)
    rb, ob = _fingerprint(b)
    if ra != rb:
        return "the result"
    if set(oa) != set(ob):
        return "the output ports"
    for port, va in oa.items():
        vb = ob[port]
        same = va.identical(vb) if isinstance(va, xr.Dataset) else va == vb
        if not same:
            return f"the output {port!r}"
    return None


def check_node(
    export: NodeExport,
    *,
    inputs: Mapping[str, InputArg],
    params: Mapping[str, JsonValue] | None = None,
    seed: int = 0,
    registry: Registry | None = None,
    distribution: str | None = None,
    raise_on_failure: bool = True,
) -> ContractReport:
    """Run the node contract checks on ``inputs`` (by port) with ``params``.

    ``distribution`` is the distribution that will export the node (the namespace check
    uses it; ``None`` refuses the built-in namespaces, as for any third-party plugin).
    Checks: ``contract``, ``params``, ``inputs``, ``runs``, ``outputs``, ``result``,
    ``sidecar``, ``deterministic`` (skipped for a node that is not ``cacheable``).
    """
    c = export.contract
    report = ContractReport(f"workflow node {c.ref}")
    add = report.results.append

    issues = plugin_contract_issues(c, distribution=distribution)
    add(CheckResult("contract", not issues, "; ".join(i.message for i in issues)))

    p = dict(params or {})
    errors = sorted(Draft202012Validator(c.params_schema).iter_errors(p), key=str)
    add(
        CheckResult(
            "params",
            not errors,
            "; ".join(f"{'/'.join(map(str, e.absolute_path)) or '/'}: {e.message}" for e in errors),
        )
    )

    values = {port: _values(raw) for port, raw in inputs.items()}
    problems = _input_problems(c, values)
    add(CheckResult("inputs", not problems, "; ".join(problems)))

    def run() -> NodeOutcome:
        ctx = NodeContext(
            node_id="check",
            use=c.ref,
            params=dict(p),
            inputs=_fresh(values),
            registry=registry,
            seed=seed,
            cancel=CancelToken(),
            log=None,
            progress=None,
            input_refs={},
            services={"governance": GovernanceServices(), "sandbox": True},
            tracker=None,
        )
        out = export.fn(ctx)
        if not isinstance(out, NodeOutcome):
            raise TypeError(f"the node returned {type(out).__name__}, not a NodeOutcome")
        return out

    try:
        outcome = run()
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        add(CheckResult("runs", False, detail[:2000]))
        for name in ("outputs", "result", "sidecar", "deterministic"):
            add(CheckResult(name, False, "the node did not run", skipped=True))
        return _finish(report, raise_on_failure)
    add(CheckResult("runs", True))

    problems = _output_problems(c, outcome)
    add(CheckResult("outputs", not problems, "; ".join(problems)))
    problems = _result_problems(c, outcome.result)
    add(CheckResult("result", not problems, "; ".join(problems)))
    add(_sidecar(c, outcome))
    add(_deterministic(c, outcome, run))
    return _finish(report, raise_on_failure)


def _sidecar(c: NodeImplementation, outcome: NodeOutcome) -> CheckResult:
    job = WorkflowNodeJob(
        workflow_run_id="wfr_check", node_id="check", use=c.ref, params={}, seed=0
    )
    try:
        sidecar, _ = output_files(job, outcome, contract=c)
        size = len(sidecar.model_dump_json().encode())
    except Exception as exc:
        return CheckResult("sidecar", False, f"{type(exc).__name__}: {exc}"[:2000])
    if size > NODE_RESULT_MAX_BYTES:
        return CheckResult(
            "sidecar", False, f"node_result.json is {size} bytes (cap {NODE_RESULT_MAX_BYTES})"
        )
    return CheckResult("sidecar", True)


def _deterministic(
    c: NodeImplementation, first: NodeOutcome, run: Callable[[], NodeOutcome]
) -> CheckResult:
    if not c.cacheable:
        return CheckResult("deterministic", True, "the node is not cacheable", skipped=True)
    try:
        second = run()
    except Exception as exc:
        return CheckResult("deterministic", False, f"the second run failed: {exc}"[:2000])
    where = _same(first, second)
    if where is None:
        return CheckResult("deterministic", True)
    return CheckResult(
        "deterministic",
        False,
        f"two runs with the same inputs, parameters and seed gave different {where}. Make "
        "the node deterministic (use ctx.seed), or set cacheable=False in its contract.",
    )


def _finish(report: ContractReport, raise_on_failure: bool) -> ContractReport:
    if raise_on_failure:
        report.raise_for_failures()
    return report


# ============================================================================== templates


def _entry(
    t: TemplateCatalogEntry | WorkflowTemplate | Mapping[str, Any],
) -> tuple[TemplateCatalogEntry | None, list[Issue]]:
    if isinstance(t, TemplateCatalogEntry):
        return t, []
    raw = t.model_dump(mode="json", exclude={"source"}) if isinstance(t, WorkflowTemplate) else t
    try:
        return TemplateCatalogEntry.model_validate(dict(raw)), []
    except ValidationError as exc:
        return None, [
            Issue(
                code=ErrorCode.WORKFLOW_INVALID,
                pointer=json_pointer(*e["loc"]),
                message="The template entry is not valid.",
                untrusted_text=str(e["msg"])[:2000],
            )
            for e in exc.errors()
        ]


def check_template(
    t: TemplateCatalogEntry | WorkflowTemplate | Mapping[str, Any],
    *,
    nodes: Mapping[str, NodeImplementation] | None = None,
    distribution: str | None = None,
    warnings: bool = False,
) -> list[Issue]:
    """Why the platform would refuse the plugin template ``t`` (empty: it would list it).

    Checks the entry (an id ``<namespace>_<name>`` in a namespace ``distribution`` may use,
    not a built-in template id), its About card and its document, validated against
    ``nodes`` (default: the built-in contracts and the installed plugin nodes). Issue
    pointers are into the entry (``/document/nodes/0/use``). With ``warnings`` the
    document's warnings are returned too.
    """
    entry, issues = _entry(t)
    if entry is None:
        return issues
    ns = entry.id.partition("_")[0]
    reason = check_namespace(entry.id, distribution, plugin=ns)
    if reason is not None:
        issues.append(Issue(code=ErrorCode.WORKFLOW_INVALID, pointer="/id", message=reason))
    if entry.id in WORKFLOW_TEMPLATES:
        issues.append(
            Issue(
                code=ErrorCode.WORKFLOW_INVALID,
                pointer="/id",
                message=f"{entry.id!r} is the id of a built-in template. Use a different id.",
            )
        )
    issues.extend(
        Issue(code=ErrorCode.ABOUT_INCOMPLETE, pointer="/about", message=problem)
        for problem in about_issues(entry.about)
    )
    result = validate_workflow(
        entry.document, nodes=nodes if nodes is not None else all_node_contracts()
    )
    keep = {Severity.ERROR, Severity.WARNING} if warnings else {Severity.ERROR}
    issues.extend(
        i.model_copy(update={"pointer": "/document" + i.pointer})
        for i in result.issues
        if i.severity in keep
    )
    return issues
