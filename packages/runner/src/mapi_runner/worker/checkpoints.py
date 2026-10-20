"""A checkpoint sink backed by ``{scratch}/checkpoints/`` and the run's checkpoint prefix.

Engines with segmented sampling (``mapi_pymc``) hand a ``SamplingCheckpoint`` to the sink
after each segment. The sink writes two flat files atomically::

    ckpt-<key>.draws.zarr.zip     the draws so far
    ckpt-<key>.meta.json          engine, counts and the (JSON) sampling state

v0.15: models that train in epochs save file checkpoints through the same sink
(:class:`~mapi_sdk.checkpoints.FileCheckpointSink`)::

    ckpt-<key>.weights.safetensors   ckpt-<key>.opt.safetensors   ckpt-<key>.meta.json

The sink tells the supervisor, which uploads them under ``RunSpec.checkpoint_prefix`` with the
``checkpoints`` POST policy. A resumed attempt receives the same names in
``RunSpec.resume`` (the resume names equal the object names relative to the checkpoint
prefix); they are downloaded into the same directory before the fit starts, so
``load(key)`` finds them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from mapi_sdk.checkpoints import (
    CHECKPOINT_FILES,
    FileCheckpoint,
    check_checkpoint,
    checkpoint_names,
)
from mapi_sdk.wire import WireLimits, read_zarr_zip, write_zarr_zip
from pydantic import JsonValue

from mapi_runner.worker.context import WorkerContext

__all__ = ["RunCheckpointSink", "checkpoint_files"]

_FILES_KIND: Final = "files"
"""``meta.json`` ``kind`` of a file checkpoint (a draws checkpoint has none)."""
_MAX_META_BYTES: Final = 16 * 1024 * 1024
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
    """Implements the ``mapi_pymc.CheckpointSink`` protocol (``save``/``load``/``clear``)
    and, v0.15, :class:`~mapi_sdk.checkpoints.FileCheckpointSink`
    (``save_files``/``load_files``)."""

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
        if info.get("key") != key or info.get("kind") == _FILES_KIND:
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

    # ------------------------------------------------------------ file checkpoints
    def save_files(
        self, key: str, files: Mapping[str, bytes], meta: Mapping[str, JsonValue], progress: int
    ) -> None:
        """Write the checkpoint's files, then its ``meta.json`` (each atomically), and ask
        the supervisor to upload them; ``progress`` (epochs done) is the segment."""
        check_checkpoint(key, files, meta, progress)
        names = checkpoint_names(key)
        doc = {
            "key": key,
            "kind": _FILES_KIND,
            "files": sorted(k for k in CHECKPOINT_FILES if k in files),
            "progress": progress,
            "meta": dict(meta),
        }
        body = json.dumps(doc, allow_nan=False, sort_keys=True).encode()
        if len(body) > _MAX_META_BYTES:
            # load_files ignores such a meta: say so now instead of resuming from scratch
            self.ctx.warning(
                "CHECKPOINT_SKIPPED",
                f"the checkpoint meta has {len(body):,} bytes. The maximum is "
                f"{_MAX_META_BYTES:,}, so the checkpoint was not saved",
            )
            return
        written: list[str] = []
        for kind in CHECKPOINT_FILES:
            if kind in files:
                _atomic_write(self.root / names[kind], bytes(files[kind]))
                written.append(names[kind])
        _atomic_write(self.root / names["meta"], body)
        written.append(names["meta"])
        self.segments += 1
        self.ctx.checkpoint(written, progress, None)

    def load_files(self, key: str) -> FileCheckpoint | None:
        """The checkpoint ``key`` in ``checkpoints/`` (this attempt's, or the previous
        attempt's downloaded by ``download_resume``), or ``None`` when it is missing or
        incomplete."""
        names = checkpoint_names(key)
        mpath = self.root / names["meta"]
        if not mpath.is_file() or mpath.stat().st_size > _MAX_META_BYTES:
            return None
        try:
            info = json.loads(mpath.read_bytes())
        except ValueError:
            return None
        if not isinstance(info, dict) or info.get("key") != key:
            return None
        if info.get("kind") != _FILES_KIND:
            return None
        kinds = info.get("files")
        progress = info.get("progress")
        meta = info.get("meta")
        if not isinstance(kinds, list) or not isinstance(meta, dict):
            return None
        if isinstance(progress, bool) or not isinstance(progress, int) or progress < 0:
            return None
        out: dict[str, bytes] = {}
        for kind in kinds:
            if kind not in CHECKPOINT_FILES:
                return None
            path = self.root / names[kind]
            if not path.is_file():
                return None
            out[kind] = path.read_bytes()
        return FileCheckpoint(key=key, files=out, meta=meta, progress=progress)
