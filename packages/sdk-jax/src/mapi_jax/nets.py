"""Networks: a small GroupNorm ResNet for images and an MLP for tables.

A :class:`Network` is a backbone (``features(x) -> (B, F)``), a dropout layer and a linear
head (``__call__(x) -> (B, K)`` logits). GroupNorm instead of BatchNorm: no batch statistics,
so evaluation is deterministic, checkpoints hold parameters only, and MC dropout needs no
mode switching beyond the dropout key.

Dropout takes an explicit key (``__call__(x, key=...)``; ``key=None`` is deterministic), so a
training step's dropout mask depends only on ``(seed, step)``: a resumed fit replays the same
masks as an uninterrupted one.

Tensor names (:func:`network_tensors`, the safetensors layout of ``weights.safetensors`` and of
pretrained weights): ``backbone.<path>`` and ``head.kernel`` / ``head.bias``, where ``<path>``
joins the nnx attribute path with dots (``backbone.stages.0.0.conv1.kernel``).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping
from typing import Any, Literal

import jax
import jax.numpy as jnp
import numpy as np
from flax import nnx
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ArchSpec",
    "Network",
    "build_network",
    "load_tensors",
    "network_tensors",
    "param_count",
    "set_tensors",
]

MAX_WIDTH = 2048
MAX_STAGES = 8


class ArchSpec(BaseModel):
    """The architecture of a network (stored in ``learned["arch"]`` and the ``mapi_arch``
    metadata of weights files)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["mlp", "resnet"]
    in_features: int | None = Field(None, ge=1, le=1_000_000, description="mlp: input columns")
    hidden: list[int] = Field([64, 64], max_length=16, description="mlp: hidden layer widths")
    channels: int = Field(3, ge=1, le=4, description="resnet: input channels")
    widths: list[int] = Field(
        [16, 32, 64], min_length=1, max_length=MAX_STAGES, description="resnet: stage widths"
    )
    blocks: list[int] = Field(
        [1, 1, 1], min_length=1, max_length=MAX_STAGES, description="resnet: blocks per stage"
    )
    groups: int = Field(8, ge=1, le=64, description="resnet: GroupNorm groups (at most)")
    stem_stride: Literal[1, 2] = 1
    image_size: tuple[int, int] | None = Field(None, description="resnet: (height, width)")
    dropout: float = Field(0.1, ge=0, lt=1, description="Dropout before the head")

    @model_validator(mode="after")
    def _check(self) -> ArchSpec:
        if any(w < 1 or w > MAX_WIDTH for w in [*self.hidden, *self.widths]):
            raise ValueError(f"layer widths must be 1..{MAX_WIDTH}")
        if self.kind == "resnet":
            if len(self.blocks) != len(self.widths):
                raise ValueError("blocks and widths must have the same length")
            if any(b < 1 or b > 16 for b in self.blocks):
                raise ValueError("each stage takes 1..16 blocks")
        return self

    @property
    def features(self) -> int:
        """Width of the penultimate features (the head's input)."""
        if self.kind == "resnet":
            return self.widths[-1]
        if self.hidden:
            return self.hidden[-1]
        if self.in_features is None:
            raise ValueError("an mlp without hidden layers needs in_features")
        return self.in_features


def _groups(groups: int, width: int) -> int:
    """The largest divisor of ``width`` that is at most ``groups``."""
    return math.gcd(groups, width) or 1


class _ConvNorm(nnx.Module):
    def __init__(
        self, cin: int, cout: int, kernel: int, stride: int, groups: int, rngs: nnx.Rngs
    ) -> None:
        self.conv = nnx.Conv(
            cin, cout, (kernel, kernel), strides=stride, padding="SAME", use_bias=False, rngs=rngs
        )
        self.norm = nnx.GroupNorm(cout, num_groups=_groups(groups, cout), rngs=rngs)

    def __call__(self, x: jax.Array) -> jax.Array:
        return self.norm(self.conv(x))


class _Block(nnx.Module):
    """Basic residual block: conv-norm-relu-conv-norm + (projected) skip, relu."""

    def __init__(self, cin: int, cout: int, stride: int, groups: int, rngs: nnx.Rngs) -> None:
        self.conv1 = _ConvNorm(cin, cout, 3, stride, groups, rngs)
        self.conv2 = _ConvNorm(cout, cout, 3, 1, groups, rngs)
        self.proj = (
            _ConvNorm(cin, cout, 1, stride, groups, rngs) if (cin != cout or stride != 1) else None
        )

    def __call__(self, x: jax.Array) -> jax.Array:
        h = nnx.relu(self.conv1(x))
        h = self.conv2(h)
        skip = x if self.proj is None else self.proj(x)
        return nnx.relu(h + skip)


class ResNetBackbone(nnx.Module):
    """Stem conv, stages of residual blocks (stride 2 from the second stage), global mean."""

    def __init__(self, arch: ArchSpec, rngs: nnx.Rngs) -> None:
        self.stem = _ConvNorm(arch.channels, arch.widths[0], 3, arch.stem_stride, arch.groups, rngs)
        stages = []
        cin = arch.widths[0]
        for i, (width, n) in enumerate(zip(arch.widths, arch.blocks, strict=True)):
            blocks = []
            for j in range(n):
                stride = 2 if (i > 0 and j == 0) else 1
                blocks.append(_Block(cin, width, stride, arch.groups, rngs))
                cin = width
            stages.append(nnx.List(blocks))
        self.stages = nnx.List(stages)

    def __call__(self, x: jax.Array) -> jax.Array:
        h = nnx.relu(self.stem(x))
        for stage in self.stages:
            for block in stage:
                h = block(h)
        return jnp.mean(h, axis=(1, 2))


class MLPBackbone(nnx.Module):
    """Dense layers with relu (and dropout between them)."""

    def __init__(self, arch: ArchSpec, rngs: nnx.Rngs) -> None:
        if arch.in_features is None:
            raise ValueError("an mlp needs in_features")
        dims = [arch.in_features, *arch.hidden]
        self.layers = nnx.List([nnx.Linear(a, b, rngs=rngs) for a, b in itertools.pairwise(dims)])
        self.dropout = nnx.Dropout(arch.dropout)

    def __call__(self, x: jax.Array, key: jax.Array | None = None) -> jax.Array:
        for i, layer in enumerate(self.layers):
            x = nnx.relu(layer(x))
            if i < len(self.layers) - 1 and key is not None:
                x = self.dropout(x, deterministic=False, rngs=jax.random.fold_in(key, i + 1))
        return x


class Network(nnx.Module):
    """Backbone + dropout + linear head. ``features`` gives the penultimate activations."""

    def __init__(self, arch: ArchSpec, n_out: int, rngs: nnx.Rngs) -> None:
        self.kind = arch.kind
        self.backbone: ResNetBackbone | MLPBackbone = (
            ResNetBackbone(arch, rngs) if arch.kind == "resnet" else MLPBackbone(arch, rngs)
        )
        self.dropout = nnx.Dropout(arch.dropout)
        self.head = nnx.Linear(arch.features, n_out, rngs=rngs)

    def features(self, x: jax.Array, key: jax.Array | None = None) -> jax.Array:
        if isinstance(self.backbone, MLPBackbone):
            return self.backbone(x, key)
        return self.backbone(x)

    def __call__(self, x: jax.Array, key: jax.Array | None = None) -> jax.Array:
        h = self.features(x, key)
        if key is not None:
            h = self.dropout(h, deterministic=False, rngs=jax.random.fold_in(key, 0))
        return self.head(h)


def build_network(arch: ArchSpec, n_out: int, *, seed: int) -> Network:
    """A freshly initialised network (``nnx.Rngs(params=seed)``)."""
    if n_out < 1:
        raise ValueError("a network needs at least one output")
    return Network(arch, n_out, nnx.Rngs(params=seed))


# =================================================================================== tensors


def _path_name(path: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in path)


def _params(net: nnx.Module) -> list[tuple[str, Any]]:
    return [(_path_name(p), v) for p, v in nnx.to_flat_state(nnx.state(net, nnx.Param))]


def network_tensors(net: nnx.Module) -> dict[str, np.ndarray]:
    """Every parameter as a float32 numpy array, by dotted name (sorted)."""
    return {name: np.asarray(v.get_value(), dtype=np.float32) for name, v in sorted(_params(net))}


def param_count(net: nnx.Module) -> int:
    return int(sum(int(np.prod(v.get_value().shape)) for _, v in _params(net)))


def set_tensors(
    net: nnx.Module,
    tensors: Mapping[str, np.ndarray],
    *,
    select: Callable[[str], bool] | None = None,
) -> list[str]:
    """Write ``tensors`` into the matching parameters of ``net`` (shapes must match).

    Only names ``select`` accepts are written (default: all present). Returns the names
    written, sorted.
    """
    state = nnx.state(net, nnx.Param)
    flat = nnx.to_flat_state(state)
    written: list[str] = []
    for path, var in flat:
        name = _path_name(path)
        if name not in tensors or (select is not None and not select(name)):
            continue
        value = np.asarray(tensors[name])
        current = var.get_value()
        if tuple(value.shape) != tuple(current.shape):
            raise MapiError(
                f"tensor {name!r} has shape {list(value.shape)}; the network needs "
                f"{list(current.shape)}",
                code=ErrorCode.SCHEMA_INVALID,
            )
        var.set_value(jnp.asarray(value, dtype=current.dtype))
        written.append(name)
    nnx.update(net, nnx.from_flat_state(flat))
    return sorted(written)


def load_tensors(
    net: nnx.Module,
    tensors: Mapping[str, np.ndarray],
    *,
    prefix: str = "backbone.",
    strict: bool = True,
) -> list[str]:
    """Load the parameters under ``prefix`` from ``tensors`` (pretrained weights).

    ``strict``: every parameter of ``net`` under ``prefix`` must be in ``tensors`` with the
    same shape (``SCHEMA_INVALID`` names the first missing ones). Tensors outside ``prefix``
    (for example a pretrained head) are ignored. Returns the names loaded.
    """
    wanted = [name for name, _ in _params(net) if name.startswith(prefix)]
    missing = [name for name in wanted if name not in tensors]
    if strict and missing:
        raise MapiError(
            f"the weights lack {len(missing)} of the {len(wanted)} tensors the network needs",
            code=ErrorCode.SCHEMA_INVALID,
            untrusted_text=", ".join(missing[:10]),
        )
    return set_tensors(net, tensors, select=lambda name: name.startswith(prefix))
