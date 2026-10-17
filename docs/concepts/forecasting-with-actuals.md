# Forecasts with actuals

A model is fitted once, but the numbers keep coming: last month's sales arrive, a preliminary
figure is revised. A forecast request can carry those **actuals** together with the **dates
to forecast**. The model updates its state with the actuals using the parameters it was fitted
with - nothing is re-estimated - and forecasts from the last actual. You do not need to refit
to move the forecast origin forward.

Nothing you send is ignored: a model that cannot use actuals refuses the request with
`ACTUALS_NOT_SUPPORTED`, and every forecast answer says what it was conditioned on.

## Sending actuals and future dates

Use `mode: forecast` with a payload (`data`, or an inline dataset on an endpoint) that holds
the target along the time dim. Per series:

* a period whose **target has a value** is an actual;
* a period whose **target is empty** (`null`, a blank cell, NaN) after the last actual is a
  period to forecast.

```json
{
  "mode": "forecast",
  "horizon": {"dim": "t", "periods": 8},
  "future_covariates": "carry_last",
  "output": {"summary": ["median", "q5", "q95"]},
  "data": {
    "coords": {"t": {"dims": ["t"], "data": ["1961-01-01", "1961-02-01", "1961-03-01",
                                            "1961-04-01", "1961-05-01"]}},
    "data_vars": {"y": {"dims": ["t"], "data": [417, 391, 419, null, null]}}
  }
}
```

This updates the airline model with January to March 1961 and forecasts April and May. In a
spreadsheet, the endpoint's Input sheet is the same table: one row per month, the target
column filled where you have actuals and left blank where you want a forecast, driver columns
next to it.

| t | y | price |
|---|---|---|
| 1961-01-01 | 417 | 10.5 |
| 1961-02-01 | 391 | 10.5 |
| 1961-03-01 | 419 | 10.9 |
| 1961-04-01 | | 11.0 |
| 1961-05-01 | | |

`GET /v1/endpoints/{alias}/schema` returns `actuals_example` for endpoints whose model can do
this: a call with inline data over the periods after the fit, every value empty, to fill in.
It also returns `input_table` for every endpoint: the same table as columns (the time column
first, then the target, the drivers and other inputs, each with what it holds) and, for a
forecasting endpoint, the periods after the fit as empty rows. Every client reads that layout:

| From | How |
|---|---|
| Google Sheets | The **Input** sheet of the generated Apps Script ([Connect Google Sheets](../tutorials/google-sheets.md#5-add-your-latest-actuals-and-future-dates)); a second function creates it |
| Python | `api.endpoints.forecast(alias, rows)` with a DataFrame or rows, or `actuals=` and `future=` ([The Python client](../guides/python-client.md#forecast-with-your-latest-actuals)) |
| The terminal | `mapi endpoints forecast ALIAS input.csv` ([The command line](../guides/cli.md#endpoints-webhooks-and-sharing)) |
| The web app | The endpoint's **Schema** tab: *Your latest actuals and the dates to forecast*, then **Use in the call** |
| curl, TypeScript, R | The endpoint's snippets add the call over the periods after the fit, to fill in |

## The rules

**Where the forecast periods come from.** When the payload has periods after the last actual,
exactly those are forecast; `horizon.periods` is not used (a warning in `issues` says so when
it differs). When the payload ends with an actual, `horizon.periods` periods after it are
forecast - or after the end of the fit when the actuals only revise fitted periods (the
fitted periods in between keep their values and get one-step-ahead predictions). `horizon.dim` always names the time dim.

**Actuals continue the fitted history.** The first actual may be the period right after the
fit, or earlier: actuals inside the fitted range **replace** the fitted values for those
periods (revised data) and the forecast is conditioned on the revision; an empty target there
keeps the fitted value. An empty target *between* the end of the fit and the last actual is a
gap: `ACTUALS_INVALID`, unless the model accepts missing observations (its input schema allows
a missing target - the state-space models do), which then filters the gap as missing.
Coordinates that skip periods are `NON_CONTIGUOUS_EXTENSION`, as for any score request.

**Drivers.** A model with drivers needs them on the actual periods after the fit (the values
observed with the actuals; `ACTUALS_INVALID` otherwise). On the periods to forecast, empty
drivers follow `future_covariates`: `supplied` (the default) refuses them
(`MISSING_FUTURE_COVARIATE`); `carry_last`, `seasonal_naive` and `zero` fill them - from the
fitted history *and the drivers of the actual periods*, so `carry_last` repeats the latest value
you sent. Values you send are never changed.

**Panels.** Each series (a cell of the target's other dims: a store, a geo, a variable of a
VAR) is conditioned on its own actuals and forecast from its own last actual, so series may end
at different dates.

**Parameters stay fixed.** Conditioning is filtering, not fitting: for a state-space model it is
statsmodels' `results.append(new, refit=False)` (or `apply` when actuals revise the fitted
range); for an autoregression the new lags. Refit from time to time so the parameters see the
new data.

## What the answer says

Every forecast carries a `conditioning` record, in the inline summary
(`ScoreCallResult.summary.conditioning`), in the run's `result.json` and in the result tree's
root attribute `conditioning`:

```json
{
  "dim": "t",
  "fit_end": "1960-12-01",
  "conditioned": true,
  "n_actuals": 3,
  "actuals_through": "1961-03-01",
  "revised": 0,
  "series_conditioned": 1,
  "n_series": 1,
  "horizon_source": "rows",
  "periods": 2,
  "parameters": "fitted",
  "series": [{"series": {}, "n_actuals": 3, "first_actual": "1961-01-01",
              "actuals_through": "1961-03-01", "revised": 0, "missing": 0,
              "origin": "1961-03-01", "forecast_from": "1961-04-01",
              "forecast_to": "1961-05-01", "n_forecast": 2}],
  "message": "Forecast updated with 3 actuals (1961-01-01 to 1961-03-01) after the fitted history, which ends 1960-12-01; the model's parameters were not re-estimated. 2 periods forecast from 1961-04-01 to 1961-05-01."
}
```

The summary table covers every period you sent plus the appended horizon: one-step-ahead
predictions on the actual periods (how well the model would have predicted each one), the
forecast on the rest. A forecast without actuals reports `"conditioned": false` and the end of
the fitted history it started from.

## Which models can

Among the built-in models, every [statsmodels model](../models/statsmodels.md#forecasting-from-new-actuals)
does (`ts.sarimax`, `ts.arima`, `ts.unobserved_components`, `ts.ets`, `ts.theta`, `ts.ardl`,
`ts.ecm`, `ts.var`, `ts.vecm`, `ts.varmax`, `ts.dynamic_factor`, `ts.markov_switching`) -
including those fitted by the `ts_readiness_arimax` workflow. The PyMC time-series models
(`ts.hsgp_trend`, `ts.gaussian_hmm`), the marketing-mix models and the tabular models refuse;
`ts.gaussian_hmm` runs its forward filter over new observations with `mode: filter` instead.

A model declares it with `capabilities.conditions_on_actuals` (see
[Capabilities](capabilities.md)); `GET …/schema` shows it as `snapshot.conditions_on_actuals`.
The check uses the plugin build that scores, so a model fitted before its plugin learned to
condition can do so without a refit. Promoting a version that cannot onto an endpoint whose
version can is a breaking change for the endpoint's callers: the promotion gate counts it with
the input-schema changes under the endpoint's `schema_change` policy (blocked by default).
Every other model refuses a forecast whose payload holds a target value:

```json
{
  "type": "urn:mapi:error:ACTUALS_NOT_SUPPORTED",
  "title": "The model cannot update its forecast with supplied actuals",
  "status": 422,
  "code": "ACTUALS_NOT_SUPPORTED",
  "errors": [{
    "code": "ACTUALS_NOT_SUPPORTED",
    "pointer": "/data",
    "message": "model 'ts.hsgp_trend@1.0.0' cannot update a forecast with actuals: the payload has values of the target 'y' for 3 periods (1961-01-01 to 1961-03-01). It would forecast from the end of its fitted history (1960-12-01) without them.",
    "suggested_action": "Refit the model with the new data to forecast from it, or leave the target empty (null) to forecast from the end of the fitted history (1960-12-01)."
  }]
}
```

Only `forecast` requests read targets as actuals: `predict` and `evaluate` payloads carry
targets for other reasons (metrics, features) and are unaffected.

| Code | HTTP | Raised when | What to do |
|---|---|---|---|
| `ACTUALS_NOT_SUPPORTED` | 422 | The payload has target values and the model cannot condition on them | Refit with the new data, or leave the target empty |
| `ACTUALS_INVALID` | 422 | Actuals leave a gap after the fit (and the model needs a complete history), or actuals after the fit lack their drivers | Fill the gap or send only the actuals before it; send the drivers |

The API checks inline payloads before starting a run (a null check of the JSON; values are
never decoded outside the sandbox) and the sandbox checks every series again.

## For plugin authors

Declare `conditions_on_actuals=True` and override `CustomModel.forecast_with_actuals`:

```python
def forecast_with_actuals(
    self,
    fitted: Fitted,
    ds: xr.Dataset,              # the canonicalised payload: actuals, periods to forecast, drivers
    actuals: ForecastActuals,    # which period is which, per series
    req: ScoreRequest,
    ctx: ScoreContext,
) -> xr.DataTree: ...
```

`ForecastActuals` (`mapi_sdk.actuals`) carries `dim`, `fit_end`, `targets`, the boolean masks
`observed` (a supplied actual; inside the fitted range it replaces the fitted value) and
`future` (a period to forecast), the derived `revised`, `gap` and `last_actual()`, and the
`conditioning` record the answer will carry. Return the `predictions` group over every period
of `ds`, as `score` does. The harness has already refused requests the model cannot serve,
appended the horizon, filled future drivers and checked gaps and drivers; the default
implementation refuses, so a plugin that claims the capability without the hook never ignores
actuals. `toy.ar1` in `mapi_sdk.testing.toy` is a small reference, and the contract kit's
`forecast_actuals` check verifies that conditioning on the fitted values reproduces the plain
forecast and that a new actual moves it.
