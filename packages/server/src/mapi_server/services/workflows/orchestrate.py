"""The control-plane driver of workflow runs: walk the DAG, start children, collect results.

:class:`WorkflowOrchestrator` drives :class:`mapi_workflows.engine.WorkflowEngine` (pure,
JSON only) over the persisted state of a run. Every :meth:`~WorkflowOrchestrator.tick`
(level-triggered, from the orchestrator's workflow loop, under ``FOR UPDATE SKIP LOCKED`` so
several orchestrators never double-start a node):

1. rebuilds the engine from the ``workflow_node_runs`` rows;
2. collects finished children: a ``workflow_node`` run's verified ``node_result.json``
   (derived datasets are registered as versions of a ``wf-<workflow>-<node>`` dataset), a
   ``fit`` run's model version, a sub-workflow's outputs;
3. applies cancellation (children are cancelled through the run state machine) and the
   run's deadline;
4. steps the engine - branches, comparisons, selections and reports are computed here from
   JSON - looks cache keys up in one query per pass, and starts the nodes it hands back;
5. persists node rows, branch decisions and events, and mirrors the status onto the parent
   ``runs`` row; a finished run emits ``workflow.run.succeeded``/``failed`` webhooks.

**Where nodes run.** User data is only decoded in sandboxes. ``model.fit`` is a ``fit`` child
run of a stored spec ``wf-<workflow>-<node>`` (its version is an ordinary model version: it
can be scored, explored, validated and promoted); ``search.grid`` runs its backtests in a
``workflow_node`` run and then fits the winner as a ``fit`` child; ``subworkflow.run``
starts a child workflow run; ``report.evidence_pack`` is built here from the evidence and the
model version's lineage; every other node is a ``workflow_node`` child run with its own
resource class and timeout, in which statsmodels fits (backtests, forecasts) run in process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, MapiError, Problem, ValidationFailed
from mapi_spec.executor import RunKind
from mapi_spec.governance import GovernancePolicy
from mapi_spec.jobs import BUNDLE_MANIFEST_OUTPUT, FIT_BUNDLE, INPUT_DATASET, side_table_input
from mapi_spec.loader import canonical_json, spec_hash
from mapi_spec.refs import DatasetRef, ModelVersionRef, WorkflowRef
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.schema import DataSchema
from mapi_spec.spec import DEFAULT_FIT_SEED
from mapi_spec.storage import ObjectMeta, PreconditionFailed, dataset_prefix
from mapi_spec.webhooks import WebhookEventType
from mapi_spec.workflow import (
    NODE_RESULT_MAX_BYTES,
    NODE_RESULT_OUTPUT,
    WORKFLOW_RUN_INPUT_PREFIX,
    BranchDecision,
    NodeError,
    NodeOutputRef,
    NodeResultSidecar,
    NodeRunStatus,
    WorkflowNodeJob,
    WorkflowRunCreate,
    WorkflowRunStatus,
    WorkflowSpec,
    WorkflowTriggerEvent,
)
from mapi_spec.workflow_nodes import SUBWORKFLOW_NODE, PortSpec, PortType
from mapi_workflows.binding import auto_bind, model_input_schema
from mapi_workflows.engine import UNKNOWN, Action, CacheHit, WorkflowEngine
from mapi_workflows.orchestrator import BoundValue, evidence_pack
from mapi_workflows.payloads import MetricsCandidate, MetricsPayload, clean_number
from mapi_workflows.specs import MODEL_LABEL, model_spec_document, node_model_name
from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.bundles import verified_bundle_object
from mapi_server.services.finalizer import dataset_sha
from mapi_server.services.runs import Scope, apply_transition, create_run, now
from mapi_server.services.runspec import StoredRef
from mapi_server.services.specs import (
    first_error,
    pin_document,
    resolve_dataset,
    resolve_model_version,
    validate_document,
)
from mapi_server.services.submit import admit, data_ref, fit_dataset_version
from mapi_server.services.webhooks.outbox import emit_event
from mapi_server.services.workflows.events import append_events
from mapi_server.services.workflows.hooks import governance_hooks
from mapi_server.services.workflows.runs import (
    ACTIVE_STATUSES,
    node_run_dto,
    request_cancel,
    set_parent_state,
    start_workflow_run,
)
from mapi_server.services.workflows.store import resolve_workflow_ref, workflow_ref

__all__ = ["WorkflowOrchestrator"]

log = logging.getLogger(__name__)

_PHASE_NODE: Final = "node"
_PHASE_FIT: Final = "fit"
_PHASE_FIT_BEST: Final = "fit_best"
_PHASE_SUB: Final = "subworkflow"
_LIVE = frozenset({RunState.PROVISIONING, RunState.RUNNING, RunState.STALLED, RunState.FINALIZING})


class _NodeFailure(Exception):
    """A node cannot be started or collected (a plain message, not a crash)."""

    def __init__(self, code: ErrorCode, message: str, untrusted: str | None = None) -> None:
        super().__init__(message)
        self.error = NodeError(
            code=code,
            message=message[:2000],
            untrusted_text=untrusted[:8000] if untrusted else None,
        )


@dataclass
class _Env:
    session: AsyncSession
    scope: Scope
    wr: m.WorkflowRunRow
    run: m.Run
    wf: m.Workflow
    spec: WorkflowSpec
    rows: dict[str, m.WorkflowNodeRunRow]
    engine: WorkflowEngine
    extra_results: dict[str, dict[str, JsonValue]] = field(default_factory=dict)


def _input_name(port: str, k: int, suffix: str = "") -> str:
    return f"{WORKFLOW_RUN_INPUT_PREFIX}{port}.{k}{suffix}"


def _settings_of(params: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
    raw = params.get("settings")
    return dict(raw) if isinstance(raw, dict) else None


class WorkflowOrchestrator:
    """Advances active workflow runs (see the module docstring)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    # ---------------------------------------------------------------------------- loop
    async def tick_all(self, *, limit: int = 200) -> int:
        """Advance every active run once; returns how many were looked at."""
        async with self.ctx.db.session() as s:
            ids = list(
                (
                    await s.execute(
                        select(m.WorkflowRunRow.run_id)
                        .where(m.WorkflowRunRow.status.in_(ACTIVE_STATUSES))
                        .order_by(m.WorkflowRunRow.updated_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        for run_id in ids:
            try:
                await self.tick(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("advancing workflow run %s failed", run_id)
        return len(ids)

    async def run_until_idle(self, *, max_ticks: int = 200) -> None:
        """Tick until no run changes (tests and the inline queue)."""
        for _ in range(max_ticks):
            if await self.tick_all() == 0:
                return

    # ---------------------------------------------------------------------------- tick
    async def tick(self, run_id: str) -> WorkflowRunStatus | None:
        """Advance one run (a no-op when another orchestrator holds it)."""
        async with self.ctx.db.session() as s:
            wr = (
                await s.execute(
                    select(m.WorkflowRunRow)
                    .where(m.WorkflowRunRow.run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if wr is None or WorkflowRunStatus(wr.status).terminal:
                return None
            run = (
                await s.execute(select(m.Run).where(m.Run.id == run_id).with_for_update())
            ).scalar_one()
            wf = await s.get(m.Workflow, wr.workflow_id)
            rev = await s.get(m.WorkflowRevisionRow, (wr.workflow_id, wr.revision))
            assert wf is not None and rev is not None
            spec = WorkflowSpec.model_validate(rev.document)
            rows = {
                r.node_id: r
                for r in (
                    await s.execute(
                        select(m.WorkflowNodeRunRow).where(m.WorkflowNodeRunRow.run_id == run_id)
                    )
                ).scalars()
            }
            scope = Scope(wr.tenant_id, wr.project_id)
            known: dict[str, CacheHit | None] = {}
            use_cache = wr.use_cache

            def cache(key: str) -> Any:
                if not use_cache:
                    return None
                return known.get(key, UNKNOWN)

            engine = WorkflowEngine.restore(
                spec,
                [node_run_dto(r) for r in rows.values()],
                [BranchDecision.model_validate(d) for d in wr.decisions],
                error=NodeError.model_validate(wr.error) if wr.error else None,
                cancel_requested=False,  # re-applied below (it lists the children to stop)
                started=wr.status != WorkflowRunStatus.QUEUED.value,
                params=wr.params,
                inputs={k: NodeOutputRef.model_validate(v) for k, v in wr.input_refs.items()},
                input_identities=wr.input_identities,
                use_cache=use_cache,
                cache=cache,
                salt=self.ctx.catalogs.image_digest,
                run_meta={"attempt": run.attempt, "trigger": wr.trigger, "id": run_id},
                subworkflow_ports=await self._sub_ports(s, scope, spec),
            )
            env = _Env(s, scope, wr, run, wf, spec, rows, engine)
            if run.state == RunState.CANCELLING.value and not wr.cancel_requested:
                wr.cancel_requested = True
            if wr.cancel_requested:
                engine.cancel()
            for row in list(rows.values()):
                if row.child_run_id and row.status in (
                    NodeRunStatus.QUEUED.value,
                    NodeRunStatus.RUNNING.value,
                ):
                    await self._collect(env, row)
            if wr.deadline is not None and now() > wr.deadline and not engine.finished:
                engine.fail_run(
                    NodeError(
                        code=ErrorCode.TIMEOUT, message="the workflow run exceeded its timeout"
                    )
                )
            for nid in sorted(engine.to_cancel):
                await self._cancel_child(env, nid)
            if not engine.cancel_requested and engine.error is None:
                for _ in range(50):
                    actions = engine.step()
                    for action in actions:
                        await self._start(env, action)
                    keys = engine.take_lookups()
                    if keys:
                        known.update(await self._lookup(env, keys))
                        continue
                    if not actions:
                        break
            if engine.finished:
                engine.finish_events()
            await self._persist(env)
            return WorkflowRunStatus(wr.status)

    # -------------------------------------------------------------------------- helpers
    async def _sub_ports(
        self, s: AsyncSession, scope: Scope, spec: WorkflowSpec
    ) -> dict[str, dict[str, PortSpec]]:
        out: dict[str, dict[str, PortSpec]] = {}
        for n in spec.nodes:
            if n.impl_id != SUBWORKFLOW_NODE:
                continue
            ref_text = n.with_.get("workflow")
            if not isinstance(ref_text, str):
                continue
            with contextlib.suppress(ValueError):
                sub = await resolve_workflow_ref(s, scope, WorkflowRef.parse(ref_text))
                if sub is not None:
                    out[n.id] = {
                        k: PortSpec(type=v.type, required=v.required and v.default is None)
                        for k, v in sub.inputs.items()
                    }
        return out

    async def _lookup(self, env: _Env, keys: set[str]) -> dict[str, CacheHit | None]:
        rows = list(
            (
                await env.session.execute(
                    select(m.WorkflowNodeRunRow)
                    .where(
                        m.WorkflowNodeRunRow.tenant_id == env.scope.tenant_id,
                        m.WorkflowNodeRunRow.project_id == env.scope.project_id,
                        m.WorkflowNodeRunRow.cache_key.in_(sorted(keys)),
                        m.WorkflowNodeRunRow.status.in_(
                            [NodeRunStatus.SUCCEEDED.value, NodeRunStatus.CACHED.value]
                        ),
                        m.WorkflowNodeRunRow.run_id != env.wr.run_id,
                    )
                    .order_by(m.WorkflowNodeRunRow.updated_at.desc())
                )
            ).scalars()
        )
        out: dict[str, CacheHit | None] = {k: None for k in keys}
        for r in rows:
            if r.cache_key is None or out.get(r.cache_key) is not None:
                continue
            try:
                outputs = {k: NodeOutputRef.model_validate(v) for k, v in r.outputs.items()}
            except ValidationError:
                continue
            out[r.cache_key] = CacheHit(
                result=dict(r.result or {}),
                outputs=outputs,
                workflow_run_id=r.cached_from or r.run_id,
            )
        return out

    async def _persist(self, env: _Env) -> None:
        engine, wr, s = env.engine, env.wr, env.session
        ts = now()
        for nr in engine.node_runs():
            row = env.rows[nr.node_id]
            result = dict(nr.result) if nr.result is not None else None
            extra = env.extra_results.get(nr.node_id)
            if extra and result is not None:
                result.update(extra)
            elif extra and nr.status in (NodeRunStatus.QUEUED, NodeRunStatus.RUNNING):
                result = {**(row.result or {}), **extra}
            elif (
                result is None
                and row.result
                and nr.status
                in (
                    NodeRunStatus.QUEUED,
                    NodeRunStatus.RUNNING,
                )
            ):
                result = row.result  # phase data of a two-phase node
            row.status = nr.status.value
            row.attempt = nr.attempt
            row.child_run_id = nr.run_id
            row.child_runs = list(nr.child_runs)
            row.cache_key = nr.cache_key
            row.cached_from = nr.cached_from
            row.params = json.loads(json.dumps(nr.params, default=str))
            row.result = json.loads(json.dumps(result, default=str)) if result is not None else None
            row.outputs = {
                k: v.model_dump(mode="json", exclude_none=True) for k, v in nr.outputs.items()
            }
            row.skipped_reason = nr.skipped_reason
            row.error = nr.error.model_dump(mode="json", exclude_none=True) if nr.error else None
            row.started_at = nr.started_at
            row.finished_at = nr.finished_at
            row.updated_at = ts
        wr.decisions = [d.model_dump(mode="json") for d in engine.decisions.values()]
        await append_events(s, wr, engine.drain_events())
        status = engine.status
        if wr.started_at is None and status is not WorkflowRunStatus.QUEUED:
            wr.started_at = ts
        previous = WorkflowRunStatus(wr.status)
        wr.status = status.value
        wr.error = engine.error.model_dump(mode="json", exclude_none=True) if engine.error else None
        wr.outputs = {
            k: v.model_dump(mode="json", exclude_none=True) for k, v in engine.outputs().items()
        }
        wr.updated_at = ts
        set_parent_state(env.run, status, engine.error)
        if status.terminal and not previous.terminal:
            wr.finished_at = ts
            await self._finished(env, status)
        await s.flush()
        from mapi_server.services.notify import notify_run

        await notify_run(s, wr.run_id, "run")

    async def _finished(self, env: _Env, status: WorkflowRunStatus) -> None:
        wr = env.wr
        if status is WorkflowRunStatus.CANCELLED:
            return
        event = (
            WebhookEventType.WORKFLOW_RUN_SUCCEEDED
            if status is WorkflowRunStatus.SUCCEEDED
            else WebhookEventType.WORKFLOW_RUN_FAILED
        )
        data: dict[str, JsonValue] = {
            "run_id": wr.run_id,
            "workflow_id": wr.workflow_id,
            "workflow": workflow_ref(env.wf, wr.revision),
            "revision": wr.revision,
            "status": status.value,
            "trigger": wr.trigger,
            "outputs": {k: v.get("ref") or v.get("object") for k, v in wr.outputs.items()},
        }
        if wr.error:
            data["code"] = wr.error.get("code")
            data["message"] = str(wr.error.get("message", ""))[:500]
        await emit_event(
            env.session,
            tenant_id=wr.tenant_id,
            project_id=wr.project_id,
            event_type=event,
            data=data,
        )

    # --------------------------------------------------------------------------- start
    async def _start(self, env: _Env, action: Action) -> None:
        nid = action.node_id
        row = env.rows[nid]
        row.result = None
        try:
            if action.impl.id == "report.evidence_pack":
                await self._evidence_pack(env, action)
                return
            if action.impl.id == SUBWORKFLOW_NODE:
                child = await self._submit_sub(env, action)
                row.phase = _PHASE_SUB
            elif action.impl.id == "model.fit":
                child = await self._submit_fit(env, action)
                row.phase = _PHASE_FIT
            else:
                child = await self._submit_node(env, action)
                row.phase = _PHASE_NODE
        except _NodeFailure as exc:
            env.engine.failed(nid, exc.error, retryable=False)
            return
        except ValidationFailed as exc:
            issue = first_error(exc.issues) or (exc.issues[0] if exc.issues else None)
            msg = issue.message if issue else exc.detail
            env.engine.failed(
                nid,
                NodeError(
                    code=issue.code if issue else exc.code,
                    message=f"the node cannot start: {msg}"[:2000],
                ),
                retryable=False,
            )
            return
        except MapiError as exc:
            env.engine.failed(
                nid, NodeError(code=exc.code, message=exc.detail[:2000]), retryable=False
            )
            return
        env.engine.attach_run(nid, child)

    def _resource_class(self, env: _Env, action: Action) -> ResourceClass:
        if action.node.resource_class is not None:
            return action.node.resource_class
        if env.wr.resource_class:
            return ResourceClass(env.wr.resource_class)
        return env.spec.execution.resource_class

    def _timeout(self, action: Action) -> int:
        if action.node.timeout is not None:
            return max(60, action.node.timeout.seconds)
        return self.ctx.settings.workflow_node_timeout_s

    async def _accept(self, env: _Env, run: m.Run) -> None:
        run.parent_run_id = env.wr.run_id
        run.token_id = env.run.token_id
        run.created_by = env.run.created_by
        await admit(env.session, self.ctx, run, audit_action=None)
        await apply_transition(env.session, run, RunTrigger.ACCEPT, Actor.API)

    async def _dataset(self, env: _Env, ref: str) -> m.DatasetVersion:
        try:
            dref = DatasetRef.parse(ref)
        except ValueError as exc:
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, f"{ref!r}: {exc}") from None
        dv, why = await resolve_dataset(env.session, env.scope, dref)
        if dv is None:
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, why or f"cannot resolve {ref}")
        return dv

    async def _model(self, env: _Env, ref: str) -> m.ModelVersion:
        try:
            mref = ModelVersionRef.parse(ref)
        except ValueError as exc:
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, f"{ref!r}: {exc}") from None
        mv, why = await resolve_model_version(env.session, env.scope, mref)
        if mv is None or mv.status != "ready":
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, why or f"model {ref} is not ready")
        return mv

    async def _json_object(self, env: _Env, ref: NodeOutputRef) -> dict[str, Any]:
        """A large JSON output another node run wrote (``run_id`` + ``object``)."""
        assert ref.run_id is not None and ref.object is not None
        child = await env.session.get(m.Run, ref.run_id)
        att = await env.session.get(m.RunAttempt, (ref.run_id, child.attempt)) if child else None
        meta = (att.outputs or {}).get(ref.object) if att else None
        if not isinstance(meta, dict):
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, f"output {ref.object} is gone")
        om = ObjectMeta.model_validate(meta)
        cap = self.ctx.settings.workflow_max_json_output_bytes
        if om.size > cap:
            raise _NodeFailure(ErrorCode.PAYLOAD_TOO_LARGE, f"{ref.object} is too large")
        raw = await self.ctx.store.get(om.bucket, om.name, generation=om.generation, max_bytes=cap)
        try:
            doc = json.loads(raw)
        except ValueError:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, f"{ref.object} is not JSON") from None
        if not isinstance(doc, dict):
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, f"{ref.object} is not an object")
        return doc

    async def _submit_node(self, env: _Env, action: Action) -> str:
        """A ``workflow_node`` child run for a node that needs data."""
        s = env.session
        inputs: dict[str, Any] = {}
        input_types: dict[str, PortType] = {}
        inline: dict[str, JsonValue] = {}
        refs: dict[str, JsonValue] = {}
        models: list[m.ModelVersion] = []
        for port, values in action.inputs.items():
            for k, ref in enumerate(values):
                name = _input_name(port, k)
                if ref.type is PortType.DATASET:
                    if ref.ref is None:
                        raise _NodeFailure(ErrorCode.INTERNAL_ERROR, f"{port}: no dataset ref")
                    dv = await self._dataset(env, ref.ref)
                    inputs[name] = data_ref(dv)
                    input_types[name] = PortType.DATASET
                    refs[name] = ref.ref
                elif ref.type is PortType.MODEL_VERSION:
                    if ref.ref is None:
                        raise _NodeFailure(ErrorCode.INTERNAL_ERROR, f"{port}: no model ref")
                    mv = await self._model(env, ref.ref)
                    models.append(mv)
                    bundle = await verified_bundle_object(
                        self.ctx.store, self.ctx.signer, mv, FIT_BUNDLE
                    )
                    inputs[name] = StoredRef(bundle.bucket, bundle.key, bundle.generation)
                    if BUNDLE_MANIFEST_OUTPUT in (mv.manifest.get("objects") or {}):
                        man = await verified_bundle_object(
                            self.ctx.store, self.ctx.signer, mv, BUNDLE_MANIFEST_OUTPUT
                        )
                        inputs[name + ".manifest"] = StoredRef(man.bucket, man.key, man.generation)
                    with contextlib.suppress(MapiError):
                        inputs[name + ".data"] = data_ref(await fit_dataset_version(s, mv))
                    input_types[name] = PortType.MODEL_VERSION
                    refs[name] = ref.ref
                elif isinstance(ref.inline, dict):
                    inline[name] = ref.inline
                    input_types[name] = ref.type
                elif ref.run_id is not None and ref.object is not None:
                    inline[name] = await self._json_object(env, ref)
                    input_types[name] = ref.type
                else:
                    raise _NodeFailure(ErrorCode.INTERNAL_ERROR, f"input {port!r} has no value")
        params: dict[str, JsonValue] = dict(action.params)
        if refs:
            params["_refs"] = refs
        if action.impl.id == "governance.policy":
            params["_policies"] = await self._policies(env, action, models)
        job = WorkflowNodeJob(
            workflow_run_id=env.wr.run_id,
            node_id=action.node_id,
            use=action.impl.ref,
            params=params,
            input_types=input_types,
            inline_inputs=inline,
            seed=int(env.wr.seed) % (2**32),
        )
        payload = job.model_dump(mode="json")
        snapshot = await self.ctx.catalogs.get(s)
        run = await create_run(
            s,
            env.scope,
            kind=RunKind.WORKFLOW_NODE,
            resource_class=self._resource_class(env, action),
            timeout_s=self._timeout(action),
            payload=payload,
            inputs=inputs,
            labels={"workflow": env.wf.name[:63], "workflow-node": action.node_id},
            spec_hash=spec_hash(payload),
            catalog_digest=snapshot.digest if snapshot else None,
        )
        await self._accept(env, run)
        return run.id

    async def _policies(
        self, env: _Env, action: Action, models: list[m.ModelVersion]
    ) -> list[JsonValue]:
        hooks = governance_hooks()
        policy_id = action.params.get("policy")
        pid = policy_id if isinstance(policy_id, str) and policy_id else None
        if hooks.resolver is None or not models:
            if pid is not None:
                raise _NodeFailure(
                    ErrorCode.POLICY_INVALID,
                    f"policy {pid!r} cannot be resolved: no governance policy store is installed",
                )
            return []
        found: list[GovernancePolicy] = list(
            await hooks.resolver(env.session, self.ctx, models[0], pid)
        )
        if pid is not None and not found:
            raise _NodeFailure(
                ErrorCode.POLICY_INVALID,
                f"policy {pid!r} was not found or does not cover this model",
            )
        return [p.model_dump(mode="json") for p in found]

    def _lineage(self, env: _Env, action: Action) -> dict[str, Any]:
        """The recorded lineage of the dataset bound to ``data`` (replayable steps)."""
        sources = action.sources.get("data") or []
        src = sources[0] if sources else None
        if src is None:
            return {}
        row = env.rows.get(src)
        lineage = ((row.result or {}).get("_lineage") or {}) if row is not None else {}
        if not isinstance(lineage, dict):
            return {}
        for _port, meta in lineage.items():
            if isinstance(meta, dict):
                return meta
        return {}

    async def _submit_fit(
        self,
        env: _Env,
        action: Action,
        *,
        settings: Mapping[str, Any] | None = None,
        data_ref_text: str | None = None,
    ) -> str:
        """A ``fit`` child run of the spec ``wf-<workflow>-<node>`` (a new revision)."""
        s = env.session
        values = action.inputs.get("data") or []
        dataset = data_ref_text or (values[0].ref if values else None)
        if dataset is None:
            raise _NodeFailure(ErrorCode.SCHEMA_INVALID, "model nodes need a dataset reference")
        pipeline: list[dict[str, Any]] = []
        lineage = self._lineage(env, action)
        if lineage.get("replayable") and lineage.get("pipeline") and lineage.get("root"):
            dataset = str(lineage["root"])
            pipeline = [dict(p) for p in lineage["pipeline"] if isinstance(p, dict)]
        split: dict[str, Any] | None = None
        for ref in action.inputs.get("split") or []:
            plan = ref.inline if isinstance(ref.inline, dict) else {}
            inner = plan.get("plan") if isinstance(plan, dict) else None
            if not isinstance(inner, dict) or inner.get("kind") != "holdout":
                raise _NodeFailure(
                    ErrorCode.SPLIT_INVALID, "model.fit takes a holdout plan (split.holdout)"
                )
            raw_split = inner.get("split")
            split = dict(raw_split) if isinstance(raw_split, dict) else {}
        dv = await self._dataset(env, dataset)
        name = node_model_name(env.wf.name, action.node_id)
        try:
            doc = model_spec_document(
                action.params,
                name=name,
                project=env.scope.project_id,
                dataset=dataset,
                pipeline=pipeline,
                split=split,
                settings=settings,
                labels={MODEL_LABEL: action.node_id[:63], "workflow": env.wf.name[:63]},
            )
        except ValueError as exc:
            raise _NodeFailure(ErrorCode.WORKFLOW_INVALID, str(exc)) from None
        snapshot = await self.ctx.catalogs.get(s)
        if snapshot is not None and dv.data_schema:
            with contextlib.suppress(ValueError, ValidationError):
                entry = snapshot.catalog.resolve_model(str(doc["model"]["use"]))
                if entry is not None:
                    schema = model_input_schema(
                        entry.manifest.schema_template, doc["model"]["settings"]
                    )
                    if schema is not None:
                        doc["data"]["bind"] = auto_bind(
                            schema, DataSchema.model_validate(dv.data_schema), doc["data"]["bind"]
                        )
        v = await validate_document(s, env.scope, self.ctx.catalogs, document=doc)
        if not v.valid or v.resolved is None or v.dataset is None:
            raise ValidationFailed(v.issues)
        spec_row = (
            await s.execute(
                select(m.Spec)
                .where(
                    m.Spec.tenant_id == env.scope.tenant_id,
                    m.Spec.project_id == env.scope.project_id,
                    m.Spec.name == name,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        ts = now()
        if spec_row is None:
            spec_row = m.Spec(
                id=new_id("spc"),
                tenant_id=env.scope.tenant_id,
                project_id=env.scope.project_id,
                name=name,
                kind="ModelSpec",
                latest_revision=1,
                created_at=ts,
                updated_at=ts,
            )
            s.add(spec_row)
            await s.flush()
            revision = 1
        else:
            spec_row.latest_revision += 1
            spec_row.updated_at = ts
            revision = spec_row.latest_revision
        s.add(
            m.SpecRevision(
                spec_id=spec_row.id,
                revision=revision,
                tenant_id=spec_row.tenant_id,
                project_id=spec_row.project_id,
                kind="ModelSpec",
                hash=v.hash or "",
                yaml=canonical_json(v.document),
                document=v.document,
                valid=True,
                issues=[i.model_dump(mode="json") for i in v.issues],
                resolved=v.resolved.model_dump(mode="json"),
                catalog_digest=v.catalog.digest if v.catalog else None,
                dataset_id=v.dataset.dataset_id,
                dataset_version=v.dataset.version,
                message=f"workflow {workflow_ref(env.wf, env.wr.revision)} node {action.node_id}",
                created_at=ts,
            )
        )
        await s.flush()
        payload = pin_document(v.document, v)
        fit = payload["model"].setdefault("fit", {})
        if isinstance(fit, dict) and fit.get("seed") is None:
            fit["seed"] = DEFAULT_FIT_SEED
        inputs = {INPUT_DATASET: data_ref(v.dataset)}
        for tname, tv in v.side_tables.items():
            inputs[side_table_input(tname)] = data_ref(tv)
        assert v.typed is not None and hasattr(v.typed, "execution")
        run = await create_run(
            s,
            env.scope,
            kind=RunKind.FIT,
            resource_class=action.node.resource_class or v.typed.execution.resource_class,
            timeout_s=(
                action.node.timeout.seconds
                if action.node.timeout
                else v.typed.execution.timeout.seconds
            ),
            payload=payload,
            inputs=inputs,
            labels={"workflow": env.wf.name[:63], "workflow-node": action.node_id},
            spec_id=spec_row.id,
            spec_revision=revision,
            spec_hash=spec_hash(payload),
            dataset_id=v.dataset.dataset_id,
            dataset_version=v.dataset.version,
            catalog_digest=v.catalog.digest if v.catalog else None,
        )
        await self._accept(env, run)
        return run.id

    async def _submit_sub(self, env: _Env, action: Action) -> str:
        ref_text = action.params.get("workflow")
        try:
            ref = WorkflowRef.parse(str(ref_text))
        except ValueError as exc:
            raise _NodeFailure(ErrorCode.WORKFLOW_INVALID, str(exc)) from None
        if ref.project != env.scope.project_id:
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, f"workflow {ref} is in another project")
        wf = (
            await env.session.execute(
                select(m.Workflow).where(
                    m.Workflow.tenant_id == env.scope.tenant_id,
                    m.Workflow.project_id == env.scope.project_id,
                    m.Workflow.name == ref.name,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise _NodeFailure(ErrorCode.REF_UNRESOLVED, f"workflow {ref} was not found")
        inputs: dict[str, str] = {}
        for port, values in action.inputs.items():
            if not values or values[0].ref is None:
                raise _NodeFailure(
                    ErrorCode.WORKFLOW_INVALID,
                    f"sub-workflow input {port!r} must be a dataset or a model",
                )
            inputs[port] = values[0].ref
        raw = action.params.get("params")
        body = WorkflowRunCreate(
            revision=ref.revision_number,
            params=dict(raw) if isinstance(raw, dict) else {},
            inputs=inputs,
            use_cache=env.wr.use_cache,
        )
        child = await start_workflow_run(
            env.session,
            self.ctx,
            env.scope,
            wf,
            body,
            trigger=WorkflowTriggerEvent.PARENT,
            parent_run_id=env.wr.run_id,
            parent_node_id=action.node_id,
            created_by=env.wr.created_by,
            token_id=env.run.token_id,
        )
        return child.run_id

    async def _evidence_pack(self, env: _Env, action: Action) -> None:
        refs = action.inputs.get("model") or []
        if not refs or refs[0].ref is None:
            raise _NodeFailure(ErrorCode.SCHEMA_INVALID, "the evidence pack needs a model")
        mv = await self._model(env, refs[0].ref)
        evidence = [BoundValue(ref=r) for r in action.inputs.get("evidence") or []]
        lineage = {
            k: v for k, v in (mv.lineage or {}).items() if isinstance(v, str | int | float | bool)
        }
        result, outputs = evidence_pack(
            action.params, refs[0].ref, evidence, lineage=lineage, scorecard=mv.scorecard
        )
        env.engine.started_node(action.node_id)
        env.engine.succeeded(action.node_id, result, outputs)

    # ------------------------------------------------------------------------- collect
    async def _collect(self, env: _Env, row: m.WorkflowNodeRunRow) -> None:
        engine, s = env.engine, env.session
        nid = row.node_id
        assert row.child_run_id is not None
        if row.phase == _PHASE_SUB:
            await self._collect_sub(env, row)
            return
        child = await s.get(m.Run, row.child_run_id)
        if child is None:
            engine.failed(
                nid,
                NodeError(code=ErrorCode.INFRA_ERROR, message="the node's run is gone"),
                retryable=False,
            )
            return
        state = RunState(child.state)
        if not state.terminal:
            if state in _LIVE and engine.node_run(nid).status is NodeRunStatus.QUEUED:
                engine.started_node(nid, run_id=child.id)
            return
        if state is not RunState.SUCCEEDED:
            if engine.cancel_requested or (state is RunState.CANCELLED and env.wr.cancel_requested):
                engine.cancelled(nid)
                return
            problem = Problem.model_validate(child.problem) if child.problem else None
            code = problem.code if problem is not None else ErrorCode.INFRA_ERROR
            msg = (problem.detail or problem.title) if problem is not None else state.value
            engine.failed(
                nid,
                NodeError(
                    code=code,
                    message=f"{msg}"[:2000],
                    untrusted_text=problem.untrusted_text if problem is not None else None,
                ),
            )
            return
        if engine.node_run(nid).status is NodeRunStatus.QUEUED:
            engine.started_node(nid, run_id=child.id)
        try:
            if row.phase in (_PHASE_FIT, _PHASE_FIT_BEST):
                await self._collect_fit(env, row, child)
            else:
                await self._collect_node(env, row, child)
        except _NodeFailure as exc:
            engine.failed(nid, exc.error, retryable=False)

    async def _collect_sub(self, env: _Env, row: m.WorkflowNodeRunRow) -> None:
        assert row.child_run_id is not None
        sub = await env.session.get(m.WorkflowRunRow, row.child_run_id)
        nid = row.node_id
        if sub is None:
            env.engine.failed(
                nid,
                NodeError(code=ErrorCode.INFRA_ERROR, message="the sub-workflow run is gone"),
                retryable=False,
            )
            return
        status = WorkflowRunStatus(sub.status)
        if not status.terminal:
            if env.engine.node_run(nid).status is NodeRunStatus.QUEUED and status is (
                WorkflowRunStatus.RUNNING
            ):
                env.engine.started_node(nid, run_id=sub.run_id)
            return
        if status is WorkflowRunStatus.SUCCEEDED:
            if env.engine.node_run(nid).status is NodeRunStatus.QUEUED:
                env.engine.started_node(nid, run_id=sub.run_id)
            outputs = {k: NodeOutputRef.model_validate(v) for k, v in sub.outputs.items()}
            env.engine.succeeded(
                nid,
                {"status": status.value, "run_id": sub.run_id, "outputs": list(sorted(outputs))},
                outputs,
            )
        elif status is WorkflowRunStatus.CANCELLED and env.engine.cancel_requested:
            env.engine.cancelled(nid)
        else:
            err = sub.error or {}
            env.engine.failed(
                nid,
                NodeError(
                    code=ErrorCode(err.get("code", ErrorCode.USER_CODE_ERROR.value)),
                    message=f"the sub-workflow {status.value}: {err.get('message', '')}"[:2000],
                ),
            )

    async def _read_json(self, meta: Mapping[str, Any], cap: int) -> Any:
        om = ObjectMeta.model_validate(meta)
        if om.size > cap:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, f"{om.name} exceeds {cap} bytes")
        raw = await self.ctx.store.get(om.bucket, om.name, generation=om.generation, max_bytes=cap)
        try:
            return json.loads(raw)
        except ValueError:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, f"{om.name} is not JSON") from None

    async def _collect_node(self, env: _Env, row: m.WorkflowNodeRunRow, child: m.Run) -> None:
        s = env.session
        att = await s.get(m.RunAttempt, (child.id, child.attempt))
        outputs_meta: dict[str, Any] = dict(att.outputs or {}) if att is not None else {}
        meta = outputs_meta.get(NODE_RESULT_OUTPUT)
        if not isinstance(meta, dict):
            raise _NodeFailure(
                ErrorCode.INTEGRITY_FAILURE, "the node run wrote no node_result.json"
            )
        try:
            side = NodeResultSidecar.model_validate(
                await self._read_json(meta, NODE_RESULT_MAX_BYTES)
            )
        except ValidationError as exc:
            raise _NodeFailure(
                ErrorCode.INTEGRITY_FAILURE, "node_result.json is invalid", str(exc)
            ) from None
        nid = row.node_id
        if side.node_id != nid:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, "node_result.json names another node")
        outputs: dict[str, NodeOutputRef] = {}
        lineage: dict[str, JsonValue] = {}
        cap = self.ctx.settings.workflow_max_json_output_bytes
        for port, ref in side.outputs.items():
            if ref.type is PortType.DATASET:
                if ref.object is None or ref.object not in outputs_meta:
                    raise _NodeFailure(
                        ErrorCode.INTEGRITY_FAILURE, f"dataset output {port!r} was not uploaded"
                    )
                pinned, meta_doc = await self._register_dataset(env, row, child, port, outputs_meta)
                outputs[port] = NodeOutputRef(type=PortType.DATASET, ref=pinned)
                lineage[port] = meta_doc
            elif ref.inline is not None:
                outputs[port] = NodeOutputRef(type=ref.type, inline=ref.inline)
            elif ref.object is not None and ref.object in outputs_meta:
                doc = await self._read_json(outputs_meta[ref.object], cap)
                outputs[port] = NodeOutputRef(type=ref.type, inline=doc)
            else:
                raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, f"output {port!r} has no value")
        result: dict[str, JsonValue] = dict(side.result)
        if side.warnings_plain:
            result.setdefault("warnings_plain", list(side.warnings_plain))
        if lineage:
            result["_lineage"] = lineage
        deferred = result.get("deferred_outputs")
        if isinstance(deferred, list) and deferred and row.phase == _PHASE_NODE:
            best = result.get("best_settings")
            if isinstance(best, dict):
                action = self._action_for(env, row)
                try:
                    fit_id = await self._submit_fit(env, action, settings=best)
                except (ValidationFailed, MapiError) as exc:
                    raise _NodeFailure(
                        exc.code, f"refitting the winner failed: {exc.detail}"
                    ) from None
                row.phase = _PHASE_FIT_BEST
                env.engine.attach_run(nid, fit_id)
                env.extra_results[nid] = {
                    "_phase1": {
                        "result": result,
                        "outputs": {
                            k: v.model_dump(mode="json", exclude_none=True)
                            for k, v in outputs.items()
                        },
                    },
                }
                return
        env.engine.succeeded(nid, result, outputs)

    def _action_for(self, env: _Env, row: m.WorkflowNodeRunRow) -> Action:
        node = env.spec.node(row.node_id)
        assert node is not None
        nr = env.engine.node_run(row.node_id)
        impl = env.engine.registry[node.impl_id]
        inputs: dict[str, list[NodeOutputRef]] = {}
        sources: dict[str, list[str | None]] = {}
        for port in node.inputs:
            for b in node.alternatives(port):
                src, _, p = b.partition(".")
                if b.startswith("inputs."):
                    ref = env.engine.inputs.get(b.removeprefix("inputs."))
                    if ref is not None:
                        inputs.setdefault(port, []).append(ref)
                        sources.setdefault(port, []).append(None)
                        break
                elif src in env.rows:
                    out = (env.rows[src].outputs or {}).get(p)
                    if out is not None:
                        inputs.setdefault(port, []).append(NodeOutputRef.model_validate(out))
                        sources.setdefault(port, []).append(src)
                        break
        return Action(
            node_id=row.node_id,
            impl=impl,
            node=node,
            params=dict(nr.params),
            inputs=inputs,
            sources=sources,
            cache_key=nr.cache_key,
            attempt=nr.attempt,
            runs_in=impl.runs_in,
        )

    async def _collect_fit(self, env: _Env, row: m.WorkflowNodeRunRow, child: m.Run) -> None:
        s = env.session
        if child.output_model_version_id is None:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, "the fit registered no model version")
        mv = await s.get(m.ModelVersion, child.output_model_version_id)
        model = await s.get(m.Model, mv.model_id) if mv is not None else None
        if mv is None or model is None:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, "the fitted model version is gone")
        ref = f"model://{mv.project_id}/{model.name}@v{mv.version}"
        mref = NodeOutputRef(type=PortType.MODEL_VERSION, ref=ref)
        nid = row.node_id
        params = env.engine.node_run(nid).params
        metrics = {k: clean_number(v) for k, v in (mv.metrics or {}).items()}
        scorecard = (mv.scorecard or {}).get("overall") if isinstance(mv.scorecard, dict) else None
        if row.phase == _PHASE_FIT_BEST:
            phase1 = (row.result or {}).get("_phase1") or {}
            result = dict(phase1.get("result") or {})
            result["model_version"] = ref
            outputs = {
                k: NodeOutputRef.model_validate(v) for k, v in (phase1.get("outputs") or {}).items()
            }
            outputs["best"] = mref
            env.engine.succeeded(nid, result, outputs)
            return
        cand = MetricsCandidate(
            name=nid,
            group=nid,
            kind="model",
            model=str(params.get("model")) if params.get("model") else None,
            settings=_settings_of(params),
            metrics=metrics,
        )
        result_metrics: dict[str, JsonValue] = {k: v for k, v in metrics.items()}
        env.engine.succeeded(
            nid,
            {
                "model_version": ref,
                "metrics": result_metrics,
                "scorecard": scorecard,
                "model": str(params.get("model")),
            },
            {
                "model": mref,
                "metrics": NodeOutputRef(
                    type=PortType.METRICS,
                    inline=MetricsPayload(candidates=[cand]).model_dump(mode="json"),
                ),
            },
        )

    async def _register_dataset(
        self,
        env: _Env,
        row: m.WorkflowNodeRunRow,
        child: m.Run,
        port: str,
        outputs_meta: Mapping[str, Any],
    ) -> tuple[str, dict[str, JsonValue]]:
        """Register a node's derived dataset as a version of ``wf-<workflow>-<node>``."""
        s = env.session
        base = f"out/{port}/"
        data_meta = ObjectMeta.model_validate(outputs_meta[base + "data.zarr.zip"])
        cap = self.ctx.settings.max_sidecar_bytes
        try:
            schema = DataSchema.model_validate(
                await self._read_json(outputs_meta[base + "schema.json"], cap)
            )
            summary = (
                DataSummary.model_validate(
                    await self._read_json(outputs_meta[base + "summary.json"], cap)
                )
                if base + "summary.json" in outputs_meta
                else None
            )
            meta_doc = (
                await self._read_json(outputs_meta[base + "meta.json"], cap)
                if base + "meta.json" in outputs_meta
                else {}
            )
        except (ValidationError, KeyError) as exc:
            raise _NodeFailure(
                ErrorCode.INTEGRITY_FAILURE, f"dataset output {port!r} is invalid", str(exc)
            ) from None
        if data_meta.md5_hash is None:
            raise _NodeFailure(ErrorCode.INTEGRITY_FAILURE, "the dataset output has no MD5")
        sha_hex = dataset_sha(data_meta)
        name = node_model_name(
            env.wf.name, row.node_id if port == "data" else f"{row.node_id}-{port}"
        )
        ds = (
            await s.execute(
                select(m.Dataset)
                .where(
                    m.Dataset.tenant_id == env.scope.tenant_id,
                    m.Dataset.project_id == env.scope.project_id,
                    m.Dataset.name == name,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        ts = now()
        if ds is None:
            ds = m.Dataset(
                id=new_id("ds"),
                tenant_id=env.scope.tenant_id,
                project_id=env.scope.project_id,
                name=name,
                labels={"workflow": env.wf.name[:63], "workflow-node": row.node_id},
                description=f"Written by node {row.node_id} of {workflow_ref(env.wf)}",
                next_version=1,
                created_at=ts,
                updated_at=ts,
            )
            s.add(ds)
            await s.flush()
        same = (
            (
                await s.execute(
                    select(m.DatasetVersion).where(
                        m.DatasetVersion.dataset_id == ds.id,
                        m.DatasetVersion.sha256 == f"sha256:{sha_hex}",
                        m.DatasetVersion.status == "ready",
                    )
                )
            )
            .scalars()
            .first()
        )
        if same is not None:
            return f"dataset://{ds.project_id}/{ds.name}@v{same.version}", dict(meta_doc)
        prefix = dataset_prefix(env.scope.tenant_id, sha_hex)
        stored: dict[str, Any] = {}
        for fname in ("data.zarr.zip", "schema.json", "summary.json", "meta.json"):
            meta = outputs_meta.get(base + fname)
            if meta is None:
                continue
            src = ObjectMeta.model_validate(meta)
            dst_name = prefix + fname
            try:
                copied = await self.ctx.store.copy(
                    src.bucket,
                    src.name,
                    self.ctx.buckets.data,
                    dst_name,
                    source_generation=src.generation,
                    if_generation_match=0,
                )
            except PreconditionFailed:
                copied = await self.ctx.store.stat(self.ctx.buckets.data, dst_name)
            stored[fname] = {
                "bucket": self.ctx.buckets.data,
                "name": copied.name,
                "generation": copied.generation,
                "size": copied.size,
                "crc32c": copied.crc32c,
                "md5": copied.md5_hash,
                "content_type": copied.content_type,
            }
        version = ds.next_version
        ds.next_version += 1
        ds.latest_version = max(ds.latest_version or 0, version)
        ds.updated_at = ts
        s.add(
            m.DatasetVersion(
                dataset_id=ds.id,
                version=version,
                tenant_id=ds.tenant_id,
                project_id=ds.project_id,
                status="ready",
                sha256=f"sha256:{sha_hex}",
                size_bytes=data_meta.size,
                data_schema=schema.model_dump(mode="json"),
                summary=summary.model_dump(mode="json") if summary else None,
                objects={
                    "data.zarr.zip": stored["data.zarr.zip"],
                    **{k: v for k, v in stored.items() if k != "data.zarr.zip"},
                },
                ingest_run_id=child.id,
                issues=[],
                created_at=ts,
                ready_at=ts,
            )
        )
        await s.flush()
        return f"dataset://{ds.project_id}/{ds.name}@v{version}", dict(meta_doc)

    # -------------------------------------------------------------------------- cancel
    async def _cancel_child(self, env: _Env, nid: str) -> None:
        row = env.rows[nid]
        child_id = row.child_run_id
        if child_id is None:
            env.engine.cancelled(nid)
            return
        if row.phase == _PHASE_SUB:
            sub = await env.session.get(m.WorkflowRunRow, child_id)
            if sub is None or WorkflowRunStatus(sub.status).terminal:
                env.engine.cancelled(nid)
            else:
                await request_cancel(env.session, sub)
            return
        child = await env.session.get(m.Run, child_id, with_for_update=True)
        if child is None:
            env.engine.cancelled(nid)
            return
        state = RunState(child.state)
        if state.terminal:
            env.engine.cancelled(nid)
        elif state not in (RunState.CANCELLING, RunState.FINALIZING):
            await apply_transition(env.session, child, RunTrigger.CANCEL, Actor.API)
