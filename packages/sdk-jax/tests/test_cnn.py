"""``dl.cnn_classifier`` through the harness: an image dataset and an image collection."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from mapi_jax.models.base import LAPLACE_FILE, WEIGHTS_FILE
from mapi_jax.testing import image_schema
from mapi_sdk.bundle import Bundle
from mapi_sdk.collection import Collection
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.tracking import MetricRecorder
from mapi_spec import load_score_request

from .conftest import REQ, cnn_spec


@pytest.fixture(scope="module")
def dataset_fit(shapes: xr.Dataset, registry: Registry) -> tuple[Bundle, MetricRecorder]:
    rec = MetricRecorder()
    b = fit_spec(
        cnn_spec(),
        shapes,
        registry=registry,
        raw_schema=image_schema(shapes),
        hooks=FitHooks(tracker=rec),
    )
    return b, rec


@pytest.fixture(scope="module")
def collection_fit(collection: Collection, registry: Registry) -> Bundle:
    return fit_spec(cnn_spec(), collection, registry=registry)


def test_dataset_fit_writes_the_bundle_contract(dataset_fit: tuple[Bundle, MetricRecorder]) -> None:
    b, rec = dataset_fit
    assert {"accuracy", "log_score"} <= set(b.metrics.values)
    groups = b.model.groups
    assert "training" in groups and "misclassified" in groups
    assert groups["training"].attrs["mapi_training"] == "1"
    assert set(b.model.native) == {WEIGHTS_FILE, LAPLACE_FILE}
    learned = b.model.learned
    assert learned["task"] == "classification"
    assert learned["classes"] == ["circle", "cross", "square"]
    assert learned["image_size"] == [8, 8] and learned["channels"] == 1
    assert learned["normalize"]["kind"] == "unit"  # type: ignore[index]
    unc = learned["uncertainty"]
    assert unc["method"] == "laplace" and unc["draws"] == 8  # type: ignore[index]
    assert unc["hessian"] == "full" and unc["prior_precision"] > 0  # type: ignore[index,operator]
    assert set(learned["train"]) >= {"best_epoch", "epochs_run", "stopped_early"}  # type: ignore[arg-type]
    assert learned["arch"]["kind"] == "resnet"  # type: ignore[index]
    # the evaluation fit's points carry the label fit=evaluation; the refit's do not
    assert any(k[0] == "loss" and "split=val" in k[1] for k in rec.series())


def test_collection_fit_has_the_same_layout(
    dataset_fit: tuple[Bundle, MetricRecorder], collection_fit: Bundle
) -> None:
    a, b = dataset_fit[0].model, collection_fit.model
    assert a.learned["classes"] == b.learned["classes"]
    assert a.learned["image_size"] == b.learned["image_size"]
    assert set(a.groups) == set(b.groups)
    assert {"accuracy", "log_score"} <= set(collection_fit.metrics.values)


def test_bundle_round_trip_and_score(
    dataset_fit: tuple[Bundle, MetricRecorder],
    shapes: xr.Dataset,
    registry: Registry,
    tmp_path: Path,
) -> None:
    b = dataset_fit[0]
    path = tmp_path / "b.zarr.zip"
    b.write(path)
    back = Bundle.read(path, registry)
    req = load_score_request(REQ + "mode: predict\n")
    test = shapes.isel(image=slice(0, 5)).drop_vars("label")
    one = score_bundle(b, req, test, registry=registry, return_full=True)
    two = score_bundle(back, req, test, registry=registry, return_full=True)
    y_hat = one["predictions"].to_dataset()["y_hat"]
    assert y_hat.dims == ("chain", "draw", "image", "class")
    assert y_hat.sizes["chain"] == 1 and y_hat.sizes["draw"] == 8 and y_hat.sizes["image"] == 5
    assert y_hat.attrs["meaning"] == "class_probability"
    np.testing.assert_allclose(y_hat.sum("class").values, 1.0, rtol=1e-6)
    np.testing.assert_array_equal(y_hat.values, two["predictions"].to_dataset()["y_hat"].values)


def test_collection_scores_only_the_requested_images(
    collection_fit: Bundle, collection: Collection, registry: Registry
) -> None:
    req = load_score_request(REQ + "mode: predict\n")
    out = score_bundle(collection_fit, req, collection, registry=registry, return_full=True)
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.sizes["image"] == collection.info.n_images
    assert np.isfinite(y_hat.values).all()


@pytest.mark.slow
@pytest.mark.parametrize("method", ["mc_dropout", "none"])
def test_other_uncertainty_methods(method: str, shapes: xr.Dataset, registry: Registry) -> None:
    spec = (
        cnn_spec(
            f", uncertainty: {{method: {method}, draws: 4}}".replace("uncertainty: {draws: 8}", ""),
            fit="{seed: 3}",
        )
        .replace("uncertainty: {draws: 8}, ", "")
        .replace("              ,", "              ")
    )
    b = fit_spec(spec, shapes, registry=registry, raw_schema=image_schema(shapes))
    assert LAPLACE_FILE not in b.model.native
    req = load_score_request(REQ + "mode: predict\n")
    out = score_bundle(b, req, shapes.isel(image=slice(0, 3)), registry=registry, return_full=True)
    y_hat = out["predictions"].to_dataset()["y_hat"]
    if method == "none":
        assert y_hat.sizes["draw"] == 1
    else:
        assert y_hat.sizes["draw"] == 4
        assert float(y_hat.std("draw").mean()) > 0  # dropout makes the draws differ


def test_a_score_request_gets_the_draws(
    dataset_fit: tuple[Bundle, MetricRecorder], shapes: xr.Dataset, registry: Registry
) -> None:
    """DL models give DRAWS: the bundle names its Laplace draws in a ``posterior`` group, so
    the harness thins them and ``output.draws`` returns the first draws of that thinning."""
    b = dataset_fit[0]
    post = b.model.groups["posterior"]
    assert post.sizes == {"chain": 1, "draw": 8} and post.attrs["inference"] == "laplace"
    assert b.thin is not None and len(b.thin) == 8
    req = load_score_request(REQ + "mode: predict\noutput: {draws: 4}\n")
    test = shapes.isel(image=slice(0, 3)).drop_vars("label")
    out = score_bundle(b, req, test, registry=registry, return_full=True)
    draws = out["draws"].to_dataset()["y_hat"]
    assert draws.sizes["sample"] == 4 and draws.sizes["image"] == 3
    full = out["predictions"].to_dataset()["y_hat"]
    for i, (_, d) in enumerate(b.thin[:4]):  # position d of the predictions is draw d
        np.testing.assert_array_equal(
            draws.isel(sample=i).values, full.isel(chain=0, draw=int(d)).values
        )
