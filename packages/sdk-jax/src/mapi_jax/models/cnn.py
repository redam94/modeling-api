"""``dl.cnn_classifier``: a small GroupNorm ResNet trained from scratch on images."""

from __future__ import annotations

from typing import ClassVar

from mapi_sdk.models import FitContext
from pydantic import Field, model_validator

from mapi_jax.models.base import DLImageModel, ImageSettings
from mapi_jax.nets import MAX_STAGES, ArchSpec, Network, build_network

__all__ = ["CNNClassifier", "CNNClassifierSettings"]


class CNNClassifierSettings(ImageSettings):
    """Settings of ``dl.cnn_classifier``."""

    widths: list[int] = Field(
        [16, 32, 64], min_length=1, max_length=MAX_STAGES, description="Channels of each stage"
    )
    blocks: list[int] = Field(
        [1, 1, 1], min_length=1, max_length=MAX_STAGES, description="Residual blocks per stage"
    )
    groups: int = Field(8, ge=1, le=64, description="GroupNorm groups (at most)")
    stem_stride: int = Field(1, ge=1, le=2, description="Stride of the first convolution")
    dropout: float = Field(0.1, ge=0, lt=1, description="Dropout before the head")

    @model_validator(mode="after")
    def _stages(self) -> CNNClassifierSettings:
        if len(self.widths) != len(self.blocks):
            raise ValueError("widths and blocks must have the same length")
        if any(w < 1 or w > 1024 for w in self.widths) or any(b < 1 or b > 16 for b in self.blocks):
            raise ValueError("each width is 1..1024 and each stage takes 1..16 blocks")
        return self


class CNNClassifier(DLImageModel[CNNClassifierSettings]):
    """A residual convolutional network (GroupNorm, global mean pooling) for image classes.

    It trains on an image dataset (``fit``) or streams an image collection (``fit_batches``).
    """

    id = "dl.cnn_classifier"
    Settings: ClassVar[type[CNNClassifierSettings]] = CNNClassifierSettings
    description = (
        "A small residual convolutional network trained on labelled images. It streams large "
        "image collections in batches, logs training and validation curves, and gives "
        "probability draws by a last-layer Laplace approximation or MC dropout."
    )

    def build(
        self,
        s: CNNClassifierSettings,
        channels: int,
        size: tuple[int, int],
        n_out: int,
        seed: int,
        ctx: FitContext,
    ) -> tuple[Network, ArchSpec]:
        arch = ArchSpec(
            kind="resnet",
            channels=channels,
            widths=list(s.widths),
            blocks=list(s.blocks),
            groups=s.groups,
            stem_stride=1 if s.stem_stride == 1 else 2,
            image_size=size,
            dropout=s.dropout,
        )
        return build_network(arch, n_out, seed=seed), arch
