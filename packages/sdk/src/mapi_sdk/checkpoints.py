"""File checkpoints (v0.15): network weights and optimiser state saved between epochs.

A model that trains in epochs (the ``dl.*`` models of ``mapi-jax``) saves its state every
``fit.checkpoint.every_epochs`` epochs through a :class:`FileCheckpointSink` found on
``FitContext.checkpoint_sink``, and asks it for the newest state when a fit starts. A
checkpoint is a few files (safetensors bytes, see :mod:`mapi_sdk.safetensors`) and a small
JSON document, named by a key the model derives from ``FitContext.fit_key``::

    ckpt-<key>.weights.safetensors   ckpt-<key>.opt.safetensors   ckpt-<key>.meta.json

The runner's sink writes these files into the run's checkpoint prefix (the same path the
PyMC draw checkpoints use); :class:`MemoryFileCheckpointSink` keeps them in memory for tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

from mapi_spec.errors import ErrorCode, MapiError
from pydantic import JsonValue

__all__ = [
    "CHECKPOINT_FILES",
    "CHECKPOINT_KEY_RE",
    "FileCheckpoint",
    "FileCheckpointSink",
    "MemoryFileCheckpointSink",
    "checkpoint_names",
]

CHECKPOINT_KEY_RE: Final = r"^[0-9a-f]{16,64}$"
"""A checkpoint key: lowercase hex (``FitContext.fit_key`` is 32 characters)."""
CHECKPOINT_FILES: Final = ("weights", "opt")
"""The files a checkpoint may hold (``meta`` is the JSON document)."""


def _check_key(key: str) -> str:
    if not isinstance(key, str) or re.match(CHECKPOINT_KEY_RE, key) is None:
        raise MapiError(
            "a checkpoint key must be 16 to 64 lowercase hex characters",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=str(key)[:200],
        )
    return key


def checkpoint_names(key: str) -> dict[str, str]:
    """The object names of the checkpoint ``key``: ``{"weights", "opt", "meta"}``."""
    _check_key(key)
    return {
        "weights": f"ckpt-{key}.weights.safetensors",
        "opt": f"ckpt-{key}.opt.safetensors",
        "meta": f"ckpt-{key}.meta.json",
    }


@dataclass(frozen=True, slots=True)
class FileCheckpoint:
    """A saved checkpoint: ``files`` by kind (``weights``/``opt``), the model's JSON
    ``meta`` and ``progress`` (epochs done)."""

    key: str
    files: dict[str, bytes]
    meta: dict[str, JsonValue]
    progress: int


@runtime_checkable
class FileCheckpointSink(Protocol):
    """Where a model saves and finds file checkpoints (the runner supplies one)."""

    def save_files(
        self, key: str, files: Mapping[str, bytes], meta: Mapping[str, JsonValue], progress: int
    ) -> None:
        """Save (replace) the checkpoint ``key``; ``files`` keys are a subset of
        :data:`CHECKPOINT_FILES`, ``progress`` is the number of epochs done."""
        ...

    def load_files(self, key: str) -> FileCheckpoint | None:
        """The newest checkpoint ``key``, or ``None``."""
        ...


def check_checkpoint(
    key: str, files: Mapping[str, bytes], meta: Mapping[str, JsonValue], progress: int
) -> None:
    """Refuse a malformed checkpoint (sinks call this before they write)."""
    _check_key(key)
    unknown = sorted(set(files) - set(CHECKPOINT_FILES))
    if unknown:
        raise MapiError(
            f"a checkpoint holds only the files {', '.join(CHECKPOINT_FILES)}",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=", ".join(unknown)[:200],
        )
    if not all(isinstance(v, bytes | bytearray | memoryview) for v in files.values()):
        raise MapiError("checkpoint files must be bytes", code=ErrorCode.SCHEMA_INVALID)
    if isinstance(progress, bool) or not isinstance(progress, int) or progress < 0:
        raise MapiError(
            "checkpoint progress must be a non-negative integer", code=ErrorCode.SCHEMA_INVALID
        )
    try:
        json.dumps(dict(meta), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MapiError(
            "checkpoint meta must be JSON", code=ErrorCode.SCHEMA_INVALID, untrusted_text=str(exc)
        ) from None


@dataclass(slots=True)
class MemoryFileCheckpointSink:
    """A :class:`FileCheckpointSink` in memory (tests): the newest checkpoint per key.

    ``saves`` counts :meth:`save_files` calls. ``meta`` is stored as a JSON round trip, so a
    resumed fit sees what a runner sink would give back.
    """

    checkpoints: dict[str, FileCheckpoint] = field(default_factory=dict)
    saves: int = 0

    def save_files(
        self, key: str, files: Mapping[str, bytes], meta: Mapping[str, JsonValue], progress: int
    ) -> None:
        check_checkpoint(key, files, meta, progress)
        self.checkpoints[key] = FileCheckpoint(
            key=key,
            files={k: bytes(v) for k, v in files.items()},
            meta=json.loads(json.dumps(dict(meta), allow_nan=False)),
            progress=progress,
        )
        self.saves += 1

    def load_files(self, key: str) -> FileCheckpoint | None:
        _check_key(key)
        return self.checkpoints.get(key)
