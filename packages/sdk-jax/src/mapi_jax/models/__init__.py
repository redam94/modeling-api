"""The ``dl.*`` model plugins."""

from mapi_jax.models.cnn import CNNClassifier, CNNClassifierSettings
from mapi_jax.models.finetune import FinetuneClassifier, FinetuneClassifierSettings
from mapi_jax.models.mlp import MLP, MLPSettings

__all__ = [
    "MLP",
    "CNNClassifier",
    "CNNClassifierSettings",
    "FinetuneClassifier",
    "FinetuneClassifierSettings",
    "MLPSettings",
]
