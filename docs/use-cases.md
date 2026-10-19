---
description: Seven problems modeling-api is built for - marketing-mix budget planning on mmm-framework, a governed forecasting pipeline, macro and VAR/VECM analysis, weekly forecasting, demand by store, customer choice and science models of images, fields, maps and graphs - with the data each needs, what you get and the tutorials that walk through them.
---

# Use cases

Each use case below starts from an item that is ready to use. This item is a Guided
template that an analyst can run from a spreadsheet, or a gallery workflow that automates the
full analysis. Each use case also has a built-in model. A data scientist can configure,
extend or replace this model in a spec. All use cases share the platform: sandboxed runs, the
scorecard, governance policies, the explorers, endpoints, snippets, webhooks and embeds.

| Use case | Start from | Model | The question it answers |
|---|---|---|---|
| [Budget planning](#marketing-mix-budget-planning) | Guided *Geo marketing mix*, or the `mmm_governed` workflow | `mmm.bayesian` (or `mmm.frequentist`), on mmm-framework | What does each channel bring in, and where must the next euro go? |
| [Governed forecasting pipeline](#governed-forecasting-pipeline) | The `ts_readiness_arimax` workflow, scheduled | `ts.sarimax` (ARIMAX), checked against naive forecasts | Can we publish this forecast each Monday? Can we prove that it is better than a naive forecast? |
| [Macro and VAR analysis](#macro-and-var-analysis) | The `var_vecm_cointegration` workflow | `ts.vecm` or `ts.var` (also `ts.varmax`, `ts.dynamic_factor`) | Do these series move together, and what happens to one when another is shocked? |
| [Weekly forecasting](#weekly-forecasting) | Guided *Weekly forecast* | `ts.hsgp_trend` | What will this series do over the next weeks, and how sure are we? |
| [Store demand](#demand-by-store) | Guided *Demand by store* | `tabular.regressor` (xgboost, quantiles) | How much will each store sell, including a store that opens next month? |
| [Customer choice](#customer-choice) | Guided *Customer choice* | `choice.multinomial_logit` | Which product does a shopper pick, and how does a price change shift that? |
| [Science models](#science-models-images-fields-maps-and-graphs) | A pymc-challenges tutorial, then a science recipe (for example `sci_spatial_ladder`) | 15 `sci.*` models, for example `sci.areal_bym2`, `sci.heat_plate` and `sci.network_latent_space` | What does the model of a research notebook tell, and can the analysis repeat with the same checks? |

## Marketing-mix budget planning

**The question.** Media spend goes to TV, search and social in some markets. What did each
channel add to revenue? What does each euro return now? How do we divide the budget of the
next quarter?

**The data.** One row for each week and market: revenue, spend for each channel, and
optional controls (for example, price or distribution). Two years of weekly data is a good
start. The platform recognises MFF long tables and Meridian-shaped tables directly.

**What you get.**

- Contribution and ROI for each channel and market with 90% ranges, response curves, and a
  decomposition of revenue into baseline and channels.
- Beliefs as **ROI or share ranges**, and **lift tests** as calibration. A sandbox translates
  them into model priors, with a preview in business units.
- **What-if sliders** whose ranges stay joint when some channels move. "Run exact",
  counterfactuals with a channel set to off, and forecasts.
- **Budget plans** from the model (`POST …:optimize`, mmm-framework planning). A plan gives
  the allocation in your bounds, the uplift and the probability that it helps. It also gives
  the stability of each move and the efficient frontier. A planning tool can call an
  **endpoint** for the plan.
- **Proof before the plan.** The default marketing-mix policy fits the model again on
  shuffled sales and with fake noise channels. It checks the effect signs and the holdout
  error, and records the evidence. The `mmm_governed` workflow does all of this in one run:
  collinearity check, fit, policy, report and evidence pack.

**Start here.**
[Your first MMM in Guided mode](guides/guided-tutorial.md) →
[Priors, lift tests and the scorecard](guides/guided-modelling.md) →
[Optimise a budget](tutorials/optimise-a-budget.md).
Writing your own MMM spec: [the marketing-mix guide](guides/mmm.md) and [mmm-framework on the platform](models/mmm-framework.md).

## Governed forecasting pipeline

**The question.** Finance publishes a weekly demand forecast. Other teams use it for their
plans. Each Monday, Finance must update it with the latest data. The person who approves it
must know three things. The data chose the model. The model is better than a naive forecast
on weeks that it did not see. The model does not only fit noise.

**The data.** One weekly KPI with a minimum of two years of history. Also drivers that you
know before (prices, promotions, holidays). You upload them as a new dataset version each
week.

**What you get.**

- The `ts_readiness_arimax` workflow. A **schedule** (`0 6 * * 1`, Europe/Berlin) or a
  **trigger** on each new dataset version runs it:
    - ADF, KPSS, Phillips-Perron and Zivot-Andrews decide the differencing.
    - An information-criterion search selects the AR and MA orders.
    - An expanding-window backtest scores ARIMAX against naive and seasonal-naive forecasts.
      It uses the same folds at 1, 4 and 8 weeks.
    - The workflow keeps the model only if it is better than these forecasts.
- A **governance policy** for forecasts that you publish: null tests, benchmarks, residual
  whiteness and holdout accuracy. It has the **approval of a second person** and
  `require_validated_for_promotion`. Thus the endpoint that the planners call can serve only
  a validated version. If not, it gives `409 NOT_VALIDATED`.
- A record of the run each week. It has the verdict of each test, each branch with the values
  that it read and the selected orders. It also has the backtest table, the Diebold-Mariano
  verdict, the governance evidence and an evidence pack for the auditor. The platform uses
  unchanged steps again from the cache. Thus a new run costs only what changed.

We captured the canonical run on a laptop, with a weekly KPI that wanders. All four tests
say "difference once". The search selects ARIMA(1, 1, 1). On 13 held-out weeks, the model is
25% better than "same as last week". But the record also tells that the one-week advantage
over naive *is possibly luck*. It also tells that two of twenty shuffled fits were equally good.
An approval needs this type of honesty
([the full output](whats-new-0.5.md#the-canonical-example-three-ways)).

**Start here.**
[Workflow cookbook: readiness to ARIMAX](guides/workflow-cookbook.md#readiness-to-arimax) →
[Validate models with governance policies](guides/governance.md) →
[Schedules and triggers](guides/workflows.md#schedules-and-triggers) →
[Endpoints and webhooks](guides/endpoints-and-webhooks.md).

## Macro and VAR analysis

**The question.** Some series drive each other. Examples are income and consumption, the
prices of close substitutes, or sales, distribution and search interest. Are the series tied
together in the long run? If one series gets a shock, how do the others respond, and for how
long? Does one series help to predict another?

**The data.** Two to five series at the same frequency, with one value for each series and
period (`value(series, quarter)`). The ingest reads a wide sheet with the `wide` mapping. The
best length is 50 periods or more.

**What you get.**

- The `var_vecm_cointegration` workflow. A Johansen trace test decides the branch. Tied
  series get a cointegration-rank search and a **VECM** on the levels. The workflow
  differences the series that are not tied. AIC selects the lag order, and the workflow fits
  a **VAR** on the changes. Granger tests run on the two branches. The forecast comes from
  the system that the workflow fitted.
- **Impulse responses with bands** and **variance decompositions** in the Model Explorer.
  Also the Johansen table, the speed of adjustment (`alpha`: how fast a gap closes), the
  long-run relations (`beta`) and Granger tables.
- `ts.varmax` when outside drivers are important. `ts.dynamic_factor` for many series that
  move with some common factors. `ts.ecm` (ARDL bounds test) for one equation with long-run
  drivers.

We ran the workflow of the cookbook on a laptop with three simulated weekly series (160
weeks). In the data, `a` and `b` share one stochastic trend, and `c` wanders alone. The
workflow found exactly that:

```text
johansen  The series share at least one long-run relationship.
          (trace statistic 69.1, rank 1)
tied      took the then arm   {'nodes.johansen.rank': 1}
rank      1
vecm      model://growth/wf-macro-system-vecm@v1
granger   Past values of the driver help predict the result.
          (b -> a, lag 1, p = 0.0015)
forecast  8 weeks: dataset://growth/wf-macro-system-forecast-forecast@v1
skipped   diff, lags, var   (the VAR branch was not needed)
```

(This is a summary of the node results of the run. The cookbook shows the client calls that
read them.)

**Start here.**
[Workflow cookbook: VAR/VECM cointegration](guides/workflow-cookbook.md#varvecm-cointegration) →
[statsmodels time-series models](models/statsmodels.md) →
[Explore a model: impulse responses](guides/model-explorer.md).

## Weekly forecasting

**The question.** What will the orders, visits, calls or sales be in the next 8 to 26 weeks?
What is the range that a planner can use?

**The data.** One date column and one numeric column, weekly or daily. For a seasonal
pattern, the history must have a minimum of two seasons.

**What you get.**

- A forecast fan: the median and the 50% and 90% ranges, with the fitted history and the
  split line. Thus you can see the result of the model on the weeks that it did not see.
- A holdout check: the error, and how frequently the actual values were in the 90% range.
- The `max_extension` of the model declares how far it can forecast. The platform refuses a
  longer horizon with `HORIZON_EXCEEDS_FIT` before any compute.
- A Google Sheet that gets a new forecast from the endpoint each morning.

**Start here.**
[The Guided tutorial](guides/guided-tutorial.md) (pick *Weekly forecast*) →
[Connect Google Sheets](tutorials/google-sheets.md) →
[Forecasting and sequential models](guides/time-series.md).

## Demand by store

**The question.** How many units will each store sell each week, with the prices, promotions
and calendar? How many units will a store sell that the model did not see?

**The data.** One row for each store and week: units sold, and drivers (for example, price
and a promotion flag).

**What you get.**

- An xgboost model with calendar features. A low and a high estimate (quantiles) for each
  store and week, and the importance of each driver.
- The platform scores new stores by declaration. The policy of the store dim tells if the
  platform can score a store that the model did not see, and how. The result does not depend
  on what the model does with an unknown category.
- An endpoint that a replenishment system can call with the plan for the next week. A webhook
  when a new fit is ready.

**Start here.**
[The Guided tutorial](guides/guided-tutorial.md) (pick *Demand by store*) →
[Tabular models](guides/sklearn.md) →
[Endpoints and webhooks](guides/endpoints-and-webhooks.md) →
[Receive webhooks](guides/webhook-receivers.md).
The `xgb_store_demand` [conformance fixture](guides/conformance.md) runs the same problem end
to end in some seconds.

## Customer choice

**The question.** On each shopping trip, which product does a household select? How does a
change in price or a promotion move the shares?

**The data.** One row for each trip and product: the product that the household selected,
its price and its promotion, if there is one. The row identifies the household and the
trip.

**What you get.**

- A conditional logit with household effects and Bayesian-bootstrap draws.
- The holdout hit rate on the last trip of each household. The scorecard compares it, in
  plain words, with a guess of the most popular product each time.
- Price and promotion counterfactuals: how the shares move when a price changes.

**Start here.**
[The Guided tutorial](guides/guided-tutorial.md) (pick *Customer choice*) →
[Built-in models](reference/models.md).

## Science models: images, fields, maps and graphs

**The question.** A research notebook fits a Bayesian model to an image, a field, a map or
a graph. For example: which districts have a high risk of a disease, and does a covariate
explain it? Which conductivity does a heated plate have, and which field does a new heater
layout give? Which effects explain the ties of a network? The analysis must repeat, and a
person who does not write PyMC code must be able to read it.

**The data.** The data of the notebook, as a file that you can upload. The export helper
writes it:

```bash
uv run python -m mapi_plugin_science.export --list
uv run python -m mapi_plugin_science.export e18 \
    --challenges <path to pymc-challenges>/data \
    --out scotland_lip_cancer.nc --spec scotland_lip_cancer.yaml
```

The file keeps the schema of the model. Thus the upload gives the correct role to each
variable. Without a copy of pymc-challenges, `--synthetic` writes data that the model
simulates.

**What you get.**

- 15 science models, for example disease maps (`sci.areal_bym2`), a heat equation
  (`sci.heat_plate`), Turing patterns (`sci.turing_pattern`), STORM frames
  (`sci.storm_localisation`), the CMB sky (`sci.cmb_field`), networks
  (`sci.network_latent_space`), graphical models (`sci.graphical_model`), phylogenies
  (`sci.phylo_regression`) and angles (`sci.circular_regression`).
- Plots for each type of data: area maps, field maps, image residuals, predictive image
  grids, network graphs, phylogenies, polar roses, colour swatches and power spectra.
- The Model Explorer: the **Fields**, **Plots**, **Parameters** and **Diagnostics**
  sections. The **Score** page predicts and simulates new data.
- Recipes that do the Bayesian workflow:
    - `sci_spatial_ladder`, `sci_network_ladder` and `sci_phylo_signal` compare model
      variants by `elpd_loo` and stacking weights. Then they tell if an effect is clear.
    - `sci_calibrate_then_design` predicts a new design only if the calibration passes its
      checks.
    - `bayes_workflow_audit` and `sci_image_model_check` do the convergence, predictive,
      prior and recovery checks.

We did the E18 tutorial in the web app with the real data. The BYM2 map has an `elpd_loo` of
−153.0, and the notebook gives −152.7. The effect of outdoor work stays clear. The
`sci_spatial_ladder` recipe took 46 s and selected the ICAR model. The notebook also gives the
ICAR model the best `elpd_loo`.

**Start here.**
[pymc-challenges in the web app](tutorials/pymc-challenges/index.md) →
[E18 Lip cancer disease map](tutorials/pymc-challenges/e18-scottish-lip-cancer.md) →
[Complex workflows](tutorials/complex-workflows.md) →
[Scientific models](reference/models.md#scientific-models-mapi-plugin-science).

## Your own model

If none of these use cases is correct for you, write your own model. A PyMC model is one
`build()` method, and an sklearn model is one `estimator()`. The platform does the
coordinates, splits, replayed preprocessing, summaries, bundles, endpoints and
integrations.
[Port a model in 15 minutes](guides/port-a-model.md) ·
[Why modeling-api?](why.md)
