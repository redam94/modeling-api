# Forecasts with actuals

You fit a model one time, but new numbers continue to come. For example, the sales of last
month arrive, or a preliminary figure gets a revision. A forecast request can send these
**actuals** together with the **dates to forecast**. The model updates its state with the
actuals and the parameters of the fit. It does not estimate the parameters again. Then it
forecasts from the last actual. Thus, you do not have to fit again to move the forecast origin
forward.

The model does not ignore the data that you send. If a model cannot use actuals, it refuses
the request with `ACTUALS_NOT_SUPPORTED`. Each forecast answer tells which data the model
conditioned on.

## Sending actuals and future dates

Use `mode: forecast` with a payload (`data`, or an inline dataset on an endpoint). The payload
must have the target along the time dim. For each series:

* A period with a **target value** is an actual.
* A period with an **empty target** (`null`, a blank cell, NaN) after the last actual is a
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

This request updates the airline model with January to March 1961, and forecasts April and
May. In a spreadsheet, the Input sheet of the endpoint is the same table. It has one row for
each month. Fill the target column where you have actuals. Leave it blank where you want a
forecast. The driver columns are next to the target column.

| t | y | price |
|---|---|---|
| 1961-01-01 | 417 | 10.5 |
| 1961-02-01 | 391 | 10.5 |
| 1961-03-01 | 419 | 10.9 |
| 1961-04-01 | | 11.0 |
| 1961-05-01 | | |

`GET /v1/endpoints/{alias}/schema` gives `actuals_example` for endpoints with a model that can
do this. It is a call with inline data for the periods after the fit, with all values empty,
for you to fill. The schema also gives `input_table` for all endpoints. It is the same table as
columns: the time column first, then the target, the drivers and the other inputs. Each column
tells what it contains. For a forecast endpoint, the table also has the periods after the fit as
empty rows. All clients read that layout:

| From | How |
|---|---|
| Google Sheets | The **Input** sheet of the generated Apps Script ([Connect Google Sheets](../tutorials/google-sheets.md#5-add-your-latest-actuals-and-future-dates)). A second function makes the sheet |
| Python | `api.endpoints.forecast(alias, rows)` with a DataFrame or rows, or `actuals=` and `future=` ([The Python client](../guides/python-client.md#forecast-with-your-latest-actuals)) |
| The terminal | `mapi endpoints forecast ALIAS input.csv` ([The command line](../guides/cli.md#endpoints-webhooks-and-sharing)) |
| The web app | The **Schema** tab of the endpoint: *Your latest actuals and the dates to forecast*, then **Use in the call** |
| curl, TypeScript, R | The snippets of the endpoint add the call for the periods after the fit, for you to fill |

## The rules

**The source of the forecast periods.** If the payload has periods after the last actual, the
model forecasts only those periods. It does not use `horizon.periods`. If the two are different,
a warning in `issues` tells you. If the payload ends with an actual, the model forecasts
`horizon.periods` periods after it. If the actuals only revise fitted periods, the forecast
starts after the end of the fit. The fitted periods between them keep their values and get
one-step-ahead predictions. Some payloads have no period after the fit and no actual: no rows,
or only fitted periods with an empty target. For these payloads, the model forecasts
`horizon.periods` periods after the fit, as for a call without data. `horizon.dim` always
gives the time dim.

**Actuals continue the fitted history.** The first actual can be the period immediately after
the fit, or an earlier period. Actuals in the fitted range **replace** the fitted values for
those periods (revised data), and the forecast conditions on the revision. An empty target in
the fitted range keeps the fitted value. An empty target *between* the end of the fit and the
last actual is a gap, and gives `ACTUALS_INVALID`. The exception is a model that accepts missing
observations (its input schema accepts a missing target, as the state-space models do). This
model filters the gap as missing data. Empty targets in the fitted range are never gaps. Thus,
all models accept revisions of separate fitted periods, with or without new actuals after the
fit. Coordinates that skip periods give `NON_CONTIGUOUS_EXTENSION`, as for all score requests.

**Only the values that you send are actuals.** Some pipeline steps fill missing targets. An
example is `core.impute` with `ffill` or `mean`, which fills the gaps of the fitted history.
Such a step never changes an empty target that you send into an actual. The model forecasts
the periods that you leave blank.

**Drivers.** A model with drivers needs them on the actual periods after the fit. These are
the values that occurred with the actuals. If they are missing, the result is
`ACTUALS_INVALID`. On the periods to forecast, empty drivers follow `future_covariates`:

- `supplied` (the default) refuses them (`MISSING_FUTURE_COVARIATE`).
- `carry_last`, `seasonal_naive` and `zero` fill them from the fitted history *and the drivers
  of the actual periods*. Thus, `carry_last` repeats the latest value that you sent.

The model never changes the values that you send.

**Panels.** For a univariate model, each series conditions on its own actuals and forecasts
from its own last actual. (A series is a cell of the other dims of the target, for example a
store or a geo.) Thus, the series can end at different dates. Series that the model models
jointly are one system: the variables of `ts.var`, `ts.vecm`, `ts.varmax` and
`ts.dynamic_factor`. Each of these series needs a value on each actual period after the fit.
If not, the result is `ACTUALS_INVALID`.

**The parameters do not change.** Conditioning is a filter, not a fit. For a state-space model,
it is `results.append(new, refit=False)` of statsmodels (or `apply` when actuals revise the
fitted range). For an autoregression, it is the new lags. Fit the model again at intervals, so
that the parameters use the new data.

## What the answer says

Each forecast has a `conditioning` record. It is in the inline summary
(`ScoreCallResult.summary.conditioning`), in the `result.json` of the run and in the root
attribute `conditioning` of the result tree:

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

The summary table covers all the periods that you sent and the added horizon. On the actual
periods, it shows one-step-ahead predictions (how well the model predicts each period). On the
other periods, it shows the forecast. A forecast without actuals gives `"conditioned": false`
and the end of the fitted history where it started.

## Which models can

All the built-in [statsmodels models](../models/statsmodels.md#forecasting-from-new-actuals)
can do this (`ts.sarimax`, `ts.arima`, `ts.unobserved_components`, `ts.ets`, `ts.theta`,
`ts.ardl`, `ts.ecm`, `ts.var`, `ts.vecm`, `ts.varmax`, `ts.dynamic_factor`,
`ts.markov_switching`). This includes the models that the `ts_readiness_arimax` workflow fits.
The PyMC time-series models (`ts.hsgp_trend`, `ts.gaussian_hmm`), the marketing-mix models and
the tabular models refuse. As an alternative, `ts.gaussian_hmm` runs its forward filter on new
observations with `mode: filter`.

A model declares this with `capabilities.conditions_on_actuals` (see
[Capabilities](capabilities.md)). `GET …/schema` shows it as `snapshot.conditions_on_actuals`.
The check uses the plugin build that scores. Thus, a model that you fitted before its plugin
could condition can do it now, without a new fit. Some promotions move a version that cannot
condition onto an endpoint with a version that can. This is a breaking change for the callers
of the endpoint. The promotion gate counts it together with the input-schema changes, under the
`schema_change` policy of the endpoint (blocked by default). All other models refuse a forecast
with a target value in the payload:

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

Only `forecast` requests read targets as actuals. `predict` and `evaluate` payloads have
targets for other reasons (metrics, features), and this function does not change them. If a
`predict` payload has target values after the fitted history, a warning in `issues` tells you
that the model does not use them. To condition on them, use `mode: forecast`.

**Models fitted in a transformed space.** Some workflow models fit on the output of
`transform.log`, `transform.box_cox` or `transform.difference`. These transforms do not have an
equivalent preprocessing step, so the endpoint cannot apply them to the data that you send.
Such a model has the label `mapi.io/data-space: transformed`. It refuses actuals with
`ACTUALS_NOT_SUPPORTED`, because it would read them as transformed values. Its
`snapshot.conditions_on_actuals` is false. Each answer has a warning that the predictions are
in the transformed space. To prevent this, put the transform in the model (an ARIMA order
`d`). Or put it in the pipeline of the model (`core.log1p` through `transform.steps`). Then
the model transforms the actuals, and the forecasts come back in the units of the data.

| Code | HTTP | Raised when | What to do |
|---|---|---|---|
| `ACTUALS_NOT_SUPPORTED` | 422 | The payload has target values and the model cannot condition on them | Fit again with the new data, or leave the target empty |
| `ACTUALS_INVALID` | 422 | Actuals leave a gap after the fit (and the model needs a complete history), or actuals after the fit do not have their drivers | Fill the gap, or send only the actuals before it. Send the drivers |
| `MISSING_FUTURE_COVARIATE` | 422 | A driver is empty on a period to forecast and `future_covariates` is `supplied` | Fill it, or set `future_covariates` to `carry_last`, `seasonal_naive` or `zero` |
| `SCHEMA_INVALID` | 422 | A target or driver cell has text where the schema needs a number (`"#N/A"`, or a number as text from a client that sends it with no change) | Send numbers. Leave the cell empty for no value |

The API checks inline payloads before it starts a run. It does a null check of the JSON for
the target and for the drivers that a forecast needs. It also does a type check for text. (The
platform never decodes values outside the sandbox.) These checks see the union of all series.
The sandbox checks each series again. Thus, a problem in only one series of a panel causes the
run to fail with the same code. An example is one store without its driver on an actual month.
In this case, the answer is `200` with the problem in `run.problem`, not a `422`.

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

`ForecastActuals` (`mapi_sdk.actuals`) has these attributes:

- `dim`, `fit_end` and `targets`.
- The boolean mask `observed`: a supplied actual. In the fitted range, it replaces the fitted
  value.
- The boolean mask `future`: a period to forecast.
- The derived `revised`, `gap` and `last_actual()`.
- The `conditioning` record of the answer.

Return the `predictions` group for all periods of `ds`, as `score` does. Before the hook, the
harness refuses the requests that the model cannot do. It also adds the horizon, fills the
future drivers and checks the gaps and the drivers. The default implementation refuses. Thus,
a plugin that declares the capability but has no hook never ignores actuals. `toy.ar1` in
`mapi_sdk.testing.toy` is a small reference. The `forecast_actuals` check of the contract kit
makes sure of two things. Conditioning on the fitted values gives the plain forecast again,
and a new actual changes the forecast.
