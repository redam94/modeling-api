# E07 induced earthquakes in Oklahoma

This tutorial does the work of the pymc-challenges notebook
`E07_spatiotemporal_earthquakes.py` in the web app. You use the model `sci.lgcp_grid@1`. You
do not write PyMC code.

**You need:**

- The platform (start it with `make dev`) and a project that you can upload datasets to. This
  tutorial uses the project `geoscience`. If your project has a different name, change the
  project part of each `dataset://` and `model://` reference, and `metadata.project` in the
  spec.
- A local copy of pymc-challenges. The export command reads one file from its `data/`
  directory.
- About 15 minutes. The fit takes about four minutes of sandbox time.

## 1. The question

For 2005 to 2008, the USGS catalogue lists one to three earthquakes of magnitude 3 or more each
year in and around Oklahoma. For 2015, it lists more than nine hundred. Most scientists relate
the increase to the deep disposal of wastewater from oil and gas production. The rate went down
after the regulators limited the injection volumes in 2015 and 2016. The active area did not
stay in one location: it moved.

The notebook asks three questions of the catalogue:

1. **Where and when** was the earthquake rate high? (Notebook section 8.)
2. **How did the area of high activity move?** (Notebook section 8, the centre of activity.)
3. **What does the model forecast for 2018 and 2019**, and how much of that forecast can you
   believe? (Notebook section 10.)

The notebook ends with this decision. Use the model to map where and when the rate was high,
with an honest range, and for short "more of the same" forecasts of *where* the next events
occur. Do not use its forecast of the *total* to plan, because the range is very wide and a
random walk cannot continue a decrease. Do not use it to decide what occurs if the injection
policy changes: the model has no column for injection.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The expected events for each cell and quarter, with mean, sd and draws (**Fields**, `sci.field_map`) | Section 8, the maps of the posterior mean and of the uncertainty |
| The expected total for each quarter with a 90% band (`sci.ensemble`) | Section 8, the temporal check of the totals |
| The forecast of the 2018 and 2019 totals against the actual values (`core.ppc_fan`, the hold-out metrics) | Section 10, the table of hold-out scores and the forecast plot |
| The posterior of `alpha`, `sigma_g`, `ell`, `eta0`, `eta_d` and `inv_phi` (**Parameters**, `core.posterior_forest`) | Sections 7 and 8, the hyperparameters of rung 4 |
| A forecast of the next 8 quarters after the data (**Score**) | Section 10, the forecast that the model makes |

The model is rung 4 of the notebook's ladder: a map that changes each year, with negative
binomial counts. See [Differences from the notebook](#10-differences-from-the-notebook) for the
other rungs.

## 2. Get the data

The export command reads `oklahoma_quakes.csv`. It projects each event to km and keeps the
events in a window of 300 km × 225 km. Then it counts them in cells of 25 km for each quarter
from 2009 Q1 to 2019 Q4. This is the same grid as the notebook. Run the command from the root of the
modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e07 \
    --challenges <path to pymc-challenges>/data \
    --out oklahoma_quakes_grid.nc --spec oklahoma_quakes_grid.yaml
```

The command writes:

```text
wrote oklahoma_quakes_grid.nc (variables quakes on quarter=44, row=9, col=12)
upload it as dataset geoscience/oklahoma_quakes_grid and fit sci.lgcp_grid
wrote the suggested spec to oklahoma_quakes_grid.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `quarter` | 44 | time, quarterly (`QS`), policy `extend` | 2009 Q1 to 2019 Q4 |
| `row` | 9 | integer, policy `fixed` | The cell row. Row 0 is the south edge |
| `col` | 12 | integer, policy `fixed` | The cell column. Column 0 is the west edge |
| `x_km`, `y_km` | `col`, `row` | float64 coordinates | The cell centres in km east and north of 97.8° W, 36.1° N |
| `quakes` | `quarter, row, col` | int64, events, target | The number of events of magnitude 3 or more in the cell and quarter |

The grid holds 2,852 events. The events for each year are 16, 33, 62, 32, 94, 586, 912, 638,
271, 153 and 55 (2009 to 2019). The largest count in one cell and quarter is 86. The attribute
`mapi_schema` gives each variable its role, so you do not map columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes counts that the model's own process simulates: a grid of 6 × 5 cells
    for 16 quarters, with known parameters. Use it to try the steps. The numbers in this
    tutorial are for the real catalogue.

    ```bash
    uv run python -m mapi_plugin_science.export e07 --synthetic --out quake_grid.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `oklahoma_quakes_grid` in **Name**. Type a description, for example "Earthquakes of
   magnitude 3 or more in 25 km cells for each quarter, 2009 to 2019".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `oklahoma_quakes_grid.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Optional: click **Preview structure**. The panel shows **Structure of the file**. Make sure
   that it shows `quakes` on `quarter`, `row` and `col`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `quakes` has the role
`target`, and `quarter` is a time dim with the policy `extend`. **Missingness** must show no
missing values: the model needs a count for each cell and quarter. A cell without events has
the count 0, not a missing value.

The file is one plain dataset. You do not need the tree fields ("Put this file in a node of a
tree (optional)") or the HDF5 path.

### 3.1 Explore the data

Look at the data before you build the spec.

1. On the version page, click **Explore this version**. The Data Explorer opens. The
   sections under *Pictures of the data* come first.
2. Click **Images and fields**.

- The viewer shows `quakes` as a map of 9 × 12 cells. Move the frame slider to go from
  quarter to quarter. The viewer shows 24 of the 44 quarters. The line below the map shows
  the mean, the minimum and the maximum count of all 44 quarters.
- **Over time** shows one series: the number of earthquakes in all cells for each quarter.
  The explorer does not make a series for each cell.
- The time patterns (memory, stable or wandering, trend and season) use this series.
- **Group comparison** is in **Not for this data**. The rows and the columns of the grid are
  not groups.

Refer to [Scientific data](../../guides/data-explorer.md#scientific-data).

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `oklahoma_quakes_grid`. The toast "Dataset set" shows.
5. Type `lgcp` in the search field. Under **Models**, select **Event counts on a map over
   time** (`sci.lgcp_grid@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `oklahoma-quakes-lgcp` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: oklahoma-quakes-lgcp, project: geoscience}
data:
  dataset: dataset://geoscience/oklahoma_quakes_grid@latest
  bind:
    dims: {period: quarter}
    vars: {count: quakes}
  split: {kind: time_holdout, dim: period, last: 8}
model:
  use: sci.lgcp_grid@^1
  settings: {max_extension: 8, era_periods: 4, m_row: 10, m_col: 14, cell_km: 25.0}
  fit: {mode: evaluate_then_refit, seed: 2016, tune: 1000, draws: 1000, chains: 4,
        engine: nutpie}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs:
  plots: [core.convergence, core.posterior_forest, core.ppc_fan, sci.field_map,
          sci.ensemble]
```

The model calls the time dim `period` and the target `count`. The `bind` block connects them to
`quarter` and `quakes` in the dataset. The split keeps the last 8 quarters (2018 and 2019) out
of the first fit, as the notebook does. `evaluate_then_refit` fits on 2009 to 2017, scores the
held-out quarters, and then fits again on all 44 quarters.

The settings for this question:

| Setting | Value | What it means here |
|---|---|---|
| `max_extension` | 8 | A forecast can go 8 quarters (two years) past the fit without a new fit. The model samples these future steps in the fit |
| `era_periods` | 4 | One era is 4 quarters, thus the map changes one time each year, as in the notebook |
| `field` | `era_walk` (default) | Each year gets a new map that is a random step from the map of the year before. This lets the area of high activity move. `static` gives one map for all years (rung 2 of the notebook) |
| `anchor` | `busiest` (default) | The year with the most events (2015) gets the full prior of the map. The other years are steps away from it |
| `likelihood` | `negbin` (default) | Negative binomial counts. Aftershocks make the counts vary more than a Poisson rate permits (rung 4 of the notebook) |
| `cell_km` | 25 | The width of a cell in km. The lengthscale prior is in km |
| `m_row`, `m_col` | 10, 14 | The basis functions of the 2-D HSGP along rows and columns. These are the values of the notebook |
| `c` | 1.5 (default) | The boundary factor of the HSGP |
| `ell_lower_km`, `ell_upper_km` | 15, 100 (default) | 90% of the lengthscale prior is between 15 km and 100 km |

The other settings (`alpha_sigma`, `sigma_g_scale`, `eta0_scale`, `eta_d_scale` and
`inv_phi_rate`) are the scales of the priors. The defaults are the priors of the notebook.

!!! note "The plots"
    The run draws only the plots in `outputs.plots`. The model suggests `sci.field_map` and
    `sci.ensemble`, but the suggested spec of the export does not include them. Add them as in
    the YAML above.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the progress of the sampler. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://geoscience/oklahoma-quakes-lgcp@v1**. Click it.

The run does two fits: one on 2009 to 2017 for the hold-out metrics, and one on all quarters.
On a laptop with nutpie, the two fits took 213 seconds. After the fits, the run calculates the
plots and the explorer views.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer, or the plot **Sampler convergence** on the
**Plots** tab. In the fit for this tutorial, there were no divergences. The largest R-hat was
1.02 and the smallest bulk ESS was 151 (of 4,000 draws). The scorecard can show a warning for
these values. The values that mix slowly are values of the map in years with few events. If
you need a stricter result, increase `tune` and `draws` to 2000.

**Posterior forest** shows the parameters. In this fit:

| Parameter | Mean | 90% range | What it tells you |
|---|---|---|---|
| `ell` | 35 km | 29 to 41 km | The size of a cluster. The map changes over a distance of about one or two cells |
| `sigma_g` | 0.46 | 0.33 to 0.60 | The step of the log total from one quarter to the next |
| `eta0` | 3.5 | 2.7 to 4.3 | The amplitude of the log map in 2015 |
| `eta_d` | 1.3 | 1.1 to 1.5 | The amplitude of the change of the map from one year to the next |
| `inv_phi` | 0.84 | 0.70 to 0.99 | The overdispersion. 0 is a Poisson rate. A value near 1 shows many bursts of events |

### 6.2 Where and when: the maps

Open **Fields** in the Model Explorer. The model has 1 output on a grid: `rate`, the expected
events for each cell and quarter.

1. Select **Mean** in **Statistic**. Move the slider of the quarters. The colour scale is
   logarithmic.
2. Select **Std. dev.** to see where the model is not sure.
3. Select **Draws** and click **Play the draws** to see possible maps that agree with the data.
4. Move the pointer over a cell to read its values.

The view shows at most 24 of the 44 quarters. The plot **Field map** on the **Plots** tab shows
the same output as panels (mean and sd) with a slider.

Look for these features, which the notebook also finds:

- A small area east of Oklahoma City in 2009 and 2010.
- The Prague sequence in 2011 and 2012.
- In 2013, a new centre north-east of Oklahoma City and a separate area near the Kansas
  border.
- In 2014 and 2015, these areas grow into one band from Oklahoma City to the border.
- From 2016, the full map becomes dark.

The uncertainty map is the opposite of the mean map. Where there are events, the sd is small.
In empty areas, the data say only "low", and the prior decides *how* low. Do not read the dark
cells of the mean map as an exact zero.

### 6.3 How did the active area move?

The web app does not show the centre of activity as a plot. You can calculate it from the
draws of `rate`: for each year, the sum of the cell centres (`x_km`, `y_km`) weighted by the
share of each cell. For this fit, the centre moved from 75 km east and 65 km south of the
reference point in 2011 to 11 km east and 32 km north in 2015. That is 64 km to the west and
97 km to the north: 116 km in total (90% range 105 to 127 km). In 2017, it was at 2 km west and
27 km north, thus it moved little while the rate went down.

### 6.4 The totals and the forecast of 2018 and 2019

**Ensemble of draws** shows the expected total of the grid for each quarter: draws, the 90%
band and the median. The rise to 2015 and the decrease after it are clear, and the band is
narrow where there are many events.

**Fit and forecast** shows the hold-out part: the model that saw only 2009 to 2017 forecasts
the sum over all cells for the 8 quarters of 2018 and 2019. The dots are the actual totals.
The hold-out metrics are in the **Metrics** card of the **Overview** tab:

| Metric | Value | Meaning |
|---|---|---|
| `coverage_90` | 0.99 | 99% of the 864 held-out cell-quarters are inside their 90% range |
| `mae` | 1.13 | The mean absolute error of the count of one cell and quarter |
| `rmse` | 2.29 | The root mean square error of the same counts |

Read these numbers as the notebook does. Most cells have no events, and a range that starts at
0 contains them with no difficulty. Thus a coverage of 99% for a 90% range does not show that
the model is good.

### 6.5 The numbers of the notebook and of this port

| Quantity | Notebook | This port |
|---|---|---|
| Events in 2018 and 2019 (held out) | 208 | 208 |
| Movement of the centre, 2011 to 2015 | About 65 km west and 100 km north | 64 km west and 97 km north, 116 km (105 to 127) |
| Quarterly totals of 2018 and 2019 inside the 90% band | 7 of 8 (2019 Q2 is just below) | 8 of 8 (2019 Q2, 7 events, is at the lower bound) |
| Median forecast of the 2-year total | More than two times the 208 actual events | 613 (90% range 154 to 3,286) |
| Held-out cell-quarters inside the 90% range | 99% | 99% |

Your numbers can be slightly different if the sampler or its version is different.

### 6.6 Make the decision

Use these outputs to make the decision of the notebook:

1. **Where and when.** Use **Fields** (mean and sd). The maps show where the rate was high, and
   the sd map shows which cells the data do not fix. This part of the model is reliable.
2. **Where next.** The forecast uses the latest map, with one or two years of random steps.
   Most held-out events fall on the bright cells of the map. Thus the map is a good short-range
   guide to *where* events can occur. Some held-out events in the south-west fall on dark cells:
   a random walk cannot forecast a new active area.
3. **How many.** Do not plan with the forecast total. The band of the total increases to more
   than two orders of magnitude by the end of 2019. The median stays flat at the level of
   2017, but the actual total went down to less than half of it. A random walk has no trend.
4. **What if the policy changes.** The model cannot answer this. It has no injection volumes.
   For that question, add the injection volumes as a cause (see the next steps).

## 7. Score the model

The fitted version uses all 44 quarters. Thus a forecast starts in 2020 Q1.

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Forecast**.
3. In **How far ahead?**, type `8` in **Periods**. The maximum is 8 (`max_extension`).
4. In **Values to score**, select **No new data**. The model forecasts from the end of the fit.
5. Open **Advanced**. Set **Quantity** to **Predictive** to get counts with the noise of the
   negative binomial. Type `200` in **Draws**.
6. Click **Forecast**.

The request that the page sends is:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://geoscience/oklahoma-quakes-lgcp@latest",
  "mode": "forecast",
  "horizon": {"dim": "period", "periods": 8},
  "output": {"kind": "predictive", "summary": ["mean", "q5", "q50", "q95"], "draws": 200}
}
```

The result has `rate(quarter, row, col)` and `total(quarter)` for 2020 Q1 to 2021 Q4. In this
fit, the median of the sum of the cells was 14 events for each quarter. The 90% range went from
4 to 39 events in 2020 Q1 to 1 to 141 events in 2021 Q4. The range increases with each quarter
because the random walk adds uncertainty.

A horizon of 9 or more quarters stops the score with `HORIZON_EXCEEDS_FIT`. To forecast
further, fit again with a larger `max_extension`. The model also accepts `predict` (the fitted
quarters) and `evaluate` (new actual counts with metrics).

If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same request.

## 8. Run a workflow recipe

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does the checks of a Bayesian
workflow on any model with draws. It fits the model with prior predictive draws and plots
them. A convergence gate needs the largest R-hat below `rhat_max` (1.01) and the smallest bulk
ESS at or above `min_ess` (400). Only then does the recipe make the posterior predictive check
and run the governance checks `sensitivity.prior` and `recovery.simulated`. Then it writes a
report with the plots.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Bayesian workflow audit** and click **Run on my data**.
3. In **1. Your data**, select `oklahoma_quakes_grid` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.lgcp_grid@^1` in **Model**.
5. Open **Fine-tune**. In **Settings**, type the `settings` of step 4 as JSON:
   `{"max_extension": 8, "era_periods": 4, "m_row": 10, "m_col": 14, "cell_km": 25.0}`.
   In **Bind**, type `{"dims": {"period": "quarter"}, "vars": {"count": "quakes"}}`.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Decisions** shows the convergence gate and the value that it read. **Outputs**
shows the plots and the report. The model version link opens the Model Explorer.

!!! caution "The default gate stops this audit"
    In the fit of this tutorial, the largest R-hat was 1.024 and the smallest bulk ESS was 151.
    Thus the default gate (R-hat below 1.01, bulk ESS 400 or more) takes the else arm, and the
    recipe skips the checks after the gate. To get the checks, increase **Tune** and **Draws**
    in **Fine-tune**. Or set **Rhat max** to `1.05` and **Min ess** to `100`, if you accept
    the risk of a less strict gate.

!!! note
    The checks after the gate refit the model. `sensitivity.prior` fits it one time for each
    value in **Prior scales** (two by default), and `recovery.simulated` fits it **N sims**
    times (20 by default). Thus the audit takes much longer than one fit. For
    `sci.lgcp_grid`, `recovery.simulated` simulates the counts from the posterior predictive
    of the model. Thus each simulated dataset has whole counts.

The recipe `sci_spatial_ladder` compares models of areas (counties, districts), and
`sci_image_model_check` is for models of images. They do not apply to this model.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/geoscience
    mapi datasets upload oklahoma_quakes_grid.nc --name oklahoma_quakes_grid
    mapi check oklahoma-quakes-lgcp.yaml --remote @     # the server's static validation
    mapi fit oklahoma-quakes-lgcp.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model oklahoma-quakes-lgcp -s params -s diagnostics

    cat > quakes-forecast.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://geoscience/oklahoma-quakes-lgcp@latest
    mode: forecast
    horizon: {dim: period, periods: 8}
    output: {kind: predictive, summary: [mean, q5, q50, q95], draws: 200}
    EOF
    mapi score quakes-forecast.yaml --remote @ --model <model id> --version 1 -o forecast.zarr.zip

    mapi workflows init bayes_workflow_audit --name quakes-audit
    mapi workflows apply quakes-audit.yaml -m "E07 audit"
    mapi workflows run quakes-audit -i data=dataset://geoscience/oklahoma_quakes_grid@latest \
      -p model=sci.lgcp_grid@^1 \
      -p 'settings={"max_extension": 8, "era_periods": 4, "m_row": 10, "m_col": 14, "cell_km": 25.0}' \
      -p 'bind={"dims": {"period": "quarter"}, "vars": {"count": "quakes"}}' --watch
    ```

    `oklahoma-quakes-lgcp.yaml` is the spec of step 4. The CLI has no `models` command. Take
    the model id from the run (`mapi runs get <run>`) or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/geoscience")
    version = api.datasets.upload("oklahoma_quakes_grid.nc", dataset="oklahoma_quakes_grid")
    print(version.ref, version.status.value)

    with open("oklahoma-quakes-lgcp.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "oklahoma-quakes-lgcp")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://geoscience/oklahoma-quakes-lgcp@latest",
            "mode": "forecast",
            "horizon": {"dim": "period", "periods": 8},
            "output": {"kind": "predictive", "summary": ["mean", "q5", "q50", "q95"]},
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["total"].sel(stat="q50").values)
    ```

    To run the audit recipe:

    ```python
    workflow = api.workflows.create(template="bayes_workflow_audit")
    run = api.workflows.run(
        workflow.id,
        inputs={"data": "dataset://geoscience/oklahoma_quakes_grid@latest"},
        params={
            "model": "sci.lgcp_grid@^1",
            "settings": {"max_extension": 8, "era_periods": 4, "m_row": 10,
                         "m_col": 14, "cell_km": 25.0},
            "bind": {"dims": {"period": "quarter"}, "vars": {"count": "quakes"}},
        },
        wait=True,
    )
    ```

## 10. Differences from the notebook

The port keeps the binned grid of notebook section 2 and the model of rung 4. It leaves
out these parts:

- **Rung 1 (time only).** The model always has a map. The other rungs are settings of the same
  model: `field: static` with `likelihood: poisson` is rung 2, and `likelihood: poisson` alone
  is rung 3.
- **The prior predictive check of section 4.** The model has the "how many" and "where"
  parametrisation from the start, so the priors do not explode.
- **The LOO comparison of section 9.** The spec of this tutorial does not ask for it. Add
  `elpd_loo` to `evaluate.metrics` in a spec for each rung. Many Pareto k values are high for
  this model, as in the notebook, so use the order of the models and not the exact numbers.
- **The centre of activity (section 8).** The web app has no plot for it. Calculate it from
  the draws of `rate` (step 6.3).
- **The yearly maps with the epicentres.** **Fields** shows quarters, not years, and it does not
  show the epicentres. The notebook shows the sum of each year.
- **The magnitudes.** The dataset keeps only the counts. The notebook does not use them in the
  model either.

**Next steps:**

- Fit the rungs of the ladder with `field` and `likelihood`, and compare them with `elpd_loo`
  and the hold-out metrics.
- Add the magnitudes as marks, or a term for aftershocks (notebook "Try it yourself", items 1
  and 2). These need a new model.
- Add injection volumes as a cause. This is the only way to answer the policy question. The
  model does not accept columns for causes.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the audit recipe.
