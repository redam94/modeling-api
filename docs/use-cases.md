---
description: Four problems modeling-api is built for - marketing-mix budget planning with Atlas, weekly forecasting, demand by store and customer choice - with the data each needs, what you get and the tutorials that walk through them.
---

# Use cases

Each use case below is a Guided template, so an analyst can start it from a spreadsheet, and
a built-in model that a data scientist can configure, extend or replace in a spec. All four
share the platform: sandboxed runs, the scorecard, endpoints, snippets, webhooks and embeds.

| Use case | Guided template | Model | The question it answers |
|---|---|---|---|
| [Budget planning](#marketing-mix-budget-planning-with-atlas) | Geo marketing mix | `mmm.hierarchical_geo` (or `mmm.pymc_marketing`, `mmm.bayesinsight`) | What does each channel bring in, and where should the next euro go? |
| [Weekly forecasting](#weekly-forecasting) | Weekly forecast | `ts.hsgp_trend` | What will this series do over the next weeks, and how sure are we? |
| [Store demand](#demand-by-store) | Demand by store | `tabular.regressor` (xgboost, quantiles) | How much will each store sell, including a store that opens next month? |
| [Customer choice](#customer-choice) | Customer choice | `choice.multinomial_logit` | Which product does a shopper pick, and how does a price change shift that? |

## Marketing-mix budget planning with Atlas

**The question.** Media spend goes to TV, search and social in several markets. What did each
channel add to revenue, what is each euro returning now, and how should next quarter's budget
be split?

**The data.** One row per week and market: revenue, spend per channel, and optional controls
such as price or distribution. Two years of weekly data is a good start. MFF long tables
(BayesInsight), Meridian-shaped tables and BayesInsight projects are recognised directly.

**What you get.**

- Contribution and ROI per channel and market with 90% ranges, response curves and a
  decomposition of revenue into baseline and channels.
- Beliefs entered as **ROI or share ranges** and **lift tests** as calibration, translated into
  model priors in a sandbox with a preview in business units.
- **What-if sliders** whose ranges stay joint when several channels move, "Run exact",
  channel-off counterfactuals and forecasts.
- An **endpoint** that a planning tool calls, and an **Atlas** `RemoteModel` that optimises
  allocations against it. On a laptop, Atlas found **+0.22 %** revenue at the same budget by
  moving spend from TV to search ([measured](operations/m3-verification.md#atlas-against-the-live-endpoint)).

**Start here.**
[Your first MMM in Guided mode](guides/guided-tutorial.md) →
[Priors, lift tests and the scorecard](guides/guided-modelling.md) →
[Optimise a budget with Atlas](guides/atlas.md).
Coming from BayesInsight: [import a BayesInsight model](tutorials/bayesinsight-import.md).
Writing your own MMM: [the marketing-mix guide](guides/mmm.md).

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

None of the four fits? A PyMC model is one `build()` method and an sklearn model one
`estimator()`; the platform handles the coordinates, splits, replayed preprocessing,
summaries, bundles, endpoints and integrations.
[Port a model in 15 minutes](guides/port-a-model.md) ·
[Why modeling-api?](why.md)
