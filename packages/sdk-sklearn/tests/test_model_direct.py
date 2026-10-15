"""Calling the plugins directly (no harness), settings validation and edge cases."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.models import FitContext, Fitted, ScoreContext
from mapi_sklearn import (
    BootstrapSettings,
    ConformalSettings,
    PointSettings,
    QuantileSettings,
    TabularBootstrap,
    TabularQuantiles,
    TabularRegressor,
)
from mapi_spec import load_score_request
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig
from pydantic import ValidationError

REQ = load_score_request(
    "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://t/x\nmode: predict\n"
)


def _ds(seed: int = 0, weeks: int = 12) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    time = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN").as_unit("ns")
    x = rng.normal(size=(3, weeks))
    return xr.Dataset(
        {
            "y": (("store", "time"), 2 * x + rng.normal(0, 0.1, (3, weeks))),
            "x": (("store", "time"), x),
        },
        coords={"store": ["a", "b", "c"], "time": time},
    )


def _fit(plugin: Any, settings: dict[str, Any], ds: xr.Dataset) -> tuple[Fitted, Any]:
    s = plugin.parse_settings(settings)
    out = plugin.fit(ds, s, FitConfig(seed=1), FitContext(seed=1))
    return Fitted(
        manifest=type(plugin).manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=FitConfig(seed=1),
        groups=out.groups,
        learned=out.learned,
        native=out.native,
    ), out


BASE = {"rows": ["store", "time"], "estimator": {"kind": "sklearn.Ridge"}}


def test_direct_fit_and_score_without_a_schema() -> None:
    plugin = TabularRegressor()
    fitted, out = _fit(
        plugin, {**BASE, "coord_features": {"store": "onehot", "time": "numeric"}}, _ds()
    )
    layout = out.learned["layout"]
    assert isinstance(layout, dict) and set(layout["encodings"]) == {"store", "time"}
    new = _ds(1).isel(store=[0]).assign_coords(store=["zz"])
    logs: list[str] = []
    tree = plugin.score(fitted, new.drop_vars("y"), REQ, ScoreContext(log=logs.append))
    pred = tree["predictions"].to_dataset()
    assert bool(np.isfinite(pred["y_hat"]).all())  # settings.new_levels (pooled) applies
    assert pred["y_hat"].attrs["pooled_new_levels"] == "store"
    # a fully masked score set predicts nothing (and says so)
    empty = _ds(2).drop_vars("y").assign(x=lambda d: d["x"] * np.nan)
    tree = plugin.score(fitted, empty, REQ, ScoreContext(log=logs.append))
    y_hat = tree["predictions"].to_dataset()["y_hat"]
    assert bool(y_hat.isnull().all()) and any("not predicted" in m for m in logs)
    assert bool(tree["predictions"].to_dataset()["contributions"].isnull().all())


def test_quantiles_on_an_empty_score_set() -> None:
    plugin = TabularQuantiles()
    fitted, _ = _fit(
        plugin,
        {
            **BASE,
            "estimator": {"kind": "xgboost.XGBRegressor", "params": {"n_estimators": 5}},
            "uncertainty": {"kind": "quantile", "quantiles": [0.2, 0.8]},
        },
        _ds(),
    )
    empty = _ds(3).drop_vars("y").assign(x=lambda d: d["x"] * np.inf)
    tree = plugin.score(fitted, empty, REQ, ScoreContext())
    y_hat = tree["predictions"].to_dataset()["y_hat"]
    assert y_hat.dims == ("quantile", "store", "time") and bool(y_hat.isnull().all())


def test_estimator_warnings_become_diagnostics() -> None:
    plugin = TabularRegressor()
    lasso = {"kind": "sklearn.Lasso", "params": {"alpha": 1e-6, "max_iter": 1, "tol": 1e-12}}
    many = {**BASE, "estimator": lasso, "coord_features": {"store": "onehot", "time": "numeric"}}
    _, out = _fit(plugin, many, _ds())
    codes = [d.code for d in out.diagnostics]
    assert "ESTIMATOR_WARNING" in codes
    assert any("ConvergenceWarning" in d.message_expert for d in out.diagnostics)


def test_invalid_parameter_combinations_are_settings_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sklearn.linear_model import Ridge
    from sklearn.utils._param_validation import InvalidParameterError

    def boom(self: Any, *a: Any, **k: Any) -> None:
        raise InvalidParameterError("bad combination")

    monkeypatch.setattr(Ridge, "fit", boom)
    with pytest.raises(MapiError) as err:
        _fit(TabularRegressor(), BASE, _ds())
    assert (
        err.value.code is ErrorCode.SETTINGS_INVALID
        and err.value.untrusted_text == "bad combination"
    )


def test_too_few_rows_and_residual_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    ds = _ds()
    with pytest.raises(MapiError) as err:
        _fit(TabularRegressor(), BASE, ds.assign(y=ds["y"] * np.nan))
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    import mapi_sklearn.model as model

    monkeypatch.setattr(model, "MAX_RESIDUALS", 5)
    _, out = _fit(TabularBootstrap(), {**BASE, "uncertainty": {"kind": "bootstrap", "n": 3}}, ds)
    assert out.groups["bootstrap_residuals"].sizes["residual"] == 5


def test_weighted_bootstrap_and_conformal_fits() -> None:
    from mapi_sklearn import TabularConformal

    ds = _ds(weeks=30).assign(w=lambda d: xr.ones_like(d["y"]))
    _, out = _fit(
        TabularBootstrap(),
        {**BASE, "weight": "w", "uncertainty": {"kind": "bootstrap", "n": 3}},
        ds,
    )
    assert len(out.native) == 3
    _, cout = _fit(
        TabularConformal(),
        {**BASE, "weight": "w", "uncertainty": {"kind": "conformal", "alpha": 0.25, "refit": True}},
        ds,
    )
    assert cout.learned["conformal"]["refit"] is True


@pytest.mark.parametrize(
    ("cls", "settings", "fragment"),
    [
        (PointSettings, {"rows": ["a", "a"], "time_kind": "none"}, "repeat"),
        (
            PointSettings,
            {"rows": ["a"], "time_kind": "none", "coord_features": {"b": "onehot"}},
            "row dims",
        ),
        (PointSettings, {"rows": ["a"], "time_kind": "none", "weight": "y"}, "target"),
        (
            PointSettings,
            {"rows": ["a"], "time_kind": "none", "columns": {"roles": ["target"]}},
            "target role",
        ),
        (
            QuantileSettings,
            {"rows": ["a"], "time_kind": "none", "uncertainty": {"quantiles": [0.5, 1.0]}},
            "strictly",
        ),
        (
            QuantileSettings,
            {"rows": ["a"], "time_kind": "none", "uncertainty": {"quantiles": [0.5, 0.5]}},
            "unique",
        ),
        (
            BootstrapSettings,
            {"rows": ["a"], "time_kind": "none", "uncertainty": {"by": "b"}},
            "must be one of rows",
        ),
        (ConformalSettings, {"rows": ["a"], "time_kind": "none"}, "time_dim"),
        (PointSettings, {"rows": ["a"]}, "must be one of rows"),
    ],
)
def test_settings_validation(cls: Any, settings: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        cls.model_validate(settings)


def test_settings_defaults() -> None:
    s = QuantileSettings.model_validate(
        {"rows": ["store", "time"], "uncertainty": {"quantiles": [0.9, 0.1]}}
    )
    assert s.uncertainty.quantiles == [0.1, 0.9] and s.time_row == "time"
    assert PointSettings.model_validate({"rows": ["a"], "time_kind": "none"}).time_row is None
    with pytest.raises(MapiError) as err:
        plugin: Any = TabularRegressor()
        plugin.fit(
            _ds(),
            PointSettings.model_validate({"rows": ["store", "time"]}),
            FitConfig(),
            FitContext(),
        )
    assert err.value.code is ErrorCode.SETTINGS_INVALID and "settings.estimator" in err.value.detail
