"""LightGBM and CatBoost classifiers (v0.14, the ``mapi-sklearn[gbm]`` extra): plugin fits,
safe persistence round trips, malformed artifacts and the missing-extra message."""

from __future__ import annotations

import json
import sys
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.errors import BundleError
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_sklearn.estimators import build_estimator, get_entry, resolve_choice
from mapi_sklearn.persistence import artifact_name, dump_estimator, load_estimator
from mapi_sklearn.testing import demand_dataset, demand_registry
from mapi_spec import load_model_spec, load_score_request
from mapi_spec.errors import ErrorCode, MapiError

LGBM = {"kind": "lightgbm.LGBMClassifier", "params": {"n_estimators": 30, "num_leaves": 8}}
CATBOOST = {"kind": "catboost.CatBoostClassifier", "params": {"iterations": 40, "depth": 3}}
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/x\n"
SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: gbm, project: retail}
data:
  dataset: dataset://retail/gbm@latest
  bind:
    dims: {time: week}
    vars: {y: y, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 8}
model:
  use: __MODEL__@^1
  settings: __SETTINGS__
  fit: {seed: 2, mode: evaluate_then_refit}
evaluate: {metrics: [accuracy, accuracy_naive, auc]}
"""


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


def _needs(kind: str) -> None:
    pytest.importorskip(kind.split(".")[0])


@pytest.fixture(scope="module")
def reg() -> Registry:
    return demand_registry()


def _xy(k: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(200, 4))
    score = X[:, 0] - 0.5 * X[:, 1]
    edges = np.quantile(score, np.linspace(0, 1, k + 1)[1:-1])
    return X, np.searchsorted(edges, score)


@pytest.mark.parametrize("choice", [LGBM, CATBOOST], ids=["lightgbm", "catboost"])
@pytest.mark.parametrize("k", [2, 3])
def test_round_trip_gives_equal_probabilities(choice: dict[str, Any], k: int) -> None:
    _needs(choice["kind"])
    entry = get_entry(choice["kind"])
    est = build_estimator(resolve_choice(choice), seed=4, n_jobs=1)
    X, y = _xy(k)
    est.fit(X, y)
    raw = dump_estimator(entry, est)
    back = load_estimator(entry, raw, name=artifact_name(entry, "point", 0))
    assert np.asarray(back.classes_).tolist() == list(range(k))
    np.testing.assert_allclose(back.predict_proba(X), est.predict_proba(X), rtol=1e-6, atol=1e-9)


def test_catboost_writes_no_files_into_the_working_directory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _needs(CATBOOST["kind"])
    monkeypatch.chdir(tmp_path)
    est = build_estimator(resolve_choice(CATBOOST), seed=1, n_jobs=1)
    X, y = _xy(3)
    est.fit(X, y)
    dump_estimator(get_entry(CATBOOST["kind"]), est)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("kind", "raw", "match"),
    [
        ("lightgbm.LGBMClassifier", b"version=v4\n", "not a LightGBM text model"),
        ("lightgbm.LGBMClassifier", b"tree\nnum_class=garbage\n", "could not be loaded"),
        ("catboost.CatBoostClassifier", b"not json", "not CatBoost JSON"),
        ("catboost.CatBoostClassifier", json.dumps({"x": 1}).encode(), "not a CatBoost model"),
        (
            "catboost.CatBoostClassifier",
            json.dumps({"oblivious_trees": "nope"}).encode(),
            "not a valid CatBoost model",
        ),
    ],
)
def test_malformed_artifacts_are_bundle_errors(kind: str, raw: bytes, match: str) -> None:
    _needs(kind)
    with pytest.raises(BundleError, match=match):
        load_estimator(get_entry(kind), raw, name="m")


def _catboost_doc() -> dict[str, Any]:
    est = build_estimator(resolve_choice(CATBOOST), seed=2, n_jobs=1)
    X, y = _xy(3)
    est.fit(X, y)
    doc: dict[str, Any] = json.loads(dump_estimator(get_entry(CATBOOST["kind"]), est))
    return doc


@pytest.mark.parametrize(
    ("breakage", "match"),
    [
        (lambda d: d["oblivious_trees"][0]["splits"][0].update(float_feature_index=99), "split"),
        (lambda d: d["oblivious_trees"][0]["splits"][0].update(split_index=10_000), "split"),
        (lambda d: d["oblivious_trees"][0].update(leaf_values=[0.1, 0.2, 0.3]), "leaf table"),
        (lambda d: d["oblivious_trees"].clear(), "non-empty"),
        (lambda d: d.update(ctr_data={}), "keys"),
        (lambda d: d["scale_and_bias"].__setitem__(1, [0.0]), "scale_and_bias"),
        (lambda d: d["features_info"].update(categorical_features=[]), "float features"),
    ],
    ids=["feature", "border", "leaves", "no-trees", "ctr", "bias", "categorical"],
)
def test_catboost_documents_are_checked_before_loading(breakage: Any, match: str) -> None:
    """CatBoost's own JSON reader crashes the process on such documents."""
    _needs(CATBOOST["kind"])
    doc = _catboost_doc()
    load_estimator(get_entry(CATBOOST["kind"]), json.dumps(doc).encode())  # intact: loads
    breakage(doc)
    with pytest.raises(BundleError, match=match):
        load_estimator(get_entry(CATBOOST["kind"]), json.dumps(doc).encode())


def test_missing_extra_is_a_settings_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod in ("lightgbm", "catboost"):
        monkeypatch.setitem(sys.modules, mod, None)  # import raises ImportError
    for kind in ("lightgbm.LGBMClassifier", "catboost.CatBoostClassifier"):
        with pytest.raises(MapiError) as err:
            build_estimator(resolve_choice({"kind": kind}), seed=0)
        assert err.value.code is ErrorCode.SETTINGS_INVALID
        assert "mapi-sklearn[gbm]" in (err.value.untrusted_text or str(err.value))
    # the sklearn kinds keep working without the extra
    from mapi_sklearn.estimators import choice_from_instance
    from sklearn.linear_model import LogisticRegression

    assert choice_from_instance(LogisticRegression(C=2.0)).kind == "sklearn.LogisticRegression"


def _tiers(binary: bool) -> tuple[xr.Dataset, Any]:
    """The demand panel with a binary (sold out) or a three-class (tier) target."""
    data = demand_dataset(stores=5, weeks=50)
    units = data["units_sold"]
    if binary:
        target = (units > units.median()).astype(float)
    else:
        lo, hi = np.quantile(units.values, [1 / 3, 2 / 3])
        code = ((units > lo).astype(int) + (units > hi).astype(int)).values
        target = xr.DataArray(np.array(["low", "mid", "high"])[code], dims=units.dims)
    ds = data.drop_vars("units_sold").assign(y=target)
    schema = infer_schema(ds, targets=["y"], policies={"store": "new_levels"})
    return ds, schema


def _spec(model: str, estimator: dict[str, Any]) -> Any:
    settings = {
        "estimator": estimator,
        "rows": ["store", "sku", "time"],
        "coord_features": {"store": "onehot", "sku": "onehot"},
    }
    return load_model_spec(
        SPEC.replace("__MODEL__", model).replace("__SETTINGS__", json.dumps(settings))
    )


@pytest.mark.parametrize("choice", [LGBM, CATBOOST], ids=["lightgbm", "catboost"])
def test_binary_plugin_fits_and_scores(choice: dict[str, Any], reg: Registry) -> None:
    _needs(choice["kind"])
    ds, schema = _tiers(binary=True)
    b = fit_spec(_spec("sklearn.tabular_classifier", choice), ds, registry=reg, raw_schema=schema)
    assert b.model.learned["classes"] == [0.0, 1.0]
    assert "feature_importance" in b.model.groups
    p = _predict(b, ds, reg)
    assert bool(((p >= 0) & (p <= 1)).all()) and p.attrs["meaning"] == "probability"
    assert b.metrics.values["auc"] > 0.6


@pytest.mark.parametrize("choice", [LGBM, CATBOOST], ids=["lightgbm", "catboost"])
def test_multiclass_plugin_fits_and_scores(choice: dict[str, Any], reg: Registry) -> None:
    _needs(choice["kind"])
    ds, schema = _tiers(binary=False)
    spec = _spec("sklearn.multiclass_classifier", choice)
    b = fit_spec(spec, ds, registry=reg, raw_schema=schema)
    p = _predict(b, ds, reg)
    assert list(p["class"].values) == ["high", "low", "mid"]
    np.testing.assert_allclose(p.sum("class").values, 1.0, atol=1e-6)
    assert b.metrics.values["auc"] > 0.7
