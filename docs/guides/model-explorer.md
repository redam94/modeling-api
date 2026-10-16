---
description: Explore a fitted model - parameters, diagnostics, fit against actuals, residual tests, decomposition, response curves, impulse responses, forecasts, backtests and side-by-side comparison.
---

# Explore a model <span class="mapi-badge">v0.5</span>

The **Model Explorer** shows what a fitted model learned and how well it works, for every
model family: Bayesian (posterior means with highest-density intervals, R-hat), frequentist
(estimates with confidence intervals and p-values, information criteria) and machine learning
(coefficients or importances). Views are computed in the sandbox from the bundle and the data
it was fitted on, and come with Vega-Lite plots. The [web app](#in-the-web-app) shows every
section with governance evidence and lineage, compares versions and optimises budgets; the
rest of this guide uses the HTTP API it calls.

## In the web app

A model version's page has **Explore**, **Compare** and, for marketing-mix models,
**Optimise budget**. The Model Explorer (`/models/<model>/versions/<n>/explore`) groups its
sections by the question they answer:

| Group | Sections |
|---|---|
| Overview | *At a glance*: the plain readings of the diagnostics and the fit, the holdout metrics and the model's [About card](../concepts/about-and-choosing.md) |
| What it learned | Parameters (a sortable, filterable table with intervals, p-values, R-hat and ESS, and a forest plot per parameter), contributions, response curves, impulse responses and variance decomposition |
| Can you trust it? | Diagnostics (convergence, fit statistics per series, the model's own tests), fit vs actual (with the band's coverage in plain words), residuals (over time, ACF, histogram, QQ plot, Ljung-Box, Jarque-Bera, Breusch-Pagan and ARCH-LM), backtest |
| Looking ahead | Forecast (a fan per series) |
| Trust and origin | Governance (the latest validation's status badge, every check with its evidence, the status history) and lineage (dataset, spec revision, fit run and workflow run, plus plugin, image, seed and libraries) |

Sections that do not apply to the model (an MMM has no impulse responses) are listed last
with the reason. Forecasts and backtests are computed on demand (**Compute forecast and
backtest**, or **Explore with options** for the horizon, the interval and the backtest plan:
expanding window or rolling origin, horizons, folds, minimum training periods and gap). As
in the Data Explorer, Guided mode puts the plain reading first and Expert mode every number.

![At a glance: the plain readings, the holdout metrics and the model's About card](../assets/screenshots/explorer-model-summary.png){ .mapi-shot }

![Parameters: estimates with their 90% intervals, R-hat and ESS](../assets/screenshots/explorer-model-params.png){ .mapi-shot }

![Residuals: over time, their ACF, histogram and QQ plot, and the tests per series](../assets/screenshots/explorer-model-residuals.png){ .mapi-shot }

![Impulse responses of a VECM: pick the shock and the response](../assets/screenshots/explorer-model-irf.png){ .mapi-shot }

![Governance evidence: each check's result, measured value, limit and evidence](../assets/screenshots/explorer-model-governance.png){ .mapi-shot }

The header shows the version's validation status; it links to the version's validations,
where a new validation is requested. Under *Governance evidence*, **Open this validation**
leads to the latest one (its status path, the reviewers' decisions and the evidence pack) and
**Policies** to the governance policies (see [Validate models with governance
policies](governance.md#in-the-web-app)). When a workflow fitted the version, *Lineage* links
the workflow run's monitor.

**Compare** (`/models/compare`) puts two to eleven versions side by side: pick each model
and version (the first is the one the tests use as reference), then **Compare**. The page
shows the holdout metrics with the best value of each metric marked, the Diebold-Mariano
tests in plain words ("about equally accurate", or an edge that is more than chance), the
ranking per metric and every version's own fit statistics.

![Three versions side by side with Diebold-Mariano tests](../assets/screenshots/explorer-model-compare.png){ .mapi-shot }

**Optimise budget** (`/models/<model>/versions/<n>/optimize`, marketing-mix models only)
asks how much to spend (today's total, a new total, a change in percent, or - in Expert mode
- the total the model would choose), how cautious to be (the expected result, the 25th or
10th percentile, or the average of the worst 10%), limits per channel (multiples of today's
spend or money, or kept as is), portfolio groups and the frontier. Bounds that cannot be met
are explained before anything runs. The plan reads "Move money from tv (−7,491) to search
(+7,491). That adds about +3,063 to revenue (90% likely between +674 and +6,030)…", with
the allocation per channel (and market), the expected result today and with the plan, the
chance the plan beats today's, the return of the next unit per channel and the efficient
frontier. In Guided mode the *What if…* step leads here with **Optimise my budget**. The
request and result are those of
[`POST …:optimize`](../models/mmm-framework.md#budget-optimisation).

![A budget plan: the allocation, the expected result and the efficient frontier](../assets/screenshots/explorer-optimize.png){ .mapi-shot }

## From Python and the command line

```python
--8<-- "client_v05_more.py:model-explorer"
```

```bash
mapi explore model model://growth/sales-arimax@v2 -s params -s residuals -s irf --horizon 12
mapi explore compare model://growth/sales-arimax@v2 model://growth/sales-ets@v1 --horizon 4
```

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
