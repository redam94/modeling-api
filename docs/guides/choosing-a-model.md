---
description: Pick the right model, template or workflow with About cards and Help me choose - in the web app, the terminal or Python - and the families of models the platform offers, compared.
---

# Choosing a model <span class="mapi-badge">v0.5</span>

You do not need to know what "SARIMAX" means to pick a good forecasting model, and you should
not have to read a statistics textbook to know when *not* to use one. Every model, preprocessing
step, workflow node, governance check and template carries an **About card** in plain words, and
**Help me choose** turns a few questions about your goal and your data into a ranked shortlist
with the reasons. This page shows both, then compares the model families for those who want the
detail. The contract behind them is in [About cards and choosing](../concepts/about-and-choosing.md).

## Answer a few questions

Eight questions, all optional; the goal counts most:

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

    `mapi choose` asks the questions one by one (numbers; several for questions that take
    more than one answer; Enter skips) and prints the shortlist, then the About card of the
    top three:

    ```text
                                  Recommended for you
     #  item                       kind    fit  difficulty    time     why
     1  Forecast with past values  model   92%  intermediate  seconds  Suits: forecast the future
        and drivers                                                    Suits: one number over time
        ts.sarimax                                                     Suits: other columns that
                                                                       could explain the result
     2  Weekly forecast            template 88% beginner      minutes  ...
    ```

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

    **Help me choose** on the model, template and workflow pickers asks the same questions and
    shows the same ranking; every card has *About* with the plain explanation first and the
    expert detail folded away.

The ranking is deterministic and explainable: a matching answer adds the question's weight, a
mismatch subtracts it, items harder than your experience or slower than your patience lose
points, and every recommendation lists its **reasons** ("Suits: forecast the future") and its
**cautions** ("Needs expert-level statistics knowledge", "Usually takes long"). Without a
`kind` the candidates are the places you start from - models, guided templates and workflow
templates; `--kind step`, `node` or `check` ranks one kind of building block.

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

Every card has the same parts: a one- or two-sentence summary without jargon, when to use it
and when not, the data it needs, what it assumes, what you get, how hard it is, how long it
usually takes, a tiny example and related items. `--expert` (or *Expert detail* in the web app)
adds the maths and the references. The platform refuses catalog items without a complete card,
so every model you can pick explains itself - including your own plugins
([the About check](plugins.md)).

## The model families, compared

| You want to | Start with | Why | Then consider |
|---|---|---|---|
| Measure what each marketing channel adds; plan budgets | `mmm.bayesian` | Pooled over markets, adstock and saturation, business priors and lift tests, ranges on everything, budget optimisation | `mmm.frequentist` for a quick first look; the `mmm_governed` workflow to gate it on null tests |
| Forecast one series with known drivers (price, promotions) | `ts.sarimax` | Past values, season and drivers, prediction intervals, forecasts from planned drivers | The `ts_readiness_arimax` workflow to choose the differencing and orders and prove it beats naive |
| Forecast without drivers, fast and robust | `ts.ets` or `ts.theta` | Strong, simple baselines that are hard to beat | The `forecast_bakeoff` workflow: let them compete |
| See the trend under the noise | `ts.unobserved_components` | Level, trend, season and noise as separate parts | `ts.hsgp_trend` for a Bayesian smooth trend |
| Know how an effect builds up over weeks | `ts.ardl` | Lags of the drivers, chosen automatically | `ts.ecm` when the result and drivers trend together |
| Several series that drive each other | `ts.var` | Impulse responses, variance decompositions, Granger tests | `ts.vecm` when they are tied in the long run (the `var_vecm_cointegration` workflow decides) |
| Spot regimes (normal vs promotion-driven) | `ts.markov_switching` | Probability of each regime per period | `ts.gaussian_hmm` for a Bayesian hidden-state model |
| Predict a value per store, product or customer | `sklearn.tabular_regressor` | Gradient boosting or forests, quantiles and conformal intervals | `tabular.hierarchical_glm` when groups should borrow strength |
| Understand which product people choose | `choice.multinomial_logit` | Price and promotion effects, household effects | |
| Check data before modelling | the [Data Explorer](data-explorer.md) | Stationarity, cross-correlation with the KPI, STL, VIF, quality warnings | |
| Check a model can be trusted | a [governance policy](null-tests.md) | Null tests, benchmarks, residuals, stability, signs | |

The detail for each family: [Marketing-mix models](../models/mmm-framework.md),
[statsmodels time-series models](../models/statsmodels.md),
[forecasting](time-series.md), [tabular models](sklearn.md),
[the built-in models](../reference/models.md).

## When the chooser is not enough

- **Compare instead of guessing.** Fit two candidates and compare them on the same holdout:
  `mapi explore compare model://growth/a@v1 model://growth/b@v1` (metrics, ranking and
  Diebold-Mariano tests), or run a [bake-off workflow](workflow-cookbook.md#forecast-bake-off).
- **Let the data decide the details.** The readiness workflow chooses differencing and ARMA
  orders by tests and information criteria, and records why.
- **Make "good enough" explicit.** A governance policy turns "it looks fine" into checks with
  thresholds, evidence and a second person's sign-off.
