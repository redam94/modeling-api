"""``dl.embed``: features from a frozen backbone in front of a sklearn head."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_jax.steps import EmbedSettings, EmbedStep
from mapi_jax.testing import TINY_ARCH, image_schema, tiny_weights_dataset
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.steps import StepContext
from mapi_spec import load_score_request

from .conftest import REQ

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: shapes-embed, project: vision}
data:
  dataset: dataset://vision/shapes@latest
  split: {kind: column, dim: image, column: split, holdout: [test]}
  side_tables:
    backbone: {ref: "dataset://vision/tiny-resnet@v1", score_policy: pinned}
pipeline:
  - {id: embed, use: dl.embed@^1, with: {weights: backbone}}
model:
  use: sklearn.multiclass_classifier@^1
  settings: {estimator: {kind: sklearn.LogisticRegression}, target: label, rows: [image],
             time_kind: none}
  fit: {seed: 1, mode: evaluate_then_refit}
evaluate: {metrics: [accuracy, brier]}
"""


def test_transform_writes_features_and_drops_pixels(shapes: xr.Dataset) -> None:
    step = EmbedStep()
    tables = {"weights": tiny_weights_dataset()}
    ctx = StepContext(step_id="embed", side_tables=tables)
    s = step.parse_settings({})
    st = step.fit(shapes, s, ctx)
    assert st.weights_sha256 is not None and len(st.weights_sha256) == 64
    out = step.transform(shapes, s, st, ctx)
    assert "pixels" not in out and out["embedding"].dims == ("image", "feature")
    assert out["embedding"].dtype == np.float32
    assert out.sizes["feature"] == TINY_ARCH.features
    kept = step.transform(shapes, step.parse_settings({"keep_pixels": True}), st, ctx)
    assert "pixels" in kept
    again = step.transform(shapes, s, st, ctx)
    np.testing.assert_array_equal(out["embedding"].values, again["embedding"].values)
    eff = step.effect(s)
    assert eff.drops == ["pixels"] and set(eff.adds) == {"embedding"}
    assert step.effect(EmbedSettings(keep_pixels=True)).drops == []
    assert EmbedStep.catalog_entry().side_table_params == ["weights"]


def test_embed_then_sklearn_head(shapes: xr.Dataset, registry: Registry) -> None:
    tables = {"backbone": tiny_weights_dataset()}
    b = fit_spec(
        SPEC, shapes, side_tables=tables, registry=registry, raw_schema=image_schema(shapes)
    )
    assert {"accuracy", "brier"} <= set(b.metrics.values)
    out = score_bundle(
        b,
        load_score_request(REQ + "mode: predict\n"),
        shapes.isel(image=slice(0, 4)).drop_vars("label"),
        side_tables=tables,
        registry=registry,
        return_full=True,
    )
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.sizes["image"] == 4 and "class" in y_hat.dims
