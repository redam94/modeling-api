"""``local-process``: run the supervisor (``mapi-runner``) as a child process (plan §8.4).

Isolation is only ``PROCESS``: the executor exists for the dev loop and tests, and it
**refuses to start in a multi-tenant profile**. Identifiers reach the run through the
environment only (``MAPI_RUN_ID``, ``MAPI_NONCE``, ``MAPI_BOOTSTRAP_URL`` and the local-only
``MAPI_EXEC_TOKEN``); nothing goes in argv. The supervisor's stdout is the event pipe: each
``{"mapi_event": …}`` line is decoded here and handed to the event ingestor, which verifies
its HMAC. A pid file per nonce lets a restarted orchestrator adopt still-running children.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mapi_spec._types import Duration
from mapi_spec.executor import (
    ExecState,
    ExecStatus,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunSubmit,
)
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    MAX_EVENT_LINE_BYTES,
    InvalidEvent,
    RunnerEnv,
    RunnerExit,
    decode_event_line,
)

from mapi_server.settings import Profile

__all__ = ["DEFAULT_ENV_PASSTHROUGH", "ExecutorRefused", "LocalProcessExecutor"]

log = logging.getLogger(__name__)

DEFAULT_ENV_PASSTHROUGH = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMBA_CACHE_DIR",
        "PYTENSOR_FLAGS",
        "SYSTEMROOT",
    }
)
"""Parent variables the supervisor inherits (no ``MAPI_*``, no ``DATABASE_URL``)."""

_STDERR_TAIL = 50


class ExecutorRefused(RuntimeError):
    """The executor must not run in this deployment profile."""


@dataclass
class _Exec:
    handle: Handle
    proc: asyncio.subprocess.Process | None
    pid: int
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    watchdog: asyncio.Task[None] | None = None
    stopper: asyncio.Task[None] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    cancelled: bool = False
    timed_out: bool = False
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL))
    invalid_lines: int = 0
    adopted: bool = False


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LocalProcessExecutor:
    """Spawns one supervisor process per attempt and reads its stdout as the event pipe."""

    name = "local-process"

    def __init__(
        self,
        *,
        command: Sequence[str],
        bootstrap_url_template: str,
        workdir: Path,
        exec_token: str | None = None,
        profile: Profile = Profile.SINGLE_TENANT,
        env_passthrough: frozenset[str] = DEFAULT_ENV_PASSTHROUGH,
        extra_env: Mapping[str, str] | None = None,
        cancel_grace_s: float = 10.0,
        max_runtime_s: int = 24 * 3600,
        parent_env: Mapping[str, str] | None = None,
    ) -> None:
        if Profile(profile) is Profile.MULTI_TENANT:
            raise ExecutorRefused(
                "the local-process executor has PROCESS isolation only and refuses to start "
                "in a multi-tenant profile (MAPI_PROFILE=multi-tenant)"
            )
        if not command:
            raise ValueError("the runner command must not be empty")
        # validates the template (exactly one {nonce}, http(s):// or gs://)
        RunnerEnv(run_id="probe", nonce="0" * 64, bootstrap_url=bootstrap_url_template)
        self._command = list(command)
        self._template = bootstrap_url_template
        self._token = exec_token
        self._workdir = workdir
        self._passthrough = env_passthrough
        self._extra_env = dict(extra_env or {})
        if any(k.startswith("MAPI_") for k in self._extra_env):
            raise ValueError("extra_env must not set MAPI_* variables")
        self._grace = cancel_grace_s
        self._max_runtime_s = max_runtime_s
        self._parent_env = dict(os.environ if parent_env is None else parent_env)
        self._execs: dict[str, _Exec] = {}
        self._events: asyncio.Queue[RunEvent] = asyncio.Queue()

    # ------------------------------------------------------------------ protocol
    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=Isolation.PROCESS,
            egress_enforced=False,
            dns_enforced=False,
            max_runtime=Duration(self._max_runtime_s),
            classes=[ResourceClass.S, ResourceClass.M, ResourceClass.L],
            max_tasks=1,
            checkpoint_storage=True,
        )

    def _environ(self, rs: RunSubmit, nonce: str) -> dict[str, str]:
        env = {k: v for k, v in self._parent_env.items() if k in self._passthrough}
        env.update(self._extra_env)
        env["PYTHONUNBUFFERED"] = "1"
        runner_env = RunnerEnv(
            run_id=rs.run_id, nonce=nonce, bootstrap_url=self._template, exec_token=self._token
        )
        env.update(runner_env.to_environ())
        return env

    def _pidfile(self, nonce: str) -> Path:
        return self._workdir / "pids" / f"{nonce}.json"

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        existing = self._execs.get(nonce)
        if existing is not None:  # duplicate submit: same execution
            return existing.handle
        adopted = await self.adopt(nonce)
        if adopted is not None:
            return adopted
        rundir = self._workdir / "runs" / nonce[:24]
        rundir.mkdir(parents=True, exist_ok=True)
        self._pidfile(nonce).parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *self._command,
            env=self._environ(rs, nonce),
            cwd=str(rundir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_EVENT_LINE_BYTES + 1024,
        )
        handle = Handle(
            executor=self.name,
            run_id=rs.run_id,
            attempt=rs.attempt,
            nonce=nonce,
            external_id=f"pid:{proc.pid}",
        )
        ex = _Exec(handle=handle, proc=proc, pid=proc.pid)
        self._execs[nonce] = ex
        self._pidfile(nonce).write_text(
            json.dumps(
                {"pid": proc.pid, "run_id": rs.run_id, "attempt": rs.attempt, "nonce": nonce}
            )
        )
        ex.readers = [
            asyncio.create_task(self._read_stdout(ex), name=f"stdout-{rs.run_id}"),
            asyncio.create_task(self._read_stderr(ex), name=f"stderr-{rs.run_id}"),
        ]
        hard_limit = min(rs.timeout.seconds, self._max_runtime_s) + self._grace
        ex.watchdog = asyncio.create_task(self._watchdog(ex, hard_limit))
        log.info("started supervisor pid=%s for run %s/%s", proc.pid, rs.run_id, rs.attempt)
        return handle

    async def adopt(self, nonce: str) -> Handle | None:
        ex = self._execs.get(nonce)
        if ex is not None:
            return ex.handle
        pidfile = self._pidfile(nonce)
        if not pidfile.is_file():
            return None
        try:
            info = json.loads(pidfile.read_text())
            pid = int(info["pid"])
            handle = Handle(
                executor=self.name,
                run_id=str(info["run_id"]),
                attempt=int(info["attempt"]),
                nonce=nonce,
                external_id=f"pid:{pid}",
            )
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable pid file %s", pidfile)
            return None
        if not _pid_alive(pid):
            return None
        self._execs[nonce] = _Exec(handle=handle, proc=None, pid=pid, adopted=True)
        log.info("adopted orphan supervisor pid=%s for run %s", pid, handle.run_id)
        return handle

    async def status(self, h: Handle) -> ExecStatus:
        ex = self._execs.get(h.nonce)
        if ex is None:
            return ExecStatus(state=ExecState.UNKNOWN, message="no such execution")
        if ex.proc is None:  # adopted after a restart: no pipe, no exit code
            if _pid_alive(ex.pid):
                return ExecStatus(state=ExecState.RUNNING, started_at=ex.started_at)
            return ExecStatus(
                state=ExecState.UNKNOWN,
                message="the adopted supervisor exited; its exit code is unavailable",
            )
        rc = ex.proc.returncode
        if rc is None or not all(t.done() for t in ex.readers):
            return ExecStatus(state=ExecState.RUNNING, started_at=ex.started_at)
        code = _exit_code(rc)
        if ex.timed_out and rc < 0:
            # killed by the hard-timeout watchdog (SIGKILL after an ignored SIGTERM): a
            # timeout, not the OOM killer that 137 means
            code = RunnerExit.TERMINATED
        if ex.cancelled:
            state = ExecState.CANCELLED
        elif code == 0:
            state = ExecState.SUCCEEDED
        else:
            state = ExecState.FAILED
        tail = "\n".join(ex.stderr_tail)[-2000:] or None
        message = "hard timeout: the supervisor was killed" if ex.timed_out else tail
        return ExecStatus(
            state=state,
            exit_code=code,
            started_at=ex.started_at,
            finished_at=ex.finished_at,
            message=message,
        )

    async def cancel(self, h: Handle) -> None:
        """SIGTERM now, SIGKILL after ``cancel_grace_s`` in the background.

        Never waits for the process: the reconciler cancels inline in its serial pass, and
        the next pass observes the terminal status. Repeated calls do not re-signal.
        """
        ex = self._execs.get(h.nonce)
        if ex is None:
            return
        ex.cancelled = True
        if ex.stopper is None:
            ex.stopper = asyncio.create_task(self._terminate(ex), name=f"stop-{h.run_id}")
            await asyncio.sleep(0)  # let the SIGTERM go out before returning

    def event_source(self) -> AsyncIterator[RunEvent]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[RunEvent]:
        while True:
            yield await self._events.get()

    async def aclose(self) -> None:
        """Terminate every child (used on shutdown of a dev process)."""
        for ex in list(self._execs.values()):
            if ex.stopper is not None:
                with contextlib.suppress(Exception):
                    await ex.stopper
            if ex.proc is not None and ex.proc.returncode is None:
                await self._terminate(ex)
            if ex.watchdog is not None:
                ex.watchdog.cancel()

    # ------------------------------------------------------------------ internals
    async def _terminate(self, ex: _Exec) -> None:
        if ex.proc is not None and ex.proc.returncode is not None:
            return
        if ex.proc is None and not _pid_alive(ex.pid):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(ex.pid, signal.SIGTERM)
        if ex.proc is None:  # adopted: no child handle to wait on; poll the pid instead
            loop = asyncio.get_running_loop()
            until = loop.time() + self._grace
            while _pid_alive(ex.pid) and loop.time() < until:
                await asyncio.sleep(0.1)
            if _pid_alive(ex.pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(ex.pid, signal.SIGKILL)
            return
        try:
            await asyncio.wait_for(ex.proc.wait(), self._grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(ex.pid, signal.SIGKILL)
            await ex.proc.wait()

    async def _watchdog(self, ex: _Exec, limit_s: float) -> None:
        assert ex.proc is not None
        try:
            await asyncio.wait_for(ex.proc.wait(), limit_s)
        except TimeoutError:
            log.warning("run %s exceeded its hard timeout; terminating", ex.handle.run_id)
            ex.timed_out = True
            await self._terminate(ex)
        ex.finished_at = datetime.now(UTC)
        with contextlib.suppress(OSError):
            self._pidfile(ex.handle.nonce).unlink()

    async def _read_stdout(self, ex: _Exec) -> None:
        assert ex.proc is not None and ex.proc.stdout is not None
        reader = ex.proc.stdout
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # longer than the limit: asyncio drops it (the tail is not JSON)
                ex.invalid_lines += 1
                continue
            if not line:
                return
            try:
                event = decode_event_line(line.rstrip(b"\r\n"))
            except InvalidEvent as exc:
                ex.invalid_lines += 1
                log.warning("invalid event line from run %s: %s", ex.handle.run_id, exc.detail)
                continue
            if event is None:
                continue  # not an event (the supervisor only prints events; ignore noise)
            if event.run_id != ex.handle.run_id or event.attempt != ex.handle.attempt:
                ex.invalid_lines += 1
                log.warning("event identity mismatch from run %s", ex.handle.run_id)
                continue
            await self._events.put(event)

    async def _read_stderr(self, ex: _Exec) -> None:
        assert ex.proc is not None and ex.proc.stderr is not None
        while True:
            try:
                line = await ex.proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                return
            ex.stderr_tail.append(line.decode("utf-8", "replace").rstrip()[:500])
