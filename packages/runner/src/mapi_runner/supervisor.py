"""The trusted supervisor (plan §8.1, §8.5, §8.7).

One supervisor runs per execution. It:

1. reads ``MAPI_RUN_ID``/``MAPI_NONCE``/``MAPI_BOOTSTRAP_URL``, fetches the RunSpec bootstrap
   object and deletes it before anything else happens;
2. starts the untrusted worker (``python -I -u -m mapi_runner.worker``) as a child with a
   scrubbed environment, a socketpair for messages and pipes for stdout/stderr - as uid
   10001 with no-new-privs when it runs as root, in *dev mode* otherwise;
3. validates every worker message, turns them and the captured output into HMAC-signed
   events on its own stdout (the only thing ever written there), enforces the log, event,
   tracking, scratch and output caps, and coalesces progress;
4. handles SIGTERM and the run deadline (forward to the worker, wait for the checkpoint
   flush, upload a ``partial`` manifest);
5. uploads the media the worker logs (v0.14: each file under ``{scratch}/media/`` is read
   safely, hashed and checked by the supervisor, then sent through the run's ``media`` POST
   policy) and emits one ``media`` event per item;
6. uploads outputs with the signed POST policy (streaming, hashing as it goes), writes the
   signed :class:`~mapi_spec.run.OutputManifest` with the signed PUT and exits with a
   :class:`~mapi_spec.run.RunnerExit` code.

The supervisor never imports plugin code, the SDK, numpy or xarray.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from importlib import metadata
from types import FrameType
from typing import Final

import httpx
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ERROR_INFO, ErrorCode, Issue, MapiError
from mapi_spec.media import ImageInfo, check_media, media_name
from mapi_spec.run import (
    ENV_REQUIRE_ISOLATION,
    MANIFEST_OBJECT_NAME,
    MAX_ERROR_ISSUES,
    OUTPUT_ARTIFACTS,
    OUTPUT_CHECKPOINTS,
    OUTPUT_MANIFEST,
    OUTPUT_MEDIA,
    CheckpointPayload,
    CompletePayload,
    ErrorPayload,
    FailureClass,
    InputDigest,
    ManifestObject,
    MediaPayload,
    OutputManifest,
    RunnerEnv,
    RunnerExit,
    RunnerPhase,
    RunSpec,
    StatePayload,
    WarningPayload,
    sign_output_manifest,
    worker_environ,
)
from mapi_spec.storage import PostPolicy, SignedPut
from pydantic import BaseModel, ValidationError

from mapi_runner import __version__
from mapi_runner._text import clean_text
from mapi_runner.bootstrap import BootstrapError, fetch_and_delete
from mapi_runner.channel import (
    MAX_START_FRAME,
    MAX_WORKER_FRAME,
    CheckpointMessage,
    DiagnosticMessage,
    ErrorMessage,
    FrameReader,
    MediaMessage,
    MetricMessage,
    MetricsMessage,
    ParamsMessage,
    ProgressMessage,
    ProtocolError,
    ResultMessage,
    StartMessage,
    StateMessage,
    TagsMessage,
    UserLogMessage,
    WarningMessage,
    WorkerMessage,
    parse_worker_message,
    send_frame,
)
from mapi_runner.events import (
    EventEmitter,
    LogFlood,
    LogGate,
    ProgressCoalescer,
    TrackingGate,
)
from mapi_runner.events import clean_payload as _clean
from mapi_runner.obslog import StderrLog, trace_id_from_env
from mapi_runner.outputs import OutputViolation, collect_outputs, open_regular
from mapi_runner.sandbox import WORKER_GID, WORKER_UID, IsolationPlan, plan_isolation
from mapi_runner.transfer import Digest, TransferError, Transfers, new_client

__all__ = ["EXIT_FOR_CLASS", "Supervisor", "SupervisorConfig", "Termination"]

EXIT_FOR_CLASS: Final[Mapping[FailureClass, RunnerExit]] = {
    FailureClass.USER: RunnerExit.USER_ERROR,
    FailureClass.RESOURCE_EXHAUSTED: RunnerExit.RESOURCE_EXHAUSTED,
    FailureClass.INFRA: RunnerExit.INFRA_ERROR,
    FailureClass.INTEGRITY: RunnerExit.INTEGRITY_ERROR,
    FailureClass.TIMEOUT: RunnerExit.TIMEOUT,
    FailureClass.SANDBOX: RunnerExit.SANDBOX_VIOLATION,
}
# what an (untrusted) worker may claim about its own failure; anything else is "user".
# INFRA is deliberately absent: the infra exit makes the platform retry the whole run, so
# only failures the supervisor observes itself (bootstrap, its own transfers) may use it.
_WORKER_CLASSES: Final[Mapping[ErrorCode, FailureClass]] = {
    ErrorCode.RESOURCE_EXHAUSTED: FailureClass.RESOURCE_EXHAUSTED,
    ErrorCode.INTEGRITY_FAILURE: FailureClass.INTEGRITY,
    ErrorCode.SANDBOX_VIOLATION: FailureClass.SANDBOX,
}
_LIB_DISTS: Final = (
    "mapi-runner",
    "mapi-spec",
    "mapi-sdk",
    "mapi-plots",
    "mapi-steps",
    "mapi-pymc",
    "mapi-sklearn",
    "numpy",
    "xarray",
    "pandas",
    "zarr",
    "pymc",
    "pytensor",
    "scikit-learn",
    "xgboost",
)
_READ_BYTES: Final = 64 * 1024
_MEDIA_DIR: Final = "media"
_MAX_MEDIA_WARNINGS: Final = 10
"""``MEDIA_INVALID`` / ``MEDIA_UPLOAD_FAILED`` warnings per attempt (later ones are dropped)."""


class Termination(StrEnum):
    SIGTERM = "sigterm"
    DEADLINE = "deadline"


@dataclass(slots=True)
class SupervisorConfig:
    """How the worker is started and how long things may take."""

    worker_python: str = sys.executable
    worker_registry: str | None = None
    """``module:callable`` returning the worker's plugin registry (default: entry points)."""
    scratch_root: str | None = None
    keep_scratch: bool = False
    term_grace_s: float = 5.0
    """Time the worker gets to stop after SIGTERM before it is killed."""
    deadline_reserve_s: float = 15.0
    """Time kept before the deadline for the checkpoint flush and the partial manifest."""
    scratch_check_s: float = 2.0
    base_image: str | None = None
    overlay_hash: str | None = None


@dataclass(slots=True)
class _Failure:
    failure_class: FailureClass
    message: str
    untrusted_text: str | None = None
    errors: list[Issue] = field(default_factory=list)
    """Typed issues the worker reported (their text is untrusted)."""


@dataclass(slots=True)
class _Pending:
    """Mutable run state shared with the checkpoint upload thread."""

    checkpoints: dict[str, ManifestObject] = field(default_factory=dict)
    async_failure: _Failure | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class _Media:
    """Media accounting of this attempt (the loop thread; ``uploaded`` under the pending lock)."""

    items: int = 0
    bytes: int = 0
    names: set[str] = field(default_factory=set)
    """Verified names submitted for upload (the same bytes are uploaded once)."""
    uploaded: dict[str, tuple[Digest, ImageInfo | None]] = field(default_factory=dict)
    warned: dict[str, int] = field(default_factory=dict)


class _ConfigError(Exception):
    """Unusable environment or bootstrap object (exit ``CONFIG_ERROR``)."""


_POINTER_MAX = 512


def _typed_issues(err: ErrorMessage) -> list[Issue]:
    """The worker's issues as typed errors of the run (``ErrorPayload.errors``): the code
    and pointer are kept (clients branch on the code, e.g. ``INGEST_UNSAFE_FILE``), the
    message is the code's platform title and the worker's text goes to ``untrusted_text``."""
    out: list[Issue] = []
    for issue in [i for i in err.issues if i.severity.value == "error"][:MAX_ERROR_ISSUES]:
        text = issue.message
        if issue.untrusted_text:
            text = f"{text}\n{issue.untrusted_text}"
        pointer = issue.pointer
        if not pointer.startswith("/") or len(pointer) > _POINTER_MAX:
            pointer = ""
        out.append(
            Issue(
                code=issue.code,
                pointer=clean_text(pointer),
                message=ERROR_INFO[issue.code].title,
                untrusted_text=clean_text(text, multiline=True)[:8000] or None,
            )
        )
    return out


class Supervisor:
    """Runs one execution; :meth:`run` returns the process exit code."""

    def __init__(
        self,
        config: SupervisorConfig,
        environ: Mapping[str, str],
        *,
        event_fd: int,
        client: httpx.Client | None = None,
        isolation: IsolationPlan | None = None,
    ) -> None:
        self.config = config
        self.environ = dict(environ)
        self.event_fd = event_fd
        self.client = client or new_client()
        self.transfers = Transfers(self.client)
        self.isolation = isolation or plan_isolation()
        self.rs: RunSpec | None = None
        self.emitter: EventEmitter | None = None
        self.progress: ProgressCoalescer | None = None
        self.logs: LogGate | None = None
        self.tracking: TrackingGate | None = None
        self.scratch: str | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.prefix = ""
        self.artifacts: PostPolicy | None = None
        self.manifest_put: SignedPut | None = None
        self.ckpt_policy: PostPolicy | None = None
        self.media_policy: PostPolicy | None = None
        self.media = _Media()
        self.terminating: Termination | None = None
        self.failure: _Failure | None = None
        self.worker_result: ResultMessage | None = None
        self.worker_error: ErrorMessage | None = None
        self.pending = _Pending()
        self._uploads: ThreadPoolExecutor | None = None
        self._futures: list[Future[None]] = []
        self._chan: socket.socket | None = None
        self._wake_r = -1
        self._wake_w = -1
        self._signals: list[int] = []
        self.diag = StderrLog(trace_id=trace_id_from_env(self.environ))

    # ================================================================ entry point
    def run(self) -> int:
        try:
            return int(self._run())
        except _ConfigError as exc:
            self._stderr(f"configuration error: {exc}")
            return int(RunnerExit.CONFIG_ERROR)
        except Exception as exc:  # a supervisor bug: report it, never hang
            self._stderr(f"supervisor failure: {type(exc).__name__}: {exc}")
            if self.emitter is not None:
                with contextlib.suppress(Exception):
                    self._emit_error(_Failure(FailureClass.INFRA, "the supervisor failed"))
            return int(RunnerExit.INFRA_ERROR)
        finally:
            self._cleanup()

    def _run(self) -> RunnerExit:
        self._install_signals()  # first: a SIGTERM during bootstrap is handled, not fatal
        rs = self._bootstrap()
        self.rs = rs
        self.diag.bind(run_id=rs.run_id, attempt=rs.attempt)
        self.emitter = EventEmitter(
            self.event_fd,
            run_id=rs.run_id,
            attempt=rs.attempt,
            key=rs.hmac_key_bytes(),
            max_event_bytes=rs.limits.max_event_bytes,
        )
        self.progress = ProgressCoalescer(self._emit, rs.limits.progress_interval_s)
        self.logs = LogGate(
            self._emit,
            line_bytes=rs.limits.log_line_bytes,
            total_bytes=rs.limits.log_total_bytes,
        )
        self.tracking = TrackingGate(
            self._emit, rs.limits.tracking, max_event_bytes=rs.limits.max_event_bytes
        )
        self._state(RunnerPhase.BOOTSTRAP, "bootstrap object read and deleted")
        if self.isolation.dev_mode and self.environ.get(ENV_REQUIRE_ISOLATION) == "1":
            # the executor promised a uid split: never run the worker under our own uid
            return self._finish_failure(
                _Failure(
                    FailureClass.SANDBOX,
                    "the executor needs worker isolation, but it is not available: "
                    + str(self.isolation.reason),
                )
            )
        if self.isolation.dev_mode:
            self._emit(
                WarningPayload(
                    code="DEV_MODE_ISOLATION",
                    message="worker isolation is reduced (development mode): "
                    + str(self.isolation.reason),
                )
            )
            self._stderr(f"WARNING: dev mode: {self.isolation.reason}")
        try:
            self._resolve_outputs(rs)
        except _ConfigError as exc:
            self._emit_error(_Failure(FailureClass.INFRA, f"invalid RunSpec outputs: {exc}"))
            return RunnerExit.CONFIG_ERROR
        remaining = (rs.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return self._finish_failure(
                _Failure(FailureClass.TIMEOUT, "the run deadline passed before the run started")
            )
        if self._signals:
            self._emit_error(_Failure(FailureClass.TIMEOUT, "the run stopped before it started"))
            return RunnerExit.TERMINATED
        self._prepare_scratch()
        self._spawn_worker(rs)
        self._loop(remaining)
        return self._finalize()

    # ================================================================== bootstrap
    def _bootstrap(self) -> RunSpec:
        try:
            env = RunnerEnv.from_environ(self.environ)
        except (ValueError, ValidationError) as exc:
            raise _ConfigError(f"bad runner environment: {_short(exc)}") from None
        try:
            obj = fetch_and_delete(env, self.client)
        except BootstrapError as exc:
            raise _ConfigError(exc.detail) from None
        try:
            rs = RunSpec.model_validate_json(obj.data)
        except ValidationError as exc:
            # never echo the document: it holds the HMAC key and signed URLs
            raise _ConfigError(
                f"the bootstrap object is not a valid RunSpec ({exc.error_count()} errors)"
            ) from None
        if rs.run_id != env.run_id or rs.nonce != env.nonce:
            raise _ConfigError("the bootstrap object belongs to a different run or nonce")
        return rs

    def _resolve_outputs(self, rs: RunSpec) -> None:
        manifest = rs.outputs[OUTPUT_MANIFEST]
        assert isinstance(manifest, SignedPut)  # enforced by RunSpec
        if not manifest.name.endswith("/" + MANIFEST_OBJECT_NAME):
            raise _ConfigError(f"outputs[{OUTPUT_MANIFEST!r}] must end in /{MANIFEST_OBJECT_NAME}")
        self.manifest_put = manifest
        self.prefix = manifest.name[: -len(MANIFEST_OBJECT_NAME)]
        artifacts = rs.outputs.get(OUTPUT_ARTIFACTS)
        if not isinstance(artifacts, PostPolicy):
            raise _ConfigError(f"outputs[{OUTPUT_ARTIFACTS!r}] must be a POST policy")
        if artifacts.prefix != self.prefix:
            raise _ConfigError("the artifacts POST policy must cover the attempt prefix")
        self.artifacts = artifacts
        ckpt = rs.outputs.get(OUTPUT_CHECKPOINTS)
        self.ckpt_policy = ckpt if isinstance(ckpt, PostPolicy) else None
        media = rs.outputs.get(OUTPUT_MEDIA)
        self.media_policy = media if isinstance(media, PostPolicy) else None

    # ===================================================================== worker
    def _prepare_scratch(self) -> None:
        root = tempfile.mkdtemp(prefix="mapi-run-", dir=self.config.scratch_root)
        self.scratch = root
        for sub in ("inputs", "outputs", "checkpoints", _MEDIA_DIR, "work"):
            os.mkdir(os.path.join(root, sub), 0o700)
        if self.isolation.drop_privileges:
            for path in (root, *(os.path.join(root, s) for s in os.listdir(root))):
                os.chown(path, WORKER_UID, WORKER_GID)
            os.chmod(root, 0o700)

    def _spawn_worker(self, rs: RunSpec) -> None:
        assert self.scratch is not None
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self._chan = parent
        os.set_inheritable(child.fileno(), True)
        argv = [
            self.config.worker_python,
            "-I",
            "-u",
            "-m",
            "mapi_runner.worker",
            "--channel-fd",
            str(child.fileno()),
        ]
        if self.config.worker_registry:
            argv += ["--registry", self.config.worker_registry]
        work = os.path.join(self.scratch, "work")
        env = worker_environ(os.environ)
        env["TMPDIR"] = work
        # M4: libhdf5 must never load a filter plugin a hostile upload names: point its plugin
        # search path at an empty directory the supervisor owns (the worker also empties the
        # path in-process before opening any HDF5 file); no file locking on scratch uploads.
        plugins = os.path.join(self.scratch, "hdf5-plugins")
        os.makedirs(plugins, mode=0o555, exist_ok=True)
        env["HDF5_PLUGIN_PATH"] = plugins
        env["HDF5_USE_FILE_LOCKING"] = "FALSE"
        if self.isolation.drop_privileges:
            env["HOME"] = work
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(child.fileno(),),
            cwd=work,
            env=env,
            start_new_session=True,
            close_fds=True,
            user=self.isolation.user,
            group=self.isolation.group,
            extra_groups=self.isolation.extra_groups,
            preexec_fn=self.isolation.preexec(),
        )
        child.close()
        start = StartMessage(
            run_id=rs.run_id,
            attempt=rs.attempt,
            kind=rs.kind,
            tenant_id=rs.tenant_id,
            project_id=rs.project_id,
            spec=rs.spec,
            inputs=rs.inputs,
            resume=rs.resume if self.ckpt_policy is not None else {},
            scratch=self.scratch,
            scratch_bytes=rs.limits.scratch_bytes,
            max_output_bytes=rs.limits.max_output_bytes,
            max_output_objects=rs.limits.max_output_objects,
            checkpointing=self.ckpt_policy is not None,
            deadline=rs.deadline,
            labels=rs.labels,
            tracking=rs.limits.tracking,
            media=self.media_policy is not None,
        )

        def send() -> None:
            # if the worker died at once, its exit status explains why
            with contextlib.suppress(OSError):
                send_frame(parent, start, max_bytes=MAX_START_FRAME)

        # a thread, so a worker that writes output before reading cannot deadlock us
        threading.Thread(target=send, name="start-frame", daemon=True).start()

    def _kill_worker(self, sig: int = signal.SIGKILL) -> None:
        proc = self.proc
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)

    # ======================================================================= loop
    def _install_signals(self) -> None:
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, _frame: FrameType | None) -> None:
            self._signals.append(signum)
            with contextlib.suppress(OSError):
                os.write(self._wake_w, b"\0")

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, handler)

    def _loop(self, remaining: float) -> None:
        assert self.proc is not None and self.proc.stdout and self.proc.stderr and self.rs
        assert self._chan is not None
        cfg = self.config
        now = time.monotonic()
        reserve = min(cfg.deadline_reserve_s, max(remaining / 4, 0.0))
        deadline_at = now + remaining - reserve
        kill_at: float | None = None
        next_scratch = now + cfg.scratch_check_s
        frames = FrameReader(MAX_WORKER_FRAME)
        sel = selectors.DefaultSelector()
        streams = {
            self._chan.fileno(): "channel",
            self.proc.stdout.fileno(): "stdout",
            self.proc.stderr.fileno(): "stderr",
        }
        for fd in streams:
            sel.register(fd, selectors.EVENT_READ)
        sel.register(self._wake_r, selectors.EVENT_READ)
        stop_reading_at: float | None = None
        try:
            while True:
                now = time.monotonic()
                timeouts = [next_scratch - now, 1.0]
                if self.terminating is None:
                    timeouts.append(deadline_at - now)
                if stop_reading_at is not None:
                    timeouts.append(stop_reading_at - now)
                due = self.progress.due_in() if self.progress else None
                if due is not None:
                    timeouts.append(due)
                if kill_at is not None:
                    timeouts.append(kill_at - now)
                for key, _ in sel.select(max(0.0, min(timeouts))):
                    fd = int(key.fd)
                    if fd == self._wake_r:
                        with contextlib.suppress(OSError):
                            os.read(self._wake_r, 1024)
                        continue
                    try:
                        data = os.read(fd, _READ_BYTES)
                    except OSError:
                        data = b""
                    name = streams[fd]
                    if not data:
                        sel.unregister(fd)
                        del streams[fd]
                        if name != "channel" and self.logs is not None and self.failure is None:
                            self.logs.close(name)  # the last, unterminated line
                        continue
                    if self.failure is not None:
                        continue  # the worker is being killed: drain, do not process
                    try:
                        if name == "channel":
                            for body in frames.feed(data):
                                self._handle(parse_worker_message(body), len(body))
                        else:
                            assert self.logs is not None
                            self.logs.feed(name, data)
                    except (ProtocolError, LogFlood, MapiError) as exc:  # TrackingFlood too
                        self._violation(exc)
                # ------------------------------------------------ periodic checks
                now = time.monotonic()
                if self.progress is not None:
                    self.progress.tick()
                with self.pending.lock:
                    async_failure = self.pending.async_failure
                if async_failure is not None and self.failure is None:
                    self.failure = async_failure
                    self._kill_worker()
                if self._signals and self.terminating is None:
                    self._terminate(Termination.SIGTERM)
                    kill_at = now + cfg.term_grace_s
                if now >= deadline_at and self.terminating is None and self.proc.poll() is None:
                    self._terminate(Termination.DEADLINE)
                    kill_at = now + min(cfg.term_grace_s, reserve / 2 if reserve else 0)
                if kill_at is not None and now >= kill_at and self.proc.poll() is None:
                    self._kill_worker()
                    kill_at = None
                if now >= next_scratch:
                    next_scratch = now + cfg.scratch_check_s
                    self._check_scratch()
                if self.failure is not None and stop_reading_at is None:
                    stop_reading_at = now + 2.0
                exited = self.proc.poll() is not None
                if exited and not streams:
                    break
                if exited and stop_reading_at is not None and now >= stop_reading_at:
                    break  # a lingering grandchild holds the pipes open
                if exited and stop_reading_at is None:
                    stop_reading_at = now + 5.0
        finally:
            sel.close()
            self._kill_worker()  # nothing of the worker's session may outlive it
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
            if frames.pending and self.failure is None:
                self._violation(ProtocolError("the worker closed the channel mid-frame"))

    def _terminate(self, why: Termination) -> None:
        self.terminating = why
        what = "SIGTERM received" if why is Termination.SIGTERM else "the run deadline is near"
        self._emit(
            WarningPayload(
                code="RUN_TERMINATING",
                message=f"{what}: stopping the worker and flushing checkpoints",
            )
        )
        self._kill_worker(signal.SIGTERM)

    def _violation(self, exc: Exception) -> None:
        if self.failure is not None:
            return
        detail = exc.detail if isinstance(exc, MapiError) else str(exc)
        if isinstance(exc, MapiError) and exc.code is ErrorCode.RESOURCE_EXHAUSTED:
            self.failure = _Failure(FailureClass.RESOURCE_EXHAUSTED, detail)
        else:
            self.failure = _Failure(FailureClass.SANDBOX, f"sandbox violation: {detail}")
        self._kill_worker()

    def _check_scratch(self) -> None:
        rs = self.rs
        if rs is None or rs.limits.scratch_bytes is None or self.scratch is None:
            return
        used = _du(self.scratch)
        if used > rs.limits.scratch_bytes and self.failure is None:
            self.failure = _Failure(
                FailureClass.RESOURCE_EXHAUSTED,
                f"the scratch space uses {used} bytes. This is more than the "
                f"{rs.limits.scratch_bytes}-byte budget",
            )
            self._kill_worker()

    # ================================================================ messages
    def _handle(self, msg: WorkerMessage, nbytes: int = 0) -> None:
        if self.worker_result is not None or self.worker_error is not None:
            raise ProtocolError("the worker sent a message after its final result")
        if isinstance(msg, MetricsMessage | ParamsMessage | TagsMessage | UserLogMessage):
            # never through the progress coalescer: every point counts, within the budgets
            assert self.tracking is not None
            self.tracking.feed(msg, nbytes)
            return
        if isinstance(msg, StateMessage):
            self._state(msg.payload.phase, msg.payload.message)
        elif isinstance(msg, ProgressMessage):
            assert self.progress is not None
            self.progress.offer(_clean(msg.payload))
        elif isinstance(msg, DiagnosticMessage | WarningMessage | MetricMessage):
            self._emit(_clean(msg.payload))
        elif isinstance(msg, CheckpointMessage):
            self._checkpoint(msg)
        elif isinstance(msg, MediaMessage):
            self._media(_clean(msg))
        elif isinstance(msg, ResultMessage):
            self.worker_result = msg
        elif isinstance(msg, ErrorMessage):
            self.worker_error = msg

    def _checkpoint(self, msg: CheckpointMessage) -> None:
        policy = self.ckpt_policy
        if policy is None or self.scratch is None:
            self._emit(
                WarningPayload(
                    code="CHECKPOINT_IGNORED",
                    message="the worker reported a checkpoint but this run has no checkpoint "
                    "prefix",
                )
            )
            return
        # open every file now, so the pair uploaded belongs to one save
        dir_fd = os.open(os.path.join(self.scratch, "checkpoints"), os.O_RDONLY | os.O_NOFOLLOW)
        opened: list[tuple[str, int, int]] = []
        try:
            for name in msg.files:
                fd, size = open_regular(dir_fd, name)
                opened.append((name, fd, size))
        except OutputViolation:
            for _, fd, _ in opened:
                os.close(fd)
            raise
        finally:
            os.close(dir_fd)
        self._futures.append(
            self._uploads_pool().submit(
                self._upload_checkpoint, policy, opened, msg.segment, msg.draws_done
            )
        )

    def _upload_checkpoint(
        self,
        policy: PostPolicy,
        files: list[tuple[str, int, int]],
        segment: int,
        draws_done: int | None,
    ) -> None:
        try:
            for name, fd, size in files:
                key = policy.prefix + name
                digest = self.transfers.post_file(
                    policy, key, fd, size, content_type=_ckpt_type(name)
                )
                obj = ManifestObject(
                    key=key,
                    size=digest.size,
                    crc32c=digest.crc32c,
                    md5=digest.md5,
                    content_type=_ckpt_type(name),
                )
                with self.pending.lock:
                    self.pending.checkpoints[name] = obj
                self._emit(
                    CheckpointPayload(
                        name=name,
                        size=digest.size,
                        crc32c=digest.crc32c,
                        segment=segment,
                        draws_done=draws_done,
                    )
                )
        except TransferError as exc:
            self._emit(
                WarningPayload(
                    code="CHECKPOINT_UPLOAD_FAILED",
                    message="the server could not upload a checkpoint. The run continues",
                    untrusted_text=exc.untrusted_text,
                )
            )
        except MapiError as exc:  # outside the policy (size): the worker misbehaved
            with self.pending.lock:
                self.pending.async_failure = _Failure(
                    FailureClass.SANDBOX, f"sandbox violation: {exc.detail}"
                )
        finally:
            for _, fd, _ in files:
                with contextlib.suppress(OSError):
                    os.close(fd)

    # ==================================================================== media
    def _media_warning(
        self, code: str, message: str, untrusted_text: str | None = None, *, limit: int = 1
    ) -> None:
        """Emit warning ``code`` at most ``limit`` times per attempt."""
        n = self.media.warned.get(code, 0)
        if n >= limit:
            return
        self.media.warned[code] = n + 1
        self._emit(
            WarningPayload(
                code=code,
                message=message,
                untrusted_text=None
                if untrusted_text is None
                else clean_text(untrusted_text, multiline=True)[:8000],
            )
        )

    def _uploads_pool(self) -> ThreadPoolExecutor:
        # one thread, shared with checkpoints: uploads and their events keep the worker's order
        if self._uploads is None:
            self._uploads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ckpt-upload")
        return self._uploads

    def _media(self, msg: MediaMessage) -> None:
        """Verify one media file the worker wrote and queue its upload (never fails the run)."""
        policy, rs, st = self.media_policy, self.rs, self.media
        if policy is None or rs is None or self.scratch is None:
            self._drop_media_file(msg.file)
            self._media_warning(
                "MEDIA_IGNORED",
                "the run logged images or tables, but it has no media prefix. "
                "The platform does not keep them",
            )
            return
        lim = rs.limits.tracking
        if st.items >= lim.media_items:
            self._drop_media_file(msg.file)
            self._media_budget(lim.media_items, lim.media_bytes)
            return
        if msg.file in st.names:
            # the same bytes again (another key, step or label set): no second upload
            st.items += 1
            self._drop_media_file(msg.file)
            self._futures.append(self._uploads_pool().submit(self._media_again, msg))
            return
        data = self._read_media_file(msg.file, lim.media_item_bytes)
        if data is None:
            return
        sha = hashlib.sha256(data).hexdigest()
        try:
            info = check_media(msg.kind, msg.content_type, data)
            name = media_name(sha, msg.content_type)
        except ValueError as exc:
            self._media_invalid(f"the media item {msg.key!r} is not valid", str(exc))
            return
        if name != msg.file:
            self._media_invalid(
                f"the media item {msg.key!r} does not have the sha256 its file name gives"
            )
            return
        if st.bytes + len(data) > lim.media_bytes:
            self._media_budget(lim.media_items, lim.media_bytes)
            return
        st.items += 1
        st.bytes += len(data)
        st.names.add(name)
        self._futures.append(
            self._uploads_pool().submit(self._upload_media, policy, msg, name, data, info)
        )

    def _media_budget(self, items: int, nbytes: int) -> None:
        self._media_warning(
            "MEDIA_BUDGET",
            f"the run logged more media than its budget ({items} items, {nbytes} bytes). "
            "The platform does not keep the media after the budget",
        )

    def _media_invalid(self, message: str, untrusted_text: str | None = None) -> None:
        self._media_warning(
            "MEDIA_INVALID",
            f"{message}. The platform does not keep it",
            untrusted_text,
            limit=_MAX_MEDIA_WARNINGS,
        )

    def _read_media_file(self, name: str, max_bytes: int) -> bytes | None:
        """The bytes of ``media/<name>`` (no links, regular files only), then the file is
        deleted; ``None`` (with a ``MEDIA_INVALID`` warning) when it cannot be used."""
        assert self.scratch is not None
        try:
            dir_fd = os.open(os.path.join(self.scratch, _MEDIA_DIR), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            self._media_invalid("the media directory cannot be opened", exc.strerror)
            return None
        try:
            try:
                fd, size = open_regular(dir_fd, name)
            except OutputViolation as exc:
                self._media_invalid("a media file cannot be opened safely", exc.detail)
                return None
            try:
                if size > max_bytes:
                    self._media_invalid(
                        f"a media file has {size} bytes, more than the {max_bytes}-byte limit"
                    )
                    return None
                data = _read_fd(fd, max_bytes + 1)
            finally:
                os.close(fd)
            if len(data) > max_bytes:
                self._media_invalid(f"a media file has more than {max_bytes} bytes")
                return None
            return data
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)
            os.close(dir_fd)

    def _drop_media_file(self, name: str) -> None:
        if self.scratch is None:
            return
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(self.scratch, _MEDIA_DIR, name))

    def _upload_media(
        self,
        policy: PostPolicy,
        msg: MediaMessage,
        name: str,
        data: bytes,
        info: ImageInfo | None,
    ) -> None:
        try:
            digest = self.transfers.post_bytes(
                policy, policy.prefix + name, data, content_type=msg.content_type
            )
        except MapiError as exc:  # TransferError too; the run continues without this item
            self._media_warning(
                "MEDIA_UPLOAD_FAILED",
                f"the server could not upload the media item {msg.key!r}. The run continues",
                exc.untrusted_text if isinstance(exc, TransferError) else exc.detail,
                limit=_MAX_MEDIA_WARNINGS,
            )
            return
        with self.pending.lock:
            self.media.uploaded[name] = (digest, info)
        self._emit_media(msg, name, digest, info)

    def _media_again(self, msg: MediaMessage) -> None:
        with self.pending.lock:
            done = self.media.uploaded.get(msg.file)
        if done is not None:  # else the first upload failed (already reported)
            self._emit_media(msg, msg.file, *done)

    def _emit_media(
        self, msg: MediaMessage, name: str, digest: Digest, info: ImageInfo | None
    ) -> None:
        self._emit(
            MediaPayload(
                key=msg.key,
                kind=msg.kind,
                content_type=msg.content_type,
                name=name,
                sha256=name.split(".", 1)[0],
                size=digest.size,
                crc32c=digest.crc32c,
                step=msg.step,
                labels=msg.labels,
                width=None if info is None else info.width,
                height=None if info is None else info.height,
                caption=msg.caption,
            )
        )

    # ================================================================= finalize
    def _finalize(self) -> RunnerExit:
        if self._uploads is not None:
            self._uploads.shutdown(wait=True)
        if self.progress is not None:
            self.progress.flush()
        with self.pending.lock:
            if self.failure is None and self.pending.async_failure is not None:
                self.failure = self.pending.async_failure
        if self.tracking is not None and (budget := self.tracking.warning()) is not None:
            self._emit(budget)  # once, before complete/error
        if self.failure is not None:
            return self._finish_failure(self.failure)
        if self.worker_result is not None:
            return self._complete("complete")
        if self.terminating is not None:
            return self._partial_or_timeout(self.terminating)
        if self.worker_error is not None:
            return self._finish_failure(self._from_worker_error(self.worker_error))
        assert self.proc is not None
        rc = self.proc.returncode
        if rc == -signal.SIGKILL:
            return self._finish_failure(
                _Failure(
                    FailureClass.RESOURCE_EXHAUSTED,
                    "the system stopped the worker (SIGKILL). Possibly the worker did not have "
                    "sufficient memory",
                )
            )
        return self._finish_failure(
            _Failure(
                FailureClass.USER,
                f"the worker stopped with status {rc} and did not send a result",
            )
        )

    def _from_worker_error(self, err: ErrorMessage) -> _Failure:
        fc = _WORKER_CLASSES.get(err.code, FailureClass.USER)
        for issue in err.issues[:20]:
            message = clean_text(issue.message)[:2000]
            self._emit(
                Diagnostic(
                    code=issue.code.value,
                    severity="error" if issue.severity.value == "error" else "warning",
                    message_expert=message,
                    message_plain=message,
                    suggested_action=None
                    if issue.suggested_action is None
                    else clean_text(issue.suggested_action)[:2000],
                )
            )
        detail = err.message if not err.untrusted_text else f"{err.message}\n{err.untrusted_text}"
        return _Failure(
            fc,
            f"the run failed with {err.code.value} (as reported by the worker)",
            untrusted_text=detail[:65536],
            errors=_typed_issues(err),
        )

    def _partial_or_timeout(self, why: Termination) -> RunnerExit:
        with self.pending.lock:
            ckpts = dict(self.pending.checkpoints)
        under = {name: obj for name, obj in ckpts.items() if obj.key.startswith(self.prefix)}
        if under:
            objects = {obj.key[len(self.prefix) :]: obj for obj in under.values()}
            return self._write_manifest("partial", objects)
        if why is Termination.SIGTERM:
            self._emit_error(
                _Failure(FailureClass.TIMEOUT, "the run stopped before the first checkpoint")
            )
            return RunnerExit.TERMINATED
        return self._finish_failure(
            _Failure(FailureClass.TIMEOUT, "the run reached its deadline without a checkpoint")
        )

    def _complete(self, status: str) -> RunnerExit:
        assert self.rs is not None and self.artifacts is not None and self.scratch is not None
        self._state(RunnerPhase.UPLOAD, "uploading outputs")
        with self.pending.lock:
            ckpts = {
                o.key[len(self.prefix) :]: o
                for o in self.pending.checkpoints.values()
                if o.key.startswith(self.prefix)
            }
        limits = self.rs.limits
        try:
            files = collect_outputs(
                os.path.join(self.scratch, "outputs"),
                max_objects=max(1, limits.max_output_objects - len(ckpts)),
            )
        except OutputViolation as exc:
            return self._finish_failure(_Failure(FailureClass.SANDBOX, exc.detail))
        # a complete run does not list its checkpoints: they stay in the run prefix (allowed
        # under checkpoints/) and are never copied into the bundle; they still count
        # against the output byte cap
        objects: dict[str, ManifestObject] = {}
        try:
            total = sum(f.size for f in files) + sum(o.size for o in ckpts.values())
            if total > limits.max_output_bytes:
                return self._finish_failure(
                    _Failure(
                        FailureClass.SANDBOX,
                        f"the outputs have a total of {total} bytes. The run can write a "
                        f"maximum of {limits.max_output_bytes}",
                    )
                )
            for f in files:
                key = self.prefix + f.name
                digest = self.transfers.post_file(
                    self.artifacts, key, f.fd, f.size, content_type=f.content_type
                )
                objects[f.name] = ManifestObject(
                    key=key,
                    size=digest.size,
                    crc32c=digest.crc32c,
                    md5=digest.md5,
                    content_type=f.content_type,
                )
        except TransferError as exc:
            return self._finish_failure(
                _Failure(FailureClass.INFRA, exc.detail, untrusted_text=exc.untrusted_text)
            )
        except MapiError as exc:
            return self._finish_failure(_Failure(FailureClass.SANDBOX, exc.detail))
        finally:
            for f in files:
                with contextlib.suppress(OSError):
                    os.close(f.fd)
        return self._write_manifest(status, objects)

    def _write_manifest(self, status: str, objects: dict[str, ManifestObject]) -> RunnerExit:
        rs, put = self.rs, self.manifest_put
        assert rs is not None and put is not None
        manifest = OutputManifest(
            tenant_id=rs.tenant_id,
            project_id=rs.project_id,
            run_id=rs.run_id,
            attempt=rs.attempt,
            kind=rs.kind,
            status=status,
            prefix=self.prefix,
            spec_hash=rs.spec_hash,
            catalog_digest=rs.catalog_digest,
            inputs={
                name: InputDigest(generation=g.generation, size=g.size, crc32c=g.crc32c)
                for name, g in rs.inputs.items()
            },
            objects=objects,
            runner_version=__version__,
            base_image=self.config.base_image,
            overlay_hash=self.config.overlay_hash,
            lib_versions=_lib_versions(),
            created_at=datetime.now(UTC),
        )
        signed = sign_output_manifest(manifest, rs.hmac_key_bytes())
        body = signed.model_dump_json().encode("utf-8")
        try:
            digest = self.transfers.put_bytes(put, body, content_type="application/json")
        except TransferError as exc:
            return self._finish_failure(
                _Failure(FailureClass.INFRA, exc.detail, untrusted_text=exc.untrusted_text)
            )
        except MapiError as exc:
            return self._finish_failure(_Failure(FailureClass.SANDBOX, exc.detail))
        self._emit(
            CompletePayload(
                status=status,
                manifest_name=put.name,
                manifest_crc32c=digest.crc32c,
                objects=len(objects),
                total_bytes=signed.total_bytes,
            )
        )
        self._state(RunnerPhase.DONE, f"{status} manifest uploaded")
        return RunnerExit.OK if status == "complete" else RunnerExit.PARTIAL

    def _finish_failure(self, failure: _Failure) -> RunnerExit:
        self._emit_error(failure)
        return EXIT_FOR_CLASS[failure.failure_class]

    def _emit_error(self, failure: _Failure) -> None:
        fc = failure.failure_class
        self._emit(
            ErrorPayload(
                code=fc.error_code,
                failure_class=fc,
                message=failure.message[:2000],
                untrusted_text=None
                if failure.untrusted_text is None
                else clean_text(failure.untrusted_text, multiline=True)[:65536],
                errors=failure.errors[:MAX_ERROR_ISSUES],
            )
        )

    # =================================================================== helpers
    def _emit(self, payload: BaseModel) -> int:
        assert self.emitter is not None
        return self.emitter.emit(payload)

    def _state(self, phase: RunnerPhase, message: str | None) -> None:
        if self.progress is not None:
            self.progress.flush()
        text = None if message is None else clean_text(message)[:500]
        self._emit(StatePayload(phase=phase, message=text))

    def _stderr(self, message: str, *, level: str = "info", **fields: object) -> None:
        self.diag.log(message, level=level, **fields)

    def _cleanup(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self._kill_worker()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
        if self.proc is not None:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        if self._chan is not None:
            self._chan.close()
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if self.scratch is not None and not self.config.keep_scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)


def _ckpt_type(name: str) -> str:
    return "application/json" if name.endswith(".json") else "application/zip"


def _read_fd(fd: int, limit: int) -> bytes:
    """Up to ``limit`` bytes of ``fd`` from its start."""
    parts: list[bytes] = []
    n = 0
    while n < limit:
        chunk = os.pread(fd, min(_READ_BYTES * 16, limit - n), n)
        if not chunk:
            break
        parts.append(chunk)
        n += len(chunk)
    return b"".join(parts)


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
    return str(exc)[:500]


def _du(root: str) -> int:
    total = 0
    for dirpath, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _lib_versions() -> dict[str, str]:
    out = {"python": ".".join(str(v) for v in sys.version_info[:3])}
    for dist in _LIB_DISTS:
        with contextlib.suppress(metadata.PackageNotFoundError):
            out[dist] = metadata.version(dist)
    return out
