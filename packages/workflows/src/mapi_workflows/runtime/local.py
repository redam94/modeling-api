"""Run a whole workflow in this process (notebooks, ``mapi workflow run --local``, tests).

:class:`LocalRunner` drives the same :class:`~mapi_workflows.engine.WorkflowEngine` as the
control plane, but executes every node here: data nodes through the runtime registry
(:mod:`mapi_workflows.runtime.nodes`), model fits in-process through the SDK harness, and
sub-workflows recursively. Each node runs in a worker thread so its ``timeout`` (and the
run's) is enforced: on expiry the node's cancel token is set and the node fails with
``TIMEOUT``. Results are cached in memory by cache key, so running the same workflow on
the same data again reuses unchanged nodes.

Never run untrusted workflows or plugins this way: there is no sandbox.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import xarray as xr
from mapi_sdk.errors import RunCancelled
from mapi_sdk.harness import data_hash
from mapi_sdk.registry import Registry
from mapi_sdk.steps import CancelToken
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.refs import WorkflowRef
from mapi_spec.workflow import (
    BranchDecision,
    NodeError,
    NodeOutputRef,
    NodeRun,
    WorkflowRunStatus,
    WorkflowSpec,
    check_run_params,
    validate_workflow,
)
from mapi_spec.workflow_nodes import SUBWORKFLOW_NODE, PortSpec, PortType
from mapi_workflows.engine import Action, CacheHit, EngineEvent, WorkflowEngine, input_identity
from mapi_workflows.runtime.governance import GovernanceServices
from mapi_workflows.runtime.nodes import node_registry
from mapi_workflows.runtime.values import (
    DatasetValue,
    ModelValue,
    NodeContext,
    NodeFn,
    NodeOutcome,
    Value,
)
from pydantic import JsonValue

__all__ = ["MAX_SUBWORKFLOW_DEPTH", "LocalRunResult", "LocalRunner", "as_value"]

MAX_SUBWORKFLOW_DEPTH = 8
"""How deeply sub-workflows may nest (a workflow that runs itself stops here)."""

SubworkflowResolver = Callable[[WorkflowRef], WorkflowSpec | None]


def as_value(v: Any) -> Value:
    """Workflow inputs: an ``xr.Dataset`` becomes a :class:`DatasetValue`; an
    ``xr.DataTree`` (a tree dataset) one whose ``data`` is its root node and whose ``tree``
    is the tree (a model node's ``nodes`` select other nodes of it)."""
    if isinstance(v, xr.Dataset):
        return DatasetValue(data=v)
    if isinstance(v, xr.DataTree):
        return DatasetValue.from_tree(v)
    if isinstance(v, DatasetValue | ModelValue | dict):
        return v
    raise TypeError(f"cannot use a {type(v).__name__} as a workflow input")


def _hash(value: DatasetValue) -> str:
    """A tree input is identified by its whole tree (its root node may be empty)."""
    if value.tree is not None and value.root is None:
        return data_hash(value.tree)
    return data_hash(value.data)


def _ref_for(value: Value, type_: PortType) -> NodeOutputRef:
    if isinstance(value, DatasetValue):
        return NodeOutputRef(type=type_, ref=value.ref or f"local://dataset/{_hash(value)}")
    if isinstance(value, ModelValue):
        return NodeOutputRef(type=type_, ref=value.ref or f"local://model/{value.bundle.bundle_id}")
    return NodeOutputRef(type=type_, inline=value)


@dataclass(slots=True)
class LocalRunResult:
    """The outcome of :meth:`LocalRunner.run`."""

    status: WorkflowRunStatus
    nodes: list[NodeRun]
    decisions: list[BranchDecision]
    outputs: dict[str, NodeOutputRef]
    values: dict[str, Value]
    events: list[EngineEvent]
    error: NodeError | None = None
    node_values: dict[str, dict[str, Value]] = field(default_factory=dict)

    def node(self, node_id: str) -> NodeRun:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)

    def output(self, name: str) -> Value:
        return self.values[name]


@dataclass(slots=True)
class _Cached:
    hit: CacheHit
    values: dict[str, Value]


class LocalRunner:
    """Execute workflows in-process (see the module docstring)."""

    def __init__(
        self,
        *,
        registry: Registry | None = None,
        nodes: Mapping[str, NodeFn] | None = None,
        governance: GovernanceServices | None = None,
        subworkflows: SubworkflowResolver | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
        seed: int = 0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.registry = registry
        self.nodes = node_registry(nodes)
        self.governance = governance or GovernanceServices()
        self.subworkflows = subworkflows
        self.on_event = on_event
        self.seed = seed
        self.log = log
        self.cache: dict[str, _Cached] = {}
        self._values: dict[str, Value] = {}
        self._depth = 0

    # ------------------------------------------------------------------------- run
    def run(
        self,
        workflow: WorkflowSpec | Mapping[str, Any] | str,
        *,
        inputs: Mapping[str, Any] | None = None,
        params: Mapping[str, JsonValue] | None = None,
        use_cache: bool = True,
        cancel: CancelToken | None = None,
    ) -> LocalRunResult:
        """Run ``workflow`` (a spec, a document or YAML text) on ``inputs``."""
        outer = self._values
        try:
            return self._run(
                workflow, inputs=inputs, params=params, use_cache=use_cache, cancel=cancel
            )
        finally:
            self._values = outer

    def _run(
        self,
        workflow: WorkflowSpec | Mapping[str, Any] | str,
        *,
        inputs: Mapping[str, Any] | None,
        params: Mapping[str, JsonValue] | None,
        use_cache: bool,
        cancel: CancelToken | None,
    ) -> LocalRunResult:
        spec = self._spec(workflow)
        validation = validate_workflow(spec, resolve_subworkflow=self.subworkflows)
        if not validation.valid:
            raise MapiError(
                "the workflow is not valid",
                code=ErrorCode.WORKFLOW_INVALID,
                issues=validation.issues,
            )
        resolved_params = check_run_params(spec, dict(params or {}))
        values: dict[str, Value] = {}
        self._values = values
        refs: dict[str, NodeOutputRef] = {}
        identities: dict[str, str] = {}
        for name, decl in spec.inputs.items():
            if name not in (inputs or {}):
                if decl.required and decl.default is None:
                    raise MapiError(
                        f"input {name!r} needs a value",
                        code=ErrorCode.WORKFLOW_INVALID,
                        pointer=f"/inputs/{name}",
                    )
                continue
            v = as_value((inputs or {})[name])
            if isinstance(v, DatasetValue) and v.ref is None:
                v.ref = f"local://inputs/{name}/{_hash(v)}"
            ref = _ref_for(v, decl.type)
            refs[name] = ref
            values[_key(ref)] = v
            identities[name] = ref.ref or ""
        cancel = cancel or CancelToken()
        engine = WorkflowEngine(
            spec,
            params=resolved_params,
            inputs=refs,
            input_identities=identities,
            use_cache=use_cache,
            cache=self._lookup,
            run_meta={"attempt": 1, "trigger": "manual"},
            subworkflow_ports=self._sub_ports(spec),
        )
        events: list[EngineEvent] = []
        deadline = time.monotonic() + spec.execution.timeout.seconds
        while not engine.finished:
            if cancel.cancelled and not engine.cancel_requested:
                engine.cancel()
            if time.monotonic() > deadline:
                engine.fail_run(
                    NodeError(code=ErrorCode.TIMEOUT, message="the workflow run timed out")
                )
            for nid in list(engine.to_cancel):
                engine.cancelled(nid)
            actions = engine.step()
            self._drain(engine, events)
            if not actions:
                if engine.finished or not engine.running():
                    break
                continue
            for action in actions:
                if cancel.cancelled:
                    if not engine.cancel_requested:
                        engine.cancel()
                    engine.cancelled(action.node_id)
                    continue
                self._execute(engine, action, values, cancel, deadline, spec)
                self._drain(engine, events)
        engine.finish_events()
        self._drain(engine, events)
        outputs = engine.outputs()
        node_values: dict[str, dict[str, Value]] = {}
        for nr in engine.node_runs():
            per = {p: x for p, r in nr.outputs.items() if (x := _value(r, values)) is not None}
            if per:
                node_values[nr.node_id] = per
        return LocalRunResult(
            status=engine.status,
            nodes=engine.node_runs(),
            decisions=list(engine.decisions.values()),
            outputs=outputs,
            values={k: y for k, r in outputs.items() if (y := _value(r, values)) is not None},
            events=events,
            error=engine.error,
            node_values=node_values,
        )

    # --------------------------------------------------------------------- helpers
    def _spec(self, workflow: WorkflowSpec | Mapping[str, Any] | str) -> WorkflowSpec:
        if isinstance(workflow, WorkflowSpec):
            return workflow
        if isinstance(workflow, str):
            from mapi_spec.workflow import load_workflow

            return load_workflow(workflow)
        return WorkflowSpec.model_validate(dict(workflow))

    def _sub_ports(self, spec: WorkflowSpec) -> dict[str, dict[str, PortSpec]]:
        out: dict[str, dict[str, PortSpec]] = {}
        for n in spec.nodes:
            if n.impl_id != SUBWORKFLOW_NODE or self.subworkflows is None:
                continue
            ref = n.with_.get("workflow")
            sub = self.subworkflows(WorkflowRef.parse(str(ref))) if isinstance(ref, str) else None
            if sub is not None:
                out[n.id] = {
                    k: PortSpec(type=v.type, required=v.required and v.default is None)
                    for k, v in sub.inputs.items()
                }
        return out

    def _drain(self, engine: WorkflowEngine, events: list[EngineEvent]) -> None:
        for e in engine.drain_events():
            events.append(e)
            if self.on_event is not None:
                self.on_event(e)

    def _lookup(self, key: str) -> CacheHit | None:
        c = self.cache.get(key)
        if c is None:
            return None
        self._values.update(c.values)
        return c.hit

    def _execute(
        self,
        engine: WorkflowEngine,
        action: Action,
        values: dict[str, Value],
        cancel: CancelToken,
        deadline: float,
        spec: WorkflowSpec,
    ) -> None:
        nid = action.node_id
        engine.started_node(nid, run_id=f"local_{nid}_{action.attempt}")
        if action.impl.id == SUBWORKFLOW_NODE:
            self._subworkflow(engine, action, values)
            return
        fn = self.nodes.get(action.impl.id)
        if fn is None:
            engine.failed(
                nid,
                NodeError(
                    code=ErrorCode.NODE_UNKNOWN,
                    message=f"no runtime implementation of {action.impl.id}",
                ),
            )
            return
        inputs: dict[str, list[Value]] = {}
        for port, refs in action.inputs.items():
            inputs[port] = [values[_key(r)] if _key(r) in values else _inline(r) for r in refs]
        token = CancelToken()
        ctx = NodeContext(
            node_id=nid,
            use=action.impl.ref,
            params=dict(action.params),
            inputs=inputs,
            registry=self.registry,
            seed=self.seed,
            cancel=token,
            log=self.log,
            input_refs=dict(action.inputs),
            services={"governance": self.governance},
        )
        timeout = action.node.timeout.seconds if action.node.timeout is not None else None
        budget = max(0.0, deadline - time.monotonic())
        limit = min(timeout, budget) if timeout is not None else budget
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["out"] = fn(ctx)
            except BaseException as exc:
                box["exc"] = exc

        th = threading.Thread(target=work, name=f"mapi-node-{nid}", daemon=True)
        started = time.monotonic()
        th.start()
        while th.is_alive():
            th.join(0.05)
            if cancel.cancelled:
                token.cancel()
            if time.monotonic() - started > limit:
                token.cancel()
                th.join(5.0)
                engine.failed(
                    nid,
                    NodeError(
                        code=ErrorCode.TIMEOUT,
                        message=f"the node did not finish within {limit:.0f}s",
                    ),
                    retryable=False,
                )
                return
        if "exc" in box:
            exc = box["exc"]
            if isinstance(exc, RunCancelled):
                if cancel.cancelled and not engine.cancel_requested:
                    engine.cancel()
                engine.cancelled(nid)
                return
            engine.failed(nid, _node_error(exc))
            return
        outcome: NodeOutcome = box["out"]
        outputs: dict[str, NodeOutputRef] = {}
        produced: dict[str, Value] = {}
        for port, value in outcome.outputs.items():
            want = action.impl.outputs.get(port)
            if want is None:
                continue
            ref = _ref_for(value, want)
            outputs[port] = ref
            produced[_key(ref)] = value
        values.update(produced)
        result = dict(outcome.result)
        if outcome.warnings_plain:
            result.setdefault("warnings_plain", list(outcome.warnings_plain[:50]))
        engine.succeeded(nid, result, outputs)
        node = engine.node_run(nid)
        if action.cache_key is not None and node.status.produced and action.impl.cacheable:
            self.cache[action.cache_key] = _Cached(
                CacheHit(
                    result=dict(node.result or {}),
                    outputs=dict(node.outputs),
                    workflow_run_id="local",
                ),
                dict(produced),
            )

    def _subworkflow(
        self, engine: WorkflowEngine, action: Action, values: dict[str, Value]
    ) -> None:
        nid = action.node_id
        ref_text = action.params.get("workflow")
        sub = None
        if self.subworkflows is not None and isinstance(ref_text, str):
            sub = self.subworkflows(WorkflowRef.parse(ref_text))
        if sub is None:
            engine.failed(
                nid,
                NodeError(
                    code=ErrorCode.REF_UNRESOLVED, message=f"the workflow {ref_text} does not exist"
                ),
            )
            return
        sub_inputs = {
            port: values[_key(refs[0])]
            for port, refs in action.inputs.items()
            if refs and _key(refs[0]) in values
        }
        if self._depth >= MAX_SUBWORKFLOW_DEPTH:
            engine.failed(
                nid,
                NodeError(
                    code=ErrorCode.WORKFLOW_INVALID,
                    message=f"sub-workflows are nested more than {MAX_SUBWORKFLOW_DEPTH} deep "
                    "(does a workflow run itself?)",
                ),
            )
            return
        raw_params = action.params.get("params")
        self._depth += 1
        try:
            res = self.run(
                sub, inputs=sub_inputs, params=raw_params if isinstance(raw_params, dict) else None
            )
        finally:
            self._depth -= 1
        if res.status is not WorkflowRunStatus.SUCCEEDED:
            msg = res.error.message if res.error else res.status.value
            engine.failed(
                nid,
                NodeError(
                    code=ErrorCode.USER_CODE_ERROR,
                    message=f"the sub-workflow {res.status.value}: {msg}",
                ),
            )
            return
        for name, ref in res.outputs.items():
            if name in res.values:
                values[_key(ref)] = res.values[name]
        engine.succeeded(
            nid,
            {"status": res.status.value, "outputs": list(sorted(res.outputs))},
            dict(res.outputs),
        )


def _value(ref: NodeOutputRef, values: Mapping[str, Value]) -> Value | None:
    v = values.get(_key(ref))
    if v is not None:
        return v
    return ref.inline if isinstance(ref.inline, dict) else None


def _key(ref: NodeOutputRef) -> str:
    return ref.ref or input_identity(ref)


def _inline(ref: NodeOutputRef) -> Value:
    if isinstance(ref.inline, dict):
        return ref.inline
    raise MapiError("an input value is missing", code=ErrorCode.INTERNAL_ERROR)


def _node_error(exc: BaseException) -> NodeError:
    if isinstance(exc, MapiError):
        return NodeError(
            code=exc.code,
            message=exc.detail[:2000],
            untrusted_text=exc.untrusted_text[:8000] if exc.untrusted_text else None,
        )
    return NodeError(
        code=ErrorCode.USER_CODE_ERROR,
        message="the node failed",
        untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
    )
