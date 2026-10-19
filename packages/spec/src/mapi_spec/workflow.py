"""Workflows: versioned DAGs of statistical tests, data processing and models (M3.5).

A :class:`WorkflowSpec` (``kind: Workflow``, YAML or JSON like every spec document) nests
tests, transforms, branches, splits, searches, model fits, evaluations, comparisons,
selections, governance checks, reports and sub-workflows into one automated pipeline::

    apiVersion: mapi/v1
    kind: Workflow
    metadata: {name: ts-readiness, project: growth}
    params:
      alpha: {type: number, default: 0.05}
    inputs:
      data: {type: dataset}
    nodes:
      - {id: adf, use: test.adf, inputs: {data: inputs.data}, with: {alpha: {$param: alpha}}}
      - {id: kpss, use: test.kpss, inputs: {data: inputs.data}}
      - id: wanders
        use: branch.if
        with: {condition: "nodes.adf.p_value >= params.alpha or nodes.kpss.reject"}
      - {id: diff, use: transform.difference, inputs: {data: inputs.data}, when: wanders.then}
      - id: order
        use: search.arma_order
        inputs: {data: [diff.data, inputs.data]}     # first available: diff ran, or not
    outputs: {order: order.table}

**Wiring.** An input port binds ``inputs.<name>`` or ``<node>.<port>``. For a single-value
port a list binds *alternatives*, the first whose node ran (a node gated by a branch arm
that was not taken is *skipped*, and so are its dependants unless they have an
alternative); for a ``multiple`` port (``PortSpec.multiple``) a list binds *every* source
that ran. A node whose required port has no available binding is skipped. ``when:
<branch>.then|else`` gates a node on a branch. Parameters (``with``) may be literals,
``{$param: name}`` (a workflow parameter) or ``{$expr: "..."}`` (a value computed from
upstream results with the safe expression language, :mod:`mapi_spec.expr`).

**Static validation** (:func:`validate_workflow`, synchronous, JSON only) reports every
problem as an :class:`~mapi_spec.errors.Issue` with a JSON pointer: unknown node
implementations, parameter schema violations and missing parameters, unbound required
ports, port-type mismatches, references to unknown nodes, ports or parameters, branch
conditions that do not parse or read fields their node does not produce, cycles, nodes that
can never run (gated by both arms of one branch) and unused results.

**Runs.** ``POST /v1/workflows/{id}/runs`` starts a ``workflow`` run (a parent run). The
orchestrator walks the DAG in topological order, evaluates branches itself, sends
``sandbox`` nodes to ``workflow_node`` runs and ``child_run`` nodes to ordinary runs
(fits), reuses unchanged results by :func:`node_cache_key`, retries per node, and records
a :class:`NodeRun` per node and a :class:`BranchDecision` per branch.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator
from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from mapi_spec._dto import (
    EXAMPLE_TS,
    Id,
    RequestModel,
    ResponseModel,
    Sha256,
    TenantId,
    dto_examples,
)
from mapi_spec._types import Duration, Name, StrictModel
from mapi_spec.catalog import Catalog
from mapi_spec.cost import RunCost
from mapi_spec.cron import check_cron, check_timezone
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, json_pointer
from mapi_spec.expr import Expression, parse_expression
from mapi_spec.loader import DEFAULT_LIMITS, Limits, parse_text, spec_hash, validation_issues
from mapi_spec.refs import DatasetRef, UseRef, WorkflowRef
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import ApiVersion, Metadata
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    SINK_KINDS,
    SUBWORKFLOW_NODE,
    NodeImplementation,
    NodeKind,
    PortType,
)

__all__ = [
    "MAX_NODES",
    "NODE_RESULT_MAX_BYTES",
    "NODE_RESULT_OUTPUT",
    "WORKFLOW_RUN_INPUT_PREFIX",
    "BranchDecision",
    "NodeError",
    "NodeOutputRef",
    "NodeResultSidecar",
    "NodeRun",
    "NodeRunStatus",
    "ScheduleCreate",
    "ScheduleUpdate",
    "TriggerCreate",
    "Workflow",
    "WorkflowCreate",
    "WorkflowEvent",
    "WorkflowEventType",
    "WorkflowExecution",
    "WorkflowInput",
    "WorkflowNode",
    "WorkflowNodeJob",
    "WorkflowParam",
    "WorkflowRevision",
    "WorkflowRevisionCreate",
    "WorkflowRun",
    "WorkflowRunCreate",
    "WorkflowRunStatus",
    "WorkflowSchedule",
    "WorkflowSpec",
    "WorkflowTrigger",
    "WorkflowTriggerEvent",
    "WorkflowUpdate",
    "WorkflowValidateRequest",
    "WorkflowValidation",
    "check_run_params",
    "load_workflow",
    "node_cache_key",
    "node_dependencies",
    "param_value_problem",
    "topological_order",
    "validate_workflow",
    "workflow_hash",
]

MAX_NODES: Final = 200
NODE_RESULT_OUTPUT: Final = "node_result.json"
"""What a ``workflow_node`` run writes: the :class:`NodeResultSidecar`."""
NODE_RESULT_MAX_BYTES: Final = 256 * 1024
"""Cap on a node's JSON result (conditions and the UI read it in the control plane)."""
WORKFLOW_RUN_INPUT_PREFIX: Final = "in."
"""``workflow_node`` run inputs are named ``in.<port>.<k>`` (k-th binding of the port)."""

_ID_RE = r"^[a-z][a-z0-9_]{0,62}$"
_BIND_RE = re.compile(
    r"^(?:inputs\.([A-Za-z_][A-Za-z0-9_.\-]{0,127})|([a-z][a-z0-9_]{0,62})\.([a-z][a-z0-9_]{0,62}))$"
)
_WHEN_RE = re.compile(r"^([a-z][a-z0-9_]{0,62})\.(then|else)$")

# ================================================================================ document

ParamType = Literal["number", "integer", "boolean", "string", "array", "object"]
_PARAM_JSON: Final[dict[str, Callable[[object], bool]]] = {
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


class WorkflowParam(StrictModel):
    """A run-time parameter (``params.<name>`` in conditions, ``{$param: name}`` in nodes)."""

    type: ParamType
    default: JsonValue = None
    required: bool = Field(False, description="Must be given at run time (no default)")
    description: str | None = Field(None, max_length=500)
    minimum: float | None = None
    maximum: float | None = None
    enum: list[JsonValue] | None = Field(None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _default(self) -> WorkflowParam:
        if self.default is not None:
            problem = param_value_problem(self, self.default)
            if problem:
                raise ValueError(f"default: {problem}")
        if self.required and self.default is not None:
            raise ValueError("a required parameter cannot have a default")
        return self


PARAM_MAX_MAGNITUDE: Final = 1e15
"""The largest magnitude a number parameter may have (conditions compute with them)."""


def param_value_problem(p: WorkflowParam, value: object) -> str | None:
    """Why ``value`` does not fit parameter ``p`` (``None`` if it does)."""
    if not _PARAM_JSON[p.type](value):
        return f"must be a {p.type}"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if isinstance(value, float) and value != value:  # NaN
            return "must be a number"
        if not -PARAM_MAX_MAGNITUDE <= value <= PARAM_MAX_MAGNITUDE:
            return f"is too large (the maximum is {PARAM_MAX_MAGNITUDE:.0e}, positive or negative)"
        if p.minimum is not None and value < p.minimum:
            return f"must be at least {p.minimum}"
        if p.maximum is not None and value > p.maximum:
            return f"must be at most {p.maximum}"
    if p.enum is not None and value not in p.enum:
        return "is not one of the permitted values"
    return None


class WorkflowInput(StrictModel):
    """A workflow input (``inputs.<name>``): a dataset, a model version, a split plan..."""

    type: PortType
    required: bool = True
    default: str | None = Field(
        None, max_length=512, description="A reference used when the run gives none"
    )
    description: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _default(self) -> WorkflowInput:
        if self.default is not None and self.type is PortType.DATASET:
            DatasetRef.parse(self.default)
        return self


Binding = str | list[str]
"""``inputs.<name>``, ``<node>.<port>``, or a list of alternatives (first available)."""


class WorkflowNode(StrictModel):
    """One node: an implementation (``use``), its parameters and its wiring."""

    id: str = Field(..., pattern=_ID_RE)
    use: str = Field(..., max_length=128, pattern=r"^[a-z]+\.[a-z0-9_]+(@\S{1,64})?$")
    kind: NodeKind | None = Field(None, description="Optional; must match the implementation")
    with_: dict[str, JsonValue] = Field(default_factory=dict, alias="with")
    inputs: dict[str, Binding] = Field(default_factory=dict, max_length=32)
    when: str | None = Field(None, description="<branch node id>.then or .else")
    label: str | None = Field(None, max_length=120)
    description: str | None = Field(None, max_length=1000)
    retries: int = Field(0, ge=0, le=3)
    timeout: Duration | None = None
    cache: bool = Field(True, description="Reuse an identical earlier result")
    on_failure: Literal["fail", "continue"] = Field(
        "fail", description="continue: the node counts as skipped for its dependants"
    )
    resource_class: ResourceClass | None = None

    @field_validator("when")
    @classmethod
    def _when(cls, v: str | None) -> str | None:
        if v is not None and not _WHEN_RE.match(v):
            raise ValueError("when must be '<branch node id>.then' or '.else'")
        return v

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, v: dict[str, Binding]) -> dict[str, Binding]:
        for port, binding in v.items():
            alts = [binding] if isinstance(binding, str) else binding
            if not alts or len(alts) > 8:
                raise ValueError(f"{port}: give 1 to 8 alternatives")
            for b in alts:
                if not _BIND_RE.match(b):
                    raise ValueError(f"{port}: {b[:60]!r} is not inputs.<name> or <node>.<port>")
        return v

    @property
    def impl_id(self) -> str:
        return self.use.split("@", 1)[0]

    def alternatives(self, port: str) -> list[str]:
        b = self.inputs.get(port)
        if b is None:
            return []
        return [b] if isinstance(b, str) else list(b)


class WorkflowExecution(StrictModel):
    """Run-wide settings."""

    max_parallel: int = Field(4, ge=1, le=32)
    resource_class: ResourceClass = ResourceClass.S
    timeout: Duration = Field(default_factory=lambda: Duration(6 * 3600))

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, v: Duration) -> Duration:
        if not 60 <= v.seconds <= 7 * 24 * 3600:
            raise ValueError("timeout must be between 1m and 7 days")
        return v


class WorkflowSpec(StrictModel):
    """The declarative workflow document (``kind: Workflow``)."""

    api_version: ApiVersion = Field(..., alias="apiVersion")
    kind: Literal["Workflow"]
    metadata: Metadata
    params: dict[Name, WorkflowParam] = Field(default_factory=dict, max_length=64)
    inputs: dict[Name, WorkflowInput] = Field(default_factory=dict, max_length=16)
    nodes: list[WorkflowNode] = Field(..., min_length=1, max_length=MAX_NODES)
    outputs: dict[Name, str] = Field(
        default_factory=dict, max_length=32, description="name -> <node>.<port>"
    )
    execution: WorkflowExecution = Field(default_factory=WorkflowExecution)

    @model_validator(mode="after")
    def _unique(self) -> WorkflowSpec:
        ids = [n.id for n in self.nodes]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        if dups:
            raise ValueError(f"node ids must be unique: {dups}")
        clash = sorted(set(ids) & {"inputs", "params", "nodes", "run"})
        if clash:
            raise ValueError(f"these node ids are reserved: {clash}")
        return self

    def node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)


def load_workflow(text: str | bytes, limits: Limits = DEFAULT_LIMITS) -> WorkflowSpec:
    """Parse YAML/JSON text with the strict spec loader and validate it as a workflow
    (``SPEC_PARSE_ERROR``/``PAYLOAD_TOO_LARGE`` from the loader, ``WORKFLOW_INVALID`` with
    pointers otherwise)."""
    data = parse_text(text, limits)
    try:
        return WorkflowSpec.model_validate(data)
    except ValidationError as exc:
        raise MapiError(
            "the document is not a valid workflow",
            code=ErrorCode.WORKFLOW_INVALID,
            issues=[
                i.model_copy(update={"code": ErrorCode.WORKFLOW_INVALID})
                for i in validation_issues(exc, data)
            ],
        ) from None


def workflow_hash(spec: WorkflowSpec | Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of a workflow document (revision identity)."""
    return spec_hash(spec)


# =============================================================================== the graph


def _placeholders(value: object, path: tuple[str | int, ...] = ()) -> Iterable[
    tuple[tuple[str | int, ...], str, str]
]:  # fmt: skip
    """``(path, "$param"|"$expr", text)`` of every placeholder inside a ``with`` value."""
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in ("$param", "$expr"):
            key = next(iter(value))
            text = value[key]
            yield path, key, text if isinstance(text, str) else ""
            return
        for k, v in value.items():
            yield from _placeholders(v, (*path, str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _placeholders(v, (*path, i))


def _expr_nodes(node: WorkflowNode) -> dict[str, list[tuple[str, Expression | None]]]:
    """Upstream node ids an expression of ``node`` reads, with the expressions (``None``
    when a text does not parse)."""
    out: dict[str, list[tuple[str, Expression | None]]] = {}
    texts: list[str] = [t for _, k, t in _placeholders(node.with_) if k == "$expr"]
    if node.impl_id == "branch.if" and isinstance(node.with_.get("condition"), str):
        texts.append(str(node.with_["condition"]))
    for text in texts:
        try:
            ex: Expression | None = parse_expression(text)
        except MapiError:
            ex = None
        if ex is None:
            out.setdefault("", []).append((text, None))
            continue
        for nid, _ in ex.node_refs():
            out.setdefault(nid, []).append((text, ex))
    return out


def node_dependencies(spec: WorkflowSpec) -> dict[str, set[str]]:
    """``node id -> ids it depends on``: input bindings, ``when`` gates and expression
    references (unknown ids are ignored here; :func:`validate_workflow` reports them)."""
    ids = {n.id for n in spec.nodes}
    deps: dict[str, set[str]] = {}
    for n in spec.nodes:
        d: set[str] = set()
        for port in n.inputs:
            for b in n.alternatives(port):
                m = _BIND_RE.match(b)
                if m and m.group(2) and m.group(2) in ids:
                    d.add(m.group(2))
        if n.when:
            m2 = _WHEN_RE.match(n.when)
            if m2 and m2.group(1) in ids:
                d.add(m2.group(1))
        d |= {nid for nid in _expr_nodes(n) if nid in ids}
        d.discard(n.id)
        deps[n.id] = d
    return deps


def topological_order(spec: WorkflowSpec) -> list[str]:
    """Node ids in an order that respects dependencies (stable: document order breaks
    ties). Raises ``WORKFLOW_INVALID`` on a cycle."""
    deps = node_dependencies(spec)
    order_index = {n.id: i for i, n in enumerate(spec.nodes)}
    indeg = {k: len(v) for k, v in deps.items()}
    users: dict[str, list[str]] = {k: [] for k in deps}
    for k, v in deps.items():
        for d in v:
            users[d].append(k)
    ready = deque(sorted((k for k, n in indeg.items() if n == 0), key=order_index.__getitem__))
    out: list[str] = []
    while ready:
        k = ready.popleft()
        out.append(k)
        for u in sorted(users[k], key=order_index.__getitem__):
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
    if len(out) != len(deps):
        cyc = _find_cycle(deps)
        raise MapiError(
            "the workflow has a cycle: " + " -> ".join(cyc),
            code=ErrorCode.WORKFLOW_INVALID,
            pointer="/nodes",
        )
    return out


def _find_cycle(deps: Mapping[str, set[str]]) -> list[str]:
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = 1
        stack.append(n)
        for d in sorted(deps.get(n, ())):
            if color.get(d) == 1:
                return [*stack[stack.index(d) :], d]
            if color.get(d) is None:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for n in sorted(deps):
        if color.get(n) is None:
            found = visit(n)
            if found:
                return found
    return []  # pragma: no cover - only called when a cycle exists


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def node_cache_key(
    impl_ref: str,
    params: Mapping[str, JsonValue],
    inputs: Mapping[str, Sequence[str]],
    *,
    salt: str = "",
) -> str:
    """``sha256:<hex>`` identifying a node execution: the resolved implementation
    (``test.adf@1.0.0``), its resolved parameters and the content identity of every input
    (dataset sha256, model version, upstream cache keys). Equal keys give equal results for
    ``cacheable`` implementations, so the orchestrator may reuse them. ``salt`` carries
    what else must match (the runner image digest)."""
    payload = {
        "impl": impl_ref,
        "params": params,
        "inputs": {k: list(v) for k, v in sorted(inputs.items())},
        "salt": salt,
    }
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ============================================================================== validation


class WorkflowValidation(ResponseModel):
    """``POST /v1/workflows:validate``: issues located by JSON pointers, the execution order
    and the resolved implementation of every node."""

    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    resolved: dict[str, str] = Field(
        default_factory=dict, description="node id -> implementation ref (id@version)"
    )

    model_config = dto_examples(
        {
            "valid": False,
            "issues": [
                {
                    "code": "WORKFLOW_INVALID",
                    "severity": "error",
                    "pointer": "/nodes/3/inputs/data",
                    "message": "node 'order' binds 'diff.series', but transform.difference "
                    "has no output 'series' (it has: data)",
                }
            ],
            "order": [],
        }
    )


def _issue(code: ErrorCode, pointer: str, message: str, *, severity: Severity = Severity.ERROR,
           action: str | None = None) -> Issue:  # fmt: skip
    return Issue(
        code=code, severity=severity, pointer=pointer, message=message[:2000],
        suggested_action=action,
    )  # fmt: skip


def _without(schema: dict[str, Any], skip: set[str]) -> dict[str, Any]:
    if not skip or "required" not in schema:
        return schema
    out = dict(schema)
    out["required"] = [r for r in schema["required"] if r not in skip]
    return out


SubworkflowResolver = Callable[[WorkflowRef], "WorkflowSpec | None"]


def validate_workflow(
    document: WorkflowSpec | Mapping[str, Any],
    *,
    catalog: Catalog | None = None,
    nodes: Mapping[str, NodeImplementation] | None = None,
    resolve_subworkflow: SubworkflowResolver | None = None,
) -> WorkflowValidation:
    """Statically check a workflow (pure; JSON only). See the module docstring.

    ``catalog`` lets ``model`` nodes' ``model`` refs be resolved against the model catalog;
    ``resolve_subworkflow`` lets ``subworkflow.run`` nodes' ports be checked against the
    referenced revision (unresolved references are an error when a resolver is given).
    """
    registry = NODE_IMPLEMENTATIONS if nodes is None else nodes
    if isinstance(document, WorkflowSpec):
        spec = document
    else:
        try:
            spec = WorkflowSpec.model_validate(dict(document))
        except ValidationError as exc:
            return WorkflowValidation(
                valid=False,
                issues=[
                    _issue(ErrorCode.WORKFLOW_INVALID, json_pointer(*e["loc"]), e["msg"])
                    for e in exc.errors()[:100]
                ],
            )
    issues: list[Issue] = []
    resolved: dict[str, str] = {}
    by_id = {n.id: n for n in spec.nodes}
    impls: dict[str, NodeImplementation] = {}
    sub_specs: dict[str, WorkflowSpec] = {}

    # --- implementations, parameters -----------------------------------------------------
    for i, n in enumerate(spec.nodes):
        base = json_pointer("nodes", i)
        impl = registry.get(n.impl_id)
        if impl is None:
            issues.append(_issue(
                ErrorCode.NODE_UNKNOWN, base + "/use",
                f"node {n.id!r} uses {n.impl_id!r}, which is not in the node catalog",
                action="Select a node from GET /v1/catalog/workflow-nodes.",
            ))  # fmt: skip
            continue
        impls[n.id] = impl
        resolved[n.id] = impl.ref
        if n.kind is not None and n.kind is not impl.kind:
            issues.append(_issue(
                ErrorCode.WORKFLOW_INVALID, base + "/kind",
                f"node {n.id!r} has kind {n.kind.value!r}, but {impl.id} is a "
                f"{impl.kind.value} node",
                action=f"Change the kind to {impl.kind.value!r} or remove it.",
            ))  # fmt: skip
        issues.extend(_check_params(spec, n, impl, base))
        if impl.id == SUBWORKFLOW_NODE:
            sub = _check_subworkflow(n, base, resolve_subworkflow, issues)
            if sub is not None:
                sub_specs[n.id] = sub
        if impl.kind is NodeKind.MODEL or impl.id == "search.grid":
            issues.extend(_check_model_ref(n, base, catalog))
        if impl.id == "plot.render":
            issues.extend(_check_plot_ref(n, base, catalog))

    # --- bindings and port types ---------------------------------------------------------
    for i, n in enumerate(spec.nodes):
        impl = impls.get(n.id)
        if impl is None:
            continue
        base = json_pointer("nodes", i)
        in_ports = _input_ports(n, impl, sub_specs)
        for port, pspec in in_ports.items():
            if pspec.required and port not in n.inputs:
                issues.append(_issue(
                    ErrorCode.WORKFLOW_INVALID, base + "/inputs",
                    f"node {n.id!r} needs input {port!r} ({pspec.type.value})",
                ))  # fmt: skip
        for port in n.inputs:
            ptr = base + json_pointer("inputs", port)
            pspec_ = in_ports.get(port)
            if pspec_ is None:
                issues.append(_issue(
                    ErrorCode.WORKFLOW_INVALID, ptr,
                    f"{impl.id} has no input {port!r} (it has: "
                    f"{', '.join(sorted(in_ports)) or 'none'})",
                ))  # fmt: skip
                continue
            alts = n.alternatives(port)
            for k, b in enumerate(alts):
                bptr = ptr if isinstance(n.inputs[port], str) else ptr + f"/{k}"
                src_type = _binding_type(spec, b, impls, sub_specs, by_id)
                if not isinstance(src_type, PortType):
                    issues.append(_issue(ErrorCode.WORKFLOW_INVALID, bptr,
                                         f"node {n.id!r}: {src_type}"))  # fmt: skip
                    continue
                if src_type is not pspec_.type:
                    issues.append(_issue(
                        ErrorCode.WORKFLOW_INVALID, bptr,
                        f"node {n.id!r}: input {port!r} must be {pspec_.type.value}, but "
                        f"{b} is {src_type.value}",
                    ))  # fmt: skip
        if n.when is not None:
            m = _WHEN_RE.match(n.when)
            assert m is not None
            gate = by_id.get(m.group(1))
            if (
                gate is None
                or impls.get(gate.id) is None
                or impls[gate.id].kind is not NodeKind.BRANCH
            ):
                issues.append(_issue(
                    ErrorCode.WORKFLOW_INVALID, base + "/when",
                    f"node {n.id!r} uses when: {n.when}, but {m.group(1)!r} is not a branch node",
                ))  # fmt: skip

    # --- expressions -----------------------------------------------------------------------
    for i, n in enumerate(spec.nodes):
        if n.id not in impls:
            continue
        base = json_pointer("nodes", i)
        issues.extend(_check_expressions(spec, n, base, impls, by_id))

    # --- outputs -----------------------------------------------------------------------------
    for name, b in spec.outputs.items():
        ptr = json_pointer("outputs", name)
        if b.startswith("inputs.") or not _BIND_RE.match(b):
            issues.append(_issue(ErrorCode.WORKFLOW_INVALID, ptr,
                                 f"output {name!r} must name <node>.<port>"))  # fmt: skip
            continue
        t = _binding_type(spec, b, impls, sub_specs, by_id)
        if not isinstance(t, PortType):
            issues.append(_issue(ErrorCode.WORKFLOW_INVALID, ptr, f"output {name!r}: {t}"))

    # --- graph ---------------------------------------------------------------------------------
    order: list[str] = []
    try:
        order = topological_order(spec)
    except MapiError as exc:
        issues.append(_issue(ErrorCode.WORKFLOW_INVALID, "/nodes", exc.detail,
                             action="Remove one of the links in the loop."))  # fmt: skip
    if order:
        issues.extend(_reachability(spec, impls, order))

    if not any(n.kind is not NodeKind.BRANCH for n in impls.values()):
        issues.append(
            _issue(
                ErrorCode.WORKFLOW_INVALID,
                "/nodes",
                "a workflow needs at least one node that is not a branch",
            )
        )
    ok = not any(i.severity is Severity.ERROR for i in issues)
    return WorkflowValidation(valid=ok, issues=issues, order=order if ok else [], resolved=resolved)


def _input_ports(
    n: WorkflowNode, impl: NodeImplementation, subs: Mapping[str, WorkflowSpec]
) -> dict[str, Any]:
    if impl.id == SUBWORKFLOW_NODE and n.id in subs:
        from mapi_spec.workflow_nodes import PortSpec

        sub = subs[n.id]
        return {
            k: PortSpec(type=v.type, required=v.required and v.default is None)
            for k, v in sub.inputs.items()
        }
    return dict(impl.inputs)


def _binding_type(
    spec: WorkflowSpec,
    binding: str,
    impls: Mapping[str, NodeImplementation],
    subs: Mapping[str, WorkflowSpec],
    by_id: Mapping[str, WorkflowNode],
) -> PortType | str:
    """The port type a binding delivers, or why it is wrong (a message)."""
    m = _BIND_RE.match(binding)
    if m is None:  # pragma: no cover - checked by the model
        return f"{binding!r} is not a binding"
    if m.group(1) is not None:
        name = m.group(1)
        inp = spec.inputs.get(name)
        if inp is None:
            return f"binds unknown workflow input {name!r}"
        return inp.type
    src, port = m.group(2), m.group(3)
    if src not in by_id:
        return f"binds unknown node {src!r}"
    impl = impls.get(src)
    if impl is None:
        return f"binds {src!r}, but the implementation of {src!r} is unknown"
    if impl.id == SUBWORKFLOW_NODE:
        sub = subs.get(src)
        if sub is None:
            return f"cannot do a check of {binding!r}: the sub-workflow is not resolved"
        target = sub.outputs.get(port)
        if target is None:
            return f"the sub-workflow of {src!r} has no output {port!r}"
        t = _binding_type(sub, target, _impls_of(sub), {}, {x.id: x for x in sub.nodes})
        return t
    out = impl.outputs.get(port)
    if out is None:
        return (
            f"binds {src}.{port}, but {impl.id} has no output {port!r} (it has: "
            f"{', '.join(sorted(impl.outputs)) or 'none'})"
        )
    return out


def _impls_of(spec: WorkflowSpec) -> dict[str, NodeImplementation]:
    return {
        n.id: NODE_IMPLEMENTATIONS[n.impl_id]
        for n in spec.nodes
        if n.impl_id in NODE_IMPLEMENTATIONS
    }


def _check_params(
    spec: WorkflowSpec, n: WorkflowNode, impl: NodeImplementation, base: str
) -> list[Issue]:
    issues: list[Issue] = []
    instance: dict[str, Any] = {}
    deferred: set[str] = set()
    for key, value in n.with_.items():
        ph = list(_placeholders(value, (key,)))
        whole = len(ph) == 1 and ph[0][0] == (key,)
        for path, kind, text in ph:
            ptr = base + json_pointer("with", *path)
            if kind == "$param":
                p = spec.params.get(text)
                if p is None:
                    issues.append(_issue(
                        ErrorCode.WORKFLOW_INVALID, ptr,
                        f"node {n.id!r} uses the unknown workflow parameter {text!r}",
                    ))  # fmt: skip
            elif not text:
                issues.append(_issue(ErrorCode.EXPRESSION_INVALID, ptr,
                                     "$expr needs a text expression"))  # fmt: skip
        if whole and ph[0][1] == "$param" and ph[0][2] in spec.params:
            p = spec.params[ph[0][2]]
            if p.default is not None:
                instance[key] = p.default
            else:
                deferred.add(key)
        elif ph:
            deferred.add(key)
        else:
            instance[key] = value
    schema = _without(dict(impl.params_schema), deferred)
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for key in sorted((deferred | set(instance)) - set(props)):
            issues.append(_issue(
                ErrorCode.WORKFLOW_INVALID, base + json_pointer("with", key),
                f"node {n.id!r} ({impl.id}): unknown parameter {key!r} (known: "
                f"{', '.join(sorted(props)) or 'none'})",
            ))  # fmt: skip
            instance.pop(key, None)
    for err in sorted(
        Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path)
    )[:20]:
        issues.append(_issue(
            ErrorCode.WORKFLOW_INVALID, base + json_pointer("with", *err.path),
            f"node {n.id!r} ({impl.id}): {err.message[:300]}",
        ))  # fmt: skip
    return issues


def _check_model_ref(n: WorkflowNode, base: str, catalog: Catalog | None) -> list[Issue]:
    ref = n.with_.get("model")
    if not isinstance(ref, str):
        return []
    if catalog is None:
        return []
    try:
        found = catalog.resolve_model(ref)
    except ValueError as exc:
        return [_issue(ErrorCode.REF_UNRESOLVED, base + "/with/model", str(exc)[:300])]
    if found is None:
        return [_issue(
            ErrorCode.REF_UNRESOLVED, base + "/with/model",
            f"node {n.id!r}: no catalog model matches {ref[:80]!r}",
            action="Select a model from GET /v1/catalog/models.",
        )]  # fmt: skip
    return []


def _check_plot_ref(n: WorkflowNode, base: str, catalog: Catalog | None) -> list[Issue]:
    """``plot.render``'s ``recipe`` names a bundled recipe (one with a built-in About) or a
    plot of the catalog. Without a catalog, plugin recipes cannot be known: no check."""
    ref = n.with_.get("recipe")
    if not isinstance(ref, str) or not ref or catalog is None:
        return []
    from mapi_spec.plot_abouts import PLOT_ABOUTS

    ptr = base + "/with/recipe"
    try:
        found = catalog.resolve_plot(ref) if "@" in ref else None
        rid = UseRef.parse(ref).id if "@" in ref else ref
    except ValueError as exc:
        return [_issue(ErrorCode.REF_UNRESOLVED, ptr, str(exc)[:300])]
    known = rid in PLOT_ABOUTS or any(e.id == rid and not e.yanked for e in catalog.plots)
    if found is None and not known:
        return [_issue(
            ErrorCode.REF_UNRESOLVED, ptr,
            f"node {n.id!r}: no plot recipe matches {ref[:80]!r}",
            action="Select a recipe from GET /v1/catalog/plots.",
        )]  # fmt: skip
    return []


def _check_subworkflow(
    n: WorkflowNode, base: str, resolve: SubworkflowResolver | None, issues: list[Issue]
) -> WorkflowSpec | None:
    ref_text = n.with_.get("workflow")
    if not isinstance(ref_text, str):
        return None
    try:
        ref = WorkflowRef.parse(ref_text)
    except ValueError as exc:
        issues.append(_issue(ErrorCode.WORKFLOW_INVALID, base + "/with/workflow", str(exc)))
        return None
    if resolve is None:
        return None
    sub = resolve(ref)
    if sub is None:
        issues.append(_issue(
            ErrorCode.REF_UNRESOLVED, base + "/with/workflow",
            f"node {n.id!r}: the workflow {ref} does not exist",
        ))  # fmt: skip
    return sub


def _check_expressions(
    spec: WorkflowSpec,
    n: WorkflowNode,
    base: str,
    impls: Mapping[str, NodeImplementation],
    by_id: Mapping[str, WorkflowNode],
) -> list[Issue]:
    issues: list[Issue] = []
    texts: list[tuple[str, str]] = [
        (base + json_pointer("with", *path), t)
        for path, kind, t in _placeholders(n.with_)
        if kind == "$expr" and t
    ]
    if impls[n.id].kind is NodeKind.BRANCH:
        cond = n.with_.get("condition")
        if isinstance(cond, str):
            texts.append((base + "/with/condition", cond))
    deps = node_dependencies(spec)
    upstream = _ancestors(deps, n.id)
    for ptr, text in texts:
        try:
            ex = parse_expression(text)
        except MapiError as exc:
            issues.append(_issue(ErrorCode.EXPRESSION_INVALID, ptr, exc.detail))
            continue
        for ref in ex.references():
            if ref.root == "params":
                name = ref.path[0] if ref.path else None
                if not isinstance(name, str) or name not in spec.params:
                    issues.append(
                        _issue(
                            ErrorCode.EXPRESSION_INVALID,
                            ptr,
                            f"{ref} names an unknown workflow parameter",
                        )
                    )
            elif ref.root == "inputs":
                name = ref.path[0] if ref.path else None
                if not isinstance(name, str) or name not in spec.inputs:
                    issues.append(
                        _issue(
                            ErrorCode.EXPRESSION_INVALID,
                            ptr,
                            f"{ref} names an unknown workflow input",
                        )
                    )
            elif ref.root == "nodes":
                if not ref.path or not isinstance(ref.path[0], str):
                    issues.append(
                        _issue(ErrorCode.EXPRESSION_INVALID, ptr, "nodes.<id> needs a node id")
                    )
                    continue
                nid = ref.path[0]
                if nid not in by_id:
                    issues.append(
                        _issue(
                            ErrorCode.EXPRESSION_INVALID,
                            ptr,
                            f"{ref} names an unknown node {nid!r}",
                        )
                    )
                    continue
                if nid == n.id or nid not in upstream:
                    issues.append(
                        _issue(
                            ErrorCode.EXPRESSION_INVALID,
                            ptr,
                            f"{ref}: node {nid!r} does not run before {n.id!r}",
                        )
                    )
                    continue
                impl = impls.get(nid)
                if impl is None or impl.id == SUBWORKFLOW_NODE or len(ref.path) < 2:
                    continue
                fld = ref.path[1]
                if not isinstance(fld, str) or fld not in impl.result_fields:
                    issues.append(_issue(
                        ErrorCode.EXPRESSION_INVALID, ptr,
                        f"{ref}: {impl.id} results have no field {fld!r} (they have: "
                        f"{', '.join(sorted(impl.result_fields)) or 'none'})",
                    ))  # fmt: skip
    return issues


def _ancestors(deps: Mapping[str, set[str]], node: str) -> set[str]:
    seen: set[str] = set()
    todo = list(deps.get(node, ()))
    while todo:
        d = todo.pop()
        if d in seen:
            continue
        seen.add(d)
        todo.extend(deps.get(d, ()))
    return seen


def _reachability(
    spec: WorkflowSpec, impls: Mapping[str, NodeImplementation], order: Sequence[str]
) -> list[Issue]:
    """Nodes that can never run (needing both arms of one branch) and unused results."""
    issues: list[Issue] = []
    index = {n.id: i for i, n in enumerate(spec.nodes)}
    by_id = {n.id: n for n in spec.nodes}
    # arms[n] = set of (branch, arm) every execution of n requires
    arms: dict[str, set[tuple[str, str]]] = {}
    for nid in order:
        n = by_id[nid]
        req: set[tuple[str, str]] = set()
        if n.when:
            m = _WHEN_RE.match(n.when)
            assert m is not None
            req.add((m.group(1), m.group(2)))
            req |= arms.get(m.group(1), set())
        for port in n.inputs:
            alts = n.alternatives(port)
            options: list[set[tuple[str, str]]] = []
            for b in alts:
                m3 = _BIND_RE.match(b)
                options.append(arms.get(m3.group(2), set()) if m3 and m3.group(2) else set())
            if options:
                # a port is satisfied by any alternative: only arms every option needs count
                req |= set.intersection(*options)
        arms[nid] = req
        branches: dict[str, str] = {}
        for b_id, arm in req:
            if branches.get(b_id, arm) != arm:
                issues.append(_issue(
                    ErrorCode.WORKFLOW_INVALID, json_pointer("nodes", index[nid]),
                    f"node {nid!r} can never run: it needs both the then and the else arm "
                    f"of {b_id!r}",
                    action="Give the input an alternative (a list of bindings) or move the "
                    "node out of one arm.",
                ))  # fmt: skip
                break
            branches[b_id] = arm
    used: set[str] = set()
    for n in spec.nodes:
        for port in n.inputs:
            for b in n.alternatives(port):
                m4 = _BIND_RE.match(b)
                if m4 and m4.group(2):
                    used.add(m4.group(2))
        if n.when:
            m5 = _WHEN_RE.match(n.when)
            if m5:
                used.add(m5.group(1))
        used |= set(_expr_nodes(n))
    for b in spec.outputs.values():
        m6 = _BIND_RE.match(b)
        if m6 and m6.group(2):
            used.add(m6.group(2))
    for n in spec.nodes:
        impl = impls.get(n.id)
        if impl is None or n.id in used or impl.kind in SINK_KINDS:
            continue
        if impl.kind is NodeKind.BRANCH:
            msg = f"branch {n.id!r} controls no node (no node uses when: {n.id}.then/else)"
        else:
            msg = f"no node or output uses the result of {n.id!r}"
        issues.append(_issue(ErrorCode.WORKFLOW_INVALID, json_pointer("nodes", index[n.id]),
                             msg, severity=Severity.WARNING))  # fmt: skip
    return issues


def check_run_params(spec: WorkflowSpec, given: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The run's parameters: ``given`` merged over defaults and checked (``WORKFLOW_INVALID``
    with a pointer for unknown names, wrong types and missing required parameters)."""
    issues: list[Issue] = []
    out: dict[str, JsonValue] = {}
    for name in given:
        if name not in spec.params:
            issues.append(
                _issue(
                    ErrorCode.WORKFLOW_INVALID,
                    json_pointer("params", name),
                    f"unknown parameter {name!r}",
                )
            )
    for name, p in spec.params.items():
        if name in given:
            problem = param_value_problem(p, given[name])
            if problem:
                issues.append(
                    _issue(
                        ErrorCode.WORKFLOW_INVALID,
                        json_pointer("params", name),
                        f"parameter {name!r} {problem}",
                    )
                )
                continue
            out[name] = given[name]
        elif p.default is not None:
            out[name] = p.default
        elif p.required:
            issues.append(
                _issue(
                    ErrorCode.WORKFLOW_INVALID,
                    json_pointer("params", name),
                    f"parameter {name!r} needs a value",
                )
            )
    if issues:
        raise MapiError("the run parameters are not valid", code=ErrorCode.WORKFLOW_INVALID,
                        issues=issues)  # fmt: skip
    return out


# ===================================================================================== DTOs

_WF_ID = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0"
_RUN_ID = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
_SHA = "sha256:" + "5a" * 32
_EXAMPLE_DOC: dict[str, JsonValue] = {
    "apiVersion": "mapi/v1",
    "kind": "Workflow",
    "metadata": {"name": "ts-readiness", "project": "growth"},
    "params": {"alpha": {"type": "number", "default": 0.05}},
    "inputs": {"data": {"type": "dataset"}},
    "nodes": [
        {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"},
         "with": {"alpha": {"$param": "alpha"}}},
        {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}},
        {"id": "wanders", "use": "branch.if",
         "with": {"condition": "nodes.adf.p_value >= params.alpha or nodes.kpss.reject"}},
        {"id": "diff", "use": "transform.difference", "inputs": {"data": "inputs.data"},
         "when": "wanders.then"},
        {"id": "order", "use": "search.arma_order",
         "inputs": {"data": ["diff.data", "inputs.data"]}},
    ],
    "outputs": {"orders": "order.table"},
}  # fmt: skip


class WorkflowCreate(RequestModel):
    """``POST /v1/workflows``: a workflow with its first revision (the document)."""

    document: dict[str, JsonValue] | None = Field(None, description="The WorkflowSpec as JSON")
    text: str | None = Field(None, max_length=256 * 1024, description="Or YAML/JSON text")
    template: str | None = Field(
        None, pattern=r"^[a-z][a-z0-9_]{0,63}$", description="Or a gallery template id"
    )
    message: str | None = Field(None, max_length=2000)

    model_config = dto_examples({"document": _EXAMPLE_DOC, "message": "first version"})

    @model_validator(mode="after")
    def _one(self) -> WorkflowCreate:
        if sum(x is not None for x in (self.document, self.text, self.template)) != 1:
            raise ValueError("give exactly one of document, text or template")
        return self


class WorkflowUpdate(RequestModel):
    """``PATCH /v1/workflows/{workflow_id}``: metadata only (documents are revisions)."""

    description: str | None = Field(None, max_length=4000)
    archived: bool | None = None

    model_config = dto_examples({"description": "Weekly readiness checks", "archived": False})


class Workflow(ResponseModel):
    """A saved workflow (``workflow://project/name``) and its latest revision."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId
    name: str
    ref: str = Field(..., description="workflow://project/name")
    description: str | None = None
    latest_revision: int = Field(..., ge=1)
    archived: bool = False
    template: str | None = Field(None, description="Gallery template it was created from")
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: str | None = None

    model_config = dto_examples(
        {
            "id": _WF_ID,
            "tenant_id": "acme",
            "project_id": "growth",
            "name": "ts-readiness",
            "ref": "workflow://growth/ts-readiness",
            "latest_revision": 2,
            "created_at": EXAMPLE_TS,
            "updated_at": EXAMPLE_TS,
        }
    )


class WorkflowRevisionCreate(RequestModel):
    """``POST /v1/workflows/{workflow_id}/revisions``."""

    document: dict[str, JsonValue] | None = None
    text: str | None = Field(None, max_length=256 * 1024)
    message: str | None = Field(None, max_length=2000)
    expected_revision: int | None = Field(
        None, ge=1, description="Refuse (409) when the latest revision is different"
    )

    model_config = dto_examples({"document": _EXAMPLE_DOC, "expected_revision": 1})

    @model_validator(mode="after")
    def _one(self) -> WorkflowRevisionCreate:
        if (self.document is None) == (self.text is None):
            raise ValueError("give exactly one of document or text")
        return self


class WorkflowRevision(ResponseModel):
    """An immutable, hashed revision; ``validation`` is its static check at save time
    (revisions with errors are stored but cannot run: ``WORKFLOW_NOT_RUNNABLE``)."""

    workflow_id: Id
    revision: int = Field(..., ge=1)
    ref: str = Field(..., description="workflow://project/name@rN")
    document: dict[str, JsonValue]
    spec_hash: Sha256
    validation: WorkflowValidation
    message: str | None = None
    created_at: AwareDatetime
    created_by: str | None = None

    model_config = dto_examples(
        {
            "workflow_id": _WF_ID,
            "revision": 2,
            "ref": "workflow://growth/ts-readiness@r2",
            "document": _EXAMPLE_DOC,
            "spec_hash": _SHA,
            "validation": {"valid": True, "issues": [],
                           "order": ["adf", "kpss", "wanders", "diff", "order"]},
            "created_at": EXAMPLE_TS,
        }
    )  # fmt: skip


class WorkflowValidateRequest(RequestModel):
    """``POST /v1/workflows:validate`` (sync, stores nothing)."""

    document: dict[str, JsonValue] | None = None
    text: str | None = Field(None, max_length=256 * 1024)

    model_config = dto_examples({"document": _EXAMPLE_DOC})

    @model_validator(mode="after")
    def _one(self) -> WorkflowValidateRequest:
        if (self.document is None) == (self.text is None):
            raise ValueError("give exactly one of document or text")
        return self


class WorkflowTriggerEvent(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    DATASET_VERSION_CREATED = "dataset.version.created"
    PARENT = "parent"
    """A sub-workflow run started by a parent workflow."""
    VALIDATION = "validation"
    """A governance workflow started by a model validation request."""


class WorkflowRunCreate(RequestModel):
    """``POST /v1/workflows/{workflow_id}/runs`` (``Idempotency-Key``)."""

    revision: int | None = Field(None, ge=1, description="Default: the latest revision")
    params: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    inputs: dict[str, str] = Field(
        default_factory=dict, max_length=16, description="input name -> reference"
    )
    use_cache: bool = Field(True, description="Reuse unchanged node results")
    resource_class: ResourceClass | None = None

    model_config = dto_examples(
        {"params": {"alpha": 0.05}, "inputs": {"data": "dataset://growth/weekly_sales@v4"}}
    )


class WorkflowRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (
            WorkflowRunStatus.SUCCEEDED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        )


class NodeRunStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CACHED = "cached"
    """Succeeded by reusing an earlier result with the same cache key."""
    SKIPPED = "skipped"
    """Gated by a branch arm that was not taken, or every alternative input was skipped."""
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def done(self) -> bool:
        return self not in (NodeRunStatus.PENDING, NodeRunStatus.QUEUED, NodeRunStatus.RUNNING)

    @property
    def produced(self) -> bool:
        """The node has outputs (dependants may bind them)."""
        return self in (NodeRunStatus.SUCCEEDED, NodeRunStatus.CACHED)


class NodeOutputRef(ResponseModel):
    """Where a node output lives: a reference (``dataset://``, ``model://``), a run artifact
    (``run_id`` + ``object``) or, for small JSON payloads (test results, split plans,
    metrics), ``inline``."""

    type: PortType
    ref: str | None = Field(None, max_length=512)
    run_id: Id | None = None
    object: str | None = Field(None, max_length=256)
    inline: JsonValue = None


class NodeError(ResponseModel):
    code: ErrorCode
    message: str = Field(..., max_length=2000)
    untrusted_text: str | None = Field(None, max_length=8000)


class NodeRun(ResponseModel):
    """One node of a workflow run."""

    node_id: str
    use: str = Field(..., description="Resolved implementation ref (id@version)")
    kind: NodeKind
    status: NodeRunStatus
    attempt: int = Field(0, ge=0)
    run_id: Id | None = Field(None, description="The workflow_node run that executed it")
    child_runs: list[Id] = Field(default_factory=list, description="Fits and other children")
    cache_key: Sha256 | None = None
    cached_from: Id | None = Field(None, description="Workflow run whose result was reused")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="Resolved params")
    result: dict[str, JsonValue] | None = Field(
        None, description="JSON result conditions read (nodes.<id>.<field>)"
    )
    outputs: dict[str, NodeOutputRef] = Field(default_factory=dict)
    skipped_reason: str | None = Field(None, max_length=500)
    error: NodeError | None = None
    cost: RunCost | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "node_id": "adf",
            "use": "test.adf@1.0.0",
            "kind": "test",
            "status": "succeeded",
            "attempt": 1,
            "run_id": _RUN_ID,
            "cache_key": _SHA,
            "params": {"alpha": 0.05, "regression": "c"},
            "result": {"statistic": -1.42, "p_value": 0.57, "reject": False,
                       "decision": "unit_root"},
            "outputs": {"result": {"type": "test_result", "inline": {"test": "test.adf"}}},
            "started_at": EXAMPLE_TS,
            "finished_at": "2026-10-15T09:30:04Z",
        }
    )  # fmt: skip


class BranchDecision(ResponseModel):
    """Why a branch went one way: the condition, its value and the values it read."""

    node_id: str
    condition: str
    value: bool
    arm: Literal["then", "else"]
    inputs: dict[str, JsonValue] = Field(
        default_factory=dict, description="Each reference in the condition -> its value"
    )
    explain_plain: str | None = Field(
        None,
        max_length=300,
        description="What the path taken means, in plain words (the branch's explain_then or "
        "explain_else); none when the branch has no explanation",
    )
    decided_at: AwareDatetime

    model_config = dto_examples(
        {
            "node_id": "wanders",
            "condition": "nodes.adf.p_value >= params.alpha or nodes.kpss.reject",
            "value": True,
            "arm": "then",
            "inputs": {"nodes.adf.p_value": 0.57, "params.alpha": 0.05, "nodes.kpss.reject": True},
            "explain_plain": "The tests found that the series wanders and does not return to "
            "a steady level. The workflow applied a difference.",
            "decided_at": EXAMPLE_TS,
        }
    )


class WorkflowRun(ResponseModel):
    """``GET /v1/workflow-runs/{run_id}``: a run of a workflow revision with every node."""

    run_id: Id = Field(..., description="The parent run (kind workflow)")
    workflow_id: Id
    revision: int = Field(..., ge=1)
    tenant_id: TenantId
    project_id: TenantId
    status: WorkflowRunStatus
    trigger: WorkflowTriggerEvent = WorkflowTriggerEvent.MANUAL
    trigger_id: Id | None = Field(None, description="The schedule or trigger that started it")
    parent_run_id: Id | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    nodes: list[NodeRun] = Field(default_factory=list)
    decisions: list[BranchDecision] = Field(default_factory=list)
    outputs: dict[str, NodeOutputRef] = Field(default_factory=dict)
    cost: RunCost | None = None
    error: NodeError | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    created_by: str | None = None

    model_config = dto_examples(
        {
            "run_id": _RUN_ID,
            "workflow_id": _WF_ID,
            "revision": 2,
            "tenant_id": "acme",
            "project_id": "growth",
            "status": "running",
            "params": {"alpha": 0.05},
            "inputs": {"data": "dataset://growth/weekly_sales@v4"},
            "nodes": [NodeRun.model_json_schema()["examples"][0]],
            "decisions": [BranchDecision.model_json_schema()["examples"][0]],
            "created_at": EXAMPLE_TS,
            "started_at": EXAMPLE_TS,
        }
    )

    def node(self, node_id: str) -> NodeRun | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)


class WorkflowEventType(StrEnum):
    """Events of a workflow run (SSE on ``…/events``; the terminal ones also go to
    webhooks as ``workflow.run.succeeded`` / ``workflow.run.failed``)."""

    RUN_STARTED = "workflow.run.started"
    NODE_QUEUED = "workflow.node.queued"
    NODE_STARTED = "workflow.node.started"
    NODE_SUCCEEDED = "workflow.node.succeeded"
    NODE_CACHED = "workflow.node.cached"
    NODE_SKIPPED = "workflow.node.skipped"
    NODE_FAILED = "workflow.node.failed"
    NODE_RETRYING = "workflow.node.retrying"
    BRANCH_DECIDED = "workflow.branch.decided"
    RUN_SUCCEEDED = "workflow.run.succeeded"
    RUN_FAILED = "workflow.run.failed"
    RUN_CANCELLED = "workflow.run.cancelled"


class WorkflowEvent(ResponseModel):
    seq: int = Field(..., ge=0)
    type: WorkflowEventType
    run_id: Id
    node_id: str | None = None
    ts: AwareDatetime
    data: dict[str, JsonValue] = Field(default_factory=dict)

    model_config = dto_examples(
        {
            "seq": 7,
            "type": "workflow.branch.decided",
            "run_id": _RUN_ID,
            "node_id": "wanders",
            "ts": EXAMPLE_TS,
            "data": {"arm": "then"},
        }
    )


# ================================================================================ automation


class ScheduleCreate(RequestModel):
    """``POST /v1/workflows/{workflow_id}/schedules``: run on a cron schedule."""

    cron: str = Field(..., max_length=200, examples=["0 6 * * 1"])
    timezone: str = Field("UTC", max_length=64, examples=["Europe/Berlin"])
    revision: int | None = Field(None, ge=1, description="Pin a revision (default: latest)")
    params: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    inputs: dict[str, str] = Field(default_factory=dict, max_length=16)
    active: bool = True
    description: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "cron": "0 6 * * 1",
            "timezone": "Europe/Berlin",
            "inputs": {"data": "dataset://growth/weekly_sales@latest"},
            "description": "Every Monday at 06:00",
        }
    )

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        try:
            return check_cron(v)
        except MapiError as exc:
            raise ValueError(exc.detail) from None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            check_timezone(v)
        except MapiError as exc:
            raise ValueError(exc.detail) from None
        return v


class ScheduleUpdate(RequestModel):
    """``PATCH /v1/workflows/{workflow_id}/schedules/{schedule_id}``."""

    cron: str | None = Field(None, max_length=200)
    timezone: str | None = Field(None, max_length=64)
    revision: int | None = Field(None, ge=1)
    params: dict[str, JsonValue] | None = None
    inputs: dict[str, str] | None = None
    active: bool | None = None

    model_config = dto_examples({"active": False}, {"cron": "0 7 * * 1-5"})

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            return check_cron(v)
        except MapiError as exc:
            raise ValueError(exc.detail) from None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                check_timezone(v)
            except MapiError as exc:
                raise ValueError(exc.detail) from None
        return v


class WorkflowSchedule(ResponseModel):
    id: Id
    workflow_id: Id
    cron: str
    timezone: str
    revision: int | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    active: bool
    description: str | None = None
    next_run_at: AwareDatetime | None = None
    last_run_id: Id | None = None
    last_run_at: AwareDatetime | None = None
    created_at: AwareDatetime
    created_by: str | None = None

    model_config = dto_examples(
        {
            "id": "sch_01jaf3f1g2h3j4k5m6n7p8q9r0",
            "workflow_id": _WF_ID,
            "cron": "0 6 * * 1",
            "timezone": "Europe/Berlin",
            "active": True,
            "next_run_at": "2026-10-19T04:00:00Z",
            "created_at": EXAMPLE_TS,
        }
    )


class TriggerCreate(RequestModel):
    """``POST /v1/workflows/{workflow_id}/triggers``: run when a new version of a dataset
    is ready; the version is bound to workflow input ``input``."""

    event: Literal["dataset.version.created"] = "dataset.version.created"
    dataset: DatasetRef = Field(..., description="dataset://project/name (any version)")
    input: Name = Field(..., description="The workflow input the new version is bound to")
    revision: int | None = Field(None, ge=1)
    params: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    active: bool = True
    debounce_seconds: int = Field(
        300, ge=0, le=86_400, description="Collapse versions arriving within this window"
    )

    model_config = dto_examples(
        {"dataset": "dataset://growth/weekly_sales", "input": "data", "debounce_seconds": 600}
    )

    @field_validator("dataset")
    @classmethod
    def _any_version(cls, v: DatasetRef) -> DatasetRef:
        if v.version is not None:
            raise ValueError("a trigger watches a dataset, not one of its versions")
        return v


class WorkflowTrigger(ResponseModel):
    id: Id
    workflow_id: Id
    event: Literal["dataset.version.created"]
    dataset: str
    input: str
    revision: int | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    active: bool
    debounce_seconds: int = 300
    last_fired_at: AwareDatetime | None = None
    last_run_id: Id | None = None
    created_at: AwareDatetime
    created_by: str | None = None

    model_config = dto_examples(
        {
            "id": "trg_01jaf3g1h2j3k4m5n6p7q8r9s0",
            "workflow_id": _WF_ID,
            "event": "dataset.version.created",
            "dataset": "dataset://growth/weekly_sales",
            "input": "data",
            "active": True,
            "created_at": EXAMPLE_TS,
        }
    )


# ============================================================================ runner contract


class WorkflowNodeJob(StrictModel):
    """``RunSpec.spec`` of a ``workflow_node`` run: execute one sandbox node.

    Inputs arrive as run inputs named ``in.<port>.<k>`` (``WORKFLOW_RUN_INPUT_PREFIX``);
    ``input_types`` gives each one's port type and ``inline_inputs`` carries small JSON
    payloads (test results, split plans, metrics) directly. The runner writes
    :data:`NODE_RESULT_OUTPUT` and one object per output port it lists there.
    """

    workflow_run_id: Id
    node_id: str = Field(..., pattern=_ID_RE)
    use: str = Field(..., description="Resolved implementation ref (id@version)")
    params: dict[str, JsonValue] = Field(default_factory=dict)
    input_types: dict[str, PortType] = Field(default_factory=dict)
    inline_inputs: dict[str, JsonValue] = Field(default_factory=dict)
    seed: int = Field(0, ge=0, lt=2**32)

    model_config = dto_examples(
        {
            "workflow_run_id": _RUN_ID,
            "node_id": "adf",
            "use": "test.adf@1.0.0",
            "params": {"alpha": 0.05, "regression": "c", "autolag": "AIC", "per_series": True},
            "input_types": {"in.data.0": "dataset"},
        }
    )


class NodeResultSidecar(ResponseModel):
    """``node_result.json`` of a ``workflow_node`` run (validated by the finalizer, capped
    at :data:`NODE_RESULT_MAX_BYTES`): the JSON result and where each output port's object
    is (``object``: a run output name) or its inline JSON payload."""

    format: Literal["mapi.node-result/v1"] = "mapi.node-result/v1"
    node_id: str
    use: str
    result: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[str, NodeOutputRef] = Field(default_factory=dict)
    warnings_plain: list[str] = Field(default_factory=list, max_length=50)
