"""v0.15 harness: image collections through ``fit_spec``/``score_bundle``.

A tiny numpy model (nearest class mean over 8 x 8 gray pixels) declares
``input="collection"``: it trains from a loader (``fit_batches``) and scores a loader
(``score_batches``); a second model that reads datasets gets a seeded sample."""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.collection import ArrayImageLoader, Collection, ImageLoader
from mapi_sdk.harness import fit_spec, score_bundle
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
from mapi_sdk.testing.images import local_collection
from mapi_spec import load_score_request
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import load_model_spec
from mapi_spec.spec import FitConfig, ScoreRequest

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
OUTPUTS: dict[str, Any] = {
    "y_hat": {"dims": ["class"], "dims_of": "label", "units": "1", "description": "p(class)"}
}


def _features(loader: ImageLoader) -> tuple[np.ndarray, np.ndarray | None]:
    loader = loader.with_size(SIZE).with_mode("gray")
    feats = np.zeros((loader.n, SIZE[0] * SIZE[1]))
    codes = np.full(loader.n, -1, dtype=np.int32)
    for batch in loader.batches(5, shuffle=True, seed=1, prefetch=1):
        feats[batch.index] = batch.pixels.reshape(len(batch.index), -1) / 255.0
        if batch.codes is not None:
            codes[batch.index] = batch.codes
    return feats, codes if loader.labels is not None else None


class ClassMeans(CustomModel[ModelSettings]):
    """Nearest class mean; probabilities = softmax(-distance), as two identical draws."""

    id = "test.class_means"
    version = "1.0.0"
    capabilities = Capabilities(
        inference="custom",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        input="collection",
    )
    schema_template = TEMPLATE
    outputs: ClassVar[dict[str, Any]] = OUTPUTS
    pad_extend = False
    scored: ClassVar[list[int]] = []
    keys: ClassVar[list[str | None]] = []

    def _fit(self, loader: ImageLoader, ctx: FitContext) -> FitOutput:
        classes = loader.classes
        feats, codes = _features(loader)
        assert codes is not None
        means = np.stack([feats[codes == k].mean(axis=0) for k in range(len(classes))])
        type(self).keys.append(ctx.fit_key)
        return FitOutput(
            groups={"means": xr.Dataset({"mean": (("class", "feature"), means)})},
            learned={"classes": classes, "n": loader.n},
        )

    def fit_batches(
        self, loader: ImageLoader, s: ModelSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        return self._fit(loader, ctx)

    def fit(self, ds: xr.Dataset, s: ModelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        return self._fit(ArrayImageLoader(ds), ctx)

    def _score(self, fitted: Fitted, loader: ImageLoader) -> xr.DataTree:
        type(self).scored.append(loader.n)
        means = fitted.groups["means"]["mean"].values
        feats, _ = _features(loader)
        d = ((feats[:, None, :] - means[None]) ** 2).sum(-1)
        p = np.exp(-(d - d.min(axis=1, keepdims=True)))
        p /= p.sum(axis=1, keepdims=True)
        y_hat = xr.DataArray(
            np.stack([p, p])[None],  # (chain=1, draw=2): the platform's DRAWS layout
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
        tree = self._score(fitted, ArrayImageLoader(rows))
        pred = tree[PREDICTIONS].to_dataset().reindex(image=ds["image"].values)
        return xr.DataTree.from_dict({PREDICTIONS: pred})


class SampleMeans(ClassMeans):
    """The same model reading datasets (it gets a sample of the collection)."""

    id = "test.sample_means"
    capabilities = Capabilities(
        inference="custom",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
    )
    seen: ClassVar[list[int]] = []

    def fit(self, ds: xr.Dataset, s: ModelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        type(self).seen.append(int(ds.sizes["image"]))
        return super().fit(ds, s, cfg, ctx)


SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: shapes, project: vision}
data:
  dataset: dataset://vision/shapes@latest
  split: {kind: column, dim: image, column: split, holdout: [test]}
model:
  use: test.class_means@^1
  fit: {seed: 3, mode: evaluate_then_refit}
evaluate: {metrics: [accuracy, log_score]}
"""
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://vision/shapes\nmode: predict\n"


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    reg.register_model(ClassMeans)
    reg.register_model(SampleMeans)
    return reg


@pytest.fixture(scope="module")
def collection(tmp_path_factory: pytest.TempPathFactory) -> Collection:
    return local_collection(tmp_path_factory.mktemp("col"), n_per_class=8, size=12, part_bytes=4096)


def test_fit_batches_on_a_collection_evaluates_the_holdout(
    collection: Collection, registry: Registry
) -> None:
    ClassMeans.scored.clear()
    b = fit_spec(SPEC, collection, registry=registry)
    assert b.metrics.values["accuracy"] > 0.6, b.metrics
    assert "log_score" in b.metrics.values
    n_test = int((collection.manifest["split"] == "test").sum())
    assert ClassMeans.scored == [n_test]  # only the holdout images were scored
    assert b.model.learned["n"] == collection.info.n_images  # the refit saw every row
    assert b.evaluation is not None and b.evaluation.model.learned["n"] == 24 - n_test


def test_score_a_subset_scores_only_the_requested_images(
    collection: Collection, registry: Registry
) -> None:
    b = fit_spec(SPEC, collection, registry=registry)
    ClassMeans.scored.clear()
    req = load_score_request(REQ)
    ids = collection.manifest["image_id"].tolist()[:5]
    sub = collection.manifest_dataset().sel(image=ids).drop_vars("label")
    out = score_bundle(b, req, sub, registry=registry, collection=collection, return_full=True)
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert list(y_hat["image"].values) == ids  # padded fit rows dropped
    assert np.isfinite(y_hat.values).all()
    assert np.allclose(y_hat.sum("class").values, 1.0)
    assert out.attrs["representation"] == "draws"
    assert ClassMeans.scored == [5]


def test_score_a_whole_collection(collection: Collection, registry: Registry) -> None:
    b = fit_spec(SPEC, collection, registry=registry)
    out = score_bundle(b, load_score_request(REQ), collection, registry=registry, return_full=True)
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.sizes["image"] == collection.info.n_images


def test_a_model_that_reads_datasets_gets_a_sample(
    collection: Collection, registry: Registry
) -> None:
    spec = load_model_spec(
        SPEC.replace("test.class_means@^1", "test.sample_means@^1").replace(
            "data:\n", "data:\n  collection: {max_samples: 12, size: [8, 8]}\n"
        )
    )
    SampleMeans.seen.clear()
    b = fit_spec(spec, collection, registry=registry)
    assert sum(SampleMeans.seen[:1]) < 12  # the evaluation fit: the train rows of 12 images
    assert SampleMeans.seen[-1] == 12  # the refit: the whole sample
    assert "accuracy" in b.metrics.values


def test_a_collection_takes_no_pipeline(collection: Collection, registry: Registry) -> None:
    spec = load_model_spec(
        SPEC.replace("model:\n", "pipeline:\n  - {id: s, use: core.scale@^1}\nmodel:\n")
    )
    with pytest.raises(MapiError, match="no pipeline steps") as err:
        fit_spec(spec, collection, registry=registry)
    assert err.value.pointer == "/pipeline"


def test_a_collection_refuses_a_masked_split(collection: Collection, registry: Registry) -> None:
    spec = load_model_spec(
        SPEC.replace(
            "{kind: column, dim: image, column: split, holdout: [test]}",
            "{kind: mask_block, dims: [image], fraction: 0.25}",
        )
    )
    with pytest.raises(MapiError, match="row splits") as err:
        fit_spec(spec, collection, registry=registry)
    assert err.value.code is ErrorCode.SPLIT_INVALID


def test_fit_key_is_stable_and_differs_per_role(collection: Collection, registry: Registry) -> None:
    ClassMeans.keys.clear()
    fit_spec(SPEC, collection, registry=registry)
    first = list(ClassMeans.keys)
    ClassMeans.keys.clear()
    fit_spec(SPEC, collection, registry=registry)
    assert ClassMeans.keys == first
    evaluation, production = first
    assert evaluation and production and evaluation != production
    assert len(production) == 32 and int(production, 16) >= 0


def test_the_dataset_path_still_works_for_a_collection_model(
    collection: Collection, registry: Registry
) -> None:
    ds = collection.loader().to_xarray()
    b = fit_spec(SPEC, ds, registry=registry, raw_schema=collection.schema)
    assert b.metrics.values["accuracy"] > 0.6


def test_contract_kit_runs_the_batch_path(collection: Collection, registry: Registry) -> None:
    from mapi_sdk.testing import ModelFixture, check_model

    fixture = ModelFixture(
        spec=SPEC,
        data=collection.loader().to_xarray(),
        raw_schema=collection.schema,
        registry=registry,
    )
    names = ["fit", "bundle_roundtrip_no_pickle", "subset_scoring", "collection_batch_path"]
    report = check_model(ClassMeans, fixture, checks=names, collection=collection)
    result = report.get("collection_batch_path")
    assert result.passed and not result.skipped, report.summary()
    skipped = check_model(ClassMeans, fixture, checks=["fit", "collection_batch_path"])
    assert skipped.get("collection_batch_path").skipped
