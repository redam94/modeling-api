"""Model plugins registered under the ``dl`` namespace (entry point ``mapi.models``)."""

from __future__ import annotations

from typing import Any

from mapi_jax.models import MLP, CNNClassifier, FinetuneClassifier

PLUGINS: list[type[Any]] = [MLP, CNNClassifier, FinetuneClassifier]
