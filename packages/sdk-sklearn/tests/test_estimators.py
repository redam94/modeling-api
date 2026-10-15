"""The allow-listed estimator registry: params validation, construction and persistence."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xgboost as xgb
from mapi_sdk.bundle import assert_no_pickle
from mapi_sdk.errors import BundleError
from mapi_sklearn.estimators import (
    ESTIMATORS,
    build_estimator,
    choice_from_instance,
    get_entry,
    resolve_choice,
)
from mapi_sklearn.persistence import artifact_name, dump_estimator, load_estimator
from mapi_sklearn.settings import PointSettings
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import TypeAdapter, ValidationError
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.svm import SVR

KINDS = sorted(ESTIMATORS)


def _data(task: str, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(120, 3))
    X[:, 2] = rng.integers(0, 4, 120)
    y = X[:, 0] - 0.5 * X[:, 1] + 0.3 * X[:, 2] + rng.normal(0, 0.1, 120)
    if task == "classification":
        y = (y > 0).astype(int)
    return X, y


def test_registry_entries_are_consistent() -> None:
    assert len(KINDS) == 12
    for kind, entry in ESTIMATORS.items():
        assert entry.kind == kind
        assert entry.choice.model_fields["kind"].annotation is not None
        assert entry.load_class().__name__ == entry.name
        if entry.quantile == "per_level":
            assert entry.quantile_setter is not None
        assert entry.task in ("regression", "classification")
    quantile_capable = {k for k, e in ESTIMATORS.items() if e.quantile}
    assert quantile_capable == {
        "sklearn.QuantileRegressor",
        "sklearn.GradientBoostingRegressor",
        "sklearn.HistGradientBoostingRegressor",
        "xgboost.XGBRegressor",
    }


@pytest.mark.parametrize("kind", KINDS)
def test_every_estimator_fits_and_round_trips_without_pickle(kind: str) -> None:
    entry = get_entry(kind)
    choice = resolve_choice({"kind": kind})
    X, y = _data(entry.task)
    est = build_estimator(choice, seed=3, n_jobs=1)
    est.fit(X, y)
    raw = dump_estimator(entry, est)
    assert_no_pickle({artifact_name(entry, "point", 0): raw})
    assert b"\x80\x04\x95" not in raw[:64]
    back = load_estimator(entry, raw, name="m")
    assert type(back) is type(est)
    if entry.task == "classification":
        np.testing.assert_allclose(back.predict_proba(X), est.predict_proba(X))
    else:
        np.testing.assert_allclose(back.predict(X), est.predict(X))
    name = artifact_name(entry, "quantile", 7)
    assert name == f"quantile/0007{'.xgb.json' if entry.persistence == 'xgboost' else '.skops'}"


@pytest.mark.parametrize(
    "kind",
    [k for k in KINDS if ESTIMATORS[k].quantile is not None],
)
def test_quantile_variants_round_trip(kind: str) -> None:
    """Quantile losses introduce extra skops types; the allow-list must cover them."""
    from mapi_sklearn.uncertainty import quantile_overrides

    entry = get_entry(kind)
    choice = resolve_choice({"kind": kind})
    X, y = _data("regression")
    for ov in quantile_overrides(entry, choice, [0.1, 0.9]):
        est = build_estimator(choice, seed=1, overrides=ov)
        est.fit(X, y)
        back = load_estimator(entry, dump_estimator(entry, est))
        np.testing.assert_allclose(back.predict(X), est.predict(X))


@pytest.mark.parametrize(
    ("kind", "loss"),
    [
        ("sklearn.GradientBoostingRegressor", "absolute_error"),
        ("sklearn.GradientBoostingRegressor", "huber"),
        ("sklearn.HistGradientBoostingRegressor", "absolute_error"),
        ("sklearn.HistGradientBoostingRegressor", "gamma"),
        ("sklearn.HistGradientBoostingRegressor", "poisson"),
    ],
)
def test_every_loss_round_trips_under_the_allow_list(kind: str, loss: str) -> None:
    entry = get_entry(kind)
    X, y = _data("regression")
    y = np.abs(y) + 0.1
    est = build_estimator(resolve_choice({"kind": kind, "params": {"loss": loss}}), seed=0)
    est.fit(X, y)
    back = load_estimator(entry, dump_estimator(entry, est))
    np.testing.assert_allclose(back.predict(X), est.predict(X))


def test_native_categorical_columns() -> None:
    X, y = _data("regression")
    X[0, 2] = np.nan
    hist = build_estimator(
        resolve_choice({"kind": "sklearn.HistGradientBoostingRegressor"}),
        seed=0,
        categorical=[2],
        n_features=3,
    )
    assert hist.get_params()["categorical_features"] == [2]
    hist.fit(X, y)
    back = load_estimator(
        get_entry(hist_kind := "sklearn.HistGradientBoostingRegressor"),
        dump_estimator(get_entry(hist_kind), hist),
    )
    np.testing.assert_allclose(back.predict(X), hist.predict(X))
    booster = build_estimator(
        resolve_choice({"kind": "xgboost.XGBRegressor", "params": {"n_estimators": 5}}),
        seed=0,
        categorical=[2],
        n_features=3,
    )
    assert booster.get_params()["feature_types"] == ["q", "q", "c"]
    booster.fit(X, y)
    xb = load_estimator(
        get_entry("xgboost.XGBRegressor"),
        dump_estimator(get_entry("xgboost.XGBRegressor"), booster),
    )
    np.testing.assert_allclose(xb.predict(X), booster.predict(X))
    with pytest.raises(ValueError, match="n_features"):
        build_estimator(resolve_choice({"kind": "xgboost.XGBRegressor"}), seed=0, categorical=[0])
    with pytest.raises(ValueError, match="no native categorical"):
        build_estimator(resolve_choice({"kind": "sklearn.Ridge"}), seed=0, categorical=[0])


def test_platform_owned_parameters() -> None:
    rf = build_estimator(
        resolve_choice({"kind": "sklearn.RandomForestRegressor"}), seed=2**40 + 5, n_jobs=3
    )
    assert rf.random_state == (2**40 + 5) % 2**32 and rf.n_jobs == 3
    lin = build_estimator(resolve_choice({"kind": "sklearn.LinearRegression"}), seed=1)
    assert "random_state" not in lin.get_params()
    booster = build_estimator(resolve_choice({"kind": "xgboost.XGBRegressor"}), seed=4)
    assert booster.get_params()["verbosity"] == 0 and booster.random_state == 4
    assert "tweedie_variance_power" not in booster.get_params() or (
        booster.get_params()["tweedie_variance_power"] is None
    )


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"kind": "sklearn.SVR"}, "not allow-listed"),
        ({"kind": "sklearn.Ridge", "params": {"alpha": -1}}, "invalid params"),
        ({"kind": "sklearn.Ridge", "params": {"copy_X": False}}, "invalid params"),
        ({"kind": "xgboost.XGBRegressor", "params": {"max_depth": 1000}}, "invalid params"),
    ],
)
def test_invalid_choices(raw: dict[str, Any], fragment: str) -> None:
    with pytest.raises(MapiError) as err:
        resolve_choice(raw)
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    assert fragment in err.value.detail


def test_choice_from_instances() -> None:
    choice = resolve_choice(Ridge(alpha=3.0, random_state=9))
    assert choice.kind == "sklearn.Ridge" and choice.params.alpha == 3.0  # type: ignore[union-attr]
    xchoice = resolve_choice(xgb.XGBRegressor(n_estimators=12, max_depth=2))
    assert xchoice.params.n_estimators == 12  # type: ignore[union-attr]
    assert resolve_choice(choice) is choice
    for bad in (SVR(), Ridge(copy_X=False)):
        with pytest.raises(MapiError) as err:
            choice_from_instance(bad)
        assert err.value.code is ErrorCode.SETTINGS_INVALID

    class Sneaky(LinearRegression):
        pass

    with pytest.raises(MapiError, match="not allow-listed"):
        resolve_choice(Sneaky())
    with pytest.raises(MapiError, match="invalid params"):
        choice_from_instance(Ridge(alpha=-2.0))


def test_settings_json_schema_checks_params_per_kind() -> None:
    """The exported settings schema is a oneOf over kinds, so params validate statically."""
    from jsonschema import Draft202012Validator

    schema = PointSettings.model_json_schema(by_alias=True)
    validator = Draft202012Validator(schema)
    ok = {"rows": ["store"], "time_kind": "none", "estimator": {"kind": "sklearn.Ridge"}}
    assert not list(validator.iter_errors(ok))
    bad = {**ok, "estimator": {"kind": "sklearn.Ridge", "params": {"max_depth": 3}}}
    assert list(validator.iter_errors(bad))
    unknown = {**ok, "estimator": {"kind": "sklearn.SVR"}}
    assert list(validator.iter_errors(unknown))
    with pytest.raises(ValidationError):
        TypeAdapter(PointSettings).validate_python(bad)


def test_persistence_refuses_untrusted_or_malformed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pickle

    import mapi_sklearn.persistence as persistence

    X, y = _data("regression")
    rf = RandomForestRegressor(n_estimators=2, random_state=0).fit(X, y)
    ridge_entry = get_entry("sklearn.Ridge")
    rf_bytes = dump_estimator(get_entry("sklearn.RandomForestRegressor"), rf)
    with pytest.raises(BundleError, match="outside the allow-list"):
        load_estimator(ridge_entry, rf_bytes)
    ridge_bytes = dump_estimator(ridge_entry, Ridge().fit(X, y))
    with pytest.raises(BundleError, match="expected LinearRegression"):
        load_estimator(get_entry("sklearn.LinearRegression"), ridge_bytes)
    with pytest.raises(BundleError, match="pickle"):
        load_estimator(ridge_entry, pickle.dumps(Ridge()))
    with pytest.raises(BundleError, match="not a skops file"):
        load_estimator(ridge_entry, b"PK\x03\x04garbage")
    xgb_entry = get_entry("xgboost.XGBRegressor")
    with pytest.raises(BundleError, match="not xgboost JSON"):
        load_estimator(xgb_entry, b"\xff\xfe not json")
    with pytest.raises(BundleError, match="not an xgboost model"):
        load_estimator(xgb_entry, b'{"hello": 1}')
    with pytest.raises(BundleError, match="could not be loaded"):
        load_estimator(xgb_entry, b'{"learner": {"broken": true}}')
    monkeypatch.setattr(persistence, "MAX_ARTIFACT_BYTES", 10)
    with pytest.raises(BundleError, match="larger than"):
        load_estimator(ridge_entry, ridge_bytes)
