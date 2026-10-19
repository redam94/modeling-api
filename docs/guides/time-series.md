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

## Missing values and sentinels

Raw series often use numbers for gaps (`-99.99`). Declare the code in the schema
(`missing_sentinel`). Then change it into NaN with `core.replace_sentinel`. Some models can
accept gaps (for example `ts.hsgp_trend`). These models do not use those periods in the
likelihood, but they predict them.

For all settings, see [Built-in models](../reference/models.md). For the fixtures (`c08`, `e24`)
that keep this behavior tested, see the [Conformance corpus](conformance.md).
