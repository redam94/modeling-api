# Forecasting and sequential models

Time causes many problems that most modelling platforms do not show. A forecast can silently go
past the range that the model can represent. A state-space model can start again from zero at
each score. A platform can score a holdout without the history that the holdout needs.
modeling-api makes each of these items explicit. It does a check of them before it uses compute.

!!! tip "Classical models"
    ARIMA/ARIMAX, exponential smoothing, theta, ARDL and error correction, VAR, VECM,
    VARMAX, dynamic factors and Markov switching come from statsmodels. See
    [statsmodels time-series models](../models/statsmodels.md).

## Horizons are part of the contract

A time dim declares its policy `extend` and, optionally, a `max_extension`. The
`max_extension` is the distance after the fitted range that a forecast can go without a new
fit. The control plane does a check of each score request against it. It uses only the metadata:

```yaml
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://climate/mauna-loa-co2
mode: forecast
horizon: {dim: t, periods: 30}
```

```json
{"code": "HORIZON_EXCEEDS_FIT", "status": 409,
 "detail": "horizon of 30 exceeds the 24 periods this fit supports along 't'; a refit is required"}
```

Models that need a bounded domain get it from the same place. `ts.hsgp_trend` sets the size of
its Hilbert-space GP from `ctx.anticipated_coords` (the fitted labels and `max_extension` future
labels). Thus the basis that it calculates at a forecast month is exactly the basis of the fit:

```python
dom = HSGPDomain.from_positions(positions_of(ctx.anticipated_coords["t"]), s.c)
phi = hsgp_basis(dom.scale(ctx.position("t")), s.m, dom.L)   # same at fit and at score
```

Score payloads must also continue the fitted range without gaps
(`NON_CONTIGUOUS_EXTENSION`). They cannot start before the fitted range (`COORD_NOT_ALLOWED`).

The runner does a check of the mode before it builds a forecast payload:

- If the model does not declare the mode in `capabilities.score_modes`, the runner refuses the
  request with `MODE_NOT_SUPPORTED`. The error lists the modes that the model supports.
- The labels of the horizon dim must be numbers or dates. A horizon along text labels (for
  example the names of images or areas) gives `COORD_NOT_ALLOWED`.
- A request without data, without a scenario dataset and without a horizon gives
  `SCHEMA_INVALID` ("the score request has no data"). Thus a model that has no `forecast` mode
  always needs data to score. The Score page of the web app shows **No new data** only for
  Forecast, or when a scenario dataset gives the values.

## Evaluate the way you will forecast

`fit.mode: evaluate_then_refit` is the default for models that you can promote. In this mode,
the platform does these steps:

1. It fits on the training window.
2. It scores the holdout **through the score path**, with the context and the state from the
   training only.
3. It records the metrics.
4. It fits again on all the data, so that forecasts start from the latest data.

The production bundle links the evaluation bundle and its metrics.

**MASE.** Add `mase` to `evaluate.metrics` to compare the holdout error with a seasonal naive
forecast. The fit divides the holdout `mae` by the mean absolute error of the naive forecast on the
training data. The season length comes from the frequency of the time dim (for example 52 for
weekly data). If the training data is shorter than one season, the naive forecast uses the value of
the period before. The naive error has the same units as the holdout error. For a pipeline that
aggregates the data, or for a model that sets its own output units (for example an MMM model),
these are the units of the model outputs. The fit calculates `mase` only when it has the training
data. Thus an evaluate run from the **Score** page skips `mase` and gives the reason in `skipped`.
A value less than 1 shows that the model is better than the naive forecast.

## Sequential models keep a terminal state

Hidden Markov models, Kalman filters and particle filters keep a summary of the past in a
state. They keep the state in a `terminal_state` group that the plugin owns. At score time,
they read this group. They do not process the history again. `ts.gaussian_hmm` keeps the
filtered state distribution at the last fitted step, `alpha_T(chain, draw, animal, state)`:

* `forecast` propagates it: `P(z_{T+h}) = alpha_T · A^h`, for each posterior draw.
* `filter` continues the forward filter over new observations. It returns an updated
  `terminal_state` group. Thus, if you filter six new steps at the same time or in two batches
  of three, you get the same state.

```yaml
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://ecology/animal-tracks-hmm
mode: filter
data: dataset://ecology/tracks_live@v1
```

The result has `predictions` (the one-step-ahead `y_hat` and the filtered `state_prob`). It also
has a `terminal_state` group. Its `attrs.step` is the last filtered step.

### A Bayesian structural time series <span class="mapi-badge">v0.13</span> { #bsts }

`ts.bsts` is a Kalman filter model: a level, a trend, seasonal patterns and drivers. It keeps
the state at the end of the fit in its `terminal_state` group, as `ts.gaussian_hmm` does. The
group has the mean and the covariance of the state for each kept draw (a maximum of 250
draws), and the matrices of the model.

* In the fitted range, `predict` gives the one-step-ahead prediction. This is the value that
  the model expected before it saw the period.
* `forecast` starts from the terminal state. The model propagates the state and adds the
  noise of each period. A forecast thus has a range that becomes wider with the horizon.
  The drivers need their future values.
* `filter` continues the Kalman filter over new periods. It returns the one-step-ahead
  predictions and an updated `terminal_state`. Then a forecast starts from the latest period
  without a new fit.

```yaml
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://retail/monthly-sales-bsts
mode: filter
data: dataset://retail/sales_last_3_months@v1
```

The model accepts periods without a value. The filter does not update the state in these
periods, and the prediction continues over them. The `core.decomposition` chart shows the
level, the seasonal parts and the effect of the drivers from the `components` group. See
[Causal models and Bayesian structural time series](../models/causal.md#tsbsts-bayesian-structural-time-series).
`causal.impact` uses the same model to measure the effect of a change.

## Plots of hidden states <span class="mapi-badge">v0.10</span> { #plots-of-hidden-states }

`ts.gaussian_hmm` and the statsmodels Markov switching model now include `core.state_sequence`
in their standard plots. The plot shows the observed series above the probability of each state
over time.

- For the HMM, the plot reads `state_prob` in the `hidden_states` group. The data has one series
  for each animal. The plot shows the first animal, and the caption names it. To show a
  different animal, set `select` in the plot builder, for example `{"animal": "a2"}`.
- For the Markov switching model, the plot reads `smoothed_prob` in the `regimes` group. The
  state dim is `regime`.
- A score run with a state output (for example `state_prob` of a `filter` score) also draws the
  plot from the score summary. A state output has a dim with the name `state` or `regime`, and
  its values are probabilities over that dim. A panel dim such as the `state` of a country is
  not a state output.

**Decomposition.** The unobserved components model, exponential smoothing (ETS) and
`ts.hsgp_trend` now include `core.decomposition` (`ts.bsts` too, from v0.13). The plot stacks the level, the seasonal part
and the other parts over time. The slope of a trend is a rate, not a part of the level. Thus the
plot does not stack it. An ETS model has a components group only when its error, trend and
season are additive.

## Missing values and sentinels

Raw series often use numbers for gaps (`-99.99`). Declare the code in the schema
(`missing_sentinel`). Then change it into NaN with `core.replace_sentinel`. Some models can
accept gaps (for example `ts.hsgp_trend`). These models do not use those periods in the
likelihood, but they predict them.

For all settings, see [Built-in models](../reference/models.md). For the fixtures (`c08`, `e24`)
that keep this behavior tested, see the [Conformance corpus](conformance.md).
