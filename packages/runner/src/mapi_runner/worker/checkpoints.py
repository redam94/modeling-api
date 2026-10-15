"""A checkpoint sink backed by ``{scratch}/checkpoints/`` and the run's checkpoint prefix.

Engines with segmented sampling (``mapi_pymc``) hand a ``SamplingCheckpoint`` to the sink
after each segment. The sink writes two flat files atomically::

    ckpt-<key>.draws.zarr.zip     the draws so far
    ckpt-<key>.meta.json          engine, counts and the (JSON) sampling state

and tells the supervisor, which uploads them under ``RunSpec.checkpoint_prefix`` with the
``checkpoints`` POST policy. A resumed attempt receives the same names in
``RunSpec.resume`` (the resume names equal the object names relative to the checkpoint
prefix); they are downloaded into the same directory before the fit starts, so
``load(key)`` finds them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from mapi_sdk.wire import WireLimits, read_zarr_zip, write_zarr_zip

from mapi_runner.worker.context import WorkerContext

__all__ = ["RunCheckpointSink", "checkpoint_files"]

_LIMITS: Final = WireLimits(
    max_elements=500_000_000,
    max_store_elements=500_000_000,
    max_variables=65_536,
    max_uncompressed_bytes=16 * 1024**3,
    max_array_bytes=8 * 1024**3,
)


def checkpoint_files(key: str) -> tuple[str, str]:
    """``(draws, meta)`` file names of checkpoint ``key``."""
    return f"ckpt-{key}.draws.zarr.zip", f"ckpt-{key}.meta.json"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class RunCheckpointSink:
    """Implements the ``mapi_pymc.CheckpointSink`` protocol (``save``/``load``/``clear``)."""

    def __init__(self, ctx: WorkerContext) -> None:
        self.ctx = ctx
        self.root = ctx.checkpoints_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.segments = 0

    def save(self, checkpoint: Any) -> None:
        draws, meta = checkpoint_files(checkpoint.key)
        raw = write_zarr_zip(checkpoint.data)
        assert raw is not None
        _atomic_write(self.root / draws, raw)
        body = json.dumps(checkpoint.meta(), allow_nan=False, sort_keys=True).encode()
        _atomic_write(self.root / meta, body)
        segment = self.segments
        self.segments += 1
        self.ctx.checkpoint([draws, meta], segment, int(checkpoint.completed))

    def load(self, key: str) -> Any:
        from mapi_pymc.checkpoint import SamplingCheckpoint

        draws, meta = checkpoint_files(key)
        mpath, dpath = self.root / meta, self.root / draws
        if not mpath.is_file() or not dpath.is_file():
            return None
        info = json.loads(mpath.read_text())
        if info.get("key") != key:
            return None
        data = read_zarr_zip(dpath.read_bytes(), limits=_LIMITS)
        return SamplingCheckpoint(
            key=info["key"],
            engine=info["engine"],
            resumable=bool(info["resumable"]),
            chains=int(info["chains"]),
            tune=int(info["tune"]),
            draws=int(info["draws"]),
            completed=int(info["completed"]),
            data=data,
            state=info.get("state", {}),
        )

    def clear(self, key: str) -> None:
        # uploaded checkpoints stay in the run prefix (they back a PARTIAL bundle);
        # only the local copies go
        for name in checkpoint_files(key):
            (self.root / name).unlink(missing_ok=True)
