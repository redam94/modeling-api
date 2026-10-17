---
description: Explore a dataset before modelling - distributions, time plots, missing data, outliers, correlations, lags, stationarity, seasonality, collinearity and data-quality warnings.
---

# Explore a dataset <span class="mapi-badge">v0.5</span>

The **Data Explorer** answers the questions to ask before you fit a model. It answers these
questions:

- What is in the data, and how does it move over time?
- Where are values missing or unusual?
- Which drivers move with the result, and with how much delay?
- Do series wander, or do they return to a level?
- How strong is the season?
- Which drivers overlap?
- What must you correct first?

The sandbox calculates each view. The control plane never reads your data. Each view comes
with Vega-Lite plots and captions in plain language. The [web app](#in-the-web-app) shows
all views. The other parts of this guide use the HTTP API, which the web app calls.

## In the web app

1. Open a dataset.
2. Click **Explore this version**. Alternatively, go to
   `/datasets/<dataset>/versions/<n>/explore`.

The page starts with the [About](#dataset-about) of the dataset. It lists the thirteen
sections in three groups:

- *The basics*: overview, data quality, distributions, over time, missing values and unusual
  values.
- *Relationships*: correlations, leads and lags, overlapping drivers and group comparison.
- *Time patterns*: memory (ACF/PACF), stable or wandering, and trend and season.

A tick marks a section that is ready. An empty circle marks a section that the platform
calculates when you ask for it. A dash marks a section that does not apply to this data. The
dash shows the reason.

![The Data Explorer in Guided mode: the About of the dataset, then the plain summary of each section](../assets/screenshots/explorer-data-overview-guided.png){ .mapi-shot }

- **Guided mode** shows the plain summary of each section first ("4 series return to a stable
  level, 3 series wander (model their changes)…"). To see the numbers, click
  *Show the statistics*. **Run the deeper checks** calculates all the sections that the
  ingest did not calculate. It uses sensible defaults (the target of the dataset as the KPI).
- **Expert mode** shows all statistics: test statistics and p-values, quantiles, VIFs and
  the condition number, and the significant lags. It also shows *All series*: the plots of
  the platform that are ready for export (`ui/explore/plots/<section>.json`).
  **Explore with options** sets the request of the run. The request contains the sections,
  the KPI and the dimensions that split the series. It also contains the lags, the season
  length, α, the correlation and outlier methods, and prewhitening.
- Pickers select the variable or series that a plot shows. Examples are one line for each
  market, the STL decomposition of a series, or the cross-correlation of a driver with the
  KPI. The plots render in the isolated plot sandbox, as all plots do.
- A section that is not calculated yet has a **Compute it now** button. This button starts
  an explore run for this section and for the other missing sections. The id of the run goes
  in the address (`?run=`). Thus you can share or reload the page. If you ask again with the
  same options, the platform answers immediately from the cached run.

![Stable or wandering: ADF and KPSS for each series with a plain verdict (Expert mode)](../assets/screenshots/explorer-data-stationarity.png){ .mapi-shot }

![Trend and season: the STL decomposition of the selected series, and the strengths of each series](../assets/screenshots/explorer-data-stl.png){ .mapi-shot }

## From Python and the command line

```python
--8<-- "client_v05_more.py:data-explorer"
```

```bash
mapi explore dataset dataset://growth/weekly_sales@latest \
  -s overview -s stationarity -s ccf -s stl --kpi sales --out views/
mapi explore dataset weekly_sales --show quality      # one view as JSON
```

## Views written at ingest

Each ingest writes the quick sections next to the dataset: `overview`, `distributions`,
`timeseries`, `missingness`, `outliers`, `correlation` and `quality`. Get the index, and then
any view, through the artifact route:

```bash
api "$API/v1/datasets/$DATASET_ID/versions/1/artifacts/ui/explore/index.json"
api "$API/v1/datasets/$DATASET_ID/versions/1/artifacts/ui/explore/quality.json"
```

Each answer is a signed link (`ArtifactLink`) to the JSON view. The index lists each section
with its `status`:

- `ok`.
- `skipped`, with the reason in plain words. An example is "the data has no time
  dimension".
- `error`.

The plots for a section are at `ui/explore/plots/<section>.json`. This file is a list of
PlotSpecs to render in the plot sandbox.

## Everything else, on demand

The heavier sections run as an **explore run**. These sections are `stl`, `stationarity`,
`ccf`, `acf_pacf`, `vif` and `groups`. All sections with other options also run as an
explore run:

```bash
api -X POST "$API/v1/datasets/$DATASET_ID/versions/1/explore" -d '{
  "sections": ["stationarity", "stl", "ccf", "vif"],
  "kpi": "revenue",
  "facet_by": ["geo"],
  "max_lag": 12,
  "prewhiten": true
}'
```

The answer (`202`) is an `Exploration` with the run. Poll `GET /v1/explorations/{run_id}`.
When the state is `done`, it holds the `index` and a signed link for each view. If you ask
again with the same request, the platform answers `200` from the last run. Thus, after the
platform calculates the explorers one time, they are instant.

| Option | Default | Meaning |
|---|---|---|
| `sections` | all 13 | The sections to calculate |
| `vars` | each numeric variable | The variables to include |
| `kpi` | the target variable | The result that the cross-correlations use |
| `time_dim` | the time dim of the schema | The dimension that "over time" means |
| `facet_by` | none (each series) | A maximum of two dims that split the series. For spend, media and the target, the platform adds the other dims. For all other variables, it calculates the average. |
| `group_by` | the first categorical dim | The dim that `groups` compares |
| `max_lag` | 26 | The lags for ACF/PACF and cross-correlations |
| `period` | from the frequency (52 weekly, 12 monthly, 7 daily) | The season length for STL |
| `alpha` | 0.05 | The significance level of the tests |
| `correlation` | `pearson` | Or `spearman` |
| `outliers`, `outlier_threshold` | `mad`, 3.5 | The robust score (`mad`, `iqr` or `zscore`, all on a standard-normal scale) and its threshold |
| `prewhiten` | true | Filter the two series with the own AR model of the driver before the cross-correlation. To see the raw correlations, turn it off. |
| `max_series` | 50 | The number of series in each view. The platform counts the other series as omitted. |

## The sections

| Section | What you see | Read it like this |
|---|---|---|
| `overview` | Dims, variables, roles, the fraction of missing values, a summary and a quality score | "104 weeks for 3 markets; 2 data problems to look at." |
| `distributions` | For each variable: histogram, mean, spread, skew, quantiles and fraction of zeros | Heavy tails or many zeros can need a transform or a different likelihood. |
| `timeseries` | Each variable over time, in facets | It is easier to see breaks, level shifts and gaps than to test for them. |
| `missingness` | A heatmap of the fraction of missing values by variable and period, and the longest gap | You cannot impute long gaps reliably. |
| `outliers` | Values that are far from the typical value, with their coordinates | Are they errors or real events? For real events, add a control variable. |
| `correlation` | The correlation matrix with p-values | It is difficult to separate drivers that move almost together. |
| `ccf` | The correlation of each driver with the KPI at each lag. A positive lag means that the driver leads. | A peak at lag 2 means that the effect shows two periods later. Prewhitening is on by default. The significance band uses the formula of Bartlett for two autocorrelated series. Thus the platform does not flag two smooth but unrelated series at half of their lags. |
| `acf_pacf` | Autocorrelation and partial autocorrelation with the significance band, and suggested AR/MA orders | Bars outside the dashed band are more than chance. |
| `stationarity` | ADF and KPSS for each series, with one verdict | `stable`, `difference` (it wanders: model the changes), `trend_stable` or `unclear` (the tests do not agree: look at the plot). |
| `stl` | Trend, season and remainder, with their strengths (0 to 1) | If the seasonal strength is more than about 0.6, model the season explicitly. |
| `vif` | The variance inflation for each driver, and the condition number | Above 10, the model cannot identify the separate effect of the driver well. |
| `groups` | Each variable across markets or stores, with an ANOVA or Kruskal-Wallis test | Very different groups can need a hierarchical model. |
| `quality` | Warnings in plain language, the most serious first. Each warning has a suggested action. | Correct errors before you model. Look at the warnings. |

The `data.quality` governance check also counts the quality warnings (see
[Validate models with governance policies](governance.md)).

## Dataset About

`GET /v1/datasets/{dataset_id}/about` describes the latest version in plain words. It uses
the schema and the coordinates of the version ("Weekly data from 2023-01-01 to 2024-12-29 (104
weeks) for 10 markets …"). The response also shows the own description of the owner. `PATCH`
changes that description:

```bash
api -X PATCH "$API/v1/datasets/$DATASET_ID/about" \
  -d '{"description_user": "Weekly media spend and revenue from the finance warehouse."}'
```

In the web app, the dataset page and the Data Explorer show the two descriptions next to each
other (*About this dataset*). Editors change the description on the page with **Edit**.

## Notes

- Views have limits. The maximums are 200 series in each view, 5,000 points in each series,
  256 variables and 200 lags. The platform thins longer series evenly and flags them
  `downsampled`.
- The statistical tests come from statsmodels and scipy. The test suite validates them
  against their reference outputs.
- Strings in views come from your data. Render them only as text.
