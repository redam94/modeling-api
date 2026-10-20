"""The training loop shared by every ``dl.*`` model.

:class:`Trainer` fits a :class:`~mapi_jax.nets.Network` on an image loader
(:class:`mapi_sdk.collection.ImageLoader`) or a :class:`~mapi_jax.tabular.TabularLoader`:

* fixed-shape batches (training ``drop_last``; evaluation batches padded to the batch size
  with a zero weight), so XLA compiles the training and the evaluation step once;
* determinism: parameters from ``nnx.Rngs(params=seed)`` (the caller builds the network), the
  dropout key of step ``t`` is ``fold_in(key(seed + 1), t)``, the loader order of epoch ``e``
  is a function of ``(seed, e)``: two fits with the same seed give the same weights bytes on
  the same backend, and a resumed fit continues exactly where the checkpoint stopped;
* tracking (contract with the ``ml.training_curves`` chart): ``loss`` and ``accuracy``
  (classification) or ``rmse``/``mae`` (regression) with the label ``split=train`` every
  ``log_every_steps`` steps and ``split=val`` at each evaluation (step = the global step at
  the end of the epoch); ``lr`` and ``grad_norm`` with the training points; ``epoch`` (epoch
  index + 1) at each epoch end; params ``dl.arch.*``,
  ``dl.train.*``, ``dl.backend``, ``dl.device``, ``dl.parameters``; media
  ``val_predictions`` and ``confusion`` (label ``split=val``) at most 20 times;
* a structured log line per epoch with the fields ``epoch``, ``train_loss``, ``val_loss`` and
  ``images_per_second``. Throughput is wall-clock time, so it is not a metric: the bundle's
  ``metrics_history`` must be the same for two fits with the same seed;
* early stopping on ``val_loss``/``val_accuracy``/``val_rmse`` with the best weights restored;
* file checkpoints every ``checkpoint_every`` epochs through a
  :class:`~mapi_sdk.checkpoints.FileCheckpointSink` (current weights + ``best.*`` weights,
  optimiser state, and the loop state in the JSON meta), resumed when the key exists.

The ``training`` group (:attr:`TrainResult.history`) holds one row per kept epoch: at most
200 epochs, and for a classifier with validation data the evaluated epochs only (at most 50,
always the first, best and last), so ``confusion(epoch, true_class, pred_class)`` has a
matrix at every epoch of the group. ``thinned_from`` gives the epoch count before thinning.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import jax
import jax.numpy as jnp
import numpy as np
import optax
import xarray as xr
from flax import nnx
from mapi_sdk.checkpoints import FileCheckpointSink
from mapi_sdk.models import FitContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.media import MAX_MEDIA_CAPTION_CHARS

from mapi_jax import _jax
from mapi_jax.checkpoint import (
    json_safe,
    optimizer_bytes,
    restore_optimizer,
)
from mapi_jax.config import TrainConfig
from mapi_jax.media import MAX_GRID_IMAGES, confusion_image, prediction_grid, thumbnail
from mapi_jax.nets import Network, network_tensors, param_count, set_tensors
from mapi_jax.tasks import ClassificationTask, RegressionTask

__all__ = [
    "MAX_CONFUSION_CLASSES",
    "MAX_CONFUSION_EPOCHS",
    "MAX_HISTORY_EPOCHS",
    "MAX_MEDIA_STEPS",
    "MISCLASSIFIED_GROUP",
    "TRAINING_GROUP",
    "TrainResult",
    "Trainer",
]

TRAINING_GROUP = "training"
MISCLASSIFIED_GROUP = "misclassified"
MAX_HISTORY_EPOCHS = 200
MAX_CONFUSION_EPOCHS = 50
MAX_CONFUSION_CLASSES = 50
MAX_MEDIA_STEPS = 20
MAX_MISCLASSIFIED = 32
THUMB_SIDE = 48
_BEST = "best."
_MONITOR_MAX = {"val_accuracy"}
_KEEP_HISTORY = 4 * MAX_HISTORY_EPOCHS
"""Epoch rows the loop state keeps: more are halved (first, best, last and the epochs with a
confusion matrix stay), so the checkpoint meta of a long fit stays small enough to resume."""
_KEEP_CONFUSION = 4 * MAX_CONFUSION_EPOCHS
"""Confusion matrices the loop state keeps (halved the same way)."""


class _Loader(Protocol):
    @property
    def n(self) -> int: ...

    def batches(self, batch_size: int, **kw: Any) -> Iterator[Any]: ...


@dataclass(slots=True)
class TrainResult:
    """What :meth:`Trainer.fit` returns (the network holds the kept weights)."""

    network: Network
    history: xr.Dataset
    best_epoch: int
    epochs_run: int
    stopped_early: bool
    steps: int
    misclassified: xr.Dataset | None


@dataclass(slots=True)
class _Eval:
    loss: float
    metric: float
    values: dict[str, float]
    probs: np.ndarray | None  # (n, K) classification
    codes: np.ndarray | None  # (n,) int
    confusion: np.ndarray | None
    sample: np.ndarray | None  # raw pixels of the first rows (media)


def _unpack(batch: Any) -> tuple[np.ndarray, np.ndarray | None, np.ndarray]:
    """(raw inputs, targets, positions) of an image or a tabular batch."""
    if hasattr(batch, "pixels"):
        return np.asarray(batch.pixels), batch.codes, np.asarray(batch.index)
    return np.asarray(batch.x), batch.y, np.asarray(batch.index)


def _pad(a: np.ndarray, size: int) -> np.ndarray:
    if a.shape[0] == size:
        return a
    pad = np.zeros((size - a.shape[0], *a.shape[1:]), dtype=a.dtype)
    return np.concatenate([a, pad], axis=0)


def _flat_params(prefix: str, doc: Mapping[str, Any], out: dict[str, Any]) -> None:
    for k, v in doc.items():
        key = f"{prefix}.{k}"
        if isinstance(v, Mapping):
            _flat_params(key, v, out)
        elif v is None:
            continue
        elif isinstance(v, bool | int | float | str):
            out[key] = v
        else:
            out[key] = json.dumps(v)


def _scale_by_path(scale: Callable[[str], float]) -> optax.GradientTransformation:
    """Multiply each update by ``scale(<dotted path>)`` (for example a smaller backbone lr)."""

    def path_name(path: tuple[Any, ...]) -> str:
        parts = []
        for k in path:
            name = getattr(k, "key", getattr(k, "name", getattr(k, "idx", None)))
            if name is not None and name != "value":
                parts.append(str(name))
        return ".".join(parts)

    def init(params: Any) -> optax.EmptyState:
        return optax.EmptyState()

    def update(updates: Any, state: Any, params: Any = None) -> tuple[Any, Any]:
        out = jax.tree_util.tree_map_with_path(lambda p, u: u * scale(path_name(p)), updates)
        return out, state

    return optax.GradientTransformation(init, update)


def make_schedule(cfg: TrainConfig, total_steps: int, steps_per_epoch: int) -> optax.Schedule:
    lr = cfg.learning_rate
    total = max(total_steps, 1)
    if cfg.schedule == "constant":
        return optax.constant_schedule(lr)
    warmup = round(cfg.warmup_epochs * steps_per_epoch) if cfg.schedule == "warmup_cosine" else 0
    warmup = min(warmup, total - 1)
    if warmup <= 0:
        return optax.cosine_decay_schedule(lr, total)
    return optax.warmup_cosine_decay_schedule(0.0, lr, warmup, total, end_value=0.0)


def make_optimizer(
    cfg: TrainConfig, schedule: optax.Schedule, lr_scale: Callable[[str], float] | None
) -> optax.GradientTransformation:
    parts: list[optax.GradientTransformation] = []
    if cfg.grad_clip is not None:
        parts.append(optax.clip_by_global_norm(cfg.grad_clip))
    if cfg.optimizer == "adamw":
        parts.append(optax.adamw(schedule, weight_decay=cfg.weight_decay))
    elif cfg.optimizer == "adam":
        parts.append(optax.adam(schedule))
    else:
        parts.append(optax.sgd(schedule, momentum=cfg.momentum or None))
    if lr_scale is not None:
        parts.append(_scale_by_path(lr_scale))
    return optax.chain(*parts)


class Trainer:
    """Train ``network`` for ``task`` with ``cfg``; see the module docstring.

    ``preprocess`` maps a batch's raw inputs (uint8 pixels or standardised table rows) to the
    float32 network input. ``trainable(path, value)`` (an nnx filter) limits the parameters
    that train (default: all). ``lr_scale(name)`` scales the update of each parameter by its
    dotted name. ``epoch_offset``/``step_offset`` shift the logged epochs and steps (a second
    training phase continues the first one's series).
    """

    def __init__(
        self,
        network: Network,
        task: ClassificationTask | RegressionTask,
        cfg: TrainConfig,
        ctx: FitContext,
        *,
        seed: int,
        preprocess: Callable[[np.ndarray], Any],
        trainable: Callable[..., bool] | None = None,
        checkpoint_key: str | None = None,
        checkpoint_every: int | None = None,
        augment: Any | None = None,
        lr_scale: Callable[[str], float] | None = None,
        epoch_offset: int = 0,
        step_offset: int = 0,
        arch_params: Mapping[str, Any] | None = None,
    ) -> None:
        self.network = network
        self.task = task
        self.cfg = cfg
        self.ctx = ctx
        self.seed = int(seed)
        self.preprocess = preprocess
        self.trainable = trainable
        self.checkpoint_key = checkpoint_key
        self.checkpoint_every = checkpoint_every
        self.augment = augment
        self.lr_scale = lr_scale
        self.epoch_offset = epoch_offset
        self.step_offset = step_offset
        self.arch_params = dict(arch_params or {})

    # ------------------------------------------------------------------------------ setup
    def _sink(self) -> FileCheckpointSink | None:
        sink = self.ctx.checkpoint_sink
        if self.checkpoint_key and self.checkpoint_every and isinstance(sink, FileCheckpointSink):
            return sink
        return None

    def _log_params(self) -> None:
        if not self.ctx.tracking:
            return
        params: dict[str, Any] = {}
        _flat_params("dl.arch", self.arch_params, params)
        _flat_params("dl.train", self.cfg.model_dump(mode="json"), params)
        params["dl.backend"] = _jax.backend()
        params["dl.device"] = _jax.device_kind()
        params["dl.parameters"] = param_count(self.network)
        try:
            self.ctx.log_params(params)
        except ValueError as exc:  # a second phase with other values: keep the first ones
            self.ctx.log_line(f"params not logged again ({exc})", "debug")

    # -------------------------------------------------------------------------------- fit
    def fit(self, train: Any, val: Any | None) -> TrainResult:
        cfg, task, ctx = self.cfg, self.task, self.ctx
        if train.n < 1:
            raise MapiError("no training rows reach the network", code=ErrorCode.SCHEMA_INVALID)
        bs = min(cfg.batch_size, train.n)
        steps_per_epoch = max(train.n // bs, 1)
        total_steps = cfg.epochs * steps_per_epoch
        if cfg.max_steps is not None:
            total_steps = min(total_steps, cfg.max_steps)
        schedule = make_schedule(cfg, total_steps, steps_per_epoch)
        tx = make_optimizer(cfg, schedule, self.lr_scale)
        wrt: Any = nnx.Param if self.trainable is None else nnx.All(nnx.Param, self.trainable)
        net = self.network
        opt = nnx.Optimizer(net, tx, wrt=wrt)
        base_key = jax.random.key(self.seed + 1)

        @nnx.jit
        def train_step(
            net: Network,
            opt: nnx.Optimizer[Any],
            x: jax.Array,
            y: jax.Array,
            w: jax.Array,
            key: jax.Array,
        ) -> tuple[jax.Array, jax.Array, jax.Array]:
            def loss_fn(net: Network) -> tuple[jax.Array, tuple[jax.Array, jax.Array]]:
                ls, ms = task.batch_sums(net(x, key), y, w)
                return ls / jnp.maximum(jnp.sum(w), 1.0), (ls, ms)

            grad_fn = nnx.value_and_grad(loss_fn, argnums=nnx.DiffState(0, wrt), has_aux=True)
            (_, (ls, ms)), grads = grad_fn(net)
            opt.update(net, grads)
            return ls, ms, optax.tree.norm(grads)

        @nnx.jit
        def eval_step(
            net: Network, x: jax.Array, y: jax.Array, w: jax.Array
        ) -> tuple[jax.Array, jax.Array, jax.Array]:
            logits = net(x)
            ls, ms = task.eval_sums(logits, y, w)
            return ls, ms, logits

        self._log_params()
        has_val = val is not None and val.n > 0
        es = cfg.early_stopping if has_val else None
        monitor = "val_loss" if has_val else "train_loss"
        if es is not None:
            monitor = es.monitor
            if monitor != "val_loss" and monitor != f"val_{task.metric}":
                ctx.log_line(
                    f"early stopping watches val_loss: {monitor} is not a metric of this task",
                    "warning",
                )
                monitor = "val_loss"
        state = _LoopState()
        sink = self._sink()
        if sink is not None and self.checkpoint_key is not None:
            found = sink.load_files(self.checkpoint_key)
            if found is not None:
                state = self._resume(found, opt)
                msg = f"Resumed from epoch {state.epoch}."
                ctx.log_line(msg, "info")
                ctx.info(msg)
        log_every = cfg.log_every_steps or max(1, total_steps // 1000)
        media_every = cfg.media_every_epochs or max(
            cfg.eval_every_epochs, math.ceil(cfg.epochs / MAX_MEDIA_STEPS)
        )
        stopped = False
        epoch = state.epoch
        while epoch < cfg.epochs and not stopped:
            ctx.check_cancelled()
            loss_sum = jnp.zeros(())
            metric_sum = jnp.zeros(())
            weight_sum = 0.0
            started, rows = time.perf_counter(), 0
            for i, batch in enumerate(
                train.batches(
                    bs,
                    epoch=epoch,
                    shuffle=True,
                    seed=self.seed,
                    augment=self.augment,
                    drop_last=True,
                )
            ):
                ctx.check_cancelled()
                raw, targets, _ = _unpack(batch)
                y = self._targets(targets, raw.shape[0])
                w = task.weights(y)
                key = jax.random.fold_in(base_key, state.step)
                ls, ms, gnorm = train_step(
                    net, opt, jnp.asarray(self.preprocess(raw)), jnp.asarray(y), jnp.asarray(w), key
                )
                state.step += 1
                loss_sum = loss_sum + ls
                metric_sum = metric_sum + ms
                weight_sum += float(w.sum())
                rows += raw.shape[0]
                if ctx.tracking and state.step % log_every == 0:
                    bw = float(max(w.sum(), 1e-12))
                    values = task.metric_value(float(ls), float(ms), bw)
                    gstep = self.step_offset + state.step
                    ctx.log_metrics(values, step=gstep, labels={"split": "train"})
                    ctx.log_metrics(
                        {"lr": float(schedule(state.step - 1)), "grad_norm": float(gnorm)},
                        step=gstep,
                    )
                ctx.progress(
                    (epoch + (i + 1) / steps_per_epoch) / cfg.epochs,
                    f"epoch {epoch + 1}/{cfg.epochs}",
                )
                if cfg.max_steps is not None and state.step >= cfg.max_steps:
                    stopped = True
                    break
            epoch += 1
            state.epoch = epoch
            train_values = task.metric_value(float(loss_sum), float(metric_sum), weight_sum)
            row: dict[str, float] = {
                "train_loss": train_values["loss"],
                f"train_{task.metric}": train_values[task.metric],
                "lr": float(schedule(max(state.step - 1, 0))),
            }
            evaluated = has_val and (
                epoch % cfg.eval_every_epochs == 0 or epoch == cfg.epochs or stopped
            )
            gstep = self.step_offset + state.step
            if evaluated:
                ev = self._evaluate(eval_step, net, val, bs)
                row["val_loss"] = ev.values["loss"]
                row[f"val_{task.metric}"] = ev.values[task.metric]
                if ctx.tracking:
                    ctx.log_metrics(ev.values, step=gstep, labels={"split": "val"})
                if ev.confusion is not None and isinstance(task, ClassificationTask):
                    state.confusion[epoch] = ev.confusion.tolist()
                if (
                    ctx.tracking_media
                    and state.media < MAX_MEDIA_STEPS
                    and (epoch % media_every == 0 or epoch == 1)
                ):
                    self._log_media(ev, val, gstep)
                    state.media += 1
                value = row[monitor]
                better = state.best_value is None or (
                    value > state.best_value + (es.min_delta if es else 0.0)
                    if monitor in _MONITOR_MAX
                    else value < state.best_value - (es.min_delta if es else 0.0)
                )
                if better:
                    state.best_value, state.best_epoch, state.bad = float(value), epoch, 0
                    state.best_weights = network_tensors(net)
                else:
                    state.bad += 1
                    if es is not None and state.bad >= es.patience:
                        stopped = True
                        state.stopped_early = True
            elif not has_val:
                state.best_epoch = epoch
            state.history.append({"epoch": epoch, **row, "evaluated": float(evaluated)})
            _compact(state)
            if ctx.tracking:
                ctx.log_metrics({"epoch": float(self.epoch_offset + epoch)}, step=gstep)
                fields: dict[str, Any] = {
                    "epoch": self.epoch_offset + epoch,
                    "train_loss": round(row["train_loss"], 6),
                    "images_per_second": round(rows / max(time.perf_counter() - started, 1e-9), 1),
                }
                if "val_loss" in row:
                    fields["val_loss"] = round(row["val_loss"], 6)
                ctx.log_line(
                    f"epoch {self.epoch_offset + epoch}/{self.epoch_offset + cfg.epochs} done",
                    "info",
                    fields={k: v for k, v in fields.items() if math.isfinite(float(v))},
                )
            if (
                sink is not None
                and self.checkpoint_every
                and (epoch % self.checkpoint_every == 0)
                and not stopped
                and epoch < cfg.epochs
            ):
                self._save(sink, net, opt, state)
        # the kept weights: the best evaluation's with early stopping (restore_best), else the
        # last epoch's; best_epoch is the best evaluation either way (the last without val)
        kept = state.epoch
        if es is not None and es.restore_best and state.best_weights is not None:
            set_tensors(net, state.best_weights)
            kept = state.best_epoch
        best_epoch = state.best_epoch if (has_val and state.best_epoch) else state.epoch
        misclassified = None
        if has_val and isinstance(task, ClassificationTask) and hasattr(val, "ids"):
            ev = self._evaluate(eval_step, net, val, bs)
            misclassified = self._misclassified(ev, val, kept)
        history = self._history(state, monitor, best_epoch)
        return TrainResult(
            network=net,
            history=history,
            best_epoch=self.epoch_offset + best_epoch,
            epochs_run=state.epoch,
            stopped_early=state.stopped_early,
            steps=state.step,
            misclassified=misclassified,
        )

    def _targets(self, targets: np.ndarray | None, n: int) -> np.ndarray:
        if isinstance(self.task, ClassificationTask):
            if targets is None:
                return np.full(n, -1, dtype=np.int32)
            return np.asarray(targets, dtype=np.int32)
        if targets is None:
            return np.full(n, np.nan, dtype=np.float32)
        return np.asarray(targets, dtype=np.float32)

    # ------------------------------------------------------------------------- evaluation
    def _evaluate(self, eval_step: Any, net: Network, loader: Any, bs: int) -> _Eval:
        task = self.task
        ls_total, ms_total, n_total = 0.0, 0.0, 0.0
        logits_all: list[np.ndarray] = []
        codes_all: list[np.ndarray] = []
        sample: list[np.ndarray] = []
        for batch in loader.batches(bs, shuffle=False, drop_last=False):
            self.ctx.check_cancelled()
            raw, targets, _ = _unpack(batch)
            m = raw.shape[0]
            y = self._targets(targets, m)
            if isinstance(task, ClassificationTask):
                w = np.ones(m, dtype=np.float32)
            else:
                w = task.weights(y)
            x = self.preprocess(_pad(raw, bs))
            ls, ms, logits = eval_step(
                net, jnp.asarray(x), jnp.asarray(_pad(y, bs)), jnp.asarray(_pad(w, bs))
            )
            ls_total += float(ls)
            ms_total += float(ms)
            n_total += float(w.sum())
            logits_all.append(np.asarray(logits)[:m])
            codes_all.append(y)
            if raw.ndim == 4 and sum(len(s) for s in sample) < MAX_GRID_IMAGES:
                sample.append(raw[: MAX_GRID_IMAGES - sum(len(s) for s in sample)])
        values = task.metric_value(ls_total, ms_total, n_total)
        if not isinstance(task, ClassificationTask):
            return _Eval(values["loss"], values[task.metric], values, None, None, None, None)
        logits = np.concatenate(logits_all) if logits_all else np.zeros((0, task.n_out))
        codes = np.concatenate(codes_all) if codes_all else np.zeros(0, dtype=np.int32)
        probs = _softmax(logits)
        confusion = None
        k = task.n_out
        if k <= MAX_CONFUSION_CLASSES:
            known = codes >= 0
            confusion = np.zeros((k, k), dtype=np.int64)
            np.add.at(confusion, (codes[known], probs[known].argmax(axis=1)), 1)
        return _Eval(
            values["loss"],
            values[task.metric],
            values,
            probs,
            codes,
            confusion,
            np.concatenate(sample) if sample else None,
        )

    def _log_media(self, ev: _Eval, val: Any, step: int) -> None:
        task = self.task
        if not isinstance(task, ClassificationTask) or ev.probs is None or ev.codes is None:
            return
        classes = list(task.classes)
        try:
            if ev.sample is not None and len(ev.sample):
                n = len(ev.sample)
                pred = [classes[int(j)] for j in ev.probs[:n].argmax(axis=1)]
                true = [classes[int(c)] if c >= 0 else "?" for c in ev.codes[:n]]
                grid = prediction_grid(ev.sample, true, pred)
                right = sum(p == t for p, t in zip(pred, true, strict=True))
                self.ctx.log_image(
                    "val_predictions",
                    grid,
                    step=step,
                    labels={"split": "val"},
                    caption=(f"Predicted / true class of {n} validation images ({right} right)")[
                        :MAX_MEDIA_CAPTION_CHARS
                    ],
                )
            if ev.confusion is not None:
                self.ctx.log_image(
                    "confusion",
                    confusion_image(ev.confusion),
                    step=step,
                    labels={"split": "val"},
                    caption=(
                        "Validation confusion matrix (rows: true class, columns: predicted "
                        f"class; dark: more): {', '.join(classes)}"
                    )[:MAX_MEDIA_CAPTION_CHARS],
                )
        except ValueError as exc:  # over a media limit: media never fail a fit
            self.ctx.log_line(f"media not logged ({exc})", "warning")

    def _misclassified(self, ev: _Eval, val: Any, epoch: int) -> xr.Dataset | None:
        task = self.task
        if ev.probs is None or ev.codes is None or not isinstance(task, ClassificationTask):
            return None
        classes = list(task.classes)
        pred = ev.probs.argmax(axis=1)
        wrong = np.flatnonzero(pred != ev.codes)
        n_val = int(ev.codes.size)
        order = wrong[np.argsort(-ev.probs[wrong, pred[wrong]], kind="stable")]
        keep = np.sort(order[:MAX_MISCLASSIFIED])
        thumbs: list[np.ndarray] = []
        if keep.size:
            sub = val.subset(keep)
            for batch in sub.batches(max(1, keep.size), shuffle=False, drop_last=False):
                for px in np.asarray(batch.pixels):
                    thumbs.append(thumbnail(px, THUMB_SIDE))
        channels = int(getattr(val, "channels", thumbs[0].shape[-1] if thumbs else 1))
        th = max((t.shape[0] for t in thumbs), default=1)
        tw = max((t.shape[1] for t in thumbs), default=1)
        arr = np.zeros((keep.size, th, tw, channels), dtype=np.uint8)
        for i, t in enumerate(thumbs):
            arr[i, : t.shape[0], : t.shape[1], :] = t.reshape(t.shape[0], t.shape[1], -1)[
                ..., :channels
            ]
        ids = np.asarray(val.ids, dtype=object)
        true_codes = ev.codes[keep]
        return xr.Dataset(
            {
                "thumb": (("example", "ty", "tx", "channel"), arr),
                "prob": (("example",), ev.probs[keep, pred[keep]].astype(np.float64)),
                "true_prob": (
                    ("example",),
                    np.where(
                        true_codes >= 0,
                        ev.probs[keep, np.clip(true_codes, 0, None)],
                        np.nan,
                    ).astype(np.float64),
                ),
            },
            coords={
                "example": np.arange(keep.size, dtype=np.int64),
                "image_id": ("example", [str(v) for v in ids[keep]]),
                "true_label": (
                    "example",
                    [classes[int(c)] if c >= 0 else "" for c in true_codes],
                ),
                "pred_label": ("example", [classes[int(j)] for j in pred[keep]]),
            },
            attrs={
                "epoch": int(self.epoch_offset + epoch),
                "n_wrong": int(wrong.size),
                "n_val": n_val,
                "color": "rgb" if channels >= 3 else "gray",
            },
        )

    # ---------------------------------------------------------------------------- history
    def _history(self, state: _LoopState, monitor: str, best_epoch: int) -> xr.Dataset:
        task = self.task
        rows = state.history
        metric = task.metric
        confusion = state.confusion if isinstance(task, ClassificationTask) else {}
        if confusion:
            rows = [r for r in rows if int(r["epoch"]) in confusion]
            limit = MAX_CONFUSION_EPOCHS
        else:
            limit = MAX_HISTORY_EPOCHS
        total = len(rows)
        if total > limit:
            epochs = [int(r["epoch"]) for r in rows]
            must = {0, total - 1}
            if best_epoch in epochs:
                must.add(epochs.index(best_epoch))
            spaced = np.unique(np.linspace(0, total - 1, limit - len(must)).round().astype(int))
            pick = sorted(must | {int(i) for i in spaced})[:limit]
            if len(pick) < limit:
                rest = [i for i in range(total) if i not in pick]
                pick = sorted(pick + rest[: limit - len(pick)])
            rows = [rows[i] for i in pick]
        epochs_arr = np.array([self.epoch_offset + int(r["epoch"]) for r in rows], dtype=np.int64)

        def col(name: str) -> np.ndarray:
            return np.array(
                [
                    float(r.get(name, math.nan)) if r.get(name) is not None else math.nan
                    for r in rows
                ],
                dtype=np.float64,
            )

        data: dict[str, Any] = {
            "train_loss": (("epoch",), col("train_loss")),
            "val_loss": (("epoch",), col("val_loss")),
            f"train_{metric}": (("epoch",), col(f"train_{metric}")),
            f"val_{metric}": (("epoch",), col(f"val_{metric}")),
            "lr": (("epoch",), col("lr")),
        }
        coords: dict[str, Any] = {"epoch": epochs_arr}
        if confusion and isinstance(task, ClassificationTask):
            mats = np.array([confusion[int(r["epoch"])] for r in rows], dtype=np.int64)
            data["confusion"] = (("epoch", "true_class", "pred_class"), mats)
            coords["true_class"] = list(task.classes)
            coords["pred_class"] = list(task.classes)
        attrs: dict[str, Any] = {
            "task": task.kind,
            "monitor": monitor,
            "best_epoch": int(self.epoch_offset + best_epoch),
            "stopped_early": int(state.stopped_early),
            "epochs_run": int(state.epoch),
            "steps": int(state.step),
            "mapi_training": "1",
        }
        if len(rows) < state.epoch:  # one row per epoch run (some thinned while training)
            attrs["thinned_from"] = int(state.epoch)
        return xr.Dataset(data, coords=coords, attrs=attrs)

    # ------------------------------------------------------------------------- checkpoints
    def _save(
        self, sink: FileCheckpointSink, net: Network, opt: nnx.Optimizer[Any], state: _LoopState
    ) -> None:
        from mapi_sdk.safetensors import write_safetensors

        assert self.checkpoint_key is not None
        tensors = dict(network_tensors(net))
        if state.best_weights is not None:
            tensors.update({_BEST + k: v for k, v in state.best_weights.items()})
        meta = json_safe(
            {
                "format": "mapi_jax.checkpoint/v1",
                "epoch": state.epoch,
                "step": state.step,
                "best_value": state.best_value,
                "best_epoch": state.best_epoch,
                "bad": state.bad,
                "media": state.media,
                "history": state.history,
                "confusion": {str(k): v for k, v in state.confusion.items()},
                "seed": self.seed,
            }
        )
        sink.save_files(
            self.checkpoint_key,
            {"weights": write_safetensors(tensors), "opt": optimizer_bytes(opt)},
            meta,
            state.epoch,
        )

    def _resume(self, found: Any, opt: nnx.Optimizer[Any]) -> _LoopState:
        from mapi_sdk.safetensors import read_safetensors

        meta = found.meta
        if meta.get("format") != "mapi_jax.checkpoint/v1" or int(meta.get("seed", -1)) != self.seed:
            raise MapiError(
                "the checkpoint does not belong to this fit", code=ErrorCode.INTEGRITY_FAILURE
            )
        tensors, _ = read_safetensors(found.files["weights"])
        current = {k: v for k, v in tensors.items() if not k.startswith(_BEST)}
        best = {k[len(_BEST) :]: v for k, v in tensors.items() if k.startswith(_BEST)}
        set_tensors(self.network, current)
        restore_optimizer(opt, found.files["opt"])
        state = _LoopState()
        state.epoch = int(meta["epoch"])
        state.step = int(meta["step"])
        state.best_value = None if meta.get("best_value") is None else float(meta["best_value"])
        state.best_epoch = int(meta.get("best_epoch") or 0)
        state.bad = int(meta.get("bad") or 0)
        state.media = int(meta.get("media") or 0)
        state.history = [
            {k: (math.nan if v is None else v) for k, v in r.items()} for r in meta["history"]
        ]
        state.confusion = {int(k): v for k, v in (meta.get("confusion") or {}).items()}
        state.best_weights = best or None
        return state


@dataclass(slots=True)
class _LoopState:
    epoch: int = 0
    step: int = 0
    best_value: float | None = None
    best_epoch: int = 0
    bad: int = 0
    media: int = 0
    stopped_early: bool = False
    best_weights: dict[str, np.ndarray] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    confusion: dict[int, Any] = field(default_factory=dict)


def _halved(epochs: list[int], keep: set[int]) -> set[int]:
    """Every other epoch of the sorted ``epochs``, plus ``keep``."""
    return {e for i, e in enumerate(epochs) if i % 2 == 0 or e in keep}


def _compact(state: _LoopState) -> None:
    """Bound the per-epoch state (it goes into every checkpoint meta): halve the confusion
    matrices above :data:`_KEEP_CONFUSION` and the rows above :data:`_KEEP_HISTORY`. A
    function of the state only, so a resumed fit compacts as the original did."""
    if len(state.confusion) > _KEEP_CONFUSION:
        epochs = sorted(state.confusion)
        kept = _halved(epochs, {epochs[0], epochs[-1], state.best_epoch})
        state.confusion = {e: state.confusion[e] for e in epochs if e in kept}
    if len(state.history) > _KEEP_HISTORY:
        epochs = [int(r["epoch"]) for r in state.history]
        kept = _halved(epochs, {epochs[0], epochs[-1], state.best_epoch, *state.confusion})
        state.history = [r for r in state.history if int(r["epoch"]) in kept]


def _softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)
