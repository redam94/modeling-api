"""Helpers of the v0.15 collection tests: a real :class:`WorkerContext` whose inputs are
signed GETs of the GCS shim (so parts are range-read like in a run), and the uploads of a
client: a manifest table and image parts (``mapi_sdk.testing.images``)."""

from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from mapi_runner.channel import MAX_WORKER_FRAME, StartMessage, parse_worker_message, recv_frame
from mapi_runner.worker.context import WorkerContext
from mapi_sdk.testing.images import build_parts, shape_images
from mapi_spec.executor import RunKind
from mapi_spec.storage import SignedGet
from shimgcs import ShimGcs

__all__ = ["Cancel", "Worker", "client_manifest", "images_and_labels", "put_inputs"]


class Cancel:
    cancelled = False

    def check(self) -> None:
        return None


class Worker:
    """A WorkerContext on one end of a socketpair; messages it sends are collected."""

    def __init__(
        self,
        scratch: Path,
        inputs: Mapping[str, SignedGet],
        *,
        kind: RunKind = RunKind.INGEST,
        spec: dict[str, Any] | None = None,
        checkpointing: bool = False,
        resume: Mapping[str, SignedGet] | None = None,
        registry: Any = None,
    ) -> None:
        self.a, self.b = socket.socketpair()
        scratch.mkdir(parents=True, exist_ok=True)
        start = StartMessage(
            run_id="run_c",
            attempt=1,
            kind=kind,
            tenant_id="default",
            project_id="default",
            spec=spec or {},
            inputs=dict(inputs),
            resume=dict(resume or {}),
            scratch=str(scratch),
            max_output_bytes=1 << 30,
            max_output_objects=1000,
            checkpointing=checkpointing,
            deadline=datetime.now(UTC) + timedelta(hours=1),
        )
        self.ctx = WorkerContext(start, self.a, cancel=Cancel())
        if registry is not None:  # a Registry object (the test's own models)
            self.ctx._registry = registry
        self.msgs: list[Any] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while (body := recv_frame(self.b, max_bytes=MAX_WORKER_FRAME)) is not None:
            self.msgs.append(parse_worker_message(body))

    def finish(self) -> list[Any]:
        self.ctx.close()
        self.a.shutdown(socket.SHUT_WR)
        self._reader.join(5)
        self.a.close()
        self.b.close()
        return self.msgs

    def output(self, name: str) -> bytes:
        return (self.ctx.outputs_dir / name).read_bytes()

    def json(self, name: str) -> Any:
        return json.loads(self.output(name))


def images_and_labels(
    n_per_class: int = 4, size: int = 8, seed: int = 0
) -> tuple[list[bytes], list[str]]:
    return shape_images(n_per_class, size, ("circle", "square", "cross"), seed=seed)


def client_manifest(
    images: Sequence[bytes],
    labels: Sequence[str],
    *,
    extra: Mapping[str, Sequence[Any]] | None = None,
) -> bytes:
    """The CSV the client uploads: ``path, sha256, label, split`` (+ ``extra`` columns)."""
    rows: dict[str, Any] = {
        "path": [f"{lab}/{i:03d}.png" for i, lab in enumerate(labels)],
        "sha256": [hashlib.sha256(d).hexdigest() for d in images],
        "label": list(labels),
        "split": ["test" if i % 4 == 3 else "train" for i in range(len(images))],
    }
    rows.update(extra or {})
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue().encode()


def put_inputs(gcs: ShimGcs, prefix: str, objects: Mapping[str, bytes]) -> dict[str, SignedGet]:
    """Store ``objects`` in the quarantine bucket and sign a GET for each (input name -> GET)."""
    bucket = gcs.buckets.quarantine
    out: dict[str, SignedGet] = {}
    for name, data in objects.items():
        gcs.put(bucket, f"{prefix}/{name}", data)
        out[name] = gcs.sign_get(bucket, f"{prefix}/{name}")
    return out


def parts_of(images: Sequence[bytes], part_bytes: int) -> list[bytes]:
    return build_parts(images, part_bytes=part_bytes)
