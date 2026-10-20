"""Network and optimiser state <-> safetensors bytes (epoch checkpoints).

The optimiser state is an optax pytree inside ``nnx.Optimizer``; it is flattened by its nnx
path (``opt_state.0.mu.head.kernel``, ``step``) into one tensor per leaf. Restoring writes the
tensors back into an optimiser built the same way (same network, transform and filter), so
the leaf set must match exactly. Weights use :func:`mapi_jax.nets.network_tensors` names.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import jax.numpy as jnp
import numpy as np
from flax import nnx
from mapi_sdk.safetensors import read_safetensors, write_safetensors
from mapi_spec.errors import ErrorCode, MapiError

from mapi_jax.nets import network_tensors, set_tensors

__all__ = [
    "json_safe",
    "optimizer_bytes",
    "optimizer_tensors",
    "restore_optimizer",
    "restore_weights",
    "weights_bytes",
]


def _name(path: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in path)


def optimizer_tensors(opt: nnx.Optimizer[Any]) -> dict[str, np.ndarray]:
    """Every array leaf of the optimiser state, by dotted nnx path."""
    out: dict[str, np.ndarray] = {}
    for path, var in nnx.to_flat_state(nnx.state(opt)):
        value = np.asarray(var.get_value() if hasattr(var, "get_value") else var)
        if value.dtype.kind == "u" and value.dtype != np.uint8:
            value = value.astype(np.int64)  # safetensors has no U16/U32/U64; restore casts back
        out[_name(path)] = value
    return out


def optimizer_bytes(opt: nnx.Optimizer[Any]) -> bytes:
    return write_safetensors(optimizer_tensors(opt))


def restore_optimizer(opt: nnx.Optimizer[Any], data: bytes) -> None:
    """Write a checkpoint's optimiser tensors back into ``opt`` (same leaf set and shapes)."""
    tensors, _ = read_safetensors(data)
    state = nnx.state(opt)
    flat = nnx.to_flat_state(state)
    names = {_name(p) for p, _ in flat}
    if names != set(tensors):
        raise MapiError(
            "the checkpoint's optimizer state does not match the training setup",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=", ".join(sorted(names ^ set(tensors))[:10]),
        )
    for path, var in flat:
        current = var.get_value()
        value = tensors[_name(path)]
        if tuple(value.shape) != tuple(np.shape(current)):
            raise MapiError(
                f"optimizer tensor {_name(path)!r} has the wrong shape",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        var.set_value(jnp.asarray(value, dtype=current.dtype))
    nnx.update(opt, nnx.from_flat_state(flat))


def weights_bytes(net: nnx.Module, metadata: Mapping[str, str] | None = None) -> bytes:
    return write_safetensors(network_tensors(net), metadata)


def restore_weights(net: nnx.Module, data: bytes) -> None:
    """Load every parameter of ``net`` from safetensors bytes (all names must be present)."""
    tensors, _ = read_safetensors(data)
    expected = set(network_tensors(net))
    if expected != set(tensors):
        raise MapiError(
            "the checkpoint's weights do not match the network",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=", ".join(sorted(expected ^ set(tensors))[:10]),
        )
    set_tensors(net, tensors)


def json_safe(value: Any) -> Any:
    """``value`` with non-finite floats replaced by ``None`` (checkpoint meta is strict JSON)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, np.generic):
        return json_safe(value.item())
    return value
