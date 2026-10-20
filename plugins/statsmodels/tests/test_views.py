"""Model Explorer sections built from statsmodels bundles validate as spec views."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_statsmodels import PLUGIN_IDS, PLUGINS
from mapi_plugin_statsmodels.datasets import sales_dataset, sales_schema
from mapi_plugin_statsmodels.examples import SALES_BIND, example_spec
from mapi_plugin_statsmodels.views import explorer_views
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry
from mapi_spec.stattests import StatTestResult
from mapi_spec.views import (
    MODEL_SECTIONS,
    DecompositionView,
    FevdView,
    IrfView,
    ModelDiagnosticsView,
    ParamTableView,
    ResidualDiagnosticsView,
)

BY_ID = {p.id: p for p in PLUGINS}


@pytest.mark.parametrize("model_id", PLUGIN_IDS)
def test_every_fit_fills_the_core_sections(model_id: str, bundles: dict[str, Bundle]) -> None:
    fitted = bundles[model_id].model
    views = BY_ID[model_id]().explorer_views(fitted)
    assert {"params", "diagnostics", "residuals"} <= set(views) <= set(MODEL_SECTIONS)
    for name, view in views.items():
        doc = json.loads(view.model_dump_json())
        type(view).model_validate(doc)  # the JSON round-trips through the spec DTO
        assert doc["format"].startswith("mapi.model-explorer."), name
    params = views["params"]
    assert isinstance(params, ParamTableView) and params.rows
    assert all(r.p_value is None or 0 <= r.p_value <= 1 for r in params.rows)
    resid = views["residuals"]
    assert isinstance(resid, ResidualDiagnosticsView)
    assert {t.test for t in resid.tests} <= {"test.ljung_box", "test.jarque_bera", "test.arch_lm"}
    assert resid.tests and resid.series and resid.qq is not None


def test_panel_rows_carry_their_facet(bundles: dict[str, Bundle]) -> None:
    views = explorer_views(bundles["ts.sarimax"].model)
    params = views["params"]
    assert isinstance(params, ParamTableView)
    assert {r.coords.get("geo") for r in params.rows} == {"north", "south"}
    resid = views["residuals"]
    assert isinstance(resid, ResidualDiagnosticsView)
    lb = next(t for t in resid.tests if t.test == "test.ljung_box")
    assert len(lb.per_series) == 2 and lb.reject_share is not None


def test_var_views_have_irf_fevd_and_granger(bundles: dict[str, Bundle]) -> None:
    fitted = bundles["ts.var"].model
    views = explorer_views(fitted)
    irf, fevd = views["irf"], views["fevd"]
    assert isinstance(irf, IrfView) and isinstance(fevd, FevdView)
    assert irf.variables == ["realgdp", "realcons", "realinv"]
    assert irf.lower is not None and irf.horizons == 12
    stored = fitted.groups["irf"]["irf"].transpose("response", "shock", "horizon").values
    np.testing.assert_allclose(np.asarray(irf.values), stored)
    diag = views["diagnostics"]
    assert isinstance(diag, ModelDiagnosticsView)
    tests = [StatTestResult.model_validate(i) for i in diag.items if i.get("kind") == "test"]
    assert len([t for t in tests if t.test == "test.granger"]) == 6


def test_vecm_diagnostics_include_the_johansen_test(bundles: dict[str, Bundle]) -> None:
    views = explorer_views(bundles["ts.vecm"].model)
    diag = views["diagnostics"]
    assert isinstance(diag, ModelDiagnosticsView)
    johansen = next(i for i in diag.items if i.get("test") == "test.johansen")
    assert johansen["decision"] == "cointegrated" and johansen["details"]["rank"] == 1
    assert "irf" in views and "fevd" in views


def test_ecm_diagnostics_include_the_bounds_test(bundles: dict[str, Bundle]) -> None:
    diag = explorer_views(bundles["ts.ecm"].model)["diagnostics"]
    assert isinstance(diag, ModelDiagnosticsView)
    bounds = next(i for i in diag.items if i.get("kind") == "test.pss_bounds")
    assert bounds["case"] == 3 and bounds["speed_of_adjustment"] < 0


def test_unobserved_components_show_a_decomposition(bundles: dict[str, Bundle]) -> None:
    views = explorer_views(bundles["ts.unobserved_components"].model)
    dec = views["decomposition"]
    assert isinstance(dec, DecompositionView)
    assert "level" in dec.components and dec.warnings_plain  # a panel: first series shown


def _fit(registry: Registry, model: str, settings: str, *, panel: bool = False) -> Bundle:
    geos = ["north", "south"] if panel else None
    data = sales_dataset(periods=70, geos=geos, seed=2)
    spec = example_spec(model, settings, SALES_BIND)
    return fit_spec(spec, data, registry=registry, raw_schema=sales_schema(geo=panel))


def test_unobserved_components_mark_the_slope_as_not_a_part(registry: Registry) -> None:
    settings = "{level: local linear trend, series_dims: [geo]}"
    fitted = _fit(registry, "ts.unobserved_components", settings, panel=True).model
    comps = fitted.groups["components"]  # stacked over geo: the attr survives
    assert {"level", "trend"} <= set(comps.data_vars) and "geo" in comps.dims
    assert comps["trend"].attrs == {"decomposition": "exclude"}
    assert "decomposition" not in comps["level"].attrs
    dec = explorer_views(fitted)["decomposition"]
    assert isinstance(dec, DecompositionView) and {"level", "trend"} <= set(dec.components)


def test_additive_ets_stores_its_components(registry: Registry) -> None:
    fitted = _fit(registry, "ts.ets", "{trend: add, seasonal: add, seasonal_periods: 4}").model
    comps = fitted.groups["components"]
    assert list(comps.data_vars) == ["level", "slope", "season"]
    assert comps["slope"].attrs == {"decomposition": "exclude"}
    assert not comps["level"].attrs and not comps["season"].attrs
    y = fitted.groups["observed_data"]["y"]
    parts = (comps["level"] + comps["season"]).sel(t=y["t"])
    # the stacked parts follow the series: same mean, smaller spread than the noise
    assert np.isfinite(parts.values).all()
    assert abs(float(parts.mean() - y.mean())) < 0.2 * float(y.std())
    assert float((y - parts).std()) <= float(y.std())
    dec = explorer_views(fitted)["decomposition"]
    assert isinstance(dec, DecompositionView)
    assert set(dec.components) == {"level", "slope", "season"}


def test_ets_decomposition_view_and_multiplicative_models(
    registry: Registry, bundles: dict[str, Bundle]
) -> None:
    fitted = bundles["ts.ets"].model  # damped additive trend, no season
    assert list(fitted.groups["components"].data_vars) == ["level", "slope"]
    assert isinstance(explorer_views(fitted)["decomposition"], DecompositionView)
    mul = _fit(registry, "ts.ets", "{error: mul, trend: add}").model
    assert "components" not in mul.groups
    assert "decomposition" not in explorer_views(mul)


def test_forecast_fans_per_series_from_a_score_run() -> None:
    from mapi_plugin_statsmodels.datasets import sales_dataset, sales_schema
    from mapi_plugin_statsmodels.examples import example_spec
    from mapi_plugin_statsmodels.views import forecast_view
    from mapi_sdk.harness import fit_spec, score_bundle
    from mapi_spec import load_score_request
    from mapi_spec.views import ModelForecastView

    data = sales_dataset(periods=72, geos=["north", "south"], seed=4)
    spec = example_spec(
        "ts.sarimax",
        "{order: [1, 0, 0], exog: [price, promo], series_dims: [geo]}",
        "{dims: {t: date}, vars: {y: sales}}",
    )
    bundle = fit_spec(spec, data.isel(date=slice(0, 64)), raw_schema=sales_schema(geo=True))
    req = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://t/t\nmode: forecast\n"
        "horizon: {dim: t, periods: 8}\n"
    )
    future = data.isel(date=slice(64, None)).drop_vars("sales")
    tree = score_bundle(bundle, req, future, return_full=True)
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    view = forecast_view(
        node.to_dataset(), observed=data["sales"].isel(date=slice(0, 64)), history=12
    )
    assert isinstance(view, ModelForecastView) and len(view.forecasts) == 2
    fan = view.forecasts[0]
    assert fan.history == 12 and len(fan.fan.coords) == 20
    assert fan.fan.quantiles[0] == 0.025 and fan.fan.observed is not None
    assert fan.model_dump()["facet"] == {"geo": "north"}
    ModelForecastView.model_validate_json(view.model_dump_json())


def test_ljung_box_counts_only_arma_terms() -> None:
    """Regression: the stored Ljung-Box test reduced its degrees of freedom by every
    parameter (constant, drivers, sigma2), so a correctly specified regression with AR(1)
    errors was flagged autocorrelated 23% of the time. Only ARMA terms count."""
    from mapi_plugin_statsmodels._common import arma_df, residual_tests

    assert arma_df(["intercept", "x1", "x2", "ar.L1", "sigma2"]) == 1
    assert arma_df(["ar.L1", "ma.L1", "ar.S.L4", "ma.S.L4", "sigma2"]) == 4
    assert arma_df(["const[0]", "ar.L1[0]", "ar.L1[1]", "ar.L2[0]", "ar.L2[1]"]) == 2
    assert arma_df(["const", "y.L1", "y.L2", "x.L0", "x.L1"]) == 2  # ARDL
    assert arma_df(["const", "y.L1", "x.L1", "D.y.L1", "D.x.L0"]) == 2  # UECM
    assert arma_df(["smoothing_level", "initial_level"]) == 0  # ETS
    import warnings

    from statsmodels.tsa.statespace.sarimax import SARIMAX

    reps, n, rejected = 120, 96, 0
    for seed in range(reps):
        rng = np.random.default_rng(seed)
        x = rng.normal(size=(n, 2))
        u = np.zeros(n)
        e = rng.normal(size=n)
        for t in range(1, n):
            u[t] = 0.5 * u[t - 1] + e[t]
        y = 1.0 + x @ np.array([0.5, -0.3]) + u
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = SARIMAX(y, exog=x, order=(1, 0, 0), trend="c").fit(disp=False)
        names = [str(v) for v in res.model.param_names]
        _stat, p, _lags = residual_tests(res.resid, model_df=arma_df(names))["test.ljung_box"]
        rejected += p < 0.05
    assert rejected / reps <= 0.09, rejected / reps
