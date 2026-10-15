"""End to end on the synthetic store × sku × week demand panel: contract kit, every
representation, new store levels, direct forecasting, contributions and persistence."""

# ruff: noqa: E501

from __future__ import annotations

import json
import pickle
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.bundle import BUNDLE_FILE, Bundle
from mapi_sdk.errors import BundleError
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sdk.wire import zip_entries
from mapi_sklearn import (
    PLUGINS,
    PointSettings,
    SklearnModel,
    TabularBootstrap,
    TabularConformal,
    TabularQuantiles,
    TabularRegressor,
    capabilities_for,
)
from mapi_sklearn.testing import demand_dataset, demand_registry, demand_schema, demand_spec
from mapi_spec import DataSchema, ModelSpec, load_model_spec, load_score_request
from mapi_spec.capabilities import Capabilities
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import Role
from mapi_spec.validate import validate_spec
from sklearn.linear_model import Ridge
from sklearn.svm import SVR

QUANTILES = {"kind": "quantile", "quantiles": [0.05, 0.1, 0.5, 0.9, 0.95]}
RIDGE = {"kind": "sklearn.Ridge", "params": {"alpha": 1.0}}
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/store-demand\n"


def req(extra: str = "mode: predict\n") -> Any:
    return load_score_request(REQ + extra)


def full(tree: xr.DataTree, group: str = "predictions") -> xr.Dataset:
    node = tree[group]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


@pytest.fixture(scope="module")
def data() -> xr.Dataset:
    return demand_dataset()


@pytest.fixture(scope="module")
def raw_schema(data: xr.Dataset) -> DataSchema:
    return demand_schema(data)


@pytest.fixture(scope="module")
def reg() -> Registry:
    return demand_registry()


@pytest.fixture(scope="module")
def fit(data: xr.Dataset, raw_schema: DataSchema, reg: Registry) -> Callable[..., Bundle]:
    cache: dict[str, Bundle] = {}

    def _fit(model: str, settings: dict[str, Any] | None = None, **kw: Any) -> Bundle:
        key = json.dumps([model, settings, kw], sort_keys=True)
        if key not in cache:
            spec = demand_spec(f"{model}@^1", settings, **kw)
            cache[key] = fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
        return cache[key]

    return _fit


def _holdout(data: xr.Dataset, weeks: int = 8) -> xr.Dataset:
    return data.isel(week=slice(-weeks, None))


def _future(data: xr.Dataset, weeks: int = 4) -> xr.Dataset:
    """Future covariates (price, promo) for the weeks right after the data."""
    last = data["week"].values[-1]
    future = np.array([last + np.timedelta64(7 * k, "D") for k in range(1, weeks + 1)])
    tail = data.drop_vars("units_sold").isel(week=slice(-weeks, None))
    return tail.assign_coords(week=future)


# ------------------------------------------------------------------------------ contract kit

KIT_CASES: list[tuple[type[Any], dict[str, Any]]] = [
    (TabularRegressor, {}),
    (TabularQuantiles, {"uncertainty": QUANTILES}),
    (TabularBootstrap, {"uncertainty": {"kind": "bootstrap", "n": 8}}),
    (TabularConformal, {"uncertainty": {"kind": "conformal", "alpha": 0.2}}),
]


@pytest.mark.mapi_contract
@pytest.mark.parametrize(("plugin", "settings"), KIT_CASES, ids=[c[0].id for c in KIT_CASES])
def test_contract_kit_passes(
    plugin: type[Any],
    settings: dict[str, Any],
    data: xr.Dataset,
    raw_schema: DataSchema,
    reg: Registry,
) -> None:
    fixture = ModelFixture(
        spec=demand_spec(f"{plugin.id}@^1", settings),
        data=data,
        raw_schema=raw_schema,
        registry=reg,
    )
    report = check_model(plugin, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "extend_typed_errors",
        "unsupported_mode",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "representation_aggregation",
        "seed_determinism",
    } <= ran
    assert "store: new level scored (pooled)" in report.get("new_level_policies").detail
    assert "sku: raised UNSEEN_LEVELS" in report.get("new_level_policies").detail


class LinearDemand(SklearnModel[PointSettings]):
    """An author-written plugin: only ``estimator()`` is implemented."""

    id = "t.acme.linear_demand"
    version = "1.0.0"
    capabilities: ClassVar[Capabilities] = capabilities_for("none").model_copy(
        update={"contribution_scale": "additive"}
    )

    def estimator(self, s: PointSettings) -> Any:
        return Ridge(alpha=0.5)


@pytest.mark.mapi_contract
def test_author_plugin_passes_the_kit_with_additive_contributions(
    data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    fixture = ModelFixture(
        spec=demand_spec("t.acme.linear_demand@^1", {"estimator": None}),
        data=data,
        raw_schema=raw_schema,
        registry=reg,
    )
    report = check_model(LinearDemand, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    assert not report.get("additive_contributions").skipped
    assert "contributions sum to 'y_hat'" in report.get("additive_contributions").detail


def test_plugins_are_discovered_through_entry_points() -> None:
    from mapi_sdk.registry import Registry as Reg

    reg = Reg()
    errors = reg.load_entry_points(groups=("mapi.models",))
    assert not errors
    ids = {m.id for m in reg.models()}
    assert {p.id for p in PLUGINS} <= ids
    catalog = reg.catalog()
    entry = catalog.resolve_model("sklearn.tabular_quantiles@^1")
    assert entry is not None
    assert entry.manifest.capabilities.representation.value == "quantiles"
    assert not entry.manifest.capabilities.loo_valid


# ---------------------------------------------------------------------------- quantiles


def test_xgboost_native_quantiles(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit(
        "sklearn.tabular_quantiles", {"uncertainty": QUANTILES}, metrics="rmse, mape, coverage_80"
    )
    assert list(b.model.native) == ["quantile/0000.xgb.json"]
    assert b.model.learned["quantile_mode"] == "native_multi"
    assert {d.code for d in b.diagnostics} >= {"QUANTILE_CROSSING"}
    coverage = b.metrics.values["coverage_80"]
    assert coverage is not None and 0.5 <= coverage <= 1.0
    ev = b.evaluation
    assert ev is not None
    out = score_bundle(ev, req(), _holdout(data), registry=reg, return_full=True)
    y_hat = full(out)["y_hat"]
    assert y_hat.dims == ("quantile", "store", "sku", "week")
    assert y_hat.attrs["quantile_method"] == "native_multi" and y_hat.attrs["rearranged"] == "true"
    assert bool((y_hat.diff("quantile") >= 0).all())
    observed = _holdout(data)["units_sold"]
    inside = (observed >= y_hat.sel(quantile=0.1)) & (observed <= y_hat.sel(quantile=0.9))
    assert 0.5 <= float(inside.mean()) <= 1.0
    summary = full(out, "summary")
    assert list(summary["stat"].values) == ["median", "q5", "q95"]
    with pytest.raises(MapiError) as err:
        score_bundle(b, req("output: {aggregate: {over: [store]}}\n"), _holdout(data), registry=reg)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    with pytest.raises(MapiError) as err:
        score_bundle(b, req("output: {draws: 10}\n"), _holdout(data), registry=reg)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


@pytest.mark.parametrize(
    "estimator",
    [
        {"kind": "sklearn.HistGradientBoostingRegressor", "params": {"max_iter": 30}},
        {"kind": "sklearn.GradientBoostingRegressor", "params": {"n_estimators": 20}},
        {"kind": "sklearn.QuantileRegressor", "params": {"alpha": 0.0}},
    ],
    ids=lambda e: e["kind"],
)
def test_per_level_quantile_models(
    estimator: dict[str, Any], data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    spec = demand_spec(
        "sklearn.tabular_quantiles@^1",
        {"uncertainty": {"kind": "quantile", "quantiles": [0.1, 0.5, 0.9]}},
        estimator=estimator,
        mode="train_only",
        metrics="rmse",
    )
    b = fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert len(b.model.native) == 3 and b.model.learned["quantile_mode"] == "per_level"
    out = score_bundle(
        b, req("output: {summary: [median]}\n"), _holdout(data), registry=reg, return_full=True
    )
    y_hat = full(out)["y_hat"]
    assert bool((y_hat.diff("quantile") >= 0).all()) and bool(np.isfinite(y_hat).all())


def test_quantiles_need_a_quantile_capable_estimator(
    data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    spec = demand_spec("sklearn.tabular_quantiles@^1", {"uncertainty": QUANTILES}, estimator=RIDGE)
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    assert "xgboost.XGBRegressor" in err.value.detail


# ----------------------------------------------------------------------------- conformal


@pytest.mark.parametrize(
    "unc",
    [
        {"kind": "conformal", "alpha": 0.2},
        {"kind": "conformal", "alpha": 0.2, "calibration_by": "random", "refit": True},
    ],
    ids=["last", "random-refit"],
)
def test_split_conformal_intervals(
    unc: dict[str, Any], fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit("sklearn.tabular_conformal", {"uncertainty": unc}, metrics="rmse, coverage_80")
    info: Any = b.model.learned["conformal"]
    assert info["n_calibration"] > 0 and info["refit"] is unc.get("refit", False)
    assert "coverage_80" in b.metrics.values
    ev = b.evaluation
    assert ev is not None
    out = score_bundle(ev, req(), _holdout(data), registry=reg, return_full=True)
    y_hat = full(out)["y_hat"]
    assert list(y_hat["bound"].values) == ["lower", "upper", "point"]
    assert y_hat.attrs["alpha"] == 0.2 and y_hat.attrs["interval"] == "split_conformal_prediction"
    lo, hi, pt = (y_hat.sel(bound=k) for k in ("lower", "upper", "point"))
    assert bool((lo <= pt).all()) and bool((pt <= hi).all())
    observed = _holdout(data)["units_sold"]
    coverage = float(((observed >= lo) & (observed <= hi)).mean())
    assert coverage >= 0.6
    assert list(full(out, "summary")["stat"].values) == [
        "point",
        "interval_lower",
        "interval_upper",
    ]


def test_conformal_calibration_too_small(
    data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    spec = demand_spec(
        "sklearn.tabular_conformal@^1",
        {"uncertainty": {"kind": "conformal", "alpha": 0.001, "calibration": 0.05}},
        mode="train_only",
    )
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID and "calibration" in err.value.detail


# ----------------------------------------------------------------------------- bootstrap


def test_bootstrap_draws_are_joint(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit(
        "sklearn.tabular_bootstrap",
        {"uncertainty": {"kind": "bootstrap", "n": 12, "by": "store"}},
        estimator=RIDGE,
    )
    assert len(b.model.native) == 12 and all(k.endswith(".skops") for k in b.model.native)
    assert b.thin is not None and b.model.posterior.sizes == {"chain": 1, "draw": 12}
    hold = _holdout(data)
    out = score_bundle(b, req("output: {draws: 5}\n"), hold, registry=reg, return_full=True)
    draws = full(out, "draws")["y_hat"]
    assert draws.sizes["sample"] == 5
    expected = full(out)["y_hat"]
    predictive = full(
        score_bundle(b, req("output: {kind: predictive}\n"), hold, registry=reg, return_full=True)
    )["y_hat"]
    assert float(predictive.std(["chain", "draw"]).mean()) > float(
        expected.std(["chain", "draw"]).mean()
    )
    total = score_bundle(b, req("output: {aggregate: {over: [store]}}\n"), hold, registry=reg)
    assert "store" not in full(total, "summary").dims
    cf = score_bundle(
        b,
        req(
            "mode: counterfactual\ninterventions: [{var: price, multiply: 1.25}]\noutput: {units: model}\n"
        ),
        hold,
        registry=reg,
        return_full=True,
    )
    delta = full(cf, "delta/predictions")["y_hat"]
    assert float(delta.mean()) < 0  # higher prices sell fewer units


# ------------------------------------------------------------------------ new store levels


def _new_store(data: xr.Dataset, source: str = "s0", name: str = "s_new") -> xr.Dataset:
    window = _holdout(data)
    new = window.sel(store=[source]).assign_coords(store=[name])
    return xr.concat(
        [window, new], dim="store", data_vars="minimal", coords="minimal", compat="override"
    )


def test_new_store_is_pooled_over_fitted_stores(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    """A new store's prediction is the training-frequency-weighted average over stores."""
    b = fit("sklearn.tabular_regressor", mode="train_only")
    mixed = _new_store(data)
    out = score_bundle(b, req("output: {units: model}\n"), mixed, registry=reg, return_full=True)
    y_hat = full(out)["y_hat"]
    new = y_hat.sel(store="s_new")
    assert bool(np.isfinite(new).all()) and y_hat.attrs["pooled_new_levels"] == "store"
    # every fitted store with s0's features, then the (equal-count) average
    stores = list(data["store"].values)
    clones = xr.concat(
        [_holdout(data).sel(store=["s0"]).assign_coords(store=[s]) for s in stores],
        dim="store",
    )
    each = full(
        score_bundle(b, req("output: {units: model}\n"), clones, registry=reg, return_full=True)
    )["y_hat"]
    # the first weeks differ: lag features read each store's own stored context tail
    late = {"week": slice(2, None)}
    xr.testing.assert_allclose(new.drop_vars("store").isel(late), each.mean("store").isel(late))


def test_new_store_zero_and_error_strategies(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    zero = fit("sklearn.tabular_regressor", {"new_levels": "zero"}, mode="train_only")
    out = score_bundle(zero, req(), _new_store(data), registry=reg, return_full=True)
    assert bool(np.isfinite(full(out)["y_hat"].sel(store="s_new")).all())
    strict = fit("sklearn.tabular_regressor", {"new_levels": "error"}, mode="train_only")
    with pytest.raises(MapiError) as err:
        score_bundle(strict, req(), _new_store(data), registry=reg)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


# ------------------------------------------------------------------------------ forecasting


def test_direct_forecast_needs_future_features(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit("sklearn.tabular_quantiles", {"uncertainty": QUANTILES})
    horizon = "mode: forecast\nhorizon: {dim: time, periods: 4}\n"
    out = score_bundle(b, req(horizon), _future(data), registry=reg, return_full=True)
    y_hat = full(out)["y_hat"]
    assert y_hat.sizes["week"] == 4 and bool(np.isfinite(y_hat).all())
    with pytest.raises(MapiError) as err:
        score_bundle(b, req(horizon), None, registry=reg)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_recursive_forecasting_is_rejected(
    data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    spec = demand_spec("sklearn.tabular_regressor@^1", {"forecast_strategy": "recursive"})
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.RECURSIVE_UNSUPPORTED
    with pytest.raises(MapiError) as err:
        TabularRegressor().parse_settings(
            PointSettings.model_validate(
                {"rows": ["a"], "time_kind": "none", "forecast_strategy": "recursive"}
            )
        )
    assert err.value.code is ErrorCode.RECURSIVE_UNSUPPORTED


# ---------------------------------------------------------------------------- contributions


def test_exact_linear_contributions(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit("sklearn.tabular_regressor", estimator=RIDGE, mode="train_only")
    assert set(b.model.groups) >= {"coefficients"}
    out = score_bundle(
        b, req("output: {units: model}\n"), _holdout(data), registry=reg, return_full=True
    )
    pred = full(out)
    contrib = pred["contributions"]
    assert contrib.attrs["method"] == "exact_linear"
    names = list(contrib["component"].values)
    assert names[0] == "intercept" and {"price", "promo_lag[1]", "store", "sku"} <= set(names)
    xr.testing.assert_allclose(
        contrib.sum("component"), pred["y_hat"].transpose(*contrib.sum("component").dims)
    )
    only = score_bundle(
        b,
        req("output: {units: model, vars: [y_hat]}\n"),
        _holdout(data),
        registry=reg,
        return_full=True,
    )
    assert "contributions" not in full(only)


def test_zeroing_contributions_for_trees_and_bootstrap(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit("sklearn.tabular_regressor", {"contributions": "zeroing"}, mode="train_only")
    assert "feature_importance" in b.model.groups
    pred = full(
        score_bundle(
            b, req("output: {units: model}\n"), _holdout(data), registry=reg, return_full=True
        )
    )
    contrib = pred["contributions"]
    assert contrib.attrs["method"] == "counterfactual_zeroing"
    names = list(contrib["component"].values)
    assert names[0] == "baseline" and names[-1] == "interaction"
    total = contrib.sum("component")
    xr.testing.assert_allclose(total, pred["y_hat"].transpose(*total.dims), rtol=1e-5)
    boot = fit(
        "sklearn.tabular_bootstrap",
        {"uncertainty": {"kind": "bootstrap", "n": 4}},
        estimator=RIDGE,
        mode="train_only",
    )
    bp = full(
        score_bundle(
            boot, req("output: {units: model}\n"), _holdout(data), registry=reg, return_full=True
        )
    )
    assert bp["contributions"].dims[:3] == ("chain", "draw", "component")
    btotal = bp["contributions"].sum("component")
    xr.testing.assert_allclose(btotal, bp["y_hat"].transpose(*btotal.dims))


def test_exact_contributions_need_a_linear_estimator(
    data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    spec = demand_spec(
        "sklearn.tabular_regressor@^1", {"contributions": "exact"}, mode="train_only"
    )
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID


# ------------------------------------------------------------------------------ persistence


@pytest.mark.parametrize(
    ("model", "settings", "estimator", "suffix"),
    [
        ("sklearn.tabular_regressor", {}, RIDGE, ".skops"),
        ("sklearn.tabular_quantiles", {"uncertainty": QUANTILES}, None, ".xgb.json"),
        (
            "sklearn.tabular_bootstrap",
            {"uncertainty": {"kind": "bootstrap", "n": 3}},
            None,
            ".xgb.json",
        ),
    ],
)
def test_bundle_persistence_round_trip_without_pickle(
    model: str,
    settings: dict[str, Any],
    estimator: dict[str, Any] | None,
    suffix: str,
    fit: Callable[..., Bundle],
    data: xr.Dataset,
    reg: Registry,
    tmp_path: Path,
) -> None:
    b = fit(model, settings, estimator=estimator, mode="train_only")
    b.write(tmp_path)
    names = zip_entries(tmp_path / BUNDLE_FILE)
    assert not [n for n in names if n.lower().endswith((".pkl", ".pickle", ".joblib"))]
    back = Bundle.read(tmp_path, reg)
    assert sorted(back.model.native) == sorted(b.model.native)
    assert all(k.endswith(suffix) for k in back.model.native)
    for raw in back.model.native.values():
        assert raw[:1] != b"\x80"  # no pickle protocol header
    hold = _holdout(data)
    xr.testing.assert_allclose(
        full(score_bundle(b, req(), hold, registry=reg), "summary"),
        full(score_bundle(back, req(), hold, registry=reg), "summary"),
    )
    key = next(iter(back.model.native))
    back.model.native[key] = pickle.dumps(Ridge())
    with pytest.raises(BundleError):
        score_bundle(back, req(), hold, registry=reg)


def test_missing_native_artifact_is_an_integrity_failure(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    b = fit("sklearn.tabular_regressor", estimator=RIDGE, mode="train_only")
    native = dict(b.model.native)
    b.model.native.clear()
    try:
        with pytest.raises(MapiError) as err:
            score_bundle(b, req(), _holdout(data), registry=reg)
        assert err.value.code is ErrorCode.INTEGRITY_FAILURE
    finally:
        b.model.native.update(native)


# ---------------------------------------------------------------------------- retransform


def test_smearing_retransformation_for_log_targets(
    fit: Callable[..., Bundle], data: xr.Dataset, reg: Registry
) -> None:
    median = fit("sklearn.tabular_regressor", estimator=RIDGE, mode="train_only")
    smear = fit(
        "sklearn.tabular_regressor", {"retransform": "smearing"}, estimator=RIDGE, mode="train_only"
    )
    assert smear.residuals and not median.residuals
    hold = _holdout(data)
    a = full(score_bundle(median, req(), hold, registry=reg, return_full=True))["y_hat"]
    s = full(score_bundle(smear, req(), hold, registry=reg, return_full=True))["y_hat"]
    assert a.attrs["retransform"] == "median" and s.attrs["retransform"] == "smearing"
    assert float((s - a).mean()) > 0  # E[exp(e)] > exp(E[e]) for the log1p target


# ------------------------------------------------------------------------- weights / masks


def test_weights_and_masks(data: xr.Dataset, reg: Registry) -> None:
    mask = xr.ones_like(data["units_sold"], dtype=bool)
    mask[0, 0, :5] = False
    ds = data.assign(weight=xr.ones_like(data["units_sold"]) * 2.0, valid=mask)
    schema = demand_schema(ds)
    vars_ = {**schema.vars}
    vars_["weight"] = vars_["weight"].model_copy(update={"role": Role.WEIGHT})
    vars_["valid"] = vars_["valid"].model_copy(update={"role": Role.MASK})
    schema = schema.model_copy(update={"vars": vars_})
    spec = demand_spec(
        "sklearn.tabular_regressor@^1",
        {"weight": "weight", "mask": "valid"},
        estimator=RIDGE,
        mode="train_only",
    )
    text = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    text["data"]["bind"]["vars"].update({"weight": "weight", "valid": "valid"})
    b = fit_spec(ModelSpec.model_validate(text), ds, registry=reg, raw_schema=schema)
    dropped = b.model.learned["dropped"]
    assert isinstance(dropped, dict) and dropped["masked"] == 5
    assert "ROWS_DROPPED" in {d.code for d in b.diagnostics}
    names = b.model.groups["coefficients"]["feature"].values.tolist()
    assert "weight" not in names and "valid" not in names


# ------------------------------------------------------------------------------ classifier

CLASSIFIER_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: stockouts, project: retail}
data:
  dataset: dataset://retail/stockouts@latest
  bind:
    dims: {time: week}
    vars: {y: sold_out, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 8}
pipeline:
  - {id: plags, use: toy.lag@^1, with: {vars: [promo], along: time, within: [store, sku], lags: [1], fill: 0.0}}
model:
  use: sklearn.tabular_classifier@^1
  settings: __SETTINGS__
  fit: {seed: 3, mode: evaluate_then_refit}
evaluate: {metrics: [rmse]}
"""


def _classifier(
    estimator: dict[str, Any], extra: dict[str, Any] | None = None
) -> tuple[ModelSpec, xr.Dataset, DataSchema]:
    data = demand_dataset()
    ds = data.assign(sold_out=(data["units_sold"] > data["units_sold"].median()).astype(float))
    ds = ds.drop_vars("units_sold")
    schema = demand_schema(ds.assign(units_sold=ds["sold_out"])).model_copy()
    from mapi_sdk.schema import infer_schema

    schema = infer_schema(ds, targets=["sold_out"], policies={"store": "new_levels"})
    settings = {
        "estimator": estimator,
        "rows": ["store", "sku", "time"],
        "coord_features": {"store": "onehot", "sku": "onehot"},
        **(extra or {}),
    }
    spec = load_model_spec(CLASSIFIER_SPEC.replace("__SETTINGS__", json.dumps(settings)))
    return spec, ds, schema


@pytest.mark.parametrize(
    "estimator",
    [
        {"kind": "sklearn.LogisticRegression", "params": {"C": 0.5}},
        {"kind": "xgboost.XGBClassifier", "params": {"n_estimators": 20, "max_depth": 2}},
        {"kind": "sklearn.RandomForestClassifier", "params": {"n_estimators": 10, "max_depth": 3}},
    ],
    ids=lambda e: e["kind"],
)
def test_binary_classifier(estimator: dict[str, Any], reg: Registry) -> None:
    spec, ds, schema = _classifier(estimator, {"contributions": "zeroing"})
    b = fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert b.model.learned["classes"] == [0.0, 1.0]
    out = score_bundle(
        b,
        req("output: {units: model}\n"),
        ds.isel(week=slice(-6, None)),
        registry=reg,
        return_full=True,
    )
    pred = full(out)
    p = pred["y_hat"]
    assert bool(((p >= 0) & (p <= 1)).all()) and p.attrs["meaning"] == "probability"
    assert p.attrs["positive_class"] == 1.0
    total = pred["contributions"].sum("component")
    xr.testing.assert_allclose(total, p.transpose(*total.dims), rtol=1e-5)


@pytest.mark.mapi_contract
def test_classifier_passes_the_kit(reg: Registry) -> None:
    from mapi_sklearn import TabularClassifier

    spec, ds, schema = _classifier({"kind": "sklearn.LogisticRegression"})
    report = check_model(
        TabularClassifier,
        ModelFixture(spec=spec, data=ds, raw_schema=schema, registry=reg),
        raise_on_failure=False,
    )
    assert report.ok, report.summary()


def test_classifier_errors(reg: Registry) -> None:
    spec, ds, schema = _classifier({"kind": "sklearn.Ridge"})
    with pytest.raises(MapiError) as err:
        fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID and "classification" in err.value.detail
    spec, ds, schema = _classifier({"kind": "sklearn.LogisticRegression"}, {"positive_class": 7})
    with pytest.raises(MapiError) as err:
        fit_spec(spec, ds, registry=reg, raw_schema=schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    spec, ds, schema = _classifier({"kind": "sklearn.LogisticRegression"})
    three = ds.assign(sold_out=ds["sold_out"] + (ds["price"] > 5))
    with pytest.raises(MapiError) as err:
        fit_spec(spec, three, registry=reg, raw_schema=schema)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_regressor_rejects_classifier_estimators(
    data: xr.Dataset, raw_schema: DataSchema, reg: Registry
) -> None:
    spec = demand_spec("sklearn.tabular_regressor@^1", estimator={"kind": "xgboost.XGBClassifier"})
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.SETTINGS_INVALID


# -------------------------------------------------------------------------- static checks


def test_settings_are_validated_statically(raw_schema: DataSchema, reg: Registry) -> None:
    catalog = reg.catalog()
    ok = validate_spec(
        demand_spec("sklearn.tabular_quantiles@^1", {"uncertainty": QUANTILES}), catalog, raw_schema
    )
    assert ok.ok, ok.issues
    model_schema = ok.resolved.model.input_schema if ok.resolved else None
    assert model_schema is not None
    assert model_schema.dims["store"].policy.value == "new_levels"
    assert model_schema.dims["sku"].policy.value == "fixed"  # tightened by the data
    assert model_schema.dims["time"].policy.value == "extend"
    bad = demand_spec(
        "sklearn.tabular_quantiles@^1",
        {"uncertainty": QUANTILES},
        estimator={"kind": "xgboost.XGBRegressor", "params": {"max_depth": -1}},
    )
    issues = validate_spec(bad, catalog, raw_schema).errors
    assert any(i.code is ErrorCode.SETTINGS_INVALID for i in issues)
    wrong_kind = demand_spec("sklearn.tabular_regressor@^1", {"uncertainty": QUANTILES})
    assert any(
        i.code is ErrorCode.SETTINGS_INVALID
        for i in validate_spec(wrong_kind, catalog, raw_schema).errors
    )
    no_estimator = demand_spec("sklearn.tabular_regressor@^1", {"estimator": None})
    assert not validate_spec(no_estimator, catalog, raw_schema).ok
    bad_rows = demand_spec("sklearn.tabular_regressor@^1", {"rows": ["store", "sku"]})
    errors = validate_spec(bad_rows, catalog, raw_schema).errors
    assert any(i.code is ErrorCode.SCHEMA_INVALID for i in errors)


def test_time_dim_must_be_a_row(data: xr.Dataset, raw_schema: DataSchema, reg: Registry) -> None:
    """Statically the template then declares 'time' categorical; at runtime settings fail."""
    spec = demand_spec("sklearn.tabular_regressor@^1", {"time_dim": "week"}, mode="train_only")
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=reg, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "categorical" in err.value.detail
    with pytest.raises(MapiError) as err:
        TabularRegressor().parse_settings(
            {"estimator": RIDGE, "rows": ["store", "time"], "time_dim": "week"}
        )
    assert err.value.code is ErrorCode.SETTINGS_INVALID


# ---------------------------------------------------------------------------- author hook


class _HookModel(SklearnModel[PointSettings]):
    id = "t.acme.hook"
    version = "1.0.0"
    choice: ClassVar[Any] = None

    def estimator(self, s: PointSettings) -> Any:
        return type(self).choice


@pytest.mark.parametrize(
    ("choice", "outcome"),
    [
        ({"kind": "sklearn.Lasso", "params": {"alpha": 0.01}}, "ok"),
        (SVR(), ErrorCode.SETTINGS_INVALID),
        (None, ErrorCode.SETTINGS_INVALID),
    ],
    ids=["mapping", "not-allow-listed", "settings-fallback-missing"],
)
def test_author_estimator_hook(
    choice: Any,
    outcome: Literal["ok"] | ErrorCode,
    data: xr.Dataset,
    raw_schema: DataSchema,
    reg: Registry,
) -> None:
    class Hook(_HookModel):
        pass

    Hook.choice = choice
    if choice is None:
        Hook.estimator = SklearnModel.estimator  # type: ignore[method-assign]
    local = reg.copy()
    local.register_model(Hook, replace=True)
    spec = demand_spec("t.acme.hook@^1", {"estimator": None}, mode="train_only")
    if outcome == "ok":
        b = fit_spec(spec, data, registry=local, raw_schema=raw_schema)
        est: Any = b.model.learned["estimator"]
        assert est["kind"] == "sklearn.Lasso" and est["params"]["alpha"] == 0.01
        return
    with pytest.raises(MapiError) as err:
        fit_spec(spec, data, registry=local, raw_schema=raw_schema)
    assert err.value.code is outcome
