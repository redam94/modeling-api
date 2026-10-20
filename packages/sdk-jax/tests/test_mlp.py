"""``dl.mlp`` through the harness: a regression and a 3-class classification."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_jax.models import MLPSettings
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_spec import load_score_request
from pydantic import ValidationError

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: table, project: demo}
data:
  dataset: dataset://demo/table@latest
  split: {kind: column, dim: row, column: split, holdout: [test]}
model:
  use: dl.mlp@^1
  settings: {rows: [row], target_dtype: __DTYPE__, hidden: [16],
             train: {epochs: 12, batch_size: 16, learning_rate: 0.02, early_stopping: null},
             uncertainty: {draws: 6}}
  fit: {seed: 1, mode: evaluate_then_refit}
evaluate: {metrics: [__METRICS__]}
"""
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://demo/table\nmode: predict\n"


def _table(kind: str, n: int = 90) -> xr.Dataset:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(n, 2))
    if kind == "regression":
        y: np.ndarray = 3.0 * x[:, 0] - x[:, 1] + rng.normal(scale=0.1, size=n) + 10
    else:
        score = x[:, 0] + 0.5 * x[:, 1]
        y = np.where(score < -0.4, "low", np.where(score > 0.4, "high", "mid"))
    split = np.where(np.arange(n) % 5 == 0, "test", "train")
    return xr.Dataset(
        {"x1": ("row", x[:, 0]), "x2": ("row", x[:, 1]), "y": ("row", y)},
        coords={"row": [f"r{i:03d}" for i in range(n)], "split": ("row", split)},
    )


def _fit(kind: str, registry: Registry) -> tuple[object, xr.Dataset]:
    ds = _table(kind)
    dtype, metrics = (
        ("float64", "rmse, mae") if kind == "regression" else ("str", "accuracy, log_score")
    )
    spec = SPEC.replace("__DTYPE__", dtype).replace("__METRICS__", metrics)
    schema = infer_schema(ds, targets=["y"], policies={"row": "new_levels"})
    return fit_spec(spec, ds, registry=registry, raw_schema=schema), ds


def test_regression(registry: Registry) -> None:
    b, ds = _fit("regression", registry)
    assert b.metrics.values["rmse"] < 2.0, b.metrics  # target sd is about 3.2
    learned = b.model.learned
    assert learned["task"] == "regression" and learned["features"]["vars"] == ["x1", "x2"]
    assert learned["target"]["sigma"] > 0
    assert "training" in b.model.groups and "misclassified" not in b.model.groups
    req = load_score_request(REQ)
    out = score_bundle(
        b, req, ds.isel(row=slice(0, 4)).drop_vars("y"), registry=registry, return_full=True
    )
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.dims == ("chain", "draw", "row") and y_hat.sizes["draw"] == 6
    assert np.isfinite(y_hat.values).all()
    pred = load_score_request(REQ + "output: {kind: predictive}\n")
    noisy = score_bundle(
        b, pred, ds.isel(row=slice(0, 4)).drop_vars("y"), registry=registry, return_full=True
    )
    assert float(noisy["predictions"].to_dataset()["y_hat"].std("draw").mean()) >= float(
        y_hat.std("draw").mean()
    )


def test_classification(registry: Registry) -> None:
    b, ds = _fit("classification", registry)
    assert b.metrics.values["accuracy"] > 0.6, b.metrics
    assert b.model.learned["classes"] == ["high", "low", "mid"]
    h = b.model.groups["training"]
    assert h["confusion"].dims == ("epoch", "true_class", "pred_class")  # validation rows
    req = load_score_request(REQ)
    out = score_bundle(
        b, req, ds.isel(row=slice(0, 5)).drop_vars("y"), registry=registry, return_full=True
    )
    y_hat = out["predictions"].to_dataset()["y_hat"]
    assert y_hat.dims == ("chain", "draw", "row", "class")
    assert y_hat.attrs["meaning"] == "class_probability"
    np.testing.assert_allclose(y_hat.sum("class").values, 1.0, rtol=1e-6)


def test_settings() -> None:
    with pytest.raises(ValidationError):
        MLPSettings.model_validate({"rows": ["class"]})
    with pytest.raises(ValidationError):
        MLPSettings.model_validate({"rows": ["row"], "features": ["y"]})
    s = MLPSettings.model_validate({"rows": ["row"], "target_dtype": "int64"})
    assert s.resolved_task == "classification"
    assert MLPSettings.model_validate({"rows": ["row"]}).resolved_task == "regression"


def test_a_score_request_gets_the_draws(registry: Registry) -> None:
    b, ds = _fit("regression", registry)
    assert b.model.groups["posterior"].sizes == {"chain": 1, "draw": 6}  # type: ignore[attr-defined]
    req = load_score_request(REQ + "output: {draws: 3}\n")
    out = score_bundle(b, req, ds.isel(row=slice(0, 4)).drop_vars("y"), registry=registry)
    assert out["draws"].to_dataset()["y_hat"].sizes["sample"] == 3


def test_rows_the_split_marks_val_are_the_validation_data(registry: Registry) -> None:
    """``train.val_split_value`` (default ``val``), as the image models: the rows whose split
    coord has the value validate, not a random ``val_fraction``."""
    ds = _table("classification")
    marks = np.where(np.arange(ds.sizes["row"]) % 3 == 0, "val", ds["split"].values)
    ds = ds.assign_coords(split=("row", marks.astype(str)))
    spec = SPEC.replace("__DTYPE__", "str").replace("__METRICS__", "accuracy")
    spec = spec.replace("mode: evaluate_then_refit", "mode: train_only")
    schema = infer_schema(ds, targets=["y"], policies={"row": "new_levels"})
    b = fit_spec(spec, ds, registry=registry, raw_schema=schema)
    train = b.model.learned["train"]
    n_val = int((marks == "val").sum())
    n_train = int((marks == "train").sum())  # the test rows are held out
    assert train["n_val"] == n_val and train["n_train"] == n_train  # type: ignore[index]
