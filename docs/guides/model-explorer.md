---
description: Explore a fitted model - parameters, diagnostics, fit against actuals, residual tests, decomposition, response curves, impulse responses, forecasts, backtests and side-by-side comparison.
---

# Explore a model <span class="mapi-badge">v0.5</span>

The **Model Explorer** shows what a fitted model learned and how well it works, for every
model family: Bayesian (posterior means with highest-density intervals, R-hat), frequentist
(estimates with confidence intervals and p-values, information criteria) and machine learning
(coefficients or importances). Views are computed in the sandbox from the bundle and the data
it was fitted on, and come with Vega-Lite plots.

## Views written at fit

Every fit writes `params`, `diagnostics`, `fit`, `residuals`, `decomposition`,
`response_curves`, `irf` and `fevd` (sections that do not apply to the model are listed as
`skipped` with the reason). Read the index and the views through the model version's UI
route:

```bash
api "$API/v1/models/$MODEL_ID/versions/3/artifacts/ui/explore/index.json"
api "$API/v1/models/$MODEL_ID/versions/3/artifacts/ui/explore/residuals.json"
```

## Forecasts, backtests and more, on demand

```bash
api -X POST "$API/v1/models/$MODEL_ID/versions/3/explore" -d '{
  "sections": ["forecast", "backtest"],
  "horizon": 12,
  "backtest": {"kind": "expanding_window", "horizons": [1, 4, 12], "n_folds": 5,
               "min_train": 104}
}'
```

The answer (`202`) is an `Exploration`; poll `GET /v1/explorations/{run_id}` for the index and
signed view links. The same request again answers `200` from the last run.

## Compare model versions

```bash
api -X POST "$API/v1/models:compare" -d '{
  "models": ["model://growth/sales-arimax@v3", "model://growth/sales-ets@v1",
             "model://growth/sales-mmm@v2"]
}'
```

This explores the first version with the others as `compare_with` and writes the `comparison`
view: every version's holdout metrics side by side, a ranking per error metric (lower is
better), and pairwise **Diebold-Mariano** tests (with the Harvey-Leybourne-Newbold small-
sample correction) on the first version's held-out periods - "is the difference in accuracy
more than chance?". Add `"sections": ["comparison", "params"]` for more.

## The sections

| Section | What you see | Families |
|---|---|---|
| `params` | One row per parameter cell: mean, sd, interval (`hdi_90` Bayesian, `ci_90` otherwise), p-value, R-hat, bulk ESS | all with parameters |
| `diagnostics` | R-hat max, ESS min, divergences, optimiser status, AIC/BIC, holdout metrics, the plugin's diagnostics, warnings in plain words | all |
| `fit` | Actual against fitted with a 90% band, the split line at the first held-out period | models over time |
| `residuals` | Residuals over time, their ACF, Ljung-Box, Jarque-Bera, Breusch-Pagan and ARCH-LM tests, a histogram and a normal QQ plot | models over time |
| `decomposition` | The model's contributions over time (summed over markets) and each part's share | models with a `component` output (MMMs, linear models) |
| `response_curves` | Mean contribution per channel at each spend level with a 90% band | MMMs with a response grid |
| `irf` | Impulse responses (response × shock × horizon) with bands | VAR, VARMAX, VECM |
| `fevd` | Forecast-error variance decomposition | VAR, VARMAX, VECM |
| `forecast` | A forecast fan over `horizon` periods after the history, drivers held at their last values | models that forecast |
| `backtest` | Refits on a rolling-origin or expanding-window plan: metrics per fold and horizon, against "same as last period" and "same as last season" | models over time |
| `comparison` | Several versions side by side (above) | all |

## For plugin authors: what the explorer reads

The explorer is model-agnostic; plugins get these views by following a few conventions:

- **Predictions** come from the score path (`predict`, expected values), so every plugin has
  `fit` and `residuals` for free.
- **Parameters** are read from, in order: the `posterior` group (chain, draw); a `params` or
  `coefficients` group - either a *table* (variables `estimate`, optional `std_err`,
  `p_value`, `ci_lower`, `ci_upper` along one parameter dim) or one variable per parameter;
  or `learned["params"]` as `{name: number}` or `{name: {estimate, std_err, p_value,
  ci_lower, ci_upper}}`. Confidence intervals without explicit bounds are `estimate ± z ·
  std_err`.
- **Convergence**: diagnostics coded `RHAT`/`ESS_BULK`, `sample_stats.diverging`,
  `learned["converged"]` (a bool) or a `NOT_CONVERGED` diagnostic, and
  `learned["aic"|"bic"|"llf"]`.
- **Decomposition**: an output with a `component` dim (contributions).
- **Impulse responses**: a group or derived quantity `irf` with variable `irf` over
  `(response, shock, horizon)` (optional `lower`/`upper`), and `fevd` over
  `(variable, horizon, shock)`.
- **Response curves**: the `response_curves` derived quantity used by the what-if sliders.

The same reads power the governance checks (see
[Validate models with governance policies](governance.md)).
