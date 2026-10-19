# E81 wind directions and animal activity (circular)

This tutorial does the work of the pymc-challenges notebook `E81_circular_data.py` in the web
app. You use the model `sci.circular_regression@1`. You do not write PyMC code.

**You need:**

- The platform (start it with `make dev`) and two projects that you can upload datasets to.
  This tutorial uses the projects `meteorology` (the wind) and `ecology` (the camera traps). If
  your projects have different names, change the project part of each `dataset://` and
  `model://` reference, and `metadata.project` in the specs.
- A local copy of pymc-challenges. The export commands read one file each from its `data/`
  directory.
- About 20 minutes. The two fits of the main path take about one minute of sandbox time
  together.

## 1. The question

Some measurements are angles. A wind blows *from* a direction between 0 and 360 degrees, and
359 degrees is next to 1 degree. An animal walks past a camera at a time of day, and 23:59 is
next to 00:01. An arithmetic mean or a normal likelihood on these values can give a wrong
answer with high confidence. The notebook uses two real datasets:

- **Wind at Santa Barbara airport** (ASOS station KSBA, 2021 to 2023). At night, cool air
  flows down from the mountains to the sea. In the afternoon, the sea breeze blows inland. Thus
  the direction turns through half of the compass each day, and the turn changes with the
  season.
- **Camera traps on Barro Colorado Island**, Panama (Rowcliffe et al. 2014). The ocelot hunts at
  night. The data give the time of day of 16,282 photographs of the ocelot and five animals that
  it can hunt.

The notebook asks these questions:

1. **How does the wind direction change with the hour and the season**, and how sure are you?
   (Notebook parts B and D.)
2. **Which model of the direction predicts better** on a year that it did not see? The models
   are one von Mises direction for each hour and season, a projected normal and a mixture of
   two wind regimes. (Part D, LOO and the held-out year 2023.)
3. **When is the ocelot active, and how much does its day overlap with the day of each prey
   animal?** (Part E, the log-Fourier density, the overlap and the activity level.)

The notebook ends with these decisions:

- Use the von Mises in its natural parameters (the canonical link). It has one mode, and it
  has no seam at ±180 degrees. Do not use a uniform prior on an angle.
- For the wind, a mixture of two regimes predicts much better than one direction for each
  hour. The single von Mises puts mass between the two lobes of the evening rose, where the
  wind seldom blows.
- Report the overlap of two activity curves with its posterior range. The ocelot overlaps
  least with the agouti (0.30) and most with the brocket, the spiny rat and the paca (0.66 to
  0.75).

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The rose of the observed directions with the range of the model in each sector (`sci.rose`) | Part D, the circular posterior predictive check |
| The density of the direction in each block of three hours, with mean, sd and draws (**Fields**, `sci.field_map`) | Part B, the rose diagrams by hour, and part D, the model roses |
| The mean direction and the mean resultant length of each block of three hours (**Score**) | Part D, the clock of wind arrows |
| `elpd_loo` of the 12,023 reports of 2021 and 2022 (**Metrics**) | Part D, "Which model predicts better?" |
| The circular holdout metrics of 2023 (**Score**, mode Evaluate) | Part D, the held-out year |
| The activity density of each species (**Fields**, `sci.profile_bands`) | Part E, the log-Fourier density |
| The overlap of each pair of species, and the activity level of each species (**Fields** and **Score**) | Part E, the overlap and the activity level |

See [Differences from the notebook](#10-differences-from-the-notebook) for the parts that the
port leaves out.

## 2. Get the data

The two datasets have different cases in the export command: `e81` for the wind and `e81b` for
the camera traps. Run the commands from the root of the modeling-api repository.

### 2.1 The wind

The export reads `asos_ksba_2021_2023.csv.gz`. It removes the calm reports, because a calm
wind has no direction. It uses Pacific standard time (UTC − 8) all year, so that the sea breeze
follows the sun and not the clock.

```bash
uv run python -m mapi_plugin_science.export e81 \
    --challenges <path to pymc-challenges>/data \
    --out ksba_wind_2021_2023.nc --spec ksba_wind_2021_2023.yaml
```

The command writes:

```text
wrote ksba_wind_2021_2023.nc (variables wind_from, hour_lst, day_of_year, year on report=18049)
upload it as dataset meteorology/ksba_wind_2021_2023 and fit sci.circular_regression
wrote the suggested spec to ksba_wind_2021_2023.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `report` | 18,049 | integer, policy `extend` | The reports in time order. The 6,026 reports of 2023 are the last ones |
| `wind_from` | `report` | float64, target, `deg` | The direction that the wind blows from, clockwise from true north. ASOS gives it in steps of 10 degrees |
| `hour_lst` | `report` | float64, covariate, `h` | Hours since midnight, local standard time |
| `day_of_year` | `report` | float64, covariate, `d` | Days since 1 January, with the fraction of the day |
| `year` | `report` | int64, constant | The year of the report: 6,096 reports in 2021, 5,919 in 2022 and 6,026 in 2023 |

### 2.2 The camera traps

The export reads `bci_camera_trap_times.csv`. It keeps the ocelot and the five prey species of
the notebook, and it changes the time of each photograph to hours since midnight.

```bash
uv run python -m mapi_plugin_science.export e81b \
    --challenges <path to pymc-challenges>/data \
    --out bci_camera_traps.nc --spec bci_camera_traps.yaml
```

The command writes:

```text
wrote bci_camera_traps.nc (variables clock_time, species_idx on record=16282, species=6)
upload it as dataset ecology/bci_camera_traps and fit sci.circular_regression
wrote the suggested spec to bci_camera_traps.yaml
```

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `record` | 16,282 | integer, policy `extend` | One photograph |
| `species` | 6 | categorical, policy `fixed` | `ocelot`, `agouti`, `paca`, `rat`, `brocket` and `peccary` |
| `clock_time` | `record` | float64, target, `h` | The time of day of the photograph, 0 to 24 hours |
| `species_idx` | `record` | int64, index of `species` | The species of the photograph |

The attribute `mapi_schema` of each file gives each variable its role, so you do not map
columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes data that the model itself simulates, with known parameters. The wind
    file has 480 reports of one year (2021). The camera-trap file has 700 records of three
    groups (`owl`, `lark` and `drifter`). Use them to try the steps. Remove the `split` line
    from the wind spec of step 4, because the synthetic file has no year 2023. The numbers in
    this tutorial are for the real data.

    ```bash
    uv run python -m mapi_plugin_science.export e81 --synthetic --out wind.nc
    uv run python -m mapi_plugin_science.export e81b --synthetic --out activity.nc
    ```

## 3. Upload the datasets

Do these steps in the project `meteorology` for the wind file. Then do them again in the
project `ecology` for the camera-trap file.

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `ksba_wind_2021_2023` in **Name**. Type a description, for example "Hourly wind
   directions at Santa Barbara airport, 2021 to 2023, without calms".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `ksba_wind_2021_2023.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Optional: click **Preview structure**. The panel shows **Structure of the file**. Make sure
   that it shows four variables on `report`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

For the camera traps, use the name `bci_camera_traps` and the file `bci_camera_traps.nc`.
**Structure of the file** shows `clock_time` and `species_idx` on `record`, and the coordinate
`species`.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `wind_from` has the role
`target` and the units `deg`. `clock_time` has the role `target` and the units `h`.

Each file is one plain dataset. You do not need the tree fields ("Put this file in a node of a
tree (optional)") or the HDF5 path.

## 4. Build the specs

### 4.1 The wind

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `ksba_wind_2021_2023`. The toast "Dataset set" shows.
5. Type `circular` in the search field. Under **Models**, select **Directions and times of
   day** (`sci.circular_regression@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `ksba-wind-direction` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: ksba-wind-direction, project: meteorology}
data:
  dataset: dataset://meteorology/ksba_wind_2021_2023@latest
  bind:
    dims: {obs: report}
    vars: {direction: wind_from, hour: hour_lst, day: day_of_year}
  split: {kind: time_holdout, dim: obs, last: 6026}
model:
  use: sci.circular_regression@^1
  settings: {kind: von_mises, units: degrees, daily_harmonics: 3, annual_harmonics: 2,
             interaction_harmonics: 2}
  fit: {mode: train_only, seed: 81, tune: 1000, draws: 1000, chains: 4, engine: nutpie,
        keep: {log_likelihood: true, posterior_predictive: {thin: 20}}}
evaluate: {metrics: [elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.rose, sci.profile_bands, sci.field_map]
```

The model calls its row dim `obs`, the target `direction`, and the covariates `hour` and `day`.
The `bind` block connects them to the names in the dataset.

The split and the fit mode do the same as the notebook:

- `split: {kind: time_holdout, dim: obs, last: 6026}` keeps the last 6,026 reports (all of
  2023) out of the fit.
- `fit.mode: train_only` fits on the 12,023 reports of 2021 and 2022, and it does not fit again
  on all reports. Thus the model version is the model of the notebook, and you can score 2023
  in step 7.
- `keep.log_likelihood: true` keeps the log density of each report, for `elpd_loo`.
- `keep.posterior_predictive: {thin: 20}` keeps simulated directions from 200 draws, for the
  rose of `sci.rose`.

The suggested spec of the export (`ksba_wind_2021_2023.yaml`) is the same spec without
`sci.profile_bands` in the plots.

The settings for this question:

| Setting | Value | What it means here |
|---|---|---|
| `kind` | `von_mises` | One von Mises direction for each report. The hour and the day of the year move its natural parameters `η = F B` (the canonical link). The mean direction is `atan2(η₂, η₁)` and the concentration is `|η|`. `regime_mixture` is the mixture of two regimes (section 6.5) |
| `units` | `degrees` | The direction is in degrees. A turn is 360. Use `hours` for a time of day and `radians` for radians |
| `daily_harmonics` | 3 | The cosine and the sine of 1, 2 and 3 cycles each day. Midnight is next to 00:01 |
| `annual_harmonics` | 2 | The cosine and the sine of 1 and 2 cycles each year. 31 December is next to 1 January |
| `interaction_harmonics` | 2 | The first two daily harmonics times the first annual harmonic. The daily turn of the wind can change with the season. With 3, 2 and 2, the model has 19 features, the same as the notebook |
| `intercept_sd`, `coef_sd` | 2, 1 (default) | The prior sds of the intercepts and of the other coefficients, as in the notebook |
| `frames` | 8 (default) | The `density` output has one row for each block of 24 / 8 = 3 hours, as the rose diagrams of the notebook |
| `angle_points` | 72 (default) | The `density` output has 72 angles, one each 5 degrees |
| `rows_per_frame` | 200 (default) | The density of a block is the mean of the densities of up to 200 fit reports in that block |
| `score_draws` | 400 (default) | The number of posterior draws that one score uses |

!!! note "Settings that do not agree"
    **Validate** does a check of each setting by itself. It does not compare two settings.
    For example, `interaction_harmonics: 4` with `daily_harmonics: 3` shows `valid`, but the
    run stops with `SETTINGS_INVALID`.

### 4.2 The camera traps

Make a second spec in the project `ecology`. Do the steps of section 4.1 with the dataset
`bci_camera_traps` and the name `bci-ocelot-activity`:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: bci-ocelot-activity, project: ecology}
data:
  dataset: dataset://ecology/bci_camera_traps@latest
  bind:
    dims: {obs: record}
    vars: {direction: clock_time, species_idx: species_idx}
model:
  use: sci.circular_regression@^1
  settings: {kind: activity, units: hours, group_dim: species, harmonics: 16, tau_sd: 2.0,
             grid_points: 256}
  fit: {mode: train_only, seed: 81, tune: 1000, draws: 1000, chains: 4, engine: nutpie,
        keep: {log_likelihood: true}}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, sci.field_map, sci.profile_bands]}
```

The settings for this question:

| Setting | Value | What it means here |
|---|---|---|
| `kind` | `activity` | One log-Fourier density for each species: `log f(θ) = Σ aₖ cos kθ + bₖ sin kθ − log Z`. It has no components, so no labels can change places |
| `units` | `hours` | The angle is a time of day. A turn is 24 hours |
| `group_dim` | `species` | The dim of the groups. The index variable is `species_idx` |
| `harmonics` | 16 | The number of Fourier terms K, as in the notebook |
| `tau_sd` | 2.0 | The prior is `aₖ, bₖ ~ N(0, (τ/k)²)` with `τ ~ HalfNormal(2)` for each species. Higher harmonics shrink more. This prior sets how smooth each curve is |
| `grid_points` | 256 | The number of angles in the sum that gives `Z` |
| `angle_points` | 72 (default) | The `density` output has 72 times of day, one each 20 minutes |

`fit.mode: train_only` fits on all records. The spec has no split. Without this line, the
validator shows the warning `SPLIT_INVALID` ("There is no holdout for the evaluation"). The
suggested spec of the export (`bci_camera_traps.yaml`) does not have this line, so add it.

`engine: nutpie` is the sampler of the notebook. The default engine of the model is `pymc`.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the progress of the sampler. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://meteorology/ksba-wind-direction@v1**. Click it.

On a laptop with nutpie, the wind fit took 45 seconds and the camera-trap fit took 21 seconds.
After the sampler, the run calculates the pointwise log densities, the simulated directions,
the `density` field, the plots and the explorer views.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.
Sections that do not apply to this model are under "Does not apply".

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer, or the plot **Sampler convergence** on the
**Plots** tab. In the wind fit for this tutorial, there were no divergences. The largest R-hat
was 1.003 and the smallest bulk ESS was 6,416 (of 4,000 draws). The caption says "All 614
parameters look converged". This count includes the cells of the `density` field.

In the camera-trap fit, there were no divergences. The largest R-hat was 1.010 and the
smallest bulk ESS was 578. Both values are for the standardised coefficients `za` and `zb`.

The canonical link gives one mode. Thus the four chains agree without special start values.
The notebook shows two problems of other models that give zero divergences: a wall at ±180
degrees from a uniform prior, and a second mode of the tan-half link. This model has neither
problem.

### 6.2 The coefficients

**Parameters** in the Model Explorer, and the plot **Posterior forest**, show the coefficients
`B(feature, component)`. There are 19 features (`1`, `cos1h`, `sin1h`, … `sin2h*sin1d`) and
two components, `eta1` and `eta2`. The components are the natural parameters of the von Mises.
`eta1` points to north and `eta2` points to east. A coefficient by itself is hard to read. Use
the mean directions of section 6.4 for the result.

### 6.3 Where does the wind come from? The fields

Open **Fields** in the Model Explorer. The model has 1 output on a grid: `density`, the
predictive density of the direction, per degree. The rows are the 8 blocks of three hours. The
label of a block is the hour at its start (0, 3, … 21). The columns are the 72 directions, from
0 to 355 degrees.

1. Select **Mean** in **Statistic**. A bright cell is a direction that the wind often comes
   from in that block.
2. Select **Std. dev.** to see where you are not sure of the density.
3. Select **Draws** and click **Play the draws** to see possible densities that agree with the
   data.
4. Move the pointer over a cell to read its values.

The plot **Field map of density** on the **Plots** tab shows the same output as panels (mean
and sd). Look for these features, which the notebook also finds:

- **Night (21:00 to 06:00).** A wide band from the north-east and the east: air that flows down
  from the mountains.
- **Afternoon (12:00 to 18:00).** A narrow, bright band from the south-west (about 225
  to 230 degrees): the sea breeze.
- **Evening (18:00 to 21:00).** A flat, dark row. The wind comes from two directions in this
  block, and one von Mises cannot show two directions.

### 6.4 The rose and the profiles

The plot **Rose of theta** (`sci.rose`) shows a rose of the observed directions of 2021 and
2022. The thick radial lines show the 90% range of the same count in data that the model
simulated. A bar that ends outside its range shows a direction that the model gets wrong.

!!! note "The default rose is in radians, and 0 is at the right"
    The rose reads the angles `theta` in radians from the bundle. In its default settings, 0 is
    at the right (east on a compass), and the angles go anticlockwise. Thus a wind from the
    west (270 degrees) is at the bottom, not at the left. For a compass (0 at the top,
    clockwise), use `sci.rose` with `clockwise: true` in a `plot.render` step of a workflow
    (step 8).

In the default rose (24 sectors of 15 degrees), only 4% of the bars end inside the range. Two
causes make this number low:

- ASOS gives the direction in steps of 10 degrees. Thus a sector of 15 degrees holds one or two
  of the reported values, and the observed bars go up and down. The simulated directions do
  not have steps.
- One von Mises direction for each hour and season cannot put mass on two lobes at the same
  time.

With `bins: 12` (sectors of 30 degrees, as in the notebook) and `clockwise: true`, 42% of the
bars end inside the range. This rose adds all hours together. Thus it does not show the
misfit of each block, and it does not separate the models of section 6.6 (the mixture gives
33%). For each block, use the **Fields** of section 6.3.

The plot **Profiles of theta** (`sci.profile_bands`) shows the simulated directions along
`report`, with the 50% and 90% bands, and the observed directions as dots. The direction is an
angle, so a band near 0 or 360 degrees is not a good summary. Use the rose and the fields.

### 6.5 The mean resultant vectors

The mean resultant vector of each block summarises the wind of that block. Its direction is
`mean_direction(frame)`, and its length is `resultant_length(frame)`: 1 if all winds of the
block come from one direction, near 0 if they come from all directions. Get these outputs with
a score (step 7.1). In the fit for this tutorial, the values are:

| Block (start hour) | `mean_direction` (degrees) | `resultant_length` | What it shows |
|---|---|---|---|
| 0 | 56 (52 to 60) | 0.39 | The night wind from the north-east |
| 3 | 37 (33 to 41) | 0.43 | The night wind from the north-east |
| 6 | 136 (132 to 139) | 0.39 | The morning wind from the south-east |
| 9 | 190 (189 to 192) | 0.65 | The turn to the sea breeze |
| 12 | 224 (223 to 225) | 0.70 | The sea breeze from the south-west |
| 15 | 230 (228 to 232) | 0.54 | The sea breeze from the south-west |
| 18 | 144 (133 to 154) | 0.13 | Two directions: the mean describes neither |
| 21 | 72 (69 to 75) | 0.41 | The night wind from the east-north-east |

The ranges are the 90% HDI of the posterior. A short vector (block 18) does not mean that the
air is still, because the data have no calms. It means that the direction is not predictable.
Always read a mean direction together with its length.

### 6.6 Which model predicts better?

The metrics are in the **Metrics** card of the **Overview** tab. `elpd_loo` scores the 12,023
reports of 2021 and 2022, in nats.

| Metric | Value | Meaning |
|---|---|---|
| `elpd_loo` | −17,385 (se 80, `p_loo` 38) | The expected log density of a left-out report, summed over the reports. Higher is better. All Pareto k values are below 0.7 |

To compare the models of the notebook, fit the spec again with each kind. Change only
`metadata.name` and `settings.kind`, for example `ksba-wind-regimes` and `kind:
regime_mixture`. Each fit makes a new model with its own `elpd_loo`. Compare the values only
between fits on the same reports. On a laptop with nutpie, the mixture fit took 202 seconds.

| `kind` | `elpd_loo` (this port) | Notebook |
|---|---|---|
| `von_mises` | −17,385 | −17,385 |
| `regime_mixture` | −15,821 (se 97, `p_loo` 57) | About 1,560 nats better than `von_mises` |

The mixture is better by 1,565 nats. **Fields** of the mixture shows why. In the block that
starts at 9, the density has two peaks, at about 150 and 225 degrees. In the block
that starts at 18, it has two peaks, at about 120 and 265 degrees. The von Mises
regression has one peak in each block, between the two lobes.

!!! note "The reports are not independent"
    The reports are hourly, and the wind of one hour is like the wind of the next hour. Thus the
    standard error of `elpd_loo` is too small. The held-out year (step 7.3) is a stricter test.

### 6.7 When is the ocelot active?

Open the model version `bci-ocelot-activity`, then **Explore**, then **Fields**. The model has
2 outputs on a grid. Select one in **Output**:

- `density`: the activity density of each species (rows) at each time of day (columns, one
  each 20 minutes), per hour.
- `overlap`: the coefficient of overlap of each pair of species (rows and columns). It is the
  area under the lower of the two densities: 1 for the same activity, 0 for animals that are
  never active at the same time.

The plot **Profiles of density** (`sci.profile_bands`) shows the density of each species along
the day with its 50% and 90% bands. Look for these features, which the notebook also finds:

- The **ocelot** is mostly active at night, with a peak in the evening.
- The **agouti** is active by day, with two peaks: after dawn and before dusk.
- The **paca** and the spiny **rat** are active at night.
- The red **brocket** deer is active at all hours.
- The collared **peccary** is active by day, with peaks at dawn and dusk.

In the fit for this tutorial, the overlaps of the ocelot with each prey are:

| Prey | Overlap with the ocelot (median) | 94% range | Notebook |
|---|---|---|---|
| agouti | 0.301 | 0.257 to 0.346 | 0.30 |
| peccary | 0.656 | 0.611 to 0.699 | 0.66 |
| paca | 0.664 | 0.618 to 0.711 | 0.66 |
| rat | 0.702 | 0.655 to 0.748 | 0.70 |
| brocket | 0.749 | 0.701 to 0.795 | 0.75 |

### 6.8 The numbers of the notebook and of this port

| Quantity | Notebook | This port |
|---|---|---|
| `elpd_loo`, von Mises regression, 2021 and 2022 | −17,385 | −17,385.45 (se 79.6) |
| Mean log score of a 2023 report, von Mises regression | −1.517 | −1.517 |
| Mean resultant length, afternoon sea breeze | 0.70 (observed, 12:00 to 18:00) | 0.70 (model, 12:00 to 15:00) |
| Mean resultant length, evening | 0.12 (observed, 18:00 to 21:00) | 0.13 (model) |
| `elpd_loo`, mixture minus von Mises | About 1,560 | 1,565 |
| Log score of all 2023 reports, mixture minus von Mises | 916 ± 75 | 917 (−8,226 against −9,143) |
| Overlap of the ocelot and the agouti | 0.30 | 0.301 |
| Overlap of the ocelot and the brocket, rat, paca and peccary | 0.75, 0.70, 0.66, 0.66 | 0.749, 0.702, 0.664, 0.656 |
| Activity level of the ocelot | About a third (0.26 to 0.43) | 0.34 (90% HDI 0.27 to 0.41) |
| Activity level of the agouti | 0.262 | 0.263 |
| Mean time and length of the agouti | 11:00 | 11.0 h, 0.52 |
| Mean time and length of the peccary | 21:00, 0.09 | 21.1 h, 0.10 |

The mean log score of 2023 comes from `CircularRegressionModel().holdout_log_score` in Python.
The web app does not show it (see section 10). Your numbers can be slightly different if the
sampler or its version is different.

### 6.9 Make the decisions

Use these outputs to make the decisions of the notebook:

1. **The link.** The model always uses the canonical link. Make sure that **Diagnostics** shows
   no divergences and an R-hat of 1.01 or less. You do not need special start values.
2. **One direction or two regimes.** Compare `elpd_loo` of `von_mises` and `regime_mixture`
   (section 6.6). Then compare the **Fields** of the two versions in the blocks that start at
   9 and 18. The mixture is better by 1,565 nats, and it shows the two lobes. Use it to predict
   the direction. Do not decide with `mae` or `coverage_90` of step 7.3. They are almost the
   same for the two kinds (52.1 and 52.4 degrees, 0.87 and 0.87), because they use one point
   for each report and do not see two lobes.
3. **Report a direction with its length.** Use `mean_direction` together with
   `resultant_length` (section 6.5). Do not report a mean direction with a length below
   about 0.2.
4. **Overlap with uncertainty.** Report the median and the range of `overlap` (section 6.7).
   The ocelot overlaps least with the agouti. The ranges of the other four prey overlap each
   other, so do not rank them.

## 7. Score the model

### 7.1 Predict: the mean direction of each block

1. On the model version page of `ksba-wind-direction`, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**. Select `ksba_wind_2021_2023`.
4. Open **Advanced**. Set **Seed (optional)** to `81`. Type
   `direction_hat, resultant, mean_direction, resultant_length` in **Variables (optional)**.
5. Click **Predict**.

The request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`) is:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://meteorology/ksba-wind-direction@latest",
  "mode": "predict",
  "data": "dataset://meteorology/ksba_wind_2021_2023@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["direction_hat", "resultant", "mean_direction", "resultant_length"]
  },
  "seed": 81
}
```

The result has `direction_hat(report)` and `resultant(report)`: the mean direction and the mean
resultant length of the predictive distribution of each report. It also has
`mean_direction(frame)` and `resultant_length(frame)` for the 8 blocks (section 6.5). The
result also shows a warning: the dataset has directions after the fitted reports, and the
predict mode does not use them. This is correct.

!!! caution "The summary of a direction near north"
    The `mean` and `hdi_90` of the summary are not circular. For a report with a mean direction
    near 0 or 360 degrees, the draws are on the two sides of north. Then the summary `mean` is
    near 180 degrees, and `hdi_90` covers almost the full circle. In the fit for this tutorial,
    538 of the 18,049 reports have this problem. For these reports, ask for draws (**Draws** in
    **Advanced**), and calculate the circular mean: `atan2(mean(sin θ), mean(cos θ))`.

!!! note "No new data"
    **No new data** does not work for this model. The request then has no `data`, and the
    server refuses it with "the score request has no data" before a run starts.

### 7.2 Simulate: new winds

`simulate` gives one simulated direction for each report and each draw. Use it to see the
spread of single winds around the mean direction.

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**. Select `ksba_wind_2021_2023`.
3. Open **Advanced**. Set **Quantity** to **Predictive**. Set **Draws** to `20`.
4. Type `direction_hat` in **Variables (optional)**.
5. Click **Simulate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://meteorology/ksba-wind-direction@latest",
  "mode": "simulate",
  "data": "dataset://meteorology/ksba_wind_2021_2023@latest",
  "output": {
    "kind": "predictive",
    "summary": ["mean", "hdi_90"],
    "draws": 20,
    "vars": ["direction_hat"]
  },
  "seed": 81
}
```

Each draw of `direction_hat` is one simulated wind, in degrees from 0 to 360. The draws spread
much more than the mean direction of section 6.5, because a single wind varies much around the
mean. For a forecast of the wind at an hour of a future day, make a dataset with `hour_lst` and
`day_of_year` of that hour, and any value in `wind_from`.

### 7.3 Evaluate: the held-out year 2023

The notebook scores the reports of 2023, which the fit did not see. To do this in the web app,
make a dataset of the 2023 reports. Run this Python code in the directory of the exported file:

```python
import xarray as xr

ds = xr.load_dataset("ksba_wind_2021_2023.nc")
ds.isel(report=ds["year"].values == 2023).to_netcdf("ksba_wind_2023.nc")
```

The new file has 6,026 reports and keeps the attribute `mapi_schema`. Upload it as the dataset
`ksba_wind_2023` (step 3). Then:

1. In **What do you want to do?**, select **Evaluate**.
2. In **Values to score**, select **Use a dataset**. Select `ksba_wind_2023`.
3. Open **Advanced**. Set **Quantity** to **Predictive**.
4. Click **Evaluate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://meteorology/ksba-wind-direction@latest",
  "mode": "evaluate",
  "data": "dataset://meteorology/ksba_wind_2023@latest",
  "output": {"kind": "predictive", "summary": ["mean", "hdi_90"]},
  "seed": 81
}
```

`direction_hat` is a circular output, so the metrics are circular. An error of 359 degrees
against 1 degree is 2 degrees. In the fit for this tutorial, the metrics of 2023 are:

| Metric | Value | Meaning |
|---|---|---|
| `mae` | 52.1 degrees | The mean error of the circular mean of the simulated winds |
| `rmse` | 67.0 degrees | The root mean square error |
| `bias` | 2.8 degrees | The mean signed error |
| `coverage_90` | 0.87 | The share of the 2023 winds inside the central 90% range of the simulated winds |
| `crps` | 34.6 degrees | The continuous ranked probability score with the arc distance. Lower is better |

`mape`, `smape`, `wape` and `r2` are not defined for a circular output, and the result skips
them.

!!! note "Use Predictive for the coverage"
    With **Expected**, each draw is the mean direction of a report, not a single wind. The
    range of the mean is narrow, so `coverage_90` is only 0.06. Use **Predictive** to compare
    single winds with their range.

### 7.4 The camera traps

For the activity model, the outputs for each species are `activity_level(frame)`,
`mean_direction(frame)` (the mean time of day), `resultant_length(frame)`, `density(frame,
angle)` and `overlap(frame, frame_)`. Send the dataset that you fitted:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://ecology/bci-ocelot-activity@latest",
  "mode": "predict",
  "data": "dataset://ecology/bci_camera_traps@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["activity_level", "mean_direction", "resultant_length", "overlap"]
  },
  "seed": 81
}
```

`activity_level` is `1 / (2π max f)`, the part of the day that a species is active, if all
animals are active at the peak of the curve. In the fit for this tutorial, the ocelot is active
0.34 of the day (90% HDI 0.27 to 0.41), and the agouti 0.26 (0.25 to 0.28).

- The model has no `forecast` mode.
- A score dataset with a species that the fit did not see stops the score.
- A score dataset without `hour_lst` or `day_of_year` (wind) stops with
  `MISSING_FUTURE_COVARIATE`.

If you promote a version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same requests.

## 8. Run a workflow recipe

The recipe `bayes_workflow_audit` (title **Bayesian workflow audit**) does the checks of a
Bayesian workflow on any model with draws. It fits the model, gates on convergence (R-hat below
1.01 and a bulk ESS of 400 or more), makes the posterior predictive check, and runs the
governance checks `sensitivity.prior` and `recovery.simulated`. Then it writes a report with
the plots.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Bayesian workflow audit** and click **Run on my data**.
3. In **1. Your data**, select `ksba_wind_2021_2023` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.circular_regression@^1` in **Model**.
5. Open **Fine-tune (optional: the defaults are correct for most data)**.
6. In **Settings**, type the `settings` of step 4 as JSON: `{"kind": "von_mises", "units": "degrees",
   "daily_harmonics": 3, "annual_harmonics": 2, "interaction_harmonics": 2}`.
7. In **Bind**, type the `bind` of step 4 as JSON: `{"dims": {"obs": "report"}, "vars": {"direction":
   "wind_from", "hour": "hour_lst", "day": "day_of_year"}}`.
8. Click **Run this recipe**.

The recipe can bind `wind_from` to `direction`, because the dataset has one target. It cannot
bind `hour_lst` and `day_of_year`, because they both have the role `covariate`. Thus give the
`bind` of step 7.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Decisions** shows the convergence gate and the value that it read. **Outputs**
shows the plots and the report. The model version link opens the Model Explorer.

!!! note "The audit fits all three years"
    The recipe has no split. Its fit uses all 18,049 reports, so its `elpd_loo` is not on the
    same scale as the value of step 6.6.

!!! note "The checks of the audit and a circular target"
    The model keeps prior draws of its coefficients, but not of the directions. Thus the plot
    "Prior predictive check" of the report can be empty. `evaluate.ppc` compares the simulated
    and the observed angles as numbers on a line, so read its coverage and p-value with care
    for winds near north. The checks that refit the model (`sensitivity.prior`,
    `recovery.simulated`) each take as long as one fit.

To see the rose as a compass, add a `plot.render` node in the workflow editor (click **Use this
template** on the card). Give it the input `model: fit.model` and `with: {recipe: sci.rose,
source: model, settings: {clockwise: true, bins: 12}}`.

The recipes `sci_spatial_ladder`, `sci_network_ladder`, `sci_phylo_signal`,
`sci_calibrate_then_design` and `sci_image_model_check` are for other science models. They do
not apply to this model.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/meteorology
    mapi datasets upload ksba_wind_2021_2023.nc --name ksba_wind_2021_2023
    mapi check ksba-wind-direction.yaml --remote @     # the server's static validation
    mapi fit ksba-wind-direction.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model ksba-wind-direction -s params -s diagnostics -s fields

    cat > winds-2023.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://meteorology/ksba-wind-direction@latest
    mode: evaluate
    data: dataset://meteorology/ksba_wind_2023@latest
    output: {kind: predictive, summary: [mean, hdi_90]}
    seed: 81
    EOF
    mapi score winds-2023.yaml --remote @ --model <model id> --version 1 -o winds-2023.zarr.zip

    mapi workflows init bayes_workflow_audit --name ksba-wind-audit
    mapi workflows apply ksba-wind-audit.yaml -m "E81 audit"
    mapi workflows run ksba-wind-audit \
      --input data=dataset://meteorology/ksba_wind_2021_2023@latest \
      --param model=sci.circular_regression@^1 \
      --param 'settings={"kind": "von_mises", "units": "degrees"}' \
      --param 'bind={"dims": {"obs": "report"}, "vars": {"direction": "wind_from", "hour": "hour_lst", "day": "day_of_year"}}' \
      --watch
    ```

    `ksba-wind-direction.yaml` is the spec of step 4.1. For the camera traps, run
    `mapi use <tenant>/ecology` and do the same with `bci_camera_traps.nc` and the spec of
    step 4.2. The CLI has no `models` command. Take the model id from the run (`mapi runs get
    <run>`) or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/meteorology")
    version = api.datasets.upload("ksba_wind_2021_2023.nc", dataset="ksba_wind_2021_2023")
    print(version.ref, version.status.value)

    with open("ksba-wind-direction.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "ksba-wind-direction")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://meteorology/ksba-wind-direction@latest",
            "mode": "predict",
            "data": "dataset://meteorology/ksba_wind_2021_2023@latest",
            "output": {
                "kind": "expected",
                "summary": ["mean", "hdi_90"],
                "vars": ["mean_direction", "resultant_length"],
            },
            "seed": 81,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["mean_direction"].sel(stat="mean").values)
    print(summary["resultant_length"].sel(stat="mean").values)
    ```

    To run the audit recipe, use `workflow = api.workflows.create(template="bayes_workflow_audit")`
    and `api.workflows.run(workflow.id,
    inputs={"data": "dataset://meteorology/ksba_wind_2021_2023@latest"},
    params={"model": "sci.circular_regression@^1", "settings": {...}, "bind": {...}},
    wait=True)` with the `settings` and the `bind` of step 4.

## 10. Differences from the notebook

The port keeps the circular-linear regressions of part D: von Mises with the canonical link,
projected normal and the mixture of two regimes. It also keeps the log-Fourier activity
densities, the overlaps and the activity levels of part E. It leaves out these parts:

- **The control dataset and the four distributions (part A).** The port does not fit the
  Oklahoma earthquake times, and it has no wrapped normal or wrapped Cauchy. To do a check of
  "no daily pattern", fit `kind: activity` with one group and `units: hours`, and look for a
  flat density.
- **The direction against the wind speed (part C).** The dataset has no speed, and the model
  has no tan-half link. The notebook shows that link only to show its two problems.
- **The log score of the held-out year.** The web app gives circular holdout metrics (step
  7.3), not the log predictive density. For the log score, use
  `CircularRegressionModel().holdout_log_score(bundle.model, ds)` in Python.
- **The day-level standard error of the 2023 difference.** Calculate it in Python from the
  pointwise log scores.
- **The circular PIT, the clock of wind arrows and the arcs of the circular HDI (part D).**
  The web app has no plot for them. `mean_direction` and `resultant_length` (step 7.1) give
  the values of the arrows for each block of three hours.
- **The gate of the mixture by hour and season (part D).** The model does not output the
  probability of each regime.
- **The von Mises mixtures of the activity, and their labels that change places (part E).** The port uses
  only the log-Fourier density, which has no labels. The kernel estimate and the bootstrap of
  the R package *overlap* are not in the port.

**Next steps:**

- Fit `kind: projected_normal` and `kind: regime_mixture`, and compare them with `elpd_loo`.
- Try the slower prior of the notebook exercise for the activity: a larger `tau_sd` or more
  `harmonics`. Then compare `activity_level` of the agouti.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the audit recipe.
