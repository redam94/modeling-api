"""JAX / flax deep-learning models for modeling-api.

``mapi_jax.plugins`` (models ``dl.mlp``, ``dl.cnn_classifier``, ``dl.finetune_classifier``)
and ``mapi_jax.steps`` (``dl.embed``) are the entry points. The public names below are
imported lazily, so ``import mapi_jax`` stays cheap; importing jax never initialises a device
backend (that happens at the first computation, inside a fit).
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.15.0"

_EXPORTS = {
    "ArchSpec": "mapi_jax.nets",
    "Network": "mapi_jax.nets",
    "build_network": "mapi_jax.nets",
    "load_tensors": "mapi_jax.nets",
    "network_tensors": "mapi_jax.nets",
    "EarlyStopping": "mapi_jax.config",
    "TrainConfig": "mapi_jax.config",
    "UncertaintyConfig": "mapi_jax.config",
    "ClassificationTask": "mapi_jax.tasks",
    "RegressionTask": "mapi_jax.tasks",
    "TabularLoader": "mapi_jax.tabular",
    "TrainResult": "mapi_jax.trainer",
    "Trainer": "mapi_jax.trainer",
    "MLP": "mapi_jax.models",
    "CNNClassifier": "mapi_jax.models",
    "FinetuneClassifier": "mapi_jax.models",
    "EmbedStep": "mapi_jax.steps",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'mapi_jax' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
