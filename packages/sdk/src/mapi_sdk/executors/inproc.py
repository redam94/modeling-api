"""In-process executor (isolation NONE) for notebooks, tests and ``mapi fit`` on own code.

Implements the :class:`mapi_spec.executor.Executor` protocol: ``submit`` starts the run in
a worker thread, ``status`` is the source of truth, ``cancel`` is cooperative (checked
between pipeline phases) and ``event_source`` yields :class:`~mapi_spec.executor.RunEvent`
objects. Payloads are JSON only; dataset references are resolved by a ``resolver``.

Supported run kinds: ``fit`` (payload ``spec``, ``data``, optional ``side_tables`` and
``raw_schema``) and ``score``/``evaluate`` (payload ``request``, ``bundle`` - a bundle
directory or a key registered with :meth:`InprocExecutor.put_bundle` - and optional
``data``/``side_tables``; otherwise the request's ``data``/``scenario`` refs are used).

Never use this executor for untrusted code: it runs plugins in the caller's process.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import traceback
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import (
    ExecState,
    ExecStatus,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunEventType,
    RunKind,
    RunSubmit,
)
from mapi_spec.loader import load_model_spec, load_score_request, parse_document
from mapi_spec.resources import ResourceClass
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec, ScoreRequest

from mapi_sdk.bundle import Bundle
from mapi_sdk.errors import RunCancelled
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.steps import CancelToken
from mapi_sdk.wire import read_zarr_zip

__all__ = ["DataResolver", "InprocExecutor", "mapping_resolver"]

DataResolver = Callable[[str], xr.Dataset]


def mapping_resolver(datasets: Mapping[str, xr.Dataset]) -> DataResolver:
    """Resolve dataset refs from a mapping; ``file://`` refs are read as zarr zips."""

    def resolve(ref: str) -> xr.Dataset:
        if ref in datasets:
            return datasets[ref]
        if ref.startswith("file://"):
            return read_zarr_zip(Path(ref.removeprefix("file://")))
        raise MapiError(f"dataset {ref!r} is not available", code=ErrorCode.REF_UNRESOLVED)

    return resolve


@dataclass(slots=True)
class _Run:
    handle: Handle
    submit: RunSubmit
    token: CancelToken = field(default_factory=CancelToken)
    state: ExecState = ExecState.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    problem: Problem | None = None
    message: str | None = None
    result: Any = None
    seq: int = 0
    task: asyncio.Task[None] | None = None
    events: list[RunEvent] = field(default_factory=list)


class InprocExecutor:
    """Runs fit/score work in a thread of the calling process."""

    name = "inproc"

    def __init__(
        self,
        *,
        registry: Registry | None = None,
        resolver: DataResolver | Mapping[str, xr.Dataset] | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        if resolver is None:
            self.resolver: DataResolver = mapping_resolver({})
        elif callable(resolver):
            self.resolver = resolver
        else:
            self.resolver = mapping_resolver(resolver)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self._runs: dict[str, _Run] = {}
        self._bundles: dict[str, Bundle] = {}
        self._subscribers: list[asyncio.Queue[RunEvent]] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None

    # ------------------------------------------------------------------ protocol
    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=Isolation.NONE,
            max_runtime="24h",
            classes=list(ResourceClass),
            max_tasks=1,
        )

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        """Start ``rs``; resubmitting the same nonce returns the existing handle."""
        if nonce in self._runs:
            return self._runs[nonce].handle
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        handle = Handle(executor=self.name, run_id=rs.run_id, attempt=rs.attempt, nonce=nonce)
        run = _Run(handle=handle, submit=rs)
        self._runs[nonce] = run
        self._emit(run, RunEventType.STATE, {"state": ExecState.PENDING.value})
        run.task = asyncio.create_task(self._execute(run))
        return handle

    async def adopt(self, nonce: str) -> Handle | None:
        run = self._runs.get(nonce)
        return None if run is None else run.handle

    async def status(self, h: Handle) -> ExecStatus:
        run = self._get(h)
        return ExecStatus(
            state=run.state,
            exit_code=None
            if not run.state.terminal
            else (0 if run.state is ExecState.SUCCEEDED else 1),
            started_at=run.started_at,
            finished_at=run.finished_at,
            message=run.message,
            problem=run.problem,
        )

    async def cancel(self, h: Handle) -> None:
        run = self._get(h)
        if run.state.terminal:
            return
        run.token.cancel("cancelled by the control plane")
        if run.state is ExecState.PENDING and run.task is not None and not run.task.done():
            run.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run.task
            self._finish(run, ExecState.CANCELLED, message="cancelled before start")

    async def event_source(self) -> AsyncIterator[RunEvent]:
        """Events of every run submitted after the iterator starts (runs forever)."""
        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(queue)

    # -------------------------------------------------------------------- extras
    def put_bundle(self, key: str, bundle: Bundle) -> None:
        """Make an in-memory bundle available to score payloads as ``{"bundle": key}``."""
        self._bundles[key] = bundle

    def result(self, h: Handle) -> Any:
        """The run's result (a :class:`Bundle` for fit, an ``xr.DataTree`` for score)."""
        return self._get(h).result

    def events(self, h: Handle) -> list[RunEvent]:
        return list(self._get(h).events)

    async def wait(self, h: Handle, timeout: float | None = None) -> ExecStatus:
        """Wait until the run is terminal (``TimeoutError`` after ``timeout`` seconds)."""
        run = self._get(h)
        if run.task is not None and not run.task.done():
            done, _ = await asyncio.wait({run.task}, timeout=timeout)
            if not done:
                raise TimeoutError(f"run {h.run_id!r} is still {run.state.value}")
        return await self.status(h)

    # ------------------------------------------------------------------ internals
    def _get(self, h: Handle) -> _Run:
        run = self._runs.get(h.nonce)
        if run is None or run.handle.run_id != h.run_id:
            raise MapiError(f"unknown run {h.run_id!r}", code=ErrorCode.NOT_FOUND)
        return run

    def _emit(self, run: _Run, kind: RunEventType, payload: dict[str, Any]) -> None:
        with self._lock:
            ev = RunEvent(
                run_id=run.handle.run_id,
                attempt=run.handle.attempt,
                seq=run.seq,
                type=kind,
                ts=datetime.now(UTC),
                payload=payload,
            )
            run.seq += 1
            run.events.append(ev)
        loop = self._loop
        off_loop = loop is not None and threading.get_ident() != self._loop_thread
        for q in list(self._subscribers):
            if off_loop:
                assert loop is not None
                loop.call_soon_threadsafe(q.put_nowait, ev)
            else:
                q.put_nowait(ev)

    def _finish(
        self,
        run: _Run,
        state: ExecState,
        *,
        problem: Problem | None = None,
        message: str | None = None,
    ) -> None:
        if run.state.terminal:
            return
        run.state = state
        run.problem = problem
        run.message = message
        run.finished_at = datetime.now(UTC)
        kind = RunEventType.COMPLETE if state is ExecState.SUCCEEDED else RunEventType.ERROR
        payload: dict[str, Any] = {"state": state.value}
        if problem is not None:
            payload["code"] = problem.code.value
        self._emit(run, RunEventType.STATE, {"state": state.value})
        self._emit(run, kind, payload)

    async def _execute(self, run: _Run) -> None:
        run.state = ExecState.RUNNING
        run.started_at = datetime.now(UTC)
        self._emit(run, RunEventType.STATE, {"state": ExecState.RUNNING.value})
        try:
            run.result = await asyncio.to_thread(self._work, run)
        except RunCancelled as exc:
            self._finish(run, ExecState.CANCELLED, problem=exc.to_problem(), message=exc.detail)
        except MapiError as exc:
            self._finish(run, ExecState.FAILED, problem=exc.to_problem(), message=exc.detail)
        except Exception as exc:  # a plugin or harness bug: report it, never crash the loop
            tb = "".join(traceback.format_exception_only(type(exc), exc))
            problem = Problem.from_code(
                ErrorCode.USER_CODE_ERROR, "the run failed", untrusted_text=tb[:8000]
            )
            self._finish(run, ExecState.FAILED, problem=problem, message="the run failed")
        else:
            self._finish(run, ExecState.SUCCEEDED, message="completed")

    def _hooks(self, run: _Run) -> FitHooks:
        return FitHooks(
            log=lambda m: self._emit(run, RunEventType.LOG, {"message": m[:4000]}),
            on_progress=lambda f, m: self._emit(
                run, RunEventType.PROGRESS, {"fraction": round(f, 4), "message": m[:500]}
            ),
            cancel=run.token,
        )

    def _work(self, run: _Run) -> Any:
        rs = run.submit
        p = rs.payload
        run.token.check()
        if rs.kind is RunKind.FIT:
            spec = _model_spec(p.get("spec"))
            data = self.resolver(str(p.get("data") or spec.data.dataset))
            tables = {k: self.resolver(str(v)) for k, v in _mapping(p.get("side_tables")).items()}
            raw = p.get("raw_schema")
            schema = DataSchema.model_validate(raw) if isinstance(raw, dict) else None
            bundle = fit_spec(
                spec, data, tables, self.registry, raw_schema=schema, hooks=self._hooks(run)
            )
            if self.output_dir is not None:
                bundle.write(self.output_dir / rs.run_id / f"a{rs.attempt}")
            return bundle
        if rs.kind in (RunKind.SCORE, RunKind.EVALUATE):
            req = _score_request(p.get("request"))
            bundle = self._bundle(p.get("bundle"))
            ref = p.get("data") or req.scenario or req.data
            payload: xr.Dataset | None = self.resolver(str(ref)) if ref is not None else None
            tables = {k: self.resolver(str(v)) for k, v in _mapping(p.get("side_tables")).items()}
            for name, dref in req.side_tables.items():
                if name not in tables:  # tables in the run payload win over request refs
                    tables[name] = self.resolver(str(dref))
            metrics = p.get("metrics")
            return score_bundle(
                bundle,
                req,
                payload,
                side_tables=tables,
                registry=self.registry,
                hooks=self._hooks(run),
                metrics=[str(m) for m in metrics] if isinstance(metrics, list) else None,
            )
        raise MapiError(
            f"the in-process executor does not run {rs.kind.value!r} runs",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )

    def _bundle(self, ref: Any) -> Bundle:
        if not isinstance(ref, str):
            raise MapiError(
                "score payload needs a 'bundle' reference", code=ErrorCode.SCHEMA_INVALID
            )
        if ref in self._bundles:
            return self._bundles[ref]
        path = Path(ref.removeprefix("file://"))
        if not path.is_dir():
            raise MapiError(f"bundle {ref!r} is not available", code=ErrorCode.REF_UNRESOLVED)
        return Bundle.read(path, self.registry)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _model_spec(value: Any) -> ModelSpec:
    if isinstance(value, str):
        return load_model_spec(value)
    if isinstance(value, Mapping):
        doc = parse_document(dict(value))
        if isinstance(doc, ModelSpec):
            return doc
    raise MapiError("fit payload needs a ModelSpec under 'spec'", code=ErrorCode.SCHEMA_INVALID)


def _score_request(value: Any) -> ScoreRequest:
    if isinstance(value, str):
        return load_score_request(value)
    if isinstance(value, Mapping):
        doc = parse_document(dict(value))
        if isinstance(doc, ScoreRequest):
            return doc
    raise MapiError(
        "score payload needs a ScoreRequest under 'request'", code=ErrorCode.SCHEMA_INVALID
    )
