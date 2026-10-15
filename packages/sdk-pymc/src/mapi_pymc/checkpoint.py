"""Checkpoints for segmented sampling (plan §5, §8.3).

A long MCMC fit tunes once and then draws in chunks. After each chunk the engine hands a
:class:`SamplingCheckpoint` to a :class:`CheckpointSink`; a retried run with the same
fit key resumes from the last checkpoint instead of starting again.

Two kinds of checkpoint exist:

* **resumable** (engine ``pymc``): the draws so far, every chain's current position and the
  full NUTS sampling state (adapted step size, mass matrix, RNG state). Resuming continues
  the same chains exactly; the result equals an uninterrupted run with the same seed.
* **snapshot** (engine ``nutpie``): the draws so far only. nutpie cannot export or restore
  its adaptation state, so a snapshot feeds a PARTIAL bundle but a retry starts again.

Nothing here pickles. Sampling states are encoded to JSON by :func:`encode_state`; arrays
travel base64-encoded and dataclasses are rebuilt only from an allow-list of PyMC modules.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
import xarray as xr
from mapi_sdk.wire import WireLimits, read_zarr_zip, write_zarr_zip

__all__ = [
    "CheckpointSink",
    "DirectoryCheckpointSink",
    "MemoryCheckpointSink",
    "SamplingCheckpoint",
    "decode_state",
    "encode_state",
    "fit_key",
]

_KEY_RE = re.compile(r"^[0-9a-f]{16,64}$")
_ALLOWED_MODULES = ("pymc.", "mapi_pymc.")
_CHECKPOINT_LIMITS = WireLimits(
    max_elements=500_000_000,
    max_variables=65_536,
    max_uncompressed_bytes=16 * 1024**3,
    max_array_bytes=8 * 1024**3,
)


@dataclass(slots=True)
class SamplingCheckpoint:
    """The state of a segmented fit after ``completed`` post-tuning draws per chain."""

    key: str
    engine: str
    resumable: bool
    chains: int
    tune: int
    draws: int
    completed: int
    data: xr.Dataset = field(default_factory=xr.Dataset)
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _KEY_RE.match(self.key):
            raise ValueError("checkpoint keys are lowercase hex digests (16-64 characters)")
        if not 0 <= self.completed <= self.draws:
            raise ValueError("completed must lie between 0 and draws")

    @property
    def fraction(self) -> float:
        """Share of the post-tuning draws already taken."""
        return self.completed / self.draws if self.draws else 1.0

    def meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "engine": self.engine,
            "resumable": self.resumable,
            "chains": self.chains,
            "tune": self.tune,
            "draws": self.draws,
            "completed": self.completed,
            "state": self.state,
        }


@runtime_checkable
class CheckpointSink(Protocol):
    """Where engines put checkpoints. The runner provides one backed by the run prefix."""

    def save(self, checkpoint: SamplingCheckpoint) -> None: ...

    def load(self, key: str) -> SamplingCheckpoint | None: ...

    def clear(self, key: str) -> None: ...


class MemoryCheckpointSink:
    """Keeps the latest checkpoint per key in memory (tests, notebooks)."""

    def __init__(self) -> None:
        self.saved: dict[str, SamplingCheckpoint] = {}
        self.history: list[tuple[str, int]] = []

    def save(self, checkpoint: SamplingCheckpoint) -> None:
        self.saved[checkpoint.key] = checkpoint
        self.history.append((checkpoint.key, checkpoint.completed))

    def load(self, key: str) -> SamplingCheckpoint | None:
        return self.saved.get(key)

    def clear(self, key: str) -> None:
        self.saved.pop(key, None)


class DirectoryCheckpointSink:
    """One directory per key: ``meta.json`` plus ``draws.zarr.zip`` (written atomically)."""

    META = "meta.json"
    DRAWS = "draws.zarr.zip"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _dir(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise ValueError("checkpoint keys are lowercase hex digests (16-64 characters)")
        return self.root / key

    def save(self, checkpoint: SamplingCheckpoint) -> None:
        d = self._dir(checkpoint.key)
        d.mkdir(parents=True, exist_ok=True)
        raw = write_zarr_zip(checkpoint.data)
        assert raw is not None
        _atomic_write(d / self.DRAWS, raw)
        meta = json.dumps(checkpoint.meta(), allow_nan=False, sort_keys=True).encode()
        _atomic_write(d / self.META, meta)

    def load(self, key: str) -> SamplingCheckpoint | None:
        d = self._dir(key)
        meta_path, draws_path = d / self.META, d / self.DRAWS
        if not meta_path.is_file() or not draws_path.is_file():
            return None
        meta = json.loads(meta_path.read_text())
        data = read_zarr_zip(draws_path.read_bytes(), limits=_CHECKPOINT_LIMITS)
        return SamplingCheckpoint(
            key=meta["key"],
            engine=meta["engine"],
            resumable=bool(meta["resumable"]),
            chains=int(meta["chains"]),
            tune=int(meta["tune"]),
            draws=int(meta["draws"]),
            completed=int(meta["completed"]),
            data=data,
            state=meta.get("state", {}),
        )

    def clear(self, key: str) -> None:
        d = self._dir(key)
        for name in (self.META, self.DRAWS):
            (d / name).unlink(missing_ok=True)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ===================================================================================== state codec


def encode_state(obj: Any) -> Any:
    """Encode a PyMC sampling state (nested dataclasses, arrays, RNG states) as JSON values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        cls = type(obj)
        return {
            "__dataclass__": f"{cls.__module__}:{cls.__qualname__}",
            "fields": {f.name: encode_state(getattr(obj, f.name)) for f in dataclasses.fields(obj)},
        }
    if isinstance(obj, np.ndarray):
        return {"__ndarray__": _encode_array(obj)}
    if isinstance(obj, np.generic):
        return {"__scalar__": _encode_array(np.asarray(obj))}
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else {"__float__": repr(obj)}
    if isinstance(obj, tuple) and hasattr(type(obj), "_fields"):
        cls = type(obj)
        return {
            "__namedtuple__": f"{cls.__module__}:{cls.__qualname__}",
            "fields": {k: encode_state(getattr(obj, k)) for k in cls._fields},  # type: ignore[attr-defined]
        }
    if isinstance(obj, tuple):
        return {"__tuple__": [encode_state(v) for v in obj]}
    if isinstance(obj, list):
        return [encode_state(v) for v in obj]
    if isinstance(obj, Mapping):
        return {"__dict__": [[encode_state(k), encode_state(v)] for k, v in obj.items()]}
    raise TypeError(f"cannot checkpoint an object of type {type(obj).__name__}")


def decode_state(obj: Any) -> Any:
    """Inverse of :func:`encode_state`; dataclasses must come from PyMC or mapi_pymc."""
    if isinstance(obj, list):
        return [decode_state(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    if "__ndarray__" in obj:
        return _decode_array(obj["__ndarray__"])
    if "__scalar__" in obj:
        return _decode_array(obj["__scalar__"])[()]
    if "__float__" in obj:
        return float(obj["__float__"])
    if "__tuple__" in obj:
        return tuple(decode_state(v) for v in obj["__tuple__"])
    if "__dict__" in obj:
        return {decode_state(k): decode_state(v) for k, v in obj["__dict__"]}
    if "__namedtuple__" in obj:
        nt = _allowed_type(str(obj["__namedtuple__"]))
        if not (issubclass(nt, tuple) and hasattr(nt, "_fields")):
            raise ValueError(f"{obj['__namedtuple__']!r} is not a namedtuple")
        return nt(**{k: decode_state(v) for k, v in obj["fields"].items()})
    if "__dataclass__" in obj:
        cls = _dataclass_type(str(obj["__dataclass__"]))
        values = {k: decode_state(v) for k, v in obj["fields"].items()}
        init = {f.name for f in dataclasses.fields(cls) if f.init}
        inst = cls(**{k: v for k, v in values.items() if k in init})
        for k, v in values.items():
            if k not in init:
                object.__setattr__(inst, k, v)
        return inst
    raise ValueError("unrecognised checkpoint state encoding")


def _encode_array(arr: np.ndarray) -> dict[str, Any]:
    if arr.dtype.kind not in "biufc":
        raise TypeError(f"cannot checkpoint arrays of dtype {arr.dtype}")
    data = np.array(arr, order="C", copy=True)  # keeps 0-d shapes (ascontiguousarray does not)
    return {
        "dtype": data.dtype.str,
        "shape": list(arr.shape),
        "b64": base64.b64encode(data.tobytes()).decode("ascii"),
    }


def _decode_array(spec: Mapping[str, Any]) -> np.ndarray:
    dtype = np.dtype(str(spec["dtype"]))
    if dtype.kind not in "biufc":
        raise ValueError(f"refusing to decode arrays of dtype {dtype}")
    raw = base64.b64decode(str(spec["b64"]))
    shape = tuple(int(s) for s in spec["shape"])
    return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()


def _allowed_type(path: str) -> type[Any]:
    module, _, qual = path.partition(":")
    if not module.startswith(_ALLOWED_MODULES) or not qual:
        raise ValueError(f"refusing to rebuild {path!r}: not a PyMC sampling-state class")
    obj: Any = importlib.import_module(module)
    for part in qual.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, type):
        raise ValueError(f"{path!r} is not a class")
    return obj


def _dataclass_type(path: str) -> type[Any]:
    obj = _allowed_type(path)
    if not dataclasses.is_dataclass(obj):
        raise ValueError(f"{path!r} is not a dataclass")
    return obj


# ======================================================================================== fit key


def fit_key(
    *,
    model_id: str,
    version: str,
    settings: Mapping[str, Any],
    fit_config: Mapping[str, Any],
    data: xr.Dataset,
) -> str:
    """Identify one fit: plugin, settings, fit config (minus checkpointing) and the data.

    The evaluation fit (train) and the production refit (full data) of
    ``evaluate_then_refit`` therefore get different keys and never resume from each other.
    """
    h = hashlib.sha256()
    cfg = {k: v for k, v in fit_config.items() if k != "checkpoint"}
    head = {"id": model_id, "version": version, "settings": settings, "fit": cfg}
    h.update(json.dumps(head, sort_keys=True, default=str).encode())
    for name in sorted(map(str, data.variables)):
        var = data.variables[name]
        h.update(f"|{name}|{var.dims}|{var.dtype.str}|".encode())
        values = np.asarray(var.values)
        if values.dtype.kind in "biufcmM":
            h.update(np.ascontiguousarray(values).tobytes())
        else:
            h.update("\x1f".join(map(str, values.reshape(-1).tolist())).encode())
    return h.hexdigest()[:32]
