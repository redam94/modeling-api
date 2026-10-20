"""A fit on an image collection (v0.15) end to end in a real WorkerContext: a collection
version made by the collection ingest (manifest, ``collection.json`` and the packs as
signed GETs of the GCS shim) -> ``run_fit`` with a tiny numpy ``fit_batches`` model
(nearest class mean) from a test registry -> bundle, evaluation metrics, the DL plot defaults
(``ml.training_curves``, ``ml.confusion_epochs``, ``ml.misclassified``), views on a decoded
sample and a file checkpoint whose names the channel accepts. Then ``run_score`` and
``run_evaluate`` read the same collection lazily."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
import xarray as xr
from collection_fixtures import Worker, client_manifest, images_and_labels, parts_of, put_inputs
from mapi_runner.channel import CheckpointMessage
from mapi_runner.jobs import IngestJob, ScoreJob
from mapi_runner.worker import score as score_mod
from mapi_runner.worker.data import CachedCollection, is_collection, load_collection, load_dataset
from mapi_runner.worker.fit import (
    CONFUSION_EPOCHS,
    MISCLASSIFIED_PLOT,
    TRAINING_CURVES,
    fit_plot_refs,
    run_fit,
)
from mapi_runner.worker.ingest import run_ingest
from mapi_runner.worker.score import run_score
from mapi_sdk.checkpoints import FileCheckpointSink, checkpoint_names
from mapi_sdk.collection import ArrayImageLoader, ImageLoader
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
    requested_positions,
)
from mapi_sdk.registry import Registry
from mapi_sdk.safetensors import write_safetensors
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.collection import (
    COLLECTION_INFO,
    COLLECTION_MANIFEST,
    CollectionInfo,
    CollectionIngest,
    CollectionOptions,
    info_input,
    pack_input,
    part_input,
)
from mapi_spec.executor import RunKind
from mapi_spec.loader import load_model_spec
from mapi_spec.spec import FitConfig, ScoreRequest
from mapi_spec.storage import SignedGet
from shimgcs import ShimGcs

pytestmark = pytest.mark.unit

SIZE = (8, 8)
TEMPLATE: dict[str, Any] = {
    "template": {
        "dims": {
            "image": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            "y": {"kind": "integer", "policy": "fixed"},
            "x": {"kind": "integer", "policy": "fixed"},
            "channel": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "pixels": {
                "dims": ["image", "y", "x", "channel"],
                "dtype": "uint8",
                "role": "covariate",
            },
            "label": {"dims": ["image"], "dtype": "str", "role": "target", "missing": "allow"},
        },
    }
}


def _features(loader: ImageLoader) -> tuple[np.ndarray, np.ndarray]:
    loader = loader.with_size(SIZE).with_mode("gray")
    feats = np.zeros((loader.n, SIZE[0] * SIZE[1]))
    codes = np.full(loader.n, -1, dtype=np.int32)
    for batch in loader.batches(4, shuffle=True, seed=1, prefetch=1):
        feats[batch.index] = batch.pixels.reshape(len(batch.index), -1) / 255.0
        if batch.codes is not None:
            codes[batch.index] = batch.codes
    return feats, codes


def _training(classes: list[str]) -> xr.Dataset:
    """A ``training`` group shaped like the mapi_jax trainer's (2 epochs)."""
    k = len(classes)
    epochs = np.array([1, 2])
    confusion = np.stack([np.eye(k, dtype=np.int64) * 2, np.eye(k, dtype=np.int64) * 3])
    return xr.Dataset(
        {
            "train_loss": ("epoch", [1.0, 0.5]),
            "val_loss": ("epoch", [1.1, 0.7]),
            "train_accuracy": ("epoch", [0.5, 0.9]),
            "val_accuracy": ("epoch", [0.4, 0.8]),
            "lr": ("epoch", [1e-3, 5e-4]),
            "confusion": (("epoch", "true_class", "pred_class"), confusion),
        },
        coords={"epoch": epochs, "true_class": classes, "pred_class": classes},
        attrs={
            "task": "classification",
            "monitor": "val_loss",
            "best_epoch": 2,
            "stopped_early": 0,
            "epochs_run": 2,
            "steps": 6,
            "mapi_training": "1",
        },
    )


def _misclassified(classes: list[str]) -> xr.Dataset:
    return xr.Dataset(
        {
            "thumb": (("example", "ty", "tx", "channel"), np.zeros((1, 8, 8, 1), np.uint8)),
            "prob": ("example", [0.7]),
            "true_prob": ("example", [0.2]),
        },
        coords={
            "example": [0],
            "image_id": ("example", ["x"]),
            "true_label": ("example", [classes[0]]),
            "pred_label": ("example", [classes[-1]]),
        },
        attrs={"epoch": 2, "n_wrong": 1, "n_val": 3, "color": "gray"},
    )


class BatchMeans(CustomModel[ModelSettings]):
    """Nearest class mean over 8 x 8 gray pixels, trained from a loader; saves one file
    checkpoint when the run gives a file sink."""

    id = "test.batch_means"
    version = "1.0.0"
    capabilities = Capabilities(
        inference="custom",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        input="collection",
        checkpointable=True,
    )
    schema_template = TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {"dims": ["class"], "dims_of": "label", "units": "1", "description": "p(class)"}
    }
    pad_extend = False
    fitted_rows: ClassVar[list[int]] = []
    scored_rows: ClassVar[list[int]] = []

    def _fit(self, loader: ImageLoader, ctx: FitContext) -> FitOutput:
        classes = loader.classes
        feats, codes = _features(loader)
        means = np.stack([feats[codes == k].mean(axis=0) for k in range(len(classes))])
        type(self).fitted_rows.append(loader.n)
        sink = ctx.checkpoint_sink
        if isinstance(sink, FileCheckpointSink) and ctx.fit_key is not None:
            weights = write_safetensors({"means": means.astype(np.float32)})
            sink.save_files(ctx.fit_key, {"weights": weights}, {"epoch": 1}, 1)
        return FitOutput(
            groups={
                "means": xr.Dataset({"mean": (("class", "feature"), means)}),
                "training": _training(classes),
                "misclassified": _misclassified(classes),
            },
            learned={"classes": classes},
        )

    def fit_batches(
        self, loader: ImageLoader, s: ModelSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        return self._fit(loader, ctx)

    def fit(self, ds: xr.Dataset, s: ModelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        return self._fit(ArrayImageLoader(ds), ctx)

    def _score(self, fitted: Fitted, loader: ImageLoader) -> xr.DataTree:
        type(self).scored_rows.append(loader.n)
        means = fitted.groups["means"]["mean"].values
        feats, _ = _features(loader)
        d = ((feats[:, None, :] - means[None]) ** 2).sum(-1)
        p = np.exp(-(d - d.min(axis=1, keepdims=True)))
        p /= p.sum(axis=1, keepdims=True)
        y_hat = xr.DataArray(
            np.stack([p, p])[None],
            dims=("chain", "draw", "image", "class"),
            coords={
                "image": np.asarray(loader.ids).astype(str),
                "class": fitted.learned["classes"],
            },
            attrs={"meaning": "class_probability"},
        )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})

    def score_batches(
        self, fitted: Fitted, loader: ImageLoader, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        return self._score(fitted, loader)

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        rows = ds.isel(image=requested_positions(ds, "image"))
        pred = self._score(fitted, ArrayImageLoader(rows))[PREDICTIONS].to_dataset()
        return xr.DataTree.from_dict({PREDICTIONS: pred.reindex(image=ds["image"].values)})


_REGISTRY: Registry | None = None


def registry() -> Registry:
    """The worker's registry: the installed plots plus the test model."""
    global _REGISTRY
    if _REGISTRY is None:
        reg = Registry()
        reg.load_entry_points()
        reg.register_model(BatchMeans, replace=True)
        _REGISTRY = reg
    return _REGISTRY


SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: shapes, project: default}
data:
  dataset: dataset://default/shapes@1
  split: {kind: column, dim: image, column: split, holdout: [test]}
model:
  use: test.batch_means@^1
  fit: {seed: 3, mode: evaluate_then_refit}
evaluate: {metrics: [accuracy, log_score]}
"""


@pytest.fixture
def version(gcs: ShimGcs, tmp_path: Path) -> dict[str, SignedGet]:
    """A collection version's fit inputs: ``dataset`` (manifest), ``dataset.info``, packs."""
    images, labels = images_and_labels(n_per_class=4, size=8)
    parts = parts_of(images, part_bytes=2500)
    objects = {"manifest": client_manifest(images, labels)}
    objects.update({part_input(i): p for i, p in enumerate(parts)})
    inputs = put_inputs(gcs, "ingest", objects)
    job = IngestJob(  # type: ignore[call-arg]
        collection=CollectionIngest(
            options=CollectionOptions(label_column="label", split_column="split"),
            parts=len(parts),
        )
    )
    w = Worker(tmp_path / "ingest", inputs)
    try:
        run_ingest(w.ctx, job)
    finally:
        w.finish()
    info = CollectionInfo.model_validate_json(w.output(COLLECTION_INFO))
    out = put_inputs(
        gcs,
        "version",
        {"dataset": w.output(COLLECTION_MANIFEST), info_input(): w.output(COLLECTION_INFO)},
    )
    for i, p in enumerate(info.packs):
        out[pack_input(i)] = inputs[str(p.input)]
    return out


def test_run_fit_on_a_collection(version: dict[str, SignedGet], tmp_path: Path) -> None:
    BatchMeans.fitted_rows.clear()
    BatchMeans.scored_rows.clear()
    spec = load_model_spec(SPEC)
    w = Worker(
        tmp_path / "fit",
        version,
        kind=RunKind.FIT,
        spec=spec.model_dump(mode="json"),
        checkpointing=True,
        registry=registry(),
    )
    try:
        assert is_collection(w.ctx)
        bundle = run_fit(w.ctx, spec)
    finally:
        msgs = w.finish()
    # the evaluation fit trained on the train rows, the refit on all 12
    assert BatchMeans.fitted_rows == [9, 12]
    assert bundle.metrics.values["accuracy"] is not None
    assert "log_score" in bundle.metrics.values
    # the plot payload: the held-out images, scored through the collection
    assert 3 in BatchMeans.scored_rows
    refs = fit_plot_refs(spec, bundle)
    assert refs == [TRAINING_CURVES, CONFUSION_EPOCHS, MISCLASSIFIED_PLOT]
    assert [p["id"] for p in w.json("plots.json")] == refs  # all three drawn
    assert (w.ctx.outputs_dir / "bundle.zarr.zip").exists()
    assert (w.ctx.outputs_dir / "scorecard.json").exists()
    # the checkpoint of each fit went to the supervisor under names the channel accepts
    ckpts = [m for m in msgs if isinstance(m, CheckpointMessage)]
    assert len(ckpts) == 2
    names = checkpoint_names(ckpts[0].files[0].split("-")[1].split(".")[0])
    assert ckpts[0].files == [names["weights"], names["meta"]]
    assert ckpts[0].segment == 1 and ckpts[0].draws_done is None
    # the collection is read lazily: only the manifest and the info were downloaded
    assert sorted(p.name for p in w.ctx.inputs_dir.iterdir()) == ["dataset", "dataset.info"]


def test_loaders_use_the_scratch_cache(version: dict[str, SignedGet], tmp_path: Path) -> None:
    w = Worker(tmp_path / "c", version, kind=RunKind.FIT)
    try:
        coll = load_collection(w.ctx)
        assert isinstance(coll, CachedCollection)
        coll.cache_dir, coll.cache_bytes = w.ctx.work_dir / "cache", 1 << 20
        loader = coll.loader()
        assert loader.to_xarray(4, SIZE, seed=0)["pixels"].shape == (4, 8, 8, 1)
        # explore/describe read a sample as the v0.14 image dataset
        sample = load_dataset(w.ctx)
        assert isinstance(sample, xr.Dataset) and sample["pixels"].dims[0] == "image"
    finally:
        w.finish()


def test_run_score_reads_the_collection(
    version: dict[str, SignedGet], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = load_model_spec(SPEC)
    fit = Worker(tmp_path / "f", version, kind=RunKind.FIT, registry=registry())
    try:
        bundle = run_fit(fit.ctx, spec)
    finally:
        fit.finish()
    BatchMeans.scored_rows.clear()
    request = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "model://default/shapes@latest",
            "mode": "predict",
        }
    )
    # the bundle is reused in-process (the score run would download it)
    monkeypatch.setattr(score_mod, "load_bundle", lambda _ctx: bundle)
    w = Worker(tmp_path / "s", version, kind=RunKind.SCORE, registry=registry())
    try:
        tree = run_score(w.ctx, ScoreJob(request=request))  # type: ignore[call-arg]
    finally:
        w.finish()
    assert BatchMeans.scored_rows == [12]  # every image, read lazily from the packs
    assert "summary" in tree.children


def test_sample_size_keeps_a_sample_under_the_cell_cap(
    version: dict[str, SignedGet], tmp_path: Path
) -> None:
    from mapi_runner.worker.data import SAMPLE_SIZE, sample_size
    from mapi_spec.ingest import IMAGE_FOLDER_MAX_CELLS

    w = Worker(tmp_path / "info", version, kind=RunKind.FIT)
    try:
        info = load_collection(w.ctx).info.model_copy(update={"mode": "rgb", "n_images": 9000})
    finally:
        w.finish()
    small = info.model_copy(update={"size": (32, 32)})
    assert sample_size(small, 500) == (32, 32)  # unchanged under the cap
    # 500 uniform 512 x 256 RGB photos would need 196M values: unchanged; 512 x 512: smaller
    assert sample_size(info.model_copy(update={"size": (512, 256)}), 500) == (512, 256)
    h, w = sample_size(info.model_copy(update={"size": (1024, 512)}), 500)
    assert h in (2 * w, 2 * w + 1)  # the same aspect
    assert 500 * h * w * 3 <= IMAGE_FOLDER_MAX_CELLS
    # 5000 samples of 224 x 224 RGB, as describe/explore read them
    h, w = sample_size(info.model_copy(update={"size": (224, 224)}), 5000)
    assert h == w and 5000 * h * w * 3 <= IMAGE_FOLDER_MAX_CELLS < 5000 * (h + 1) ** 2 * 3
    # mixed sizes: the default size
    assert sample_size(info.model_copy(update={"size": None}), 5000) == SAMPLE_SIZE


def test_a_mixed_size_collection_reads_as_a_sample(
    version: dict[str, SignedGet], gcs: ShimGcs, tmp_path: Path
) -> None:
    """explore/describe/priors/governance read a collection through ``load_dataset``: a
    collection whose images have no one size is resized, not refused."""
    from mapi_runner.worker.data import SAMPLE_SIZE

    w = Worker(tmp_path / "info", version, kind=RunKind.FIT)
    try:
        info = load_collection(w.ctx).info
    finally:
        w.finish()
    mixed = info.model_copy(update={"size": None}).model_dump_json().encode()
    inputs = {**version, **put_inputs(gcs, "mixed", {info_input(): mixed})}
    w = Worker(tmp_path / "mixed", inputs, kind=RunKind.FIT)
    try:
        sample = load_dataset(w.ctx)
    finally:
        w.finish()
    assert isinstance(sample, xr.Dataset)
    assert sample["pixels"].shape[1:3] == SAMPLE_SIZE


def test_a_fit_does_not_fail_when_its_view_sample_cannot_be_decoded(
    version: dict[str, SignedGet], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The views read a decoded sample; a fit that trains on batches does not need it, so a
    sample that cannot be decoded is a warning, not a failed fit."""
    from mapi_runner.channel import WarningMessage
    from mapi_runner.worker import fit as fit_mod
    from mapi_spec.errors import ErrorCode, MapiError

    def refuse(_collection: object) -> xr.Dataset:
        raise MapiError("the sample needs too many values", code=ErrorCode.PAYLOAD_TOO_LARGE)

    monkeypatch.setattr(fit_mod, "view_sample", refuse)
    spec = load_model_spec(SPEC)
    w = Worker(tmp_path / "fit", version, kind=RunKind.FIT, registry=registry())
    try:
        bundle = run_fit(w.ctx, spec)
    finally:
        msgs = w.finish()
    assert bundle.metrics.values["accuracy"] is not None
    codes = [m.payload.code for m in msgs if isinstance(m, WarningMessage)]
    assert "VIEW_SAMPLE_FAILED" in codes
