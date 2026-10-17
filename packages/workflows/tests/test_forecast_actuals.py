"""A model fitted by the ``ts_readiness_arimax`` workflow forecasts from actuals sent with
the request (:mod:`mapi_spec.actuals`).

The workflow difference the series inside the model (``order[1]`` and the seasonal ``D``
of SARIMAX), so the actuals are sent - and the forecast comes back - in the original
units; the fitted parameters are applied to them without re-estimation.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_plugin_statsmodels import SarimaxSettings
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_spec import load_score_request
from mapi_spec.schema import DataSchema
from mapi_spec.workflow import WorkflowRunStatus
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX, instantiate_template
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.runtime.values import DatasetValue, ModelValue
from wf_ctx import run_node
from wf_models import registry, weekly_series

RTOL = 1e-6
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://default/ts-forecast\n"


def monthly_counts(n: int = 150, seed: int = 3) -> xr.Dataset:
    """Monthly int64 counts from 1949-01 with a trend and a yearly season (airline-like),
    ``y(t)`` with ``freq: MS`` - the shape of the series behind the reported endpoint."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    level = 4.8 + 0.01 * t + np.cumsum(rng.normal(0, 0.02, n))
    y = np.exp(level + 0.15 * np.sin(2 * np.pi * t / 12) + rng.normal(0, 0.02, n))
    date = pd.date_range("1949-01-01", periods=n, freq="MS").as_unit("ns")
    ds = xr.Dataset({"y": (("t",), np.round(y).astype(np.int64))}, coords={"t": date})
    schema = DataSchema.model_validate(
        {
            "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
            "vars": {"y": {"dims": ["t"], "dtype": "int64", "role": "target"}},
        }
    )
    ds.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    return ds


def forecast(bundle: Bundle, payload: xr.Dataset | None) -> tuple[xr.DataArray, dict[str, Any]]:
    """The reported call: forecast 8 months, median and 90% band."""
    req = load_score_request(
        HEADER + "mode: forecast\nhorizon: {dim: t, periods: 8}\nfuture_covariates: carry_last\n"
    )
    tree = score_bundle(bundle, req, payload, registry=registry(), return_full=True)
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"], json.loads(str(tree.attrs["conditioning"]))


def test_a_workflow_fitted_sarimax_forecasts_from_new_actuals() -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data = monthly_counts()
    train = data.isel(t=slice(0, 144))  # 1949-01 .. 1960-12
    spec = instantiate_template(TS_READINESS_ARIMAX, name="ts-forecast", project="default")
    params = {"season": 12, "n_folds": 3, "horizons": [1, 4, 8], "max_p": 2, "max_q": 1}
    res = LocalRunner(registry=registry()).run(spec, inputs={"data": train}, params=params)
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    model = res.node_values["fit"]["model"]
    assert isinstance(model, ModelValue)
    bundle = model.bundle
    settings = SarimaxSettings.model_validate(bundle.model.settings)
    assert settings.order[1] + settings.effective_seasonal()[1] >= 1  # differenced in the model

    plain, plain_record = forecast(bundle, None)
    assert plain_record["conditioned"] is False

    new = data.isel(t=slice(144, 147))  # actuals for 1961-01 .. 1961-03
    fc, record = forecast(bundle, new)
    months = pd.date_range("1961-01-01", periods=11, freq="MS").as_unit("ns").values
    np.testing.assert_array_equal(fc["t"].values, months)
    assert record["conditioned"] is True
    assert record["n_actuals"] == 3
    assert record["actuals_through"].startswith("1961-03-01")
    assert record["horizon_source"] == "horizon"
    assert "1961-04-01" in record["message"] and "1961-11-01" in record["message"]

    # statsmodels with the fitted parameters on the history plus the actuals
    names = [str(p) for p in bundle.model.learned["cells"][0]["params"]]
    est = bundle.model.groups["params"]["estimate"].sel(param=names).values.astype(float)
    y = pd.Series(np.asarray(train["y"].values, dtype=float), name="y")
    direct = SARIMAX(
        y,
        order=settings.order,
        seasonal_order=settings.effective_seasonal(),
        trend=settings.effective_trend(),
    ).filter(est)
    new_y = pd.Series(
        np.asarray(new["y"].values, dtype=float), index=pd.RangeIndex(144, 147), name="y"
    )
    ref = direct.append(new_y, refit=False).get_forecast(8)
    median = np.asarray(fc.sel(quantile=0.5).values, dtype=float)
    np.testing.assert_allclose(median[3:], np.asarray(ref.predicted_mean), rtol=RTOL)
    q95 = np.asarray(fc.sel(quantile=0.95).values, dtype=float)
    np.testing.assert_allclose(q95[3:], np.asarray(ref.conf_int(alpha=0.1))[:, 1], rtol=RTOL)

    # the reported symptom: wild actuals used to give the same numbers - now they count
    wild = new.copy(deep=True)
    wild["y"].values[:] = [9999, 1, 50000]
    moved, _ = forecast(bundle, wild)
    assert not np.allclose(np.asarray(moved.sel(quantile=0.5).values)[3:], median[3:])
    # the fit's own last months as actuals give the plain forecast
    own, _ = forecast(bundle, train.isel(t=slice(141, 144)))
    np.testing.assert_allclose(
        np.asarray(own.sel(quantile=0.5).values)[3:],
        np.asarray(plain.sel(quantile=0.5).values),
        rtol=RTOL,
    )


def test_a_transform_step_in_the_workflow_is_replayed_on_the_actuals() -> None:
    """``transform.steps`` (log1p of the target) becomes the bundle's first pipeline step:
    actuals in the original units are transformed before conditioning and the forecast is
    transformed back."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data = weekly_series(110, seed=5)
    train, new = data.isel(time=slice(0, 100)), data.isel(time=slice(100, 103))
    future = data.isel(time=slice(103, 106)).copy(deep=True)
    future["sales"].values[:] = np.nan
    logged = run_node(
        "transform.steps",
        {"steps": [{"id": "lp", "use": "core.log1p@^1", "with": {"vars": ["sales"]}}]},
        data=DatasetValue(data=train),
        registry=registry(),
    ).outputs["data"]
    fitted = run_node(
        "model.fit",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=logged,
        registry=registry(),
    ).outputs["model"]
    assert isinstance(fitted, ModelValue)
    bundle = fitted.bundle
    assert [s.use.id for s in bundle.spec.pipeline] == ["core.log1p"]

    req = load_score_request(HEADER + "mode: forecast\nhorizon: {dim: t, periods: 3}\n")
    payload = xr.concat([new, future], dim="time")
    tree = score_bundle(bundle, req, payload, registry=registry(), return_full=True)
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    fc = node.to_dataset()["y_hat"]
    record = json.loads(str(tree.attrs["conditioning"]))
    assert record["n_actuals"] == 3 and record["horizon_source"] == "rows"

    names = [str(p) for p in bundle.model.learned["cells"][0]["params"]]
    est = bundle.model.groups["params"]["estimate"].sel(param=names).values.astype(float)
    x = pd.DataFrame({"price": np.asarray(train["price"].values, dtype=float)})
    direct = SARIMAX(
        pd.Series(np.log1p(np.asarray(train["sales"].values, dtype=float)), name="y"),
        exog=x,
        order=(1, 0, 0),
        trend="c",
    ).filter(est)
    idx_new, idx_future = pd.RangeIndex(100, 103), pd.RangeIndex(103, 106)
    upd = direct.append(
        pd.Series(np.log1p(new["sales"].values), index=idx_new, name="y"),
        exog=pd.DataFrame({"price": new["price"].values}, index=idx_new),
        refit=False,
    )
    ref = upd.get_forecast(
        3, exog=pd.DataFrame({"price": future["price"].values}, index=idx_future)
    )
    median = np.asarray(fc.sel(quantile=0.5).values, dtype=float)[3:]
    np.testing.assert_allclose(median, np.expm1(np.asarray(ref.predicted_mean)), rtol=RTOL)
    assert 50 < float(median.mean()) < 200  # original units, not log units
