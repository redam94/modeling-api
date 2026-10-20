"""Steps registered under the ``dl`` namespace (entry point ``mapi.steps``).

``dl.embed`` turns images into features with a frozen pretrained backbone (a weights side
table): ``{out}({image_dim}, {feature_dim})`` float32, the backbone's global-mean-pooled
activations of each image (pixels scaled to ``[0, 1]``). A head model then reads the features:
``sklearn.multiclass_classifier`` (it flattens the feature dim into columns) or
``tabular.image_logistic`` with ``input: features``. The step learns nothing; its state
records the sha256 of the weights it used.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import xarray as xr
from mapi_sdk.collection import ArrayImageLoader
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState
from mapi_spec.effects import SchemaEffect, StepKind
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, JsonValue, model_validator

from mapi_jax.models.base import Name, Normalize, network_features
from mapi_jax.weights import read_pretrained

__all__ = ["STEPS", "EmbedSettings", "EmbedState", "EmbedStep"]


class EmbedSettings(StepSettings):
    """Settings of ``dl.embed``."""

    pixels: Name = Field("pixels", description="The image variable (image, y, x, channel)")
    weights: Name = Field("weights", description="The data.side_tables entry with the weights")
    out: Name = Field("embedding", description="The feature variable the step writes")
    feature_dim: Name = Field("feature", description="The dim of the features")
    image_dim: Name = Field("image", description="The dim that lists the images")
    size: tuple[int, int] | None = Field(
        None, description="Height and width the images are resized to (default: the weights')"
    )
    batch_size: int = Field(64, ge=1, le=4096, description="Images per forward pass")
    keep_pixels: bool = Field(False, description="Keep the pixel variable after the step")

    @model_validator(mode="after")
    def _check(self) -> EmbedSettings:
        if self.out == self.pixels:
            raise ValueError("out must differ from pixels")
        if self.feature_dim in {self.image_dim, "y", "x", "channel"}:
            raise ValueError("feature_dim must be a new dim")
        if self.size is not None and min(self.size) < 1:
            raise ValueError("size must be (height, width) with both >= 1")
        return self


class EmbedState(StepState):
    weights_sha256: str | None = None


class EmbedStep(Step[EmbedSettings, EmbedState]):
    """Image features from a frozen pretrained backbone (a weights side table)."""

    id = "dl.embed"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = EmbedSettings
    State = EmbedState
    var_params = ("pixels",)
    dim_params = ("image_dim",)
    side_table_params = ("weights",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "feature_dim", "image_dim"],
        "transforms_dims": {"{feature_dim}": {"kind": "integer", "policy": "fixed"}},
        "adds": {
            "{out}": {
                "dims": ["{image_dim}", "{feature_dim}"],
                "dtype": "float32",
                "role": "covariate",
            }
        },
    }
    summary = "Image features from a frozen pretrained network"
    summary_plain = "Turns each image into a list of numbers with a pretrained network"

    def effect(self, s: EmbedSettings) -> SchemaEffect:
        eff = super().effect(s)
        if s.keep_pixels:
            return eff
        return eff.model_copy(update={"drops": [s.pixels]})

    def fit(self, ds: xr.Dataset, s: EmbedSettings, ctx: StepContext) -> EmbedState:
        pre = read_pretrained(ctx.side_table(s.weights), name=s.weights)
        return self.new_state(weights_sha256=pre.sha256)

    def transform(
        self, ds: xr.Dataset, s: EmbedSettings, st: EmbedState, ctx: StepContext
    ) -> xr.Dataset:
        if s.pixels not in ds:
            raise MapiError(
                f"step {ctx.step_id!r}: the data has no image variable {s.pixels!r}",
                code=ErrorCode.SCHEMA_INVALID,
            )
        pre = read_pretrained(ctx.side_table(s.weights), name=s.weights)
        if pre.arch.kind != "resnet":
            raise MapiError(
                f"step {ctx.step_id!r}: the weights {s.weights!r} are not an image network",
                code=ErrorCode.SCHEMA_INVALID,
            )
        loader: Any = ArrayImageLoader(ds, pixels=s.pixels, target=None, dim=s.image_dim)
        size = s.size or pre.arch.image_size or loader.size
        loader = loader.with_mode("rgb" if pre.arch.channels == 3 else "gray").with_size(
            (int(size[0]), int(size[1]))
        )
        net = pre.network(1, seed=0)
        bs = max(1, min(s.batch_size, loader.n))
        norm = Normalize((0.0,), (1.0,), "unit")
        feats = network_features(net, (b.pixels for b in loader.batches(bs)), norm, bs)
        if feats.size == 0:
            feats = np.zeros((loader.n, pre.arch.features))
        out = xr.DataArray(
            feats.astype(np.float32),
            dims=(s.image_dim, s.feature_dim),
            coords={
                s.image_dim: ds[s.image_dim].values,
                s.feature_dim: np.arange(feats.shape[1], dtype=np.int64),
            },
        )
        result = ds.assign({s.out: out})
        if not s.keep_pixels:
            result = result.drop_vars(s.pixels)
        return result


STEPS: list[type[Step[Any, Any]]] = [EmbedStep]
