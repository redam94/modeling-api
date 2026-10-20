"""``tabular.image_logistic`` (v0.14): image features, scoring from a hand-built posterior,
refusals, and a tiny nutpie fit on the synthetic shapes with media logged."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_tabular import ImageLogistic, ImageLogisticSettings
from mapi_plugin_tabular.image_data import shapes_dataset, shapes_schema, shapes_zip
from mapi_plugin_tabular.image_logistic import (
    FEATURES,
    MAX_SCORE_DRAWS,
    _Basis,
    _block_mean,
    _fit_basis,
    _grid,
    _project,
    _thin,
)
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.media import image_item
from mapi_sdk.metrics import CATEGORICAL_METRICS, compute_metrics
from mapi_sdk.models import Fitted, ScoreContext
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.tracking import MetricRecorder
from mapi_spec import ErrorCode, MapiError, Representation, load_score_request
from mapi_spec.loader import load_model_spec
from mapi_spec.media import MAX_IMAGE_SIDE
from mapi_spec.spec import FitConfig
from pydantic import ValidationError

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: shapes, project: vision}
data:
  dataset: dataset://vision/shapes@latest
  split: {kind: column, dim: image, column: split, holdout: [test]}
model:
  use: tabular.image_logistic@^1
  settings: {n_components: 8}
  fit: {seed: 4, mode: evaluate_then_refit, engine: nutpie, tune: 300, draws: 200, chains: 2}
evaluate: {metrics: [accuracy, accuracy_naive, brier, auc]}
"""
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://vision/shapes\n"


def _settings(**kw: Any) -> ImageLogisticSettings:
    return ImageLogisticSettings.model_validate(kw)


def _flat(ds: xr.Dataset, k: int = 1) -> np.ndarray:
    px = ds["pixels"].transpose("image", "y", "x", "channel").values / 255.0
    return _block_mean(px, k).reshape(ds.sizes["image"], -1)


# ------------------------------------------------------------------------------- example data


def test_shapes_dataset_has_the_image_folder_layout() -> None:
    ds = shapes_dataset(n_per_class=8, size=10)
    assert ds["pixels"].dims == ("image", "y", "x", "channel")
    assert ds["pixels"].dtype == np.uint8 and ds["pixels"].attrs["mapi_image"] == "1"
    assert list(ds["channel"].values) == ["gray"] and ds["image"].values[0] == "00"
    assert sorted(set(ds["label"].values)) == ["circle", "cross", "square"]
    test = ds["split"].values == "test"
    for c in ("circle", "cross", "square"):
        assert int(np.sum(test & (ds["label"].values == c))) == 2  # 25 % of 8
    rgb = shapes_dataset(n_per_class=2, size=8, rgb=True, classes=("ring", "triangle"))
    assert list(rgb["channel"].values) == ["r", "g", "b"]
    np.testing.assert_array_equal(
        shapes_dataset(n_per_class=3, size=8)["pixels"].values,
        shapes_dataset(n_per_class=3, size=8)["pixels"].values,
    )


def test_shapes_zip_writes_pngs_and_a_manifest(tmp_path: Path) -> None:
    out = shapes_zip(tmp_path / "shapes.zip", n_per_class=2, size=8)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        manifest = zf.read("manifest.csv").decode().splitlines()
        first = zf.read(names[0])
    assert "manifest.csv" in names and len(names) == 7
    assert manifest[0] == "path,label,split" and len(manifest) == 7
    assert first.startswith(b"\x89PNG\r\n\x1a\n")


# ----------------------------------------------------------------------------------- features


def test_block_mean_and_pca_are_deterministic() -> None:
    ds = shapes_dataset(n_per_class=10, size=12)
    px = ds["pixels"].transpose("image", "y", "x", "channel").values / 255.0
    blocks = _block_mean(px, 3)
    assert blocks.shape == (30, 4, 4, 1)
    np.testing.assert_allclose(blocks[0, 0, 0, 0], px[0, :3, :3, 0].mean())
    s = _settings(n_components=6)
    a, b = _fit_basis(_flat(ds), s, seed=1), _fit_basis(_flat(ds), s, seed=2)
    assert a.components is not None and b.components is not None
    np.testing.assert_allclose(a.components, b.components)
    z = _project(_flat(ds), a)
    assert z.shape == (30, 6)
    np.testing.assert_allclose(z.std(axis=0), 1.0, rtol=1e-6)
    assert a.explained is not None and 0 < a.explained <= 1
    ds_a = a.dataset(s, (12, 12, 1), ["circle", "cross", "square"])
    back = _Basis.from_dataset(ds_a)
    np.testing.assert_allclose(_project(_flat(ds), back), z)
    assert json.loads(ds_a.attrs["classes"]) == ["circle", "cross", "square"]


def test_pixel_features_and_their_limit() -> None:
    ds = shapes_dataset(n_per_class=4, size=16)
    basis = _fit_basis(_flat(ds, 2), _settings(features="pixels", downsample=2), seed=0)
    assert basis.components is None and basis.scale.size == 64
    with pytest.raises(MapiError) as err:
        _fit_basis(_flat(ds), _settings(features="pixels", max_features=64), seed=0)
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_settings_refusals() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        _settings(image_dim="sample")
    with pytest.raises(ValidationError, match="different"):
        _settings(pixels="x1", target="x1")
    with pytest.raises(ValidationError, match="repeat"):
        _settings(class_order=["a", "a"])


def test_draws_are_thinned_by_a_draw_stride() -> None:
    post = xr.Dataset(
        {"alpha": (("chain", "draw", "class"), np.zeros((4, 1000, 2)))},
        coords={"chain": np.arange(4), "draw": np.arange(1000), "class": ["a", "b"]},
    )
    thin = _thin(post, MAX_SCORE_DRAWS)
    assert thin.sizes["chain"] == 4 and thin.sizes["chain"] * thin.sizes["draw"] <= 500
    assert list(thin["draw"].values[:3]) == [0, 8, 16] and "sample" not in thin.dims


# ------------------------------------------------------------------------------------- score


def _fitted(ds: xr.Dataset, classes: list[str], seed: int = 0) -> Fitted:
    """A fitted model whose posterior holds random draws (no MCMC)."""
    s = _settings(n_components=4)
    basis = _fit_basis(_flat(ds), s, seed=0)
    rng = np.random.default_rng(seed)
    k = len(classes)
    alpha = rng.normal(size=(2, 30, k))
    beta = rng.normal(size=(2, 30, 4, k))
    post = xr.Dataset(
        {
            "alpha": (("chain", "draw", "class"), alpha - alpha.mean(-1, keepdims=True)),
            "beta": (("chain", "draw", "feature", "class"), beta - beta.mean(-1, keepdims=True)),
        },
        coords={
            "chain": [0, 1],
            "draw": np.arange(30),
            "class": classes,
            "feature": [f"pc{j}" for j in range(4)],
        },
    )
    return Fitted(
        manifest=ImageLogistic.manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=FitConfig(),
        groups={"posterior": post, FEATURES: basis.dataset(s, (12, 12, 1), classes)},
    )


def test_score_from_a_hand_built_posterior() -> None:
    ds = shapes_dataset(n_per_class=5, size=12)
    classes = ["circle", "cross", "square"]
    out = ImageLogistic().score(
        _fitted(ds, classes), ds.drop_vars("label"), load_score_request(REQ), ScoreContext()
    )
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.dims == ("chain", "draw", "image", "class")
    assert y_hat.sizes["image"] == 15 and list(y_hat["class"].values) == classes
    assert y_hat.attrs["meaning"] == "class_probability"
    np.testing.assert_allclose(y_hat.sum("class").values, 1.0)
    # an unknown label in the holdout counts as a miss
    obs = ds["label"].copy()
    obs.values[0] = "hexagon"
    values, _ = compute_metrics(y_hat, obs, Representation.DRAWS, ["accuracy", "log_score"])
    base, _ = compute_metrics(y_hat, ds["label"], Representation.DRAWS, ["accuracy"])
    assert values["accuracy"] <= base["accuracy"]
    assert values["log_score"] < np.log(0.5)


def test_score_refusals() -> None:
    ds = shapes_dataset(n_per_class=3, size=12)
    fitted = _fitted(ds, ["circle", "cross", "square"])
    req, ctx = load_score_request(REQ), ScoreContext()
    bad = ds.assign(pixels=ds["pixels"].astype(float) * 2)  # values up to 510
    with pytest.raises(MapiError, match="from 0 to 255") as err:
        ImageLogistic().score(fitted, bad, req, ctx)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    as_float = ds.assign(pixels=ds["pixels"].astype(float))  # numbers in 0..255: accepted
    ImageLogistic().score(fitted, as_float, req, ctx)
    small = shapes_dataset(n_per_class=3, size=10)
    with pytest.raises(MapiError, match="fitted on 12 x 12"):
        ImageLogistic().score(fitted, small, req, ctx)
    with pytest.raises(MapiError) as err:
        ImageLogistic().score(fitted, ds.drop_vars("pixels"), req, ctx)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_template_and_manifest() -> None:
    man = ImageLogistic.manifest()
    schema = man.schema_template.resolve({"image_dim": "photo", "target": "kind"})
    assert schema.vars["pixels"].dims == ("photo", "y", "x", "channel")
    assert schema.vars["pixels"].dtype == "uint8"
    assert schema.vars["kind"].dtype == "str" and schema.vars["kind"].missing == "allow"
    assert schema.dims["photo"].new_level_strategy == "pooled"
    assert list(man.plots) == ["core.roc_pr", "core.confusion", "core.convergence"]
    assert man.capabilities.loo_valid is False


# --------------------------------------------------------------------------------- MCMC fit


def test_the_guide_spec_names_metrics_for_classes() -> None:
    """The default metrics (rmse, mae, mape) skip a categorical target: the guide's spec
    must name metrics that a classifier has, or the run shows no holdout metric."""
    guide = Path(__file__).resolve().parents[3] / "docs" / "guides" / "image-data.md"
    blocks = re.findall(r"```yaml\n(.*?)```", guide.read_text("utf-8"), flags=re.S)
    (text,) = [b for b in blocks if "tabular.image_logistic" in b]
    spec = load_model_spec(text)
    assert spec.evaluate.metrics
    assert set(spec.evaluate.metrics) <= set(CATEGORICAL_METRICS)
    ImageLogisticSettings.model_validate(spec.model.settings)


class _MediaCtx:
    """Just enough of a fit context for ``_log_images``: encodes each image as a run would."""

    tracking_media = True

    def __init__(self) -> None:
        self.items: list[Any] = []
        self.lines: list[tuple[str, str]] = []

    def log_image(self, key: str, image: Any, caption: str | None = None) -> None:
        self.items.append(image_item(key, image, caption=caption))

    def log_line(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))


def test_grid_wraps_into_rows_within_the_image_side_limit() -> None:
    # 32 classes of 128 x 128 used to make one 128 x 4127 strip, which image_item refuses
    grid, n = _grid([np.zeros((128, 128, 1))] * 32)
    assert grid is not None and n == 32
    assert grid.shape == (2 * 128 + 1, 31 * 129 - 1)
    assert max(grid.shape) <= MAX_IMAGE_SIDE
    assert image_item("class_means", grid).content_type == "image/png"
    # 8 wide tiles (16 x 512, 8 * 513 - 1 = 4103 in one strip) wrap the same way
    grid, n = _grid([np.zeros((16, 512, 3))] * 8)
    assert grid is not None and n == 8 and grid.shape == (2 * 17 - 1, 7 * 513 - 1, 3)
    # tiles that cannot fit in the square are left out; one oversized tile gives nothing
    grid, n = _grid([np.zeros((1024, 1024, 1))] * 20)
    assert grid is not None and n == 9 and grid.shape == (3 * 1025 - 1, 3 * 1025 - 1)
    assert _grid([np.zeros((10, MAX_IMAGE_SIDE + 1, 1))]) == (None, 0)


def test_media_never_fail_a_fit_with_many_classes_or_wide_images() -> None:
    rng = np.random.default_rng(0)
    n_classes, shape = 40, (128, 128, 1)
    blocks = rng.random((2 * n_classes, *shape))
    codes = np.repeat(np.arange(n_classes), 2)
    classes = [f"c{j}" for j in range(n_classes)]
    comps = rng.random((8, int(np.prod(shape))))
    basis = _Basis(np.zeros(comps.shape[1]), np.ones(8), comps, 1, 0.5)
    ctx = _MediaCtx()
    ImageLogistic()._log_images(ctx, blocks, codes, classes, basis, shape)  # type: ignore[arg-type]
    assert [m.key for m in ctx.items] == ["pca_components", "class_means"]
    assert all(max(m.height or 0, m.width or 0) <= MAX_IMAGE_SIDE for m in ctx.items)
    assert not ctx.lines
    # a tile over the side limit is skipped with a warning line instead of raising
    wide = (4, MAX_IMAGE_SIDE + 1, 1)
    ctx = _MediaCtx()
    ImageLogistic()._log_images(  # type: ignore[arg-type]
        ctx,
        rng.random((2, *wide)),
        np.array([0, 1]),
        ["a", "b"],
        _Basis(np.zeros(1), np.ones(1), None, 1, None),
        wide,
    )
    assert not ctx.items and ctx.lines and ctx.lines[0][0] == "warning"


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.mark.slow
def test_tiny_fit_classifies_the_shapes_and_logs_media(registry: Registry) -> None:
    ds = shapes_dataset(n_per_class=20, size=12)
    schema = shapes_schema(ds)
    assert schema.vars["pixels"].dtype == "uint8"
    recorder = MetricRecorder()
    b = fit_spec(SPEC, ds, registry=registry, raw_schema=schema, hooks=FitHooks(tracker=recorder))
    assert b.metrics.values["accuracy"] > 0.8, b.metrics
    assert b.metrics.values["auc"] > 0.9
    assert {"alpha", "beta"} <= set(b.model.posterior.data_vars)
    assert FEATURES in b.model.groups
    keys = {m.key for m in recorder.media}
    assert {"pca_components", "class_means"} <= keys
    assert all(m.content_type == "image/png" for m in recorder.media)
    assert recorder.params["image.features"] == "pca"
    req = load_score_request(REQ + "mode: predict\n")
    test = ds.isel(image=np.flatnonzero(ds["split"].values == "test"))
    out = score_bundle(b, req, test.drop_vars("label"), registry=registry, return_full=True)
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.sizes["chain"] * y_hat.sizes["draw"] <= MAX_SCORE_DRAWS
    pred = y_hat.mean(("chain", "draw")).argmax("class").values
    hits = np.asarray(y_hat["class"].values)[pred] == test["label"].values
    assert hits.mean() > 0.8
