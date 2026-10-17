"""The workflow DAG engine: a pure, JSON-only state machine over a :class:`WorkflowSpec`.

The engine decides *what* runs and *when*; a driver decides *how*. The same engine drives
the control-plane orchestrator (which starts ``workflow_node`` and ``fit`` runs in sandboxes)
and the in-process runner (:mod:`mapi_workflows.runtime.local`, for notebooks, the CLI and
tests). It never touches data: node results and inline outputs are JSON, datasets and
model versions are references.

A driver loop::

    engine = WorkflowEngine(spec, params=..., inputs=...)
    while not engine.finished:
        for action in engine.step():        # nodes the driver must execute now
            ...start it...                   # then, later:
        engine.succeeded(node_id, result, outputs)   # or .failed(...), .cancelled(...)
        events = engine.drain_events()       # persist / stream them

``step`` does everything that needs no execution itself: it skips nodes gated by a branch arm
that was not taken (or whose every input alternative was skipped), evaluates branch
conditions with the safe expression language (:mod:`mapi_spec.expr`, never ``eval``),
computes orchestrator nodes (comparisons, selections, reports) from upstream JSON, resolves
``{$param}``/``{$expr}`` parameters, computes cache keys and reuses earlier results through
the driver's ``cache`` lookup, and hands back at most ``execution.max_parallel`` nodes to run.

Failure semantics: a failing node with ``on_failure: fail`` (the default) fails the run
fast - pending nodes are cancelled and :attr:`WorkflowEngine.to_cancel` lists the running
ones the driver must stop; with ``on_failure: continue`` it counts as skipped for its
dependants. ``retries`` re-queue a failed node (``workflow.node.retrying``) unless the error
is not retryable. :meth:`WorkflowEngine.cancel` cancels the run the same way.

The engine's state is the list of :class:`~mapi_spec.workflow.NodeRun` and
:class:`~mapi_spec.workflow.BranchDecision` records, so a driver that persists them can
rebuild it at any time (:meth:`WorkflowEngine.restore`).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.expr import Ref, parse_expression
from mapi_spec.workflow import (
    NODE_RESULT_MAX_BYTES,
    BranchDecision,
    NodeError,
    NodeOutputRef,
    NodeRun,
    NodeRunStatus,
    WorkflowEventType,
    WorkflowNode,
    WorkflowRunStatus,
    WorkflowSpec,
    node_cache_key,
    node_dependencies,
    topological_order,
)
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    SUBWORKFLOW_NODE,
    NodeImplementation,
    NodeKind,
    PortSpec,
    PortType,
    RunsIn,
)
from pydantic import JsonValue

from mapi_workflows.orchestrator import BoundValue, OrchestratorContext, run_orchestrator_node

__all__ = [
    "MAX_STEP_PASSES",
    "RETRYABLE_CODES",
    "UNKNOWN",
    "Action",
    "CacheHit",
    "CacheLookup",
    "EngineEvent",
    "WorkflowEngine",
    "input_identity",
    "json_size",
]

MAX_STEP_PASSES: Final = 10_000
"""Guard on the number of inline decisions one :meth:`WorkflowEngine.step` may take."""

RETRYABLE_CODES: Final = frozenset(
    {
        ErrorCode.INFRA_ERROR,
        ErrorCode.TIMEOUT,
        ErrorCode.RESOURCE_EXHAUSTED,
        ErrorCode.USER_CODE_ERROR,
        ErrorCode.INTERNAL_ERROR,
    }
)
"""Errors a node's ``retries`` apply to. Parameter, expression and data errors are not
retried: running the same thing again gives the same answer."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def json_size(value: object) -> int:
    """Bytes of the canonical JSON encoding of ``value``."""
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode())


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_identity(ref: NodeOutputRef) -> str:
    """The content identity of a node output, for cache keys: the reference of a dataset or
    model version (immutable), the run object, or the hash of an inline payload."""
    if ref.ref is not None:
        return f"{ref.type.value}:{ref.ref}"
    if ref.run_id is not None and ref.object is not None:
        return f"{ref.type.value}:run:{ref.run_id}/{ref.object}"
    return f"{ref.type.value}:inline:{_digest(ref.inline)}"


@dataclass(frozen=True, slots=True)
class CacheHit:
    """An earlier execution with the same cache key (from the driver's store)."""

    result: dict[str, JsonValue]
    outputs: dict[str, NodeOutputRef]
    workflow_run_id: str | None = None


class _Unknown:
    """The driver has not looked the key up yet (see :data:`UNKNOWN`)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN: Final = _Unknown()
"""What a :data:`CacheLookup` returns for a key it has not looked up yet. The node then
waits, the key is listed by :meth:`WorkflowEngine.take_lookups`, and the driver looks the
keys up (in one database query, say) before the next :meth:`WorkflowEngine.step`."""

CacheLookup = Callable[[str], "CacheHit | _Unknown | None"]


@dataclass(frozen=True, slots=True)
class Action:
    """A node the driver must execute now.

    ``inputs`` maps each bound input port to the values it receives (one for a single
    port, every available source for a ``multiple`` port); ``sources`` gives the node id
    each value came from (``None`` for workflow inputs).
    """

    node_id: str
    impl: NodeImplementation
    node: WorkflowNode
    params: dict[str, JsonValue]
    inputs: dict[str, list[NodeOutputRef]]
    sources: dict[str, list[str | None]]
    cache_key: str | None
    attempt: int
    runs_in: RunsIn


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """One event of the run (persisted and streamed by the driver)."""

    type: WorkflowEventType
    node_id: str | None
    data: dict[str, JsonValue]
    ts: datetime


@dataclass(slots=True)
class _State:
    node_id: str
    impl: NodeImplementation | None
    status: NodeRunStatus = NodeRunStatus.PENDING
    attempt: int = 0
    params: dict[str, JsonValue] = field(default_factory=dict)
    result: dict[str, JsonValue] | None = None
    outputs: dict[str, NodeOutputRef] = field(default_factory=dict)
    cache_key: str | None = None
    cached_from: str | None = None
    skipped_reason: str | None = None
    error: NodeError | None = None
    run_id: str | None = None
    child_runs: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def dto(self) -> NodeRun:
        impl = self.impl
        return NodeRun(
            node_id=self.node_id,
            use=impl.ref if impl is not None else "unknown@0.0.0",
            kind=impl.kind if impl is not None else NodeKind.TEST,
            status=self.status,
            attempt=self.attempt,
            run_id=self.run_id,
            child_runs=list(self.child_runs),
            cache_key=self.cache_key,
            cached_from=self.cached_from,
            params=self.params,
            result=self.result,
            outputs=dict(self.outputs),
            skipped_reason=self.skipped_reason,
            error=self.error,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


# =========================================================================== param helpers


def _resolve_value(
    value: JsonValue, params: Mapping[str, JsonValue], env: Mapping[str, object]
) -> JsonValue:
    """Replace ``{$param: name}`` and ``{$expr: text}`` placeholders (recursively)."""
    if isinstance(value, dict):
        if len(value) == 1:
            key = next(iter(value))
            if key == "$param":
                name = value[key]
                if not isinstance(name, str) or name not in params:
                    raise MapiError(
                        f"workflow parameter {name!r} has no value",
                        code=ErrorCode.WORKFLOW_INVALID,
                    )
                return copy.deepcopy(params[name])
            if key == "$expr":
                text = value[key]
                if not isinstance(text, str) or not text:
                    raise MapiError(
                        "$expr needs a text expression", code=ErrorCode.EXPRESSION_INVALID
                    )
                out = parse_expression(text).evaluate(env)
                return copy.deepcopy(out)
        return {k: _resolve_value(v, params, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, params, env) for v in value]
    return value


def _with_defaults(schema: Mapping[str, Any], params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return params
    out = dict(params)
    for name, sub in props.items():
        if name not in out and isinstance(sub, Mapping) and "default" in sub:
            out[name] = copy.deepcopy(sub["default"])
    return out


def _check_params(impl: NodeImplementation, params: Mapping[str, JsonValue]) -> str | None:
    errors = sorted(
        Draft202012Validator(dict(impl.params_schema)).iter_errors(dict(params)),
        key=lambda e: list(e.path),
    )
    if not errors:
        return None
    e = errors[0]
    where = "/".join(str(p) for p in e.path)
    return f"parameter {where or '(root)'}: {e.message[:300]}"


def _ref_text(ref: Ref) -> str:
    return str(ref)


# =================================================================================== engine


class WorkflowEngine:
    """The DAG state machine of one workflow run (see the module docstring).

    ``inputs`` are the run's workflow inputs (``inputs.<name>``) as references;
    ``input_identities`` override their cache identity (a dataset's content hash rather than
    a ``@latest`` ref). ``run_meta`` is what conditions read as ``run.*``.
    ``subworkflow_ports`` gives the input ports of ``subworkflow.run`` nodes (from the
    referenced revision), which the static catalog cannot know.
    """

    def __init__(
        self,
        spec: WorkflowSpec,
        *,
        params: Mapping[str, JsonValue] | None = None,
        inputs: Mapping[str, NodeOutputRef] | None = None,
        input_identities: Mapping[str, str] | None = None,
        registry: Mapping[str, NodeImplementation] | None = None,
        use_cache: bool = True,
        cache: CacheLookup | None = None,
        salt: str = "",
        run_meta: Mapping[str, JsonValue] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        subworkflow_ports: Mapping[str, Mapping[str, PortSpec]] | None = None,
    ) -> None:
        self.spec = spec
        self.params: dict[str, JsonValue] = dict(params or {})
        self.inputs: dict[str, NodeOutputRef] = dict(inputs or {})
        self.input_identities = dict(input_identities or {})
        self.registry = NODE_IMPLEMENTATIONS if registry is None else registry
        self.use_cache = use_cache
        self.cache = cache
        self.salt = salt
        self.run_meta: dict[str, JsonValue] = dict(run_meta or {})
        self.clock = clock
        self.subworkflow_ports = {k: dict(v) for k, v in (subworkflow_ports or {}).items()}
        self.order = topological_order(spec)
        self.deps = node_dependencies(spec)
        self.nodes = {n.id: n for n in spec.nodes}
        self.states: dict[str, _State] = {
            n.id: _State(node_id=n.id, impl=self.registry.get(n.impl_id)) for n in spec.nodes
        }
        self.decisions: dict[str, BranchDecision] = {}
        self.events: list[EngineEvent] = []
        self.error: NodeError | None = None
        self.cancel_requested = False
        self.to_cancel: set[str] = set()
        self.started = False
        self._lookups: set[str] = set()
        missing = sorted(i for i, s in self.states.items() if s.impl is None)
        if missing:
            raise MapiError(
                f"unknown node implementation(s) for {missing}",
                code=ErrorCode.NODE_UNKNOWN,
            )

    # ------------------------------------------------------------------------ restore
    @classmethod
    def restore(
        cls,
        spec: WorkflowSpec,
        node_runs: Iterable[NodeRun],
        decisions: Iterable[BranchDecision] = (),
        *,
        error: NodeError | None = None,
        cancel_requested: bool = False,
        started: bool = True,
        **kwargs: Any,
    ) -> WorkflowEngine:
        """An engine in the state the persisted records describe (``started=False`` for a
        run that was created but never stepped)."""
        eng = cls(spec, **kwargs)
        eng.started = started
        for nr in node_runs:
            st = eng.states.get(nr.node_id)
            if st is None:
                continue
            st.status = nr.status
            st.attempt = nr.attempt
            st.params = dict(nr.params)
            st.result = dict(nr.result) if nr.result is not None else None
            st.outputs = dict(nr.outputs)
            st.cache_key = nr.cache_key
            st.cached_from = nr.cached_from
            st.skipped_reason = nr.skipped_reason
            st.error = nr.error
            st.run_id = nr.run_id
            st.child_runs = list(nr.child_runs)
            st.started_at = nr.started_at
            st.finished_at = nr.finished_at
        for d in decisions:
            eng.decisions[d.node_id] = d
        eng.error = error
        eng.cancel_requested = cancel_requested
        return eng

    # --------------------------------------------------------------------- read side
    def node_runs(self) -> list[NodeRun]:
        """Every node's record, in execution order."""
        return [self.states[n].dto() for n in self.order]

    def node_run(self, node_id: str) -> NodeRun:
        return self.states[node_id].dto()

    def running(self) -> list[str]:
        return [
            n
            for n in self.order
            if self.states[n].status in (NodeRunStatus.QUEUED, NodeRunStatus.RUNNING)
        ]

    @property
    def finished(self) -> bool:
        return all(s.status.done for s in self.states.values())

    @property
    def status(self) -> WorkflowRunStatus:
        if not self.finished:
            if self.cancel_requested:
                return WorkflowRunStatus.CANCELLING
            return WorkflowRunStatus.RUNNING if self.started else WorkflowRunStatus.QUEUED
        if self.error is not None:
            return WorkflowRunStatus.FAILED
        if self.cancel_requested:
            return WorkflowRunStatus.CANCELLED
        return WorkflowRunStatus.SUCCEEDED

    def outputs(self) -> dict[str, NodeOutputRef]:
        """The workflow's declared outputs that were produced."""
        out: dict[str, NodeOutputRef] = {}
        for name, binding in self.spec.outputs.items():
            node_id, _, port = binding.partition(".")
            st = self.states.get(node_id)
            if st is not None and st.status.produced and port in st.outputs:
                out[name] = st.outputs[port]
        return out

    def take_lookups(self) -> set[str]:
        """Cache keys the lookup answered :data:`UNKNOWN` for since the last call."""
        out, self._lookups = self._lookups, set()
        return out

    def drain_events(self) -> list[EngineEvent]:
        out, self.events = self.events, []
        return out

    def env(self) -> dict[str, object]:
        """What conditions and ``$expr`` read: produced results, params, inputs, run."""
        nodes: dict[str, object] = {
            nid: st.result or {} for nid, st in self.states.items() if st.status.produced
        }
        return {
            "nodes": nodes,
            "params": self.params,
            "inputs": {k: (v.ref or v.object or "") for k, v in self.inputs.items()},
            "run": self.run_meta,
        }

    # ------------------------------------------------------------------------ events
    def _emit(self, type_: WorkflowEventType, node_id: str | None, **data: JsonValue) -> None:
        self.events.append(EngineEvent(type_, node_id, dict(data), self.clock()))

    # -------------------------------------------------------------------------- step
    def step(self) -> list[Action]:
        """Advance everything that needs no execution; return the nodes to start now."""
        if not self.started:
            self.started = True
            self._emit(WorkflowEventType.RUN_STARTED, None, nodes=len(self.order))
        actions: list[Action] = []
        if self.error is not None or self.cancel_requested:
            return actions
        passes = 0
        progress = True
        while progress:
            progress = False
            passes += 1
            if passes > MAX_STEP_PASSES:  # pragma: no cover - a DAG converges much sooner
                break
            for nid in self.order:
                st = self.states[nid]
                if st.status is not NodeRunStatus.PENDING:
                    continue
                if not all(self.states[d].status.done for d in self.deps[nid]):
                    continue
                running = len(self.running())
                outcome = self._advance(nid, can_start=running < self.spec.execution.max_parallel)
                if isinstance(outcome, Action):
                    actions.append(outcome)
                    progress = True
                elif outcome:
                    progress = True
                if self.error is not None:
                    return actions
        return actions

    def _advance(self, nid: str, *, can_start: bool) -> Action | bool:
        """Decide one pending node whose dependencies are done. Returns an action to run,
        ``True`` when the node was settled inline, ``False`` when it must wait."""
        node, st = self.nodes[nid], self.states[nid]
        impl = st.impl
        assert impl is not None
        why = self._gate(node)
        if why is not None:
            self._skip(nid, why)
            return True
        bound = self._bind(node, impl)
        if isinstance(bound, str):
            self._skip(nid, bound)
            return True
        inputs, sources = bound
        try:
            params = self._params(node, impl)
        except MapiError as exc:
            st.attempt += 1
            self._fail(nid, NodeError(code=exc.code, message=exc.detail[:2000]), retry=False)
            return True
        st.params = params
        key: str | None = None
        if impl.cacheable and impl.kind is not NodeKind.BRANCH:
            key = node_cache_key(
                impl.ref,
                params,
                {
                    p: [self._identity(nid, p, i, ref) for i, ref in enumerate(refs)]
                    for p, refs in inputs.items()
                },
                salt=self.salt,
            )
        st.cache_key = key
        if impl.kind is NodeKind.BRANCH:
            st.attempt += 1
            self._branch(nid, params)
            return True
        if impl.runs_in == "orchestrator" and impl.id != SUBWORKFLOW_NODE:
            st.attempt += 1
            st.started_at = self.clock()
            self._orchestrate(nid, impl, params, inputs, sources)
            return True
        if key is not None and node.cache and self.use_cache and self.cache is not None:
            hit = self.cache(key)
            if isinstance(hit, _Unknown):
                self._lookups.add(key)
                return False
            if hit is not None:
                st.attempt += 1
                st.status = NodeRunStatus.CACHED
                st.result = dict(hit.result)
                st.outputs = dict(hit.outputs)
                st.cached_from = hit.workflow_run_id
                st.started_at = st.finished_at = self.clock()
                self._emit(
                    WorkflowEventType.NODE_CACHED,
                    nid,
                    cache_key=key,
                    cached_from=hit.workflow_run_id,
                )
                return True
        if not can_start:
            return False
        st.attempt += 1
        st.status = NodeRunStatus.QUEUED
        st.error = None
        self._emit(WorkflowEventType.NODE_QUEUED, nid, attempt=st.attempt, use=impl.ref)
        return Action(
            node_id=nid,
            impl=impl,
            node=node,
            params=params,
            inputs=inputs,
            sources=sources,
            cache_key=key,
            attempt=st.attempt,
            runs_in=impl.runs_in,
        )

    def _identity(self, nid: str, port: str, index: int, ref: NodeOutputRef) -> str:
        node = self.nodes[nid]
        alts = node.alternatives(port)
        for b in alts:
            if b.startswith("inputs."):
                name = b.removeprefix("inputs.")
                if self.inputs.get(name) == ref and name in self.input_identities:
                    return self.input_identities[name]
        return input_identity(ref)

    def _gate(self, node: WorkflowNode) -> str | None:
        """Why a ``when``-gated node does not run (``None``: it may run)."""
        if node.when is None:
            return None
        branch_id, _, arm = node.when.partition(".")
        bst = self.states.get(branch_id)
        if bst is None or not bst.status.produced:
            return f"its branch {branch_id!r} did not run"
        decision = self.decisions.get(branch_id)
        taken = decision.arm if decision is not None else (bst.result or {}).get("arm")
        if taken != arm:
            return f"branch {branch_id!r} took the {taken} path"
        return None

    def _available(self, binding: str) -> tuple[NodeOutputRef, str | None] | None:
        if binding.startswith("inputs."):
            name = binding.removeprefix("inputs.")
            ref = self.inputs.get(name)
            return (ref, None) if ref is not None else None
        src, _, port = binding.partition(".")
        st = self.states.get(src)
        if st is None or not st.status.produced:
            return None
        out = st.outputs.get(port)
        return (out, src) if out is not None else None

    def _ports(self, node: WorkflowNode, impl: NodeImplementation) -> dict[str, PortSpec]:
        if impl.id == SUBWORKFLOW_NODE:
            ports = self.subworkflow_ports.get(node.id)
            if ports is not None:
                return dict(ports)
            return {p: PortSpec(type=PortType.DATASET, required=False) for p in node.inputs}
        return dict(impl.inputs)

    def _bind(
        self, node: WorkflowNode, impl: NodeImplementation
    ) -> tuple[dict[str, list[NodeOutputRef]], dict[str, list[str | None]]] | str:
        """The values bound to each input port, or why the node cannot run."""
        inputs: dict[str, list[NodeOutputRef]] = {}
        sources: dict[str, list[str | None]] = {}
        for port, spec in self._ports(node, impl).items():
            alts = node.alternatives(port)
            found: list[tuple[NodeOutputRef, str | None]] = []
            for b in alts:
                hit = self._available(b)
                if hit is None:
                    continue
                found.append(hit)
                if not spec.multiple:
                    break
            if not found:
                if spec.required:
                    if not alts:
                        return f"input {port!r} is not bound"
                    return f"no value reached input {port!r} (every source was skipped)"
                continue
            inputs[port] = [f[0] for f in found]
            sources[port] = [f[1] for f in found]
        return inputs, sources

    def _params(self, node: WorkflowNode, impl: NodeImplementation) -> dict[str, JsonValue]:
        env = self.env()
        resolved = _resolve_value(dict(node.with_), self.params, env)
        assert isinstance(resolved, dict)
        if impl.kind is NodeKind.BRANCH:
            return resolved
        params = _with_defaults(impl.params_schema, resolved)
        problem = _check_params(impl, params)
        if problem is not None:
            raise MapiError(f"node {node.id!r}: {problem}", code=ErrorCode.WORKFLOW_INVALID)
        return params

    # ----------------------------------------------------------------- inline work
    def _branch(self, nid: str, params: Mapping[str, JsonValue]) -> None:
        st = self.states[nid]
        text = params.get("condition")
        if not isinstance(text, str):
            err = NodeError(code=ErrorCode.WORKFLOW_INVALID, message="a branch needs a condition")
            self._fail(nid, err, retry=False)
            return
        env = self.env()
        try:
            ex = parse_expression(text)
            value = ex.evaluate_bool(env)
        except MapiError as exc:
            self._fail(nid, NodeError(code=exc.code, message=exc.detail[:2000]), retry=False)
            return
        seen: dict[str, JsonValue] = {}
        for ref in ex.references():
            key = _ref_text(ref)
            if key in seen:
                continue
            try:
                v = parse_expression(key).evaluate(env)
            except MapiError:
                v = None
            seen[key] = v
        arm: Literal["then", "else"] = "then" if value else "else"
        explain = params.get(f"explain_{arm}")
        plain = explain.strip()[:300] if isinstance(explain, str) and explain.strip() else None
        now = self.clock()
        decision = BranchDecision(
            node_id=nid,
            condition=text,
            value=value,
            arm=arm,
            inputs=seen,
            explain_plain=plain,
            decided_at=now,
        )
        self.decisions[nid] = decision
        st.status = NodeRunStatus.SUCCEEDED
        st.result = {"value": value, "arm": arm}
        st.started_at = st.finished_at = now
        self._emit(WorkflowEventType.BRANCH_DECIDED, nid, arm=arm, value=value, inputs=seen)
        self._emit(WorkflowEventType.NODE_SUCCEEDED, nid, arm=arm)

    def _impl_id(self, nid: str | None) -> str | None:
        impl = self.states[nid].impl if nid is not None else None
        return impl.id if impl is not None else None

    def _orchestrate(
        self,
        nid: str,
        impl: NodeImplementation,
        params: dict[str, JsonValue],
        inputs: dict[str, list[NodeOutputRef]],
        sources: dict[str, list[str | None]],
    ) -> None:
        bound = {
            port: [
                BoundValue(
                    ref=ref,
                    source=src,
                    source_params=self.states[src].params if src else {},
                    source_result=(self.states[src].result or {}) if src else {},
                    source_use=self._impl_id(src),
                )
                for ref, src in zip(refs, sources[port], strict=True)
            ]
            for port, refs in inputs.items()
        }
        ctx = OrchestratorContext(
            node_id=nid,
            node=self.nodes[nid],
            node_runs=[self.states[n].dto() for n in self.order],
            decisions=list(self.decisions.values()),
            nodes={n.id: n for n in self.spec.nodes},
            registry=self.registry,
            title=self.spec.metadata.name,
        )
        try:
            result, outputs = run_orchestrator_node(impl.id, params, bound, ctx)
        except MapiError as exc:
            self._fail(nid, NodeError(code=exc.code, message=exc.detail[:2000]), retry=False)
            return
        self.succeeded(nid, result, outputs)

    # ----------------------------------------------------------------- driver feedback
    def started_node(self, node_id: str, *, run_id: str | None = None) -> None:
        """The driver started executing ``node_id`` (in run ``run_id``)."""
        st = self.states[node_id]
        if st.status is not NodeRunStatus.QUEUED:
            return
        st.status = NodeRunStatus.RUNNING
        st.started_at = self.clock()
        if run_id is not None:
            st.run_id = run_id
        self._emit(WorkflowEventType.NODE_STARTED, node_id, attempt=st.attempt, run_id=run_id)

    def attach_run(self, node_id: str, run_id: str) -> None:
        """A queued node was handed to run ``run_id`` (it is ``running`` once the driver
        calls :meth:`started_node`)."""
        st = self.states[node_id]
        st.run_id = run_id
        if run_id not in st.child_runs:
            st.child_runs.append(run_id)

    def add_child(self, node_id: str, run_id: str) -> None:
        st = self.states[node_id]
        if run_id not in st.child_runs:
            st.child_runs.append(run_id)

    def succeeded(
        self,
        node_id: str,
        result: Mapping[str, JsonValue] | None,
        outputs: Mapping[str, NodeOutputRef] | None = None,
    ) -> None:
        """``node_id`` finished: its JSON result and its outputs (port → reference)."""
        st = self.states[node_id]
        if st.status.done:
            return
        impl = st.impl
        assert impl is not None
        result = dict(result or {})
        outputs = dict(outputs or {})
        if json_size(result) > NODE_RESULT_MAX_BYTES:
            self._fail(
                node_id,
                NodeError(
                    code=ErrorCode.PAYLOAD_TOO_LARGE,
                    message=f"the node's result exceeds {NODE_RESULT_MAX_BYTES} bytes",
                ),
                retry=False,
            )
            return
        declared = self._declared_outputs(node_id, impl)
        for port, ref in outputs.items():
            want = declared.get(port)
            if want is not None and ref.type is not want:
                self._fail(
                    node_id,
                    NodeError(
                        code=ErrorCode.INTEGRITY_FAILURE,
                        message=f"output {port!r} is a {ref.type.value}, not a {want.value}",
                    ),
                    retry=False,
                )
                return
            if want is None and impl.id != SUBWORKFLOW_NODE:
                self._fail(
                    node_id,
                    NodeError(
                        code=ErrorCode.INTEGRITY_FAILURE,
                        message=f"{impl.id} has no output {port!r}",
                    ),
                    retry=False,
                )
                return
        if st.started_at is None:
            st.started_at = self.clock()
        st.status = NodeRunStatus.SUCCEEDED
        st.result = result
        st.outputs = outputs
        st.error = None
        st.finished_at = self.clock()
        ports: list[JsonValue] = list(sorted(outputs))
        self._emit(WorkflowEventType.NODE_SUCCEEDED, node_id, attempt=st.attempt, outputs=ports)

    def _declared_outputs(self, node_id: str, impl: NodeImplementation) -> dict[str, PortType]:
        return dict(impl.outputs)

    def failed(self, node_id: str, error: NodeError, *, retryable: bool | None = None) -> None:
        """``node_id`` failed. It is retried when it has retries left and the error is
        retryable (by default: :data:`RETRYABLE_CODES`)."""
        st = self.states[node_id]
        if st.status.done:
            return
        retry = error.code in RETRYABLE_CODES if retryable is None else retryable
        self._fail(node_id, error, retry=retry)

    def cancelled(self, node_id: str) -> None:
        """The driver confirms that a running node was stopped."""
        st = self.states[node_id]
        self.to_cancel.discard(node_id)
        if st.status.done:
            return
        st.status = NodeRunStatus.CANCELLED
        st.finished_at = self.clock()
        st.skipped_reason = st.skipped_reason or "the run was cancelled"

    def cancel(self) -> None:
        """Cancel the run: pending nodes are cancelled now; running ones are listed in
        :attr:`to_cancel` for the driver to stop (then :meth:`cancelled`)."""
        if self.finished:
            return
        self.cancel_requested = True
        self._stop_all("the run was cancelled")

    def fail_run(self, error: NodeError) -> None:
        """Fail the whole run (a run-level timeout or an infrastructure problem)."""
        if self.error is None:
            self.error = error
        self._stop_all(error.message)

    def _stop_all(self, reason: str) -> None:
        now = self.clock()
        for nid in self.order:
            st = self.states[nid]
            unstarted = st.status is NodeRunStatus.QUEUED and st.run_id is None
            if st.status is NodeRunStatus.PENDING or unstarted:
                st.status = NodeRunStatus.CANCELLED
                st.skipped_reason = reason[:500]
                st.finished_at = now
            elif st.status in (NodeRunStatus.QUEUED, NodeRunStatus.RUNNING):
                self.to_cancel.add(nid)

    def _skip(self, nid: str, reason: str) -> None:
        st = self.states[nid]
        st.status = NodeRunStatus.SKIPPED
        st.skipped_reason = reason[:500]
        st.started_at = st.finished_at = self.clock()
        self._emit(WorkflowEventType.NODE_SKIPPED, nid, reason=reason[:500])

    def _fail(self, nid: str, error: NodeError, *, retry: bool) -> None:
        st = self.states[nid]
        node = self.nodes[nid]
        if retry and st.attempt <= node.retries and not self.cancel_requested:
            st.status = NodeRunStatus.PENDING
            st.error = error
            st.run_id = None
            self._emit(
                WorkflowEventType.NODE_RETRYING,
                nid,
                attempt=st.attempt,
                code=error.code.value,
                message=error.message[:500],
            )
            return
        st.status = NodeRunStatus.FAILED
        st.error = error
        st.finished_at = self.clock()
        if st.started_at is None:
            st.started_at = st.finished_at
        self._emit(
            WorkflowEventType.NODE_FAILED, nid, code=error.code.value, message=error.message[:500]
        )
        if node.on_failure == "fail":
            if self.error is None:
                self.error = NodeError(
                    code=error.code,
                    message=f"node {nid!r} failed: {error.message}"[:2000],
                    untrusted_text=error.untrusted_text,
                )
            self._stop_all(f"node {nid!r} failed")

    # -------------------------------------------------------------------- finishing
    def finish_events(self) -> None:
        """Emit the terminal run event once every node is done (idempotent per call site)."""
        if not self.finished:
            return
        status = self.status
        kind = {
            WorkflowRunStatus.SUCCEEDED: WorkflowEventType.RUN_SUCCEEDED,
            WorkflowRunStatus.FAILED: WorkflowEventType.RUN_FAILED,
            WorkflowRunStatus.CANCELLED: WorkflowEventType.RUN_CANCELLED,
        }[status]
        data: dict[str, JsonValue] = {"status": status.value}
        if self.error is not None:
            data["code"] = self.error.code.value
            data["message"] = self.error.message[:500]
        self._emit(kind, None, **data)


def bound_refs(values: Sequence[NodeOutputRef]) -> list[str]:
    """Plain references of bound values (for logs)."""
    return [v.ref or v.object or "inline" for v in values]
