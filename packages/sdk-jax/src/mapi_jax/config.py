"""Training and uncertainty settings shared by the ``dl.*`` models.

Every model embeds :class:`TrainConfig` (``settings.train``) and :class:`UncertaintyConfig`
(``settings.uncertainty``); both are frozen and reject unknown keys, so they appear in the
model's settings JSON schema as plain nested objects.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["EarlyStopping", "TrainConfig", "UncertaintyConfig"]

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class EarlyStopping(BaseModel):
    """Stop when the monitored validation metric has not improved for ``patience`` evaluations."""

    model_config = _FROZEN

    monitor: Literal["val_loss", "val_accuracy", "val_rmse"] = Field(
        "val_loss", description="The validation metric to watch"
    )
    patience: int = Field(5, ge=1, le=1000, description="Evaluations without improvement")
    min_delta: float = Field(0.0, ge=0, description="The smallest change that counts as better")
    restore_best: bool = Field(True, description="Keep the weights of the best evaluation")


class TrainConfig(BaseModel):
    """How a network is trained (optimizer, schedule, validation, logging)."""

    model_config = _FROZEN

    epochs: int = Field(20, ge=1, le=10_000, description="Passes over the training rows")
    batch_size: int = Field(64, ge=1, le=8192, description="Rows per gradient step")
    optimizer: Literal["adamw", "adam", "sgd"] = "adamw"
    learning_rate: float = Field(1e-3, gt=0, le=10, description="Peak learning rate")
    weight_decay: float = Field(1e-4, ge=0, le=1, description="Decoupled weight decay (adamw)")
    momentum: float = Field(0.9, ge=0, lt=1, description="Momentum (sgd)")
    schedule: Literal["constant", "cosine", "warmup_cosine"] = "warmup_cosine"
    warmup_epochs: float = Field(1.0, ge=0, le=100, description="Linear warm-up (warmup_cosine)")
    grad_clip: float | None = Field(1.0, gt=0, description="Clip the global gradient norm")
    label_smoothing: float = Field(0.0, ge=0, lt=1, description="Classification only")
    val_fraction: float = Field(
        0.1, ge=0, lt=0.9, description="Stratified share of the training rows for validation"
    )
    val_split_value: str | None = Field(
        "val", description="Rows whose split coord equals this value are the validation set"
    )
    early_stopping: EarlyStopping | None = Field(default_factory=EarlyStopping)
    log_every_steps: int | None = Field(
        None, ge=1, description="Steps between logged training metrics (default: <= 1000 points)"
    )
    eval_every_epochs: int = Field(1, ge=1, description="Epochs between validation passes")
    media_every_epochs: int | None = Field(
        None, ge=1, description="Epochs between logged images (default: at most 20 times)"
    )
    max_steps: int | None = Field(None, ge=1, description="Stop after this many gradient steps")


class UncertaintyConfig(BaseModel):
    """How a trained network gives draws (``chain=1``, ``draw``)."""

    model_config = _FROZEN

    method: Literal["laplace", "mc_dropout", "none"] = Field(
        "laplace",
        description="laplace: last-layer Laplace; mc_dropout: dropout at score; none: one draw",
    )
    draws: int | None = Field(
        None, ge=1, le=1000, description="Draws per prediction (default: fit draws, else 100)"
    )
    hessian: Literal["auto", "full", "diag"] = Field(
        "auto", description="auto: full when (features + 1) x outputs <= 2048"
    )
    max_rows: int = Field(20_000, ge=10, description="Training rows used for the Laplace Hessian")
    prior_precision: float | None = Field(
        None, gt=0, description="None: maximise the Laplace evidence on a grid 1e-4..1e4"
    )
