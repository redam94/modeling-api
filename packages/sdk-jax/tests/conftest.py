"""Shared tiny fixtures (module/session scope so compiled networks are reused)."""

from __future__ import annotations

from pathlib import Path

import pytest
import xarray as xr
from mapi_sdk.collection import Collection
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing.images import image_dataset, local_collection

#: Tiny ResNet settings shared by the image model tests.
TINY_CNN = {"widths": [4, 8], "blocks": [1, 1], "groups": 2}

REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://vision/shapes\n"


def cnn_spec(settings: str = "", fit: str = "{seed: 3, mode: evaluate_then_refit}") -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: shapes, project: vision}}
data:
  dataset: dataset://vision/shapes@latest
  split: {{kind: column, dim: image, column: split, holdout: [test]}}
model:
  use: dl.cnn_classifier@^1
  settings: {{widths: [4, 8], blocks: [1, 1], groups: 2,
              train: {{epochs: 3, batch_size: 8, learning_rate: 0.01}},
              uncertainty: {{draws: 8}}{settings}}}
  fit: {fit}
evaluate: {{metrics: [accuracy, log_score]}}
"""


@pytest.fixture(scope="session")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="session")
def shapes() -> xr.Dataset:
    """18 gray 8 x 8 images of 3 classes with a train/test split (the v0.14 image layout)."""
    return image_dataset(n_per_class=6, size=8)


@pytest.fixture(scope="session")
def collection(tmp_path_factory: pytest.TempPathFactory) -> Collection:
    root: Path = tmp_path_factory.mktemp("collection")
    return local_collection(root, n_per_class=6, size=8)
