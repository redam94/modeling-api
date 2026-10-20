"""The training loop: learning, tracking contract, media, cancellation, early stopping,
determinism and the ``training`` group layout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_jax.config import EarlyStopping, TrainConfig
from mapi_jax.models.base import Normalize
from mapi_jax.nets import ArchSpec, build_network, network_tensors
from mapi_jax.tabular import TabularLoader
from mapi_jax.tasks import ClassificationTask, RegressionTask
from mapi_jax.trainer import (
    MAX_CONFUSION_EPOCHS,
    Trainer,
    TrainResult,
)
from mapi_sdk.collection import Collection, ImageLoader
from mapi_sdk.errors import RunCancelled
from mapi_sdk.models import FitContext
from mapi_sdk.safetensors import write_safetensors
from mapi_sdk.steps import CancelToken
from mapi_sdk.tracking import MetricRecorder

ARCH = ArchSpec(kind="resnet", channels=1, widths=[4, 8], blocks=[1, 1], groups=2, dropout=0.1)
UNIT = Normalize((0.0,), (1.0,), "unit")


def _loaders(collection: Collection) -> tuple[ImageLoader, ImageLoader, list[str]]:
    loader = collection.loader()
    classes = loader.classes
    loader = loader.with_classes(classes)
    train, val = loader.split(0.3, seed=0, stratify=True)
    return train, val, classes


@dataclass
class Run:
    result: TrainResult
    recorder: MetricRecorder
    classes: list[str]


def _train(
    collection: Collection,
    cfg: TrainConfig,
    *,
    seed: int = 0,
    recorder: MetricRecorder | None = None,
    cancel: CancelToken | None = None,
    on_progress: Any = None,
) -> Run:
    train, val, classes = _loaders(collection)
    rec = recorder or MetricRecorder()
    ctx = FitContext(seed=seed, tracker=rec, cancel=cancel, on_progress=on_progress)
    net = build_network(ARCH, len(classes), seed=seed)
    result = Trainer(
        net,
        ClassificationTask(classes),
        cfg,
        ctx,
        seed=seed,
        preprocess=UNIT,
        arch_params=ARCH.model_dump(mode="json"),
    ).fit(train, val)
    return Run(result, rec, classes)


CFG = TrainConfig(epochs=4, batch_size=6, learning_rate=0.02, early_stopping=None)


@pytest.fixture(scope="module")
def run(collection: Collection) -> Run:
    return _train(collection, CFG)


def test_loss_goes_down(run: Run) -> None:
    h = run.result.history
    assert h["train_loss"].values[-1] < h["train_loss"].values[0]
    assert run.result.epochs_run == 4 and not run.result.stopped_early
    assert run.result.steps == 4 * (12 // 6)


def test_tracking_contract(run: Run) -> None:
    series = run.recorder.series()
    keys = set(series)
    for key in ("loss", "accuracy"):
        assert (key, "split=train") in keys and (key, "split=val") in keys
    for key in ("lr", "grad_norm", "epoch"):
        assert (key, "") in keys
    # wall-clock throughput is a log line field, not a metric (seed-deterministic history)
    assert not any(k[0] == "images_per_second" for k in keys)
    epochs = [f for _, msg, f in run.recorder.lines if msg.startswith("epoch ")]
    assert len(epochs) == 4 and all(f["images_per_second"] > 0 for f in epochs)
    steps_per_epoch = run.result.steps // 4
    val_steps = [s for s, _ in series[("loss", "split=val")]]
    assert val_steps == [steps_per_epoch * e for e in range(1, 5)]
    assert [v for _, v in series[("epoch", "")]] == [1.0, 2.0, 3.0, 4.0]
    params = run.recorder.params
    assert params["dl.arch.kind"] == "resnet" and params["dl.train.epochs"] == 4
    assert params["dl.backend"] == "cpu" and params["dl.parameters"] > 0
    assert "dl.device" in params


def test_media_val_predictions_and_confusion(run: Run) -> None:
    media = run.recorder.media
    keys = {m.key for m in media}
    assert keys == {"val_predictions", "confusion"}
    assert all(m.content_type == "image/png" and m.labels == {"split": "val"} for m in media)
    assert len([m for m in media if m.key == "confusion"]) == 4  # every evaluation (<= 20)


def test_training_group_layout(run: Run) -> None:
    h = run.result.history
    assert list(h["epoch"].values) == [1, 2, 3, 4]
    for v in ("train_loss", "val_loss", "train_accuracy", "val_accuracy", "lr"):
        assert h[v].dims == ("epoch",) and h[v].dtype == np.float64
    assert h["confusion"].dims == ("epoch", "true_class", "pred_class")
    assert list(h["true_class"].values) == run.classes
    assert int(h["confusion"].isel(epoch=0).sum()) == 6  # the validation images
    assert h.attrs["task"] == "classification" and h.attrs["mapi_training"] == "1"
    assert h.attrs["monitor"] == "val_loss" and h.attrs["epochs_run"] == 4
    mis = run.result.misclassified
    assert mis is not None
    assert mis["thumb"].dims == ("example", "ty", "tx", "channel")
    assert mis["thumb"].dtype == np.uint8 and mis.sizes["ty"] <= 48
    assert mis.attrs["n_val"] == 6 and mis.attrs["n_wrong"] == mis.sizes["example"]
    assert mis.attrs["color"] == "gray"
    assert set(mis.coords) >= {"image_id", "true_label", "pred_label", "example"}
    assert (mis["true_label"].values != mis["pred_label"].values).all()


def test_same_seed_same_bytes_other_seed_differs(collection: Collection, run: Run) -> None:
    again = _train(collection, CFG)
    other = _train(collection, CFG, seed=1)

    def blob(r: Run) -> bytes:
        return write_safetensors(network_tensors(r.result.network))

    assert blob(again) == blob(run)
    assert blob(other) != blob(run)


def test_cancellation_stops_the_fit(collection: Collection) -> None:
    token = CancelToken()

    def progress(fraction: float, message: str) -> None:
        token.cancel("test")

    with pytest.raises(RunCancelled):
        _train(collection, CFG, cancel=token, on_progress=progress)


def test_early_stopping_restores_the_best_weights(collection: Collection) -> None:
    # an impossible min_delta: the first evaluation stays the best and patience 1 stops at 2
    stop = TrainConfig(
        epochs=5,
        batch_size=6,
        learning_rate=0.02,
        schedule="constant",
        early_stopping=EarlyStopping(patience=1, min_delta=1e6),
    )
    r = _train(collection, stop)
    assert r.result.stopped_early and r.result.epochs_run == 2
    assert r.result.best_epoch == 1 and r.result.history.attrs["stopped_early"] == 1
    one = _train(collection, stop.model_copy(update={"epochs": 1, "early_stopping": None}))
    a, b = network_tensors(r.result.network), network_tensors(one.result.network)
    assert all(np.array_equal(a[k], b[k]) for k in a)  # the epoch-1 weights came back


def test_history_keeps_first_best_last_within_the_caps() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(24, 3)).astype(np.float32)
    y = (x[:, 0] > 0).astype(np.int32)
    loader = TabularLoader(x, y, strata=y)
    train, val = loader.split(0.25, seed=0)
    cfg = TrainConfig(epochs=60, batch_size=18, early_stopping=None, learning_rate=0.05)
    ctx = FitContext(seed=0)
    net = build_network(ArchSpec(kind="mlp", in_features=3, hidden=[4]), 2, seed=0)
    res = Trainer(
        net, ClassificationTask(["a", "b"]), cfg, ctx, seed=0, preprocess=lambda r: r
    ).fit(train, val)
    h = res.history
    assert h.sizes["epoch"] == MAX_CONFUSION_EPOCHS and h.attrs["thinned_from"] == 60
    epochs = list(h["epoch"].values)
    assert epochs[0] == 1 and epochs[-1] == 60 and res.best_epoch in epochs
    assert h["confusion"].sizes["epoch"] == MAX_CONFUSION_EPOCHS


def test_regression_metrics_in_target_units() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(40, 2)).astype(np.float32)
    y = (2 * x[:, 0] + rng.normal(scale=0.1, size=40)).astype(np.float32)
    loader = TabularLoader(x, y)
    train, val = loader.split(0.25, seed=0, stratify=False)
    rec = MetricRecorder()
    net = build_network(ArchSpec(kind="mlp", in_features=2, hidden=[8]), 1, seed=0)
    res = Trainer(
        net,
        RegressionTask(0.0, 10.0),
        TrainConfig(epochs=3, batch_size=10, early_stopping=EarlyStopping(monitor="val_accuracy")),
        FitContext(seed=0, tracker=rec),
        seed=0,
        preprocess=lambda r: r,
    ).fit(train, val)
    h = res.history
    assert {"train_rmse", "val_rmse"} <= set(h.data_vars) and "confusion" not in h
    assert h.attrs["task"] == "regression"
    assert h.attrs["monitor"] == "val_loss"  # val_accuracy is not a regression metric
    assert ("rmse", "split=val") in rec.series() and ("mae", "split=train") in rec.series()
    assert res.misclassified is None
    assert isinstance(h, xr.Dataset)


def test_lr_scale_by_parameter_name() -> None:
    import jax.numpy as jnp
    import optax
    from flax import nnx
    from mapi_jax.trainer import _scale_by_path

    seen: list[str] = []

    def scale(name: str) -> float:
        seen.append(name)
        return 0.0 if name.startswith("backbone") else 1.0

    net = build_network(ArchSpec(kind="mlp", in_features=3, hidden=[4]), 2, seed=0)
    opt = nnx.Optimizer(net, optax.chain(optax.sgd(0.1), _scale_by_path(scale)), wrt=nnx.Param)
    before = network_tensors(net)
    grads = nnx.grad(lambda n: n(jnp.ones((2, 3))).sum())(net)
    opt.update(net, grads)
    after = network_tensors(net)
    assert set(seen) == set(before)
    assert np.array_equal(after["backbone.layers.0.kernel"], before["backbone.layers.0.kernel"])
    assert not np.array_equal(after["head.kernel"], before["head.kernel"])


def test_the_loop_state_of_a_long_fit_stays_bounded() -> None:
    """Every checkpoint meta holds the loop state: 5000 evaluated epochs of 50 classes must
    stay far below the runner's 16 MiB meta cap (else a resumed fit starts again)."""
    import json

    from mapi_jax import trainer as trainer_mod

    state = trainer_mod._LoopState()
    k = 50
    for epoch in range(1, 5001):
        state.epoch = epoch
        if epoch == 1234:
            state.best_epoch = epoch
        state.confusion[epoch] = (np.eye(k, dtype=np.int64) * 100).tolist()
        state.history.append({"epoch": epoch, "train_loss": 0.1, "val_loss": 0.2})
        trainer_mod._compact(state)
    assert len(state.confusion) <= trainer_mod._KEEP_CONFUSION
    assert len(state.history) <= trainer_mod._KEEP_HISTORY
    for kept in (state.confusion, [int(r["epoch"]) for r in state.history]):
        assert {1, 1234, 5000} <= set(kept)
    meta = {"history": state.history, "confusion": {str(e): m for e, m in state.confusion.items()}}
    assert len(json.dumps(meta)) < 4 * 1024**2


def test_a_compacted_history_still_ends_thinned_and_resumes_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mapi_jax import trainer as trainer_mod
    from mapi_sdk.checkpoints import MemoryFileCheckpointSink

    monkeypatch.setattr(trainer_mod, "_KEEP_CONFUSION", 12)
    monkeypatch.setattr(trainer_mod, "_KEEP_HISTORY", 16)
    rng = np.random.default_rng(0)
    x = rng.normal(size=(24, 3)).astype(np.float32)
    y = (x[:, 0] > 0).astype(np.int32)
    train, val = TabularLoader(x, y, strata=y).split(0.25, seed=0)
    cfg = TrainConfig(epochs=40, batch_size=18, early_stopping=None, learning_rate=0.05)

    def fit(sink: MemoryFileCheckpointSink, stop_after: int | None = None) -> TrainResult:
        net = build_network(ArchSpec(kind="mlp", in_features=3, hidden=[4]), 2, seed=0)
        token = CancelToken()

        def progress(fraction: float, message: str) -> None:
            if stop_after is not None and sink.saves >= stop_after:
                token.cancel("interrupted")

        ctx = FitContext(seed=0, checkpoint_sink=sink, cancel=token, on_progress=progress)
        return Trainer(
            net,
            ClassificationTask(["a", "b"]),
            cfg,
            ctx,
            seed=0,
            preprocess=lambda r: r,
            checkpoint_key="0123456789abcdef0123456789abcdef",
            checkpoint_every=1,
        ).fit(train, val)

    full = fit(MemoryFileCheckpointSink())
    h = full.history
    assert h.attrs["thinned_from"] == 40 and h.sizes["epoch"] <= 12
    epochs = list(h["epoch"].values)
    assert epochs[0] == 1 and epochs[-1] == 40 and full.best_epoch in epochs
    # an interrupted fit (checkpoint after epoch 25) resumes to the same history
    sink = MemoryFileCheckpointSink()
    with pytest.raises(RunCancelled):
        fit(sink, stop_after=25)
    meta = sink.checkpoints["0123456789abcdef0123456789abcdef"].meta
    assert meta["epoch"] == 25
    assert len(meta["history"]) <= 16 and len(meta["confusion"]) <= 12  # type: ignore[arg-type]
    resumed = fit(sink)
    xr.testing.assert_identical(resumed.history, h)
