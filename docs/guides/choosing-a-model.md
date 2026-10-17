---
description: Pick the right model, template or workflow with About cards and Help me choose - in the web app, the terminal or Python - and the families of models the platform offers, compared.
---

# Choosing a model <span class="mapi-badge">v0.5</span>

You do not need to know what "SARIMAX" means to select a good forecast model. Also, you do
not need to read a statistics textbook to know when *not* to use one. Each model,
preprocessing step, workflow node, governance check and template has an **About card** in
plain words. **Help me choose** asks some questions about your goal and your data. Then it
gives a ranked shortlist with the reasons. This page shows the two tools. Then it compares
the model families, for persons who want the details. For the contract behind the tools, see
[About cards and choosing](../concepts/about-and-choosing.md).

## Answer a few questions

There are eight questions. All are optional. The goal has the most weight:

| Question | Answers |
|---|---|
| What do you want to do? | measure what marketing adds · plan a budget · forecast · understand drivers · predict per item · understand choices · find regimes · see series move together · check data · validate a model |
| What does your data look like? | one number over time · a few related numbers · the same numbers for many markets, stores or products · purchases or choices · a table without time |
| How often is it recorded? | daily · weekly · monthly or less often · not over time |
| How much history? | under a year · one to three years · more |
| Other columns that could explain it? | yes (prices, promotions, weather, spend) · no |
| Do you need a range? | essential · nice to have · one number is enough |
| How comfortable with statistics? | new to it · the basics · expert |
| How quickly do you need results? | seconds · minutes · can wait |

=== "Terminal"

    ```bash
    --8<-- "cli_v05.sh:choose"
    ```

    `mapi choose` asks the questions one at a time. Type a number. For questions that accept
    more than one answer, type more than one number. To skip a question, push Enter. The
    command shows the shortlist, and then the About card of the first three items. This is
    the result for the four answers above, on a local stack (shortened):

    ```text
    Recommended for you
     # ┃ item                ┃ kind              ┃ fit  ┃ difficulty   ┃ time               ┃ why
    ━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━
     1 │ Smooth trend        │ model             │ 100% │ beginner     │ minutes            │ Suits: forecast the
       │ forecast            │                   │      │              │                    │ future
       │ ts.hsgp_trend       │                   │      │              │                    │ Suits: one number
       │                     │                   │      │              │                    │ over time
     2 │ Weekly forecast     │ template          │ 100% │ beginner     │ minutes            │ …
       │ weekly_forecast     │                   │      │              │                    │
     3 │ Forecast bake-off   │ workflow_template │ 100% │ beginner     │ more than 15       │ …
       │ workflow.forecast_b │                   │      │              │ minutes            │
       │ akeoff              │                   │      │              │                    │
     4 │ Time-series         │ workflow_template │ 100% │ beginner     │ minutes            │ …
       │ readiness to ARIMAX │                   │      │              │                    │
     5 │ Effects that build  │ model             │ 86%  │ intermediate │ seconds            │ Suits: forecast the
       │ up over time        │                   │      │              │                    │ future
       │ ts.ardl             │                   │      │              │                    │ …
       │                     │                   │      │              │                    │ Suits: yes (prices,
       │                     │                   │      │              │                    │ promotions,
       │                     │                   │      │              │                    │ weather, spend)
       │                     │                   │      │              │                    │ careful: Needs
       │                     │                   │      │              │                    │ intermediate-level
       │                     │                   │      │              │                    │ statistics
       │                     │                   │      │              │                    │ knowledge

    Smooth trend forecast  model ts.hsgp_trend
    Forecasts one regular series with a smooth, flexible trend and a repeating seasonal pattern, and gives a range of likely values.
    difficulty: beginner   typical time: minutes
    …
    unanswered (answer them to sharpen the ranking): frequency, history, uncertainty, speed
    ```

    The answer "new to it" (statistics) has an effect. Models that need more experience
    have a caution, and they rank below the models for beginners. Examples are `ts.ardl`
    above and `ts.sarimax` in position six. The models for beginners include the
    *readiness* workflow, which selects the settings of an ARIMAX for you. If you answer
    `experience=intermediate`, or skip the question, `ts.ardl` and `ts.sarimax` come first.
    These are the models that use the drivers.

=== "Python"

    ```python
    --8<-- "client_v05.py:choose"
    ```

=== "HTTP"

    ```bash
    api GET "/v1/catalog/chooser"        # the questions
    api GET "/v1/catalog/recommendations?goal=forecast&data_shape=one_series&drivers=yes&experience=beginner&limit=3"
    api GET "/v1/catalog/abouts?kind=model"
    ```

=== "Web app"

    **Help me choose** on the model, template and workflow pickers asks the same questions.
    It shows the same ranking. Each card has *About*. *About* shows the plain explanation
    first. The expert details are folded away.

The ranking is deterministic, and you can see why each item has its position:

- An answer that matches adds the weight of the question. An answer that does not match
  subtracts it.
- Items that are more difficult than your experience lose points. Items that are slower
  than your answer for speed also lose points.
- Each recommendation lists its **reasons** ("Suits: forecast the future").
- Each recommendation lists its **cautions** ("Needs expert-level statistics knowledge",
  "Usually takes long").

Without a `kind`, the candidates are the items that you start from: models, guided templates
and workflow templates. `--kind step`, `node` or `check` ranks only one type of item.

## Read an About card

```bash
mapi about ts.ets
```

```text
Exponential smoothing forecast  ts.ets
Forecasts a series by giving recent values more weight than older ones, with optional trend and
seasonality, and is quick and robust.
difficulty: beginner   typical time: seconds
Use it when
  • You need a fast, reliable baseline forecast
  • The level, trend and seasonality shift slowly
Pick something else when
  • Outside drivers explain most of the movement
It needs
  • One number over time at a regular frequency
It assumes
  • Recent history is the best guide to the near future
You get
  • A forecast with a range
  • Level, trend and seasonal parts
Example: Forecast monthly demand 12 months ahead with a damped trend.
Related: ts.theta, ts.arima, evaluate.baseline
(--expert shows the maths and references)
```

Each card has the same parts:

- A summary of one or two sentences, without jargon.
- When to use the item, and when not to use it.
- The data that it needs, and what it assumes.
- What you get.
- How difficult it is, and how long it usually takes.
- A very small example, and related items.

`--expert` (or *Expert detail* in the web app) adds the maths and the references. The
platform refuses catalog items that do not have a complete card. Thus each model that you
can select explains itself. This also applies to your own plugins
([the About check](plugins.md)).

## The model families, compared

| You want to | Start with | Why | Then consider |
|---|---|---|---|
| Measure what each marketing channel adds. Plan budgets. | `mmm.bayesian` | Pooled over markets. Adstock and saturation, business priors and lift tests. Ranges on all values. Budget optimisation. | `mmm.frequentist` for a quick first look. The `mmm_governed` workflow to gate it on null tests. |
| Forecast one series with known drivers (price, promotions) | `ts.sarimax` | Past values, season and drivers. Prediction intervals. Forecasts from planned drivers. | The `ts_readiness_arimax` workflow. It selects the differencing and orders, and proves that the model is better than naive. |
| Forecast without drivers, fast and robust | `ts.ets` or `ts.theta` | Strong, simple baselines that are difficult to beat | The `forecast_bakeoff` workflow, which lets them compete |
| See the trend under the noise | `ts.unobserved_components` | Level, trend, season and noise as separate parts | `ts.hsgp_trend` for a Bayesian smooth trend |
| Know how an effect builds up over weeks | `ts.ardl` | Lags of the drivers, selected automatically | `ts.ecm` when the result and drivers have the same trend |
| Several series that drive each other | `ts.var` | Impulse responses, variance decompositions, Granger tests | `ts.vecm` when they are tied in the long run. The `var_vecm_cointegration` workflow decides. |
| Find regimes (normal or caused by promotions) | `ts.markov_switching` | Probability of each regime for each period | `ts.gaussian_hmm` for a Bayesian hidden-state model |
| Predict a value for each store, product or customer | `sklearn.tabular_regressor` | Gradient boosting or forests. Quantiles and conformal intervals. | `tabular.hierarchical_glm` when groups must borrow strength |
| Understand which product people select | `choice.multinomial_logit` | Price and promotion effects, household effects | |
| Check data before you model it | the [Data Explorer](data-explorer.md) | Stationarity, cross-correlation with the KPI, STL, VIF, quality warnings | |
| Make sure that you can trust a model | a [governance policy](null-tests.md) | Null tests, benchmarks, residuals, stability, signs | |

For the details of each family, see these pages:
[Marketing-mix models](../models/mmm-framework.md),
[statsmodels time-series models](../models/statsmodels.md),
[forecasting](time-series.md), [tabular models](sklearn.md) and
[the built-in models](../reference/models.md).

## When the chooser is not enough

- **Compare. Do not guess.** Fit two candidates and compare them on the same holdout. Use
  `mapi explore compare model://growth/a@v1 model://growth/b@v1` (metrics, ranking and
  Diebold-Mariano tests). Alternatively, run a
  [bake-off workflow](workflow-cookbook.md#forecast-bake-off).
- **Let the data decide the details.** The readiness workflow selects differencing and ARMA
  orders with tests and information criteria. It records the reasons.
- **Make "good enough" explicit.** A governance policy changes "it looks fine" into checks.
  The checks have thresholds, evidence and the approval of a second person.
