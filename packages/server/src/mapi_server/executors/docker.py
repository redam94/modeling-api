"""``docker`` / ``docker-gvisor``: run the supervisor in a locked-down container (plan §8.4).

Each attempt is a fresh container from the base image that ships the run's model plugin
(``images`` maps the plugin namespace from :data:`~mapi_spec.executor.PLUGIN_LABEL` to an
image; everything else uses ``image``) with: no network (only usable with a non-network
bootstrap transport), the GCS shim's network namespace (``container:<shim>``, the compose
setup) or an internal network holding only the shim, a read-only root filesystem with a size-capped
``/tmp`` tmpfs, all capabilities dropped, ``no-new-privileges``, a non-root user, pids/CPU/
memory limits from the resource class, and optionally the gVisor runtime (``runsc``).
Identifiers travel in the environment only; the container's stdout is the event pipe.
The Docker SDK is synchronous, so every call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime
from typing import Any

from mapi_spec._types import Duration
from mapi_spec.executor import (
    PLUGIN_LABEL,
    ExecState,
    ExecStatus,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunSubmit,
)
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.run import InvalidEvent, RunnerEnv, decode_event_line

__all__ = ["LABEL_ATTEMPT", "LABEL_NONCE", "LABEL_RUN", "DockerExecutor", "docker_available"]

log = logging.getLogger(__name__)

LABEL_NONCE = "dev.mapi.nonce"
LABEL_RUN = "dev.mapi.run_id"
LABEL_ATTEMPT = "dev.mapi.attempt"
_TMPFS_FRACTION = 1.0  # the scratch budget is the tmpfs size


def docker_available() -> bool:
    """True when the docker SDK is installed and a daemon answers ``ping``."""
    try:
        import docker  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        return False
    try:
        client = docker.from_env(timeout=3)
        ok = bool(client.ping())
        client.close()
        return ok
    except Exception:
        return False


def _parse_ts(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00")[:26] + "+00:00")
    return None


class DockerExecutor:
    """Runs ``mapi-runner`` in a container per attempt (``runtime='runsc'`` for gVisor)."""

    def __init__(
        self,
        *,
        image: str,
        bootstrap_url_template: str,
        images: Mapping[str, str] | None = None,
        exec_token: str | None = None,
        command: list[str] | None = None,
        network: str = "none",
        gvisor: bool = False,
        gvisor_runtime: str = "runsc",
        user: str = "10001:10001",
        cancel_grace_s: float = 10.0,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        RunnerEnv(run_id="probe", nonce="0" * 64, bootstrap_url=bootstrap_url_template)
        self.name = "docker-gvisor" if gvisor else "docker"
        self._image = image
        self._images = dict(images or {})
        self._template = bootstrap_url_template
        self._token = exec_token
        self._command = command
        self._network = network
        self._gvisor = gvisor
        self._runtime = gvisor_runtime
        self._user = user
        self._grace = cancel_grace_s
        self._client_factory = client_factory or self._default_client
        self._client: Any = None
        self._events: asyncio.Queue[tuple[str, RunEvent]] = asyncio.Queue()
        self._pending: dict[str, int] = {}
        """Events per nonce queued or handed out but not yet processed by the consumer."""
        self._consumers = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._followers: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._stopping: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def _default_client() -> Any:
        try:
            import docker  # type: ignore[import-untyped,unused-ignore]
        except ImportError as exc:
            raise RuntimeError(
                "the docker executor needs `pip install mapi-server[docker]`"
            ) from exc
        return docker.from_env()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # ------------------------------------------------------------------ protocol
    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=Isolation.GVISOR if self._gvisor else Isolation.PROCESS,
            egress_enforced=self._network == "none",
            dns_enforced=self._network == "none",
            max_runtime=Duration(24 * 3600),
            classes=[ResourceClass.S, ResourceClass.M, ResourceClass.L],
            max_tasks=1,
            checkpoint_storage=True,
        )

    def image_for(self, rs: RunSubmit) -> str:
        """The base image that ships the run's plugin (the default for plugin-less runs)."""
        return self._images.get(rs.labels.get(PLUGIN_LABEL, ""), self._image)

    def container_options(self, rs: RunSubmit, nonce: str) -> dict[str, Any]:
        """Keyword arguments for ``containers.run`` (pure; unit-tested without a daemon)."""
        spec = RESOURCE_CLASSES[rs.resource_class]
        env = RunnerEnv(
            run_id=rs.run_id, nonce=nonce, bootstrap_url=self._template, exec_token=self._token
        ).to_environ()
        env["PYTHONUNBUFFERED"] = "1"
        scratch = spec.scratch_bytes or 1024**3
        opts: dict[str, Any] = {
            "image": self.image_for(rs),
            "environment": env,
            "detach": True,
            "network_mode": self._network,
            "read_only": True,
            "tmpfs": {"/tmp": f"rw,noexec,nosuid,size={int(scratch * _TMPFS_FRACTION)}"},
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "user": self._user,
            "pids_limit": 512,
            "labels": {LABEL_NONCE: nonce, LABEL_RUN: rs.run_id, LABEL_ATTEMPT: str(rs.attempt)},
            "stdin_open": False,
            "tty": False,
            "init": True,
        }
        if spec.ram_bytes is not None:
            opts["mem_limit"] = spec.ram_bytes
            opts["memswap_limit"] = spec.ram_bytes
        if spec.vcpu is not None:
            opts["nano_cpus"] = spec.vcpu * 1_000_000_000
        if self._command:
            opts["command"] = list(self._command)
        if self._gvisor:
            opts["runtime"] = self._runtime
        return opts

    async def submit(self, rs: RunSubmit, nonce: str) -> Handle:
        self._loop = asyncio.get_running_loop()
        existing = await self.adopt(nonce)
        if existing is not None:
            return existing
        opts = self.container_options(rs, nonce)
        container = await asyncio.to_thread(self.client.containers.run, **opts)
        handle = Handle(
            executor=self.name,
            run_id=rs.run_id,
            attempt=rs.attempt,
            nonce=nonce,
            external_id=str(container.id),
        )
        self._follow(handle, container)
        return handle

    async def adopt(self, nonce: str) -> Handle | None:
        self._loop = asyncio.get_running_loop()
        found = await asyncio.to_thread(
            self.client.containers.list, all=True, filters={"label": f"{LABEL_NONCE}={nonce}"}
        )
        if not found:
            return None
        container = found[0]
        labels = container.labels or {}
        handle = Handle(
            executor=self.name,
            run_id=labels.get(LABEL_RUN, "unknown"),
            attempt=int(labels.get(LABEL_ATTEMPT, "1")),
            nonce=nonce,
            external_id=str(container.id),
        )
        self._follow(handle, container)
        return handle

    async def status(self, h: Handle) -> ExecStatus:
        if h.external_id is None:
            return ExecStatus(state=ExecState.UNKNOWN, message="no container id")
        try:
            container = await asyncio.to_thread(self.client.containers.get, h.external_id)
            await asyncio.to_thread(container.reload)
        except Exception as exc:
            if type(exc).__name__ == "NotFound":
                return ExecStatus(state=ExecState.UNKNOWN, message="container not found")
            raise
        st = container.attrs.get("State", {})
        status = st.get("Status")
        started = _parse_ts(st.get("StartedAt"))
        if status in ("created", "restarting"):
            return ExecStatus(state=ExecState.PENDING, started_at=started)
        if status in ("running", "paused"):
            return ExecStatus(state=ExecState.RUNNING, started_at=started)
        follower = self._followers.get(h.nonce)
        if (follower is not None and follower.is_alive()) or (
            self._consumers > 0 and self._pending.get(h.nonce, 0) > 0
        ):
            # still draining stdout, or its events are not ingested yet (complete is last)
            return ExecStatus(state=ExecState.RUNNING, started_at=started)
        code = int(st.get("ExitCode", 1))
        if st.get("OOMKilled"):
            code = 137
        cancelled = h.nonce in self._cancelled
        state = (
            ExecState.CANCELLED
            if cancelled
            else (ExecState.SUCCEEDED if code == 0 else ExecState.FAILED)
        )
        return ExecStatus(
            state=state,
            exit_code=code,
            started_at=started,
            finished_at=_parse_ts(st.get("FinishedAt")),
            message=(st.get("Error") or None),
        )

    async def cancel(self, h: Handle) -> None:
        """Start ``docker stop`` (SIGTERM, SIGKILL after the grace) without waiting for it.

        ``container.stop`` blocks for up to the grace period; the reconciler cancels inline
        in its serial pass, so the stop runs in a background task and the next pass
        observes the exit. Repeated calls while a stop is in flight do nothing.
        """
        if h.external_id is None:
            return
        self._cancelled.add(h.nonce)
        pending = self._stopping.get(h.nonce)
        if pending is not None and not pending.done():
            return
        try:
            container = await asyncio.to_thread(self.client.containers.get, h.external_id)
        except Exception as exc:
            if type(exc).__name__ == "NotFound":
                return
            raise
        task = asyncio.create_task(
            asyncio.to_thread(container.stop, timeout=int(self._grace)),
            name=f"docker-stop-{h.run_id}",
        )
        task.add_done_callback(lambda t: self._stopped(h, t))
        self._stopping[h.nonce] = task

    def _stopped(self, h: Handle, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.warning("docker stop of run %s failed: %r", h.run_id, task.exception())

    def event_source(self) -> AsyncIterator[RunEvent]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[RunEvent]:
        self._consumers += 1
        try:
            while True:
                nonce, event = await self._events.get()
                try:
                    yield event
                finally:  # the consumer is back for the next one: this one is processed
                    left = self._pending.get(nonce, 0) - 1
                    if left > 0:
                        self._pending[nonce] = left
                    else:
                        self._pending.pop(nonce, None)
        finally:
            self._consumers -= 1

    def _enqueue(self, nonce: str, event: RunEvent) -> None:
        """Runs on the event loop (``call_soon_threadsafe`` from a follower thread)."""
        self._pending[nonce] = self._pending.get(nonce, 0) + 1
        self._events.put_nowait((nonce, event))

    # ------------------------------------------------------------------ internals
    def _follow(self, handle: Handle, container: Any) -> None:
        if handle.nonce in self._followers and self._followers[handle.nonce].is_alive():
            return
        loop = self._loop
        if loop is None:
            raise RuntimeError("submit/adopt must be called from a running event loop")

        def pump() -> None:
            buf = b""
            for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=False):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch(loop, handle, line)
            if buf:
                self._dispatch(loop, handle, buf)

        t = threading.Thread(target=pump, name=f"docker-logs-{handle.run_id}", daemon=True)
        self._followers[handle.nonce] = t
        t.start()

    def _dispatch(self, loop: asyncio.AbstractEventLoop, handle: Handle, line: bytes) -> None:
        try:
            event = decode_event_line(line.rstrip(b"\r"))
        except InvalidEvent as exc:
            log.warning("invalid event line from run %s: %s", handle.run_id, exc.detail)
            return
        if event is None or event.run_id != handle.run_id or event.attempt != handle.attempt:
            return
        loop.call_soon_threadsafe(self._enqueue, handle.nonce, event)
