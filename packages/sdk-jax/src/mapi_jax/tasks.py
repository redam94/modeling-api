"""Training tasks: the loss and the metrics of a classification or a regression head.

Every function takes a per-row weight ``w`` (0 for padding rows and unknown labels), so the
trainer can feed fixed-shape batches and XLA compiles each step once. Sums (not means) come
back from the batch functions; the trainer divides by the summed weight.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import jax
import jax.numpy as jnp
import numpy as np
import optax

__all__ = ["ClassificationTask", "RegressionTask", "Task"]


@dataclass(frozen=True, slots=True)
class ClassificationTask:
    """Softmax cross-entropy over ``classes`` (targets are int codes, ``-1`` = unknown)."""

    classes: Sequence[str]
    label_smoothing: float = 0.0
    kind: Literal["classification"] = field(default="classification", init=False)

    @property
    def n_out(self) -> int:
        return len(self.classes)

    @property
    def metric(self) -> str:
        """The second metric next to ``loss``."""
        return "accuracy"

    def weights(self, targets: np.ndarray) -> np.ndarray:
        return (np.asarray(targets) >= 0).astype(np.float32)

    def batch_sums(
        self, logits: jax.Array, targets: jax.Array, w: jax.Array
    ) -> tuple[jax.Array, jax.Array]:
        """``(sum of weighted losses, sum of weighted hits)`` of a batch."""
        k = logits.shape[-1]
        codes = jnp.clip(targets, 0, k - 1).astype(jnp.int32)
        onehot = jax.nn.one_hot(codes, k)
        if self.label_smoothing > 0:
            onehot = optax.smooth_labels(onehot, self.label_smoothing)
        loss = optax.softmax_cross_entropy(logits, onehot)
        hits = (jnp.argmax(logits, axis=-1) == targets).astype(jnp.float32)
        return jnp.sum(loss * w), jnp.sum(hits * w)

    def eval_sums(
        self, logits: jax.Array, targets: jax.Array, w: jax.Array
    ) -> tuple[jax.Array, jax.Array]:
        """As :meth:`batch_sums` but without label smoothing (validation loss is plain NLL);
        unknown labels (``-1``) count as misses with weight 1 (the caller sets ``w``)."""
        k = logits.shape[-1]
        known = targets >= 0
        codes = jnp.clip(targets, 0, k - 1).astype(jnp.int32)
        nll = optax.softmax_cross_entropy_with_integer_labels(logits, codes)
        loss = jnp.where(known, nll, 0.0)
        hits = ((jnp.argmax(logits, axis=-1) == targets) & known).astype(jnp.float32)
        return jnp.sum(loss * w), jnp.sum(hits * w)

    def metric_value(self, loss_sum: float, metric_sum: float, n: float) -> dict[str, float]:
        n = max(n, 1e-12)
        return {"loss": loss_sum / n, "accuracy": metric_sum / n}


@dataclass(frozen=True, slots=True)
class RegressionTask:
    """Mean squared error on a standardised target ``(y - mean) / std``.

    ``rmse`` and ``mae`` are reported in the target's units (times ``std``).
    """

    mean: float = 0.0
    std: float = 1.0
    kind: Literal["regression"] = field(default="regression", init=False)

    @property
    def n_out(self) -> int:
        return 1

    @property
    def metric(self) -> str:
        return "rmse"

    def weights(self, targets: np.ndarray) -> np.ndarray:
        return np.isfinite(np.asarray(targets, dtype=float)).astype(np.float32)

    def batch_sums(
        self, logits: jax.Array, targets: jax.Array, w: jax.Array
    ) -> tuple[jax.Array, jax.Array]:
        """``(sum of weighted squared errors, sum of weighted absolute errors)``."""
        err = logits[:, 0] - jnp.nan_to_num(targets)
        return jnp.sum(err**2 * w), jnp.sum(jnp.abs(err) * w)

    def eval_sums(
        self, logits: jax.Array, targets: jax.Array, w: jax.Array
    ) -> tuple[jax.Array, jax.Array]:
        return self.batch_sums(logits, targets, w)

    def metric_value(self, loss_sum: float, metric_sum: float, n: float) -> dict[str, float]:
        n = max(n, 1e-12)
        mse = loss_sum / n
        return {
            "loss": mse,
            "rmse": float(np.sqrt(mse)) * self.std,
            "mae": metric_sum / n * self.std,
        }


Task = ClassificationTask | RegressionTask
