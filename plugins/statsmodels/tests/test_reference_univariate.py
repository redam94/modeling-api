"""Reference values: the univariate plugins against direct statsmodels calls on the same data.

Every model is fitted through the SDK harness (binding, validation, bundle) and compared
with the statsmodels estimator called directly: parameters, one-step in-sample predictions
and forecast intervals (the 5% and 95% quantiles are the 90% interval).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_statsmodels.datasets import (
    ECM_SCHEMA,
    SALES_SCHEMA,
    ecm_dataset,
    sales_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import load_score_request
from mapi_spec.schema import DataSchema

RTOL = 1e-7
SALES_BIND = "{dims: {t: date}, vars: {y: sales}}"


def spec(model: str, settings: str, bind: str = SALES_BIND) -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: ref, project: tests}}
data: {{dataset: "dataset://tests/ref@latest", bind: {bind}}}
model: {{use: "{model}@^1", settings: {settings}, fit: {{seed: 1}}}}
"""


def fit(
    registry: Registry,
    model: str,
    settings: str,
    data: xr.Dataset,
    schema: DataSchema,
    bind: str = SALES_BIND,
) -> Bundle:
    return fit_spec(spec(model, settings, bind), data, registry=registry, raw_schema=schema)


def predict(
    registry: Registry,
    bundle: Bundle,
    payload: xr.Dataset | None,
    *,
    mode: str = "predict",
    periods: int | None = None,
) -> xr.DataArray:
    body = f"mode: {mode}\n" + (f"horizon: {{dim: t, periods: {periods}}}\n" if periods else "")
    req = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://tests/ref\n" + body
    )
    tree = score_bundle(bundle, req, payload, registry=registry, return_full=True)
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"]


def q(da: xr.DataArray, level: float) -> np.ndarray:
    return np.asarray(da.sel(quantile=level).values, dtype=float)


@pytest.fixture(scope="module")
def sales() -> xr.Dataset:
    return sales_dataset(periods=84, seed=11)


def only(ds: xr.Dataset, *keep: str) -> tuple[xr.Dataset, DataSchema]:
    """``sales`` plus the drivers in ``keep`` and the matching raw schema (drivers a model
    does not use must not be declared future-required)."""
    drop = [v for v in ("price", "promo") if v not in keep]
    data = SALES_SCHEMA.model_dump()
    for v in drop:
        data["vars"].pop(v)
    return ds.drop_vars(drop), DataSchema.model_validate(data)


def _xy(ds: xr.Dataset, names: list[str]) -> tuple[pd.Series, pd.DataFrame]:
    y = pd.Series(np.asarray(ds["sales"].values, dtype=float), name="y")
    x = pd.DataFrame({n: np.asarray(ds[n].values, dtype=float) for n in names})
    return y, x


# ================================================================================= SARIMAX


def test_sarimax_matches_statsmodels(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    train, future = sales.isel(date=slice(0, 76)), sales.isel(date=slice(76, None))
    b = fit(registry, "ts.sarimax", "{order: [1, 0, 1], exog: [price, promo]}", train, SALES_SCHEMA)
    y, x = _xy(train, ["price", "promo"])
    direct = SARIMAX(y, exog=x, order=(1, 0, 1), trend="c").fit(
        disp=False, maxiter=200, method="lbfgs"
    )
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=RTOL)
    se = b.model.groups["params"]["std_err"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(se.values, np.asarray(direct.bse), rtol=1e-5)

    fc = predict(registry, b, future.drop_vars("sales"), mode="forecast", periods=8)
    ref = direct.get_forecast(
        8, exog=future[["price", "promo"]].to_dataframe()[["price", "promo"]].to_numpy()
    )
    ci = np.asarray(ref.conf_int(alpha=0.1))
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.05), ci[:, 0], rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.95), ci[:, 1], rtol=RTOL)

    ins = predict(registry, b, train.drop_vars("sales"))
    one_step = direct.get_prediction(start=0, end=75)
    np.testing.assert_allclose(q(ins, 0.5), np.asarray(one_step.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(
        q(ins, 0.05)[5:], np.asarray(one_step.conf_int(alpha=0.1))[5:, 0], rtol=RTOL
    )


def test_seasonal_sarimax_with_differencing(registry: Registry) -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    rng = np.random.default_rng(4)
    n = 96
    date = pd.date_range("2020-01-05", periods=n, freq="W-SUN").as_unit("ns")
    season = 5 * np.sin(2 * np.pi * np.arange(n) / 12)
    sales = 50 + np.cumsum(rng.normal(0, 1, n)) + season + rng.normal(0, 0.5, n)
    ds = xr.Dataset({"sales": (("date",), sales)}, coords={"date": date})
    schema = DataSchema.model_validate(
        {
            "dims": {"date": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
            "vars": {"sales": {"dims": ["date"], "dtype": "float64", "role": "target"}},
        }
    )
    b = fit(registry, "ts.sarimax", "{order: [0, 1, 1], seasonal_order: [0, 1, 1, 12]}", ds, schema)
    direct = SARIMAX(
        pd.Series(sales, name="y"), order=(0, 1, 1), seasonal_order=(0, 1, 1, 12), trend="n"
    ).fit(disp=False, maxiter=200, method="lbfgs")
    fc = predict(registry, b, None, mode="forecast", periods=12)
    ref = direct.get_forecast(12)
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.975), np.asarray(ref.conf_int(alpha=0.05))[:, 1], rtol=RTOL)
    assert b.model.learned["cells"][0]["burn"] == 13  # d + D*s diffuse periods


def test_arima_matches_statsmodels(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.arima.model import ARIMA

    b = fit(registry, "ts.arima", "{order: [2, 1, 0], trend: t}", *only(sales))
    y, _ = _xy(sales, [])
    direct = ARIMA(y, order=(2, 1, 0), trend="t").fit(
        method="statespace", method_kwargs={"maxiter": 200, "disp": False, "method": "lbfgs"}
    )
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=RTOL)
    fc = predict(registry, b, None, mode="forecast", periods=6)
    ref = direct.get_forecast(6)
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.05), np.asarray(ref.conf_int(alpha=0.1))[:, 0], rtol=RTOL)


def test_unobserved_components_matches_statsmodels(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.statespace.structural import UnobservedComponents

    train, future = sales.isel(date=slice(0, 78)), sales.isel(date=slice(78, None))
    b = fit(
        registry,
        "ts.unobserved_components",
        "{level: local level, exog: [price]}",
        *only(train, "price"),
    )
    y, x = _xy(train, ["price"])
    direct = UnobservedComponents(y, level="local level", exog=x).fit(
        disp=False, maxiter=200, method="lbfgs"
    )
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=1e-6)
    fc = predict(registry, b, future.drop_vars(["sales", "promo"]), mode="forecast", periods=6)
    ref = direct.get_forecast(6, exog=np.asarray(future["price"].values).reshape(-1, 1))
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(ref.predicted_mean), rtol=1e-6)
    np.testing.assert_allclose(q(fc, 0.95), np.asarray(ref.conf_int(alpha=0.1))[:, 1], rtol=1e-6)
    comps = b.model.groups["components"]
    np.testing.assert_allclose(
        comps["level"].values, np.asarray(direct.level["smoothed"]), rtol=1e-6
    )


# ===================================================================================== ETS


def test_ets_matches_statsmodels(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    b = fit(registry, "ts.ets", "{trend: add, damped_trend: true}", *only(sales))
    y, _ = _xy(sales, [])
    direct = ETSModel(y, error="add", trend="add", damped_trend=True).fit(disp=False, maxiter=1000)
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=1e-6)
    fc = predict(registry, b, None, mode="forecast", periods=5)
    ref = direct.get_prediction(start=84, end=88)
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(ref.predicted_mean), rtol=1e-6)
    np.testing.assert_allclose(q(fc, 0.05), np.asarray(ref.pred_int(alpha=0.1))[:, 0], rtol=1e-6)
    ins = predict(registry, b, only(sales)[0].drop_vars(["sales"]))
    np.testing.assert_allclose(q(ins, 0.5), np.asarray(direct.fittedvalues), rtol=1e-6)


def test_multiplicative_ets_intervals_are_seeded_simulations(registry: Registry) -> None:
    ds = sales_dataset(periods=60, seed=3)
    b = fit(registry, "ts.ets", "{error: mul, trend: add}", *only(ds))
    a = predict(registry, b, None, mode="forecast", periods=4)
    c = predict(registry, b, None, mode="forecast", periods=4)
    xr.testing.assert_identical(a, c)  # same seed, same simulated quantiles
    assert bool((a.diff("quantile") >= 0).all())


# =================================================================================== Theta


def test_theta_matches_statsmodels(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.forecasting.theta import ThetaModel

    b = fit(registry, "ts.theta", "{}", *only(sales))
    y, _ = _xy(sales, [])
    direct = ThetaModel(y, period=1, deseasonalize=False).fit()
    est = b.model.groups["params"]["estimate"].sel(param=["b0", "alpha"])
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=RTOL)
    fc = predict(registry, b, None, mode="forecast", periods=6)
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(direct.forecast(6, theta=2)), rtol=RTOL)
    pi = direct.prediction_intervals(6, theta=2, alpha=0.1)
    np.testing.assert_allclose(q(fc, 0.05), np.asarray(pi["lower"]), rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.95), np.asarray(pi["upper"]), rtol=RTOL)


def test_theta_in_sample_step_reproduces_the_one_step_forecast() -> None:
    """The in-sample one-step formula at period n equals ``forecast(1)`` of the fit on
    periods 0..n-1, with and without seasonal adjustment."""
    from mapi_plugin_statsmodels._common import Cell
    from mapi_plugin_statsmodels.smoothing import _theta_insample
    from statsmodels.tsa.forecasting.theta import ThetaModel

    rng = np.random.default_rng(9)
    n = 48
    seasonal = 1 + 0.2 * np.sin(2 * np.pi * np.arange(n + 1) / 4)
    y = (100 + np.cumsum(rng.normal(0.5, 1, n + 1))) * seasonal
    for period, deseason in ((1, False), (4, True)):
        res = ThetaModel(
            pd.Series(y[:n]), period=period, deseasonalize=deseason, use_test=False
        ).fit()
        mean, _ = _theta_insample(res, Cell(key={}, y=y, x=None), 2.0)
        np.testing.assert_allclose(mean[n], float(np.asarray(res.forecast(1))[0]), rtol=1e-10)


# ==================================================================================== ARDL


def test_ardl_matches_statsmodels(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.ardl import ARDL

    train, future = sales.isel(date=slice(0, 78)), sales.isel(date=slice(78, None))
    b = fit(registry, "ts.ardl", "{lags: 2, order: 1, exog: [price, promo]}", train, SALES_SCHEMA)
    y, x = _xy(train, ["price", "promo"])
    direct = ARDL(y, 2, x, 1, trend="c").fit()
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.params.index))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=RTOL)
    fc = predict(registry, b, future.drop_vars("sales"), mode="forecast", periods=6)
    xf = pd.DataFrame(
        {n: np.asarray(future[n].values) for n in ("price", "promo")}, index=range(78, 84)
    )
    ref = direct.get_prediction(start=78, end=83, exog_oos=xf)
    np.testing.assert_allclose(q(fc, 0.5), np.asarray(ref.predicted_mean), rtol=RTOL)
    # the h-step variance is sigma2 * sum(psi_j^2) of the AR polynomial of y
    phi = [float(direct.params["y.L1"]), float(direct.params["y.L2"])]
    psi = [1.0, phi[0]]
    for j in range(2, 6):
        psi.append(phi[0] * psi[j - 1] + phi[1] * psi[j - 2])
    var = float(direct.sigma2) * np.cumsum(np.square(psi))
    z = 1.6448536269514722
    np.testing.assert_allclose(q(fc, 0.95) - q(fc, 0.5), z * np.sqrt(var), rtol=1e-6)


def test_ardl_auto_selection_matches_ardl_select_order(
    registry: Registry, sales: xr.Dataset
) -> None:
    from statsmodels.tsa.ardl import ardl_select_order

    b = fit(
        registry,
        "ts.ardl",
        "{lags: 4, order: 3, auto: true, ic: bic, exog: [price, promo]}",
        sales,
        SALES_SCHEMA,
    )
    y, x = _xy(sales, ["price", "promo"])
    sel = ardl_select_order(y, 4, x, 3, trend="c", ic="bic")
    cell = b.model.learned["cells"][0]
    assert cell["ar_lags"] == [int(v) for v in (sel.model.ar_lags or [])]
    res = sel.model.fit()
    est = b.model.groups["params"]["estimate"].sel(param=list(res.params.index))
    np.testing.assert_allclose(est.values, np.asarray(res.params), rtol=RTOL)


# ===================================================================================== ECM


def test_ecm_matches_statsmodels_uecm(registry: Registry) -> None:
    from statsmodels.tsa.ardl import UECM

    ds = ecm_dataset(periods=140, seed=2)
    b = fit(
        registry,
        "ts.ecm",
        "{lags: 2, order: 2, exog: [income]}",
        ds,
        ECM_SCHEMA,
        "{dims: {t: month}, vars: {y: sales}}",
    )
    y = pd.Series(np.asarray(ds["sales"].values), name="y")
    x = pd.DataFrame({"income": np.asarray(ds["income"].values)})
    direct = UECM(y, 2, x, 2, trend="c").fit()
    params = b.model.groups["params"]
    est = params["estimate"].sel(param=list(direct.params.index))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=1e-7)
    lr = params["estimate"].sel(param=[f"long_run.{n}" for n in direct.ci_params.index])
    np.testing.assert_allclose(lr.values, np.asarray(direct.ci_params), rtol=1e-7)
    bounds = direct.bounds_test(case=3)
    ecm = b.model.groups["ecm"]
    assert float(ecm["bounds_statistic"]) == pytest.approx(float(bounds.statistic), rel=1e-9)
    assert float(ecm["bounds_p_upper"]) == pytest.approx(float(bounds.pvalue["upper"]), rel=1e-9)
    # the data are built with 30% of the gap closed per month and long run 2 + 0.8 income
    assert float(ecm["speed_of_adjustment"]) == pytest.approx(-0.3, abs=0.15)
    assert float(params["estimate"].sel(param="long_run.income")) == pytest.approx(-0.8, abs=0.1)
    assert b.model.learned["cells"][0]["bounds_verdict"] == "levels_relationship"


def test_ecm_on_the_danish_money_demand_data(registry: Registry) -> None:
    """Johansen & Juselius' Danish money-demand data (statsmodels docs' ARDL example):
    the plugin's UECM equals statsmodels' on lrm ~ lry + ibo + ide."""
    from statsmodels.datasets import danish_data
    from statsmodels.tsa.ardl import UECM

    raw = danish_data.load().data
    names = ["lry", "ibo", "ide"]
    date = pd.date_range("1974-01-01", periods=len(raw), freq="QS-JAN").as_unit("ns")
    ds = xr.Dataset(
        {c: (("quarter",), np.asarray(raw[c], dtype=float)) for c in ["lrm", *names]},
        coords={"quarter": date},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {"quarter": {"kind": "time", "policy": "extend", "freq": "QS-JAN"}},
            "vars": {
                "lrm": {"dims": ["quarter"], "dtype": "float64", "role": "target"},
                **{n: {"dims": ["quarter"], "dtype": "float64"} for n in names},
            },
        }
    )
    b = fit(
        registry,
        "ts.ecm",
        "{lags: 3, order: 2, exog: [lry, ibo, ide]}",
        ds,
        schema,
        "{dims: {t: quarter}, vars: {y: lrm}}",
    )
    y = pd.Series(np.asarray(raw["lrm"], dtype=float), name="y")
    direct = UECM(y, 3, raw[names].reset_index(drop=True), 2, trend="c").fit()
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.params.index))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=1e-7)
    bounds = direct.bounds_test(case=3)
    assert float(b.model.groups["ecm"]["bounds_statistic"]) == pytest.approx(
        float(bounds.statistic), rel=1e-9
    )
