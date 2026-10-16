# Guided mode <span class="mapi-badge">v0.4</span>

Guided mode is for analysts who know their business but not PyMC. It is organised around
**tasks, not screens**: pick what you want to learn, answer questions about your columns,
state what you already believe, run, read a plain-language report and play with what-if
sliders. Behind every answer sits the same `ModelSpec` that Expert mode edits, so "View as
YAML" always shows the real thing and Expert edits round-trip back.

This page explains the concepts and the contracts in `mapi_spec` that the server, the web app
and the client share. The endpoints are listed in the
[HTTP API reference](../reference/http-api.md#guided-and-integrations).

!!! info "Status"
    The server and the runner serve every contract on this page: the data path (templates,
    role detection, drafts, the sandboxed ingest presets, the BayesInsight import and the
    persona lock, walked through in
    [From a spreadsheet to a Guided draft](../guides/guided-data.md)) and the modelling path
    (business priors, lift tests, the scorecard, the what-if grids and the forecast and
    counterfactual views, in [Guided modelling](../guides/guided-modelling.md)). The web app's
    Guided screens use all of it: see
    [Your first analysis in Guided mode](../guides/guided-tutorial.md).

## The persona lock

Every user picks Guided or Expert in the header, and the choice is remembered (until then, a
self-service workspace starts in Guided mode and any other in Expert). A project admin
can **lock a project to Guided mode** (`Project.guided_locked`, set with
`PATCH /v1/tenants/{tenant_id}/projects/{project_id}`, audited as `project.guided_lock`):
members below admin then only see Guided screens.

The API holds those members to the same line when they **write specs**
(`mapi_spec.guided.guided_lock_issues`): a stored spec must be a Guided spec - its
`metadata.labels.guided` names a template and `model.use` is that template's plugin - and
the fields the template marks `advanced` or `hidden` keep their values (a revision keeps its
parent's, a new spec the template's). Answers, `basic` settings, business priors and lift
tests stay editable. Anything else is `403 FORBIDDEN` with one issue per JSON pointer.
Admins, static validation, runs and scoring - and so integrations calling endpoints - are not
affected.

## 1. "What do you want to learn?" - templates

A template (`GuidedTemplate`) is data: a title and description in plain words, the questions,
the built-in plugin it uses, a `ModelSpec` template with `{{placeholder}}` strings, default
settings, the scorecard checks that apply and `x-ui-level` hints for every field.

| Template | Plugin | For | What-if | Forecast | Business priors |
|---|---|---|---|---|---|
| `geo_mmm` - Geo marketing mix | `mmm.hierarchical_geo` | What each channel adds in each market; moving budget | yes | yes | yes, and lift tests |
| `weekly_forecast` - Weekly forecast | `ts.hsgp_trend` | One series, weeks or months ahead, with a range | — | yes | — |
| `store_demand` - Demand by store | `tabular.regressor` | Units per store and week from prices and promotions (low/median/high) | — | yes | — |
| `customer_choice` - Customer choice | `choice.multinomial_logit` | Which product customers pick and how price shifts the choice | presets | — | — |

!!! tip "Customer choice: a conditional logit with household effects"
    `customer_choice` targets the built-in `choice.multinomial_logit`: alternative-specific
    utilities over price and promotion, household-specific product preferences (a
    random-intercept logit fitted at its posterior mode) and Bayesian-bootstrap draws over
    households, so shares, counterfactual deltas and sums over trips keep their uncertainty.
    A product without a price on a trip was not on the shelf; new households draw their
    preferences from the fitted spread. Choice values must name products the way the price
    columns do (`price_cola` goes with the choice `cola`). See
    [Built-in models](../reference/models.md#choicemultinomial_logit-which-product-customers-pick).

Every template fits with `fit.mode: evaluate_then_refit`: the model is first fitted without
the latest periods to measure its forecast error, then refitted on everything, which is what
endpoints require.

## 2. Questions instead of a role mapper

Instead of mapping columns to dims and roles, the user answers questions such as "Which column
is the result you want to explain?" or "Which columns are media spend (one per channel)?". A
question (`GuidedQuestion`) says what its answer becomes:

- a **dim** (`date` → the time dim, `geo` → markets), with the dim's policy;
- a **variable with a role** (`kpi` → the target);
- **one variable along a new dim** built from several columns (`spend` → one `spend` variable
  along `channel`, one label per column: `tv_spend` becomes channel `tv`);
- several separate variables (`controls`), or a choice or a number (frequency, holdout length).

### Role detection

The browser profiles the upload locally (nothing hostile reaches the servers just to be
previewed) and sends `ColumnProfile`s - name, type, row and missing counts, distinct values,
range, detected date frequency. `POST /v1/guided/detect-roles` (or the pure function
`mapi_spec.guided.suggest_roles`) ranks the columns for every question and pre-fills answers:

- names are split into words (`SearchSpend` → `search spend`) and matched against each
  question's keywords;
- types must fit (a date question wants dates; spend wants numbers, preferably never negative);
- label-like columns (few repeated values) suit dim questions; mostly empty columns lose points.

- dates in order and trip numbers that count up suit ordered dims; spend with negative values
  and columns that never change lose points.

Every candidate comes with a `confidence` between 0 and 1 and a `reason_plain` such as "named
like a revenue column; never negative". Answers are pre-filled at confidence 0.5 or more, a
column answers at most one question, and the data frequency is read from the chosen date
column. Required questions left without a candidate come back as warnings.

Detection also works on a **stored dataset version** (`dataset` instead of `columns`): its
ingest run wrote `columns.json`, the sandbox's profile of every uploaded column (for MFF
uploads: of the pivoted variables), and versions without one are profiled from their stored
schema and summary - the API only ever reads that JSON.

### Drafts and "View as YAML"

`POST /v1/guided/drafts` (`mapi_spec.guided.build_draft`) turns `GuidedAnswers` into a
`GuidedDraft`:

- an `IngestMapping` for the upload (column names become safe dataset names:
  `Revenue (EUR)` → `revenue_eur`);
- the `ModelSpec` document, with every placeholder filled from answers, template defaults or
  lookups (for example the season length from the frequency), and the business priors and
  lift tests in its `calibration` block;
- the same spec as YAML.

The server validates the draft statically against the catalog, with the schema the mapping
promises (`mapi_spec.guided.draft_schema`), and returns the findings in `issues`. Nothing is
stored: upload the data as a dataset version with the mapping and save the spec with
`POST /v1/specs`. Wrong answers come back as `GUIDED_ANSWERS_INVALID` pointing at
`/answers/<question>`.

## File formats and presets

Uploads are parsed only in a sandboxed ingest run. Besides an explicit column mapping, the
`IngestMapping.preset` field names a file contract marketing teams already have:

| Format or preset | Contract |
|---|---|
| `csv`, `parquet` | A column mapping: `dims`, `vars` and, for wide tables, `wide` (`WideVarMapping`: one variable from one column per label) |
| `mff` (format or preset) | The MFF long table below |
| `meridian` | Google Meridian's `CoordToColumns` wide table (`MeridianColumns`): `time`, `geo`, `kpi`, `controls`, and parallel `media`/`media_spend` (and reach/frequency) lists per channel |
| `bayesinsight` | An MFF table plus BayesInsight's `metadata.json` (`BayesInsightMetadata`) |
| `zarr_zip`, `netcdf` | Self-describing labelled datasets |

The **MFF** ("model feature format") long table has one row per period, key combination and
variable - the columns BayesInsight uses (`mapi_spec.ingest.MFF_COLUMNS`):

| Column | Meaning |
|---|---|
| `Period` | The date; weekly data must fall on the same weekday |
| `Geography`, `Product`, `Outlet`, `Campaign`, `Creative` | Key columns; each becomes a dim when it has more than one value |
| `VariableName` | The variable (`Sales`, `TV_Spend`, ...) |
| `VariableValue` | The value |

MFF tables can also be pivoted and then mapped like a CSV (the variables become columns),
`MffOptions.combine` joins key columns into one dim, and the BayesInsight preset keeps only
what `metadata.json` allows. The ingest enforces caps on rows, columns, cell length and
decompression and explains every refusal; see
[File formats and presets](../guides/guided-data.md#file-formats-and-presets).

### BayesInsight models

A BayesInsight project imports as a draft (`POST /v1/guided/import/bayesinsight`,
`mapi_spec.bayesinsight.import_bayesinsight`) for the built-in geo MMM
`mmm.hierarchical_geo` (Q8): the data with the `bayesinsight` preset - media variables
gathered into `spend` along `channel`, the `row_ids` key columns joined into one `geo` dim -
and a spec that carries every feature with an equivalent (delayed or geometric adstock and
its Beta/Exponential priors per channel, Hill saturation, pooling, standardised controls,
Fourier seasonality). Features without one - non-Normal likelihoods, coefficient priors in
KPI units, Weibull adstock, S-shaped curves, sign constraints - are listed as
`IMPORT_UNSUPPORTED` warnings; models that cannot run (no single `exog` KPI, no media,
names that do not match `row_ids`) are refused with `IMPORT_UNSUPPORTED`.

`target: mmm.bayesinsight` imports for the dedicated built-in
[`mmm.bayesinsight`](../reference/models.md#mmmbayesinsight-a-bayesinsight-model_defjson),
which runs BayesInsight's model as `model_def.json` defines it: its likelihoods (Normal,
log-normal, Poisson, negative binomial, zero-inflated), coefficient and intercept priors in
the KPI's own units, Hill, linear and S-shaped curves before delayed, geometric or Weibull
carry-over, enforced sign constraints, fixed and partially pooled effects per row id, Fourier
seasons and local spline trends. Every row id becomes a panel dim of its own, variables keep
their MFF names, and the draft is validated against the catalog like any other. What the
plugin cannot run (an `MvNormal` likelihood, time transforms, unknown prior families) falls
back to BayesInsight's default with an `IMPORT_UNSUPPORTED` warning; faithful but surprising
behaviour (no likelihood means Poisson, random effects with `partial_pooling_sigma: 0` pool
nothing) is an `info` issue.

## 3. Business priors and lift tests

Users state what they already believe **in business units** (`BusinessPriors`):

```yaml
calibration:
  business_priors:
    channels:
      tv:     {roi: [1.5, 4.0], credibility: 0.8}   # every euro returns 1.5-4 euros, 80% sure
      search: {roi: [2.0, 6.0], credibility: 0.9}
      social: {contribution_share: [0.02, 0.10]}    # 2-10% of revenue
    baseline_share: {share: [0.5, 0.8]}
  lift_tests:
    - {id: tv-holdout-2026q2, channel: tv, geo: de, start: 2026-04-06, end: 2026-05-03,
       spend_delta: -120000, kpi_delta: -260000, ci: [-380000, -140000], ci_level: 0.9}
```

- **Credibility** is the probability that the truth lies inside the range. A range
  `[low, high]` at credibility *c* becomes a log-normal (ROI) or logit-normal (share) whose
  central *c* interval is exactly that range (`lognormal_from_range`).
- Shares must fit inside the KPI: the smallest shares (channels plus baseline) cannot add up
  to more than 100% (`BUSINESS_PRIORS_INVALID`).
- **Lift tests** are experiment results used as calibration data: extra (or removed) spend on
  one channel, optionally in one geo, over a window, and the incremental KPI it caused with a
  standard error or confidence interval. The model treats each as an extra observation of the
  channel's incremental effect.
- Only models that declare them (`Capabilities.calibration`) accept them; otherwise static
  validation reports `CALIBRATION_UNSUPPORTED`.

Turning beliefs into model-space priors needs the data's scale and the model's transforms, so
it runs in the sandbox as a `translate_priors` run (`POST /v1/guided/prior-translations`). Its
result (`PriorTranslationResult`) holds the priors to write at `/model/settings/priors`
(`apply_prior_translation`) and a **prior-predictive preview in business units**: ROI and
contribution-share distributions per channel, the baseline share and a KPI fan - so users see
what their beliefs imply before fitting. The math, the lift-test likelihood and the checks
each layer runs are in [Guided modelling](../guides/guided-modelling.md).

## 4. Run, then the sanity scorecard { #scorecard }

Every fit gets a `Scorecard` of plain-language checks, each `pass`, `warn` or `fail`, with the
measured value, the threshold, a `message_plain`, a suggested action and a link back to this
page. `overall` is the worst item. Templates choose their checks and may tighten thresholds.
The catalog is `mapi_spec.scorecard.SCORECARD_CHECKS`:

#### Convergence

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-convergence-rhat"></a>`convergence.rhat` (Chains agree) | `r_hat_max` | warn above 1.01, fail above 1.05 | Run more tuning and draws, or simplify the model. |
| <a id="check-convergence-ess"></a>`convergence.ess` (Enough independent draws) | `ess_bulk_min` | warn below 400, fail below 100 | Increase the number of draws. |
| <a id="check-convergence-divergences"></a>`convergence.divergences` (Sampler stayed on track) | `divergence_rate` | warn above 0%, fail above 1% | Raise target_accept, or use more informative priors. |

#### Holdout error

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-holdout-mape"></a>`holdout.mape` (Holdout error) | `mape` | warn above 10%, fail above 25% | Check the last periods of data for gaps or one-off events, and add holidays or controls that explain them. |
| <a id="check-holdout-coverage-90"></a>`holdout.coverage_90` (Uncertainty is honest) | `coverage_90` | warn when more than 10% away from 90%, fail when more than 25% away | Look for trends or shifts the model does not capture. |
| <a id="check-holdout-hit-rate"></a>`holdout.hit_rate` (Beats a naive guess) | `accuracy_lift` | warn below +2 points, fail below -10 points | Add what drives the choice for every product (price, promotions, displays), or collect more trips per customer. |

`holdout.hit_rate` is the check of the `customer_choice` template: on each household's
held-out trip, the model's most likely product is compared with the product bought
(`accuracy`, the hit rate) and with always guessing the most popular product of the held-out
trips (`accuracy_naive` - a strict baseline, since it knows the holdout's own shares).
`accuracy_lift` is the difference in percentage points. The template also reports the
`log_score` (mean log probability of the bought product; higher is better) and the
multi-class `brier` score (0 is perfect) in the model's metrics.

#### Plausibility

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-plausibility-negative-roi"></a>`plausibility.negative_roi` (No channel loses money) | `negative_roi_share` | warn above 10%, fail above 50% | Check the channel's spend data, or state an ROI range in business priors. |
| <a id="check-plausibility-roi-absurd"></a>`plausibility.roi_absurd` (Returns are believable) | `roi_max` | warn above 20, fail above 100 | Check spend units and gaps, or state an ROI range in business priors. |
| <a id="check-plausibility-share-outlier"></a>`plausibility.share_outlier` (No channel dominates) | `max_channel_share` | warn above 60%, fail above 90% | Check for a missing control (price, promotions, seasonality). |
| <a id="check-plausibility-baseline-share"></a>`plausibility.baseline_share` (Baseline is plausible) | `baseline_share` | warn when more than 40% away from 50%, fail when more than 48% away | State a baseline share in business priors, or review the controls. |
| <a id="check-plausibility-prior-overlap"></a>`plausibility.prior_overlap` (The data informed the model) | `prior_overlap_max` | warn above 90%, fail above 99% | Add data that varies the driver (or a lift test), or state a business prior you trust for it. |

#### Data

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-data-missing"></a>`data.missing` (Few missing values) | `missing_fraction` | warn above 5%, fail above 30% | Fill the gaps in the source data, or drop the affected series. |
| <a id="check-data-history"></a>`data.history` (Enough history) | `history_periods` | warn below 104, fail below 52 | Add more history if you have it. |
| <a id="check-data-spend-variation"></a>`data.spend_variation` (Spend varies enough) | `spend_cv_min` | warn below 10%, fail below 2% | Add a lift test for the channel, or state its ROI in business priors. |

How the runner measures each check is in
[Guided modelling](../guides/guided-modelling.md#the-sanity-scorecard). The scorecard also
gates promotion to an endpoint: see
[Integrations](integrations.md#promotion-gate).

## 5. What-if sliders

Sliders answer instantly because every MMM fit precomputes **per-draw response grids**
(`ResponseGridView`, `ui/grids/response_curves.json`, format `mapi.response-grid/v1`): for
each thinned posterior draw, channel and spend multiplier, the channel's total contribution
over the fit window, the spend at each grid point, the actual spend and the baseline.

A `WhatIfScenario` sets a multiplier per channel (`1` = what was actually spent). The
interpolation contract, shared by the web app and `mapi_spec.whatif.evaluate_scenario`:

1. a channel without a multiplier keeps `1`; multipliers outside the grid are refused
   (`SCENARIO_OUT_OF_RANGE`) - the grid is never extrapolated;
2. per draw and channel, the contribution is interpolated **linearly** between the two grid
   points around the multiplier, and so is spend;
3. per draw, KPI = baseline + the channels' contributions, and the change is taken against all
   multipliers at `1`; means and quantiles are computed **after** summing, so the uncertainty
   band stays joint across channels.

The web app answers the sliders in the browser with a TypeScript port of this contract, which
its unit tests check against golden cases the spec tests export
(`packages/spec/tests/snapshots/whatif_golden.json`).

**Run exact** turns the scenario into a `counterfactual` score request with one `multiply`
intervention per changed channel and submits a real score run
(`POST /v1/guided/requests:compile`, then score).

### Forecasts and counterfactuals

Guided forecasts (`forecast_request`) ask for a horizon and a policy for covariates whose
future the user did not supply (`ScoreRequest.future_covariates`): `carry_last` (default in
Guided mode), `seasonal_naive`, `zero`, or `supplied` with a scenario dataset. Intervention
presets build counterfactuals in one click: **channel off** (`ChannelOff`: that channel's spend
set to zero) and **budget × k** (`BudgetScale`: every, or the listed, channel's spend scaled
by *k*), over the fit window or over a forecast horizon. Responses default to expected values
with the mean and a 90% interval (Q2).
