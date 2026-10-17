"""Forecasts conditioned on actuals (:mod:`mapi_spec.actuals`) for every statsmodels plugin.

Each model is fitted through the SDK harness, then scored in ``forecast`` mode with a payload
that carries actuals after the fit (and sometimes revisions of its last periods) plus the
periods to forecast. The result is compared with statsmodels called directly with the
*fitted* parameters on the extended data - ``results.append(new, refit=False)`` for the
state-space and ARDL models, the VAR/VECM forecast functions on the supplied lags, the
fixed-parameter ETS smoother, the theta forecast with the fixed SES weight and the Hamilton
filter. Nothing is re-estimated: forecast bands keep the fitted noise variance.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_statsmodels import PLUGINS
from mapi_plugin_statsmodels.datasets import (
    ECM_SCHEMA,
    GROWTH_SCHEMA,
    MACRO_SCHEMA,
    REGIME_SCHEMA,
    SALES_SCHEMA,
    ecm_dataset,
    macro_dataset,
    macro_growth,
    regime_dataset,
    sales_dataset,
    sales_schema,
)
from mapi_plugin_statsmodels.examples import Example, examples
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import load_score_request
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from scipy import optimize, stats

RTOL = 1e-6
SALES_BIND = "{dims: {t: date}, vars: {y: sales}}"
MACRO_BIND = "{dims: {t: quarter, variable: series}, vars: {y: value}}"
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://tests/actuals\n"


# =================================================================================== helpers


def spec(model: str, settings: str, bind: str = SALES_BIND, pipeline: str = "") -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: actuals, project: tests}}
data: {{dataset: "dataset://tests/actuals@latest", bind: {bind}}}
{pipeline}
model: {{use: "{model}@^1", settings: {settings}, fit: {{seed: 1}}}}
"""


def fit(
    registry: Registry,
    model: str,
    settings: str,
    data: xr.Dataset,
    schema: DataSchema,
    bind: str = SALES_BIND,
    pipeline: str = "",
) -> Bundle:
    return fit_spec(
        spec(model, settings, bind, pipeline), data, registry=registry, raw_schema=schema
    )


def forecast(
    registry: Registry,
    bundle: Bundle,
    payload: xr.Dataset | None,
    *,
    periods: int = 4,
    future_covariates: str | None = None,
    history: xr.Dataset | None = None,
) -> tuple[xr.DataArray, dict[str, Any]]:
    """``y_hat`` (quantiles, every payload period plus appended ones) and the conditioning
    record of a forecast."""
    body = f"mode: forecast\nhorizon: {{dim: t, periods: {periods}}}\n"
    if future_covariates:
        body += f"future_covariates: {future_covariates}\n"
    tree = score_bundle(
        bundle,
        load_score_request(HEADER + body),
        payload,
        registry=registry,
        return_full=True,
        history=history,
    )
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    record = json.loads(str(tree.attrs.get("conditioning", "{}")))
    return node.to_dataset()["y_hat"], record


def q(da: xr.DataArray, level: float, **sel: Any) -> np.ndarray:
    return np.asarray(da.sel(quantile=level, **sel).values, dtype=float)


def params_of(bundle: Bundle, cell: int = 0, **key: Any) -> tuple[list[str], np.ndarray]:
    """The stored parameter names and estimates of one fitted cell."""
    learned = bundle.model.learned["cells"][cell]
    names = [str(p) for p in learned["params"]]
    est = bundle.model.groups["params"]["estimate"]
    if key:
        est = est.sel(key)
    return names, np.asarray(est.sel(param=names).values, dtype=float)


def gaussian(mean: Any, var: Any, level: float) -> np.ndarray:
    return np.asarray(mean, dtype=float) + stats.norm.ppf(level) * np.sqrt(
        np.asarray(var, dtype=float)
    )


def series(values: Any, start: int, name: str = "y") -> pd.Series:
    arr = np.asarray(values, dtype=float)
    return pd.Series(arr, index=pd.RangeIndex(start, start + arr.shape[0]), name=name)


def frame(ds: xr.Dataset, names: list[str], start: int) -> pd.DataFrame:
    cols = {n: np.asarray(ds[n].values, dtype=float) for n in names}
    n = len(next(iter(cols.values())))
    return pd.DataFrame(cols, index=pd.RangeIndex(start, start + n))


def blank(ds: xr.Dataset, var: str, **where: Any) -> xr.Dataset:
    """``ds`` with ``var`` empty (NaN) where ``where`` selects (all of it by default)."""
    out = ds.copy(deep=True)
    if not where:
        out[var] = out[var] * np.nan
        return out
    out[var].loc[where] = np.nan
    return out


@pytest.fixture(scope="module")
def sales() -> xr.Dataset:
    return sales_dataset(periods=92, seed=11)


def only(ds: xr.Dataset, *keep: str) -> tuple[xr.Dataset, DataSchema]:
    drop = [v for v in ("price", "promo") if v not in keep]
    doc = SALES_SCHEMA.model_dump()
    for v in drop:
        doc["vars"].pop(v)
    return ds.drop_vars(drop), DataSchema.model_validate(doc)


# =============================================================================== capability


@pytest.mark.parametrize("plugin", PLUGINS, ids=[p.id for p in PLUGINS])
def test_every_statsmodels_plugin_declares_the_capability(plugin: Any) -> None:
    caps = plugin.manifest().capabilities
    assert caps.conditions_on_actuals is True
    assert "forecast" in caps.score_modes


# ============================================================================ state space


def test_sarimax_with_drivers_equals_append(registry: Registry, sales: xr.Dataset) -> None:
    """Actuals after the fit with their drivers, then listed future dates with drivers."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    train = sales.isel(date=slice(0, 76))
    new = sales.isel(date=slice(76, 80))
    future = sales.isel(date=slice(80, 84))
    b = fit(registry, "ts.sarimax", "{order: [1, 0, 1], exog: [price, promo]}", train, SALES_SCHEMA)
    payload = xr.concat([new, blank(future, "sales")], dim="date")
    fc, record = forecast(registry, b, payload, periods=4)

    _, est = params_of(b)
    x_cols = ["price", "promo"]
    direct = SARIMAX(
        series(train["sales"].values, 0), exog=frame(train, x_cols, 0), order=(1, 0, 1), trend="c"
    ).filter(est)
    upd = direct.append(series(new["sales"].values, 76), exog=frame(new, x_cols, 76), refit=False)
    ref = upd.get_forecast(4, exog=frame(future, x_cols, 80))
    tail = {"date": future["date"].values}
    np.testing.assert_allclose(q(fc, 0.5, **tail), np.asarray(ref.predicted_mean), rtol=RTOL)
    ci = np.asarray(ref.conf_int(alpha=0.1))
    np.testing.assert_allclose(q(fc, 0.05, **tail), ci[:, 0], rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.95, **tail), ci[:, 1], rtol=RTOL)
    # one-step-ahead predictions on the actual rows
    one = upd.get_prediction(start=76, end=79)
    head = {"date": new["date"].values}
    np.testing.assert_allclose(q(fc, 0.5, **head), np.asarray(one.predicted_mean), rtol=RTOL)

    assert record["conditioned"] is True
    assert record["n_actuals"] == 4
    assert record["horizon_source"] == "rows"
    assert record["parameters"] == "fitted"
    assert record["actuals_through"].startswith(str(new["date"].values[-1])[:10])


def test_sarimax_payload_ending_with_an_actual_appends_the_horizon(
    registry: Registry, sales: xr.Dataset
) -> None:
    """No drivers in the model: the horizon is appended after the last actual."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data, schema = only(sales)
    train, new = data.isel(date=slice(0, 80)), data.isel(date=slice(80, 83))
    b = fit(registry, "ts.sarimax", "{order: [2, 0, 0]}", train, schema)
    fc, record = forecast(registry, b, new, periods=5)
    _, est = params_of(b)
    direct = SARIMAX(series(train["sales"].values, 0), order=(2, 0, 0), trend="c").filter(est)
    ref = direct.append(series(new["sales"].values, 80), refit=False).get_forecast(5)
    assert fc.sizes["date"] == 3 + 5
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(
        q(fc, 0.975)[3:], np.asarray(ref.conf_int(alpha=0.05))[:, 1], rtol=RTOL
    )
    assert record["horizon_source"] == "horizon"
    assert record["periods"] == 5


def monthly_counts(n: int = 156, seed: int = 3) -> xr.Dataset:
    """A monthly int64 count series with trend and a yearly season (airline-like)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    y = np.exp(4.8 + 0.01 * t + 0.15 * np.sin(2 * np.pi * t / 12) + rng.normal(0, 0.03, n))
    date = pd.date_range("1949-01-01", periods=n, freq="MS").as_unit("ns")
    return xr.Dataset({"y": (("t",), np.round(y).astype(np.int64))}, coords={"t": date})


MONTHLY_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {"y": {"dims": ["t"], "dtype": "int64", "role": "target"}},
    }
)


def test_seasonal_differenced_sarimax_on_monthly_counts(registry: Registry) -> None:
    """The readiness workflow's model: ARIMA(p,1,q)(0,1,0,12) on an int64 monthly series
    (its differencing lives in the state space, so the forecast is in the original units).
    The actuals move the forecast; sending the fit's own last values does not."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data = monthly_counts()
    train, new = data.isel(t=slice(0, 144)), data.isel(t=slice(144, 147))
    settings = "{order: [1, 1, 1], seasonal_order: [0, 1, 0, 12]}"
    b = fit(registry, "ts.sarimax", settings, train, MONTHLY_SCHEMA, bind="{}")
    plain, _ = forecast(registry, b, None, periods=8)
    fc, record = forecast(registry, b, new, periods=8)
    _, est = params_of(b)
    y = series(train["y"].values, 0)
    direct = SARIMAX(y, order=(1, 1, 1), seasonal_order=(0, 1, 0, 12), trend="n").filter(est)
    ref = direct.append(series(new["y"].values, 144), refit=False).get_forecast(8)
    want = pd.date_range("1961-04-01", periods=8, freq="MS").as_unit("ns").values
    np.testing.assert_array_equal(fc["t"].values[3:], want)
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(
        q(fc, 0.05)[3:], np.asarray(ref.conf_int(alpha=0.1))[:, 0], rtol=RTOL
    )
    assert record["actuals_through"].startswith("1961-03-01")
    assert record["n_actuals"] == 3
    assert "1961-04-01" in record["message"]

    # extreme actuals (the values the user tried) are not ignored
    wild = new.copy(deep=True)
    wild["y"].values[:] = [9999, 1, 50000]
    moved, _ = forecast(registry, b, wild, periods=8)
    assert not np.allclose(q(moved, 0.5)[3:], q(fc, 0.5)[3:])
    # the fit's own last three months as actuals give the plain forecast
    own, _ = forecast(registry, b, train.isel(t=slice(141, 144)), periods=8)
    np.testing.assert_allclose(q(own, 0.5)[3:], q(plain, 0.5), rtol=RTOL)
    np.testing.assert_allclose(q(own, 0.95)[3:], q(plain, 0.95), rtol=RTOL)


def test_arima_revisions_replace_fitted_values(registry: Registry, sales: xr.Dataset) -> None:
    """Actuals inside the fitted range revise it; the parameters stay the fitted ones."""
    from statsmodels.tsa.arima.model import ARIMA

    data, schema = only(sales)
    train = data.isel(date=slice(0, 80))
    b = fit(registry, "ts.arima", "{order: [2, 1, 0], trend: t}", train, schema)
    revised = data.isel(date=slice(78, 83)).copy(deep=True)  # 2 revisions + 3 new
    revised["sales"].values[:2] += np.array([5.0, -3.0])
    payload = xr.concat([revised, blank(data.isel(date=slice(83, 86)), "sales")], dim="date")
    fc, record = forecast(registry, b, payload, periods=3)

    _, est = params_of(b)
    y_all = np.concatenate([train["sales"].values[:78], revised["sales"].values])
    ref_res = ARIMA(series(y_all, 0), order=(2, 1, 0), trend="t").filter(est)
    ref = ref_res.get_forecast(3)
    np.testing.assert_allclose(q(fc, 0.5)[-3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(
        q(fc, 0.05)[-3:], np.asarray(ref.conf_int(alpha=0.1))[:, 0], rtol=RTOL
    )
    one = ref_res.get_prediction(start=78, end=82)
    np.testing.assert_allclose(q(fc, 0.5)[:5], np.asarray(one.predicted_mean), rtol=RTOL)
    assert record["revised"] == 2
    assert record["n_actuals"] == 5


def test_unobserved_components_filters_a_gap_in_the_actuals(
    registry: Registry, sales: xr.Dataset
) -> None:
    """``missing: allow`` models filter an empty period among the actuals."""
    from statsmodels.tsa.statespace.structural import UnobservedComponents

    data, schema = only(sales, "price")
    train = data.isel(date=slice(0, 80))
    b = fit(
        registry, "ts.unobserved_components", "{level: local level, exog: [price]}", train, schema
    )
    new = data.isel(date=slice(80, 84)).copy(deep=True)
    new["sales"].values[1] = np.nan  # a gap between actuals
    future = blank(data.isel(date=slice(84, 87)), "sales")
    fc, record = forecast(registry, b, xr.concat([new, future], dim="date"), periods=3)

    _, est = params_of(b)
    direct = UnobservedComponents(
        series(train["sales"].values, 0), level="local level", exog=frame(train, ["price"], 0)
    ).filter(est)
    upd = direct.append(
        series(new["sales"].values, 80), exog=frame(new, ["price"], 80), refit=False
    )
    ref = upd.get_forecast(3, exog=frame(future, ["price"], 84))
    np.testing.assert_allclose(q(fc, 0.5)[-3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    np.testing.assert_allclose(
        q(fc, 0.95)[-3:], np.asarray(ref.conf_int(alpha=0.1))[:, 1], rtol=RTOL
    )
    assert record["series"][0]["missing"] == 1
    assert record["n_actuals"] == 3


def test_a_gap_needs_its_drivers(registry: Registry, sales: xr.Dataset) -> None:
    data, schema = only(sales, "price")
    b = fit(
        registry,
        "ts.unobserved_components",
        "{level: local level, exog: [price]}",
        data.isel(date=slice(0, 80)),
        schema,
    )
    new = data.isel(date=slice(80, 83)).copy(deep=True)
    new["sales"].values[1] = np.nan
    new["price"].values[1] = np.nan
    with pytest.raises(MapiError) as info:
        forecast(registry, b, new, periods=2, future_covariates="carry_last")
    assert info.value.code is ErrorCode.ACTUALS_INVALID
    assert "price" in str(info.value.untrusted_text)


# ======================================================================================= ETS


def test_ets_uses_the_fitted_smoothing_weights_and_noise(
    registry: Registry, sales: xr.Dataset
) -> None:
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    data, schema = only(sales)
    train, new = data.isel(date=slice(0, 80)), data.isel(date=slice(80, 84))
    b = fit(registry, "ts.ets", "{trend: add, damped_trend: true}", train, schema)
    plain, _ = forecast(registry, b, None, periods=6)
    fc, _ = forecast(registry, b, new, periods=6)
    names, est = params_of(b)
    y_ext = np.concatenate([train["sales"].values, new["sales"].values])
    mod = ETSModel(series(y_ext, 0), error="add", trend="add", damped_trend=True)
    order = [names.index(n) for n in mod.param_names]
    ref = mod.smooth(est[order]).get_prediction(start=84, end=89)
    np.testing.assert_allclose(q(fc, 0.5)[4:], np.asarray(ref.predicted_mean), rtol=RTOL)
    # the band width depends only on the parameters and the fitted noise variance
    width = lambda da: q(da, 0.95) - q(da, 0.05)  # noqa: E731
    np.testing.assert_allclose(width(fc)[4:], width(plain), rtol=RTOL)


def test_multiplicative_ets_conditions_by_simulation(registry: Registry) -> None:
    data = monthly_counts(120)
    train, new = data.isel(t=slice(0, 108)), data.isel(t=slice(108, 111))
    settings = "{error: mul, trend: add, seasonal: mul, seasonal_periods: 12}"
    b = fit(registry, "ts.ets", settings, train, MONTHLY_SCHEMA, bind="{}")
    plain, _ = forecast(registry, b, None, periods=4)
    own, _ = forecast(registry, b, train.isel(t=slice(105, 108)), periods=4)
    np.testing.assert_allclose(q(own, 0.5)[3:], q(plain, 0.5), rtol=RTOL)
    up = new.copy(deep=True)
    up["y"].values[:] = (up["y"].values * 1.5).astype(np.int64)
    high, _ = forecast(registry, b, up, periods=4)
    base, _ = forecast(registry, b, new, periods=4)
    assert (q(high, 0.5)[3:] > q(base, 0.5)[3:]).all()


# ===================================================================================== Theta


def test_theta_uses_the_fitted_weight(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.forecasting.theta import ThetaModel, ThetaModelResults
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    data, schema = only(sales)
    train, new = data.isel(date=slice(0, 80)), data.isel(date=slice(80, 83))
    b = fit(registry, "ts.theta", "{deseasonalize: false}", train, schema)
    fc, _ = forecast(registry, b, new, periods=5)
    names, est = params_of(b)
    p = dict(zip(names, est, strict=True))
    y_ext = np.concatenate([train["sales"].values, new["sales"].values])
    ses = ExponentialSmoothing(y_ext, initial_level=y_ext[0], initialization_method="known").fit(
        smoothing_level=p["alpha"], optimized=False
    )
    model = ThetaModel(series(y_ext, 0), deseasonalize=False)
    ref = ThetaModelResults(
        p["b0"], p["alpha"], p["sigma2"], float(ses.forecast(1)[0]), np.empty(0), False, model
    )
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.forecast(5, theta=2.0)), rtol=RTOL)
    lo, hi = (np.asarray(a) for a in ref.prediction_intervals(5, alpha=0.1, theta=2.0).T.values)
    np.testing.assert_allclose(q(fc, 0.05)[3:], lo, rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.95)[3:], hi, rtol=RTOL)


def test_seasonal_theta_keeps_the_fitted_season(registry: Registry) -> None:
    data = monthly_counts(120)
    train = data.isel(t=slice(0, 108))
    b = fit(registry, "ts.theta", "{period: 12, use_test: false}", train, MONTHLY_SCHEMA, bind="{}")
    plain, _ = forecast(registry, b, None, periods=12)
    own, _ = forecast(registry, b, train.isel(t=slice(105, 108)), periods=12)
    np.testing.assert_allclose(q(own, 0.5)[3:], q(plain, 0.5), rtol=RTOL)
    new = data.isel(t=slice(108, 111))
    fc, _ = forecast(registry, b, new, periods=9)
    # the forecast for the next season's months keeps the fitted seasonal shape
    ratio = q(fc, 0.5)[3:] / q(plain, 0.5)[3:12]
    assert np.ptp(ratio) < 0.02


def test_theta_by_maximum_likelihood_filters_with_the_fitted_ima(
    registry: Registry, sales: xr.Dataset
) -> None:
    """``use_mle``: the SES state comes from the fitted IMA(1,1) with drift, filtered over
    the extended history with the stored parameters."""
    from statsmodels.tsa.forecasting.theta import ThetaModel, ThetaModelResults
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data, schema = only(sales)
    train, new = data.isel(date=slice(0, 80)), data.isel(date=slice(80, 83))
    b = fit(registry, "ts.theta", "{deseasonalize: false, use_mle: true}", train, schema)
    plain, _ = forecast(registry, b, None, periods=4)
    own, _ = forecast(registry, b, train.isel(date=slice(77, 80)), periods=4)
    np.testing.assert_allclose(q(own, 0.5)[3:], q(plain, 0.5), rtol=RTOL)
    fc, _ = forecast(registry, b, new, periods=4)
    names, est = params_of(b)
    p = dict(zip(names, est, strict=True))
    y_ext = np.concatenate([train["sales"].values, new["sales"].values])
    ima = SARIMAX(y_ext, order=(0, 1, 1), trend="c").filter([p["b0"], p["alpha"] - 1, p["sigma2"]])
    one_step = float(np.asarray(ima.forecast(1))[0]) - p["b0"]
    ref = ThetaModelResults(
        p["b0"],
        p["alpha"],
        p["sigma2"],
        one_step,
        np.empty(0),
        True,
        ThetaModel(series(y_ext, 0), deseasonalize=False),
    )
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.forecast(4)), rtol=RTOL)


# ===================================================================================== ARDL


def test_ardl_equals_append(registry: Registry, sales: xr.Dataset) -> None:
    from statsmodels.tsa.ardl import ARDL

    train = sales.isel(date=slice(0, 76))
    new, future = sales.isel(date=slice(76, 79)), sales.isel(date=slice(79, 83))
    b = fit(registry, "ts.ardl", "{lags: 2, order: 1, exog: [price, promo]}", train, SALES_SCHEMA)
    fc, _ = forecast(registry, b, xr.concat([new, blank(future, "sales")], "date"), periods=4)
    cols = ["price", "promo"]
    direct = ARDL(
        series(train["sales"].values, 0),
        2,
        frame(train, cols, 0),
        {"price": [0, 1], "promo": [0, 1]},
    ).fit()
    upd = direct.append(series(new["sales"].values, 76), frame(new, cols, 76), refit=False)
    ref = upd.get_prediction(start=79, end=82, exog_oos=frame(future, cols, 79))
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    one = upd.predict(start=76, end=78)
    np.testing.assert_allclose(q(fc, 0.5)[:3], np.asarray(one), rtol=RTOL)
    # the band uses the fitted residual variance (not one re-estimated on the new data)
    var = np.asarray(ref.var_pred_mean) * float(direct.sigma2) / float(upd.sigma2)
    np.testing.assert_allclose(q(fc, 0.95)[3:], gaussian(ref.predicted_mean, var, 0.95), rtol=RTOL)


def test_ecm_forecasts_from_the_levels_ardl(registry: Registry) -> None:
    from statsmodels.tsa.ardl import ARDL

    data = ecm_dataset(periods=100, seed=4)
    train, new = data.isel(month=slice(0, 90)), data.isel(month=slice(90, 93))
    future = data.isel(month=slice(93, 96))
    bind = "{dims: {t: month}, vars: {y: sales}}"
    b = fit(registry, "ts.ecm", "{lags: 1, order: 1, exog: [income]}", train, ECM_SCHEMA, bind=bind)
    fc, _ = forecast(registry, b, xr.concat([new, blank(future, "sales")], "month"), periods=3)
    direct = ARDL(
        series(train["sales"].values, 0), 1, frame(train, ["income"], 0), 1, trend="c"
    ).fit()
    upd = direct.append(series(new["sales"].values, 90), frame(new, ["income"], 90), refit=False)
    ref = upd.get_prediction(start=93, end=95, exog_oos=frame(future, ["income"], 93))
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.predicted_mean), rtol=RTOL)


# ============================================================================ VAR and VECM


def macro_payload(ds: xr.Dataset, start: int, stop: int, future: int = 0) -> xr.Dataset:
    new = ds.isel(quarter=slice(start, stop))
    if not future:
        return new
    fut = blank(ds.isel(quarter=slice(stop, stop + future)), "value")
    return xr.concat([new, fut], dim="quarter")


def ky(ds: xr.Dataset) -> np.ndarray:
    """``(t, k)`` values in the fitted variable order."""
    return np.asarray(ds["value"].transpose("quarter", "series").values, dtype=float)


def test_var_forecasts_from_the_supplied_lags(registry: Registry) -> None:
    from statsmodels.tsa.api import VAR

    data = macro_growth(quarters=100)
    train = data.isel(quarter=slice(0, 90))
    b = fit(registry, "ts.var", "{lags: 2}", train, GROWTH_SCHEMA, bind=MACRO_BIND)
    fc, record = forecast(registry, b, macro_payload(data, 90, 93, future=4), periods=4)
    direct = VAR(ky(train)).fit(2, trend="c")
    y_ext = np.vstack([ky(train), ky(data.isel(quarter=slice(90, 93)))])
    ref = direct.forecast(y_ext[-2:], steps=4)
    var = np.diagonal(direct.mse(4), axis1=1, axis2=2)
    fc = fc.transpose("quantile", "quarter", "series")
    np.testing.assert_allclose(q(fc, 0.5)[3:], ref, rtol=RTOL)
    np.testing.assert_allclose(q(fc, 0.05)[3:], gaussian(ref, var, 0.05), rtol=RTOL)
    one = np.vstack([direct.forecast(y_ext[j - 2 : j], steps=1) for j in (90, 91, 92)])
    np.testing.assert_allclose(q(fc, 0.5)[:3], one, rtol=RTOL)
    assert record["n_series"] == 3


def test_var_with_a_trend_continues_the_trend_index(registry: Registry) -> None:
    from statsmodels.tsa.api import VAR
    from statsmodels.tsa.vector_ar.var_model import forecast as var_forecast

    data = macro_growth(quarters=100)
    train = data.isel(quarter=slice(0, 90))
    b = fit(registry, "ts.var", "{lags: 1, trend: ct}", train, GROWTH_SCHEMA, bind=MACRO_BIND)
    plain, _ = forecast(registry, b, None, periods=3)
    own, _ = forecast(registry, b, macro_payload(train, 88, 90), periods=3)
    np.testing.assert_allclose(q(own, 0.5)[..., 2:], q(plain, 0.5), rtol=RTOL)
    fc, _ = forecast(registry, b, macro_payload(data, 90, 92), periods=3)
    direct = VAR(ky(train)).fit(1, trend="ct")
    y_ext = np.vstack([ky(train), ky(data.isel(quarter=slice(90, 92)))])
    t = np.arange(93, 96, dtype=float)  # 1-based periods after 92 observations
    ref = var_forecast(
        y_ext[-1:], direct.coefs, direct.coefs_exog.T, 3, np.column_stack([np.ones(3), t])
    )
    fc = fc.transpose("quantile", "quarter", "series")
    np.testing.assert_allclose(q(fc, 0.5)[2:], ref, rtol=RTOL)


@pytest.mark.parametrize("deterministic", ["ci", "colo", "cili"])
def test_vecm_forecasts_from_the_supplied_levels(registry: Registry, deterministic: str) -> None:
    from statsmodels.tsa.vector_ar.vecm import VECM

    data = macro_dataset(quarters=110)
    train = data.isel(quarter=slice(0, 100))
    settings = f"{{k_ar_diff: 2, coint_rank: 1, deterministic: {deterministic}}}"
    b = fit(registry, "ts.vecm", settings, train, MACRO_SCHEMA, bind=MACRO_BIND)
    fc, _ = forecast(registry, b, macro_payload(data, 100, 104, future=3), periods=3)
    direct = VECM(ky(train), k_ar_diff=2, coint_rank=1, deterministic=deterministic).fit()
    y_ext = np.vstack([ky(train), ky(data.isel(quarter=slice(100, 104)))])
    upd = copy.copy(direct)
    upd.y_all = y_ext.T
    upd.nobs = y_ext.shape[0] - direct.k_ar
    ref = upd.predict(steps=3)
    fc = fc.transpose("quantile", "quarter", "series")
    np.testing.assert_allclose(q(fc, 0.5)[4:], ref, rtol=RTOL)
    # the one-step predictions inside the fit are the fitted values
    ins, _ = forecast(registry, b, macro_payload(train, 95, 100), periods=1)
    fitted = np.asarray(direct.fittedvalues)[-5:]
    np.testing.assert_allclose(
        q(ins.transpose("quantile", "quarter", "series"), 0.5)[:5], fitted, rtol=RTOL
    )


def test_varmax_equals_append(registry: Registry) -> None:
    from statsmodels.tsa.statespace.varmax import VARMAX

    data = macro_growth(quarters=100)
    train = data.isel(quarter=slice(0, 90))
    b = fit(registry, "ts.varmax", "{order: [1, 0]}", train, GROWTH_SCHEMA, bind=MACRO_BIND)
    fc, _ = forecast(registry, b, macro_payload(data, 90, 92, future=3), periods=3)
    names, est = params_of(b)
    cols = [str(v) for v in train["series"].values]
    endog = pd.DataFrame(ky(train), columns=cols)
    mod = VARMAX(endog, order=(1, 0), trend="c")
    direct = mod.filter(est[[names.index(n) for n in mod.param_names]])
    new = pd.DataFrame(
        ky(data.isel(quarter=slice(90, 92))), columns=cols, index=pd.RangeIndex(90, 92)
    )
    ref = direct.append(new, refit=False).get_forecast(3)
    fc = fc.transpose("quantile", "quarter", "series")
    np.testing.assert_allclose(q(fc, 0.5)[2:], np.asarray(ref.predicted_mean), rtol=RTOL)


def test_dynamic_factor_equals_append(registry: Registry) -> None:
    from statsmodels.tsa.statespace.dynamic_factor import DynamicFactor

    data = macro_growth(quarters=100)
    train = data.isel(quarter=slice(0, 90))
    b = fit(registry, "ts.dynamic_factor", "{k_factors: 1}", train, GROWTH_SCHEMA, bind=MACRO_BIND)
    fc, _ = forecast(registry, b, macro_payload(data, 90, 93), periods=4)
    names, est = params_of(b)
    cols = [str(v) for v in train["series"].values]
    mod = DynamicFactor(pd.DataFrame(ky(train), columns=cols), k_factors=1, factor_order=1)
    direct = mod.filter(est[[names.index(n) for n in mod.param_names]])
    new = pd.DataFrame(
        ky(data.isel(quarter=slice(90, 93))), columns=cols, index=pd.RangeIndex(90, 93)
    )
    ref = direct.append(new, refit=False).get_forecast(4)
    fc = fc.transpose("quantile", "quarter", "series")
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    ci = np.asarray(ref.conf_int(alpha=0.1))[:, 3:]  # upper bounds of the 3 series
    np.testing.assert_allclose(q(fc, 0.95)[3:], ci, rtol=RTOL)


def test_multivariate_actuals_need_every_series(registry: Registry) -> None:
    data = macro_growth(quarters=100)
    b = fit(
        registry,
        "ts.var",
        "{lags: 1}",
        data.isel(quarter=slice(0, 90)),
        GROWTH_SCHEMA,
        bind=MACRO_BIND,
    )
    payload = macro_payload(data, 90, 92).copy(deep=True)
    payload["value"].loc[{"series": "realinv", "quarter": payload["quarter"].values[-1]}] = np.nan
    with pytest.raises(MapiError) as info:
        forecast(registry, b, payload, periods=2)
    assert info.value.code is ErrorCode.ACTUALS_INVALID
    assert "realinv" in str(info.value.untrusted_text)  # the plugin's own words


# ========================================================================== Markov switching


def _mixture_quantile(mu: np.ndarray, w: np.ndarray, sd: np.ndarray, level: float) -> float:
    cdf = lambda x: float((w * stats.norm.cdf((x - mu) / sd)).sum()) - level  # noqa: E731
    return float(optimize.brentq(cdf, mu.min() - 20 * sd.max(), mu.max() + 20 * sd.max()))


def test_markov_regression_filters_the_actuals(registry: Registry) -> None:
    from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

    raw = regime_dataset(periods=130, seed=3).drop_vars("regime")
    train = raw.isel(week=slice(0, 110))
    bind = "{dims: {t: week}, vars: {y: demand}}"
    b = fit(
        registry,
        "ts.markov_switching",
        "{k_regimes: 2, time_kind: integer}",
        train,
        REGIME_SCHEMA,
        bind=bind,
    )
    new = raw.isel(week=slice(110, 114))
    fc, _ = forecast(registry, b, new, periods=3)
    names, est = params_of(b)
    y_ext = np.concatenate([train["demand"].values, new["demand"].values])
    mod = MarkovRegression(series(y_ext, 0), k_regimes=2, trend="c", switching_variance=True)
    p = est[[names.index(n) for n in mod.param_names]]
    res = mod.filter(p)
    trans = np.asarray(res.regime_transition)[:, :, 0]
    prob = np.asarray(res.filtered_marginal_probabilities)[-1]
    pd_ = dict(zip(mod.param_names, p, strict=True))
    mu = np.array([pd_["const[0]"], pd_["const[1]"]])
    sd = np.sqrt([pd_["sigma2[0]"], pd_["sigma2[1]"]])
    for h in range(3):
        prob = trans @ prob
        for level in (0.05, 0.5, 0.95):
            want = _mixture_quantile(mu, prob, sd, level)
            assert q(fc, level)[4 + h] == pytest.approx(want, rel=1e-6)


def test_markov_autoregression_simulates_from_the_filtered_regimes(registry: Registry) -> None:
    """Order > 0: forecasts are seeded simulations from the regime probabilities filtered
    over the actuals and the actuals' own lags."""
    raw = regime_dataset(periods=130, seed=3).drop_vars("regime")
    train = raw.isel(week=slice(0, 110))
    bind = "{dims: {t: week}, vars: {y: demand}}"
    settings = "{k_regimes: 2, order: 1, time_kind: integer, simulations: 4000}"
    b = fit(registry, "ts.markov_switching", settings, train, REGIME_SCHEMA, bind=bind)
    plain, _ = forecast(registry, b, None, periods=3)
    own, _ = forecast(registry, b, train.isel(week=slice(107, 110)), periods=3)
    for level in (0.05, 0.5, 0.95):
        np.testing.assert_allclose(q(own, level)[3:], q(plain, level), rtol=RTOL)
    new = raw.isel(week=slice(110, 112)).copy(deep=True)
    for value, other in ((8.0, 18.0),):
        low, high = new.copy(deep=True), new.copy(deep=True)
        low["demand"].values[:] = value
        high["demand"].values[:] = other
        f_low, _ = forecast(registry, b, low, periods=3)
        f_high, _ = forecast(registry, b, high, periods=3)
        assert (q(f_high, 0.5)[2:] > q(f_low, 0.5)[2:] + 2.0).all()


# ===================================================================================== panels


def test_panel_series_are_conditioned_on_their_own_ragged_actuals(registry: Registry) -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data = sales_dataset(periods=86, geos=["north", "south"], seed=6)
    train = data.isel(date=slice(0, 76))
    b = fit(
        registry,
        "ts.sarimax",
        "{order: [1, 0, 0], exog: [price, promo], series_dims: [geo]}",
        train,
        sales_schema(geo=True),
    )
    payload = data.isel(date=slice(76, 82)).copy(deep=True)
    # north: 3 actuals then 3 periods to forecast; south: 1 actual then 5
    payload["sales"].loc[{"geo": "north", "date": payload["date"].values[3:]}] = np.nan
    payload["sales"].loc[{"geo": "south", "date": payload["date"].values[1:]}] = np.nan
    fc, record = forecast(registry, b, payload, periods=6)
    cols = ["price", "promo"]
    for gi, (geo, k) in enumerate((("north", 3), ("south", 1))):
        cell_tr = train.sel(geo=geo)
        cell_new = data.isel(date=slice(76, 76 + k)).sel(geo=geo)
        cell_fut = data.isel(date=slice(76 + k, 82)).sel(geo=geo)
        _, est = params_of(b, gi, geo=geo)
        direct = SARIMAX(
            series(cell_tr["sales"].values, 0),
            exog=frame(cell_tr, cols, 0),
            order=(1, 0, 0),
            trend="c",
        ).filter(est)
        upd = direct.append(
            series(cell_new["sales"].values, 76), exog=frame(cell_new, cols, 76), refit=False
        )
        ref = upd.get_forecast(6 - k, exog=frame(cell_fut, cols, 76 + k))
        got = q(fc, 0.5, geo=geo)[k:]
        np.testing.assert_allclose(got, np.asarray(ref.predicted_mean), rtol=RTOL)
    by = {s["series"]["geo"]: s for s in record["series"]}
    assert by["north"]["n_actuals"] == 3 and by["north"]["n_forecast"] == 3
    assert by["south"]["n_actuals"] == 1 and by["south"]["n_forecast"] == 5


# ============================================================== transformed (pipeline) data


def test_a_log_pipeline_conditions_in_model_space_and_answers_in_original_units(
    registry: Registry, sales: xr.Dataset
) -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data, schema = only(sales)
    train, new = data.isel(date=slice(0, 80)), data.isel(date=slice(80, 83))
    pipeline = "pipeline:\n- {id: log, use: core.log1p@^1, with: {vars: [y]}}"
    b = fit(registry, "ts.sarimax", "{order: [1, 0, 0]}", train, schema, pipeline=pipeline)
    fc, _ = forecast(registry, b, new, periods=4)
    _, est = params_of(b)
    direct = SARIMAX(series(np.log1p(train["sales"].values), 0), order=(1, 0, 0), trend="c").filter(
        est
    )
    ref = direct.append(series(np.log1p(new["sales"].values), 80), refit=False).get_forecast(4)
    np.testing.assert_allclose(q(fc, 0.5)[3:], np.expm1(np.asarray(ref.predicted_mean)), rtol=RTOL)
    np.testing.assert_allclose(
        q(fc, 0.95)[3:], np.expm1(np.asarray(ref.conf_int(alpha=0.1))[:, 1]), rtol=RTOL
    )


# =========================================================== properties, every plugin example


def _extend_dim(case: Example) -> str:
    return next(d for d, spec in case.schema.dims.items() if spec.policy.value == "extend")


def _target(case: Example) -> str:
    return next(v for v, spec in case.schema.vars.items() if spec.role.value == "target")


def _time_of(da: xr.DataArray, case: Example) -> str:
    dim = _extend_dim(case)
    return dim if dim in da.dims else "t"


@pytest.mark.parametrize("model_id", sorted(examples()))
def test_the_fits_own_last_values_reproduce_the_plain_forecast(
    model_id: str, registry: Registry, bundles: dict[str, Bundle]
) -> None:
    case = examples()[model_id]
    b, tdim = bundles[model_id], _extend_dim(case)
    n = case.data.sizes[tdim]
    plain, plain_rec = forecast(
        registry, b, None, periods=4, future_covariates="carry_last", history=case.data
    )
    own, rec = forecast(
        registry,
        b,
        case.data.isel({tdim: slice(n - 3, n)}),
        periods=4,
        future_covariates="carry_last",
        history=case.data,
    )
    assert plain_rec["conditioned"] is False
    assert rec["conditioned"] is True and rec["revised"] == rec["n_actuals"]
    t = _time_of(own, case)
    tail = own.sel({t: plain[t].values})
    for level in (0.05, 0.5, 0.95):
        np.testing.assert_allclose(
            q(tail, level), q(plain.transpose(*tail.dims), level), rtol=RTOL, atol=1e-9
        )


def _next_label(labels: np.ndarray) -> Any:
    """The period after the last of ``labels`` (by their frequency for dates)."""
    if labels.dtype.kind == "M":
        idx = pd.DatetimeIndex(labels)
        freq = pd.infer_freq(idx)
        assert freq is not None
        return pd.date_range(idx[-1], periods=2, freq=freq).as_unit("ns").values[1]
    return labels[-1] + (labels[-1] - labels[-2])


@pytest.mark.parametrize("model_id", sorted(examples()))
def test_a_jump_in_the_actuals_moves_the_forecast(
    model_id: str, registry: Registry, bundles: dict[str, Bundle]
) -> None:
    """A new actual far from the last fitted value changes the forecast after it (in one
    direction at least: a regime model already sure of the high regime does not move on a
    jump up). For univariate models a jump up raises the next forecast unless the model
    has regimes."""
    case = examples()[model_id]
    b, tdim, y = bundles[model_id], _extend_dim(case), _target(case)
    n = case.data.sizes[tdim]
    label = _next_label(case.data[tdim].values)
    base = case.data.isel({tdim: [n - 1]}).assign_coords({tdim: [label]})
    spread = 10.0 * float(np.nanstd(case.data[y].values))
    kw: dict[str, Any] = {"periods": 3, "future_covariates": "carry_last", "history": case.data}
    calm, _ = forecast(registry, b, base, **kw)
    t = _time_of(calm, case)
    ahead = calm[t].values[1:]
    moves = {}
    for sign in (1.0, -1.0):
        jump = base.copy(deep=True)
        jump[y] = jump[y] + sign * spread
        moved, _ = forecast(registry, b, jump, **kw)
        moves[sign] = q(moved.sel({t: ahead}), 0.5) - q(calm.sel({t: ahead}), 0.5)
    assert max(np.abs(d).max() for d in moves.values()) > 1e-3, "the actual was ignored"
    if model_id not in ("ts.var", "ts.vecm", "ts.varmax", "ts.dynamic_factor"):
        if model_id != "ts.markov_switching":
            assert float(moves[1.0].reshape(-1)[0]) > 0
        assert float(moves[-1.0].reshape(-1)[0]) < 0
