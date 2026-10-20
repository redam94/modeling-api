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
| What do you want to do? | measure what marketing adds · plan a budget · measure the effect of a campaign, a launch or a test · forecast · understand drivers · predict per item · understand choices · find regimes · see series move together · check data · validate a model · make a map or an image from measurements · fit a physical model to measurements · put items or images into classes (v0.14) |
| What does your data look like? | one number over time · a few related numbers · the same numbers for many markets, stores or products · purchases or choices · a table without time · a map, an image or values on a regular grid · images, pictures of the same size (v0.14) |
| How often do you record the data? | daily · weekly · monthly or less often · not over time |
| How much history? | under a year · one to three years · more |
| Other columns that could explain it? | yes (prices, promotions, weather, spend) · no |
| Do you need a range? | essential · it would be good · one number is enough |
| How much do you know about statistics? | new to it · the basics · expert |
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
| Measure marketing effects that go through visits, searches or awareness | `mmm.nested` | Direct and indirect effect of each channel. A measure can have empty periods, for example a monthly survey. National data. | The `mediated_mmm` Guided template. `mmm.bayesian` when price or promotions change sales much (it takes controls). |
| Measure marketing effects on two or more results, such as related products | `mmm.multivariate` | One budget, several results with correlated noise, and the effects between the results (associations, not causes). National data. | The `multi_kpi_mmm` Guided template. One `mmm.bayesian` model for each result when the results do not interact. |
| Measure marketing with a count (orders, sign-ups) or a share as the result | `mmm.bayesian` with `likelihood: poisson`, `negative_binomial` or `beta` | The predictions stay whole numbers or between 0 and 1 | `normal` when the counts are large. ROI priors and lift tests need it. |
| Measure the effect of a campaign, a launch or a price change on one series <span class="mapi-badge">v0.13</span> | `causal.impact` | The actual values against a prediction without the change, from the history and control series. The effect in each period and in total, with a range. | The `campaign_impact` Guided template. `causal.synthetic_control` when you have the same number for many markets. |
| Measure a geo-lift test (a campaign in some markets only) <span class="mapi-badge">v0.13</span> | `causal.synthetic_control` | A weighted mix of the other markets follows each test market. The result is a lift test for the MMM. | The `geo_lift` Guided template. The `geo_lift_mmm` workflow calibrates `mmm.bayesian` with the test. |
| Measure a change that some units got and other units never got <span class="mapi-badge">v0.13</span> | `causal.did` | Unit and time effects, and one effect for each period before and after the change (event study) | `causal.synthetic_control` when only a few units got the change |
| Forecast one series with known drivers (price, promotions) | `ts.sarimax` | Past values, season and drivers. Prediction intervals. Forecasts from planned drivers. | The `ts_readiness_arimax` workflow. It selects the differencing and orders, and proves that the model is better than naive. |
| Forecast without drivers, fast and robust | `ts.ets` or `ts.theta` | Strong, simple baselines that are difficult to beat | The `forecast_bakeoff` workflow, which lets them compete |
| See the trend under the noise | `ts.unobserved_components` | Level, trend, season and noise as separate parts | `ts.hsgp_trend` for a Bayesian smooth trend. `ts.bsts` for the same parts with ranges, gaps in the data and a `filter` mode. |
| Know how an effect builds up over weeks | `ts.ardl` | Lags of the drivers, selected automatically | `ts.ecm` when the result and drivers have the same trend |
| Several series that drive each other | `ts.var` | Impulse responses, variance decompositions, Granger tests | `ts.vecm` when they are tied in the long run. The `var_vecm_cointegration` workflow decides. |
| Find regimes (normal or caused by promotions) | `ts.markov_switching` | Probability of each regime for each period | `ts.gaussian_hmm` for a Bayesian hidden-state model |
| Predict a value for each store, product or customer | `sklearn.tabular_regressor` | Gradient boosting or forests. Quantiles and conformal intervals. | `tabular.hierarchical_glm` when groups must borrow strength |
| Understand which product people select | `choice.multinomial_logit` | Price and promotion effects, household effects | |
| Predict yes or no for each row <span class="mapi-badge">v0.14</span> | `sklearn.tabular_classifier` | The probability of yes. Logistic regression, forests, xgboost, histogram boosting, LightGBM or CatBoost. | `sklearn.multiclass_classifier` for three or more classes |
| Put each row into one of three or more classes <span class="mapi-badge">v0.14</span> | `sklearn.multiclass_classifier` | The probability of each class. ROC and precision-recall curves for each class, and a confusion matrix. | `tabular.hierarchical_glm` with `family: ordinal` when the classes have an order |
| Classify small images <span class="mapi-badge">v0.14</span> | `tabular.image_logistic` | A Bayesian model on the main patterns of the pixels. A range for the probability of each class. | `sklearn.multiclass_classifier` on features that you calculate first, for larger images. See [Image data](image-data.md). |
| Classify thousands of images or more <span class="mapi-badge">v0.15</span> | `dl.cnn_classifier` | A neural network that trains on all images of a collection. Training curves, early stopping and checkpoints. The GPU class makes it faster. | `dl.finetune_classifier` when you have pretrained weights. See [Deep learning](deep-learning.md). |
| Classify images with few labels and a pretrained network <span class="mapi-badge">v0.15</span> | the step `dl.embed` and `sklearn.multiclass_classifier` | Image features from a frozen network, then a fast classifier | `tabular.image_logistic` with `input: features` for a range on each probability |
| Predict a value or a class for each row of a large table with a neural network <span class="mapi-badge">v0.15</span> | `dl.mlp` | Relations that are not linear. Training and validation curves. Draws from the last layer. | `sklearn.tabular_regressor` first: on most tables, boosted trees are as good and faster |
| Model counts with many zeros, shares or positive amounts <span class="mapi-badge">v0.14</span> | `tabular.hierarchical_glm` with `family: zero_inflated_poisson`, `negative_binomial`, `beta` or `gamma` | Group effects with sharing, and a likelihood that fits the data | |
| Check data before you model it | the [Data Explorer](data-explorer.md) | Stationarity, cross-correlation with the KPI, STL, VIF, quality warnings | |
| Make sure that you can trust a model | a [governance policy](null-tests.md) | Null tests, benchmarks, residuals, stability, signs | |

For the details of each family, see these pages:
[Marketing-mix models](../models/mmm-framework.md),
[statsmodels time-series models](../models/statsmodels.md),
[forecasting](time-series.md), [tabular models](sklearn.md) and
[the built-in models](../reference/models.md).

## Science models, compared

The science models (`sci.*`, package `mapi-plugin-science`) come from the
[pymc-challenges case studies](../tutorials/pymc-challenges/index.md). They are for maps,
images, fields, graphs, trees and angles. All of them are Bayesian and give a range for each
value. The `base-bayes` runner image contains them. The chooser shows them only when the
catalog has the plugin.

To find them with **Help me choose**, answer the goal "Make a map or an image from
measurements" or "Fit a physical model to measurements". Then answer the data shape "A map,
an image or values on a regular grid". The graph, tree and angle models use the goal
"Understand what causes changes in a number" with the data shape "A table of items without a
time order". Most of these models need expert-level statistics knowledge. Thus, if you answer
"new to it", they rank lower and show a caution.

```bash
api GET "/v1/catalog/recommendations?goal=map_fields&data_shape=grid&kind=model"
```

| Your data | Use | When to use it |
|---|---|---|
| Counts of events in each cell of a map, for each period | `sci.lgcp_grid` | You want a map of the rate and a forecast of the next periods |
| Counts of cells across a scratch, at some times | `sci.fisher_kpp` | You want the speed at which cells move, divide and close the wound |
| A map of the movement of a gel under cells | `sci.traction_force` | You want the force map and the strain energy |
| A telescope image of a lens galaxy with a ring or arcs | `sci.strong_lens` | You want the Einstein radius and the source galaxy without the lens |
| Temperatures at some points of a heated plate | `sci.heat_plate` | You want the heat properties and the temperature map of a new heater layout |
| Images of a pattern of spots or stripes that starts to grow | `sci.turing_pattern` | You want the reaction and diffusion rates and the probability of a Turing pattern |
| Raw frames of a STORM or PALM microscope (EMCCD camera) | `sci.storm_localisation` | You want the position of each molecule in nm, with an error bar |
| Many small images of the same size | `sci.ibp_features` | You want the parts that the images share, or to fill in hidden pixels |
| The colour names that the speakers of one language gave to colour chips | `sci.colour_naming` | You want the boundaries between the colour words and a map of the words |
| A CMB temperature map of a small flat patch, with a mask | `sci.cmb_field` | You want the sky behind the mask and the bandpowers |
| Counts of cases and expected counts for each area of a map | `sci.areal_bym2` | You want a map of the risk that borrows strength from the neighbour areas |
| Ties between pairs of nodes (yes or no) | `sci.network_latent_space` | You want the effect of pair covariates and the chance of a tie for each pair |
| The same variables, measured on many samples | `sci.graphical_model` | You want the direct links between the variables, with a probability for each link |
| One trait value for each species, and a tree of the species | `sci.phylo_regression` | You want a slope across species that does not count shared ancestors many times |
| Angles: wind directions or times of day | `sci.circular_regression` | You want the most probable direction for each hour and season, or the activity of species |

The About card of each model tells when *not* to use it. For example, `sci.areal_bym2` cannot
predict areas that are not on the map, and `sci.cmb_field` is not correct for a patch of more
than about 20 degrees. For all settings, see
[Scientific models](../reference/models.md#scientific-models-mapi-plugin-science).

The workflow gallery has recipes that compare science models or check them:

| Recipe | What it does |
|---|---|
| `sci_spatial_ladder` | Fits `sci.areal_bym2` with iid, ICAR and BYM2 area effects and keeps the best model by `elpd_loo` and stacking weights |
| `sci_network_ladder` | Fits `sci.network_latent_space` with covariates, sociality and a latent space and keeps the best model |
| `sci_phylo_signal` | Fits `sci.phylo_regression` with four residual kinds and keeps the best model |
| `sci_calibrate_then_design` | Fits `sci.heat_plate` with one run held out, and predicts a new heater layout only if the holdout error and R-hat are in their limits |
| `sci_image_model_check` | Shows residual maps and predicted images next to the observed image of an image model |
| `bayes_workflow_audit` | Does the checks of a careful Bayesian workflow for each model that gives draws |

For the documents of these recipes, see the [workflow cookbook](workflow-cookbook.md).

## When the chooser is not enough

- **Compare. Do not guess.** Fit two candidates and compare them on the same holdout. Use
  `mapi explore compare model://growth/a@v1 model://growth/b@v1` (metrics, ranking and
  Diebold-Mariano tests). Alternatively, run a
  [bake-off workflow](workflow-cookbook.md#forecast-bake-off).
- **Keep the candidates together.** From v0.14, put the fits of the candidates in one
  experiment. The compare page then shows their metrics, params and plots side by side. See
  [Compare runs and use experiments](run-comparison.md).
- **Let the data decide the details.** The readiness workflow selects differencing and ARMA
  orders with tests and information criteria. It records the reasons.
- **Make "good enough" explicit.** A governance policy changes "it looks fine" into checks.
  The checks have thresholds, evidence and the approval of a second person.
