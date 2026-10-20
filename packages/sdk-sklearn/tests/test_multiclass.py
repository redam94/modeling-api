"""``sklearn.multiclass_classifier`` (v0.14): one probability per class over text or integer
labels, class_order, holdout metrics and the contract kit."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sklearn import MulticlassClassifier, MulticlassClassifierSettings
from mapi_sklearn.testing import demand_dataset, demand_registry
from mapi_spec import DataSchema, ModelSpec, load_model_spec, load_score_request
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import ValidationError

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: demand-tiers, project: retail}
data:
  dataset: dataset://retail/demand_tiers@latest
  bind:
    dims: {time: week}
    vars: {y: tier, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 8}
model:
  use: sklearn.multiclass_classifier@^1
  settings: __SETTINGS__
  fit: {seed: 5, mode: evaluate_then_refit}
evaluate: {metrics: [accuracy, accuracy_naive, brier, auc]}
"""
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/demand-tiers\n"
LABELS = np.array(["low", "mid", "high"])


@pytest.fixture(scope="module")
def reg() -> Registry:
    return demand_registry()


def _data(numeric: bool = False) -> xr.Dataset:
    data = demand_dataset(stores=6, weeks=50)
    units = data["units_sold"]
    lo, hi = np.quantile(units.values, [1 / 3, 2 / 3])
    code = (units > lo).astype(int) + (units > hi).astype(int)
    tier = (
        code if numeric else xr.DataArray(LABELS[code.values], dims=code.dims, coords=code.coords)
    )
    return data.drop_vars("units_sold").assign(tier=tier)


def _case(
    estimator: dict[str, Any], extra: dict[str, Any] | None = None, *, numeric: bool = False
) -> tuple[ModelSpec, xr.Dataset, DataSchema]:
    ds = _data(numeric)
    schema = infer_schema(ds, targets=["tier"], policies={"store": "new_levels"})
    settings = {
        "estimator": estimator,
        "rows": ["store", "sku", "time"],
        "coord_features": {"store": "onehot", "sku": "onehot"},
        **({"target_dtype": "int64"} if numeric else {}),
        **(extra or {}),
    }
    spec = load_model_spec(SPEC.replace("__SETTINGS__", json.dumps(settings)))
    return spec, ds, schema


def _predict(b: Any, ds: xr.Dataset, reg: Registry) -> xr.DataArray:
    out = score_bundle(
        b,
        load_score_request(REQ + "mode: predict\noutput: {units: model}\n"),
        ds.isel(week=slice(-6, None)),
        registry=reg,
        return_full=True,
    )
    node = out["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"]


ESTIMATORS = [
    {"kind": "sklearn.LogisticRegression", "params": {"C": 1.0, "max_iter": 500}},
    {"kind": "sklearn.RandomForestClassifier", "params": {"n_estimators": 20, "max_depth": 4}},
    {"kind": "sklearn.HistGradientBoostingClassifier", "params": {"max_iter": 30}},
    {"kind": "xgboost.XGBClassifier", "params": {"n_estimators": 20, "max_depth": 3}},
]


@pytest.mark.parametrize("estimator", ESTIMATORS, ids=lambda e: e["kind"])
def test_text_labels_give_one_probability_per_class(
    estimator: dict[str, Any], reg: Registry
) -> None:
    spec, ds, schema = _case(estimator)
    b = fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert b.model.learned["class_labels"] == ["high", "low", "mid"]
    p = _predict(b, ds, reg)
    assert p.dims[-1] == "class" and list(p["class"].values) == ["high", "low", "mid"]
    assert p.attrs["meaning"] == "class_probability"
    np.testing.assert_allclose(p.sum("class").values, 1.0, atol=1e-6)
    assert bool(((p >= 0) & (p <= 1)).all())
    values = b.metrics.values
    for name in ("accuracy", "brier", "auc"):
        assert values[name] is not None, b.metrics
    assert values["accuracy"] > values["accuracy_naive"]
    assert 0.5 < values["auc"] <= 1.0


def test_integer_labels_and_class_order(reg: Registry) -> None:
    spec, ds, schema = _case(
        {"kind": "sklearn.LogisticRegression", "params": {"max_iter": 500}},
        {"class_order": ["2", "1", "0", "3"]},
        numeric=True,
    )
    assert ds["tier"].dtype.kind == "i"
    b = fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert b.model.learned["class_labels"] == ["2", "1", "0", "3"]
    p = _predict(b, ds, reg)
    assert list(p["class"].values) == ["2", "1", "0", "3"]
    assert float(p.sel({"class": "3"}).max()) == 0.0  # never seen in training
    np.testing.assert_allclose(p.sum("class").values, 1.0, atol=1e-6)
    assert b.metrics.values["accuracy"] is not None and b.metrics.values["auc"] is not None


def test_missing_and_empty_labels_do_not_train(reg: Registry) -> None:
    spec, ds, schema = _case({"kind": "sklearn.LogisticRegression", "params": {"max_iter": 500}})
    tier = ds["tier"].values.copy()
    tier[0, 0, :5] = ""
    b = fit_spec(spec, ds.assign(tier=(ds["tier"].dims, tier)), registry=reg, raw_schema=schema)
    assert b.model.learned["class_labels"] == ["high", "low", "mid"]
    assert b.model.learned["dropped"]["missing_target"] >= 5


def test_settings_and_data_errors(reg: Registry) -> None:
    with pytest.raises(ValidationError, match="classifier is needed"):
        MulticlassClassifierSettings.model_validate(
            {"estimator": {"kind": "sklearn.Ridge"}, "rows": ["item"], "time_kind": "none"}
        )
    with pytest.raises(ValidationError, match="repeat"):
        MulticlassClassifierSettings.model_validate(
            {
                "estimator": {"kind": "sklearn.LogisticRegression"},
                "rows": ["item"],
                "time_kind": "none",
                "class_order": ["a", "a"],
            }
        )
    spec, ds, schema = _case(
        {"kind": "sklearn.LogisticRegression"}, {"class_order": ["low", "mid"]}
    )
    with pytest.raises(MapiError) as err:
        fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID and "high" in (
        err.value.untrusted_text or ""
    )
    spec, ds, schema = _case({"kind": "sklearn.LogisticRegression"}, {"max_classes": 2})
    with pytest.raises(MapiError) as err:
        fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    spec, ds, schema = _case({"kind": "sklearn.LogisticRegression"})
    with pytest.raises(MapiError) as err:
        fit_spec(
            spec, ds.assign(tier=ds["tier"].where(False, "low")), registry=reg, raw_schema=schema
        )
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_manifest() -> None:
    man = MulticlassClassifier.manifest()
    assert list(man.plots) == ["core.roc_pr", "core.confusion", "ml.feature_importance"]
    assert set(man.capabilities.score_modes) == {"predict", "evaluate"}
    assert list(man.outputs["y_hat"].dims) == ["class"]


@pytest.mark.mapi_contract
def test_multiclass_classifier_passes_the_kit(reg: Registry) -> None:
    spec, ds, schema = _case({"kind": "sklearn.LogisticRegression", "params": {"max_iter": 500}})
    report = check_model(
        MulticlassClassifier,
        ModelFixture(spec=spec, data=ds, raw_schema=schema, registry=reg),
        raise_on_failure=False,
    )
    assert report.ok, report.summary()
