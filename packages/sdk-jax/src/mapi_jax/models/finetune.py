"""``dl.finetune_classifier``: a pretrained backbone (a weights side table) with a new head.

The pretrained weights are a dataset version (a ``safetensors`` ingest) named by the setting
``pretrained`` in ``data.side_tables``; the architecture comes from the weights. Training runs
in up to two phases: ``head_epochs`` with the backbone frozen, then the rest of
``train.epochs`` with every parameter (the backbone's updates scaled by
``backbone_lr_scale``). ``freeze: backbone`` keeps the backbone frozen throughout.
Gray images into an RGB backbone repeat their channel (and RGB into gray average).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal

import numpy as np
from mapi_sdk.collection import ImageLoader
from mapi_sdk.models import FitContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig
from pydantic import Field

from mapi_jax.models.base import (
    DLImageModel,
    ImageSettings,
    Name,
    checkpoint_every,
    checkpoint_key,
)
from mapi_jax.nets import ArchSpec, Network
from mapi_jax.tasks import ClassificationTask
from mapi_jax.trainer import Trainer, TrainResult
from mapi_jax.weights import Pretrained, read_pretrained

__all__ = ["FinetuneClassifier", "FinetuneClassifierSettings"]


class FinetuneClassifierSettings(ImageSettings):
    """Settings of ``dl.finetune_classifier``."""

    pretrained: Name = Field(
        "pretrained", description="The data.side_tables entry with the pretrained weights"
    )
    freeze: Literal["none", "backbone"] = Field("none", description="backbone: train the head only")
    head_epochs: int = Field(
        1, ge=0, le=10_000, description="Epochs that train the head only before the rest"
    )
    backbone_lr_scale: float = Field(
        0.1, gt=0, le=1, description="Learning rate of the backbone relative to the head"
    )
    dropout: float | None = Field(
        None, ge=0, lt=1, description="Dropout before the head (default: the weights' value)"
    )


def _is_head(path: tuple[Any, ...], value: Any) -> bool:
    return bool(path) and str(path[0]) == "head"


class FinetuneClassifier(DLImageModel[FinetuneClassifierSettings]):
    """Fine-tune a pretrained ResNet backbone (from a weights side table) for image classes."""

    id = "dl.finetune_classifier"
    Settings: ClassVar[type[FinetuneClassifierSettings]] = FinetuneClassifierSettings
    side_table_params = ("pretrained",)
    description = (
        "A pretrained residual network (weights from a dataset version) with a new classifier "
        "head, trained on labelled images. The backbone can stay frozen. No download: the "
        "weights are a pinned side table."
    )

    def _pretrained(self, s: FinetuneClassifierSettings, ctx: FitContext) -> Pretrained:
        table = ctx.side_tables.get(s.pretrained)
        if table is None:
            raise MapiError(
                f"the fit has no side table {s.pretrained!r} with the pretrained weights",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings/pretrained",
            )
        pre = read_pretrained(table, name=s.pretrained)
        if pre.arch.kind != "resnet":
            raise MapiError(
                f"the weights {s.pretrained!r} are an {pre.arch.kind}; the model needs a resnet",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/side_tables/{s.pretrained}",
            )
        return pre

    def input_mode(
        self, s: FinetuneClassifierSettings, loader: ImageLoader, ctx: FitContext
    ) -> Literal["gray", "rgb"]:
        return "rgb" if self._pretrained(s, ctx).arch.channels == 3 else "gray"

    def input_size(
        self, s: FinetuneClassifierSettings, loader: ImageLoader, ctx: FitContext
    ) -> tuple[int, int]:
        if s.image_size is not None:
            return (int(s.image_size[0]), int(s.image_size[1]))
        size = self._pretrained(s, ctx).arch.image_size
        return (int(size[0]), int(size[1])) if size is not None else loader.size

    def build(
        self,
        s: FinetuneClassifierSettings,
        channels: int,
        size: tuple[int, int],
        n_out: int,
        seed: int,
        ctx: FitContext,
    ) -> tuple[Network, ArchSpec]:
        pre = self._pretrained(s, ctx)
        update: dict[str, Any] = {"image_size": size}
        if s.dropout is not None:
            update["dropout"] = s.dropout
        arch = pre.arch.model_copy(update=update)
        net = Pretrained(arch, pre.tensors, pre.sha256).network(n_out, seed=seed)
        if ctx.tracking and pre.sha256:
            ctx.log_params({"dl.pretrained.sha256": pre.sha256})
        return net, arch

    def train(
        self,
        net: Network,
        arch: ArchSpec,
        task: ClassificationTask,
        s: FinetuneClassifierSettings,
        cfg: FitConfig,
        ctx: FitContext,
        train: ImageLoader,
        val: ImageLoader | None,
        preprocess: Callable[[np.ndarray], Any],
        seed: int,
    ) -> TrainResult:
        t = s.train
        head_epochs = t.epochs if s.freeze == "backbone" else min(s.head_epochs, t.epochs)
        common: dict[str, Any] = {
            "seed": seed,
            "preprocess": preprocess,
            "checkpoint_every": checkpoint_every(cfg),
            "augment": s.augment if s.augment.active else None,
            "arch_params": arch.model_dump(mode="json"),
        }
        first: TrainResult | None = None
        if head_epochs > 0:
            first = Trainer(
                net,
                task,
                t.model_copy(update={"epochs": head_epochs}),
                ctx,
                trainable=_is_head,
                checkpoint_key=checkpoint_key(ctx, self.id, s, seed, "head"),
                **common,
            ).fit(train, val)
            if head_epochs >= t.epochs:
                return first
            net = first.network
        scale = s.backbone_lr_scale
        second = Trainer(
            net,
            task,
            t.model_copy(update={"epochs": t.epochs - head_epochs}),
            ctx,
            lr_scale=lambda name: 1.0 if name.startswith("head") else scale,
            checkpoint_key=checkpoint_key(ctx, self.id, s, seed, "all"),
            epoch_offset=head_epochs,
            step_offset=0 if first is None else first.steps,
            **common,
        ).fit(train, val)
        return _join(first, second) if first is not None else second


def _join(first: TrainResult, second: TrainResult) -> TrainResult:
    """One result of two phases: histories end to end, the second phase's network."""
    import xarray as xr

    h1, h2 = first.history, second.history
    history = xr.concat([h1, h2], dim="epoch", data_vars="all", coords="minimal", join="outer")
    attrs = dict(h2.attrs)
    attrs["epochs_run"] = first.epochs_run + second.epochs_run
    attrs["steps"] = first.steps + second.steps
    history.attrs = attrs
    return TrainResult(
        network=second.network,
        history=history,
        best_epoch=second.best_epoch,
        epochs_run=first.epochs_run + second.epochs_run,
        stopped_early=second.stopped_early,
        steps=first.steps + second.steps,
        misclassified=second.misclassified,
    )
