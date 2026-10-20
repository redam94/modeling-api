"""The worker's side of the run: channel client, scratch layout, inputs and hooks."""

from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import (
    ChainProgress,
    MetricPayload,
    ProgressPayload,
    RunnerPhase,
    StatePayload,
    WarningPayload,
)
from mapi_spec.storage import SignedGet, check_object_name
from pydantic import BaseModel

from mapi_runner.channel import (
    CheckpointMessage,
    DiagnosticMessage,
    MetricMessage,
    ProgressMessage,
    StartMessage,
    StateMessage,
    WarningMessage,
    send_frame,
)
from mapi_runner.transfer import Transfers, new_client

if TYPE_CHECKING:
    from mapi_runner.worker.tracking import WorkerTracker

__all__ = ["WorkerContext", "load_registry"]

_PROGRESS_MIN_INTERVAL = 0.25


def load_registry(ref: str | None) -> Any:
    """The plugin registry: entry points by default, or ``module:callable``."""
    from mapi_sdk.registry import default_registry

    if not ref:
        return default_registry()
    module, _, attr = ref.partition(":")
    if not module or not attr:
        raise ValueError("--registry must look like module:callable")
    factory = getattr(importlib.import_module(module), attr)
    return factory()


class WorkerContext:
    """What a run kind implementation uses: inputs, outputs, events, hooks, registry."""

    def __init__(
        self,
        start: StartMessage,
        sock: socket.socket,
        *,
        cancel: Any,
        registry_ref: str | None = None,
    ) -> None:
        self.start = start
        self._sock = sock
        self._send_lock = threading.Lock()
        self.cancel = cancel
        self._registry_ref = registry_ref
        self._registry: Any = None
        root = Path(start.scratch)
        self.inputs_dir = root / "inputs"
        self.outputs_dir = root / "outputs"
        self.checkpoints_dir = root / "checkpoints"
        self.media_dir = root / "media"
        """Where the tracker writes logged media for the supervisor (v0.14)."""
        self.work_dir = root / "work"
        self._transfers: Transfers | None = None
        self._last_progress = 0.0
        self._last_fraction: float | None = None
        self._last_message = ""
        self._chains: list[ChainProgress] = []
        self._tracker: WorkerTracker | None = None
        self._tracker_lock = threading.Lock()
        self._phase = RunnerPhase.BOOTSTRAP
        self.total_written = 0

    # -------------------------------------------------------------------- channel
    def _send(self, msg: BaseModel) -> None:
        with self._send_lock:
            send_frame(self._sock, msg)  # type: ignore[arg-type]

    def send(self, msg: BaseModel) -> None:
        """Send one message (thread-safe: the tracker flushes from a background thread)."""
        self._send(msg)

    def state(self, phase: RunnerPhase, message: str | None = None) -> None:
        self._phase = phase
        self._send(
            StateMessage(
                payload=StatePayload(
                    phase=phase, message=None if message is None else message[:500]
                )
            )
        )

    def progress(self, fraction: float | None, message: str = "", *, force: bool = False) -> None:
        """Report progress (at most one message per 0.25 s); carries the latest chains."""
        self._last_fraction, self._last_message = fraction, message
        now = time.monotonic()
        if not force and now - self._last_progress < _PROGRESS_MIN_INTERVAL and fraction != 1.0:
            return
        self._last_progress = now
        frac = None if fraction is None else min(max(float(fraction), 0.0), 1.0)
        self._send(
            ProgressMessage(
                payload=ProgressPayload(
                    phase=self._phase, fraction=frac, message=message[:500], chains=self._chains
                )
            )
        )

    def set_chains(self, chains: Sequence[ChainProgress]) -> None:
        """Keep the newest per-chain progress and send it with a (throttled) progress message
        that repeats the last fraction and message. Any thread may call it."""
        self._chains = [
            c if isinstance(c, ChainProgress) else ChainProgress.model_validate(c)
            for c in list(chains)[:64]
        ]
        self.progress(self._last_fraction, self._last_message)

    def warning(self, code: str, message: str, untrusted_text: str | None = None) -> None:
        self._send(
            WarningMessage(
                payload=WarningPayload(
                    code=code,
                    message=message[:2000],
                    untrusted_text=None if untrusted_text is None else untrusted_text[:8000],
                )
            )
        )

    def diagnostic(self, diagnostic: Diagnostic) -> None:
        self._send(DiagnosticMessage(payload=diagnostic))

    def metric(
        self,
        name: str,
        value: float,
        *,
        step: int | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        """A legacy scalar ``metric`` event (end-of-run metrics such as holdout errors)."""
        self._send(
            MetricMessage(
                payload=MetricPayload(name=name, value=value, step=step, labels=labels or {})
            )
        )

    def checkpoint(self, files: list[str], segment: int, draws_done: int | None) -> None:
        self._send(CheckpointMessage(files=files, segment=segment, draws_done=draws_done))

    def log(self, message: str) -> None:
        """Harness log line (goes through the captured stderr like any other output)."""
        print(message, file=sys.stderr, flush=True)

    # ------------------------------------------------------------------- tracking
    @property
    def tracker(self) -> WorkerTracker:
        """The run's tracker (created on first use with the run's tracking budgets)."""
        if self._tracker is None:
            with self._tracker_lock:
                if self._tracker is None:
                    from mapi_runner.worker.tracking import WorkerTracker

                    self._tracker = WorkerTracker(self, self.start.tracking)
        return self._tracker

    def close(self) -> None:
        """Flush the tracker (before the worker sends its result or error). Idempotent."""
        if self._tracker is not None:
            self._tracker.close()

    # ------------------------------------------------------------------- registry
    @property
    def registry(self) -> Any:
        if self._registry is None:
            self._registry = load_registry(self._registry_ref)
        return self._registry

    # --------------------------------------------------------------------- inputs
    @property
    def transfers(self) -> Transfers:
        if self._transfers is None:
            self._transfers = Transfers(new_client())
        return self._transfers

    def has_input(self, name: str) -> bool:
        return name in self.start.inputs

    def input_names(self, prefix: str) -> list[str]:
        return sorted(n for n in self.start.inputs if n.startswith(prefix))

    def scratch_used(self) -> int:
        total = 0
        for dirpath, _dirs, names in os.walk(self.start.scratch, followlinks=False):
            for n in names:
                try:
                    total += os.lstat(os.path.join(dirpath, n)).st_size
                except OSError:
                    continue
        return total

    def _budget(self) -> int | None:
        if self.start.scratch_bytes is None:
            return None
        return max(0, self.start.scratch_bytes - self.scratch_used())

    def download(self, name: str, *, required: bool = True) -> Path | None:
        """Range-read input ``name`` into ``inputs/`` (verified; within the scratch budget)."""
        get = self.start.inputs.get(name)
        if get is None:
            if required:
                raise MapiError(
                    f"the run needs an input named {name!r}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/inputs/{name}",
                )
            return None
        return self._download(get, self.inputs_dir / name)

    def _download(self, get: SignedGet, dest: Path) -> Path:
        self.cancel.check()
        self.transfers.download(get, dest, budget=self._budget())
        return dest

    def download_resume(self) -> list[str]:
        """Previous attempt's checkpoints into ``checkpoints/`` (names validated)."""
        names: list[str] = []
        for name, get in sorted(self.start.resume.items()):
            check_object_name(name)
            if "/" in name:
                raise MapiError(f"resume name {name!r} must be a plain file name")
            self._download(get, self.checkpoints_dir / name)
            names.append(name)
        return names

    # -------------------------------------------------------------------- outputs
    def output_path(self, name: str) -> Path:
        """Where to write output ``name`` (relative object name under the attempt prefix)."""
        check_object_name(name)
        if name.endswith("/") or name == "manifest.json" or name.startswith("checkpoints/"):
            raise ValueError(f"{name!r} is not a valid output name")
        path = self.outputs_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_output(self, name: str, data: bytes) -> Path:
        path = self.output_path(name)
        self._account(len(data))
        path.write_bytes(data)
        return path

    def write_json(self, name: str, obj: Any) -> Path:
        data = json.dumps(obj, indent=2, sort_keys=True, allow_nan=False, default=str).encode()
        return self.write_output(name, data)

    def _account(self, n: int) -> None:
        self.total_written += n
        if self.total_written > self.start.max_output_bytes:
            raise MapiError(
                f"outputs exceed the {self.start.max_output_bytes}-byte limit of this run",
                code=ErrorCode.RESOURCE_EXHAUSTED,
            )

    # ---------------------------------------------------------------------- hooks
    def fit_hooks(
        self, *, checkpoint_sink: Any = None, window: tuple[float, float] = (0.0, 1.0)
    ) -> Any:
        from mapi_sdk.harness import FitHooks

        lo, hi = window

        def on_progress(fraction: float, message: str) -> None:
            self.progress(lo + (hi - lo) * fraction, message)

        return FitHooks(
            log=self.log,
            on_progress=on_progress,
            cancel=self.cancel,
            checkpoint_sink=checkpoint_sink,
            tracker=self.tracker,
        )


ProgressFn = Callable[[float, str], None]
