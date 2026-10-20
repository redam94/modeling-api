"""``dl.finetune_classifier`` with pretrained weights from a side table."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_jax.models.base import WEIGHTS_FILE
from mapi_jax.testing import image_schema, tiny_pretrained, tiny_weights_dataset
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.safetensors import read_safetensors
from mapi_spec import load_score_request
from mapi_spec.errors import MapiError

from .conftest import REQ


def _spec(settings: str) -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: shapes-ft, project: vision}}
data:
  dataset: dataset://vision/shapes@latest
  side_tables:
    pretrained: {{ref: "dataset://vision/tiny-resnet@v1", score_policy: pinned}}
model:
  use: dl.finetune_classifier@^1
  settings: {{train: {{epochs: 2, batch_size: 6, learning_rate: 0.02}},
              uncertainty: {{method: none}}{settings}}}
  fit: {{seed: 2}}
"""


def _backbone(raw: bytes) -> dict[str, np.ndarray]:
    tensors, _ = read_safetensors(raw)
    return {k: v for k, v in tensors.items() if k.startswith("backbone.")}


def test_frozen_backbone_keeps_the_pretrained_weights(
    shapes: xr.Dataset, registry: Registry
) -> None:
    b = fit_spec(
        _spec(", freeze: backbone"),
        shapes,
        side_tables={"pretrained": tiny_weights_dataset()},
        registry=registry,
        raw_schema=image_schema(shapes),
    )
    after = _backbone(b.model.native[WEIGHTS_FILE])
    before = _backbone(tiny_pretrained())
    assert set(after) == set(before)
    assert all(np.array_equal(after[k], before[k]) for k in before)
    assert b.model.learned["arch"]["widths"] == [4, 8]  # type: ignore[index]
    out = score_bundle(
        b,
        load_score_request(REQ + "mode: predict\n"),
        shapes.isel(image=slice(0, 3)),
        side_tables={"pretrained": tiny_weights_dataset()},
        registry=registry,
        return_full=True,
    )
    assert out["predictions"].to_dataset()["y_hat"].sizes["image"] == 3


def test_head_then_all_phases_move_the_backbone(shapes: xr.Dataset, registry: Registry) -> None:
    b = fit_spec(
        _spec(", head_epochs: 1, backbone_lr_scale: 0.5"),
        shapes,
        side_tables={"pretrained": tiny_weights_dataset()},
        registry=registry,
        raw_schema=image_schema(shapes),
    )
    after = _backbone(b.model.native[WEIGHTS_FILE])
    before = _backbone(tiny_pretrained())
    assert not all(np.array_equal(after[k], before[k]) for k in before)
    h = b.model.groups["training"]
    assert list(h["epoch"].values) == [1, 2]
    assert h.attrs["epochs_run"] == 2


def test_missing_side_table_is_refused(shapes: xr.Dataset, registry: Registry) -> None:
    with pytest.raises(MapiError):
        fit_spec(_spec(""), shapes, registry=registry, raw_schema=image_schema(shapes))
