# Forecasting and sequential models

Time is where most modelling platforms quietly cut corners: a forecast that silently runs
past what the model can represent, a state-space model that restarts from scratch at every
score, a holdout scored without the history it depends on. modeling-api makes each of these
explicit and checks them before any compute is spent.

!!! tip "Classical models"
    ARIMA/ARIMAX, exponential smoothing, theta, ARDL and error correction, VAR, VECM,
    VARMAX, dynamic factors and Markov switching come from statsmodels; see
    [statsmodels time-series models](../models/statsmodels.md).

## Horizons are part of the contract

A time dim declares its policy `extend` and, optionally, a `max_extension`: how far past the
fitted range a forecast may go without a refit. The control plane checks every score request
against it from metadata alone:

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

Models that need a bounded domain take it from the same place. `ts.hsgp_trend` sizes its
Hilbert-space GP from `ctx.anticipated_coords` (the fitted labels plus `max_extension`
future labels), so the basis it evaluates at a forecast month is exactly the basis it was
fitted with:

```python
dom = HSGPDomain.from_positions(positions_of(ctx.anticipated_coords["t"]), s.c)
phi = hsgp_basis(dom.scale(ctx.position("t")), s.m, dom.L)   # same at fit and at score
```

Score payloads must also continue the fitted range without gaps
(`NON_CONTIGUOUS_EXTENSION`) and may not start before it (`COORD_NOT_ALLOWED`).

## Evaluate the way you will forecast

With `fit.mode: evaluate_then_refit` (the default for promotable models) the platform fits on
the training window, scores the holdout **through the score path** (with the context and
state from training only), records the metrics, then refits on everything so forecasts start
from the latest data. The production bundle links the evaluation bundle and its metrics.

## Sequential models keep a terminal state

Hidden Markov models, Kalman filters and particle filters summarise the past in a state.
They store it in a plugin-owned `terminal_state` group and read it at score time instead of
replaying history. `ts.gaussian_hmm` stores the filtered state distribution at the last
fitted step, `alpha_T(chain, draw, animal, state)`:

* `forecast` propagates it: `P(z_{T+h}) = alpha_T · A^h`, per posterior draw;
* `filter` continues the forward filter over new observations and returns an updated
  `terminal_state` group, so filtering six new steps at once or in two batches of three gives
  the same state.

```yaml
apiVersion: mapi/v1
kind: ScoreRequest
model: endpoint://ecology/animal-tracks-hmm
mode: filter
data: dataset://ecology/tracks_live@v1
```

The result has `predictions` (one-step-ahead `y_hat` and filtered `state_prob`) and a
`terminal_state` group with `attrs.step` set to the last filtered step.

## Missing values and sentinels

Raw series often encode gaps as numbers (`-99.99`). Declare the code in the schema
(`missing_sentinel`) and turn it into NaN with `core.replace_sentinel`; models that support
gaps (such as `ts.hsgp_trend`) leave those periods out of the likelihood and still predict
them.

See [Built-in models](../reference/models.md) for every setting, and the
[Conformance corpus](conformance.md) for the fixtures (`c08`, `e24`) that keep this behaviour
tested.
