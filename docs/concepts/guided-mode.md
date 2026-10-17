# Guided mode <span class="mapi-badge">v0.4</span>

Guided mode is for analysts who know their business but do not know PyMC. Guided mode uses
**tasks, not screens**. You select what you want to learn and answer questions about your
columns. You tell what you already believe. Then you start a run, read a report in plain
language and move the what-if sliders. Each answer goes into the same `ModelSpec` that Expert
mode edits. Thus "View as YAML" always shows the real spec, and edits in Expert mode come back
to Guided mode.

This page tells about the concepts and the contracts in `mapi_spec`. The server, the web app
and the client use the same contracts. The
[HTTP API reference](../reference/http-api.md#guided-and-integrations) gives the endpoints.

!!! info "Status"
    The server and the runner serve all the contracts on this page. The data path includes
    the templates, role detection, drafts, the ingest presets in the sandbox and the persona
    lock. [From a spreadsheet to a Guided draft](../guides/guided-data.md) shows the data path.
    The modelling path includes business priors, lift tests, the scorecard, the what-if grids
    and the forecast and counterfactual views. [Guided modelling](../guides/guided-modelling.md)
    shows the modelling path. The Guided screens of the web app use all of it. Refer to
    [Your first analysis in Guided mode](../guides/guided-tutorial.md).

## The persona lock

Each user selects Guided or Expert in the header, and the app keeps the selection. Before a
user selects a mode, a self-service workspace starts in Guided mode. All other workspaces
start in Expert mode. A project admin can **lock a project to Guided mode**
(`Project.guided_locked`). In the web app, use **Settings > Mode > Lock
… to Guided mode**. With the API, use
`PATCH /v1/tenants/{tenant_id}/projects/{project_id}`. The audit log records the change as
`project.guided_lock`. After the lock, members below admin see only Guided screens.

The API applies the same limit when these members **write specs**
(`mapi_spec.guided.guided_lock_issues`). A stored spec must be a Guided spec. Its
`metadata.labels.guided` names a template, and `model.use` is the plugin of that template.
The fields that the template marks `advanced` or `hidden` keep their values. A revision keeps
the values of its parent, and a new spec keeps the values of the template. Members can edit
answers, `basic` settings, business priors and lift tests. All other changes get
`403 FORBIDDEN`, with one issue for each JSON pointer.

The lock has no effect on admins, static validation, runs or scoring. Thus integrations that
call endpoints also have no limit.

## 1. "What do you want to learn?" - templates

A template (`GuidedTemplate`) is data. It contains these items:

- a title and a description in plain words
- the questions
- the built-in plugin that it uses
- a `ModelSpec` template with `{{placeholder}}` strings
- default settings
- the scorecard checks that apply
- `x-ui-level` hints for each field.

| Template | Plugin | For | What-if | Forecast | Business priors |
|---|---|---|---|---|---|
| `geo_mmm` - Geo marketing mix | `mmm.bayesian` (mmm-framework) | The contribution of each channel in each market; budget changes between channels | yes | yes | yes, and lift tests |
| `weekly_forecast` - Weekly forecast | `ts.hsgp_trend` | One series, some weeks or months ahead, with a range | — | yes | — |
| `store_demand` - Demand by store | `tabular.regressor` | Units for each store and week from prices and promotions (low/median/high) | — | yes | — |
| `customer_choice` - Customer choice | `choice.multinomial_logit` | The product that customers select, and how price changes the selection | presets | — | — |

!!! tip "Customer choice: a conditional logit with household effects"
    `customer_choice` uses the built-in `choice.multinomial_logit`. The model has utilities
    for each alternative from price and promotion. It has a product preference for each
    household: a random-intercept logit, fitted at its posterior mode. It also has
    Bayesian-bootstrap draws over households. Thus shares, counterfactual deltas and sums over
    trips keep their uncertainty. If a product has no price on a trip, the product was not on
    the shelf. New households draw their preferences from the fitted spread. Choice values
    must name products in the same way as the price columns (`price_cola` goes with the choice
    `cola`). Refer to
    [Built-in models](../reference/models.md#choicemultinomial_logit-which-product-customers-pick).

All templates fit with `fit.mode: evaluate_then_refit`. First, the platform fits the model
without the latest periods, to measure its forecast error. Then it fits the model again on all
the data. Endpoints need this fit mode.

## 2. Questions instead of a role mapper

The user does not map columns to dims and roles. The user answers questions, for example
"Which column is the result you want to explain?" or "Which columns are media spend (one per
channel)?". A question (`GuidedQuestion`) tells what its answer becomes:

- a **dim** (`date` → the time dim, `geo` → markets), with the policy of the dim
- a **variable with a role** (`kpi` → the target)
- **one variable along a new dim** that the platform makes from some columns (`spend` → one
  `spend` variable along `channel`, with one label for each column: `tv_spend` becomes channel
  `tv`)
- some separate variables (`controls`), or a choice or a number (frequency, holdout length).

### Role detection

The browser makes a profile of the upload on the local computer. Thus no dangerous data goes
to the servers for a preview. The browser sends `ColumnProfile`s. Each profile has the name,
the type, the row and missing counts, the distinct values, the range and the date frequency
that the browser found. `POST /v1/guided/detect-roles` (or the pure function
`mapi_spec.guided.suggest_roles`) ranks the columns for each question and fills in the
answers:

- The function splits names into words (`SearchSpend` → `search spend`). It compares the words
  with the keywords of each question.
- Types must agree. A date question needs dates. A spend question needs numbers, and numbers
  that are never negative are better.
- Columns that look like labels (few values that repeat) are good for dim questions. Columns
  that are almost empty lose points.
- Dates in sequence and trip numbers that increase are good for ordered dims. Spend with
  negative values and columns that do not change lose points.

Each candidate has a `confidence` between 0 and 1 and a `reason_plain`, for example "named
like a revenue column; never negative". The function fills in an answer when the confidence
is 0.5 or more. A column answers a maximum of one question. The function reads the data
frequency from the selected date column. If a necessary question has no candidate, the
function returns a warning.

Detection also works on a **stored dataset version** (`dataset` instead of `columns`). The
ingest run of the version wrote `columns.json`. This file is the profile that the sandbox made
of each uploaded column (for MFF uploads, of the pivoted variables). For versions without this
file, the platform makes the profile from the stored schema and summary. The API reads only
that JSON.

### Drafts and "View as YAML"

`POST /v1/guided/drafts` (`mapi_spec.guided.build_draft`) changes `GuidedAnswers` into a
`GuidedDraft`. The draft contains these items:

- an `IngestMapping` for the upload. Column names become safe dataset names:
  `Revenue (EUR)` → `revenue_eur`.
- the `ModelSpec` document. Each placeholder gets a value from the answers, the template
  defaults or lookups (for example, the season length from the frequency). The `calibration`
  block contains the business priors and lift tests.
- the same spec as YAML.

The server does a static validation of the draft against the catalog. It uses the schema that
the mapping gives (`mapi_spec.guided.draft_schema`). It returns the findings in `issues`. The
server stores nothing. Upload the data as a dataset version with the mapping. Then save the
spec with `POST /v1/specs`. Incorrect answers come back as `GUIDED_ANSWERS_INVALID`, with a
pointer to `/answers/<question>`.

## File formats and presets

Only a sandboxed ingest run parses uploads. You can give an explicit column mapping. Or the
`IngestMapping.preset` field can name a file contract that marketing teams already use:

| Format or preset | Contract |
|---|---|
| `csv`, `parquet` | A column mapping: `dims`, `vars` and, for wide tables, `wide` (`WideVarMapping`: one variable from one column for each label) |
| `mff` (format or preset) | The MFF long table below |
| `meridian` | The `CoordToColumns` wide table of Google Meridian (`MeridianColumns`): `time`, `geo`, `kpi`, `controls`, and parallel `media`/`media_spend` (and reach/frequency) lists for each channel |
| `zarr_zip`, `netcdf` | Labelled datasets that describe themselves |

The **MFF** ("model feature format") long table has one row for each period, key combination
and variable. mmm-framework uses the same MFF convention (`mapi_spec.ingest.MFF_COLUMNS`):

| Column | Meaning |
|---|---|
| `Period` | The date. Weekly data must be on the same day of the week. |
| `Geography`, `Product` | Panel levels. Each column becomes a dim when it has more than one level. A blank label (or `National`, `Total`, `All`) is the total. The ingest spreads a variable that has values only in the total (national TV) across the levels. Geographies get equal parts. Products get parts by the share of the KPI (`MffOptions.allocation`). mmm-framework does the same. |
| `Outlet`, `Campaign`, `Creative` | Breakdowns. The ingest adds together rows that are different only in these columns. `MffOptions.split` keeps the column as a dim. |
| `VariableName` | The variable (`Sales`, `TV_Spend`, ...) |
| `VariableValue` | The value |

You can also pivot MFF tables and then map them like a CSV file (the variables become
columns). `MffOptions.combine` joins key columns into one dim. The ingest applies limits on
rows, columns, cell length and decompression. It explains each refusal. Refer to
[File formats and presets](../guides/guided-data.md#file-formats-and-presets).

## 3. Business priors and lift tests

Users tell what they already believe **in business units** (`BusinessPriors`):

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

- **Credibility** is the probability that the true value is in the range. A range
  `[low, high]` at credibility *c* becomes a log-normal (ROI) or a logit-normal (share)
  distribution. The central *c* interval of this distribution is exactly that range
  (`lognormal_from_range`).
- Shares must fit in the KPI. The smallest shares (channels plus baseline) cannot add up to
  more than 100% (`BUSINESS_PRIORS_INVALID`).
- You must give a minimum of one belief, but any one is sufficient. `baseline_share` alone (no
  `channels`) makes only the baseline narrower. Channels without a belief keep the default
  priors of the model.
- **Lift tests** are experiment results that the model uses as calibration data. A lift test
  has extra (or removed) spend on one channel, optionally in one geo, over a window. It also
  has the incremental KPI that the spend caused, with a standard error or a confidence
  interval. The model uses each lift test as an extra observation of the incremental effect of
  the channel.
- Only models that declare them (`Capabilities.calibration`) accept them. For other models,
  static validation reports `CALIBRATION_UNSUPPORTED`.

To change beliefs into priors in model space, the platform needs the scale of the data and the
transforms of the model. Thus this change occurs in the sandbox as a `translate_priors` run
(`POST /v1/guided/prior-translations`). Its result (`PriorTranslationResult`) contains the
priors to write at `/model/settings/priors` (`apply_prior_translation`). It also contains a
**prior-predictive preview in business units**. The preview shows the ROI and
contribution-share distributions for each channel, the baseline share and a KPI fan. Thus
users see the effect of their beliefs before the fit. [Guided modelling](../guides/guided-modelling.md)
gives the math, the lift-test likelihood and the checks that each layer does.

## 4. Run, then the sanity scorecard { #scorecard }

Each fit gets a `Scorecard` of checks in plain language. Each check is `pass`, `warn` or
`fail`. It has the measured value, the threshold, a `message_plain`, a suggested action and a
link back to this page. `overall` is the worst item. Templates select their checks and can make
thresholds stricter. The catalog is `mapi_spec.scorecard.SCORECARD_CHECKS`:

#### Convergence

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-convergence-rhat"></a>`convergence.rhat` (Chains agree) | `r_hat_max` | warn above 1.01, fail above 1.05 | Run more tuning and draws, or make the model simpler. |
| <a id="check-convergence-ess"></a>`convergence.ess` (Enough independent draws) | `ess_bulk_min` | warn below 400, fail below 100 | Increase the number of draws. |
| <a id="check-convergence-divergences"></a>`convergence.divergences` (The sampler had no problems) | `divergence_rate` | warn above 0%, fail above 1% | Increase target_accept, or use more informative priors. |

#### Holdout error

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-holdout-mape"></a>`holdout.mape` (Holdout error) | `mape` | warn above 10%, fail above 25% | Look for gaps or one-time events in the last periods of data. Add holidays or controls that explain them. |
| <a id="check-holdout-coverage-90"></a>`holdout.coverage_90` (The uncertainty is correct) | `coverage_90` | warn when more than 10% away from 90%, fail when more than 25% away | Look for trends or shifts that the model does not include. |
| <a id="check-holdout-hit-rate"></a>`holdout.hit_rate` (Better than a simple guess) | `accuracy_lift` | warn below +2 points, fail below -10 points | Add the drivers of the choice for each product (price, promotions, displays). Or collect more trips for each customer. |

`holdout.hit_rate` is the check of the `customer_choice` template. For each household, the
check uses the trip in the holdout. It compares the most probable product of the model with
the product that the household bought (`accuracy`, the hit rate). It also compares the model
with a guess that is always the most popular product of the holdout trips (`accuracy_naive`).
This baseline is strict, because it knows the shares of the holdout. `accuracy_lift` is the
difference in percentage points. The template also reports two more values in the metrics of
the model. These are the `log_score` (the mean log probability of the bought product, where
higher is better) and the multi-class `brier` score (0 is perfect).

#### Plausibility

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-plausibility-negative-roi"></a>`plausibility.negative_roi` (No channel loses money) | `negative_roi_share` | warn above 10%, fail above 50% | Examine the spend data of the channel, or give an ROI range in business priors. |
| <a id="check-plausibility-roi-absurd"></a>`plausibility.roi_absurd` (Returns are not too high) | `roi_max` | warn above 20, fail above 100 | Examine the spend units and gaps, or give an ROI range in business priors. |
| <a id="check-plausibility-share-outlier"></a>`plausibility.share_outlier` (No channel is too large) | `max_channel_share` | warn above 60%, fail above 90% | Look for a missing control (price, promotions, seasonality). |
| <a id="check-plausibility-baseline-share"></a>`plausibility.baseline_share` (The baseline is in the usual range) | `baseline_share` | warn when more than 40% away from 50%, fail when more than 48% away | Give a baseline share in business priors, or examine the controls. |
| <a id="check-plausibility-prior-overlap"></a>`plausibility.prior_overlap` (The data changed the model) | `prior_overlap_max` | warn above 90%, fail above 99% | Add data where the driver changes (or a lift test). Or give a business prior for it that you trust. |

#### Data

| Check | Metric | Thresholds | What to do |
|---|---|---|---|
| <a id="check-data-missing"></a>`data.missing` (Few missing values) | `missing_fraction` | warn above 5%, fail above 30% | Fill the gaps in the source data, or remove the series that have gaps. |
| <a id="check-data-history"></a>`data.history` (Enough history) | `history_years` | warn below 2 years, fail below 1 year (the `weekly_forecast` template: below 0.5) | Add more history if you have it. |
| <a id="check-data-spend-variation"></a>`data.spend_variation` (Spend changes enough) | `spend_cv_min` | warn below 10%, fail below 2% | Add a lift test for the channel, or give its ROI in business priors. |

[Guided modelling](../guides/guided-modelling.md#the-sanity-scorecard) tells how the runner
measures each check. The scorecard also controls promotion to an endpoint. Refer to
[Integrations](integrations.md#promotion-gate).

## 5. What-if sliders

Sliders give answers immediately, because each MMM fit calculates **response grids for each
draw** before you use them (`ResponseGridView`, `ui/grids/response_curves.json`, format
`mapi.response-grid/v1`). The grid has values for each thinned posterior draw, channel and
spend multiplier. It contains the total contribution of the channel over the fit window and
the spend at each grid point. It also contains the actual spend and the baseline.

A `WhatIfScenario` sets a multiplier for each channel (`1` = the actual spend). The web app
and `mapi_spec.whatif.evaluate_scenario` use the same interpolation contract:

1. A channel without a multiplier keeps `1`. The contract refuses multipliers outside the grid
   (`SCENARIO_OUT_OF_RANGE`). It never extrapolates the grid.
2. For each draw and channel, the contract interpolates the contribution **linearly** between
   the two grid points around the multiplier. It does the same for spend.
3. For each draw, KPI = baseline + the contributions of the channels. The change is the
   difference from the KPI with all multipliers at `1`. The contract calculates means and
   quantiles **after** the sum. Thus the uncertainty band stays joint across channels.

The web app calculates the slider answers in the browser with a TypeScript port of this
contract. Its unit tests compare the port with golden cases that the spec tests export
(`packages/spec/tests/snapshots/whatif_golden.json`).

**Run exact** changes the scenario into a `counterfactual` score request, with one `multiply`
intervention for each changed channel. Then it starts a real score run
(`POST /v1/guided/requests:compile`, then score).

### Forecasts and counterfactuals

Guided forecasts (`forecast_request`) need a horizon. They also need a policy for covariates
that have no future values from the user (`ScoreRequest.future_covariates`). The policies are
`carry_last` (the default in Guided mode), `seasonal_naive`, `zero`, or `supplied` with a
scenario dataset. Intervention presets make counterfactuals with one click:

- **channel off** (`ChannelOff`): the spend of that channel is zero.
- **budget × k** (`BudgetScale`): the platform multiplies the spend of all channels, or of the
  listed channels, by *k*.

The presets apply over the fit window or over a forecast horizon. By default, responses are
expected values with the mean and a 90% interval (Q2).
