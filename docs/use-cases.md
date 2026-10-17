---
description: Six problems modeling-api is built for - marketing-mix budget planning on mmm-framework, a governed forecasting pipeline, macro and VAR/VECM analysis, weekly forecasting, demand by store and customer choice - with the data each needs, what you get and the tutorials that walk through them.
---

# Use cases

Each use case below starts from something ready-made - a Guided template an analyst can run
from a spreadsheet, or a gallery workflow that automates the whole analysis - and a built-in
model that a data scientist can configure, extend or replace in a spec. All of them share the
platform: sandboxed runs, the scorecard, governance policies, the explorers, endpoints,
snippets, webhooks and embeds.

| Use case | Start from | Model | The question it answers |
|---|---|---|---|
| [Budget planning](#marketing-mix-budget-planning) | Guided *Geo marketing mix*, or the `mmm_governed` workflow | `mmm.bayesian` (or `mmm.frequentist`), on mmm-framework | What does each channel bring in, and where should the next euro go? |
| [Governed forecasting pipeline](#governed-forecasting-pipeline) | The `ts_readiness_arimax` workflow, scheduled | `ts.sarimax` (ARIMAX), checked against naive forecasts | Can we publish this forecast every Monday - and prove it beats doing nothing clever? |
| [Macro and VAR analysis](#macro-and-var-analysis) | The `var_vecm_cointegration` workflow | `ts.vecm` or `ts.var` (also `ts.varmax`, `ts.dynamic_factor`) | Do these series move together, and what happens to one when another is shocked? |
| [Weekly forecasting](#weekly-forecasting) | Guided *Weekly forecast* | `ts.hsgp_trend` | What will this series do over the next weeks, and how sure are we? |
| [Store demand](#demand-by-store) | Guided *Demand by store* | `tabular.regressor` (xgboost, quantiles) | How much will each store sell, including a store that opens next month? |
| [Customer choice](#customer-choice) | Guided *Customer choice* | `choice.multinomial_logit` | Which product does a shopper pick, and how does a price change shift that? |

## Marketing-mix budget planning

**The question.** Media spend goes to TV, search and social in several markets. What did each
channel add to revenue, what is each euro returning now, and how should next quarter's budget
be split?

**The data.** One row per week and market: revenue, spend per channel, and optional controls
such as price or distribution. Two years of weekly data is a good start. MFF long tables
and Meridian-shaped tables are recognised directly.

**What you get.**

- Contribution and ROI per channel and market with 90% ranges, response curves and a
  decomposition of revenue into baseline and channels.
- Beliefs entered as **ROI or share ranges** and **lift tests** as calibration, translated into
  model priors in a sandbox with a preview in business units.
- **What-if sliders** whose ranges stay joint when several channels move, "Run exact",
  channel-off counterfactuals and forecasts.
- **Budget plans** from the model itself (`POST …:optimize`, mmm-framework planning): the
  allocation within your bounds, the uplift and the chance it helps, how stable each move
  is, and the efficient frontier - and an **endpoint** a planning tool calls.
- **Proof before the plan.** The default marketing-mix policy refits the model on shuffled
  sales and with fake noise channels, checks the effect signs and the holdout error, and
  records the evidence; the `mmm_governed` workflow does it all - collinearity check, fit,
  policy, report, evidence pack - in one run.

**Start here.**
[Your first MMM in Guided mode](guides/guided-tutorial.md) →
[Priors, lift tests and the scorecard](guides/guided-modelling.md) →
[Optimise a budget](tutorials/optimise-a-budget.md).
Writing your own MMM spec: [the marketing-mix guide](guides/mmm.md) and [mmm-framework on the platform](models/mmm-framework.md).

## Governed forecasting pipeline

**The question.** Finance publishes a weekly demand forecast that other teams plan on. Every
Monday it has to be refreshed on the latest data, and whoever signs it off wants to know that
the model was chosen by the data, beats a naive forecast on weeks it never saw, and is not
just fitting noise.

**The data.** One weekly KPI with at least two years of history, plus drivers you know ahead
(prices, promotions, holidays), uploaded as a new dataset version every week.

**What you get.**

- The `ts_readiness_arimax` workflow, run by a **schedule** (`0 6 * * 1`, Europe/Berlin) or a
  **trigger** on each new dataset version: ADF, KPSS, Phillips-Perron and Zivot-Andrews decide
  the differencing, an information-criterion search picks the AR and MA orders, an
  expanding-window backtest scores ARIMAX against naive and seasonal-naive on the same folds
  at 1, 4 and 8 weeks, and the model is kept only if it beats them.
- A **governance policy** for forecasts you publish - null tests, benchmarks, residual
  whiteness, holdout accuracy - with a **second person's approval** and
  `require_validated_for_promotion`, so the endpoint the planners call can only ever serve a
  validated version (`409 NOT_VALIDATED` otherwise).
- Every week's run on the record: each test's verdict, each branch with the values it read,
  the chosen orders, the backtest table, the Diebold-Mariano verdict, the governance evidence
  and an evidence pack for the auditor. Unchanged steps are reused from the cache, so a
  rerun costs only what changed.

The canonical run on a wandering weekly KPI, captured on a laptop: all four tests say
"difference once", the search picks ARIMA(1, 1, 1), the model beats "same as last week" by
25% on 13 held-out weeks - and the record also says that the one-week edge over naive *may be
luck* and that two of twenty shuffled fits did as well. That is the kind of honesty a
sign-off needs ([the full output](whats-new-0.5.md#the-canonical-example-three-ways)).

**Start here.**
[Workflow cookbook: readiness to ARIMAX](guides/workflow-cookbook.md#readiness-to-arimax) →
[Validate models with governance policies](guides/governance.md) →
[Schedules and triggers](guides/workflows.md#schedules-and-triggers) →
[Endpoints and webhooks](guides/endpoints-and-webhooks.md).

## Macro and VAR analysis

**The question.** Several series drive each other - income and consumption, prices of close
substitutes, sales, distribution and search interest. Are they tied together in the long run?
If one is shocked, how do the others respond, and for how long? Does one help predict
another?

**The data.** Two to five series at the same frequency, one value per series and period
(`value(series, quarter)`; a wide sheet is ingested with the `wide` mapping), ideally 50
periods or more.

**What you get.**

- The `var_vecm_cointegration` workflow: a Johansen trace test decides the branch. Tied
  series get a cointegration-rank search and a **VECM** on the levels; untied ones are
  differenced, the lag order is chosen by AIC and a **VAR** is fitted on the changes.
  Granger tests run either way, and a forecast comes from whichever system was fitted.
- **Impulse responses with bands** and **variance decompositions** in the Model Explorer,
  the Johansen table, the speed of adjustment (`alpha`: how fast a gap closes) and the
  long-run relations (`beta`), and Granger tables.
- `ts.varmax` when outside drivers matter, `ts.dynamic_factor` for many series moving with a
  few common factors, `ts.ecm` (ARDL bounds test) for one equation with long-run drivers.

On three simulated weekly series where `a` and `b` share one stochastic trend and `c`
wanders on its own (160 weeks), the cookbook's workflow found exactly that, run on a laptop:

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

(A summary of the run's node results; the cookbook shows the client calls that read them.)

**Start here.**
[Workflow cookbook: VAR/VECM cointegration](guides/workflow-cookbook.md#varvecm-cointegration) →
[statsmodels time-series models](models/statsmodels.md) →
[Explore a model: impulse responses](guides/model-explorer.md).

## Weekly forecasting

**The question.** Orders, visits, calls or sales: what will the next 8 to 26 weeks look
like, with a range a planner can use?

**The data.** One date column and one numeric column, weekly or daily, with at least two
seasons of history for a seasonal pattern.

**What you get.**

- A forecast fan: the median and the 50% and 90% ranges, with the fitted history and the
  split line, so you can see how the model did on the weeks it did not see.
- A holdout check: the error and how often the actual values fell inside the 90% range.
- The model's `max_extension` declares how far it may forecast; a longer horizon is refused
  with `HORIZON_EXCEEDS_FIT` before any compute.
- A Google Sheet that refreshes the forecast every morning from the endpoint.

**Start here.**
[The Guided tutorial](guides/guided-tutorial.md) (pick *Weekly forecast*) →
[Connect Google Sheets](tutorials/google-sheets.md) →
[Forecasting and sequential models](guides/time-series.md).

## Demand by store

**The question.** How many units will each store sell each week, given prices, promotions and
the calendar, and what about a store the model has never seen?

**The data.** One row per store and week: units sold, and drivers such as price and a
promotion flag.

**What you get.**

- An xgboost model with calendar features, a low and a high estimate (quantiles) per store and
  week, and the importance of each driver.
- New stores scored by declaration: the store dim's policy says whether an unseen store may
  be scored and how, instead of whatever the model does with an unknown category.
- An endpoint a replenishment system can call with next week's plan, and a webhook when a
  refit is ready.

**Start here.**
[The Guided tutorial](guides/guided-tutorial.md) (pick *Demand by store*) →
[Tabular models](guides/sklearn.md) →
[Endpoints and webhooks](guides/endpoints-and-webhooks.md) →
[Receive webhooks](guides/webhook-receivers.md).
The `xgb_store_demand` [conformance fixture](guides/conformance.md) runs the same problem end
to end in a few seconds.

## Customer choice

**The question.** On each shopping trip, which product does a household pick, and how does a
price change or a promotion shift the shares?

**The data.** One row per trip and product: which product was chosen, its price and any
promotion, with the household and the trip identified.

**What you get.**

- A conditional logit with household effects and Bayesian-bootstrap draws.
- The holdout hit rate on each household's last trip, compared with always guessing the most
  popular product, in plain words on the scorecard.
- Price and promotion counterfactuals: how the shares move when a price changes.

**Start here.**
[The Guided tutorial](guides/guided-tutorial.md) (pick *Customer choice*) →
[Built-in models](reference/models.md).

## Your own model

None of these fits? A PyMC model is one `build()` method and an sklearn model one
`estimator()`; the platform handles the coordinates, splits, replayed preprocessing,
summaries, bundles, endpoints and integrations.
[Port a model in 15 minutes](guides/port-a-model.md) ·
[Why modeling-api?](why.md)
