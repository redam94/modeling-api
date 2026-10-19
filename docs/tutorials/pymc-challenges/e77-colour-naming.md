# E77 how languages name colours

This tutorial does the work of the pymc-challenges notebook `E77_colour_naming.py` in the web
app. You use the model `sci.colour_naming@1`. You do not write PyMC code.

**You need:**

- A running platform (`make dev`) and a project that you can upload datasets to. This tutorial
  uses the project `linguistics`. If your project has a different name, change the project part
  of each `dataset://` and `model://` reference, and `metadata.project` in the spec.
- A local copy of pymc-challenges. The export command reads four files from its `data/`
  directory.
- About 15 minutes. The fit takes about four minutes of sandbox time.

## 1. The question

The World Color Survey (WCS) asked the speakers of 110 unwritten languages to name 330 colour
chips. Each speaker named each chip once. This tutorial uses **Nafaanra**, a language of Ghana
with 29 speakers in the survey. Nafaanra has a small system of four major terms: **F**
(*finge*, white and the palest chips), **W** (*wɔɔ*, dark, green, blue and purple), **N**
(*nyie*, red, orange, yellow and brown) and **B** (*bubuo*, some pale purples).

The notebook asks these questions of the naming of one language:

1. **Where are the boundaries between the colour terms**, and how sure are you of them?
   (Notebook parts A, B and D.)
2. **Do the speakers agree?** Is a chip that 55% of the speakers call N a fuzzy boundary that all
   speakers share, or a sharp boundary that each speaker puts in a different place? (Part C.)
3. **How many categories does the language really have?** (Part E, the effective number of
   categories.)
4. **How well can you predict a new speaker?** (Part G, the held-out speakers.)

The notebook ends with these decisions:

- Use a model in CIELAB colour space, not on the printed grid. A grid model must know that hue
  is a circle.
- Use the hierarchical model (speaker effects) to describe the speakers. The pooled model gets
  each chip right, but it fails a check for each speaker.
- Keep two uncertainties apart. New speakers vary much along the boundaries. The language
  itself is known to about one chip.
- A focus (the best example that speakers mark) is not the chip that most speakers agree on.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The share of the speakers who give each term to each chip, on the palette, with mean, sd and draws (**Fields**, `sci.field_map`) | Part D, the disc charts of each term |
| Each chip in the colour of its most probable term, next to the observed naming (`sci.swatches`) | Parts A and B, the raw naming map and the "poster" maps |
| The between-speaker sds `sd` and the weights `W` (**Parameters**, `core.posterior_forest`) | Part C, the sds of the speaker effects |
| The scores of the 9 held-out speakers (`log_score`, `accuracy`, `accuracy_lift`, `brier`) | Part G, the log score of new speakers |
| The effective number of categories of one speaker and of the language (**Score**) | Part E, the effective categories |
| Simulated new speakers (**Score**, mode Simulate) | Part D, the animation of new speakers |

See [Differences from the notebook](#10-differences-from-the-notebook) for the parts that the
port leaves out.

## 2. Get the data

The export command reads `wcs_naming.npz`, `wcs_terms.csv`, `wcs_chips.csv` and
`wcs_languages.csv`. It keeps the 29 speakers of Nafaanra (WCS language 77). A term is
*major* if at least a third of the speakers used it. Each major term keeps its WCS
abbreviation, and all other terms become `other`. Run the command from the root of the
modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e77 \
    --challenges <path to pymc-challenges>/data \
    --out wcs_nafaanra.nc --spec wcs_nafaanra.yaml
```

The command writes:

```text
wrote wcs_nafaanra.nc (variables response, cie_l, cie_a, cie_b, wcs_row, wcs_col on speaker=29, chip=330)
upload it as dataset linguistics/wcs_nafaanra and fit sci.colour_naming
wrote the suggested spec to wcs_nafaanra.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `speaker` | 29 | categorical, policy `new_levels`, strategy `hyperprior` | The speakers, `s01` to `s29` |
| `chip` | 330 | integer, policy `fixed` | The WCS chip number |
| `grid` | `chip` | text coordinate | The name of the chip on the palette, for example `E29` |
| `response` | `speaker, chip` | str, target, missing values allowed | The term that the speaker gave to the chip. An empty value is no answer |
| `cie_l`, `cie_a`, `cie_b` | `chip` | float64, covariate | The CIELAB colour of the chip |
| `wcs_row`, `wcs_col` | `chip` | int64, covariate | The place of the chip on the palette. Rows A to J are 0 to 9. Column 0 holds the greys, and columns 1 to 40 are the hues |

The dataset holds 9,570 answers: W 5,082, N 2,757, F 1,550 and B 173. Only 8 cells have no
answer. All terms of Nafaanra are major, so the dataset has no `other` term. 16 of the 29
speakers never use B. The other 13 use it for 1 to 24 chips. The attribute `mapi_schema` gives
each variable its role, so you do not map columns. The attribute `glosses` gives the word for
each abbreviation.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes naming that the model's own process simulates: 12 speakers, a small
    palette of 38 chips and three terms (`light`, `dark` and `warm`), with known parameters.
    Use it to try the steps. The numbers in this tutorial are for Nafaanra.

    ```bash
    uv run python -m mapi_plugin_science.export e77 --synthetic --out colour_naming.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `wcs_nafaanra` in **Name**. Type a description, for example "Colour naming of the 29
   Nafaanra speakers of the World Color Survey".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `wcs_nafaanra.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Optional: click **Preview structure**. The panel shows **Structure of the file**. Make sure
   that it shows `response` on `speaker` and `chip`, and five variables on `chip`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `response` has the role
`target` and the type `str`. **Missingness** shows the 8 cells without an answer. The model
accepts them: a missing answer leaves the likelihood.

The file is one plain dataset. You do not need the tree fields ("Put this file in a node of a
tree (optional)") or the HDF5 path.

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `wcs_nafaanra`. The toast "Dataset set" shows.
5. Type `colour` in the search field. Under **Models**, select **How people name colours**
   (`sci.colour_naming@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `nafaanra-colour-naming` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: nafaanra-colour-naming, project: linguistics}
data:
  dataset: dataset://linguistics/wcs_nafaanra@latest
  bind:
    vars: {naming: response, lab_l: cie_l, lab_a: cie_a, lab_b: cie_b,
           grid_row: wcs_row, grid_col: wcs_col}
  split: {kind: leave_group_out, dim: speaker,
          groups: [s03, s04, s07, s13, s18, s19, s22, s25, s27]}
model:
  use: sci.colour_naming@^1
  settings: {kind: hierarchical, features: cielab, new_speakers: 20}
  fit: {mode: evaluate_then_refit, seed: 77, tune: 1000, draws: 1000, chains: 4,
        engine: nutpie}
evaluate: {metrics: [log_score, accuracy, accuracy_lift, brier, elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.swatches, sci.field_map]
```

The model calls the target `naming` and the chip variables `lab_l`, `lab_a`, `lab_b`,
`grid_row` and `grid_col`. The `bind` block connects them to the names in the dataset. The
dims `speaker` and `chip` have the same names in the model and in the dataset.

The split holds out the same 9 speakers as the notebook. `evaluate_then_refit` fits on the
other 20 speakers and scores the 9 held-out speakers as new speakers. Then it fits again on all
29 speakers. The suggested spec of the export (`wcs_nafaanra.yaml`) is the same spec without
`new_speakers: 20`, which is the default.

The settings for this question:

| Setting | Value | What it means here |
|---|---|---|
| `kind` | `hierarchical` | Each speaker gets an intercept and linear weights (on L, a and b) of their own, around those of the language. A speaker can use a term more or less, and move its boundaries. `pooled` gives all speakers the same naming |
| `features` | `cielab` | The logit of each term is a quadratic surface in CIELAB colour space. `grid_line` and `grid_circle` use the row and the hue column of the palette (notebook part B) |
| `new_speakers` | 20 | In each draw, the fit simulates 20 new speakers to calculate the share of the speakers who give each term to each chip. The notebook also uses 20 |
| `w_sd` | 3 (default) | The prior sd of the zero-sum weights. The notebook uses the same scale after its prior predictive check |
| `sd_scale` | 1 (default) | The half-normal scale of the sds between the speakers |
| `n_random` | 4 (default) | The speaker effects are on the first 4 features: the intercept, L, a and b |
| `hue_columns` | the largest `grid_col` (default) | Only for `grid_circle`: the number of hue columns around the circle. The WCS has 40 |
| `score_draws` | 200 (default) | The number of posterior draws that one score uses |

!!! note "Settings that do not agree"
    **Validate** does a check of each setting by itself. It does not compare two settings.
    For example, `features: grid_line` with `n_random: 9` shows `valid`, but the run stops
    with `SETTINGS_INVALID` because `grid_line` has only 7 features.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the progress of the sampler. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://linguistics/nafaanra-colour-naming@v1**. Click it.

The run does two fits: one on the 20 training speakers for the metrics of the held-out
speakers, and one on all 29 speakers. On a laptop with nutpie, the two fits took 232 seconds. After the fits, the run calculates the
language-level shares for each draw, the plots and the explorer views.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer, or the plot **Sampler convergence** on the
**Plots** tab. In the fit for this tutorial, there were no divergences.
The largest R-hat was 1.02 and the smallest bulk ESS was 278 (of 4,000 draws). Both values are
for the weights `W`. The sds `sd` mix well (R-hat 1.01 or less, bulk ESS 730 or more). The
scorecard shows warnings for R-hat, for the bulk ESS and for 17 chips with a high Pareto k in
`elpd_loo`. If you need a stricter result, increase `tune` and `draws` to 2000.

### 6.2 Do the speakers agree?

**Posterior forest** shows the weights `W` and the sds `sd` between the speakers. The sds are
on the logit scale of the standardised features. In this fit, the means of `sd` are:

| Feature | W | N | F | B |
|---|---|---|---|---|
| Intercept (`1`) | 0.25 | 0.36 | 0.47 | 2.64 |
| `L` | 0.56 | 0.09 | 0.22 | 0.45 |
| `a` | 0.54 | 0.54 | 0.13 | 1.13 |
| `b` | 0.36 | 0.29 | 0.10 | 0.20 |

Read the table as the notebook does:

- The intercept sds of F, N and W are small. The speakers use these three terms at similar
  rates.
- The intercept sd of **B** is large. Some speakers use B for a patch of chips, and others never
  use it.
- The larger linear sds (W on L and a, N on a) show that the speakers put the light/dark and
  the warm/cool boundaries in different places.

The pooled model cannot show this. It says that each speaker names a boundary chip with the
same probabilities.

### 6.3 Where are the boundaries? The maps

Open **Fields** in the Model Explorer. The model has 1 output on a grid: `p_term`, the share of
the speakers of the language who give each term to each chip. The rows are the 10 rows of the
palette (A at the top), and the columns are the 41 columns (the greys in column 0).

1. Move the slider `term` to select a term: W, N, F or B.
2. Select **Mean** in **Statistic**. A bright cell is a chip that most speakers give this term.
3. Select **Std. dev.** to see where you are not sure of the share. The sd is large along the
   boundaries.
4. Select **Draws** and click **Play the draws** to see possible maps that agree with the data.
5. Move the pointer over a cell to read its values.

The cells of row A and row J, except column 0, have no chip. They are empty. The plot
**Field map** on the **Plots** tab shows the same output as panels (mean and sd) with a slider.

Look for these features, which the notebook also finds:

- **F** covers white and row B. Its boundary under row B is sharp.
- **W** covers the dark greys, black and almost all greens, blues and purples.
- **N** covers the reds, oranges, pinks, browns and yellows. The N/W boundary runs along a
  diagonal from the dark browns to the yellow-greens, and through the purples near columns 34
  to 37. It is wide: a band of several chips, not a line.
- **B** is small everywhere. Its most probable chip is D35, a pale purple. Only 11% of new
  speakers (90% range 3% to 21%) give it B.

### 6.4 The posterised map

The plot **Colour swatches** (`sci.swatches`, title "Colours of mode_term_rgb") paints each chip
on the palette in the display colour of its most probable term. The display colour of a term is
the average colour of the chips where the term is most probable.

- The first panel shows the observed naming: each chip in the colour of the term that most
  speakers gave it.
- The mean panel shows the average over the draws.
- The draw panels show possible maps of the language. Where they differ, the majority term of
  the chip is not sure.

In this fit, the draw panels differ only on a small number of chips. On 27 of the 330 chips,
less than 95% of the draws give the same majority term. Look for these chips along the
boundaries of section 6.3.

### 6.5 The held-out speakers

The metrics are in the **Metrics** card of the **Overview** tab. They score the 2,970 answers of
the 9 held-out speakers. The model did not see these speakers, so it predicts them as new
speakers.

| Metric | Value | Meaning |
|---|---|---|
| `log_score` | −0.403 | The mean log probability of the answer that the speaker gave, in nats for each answer. Higher is better |
| `accuracy` | 0.882 | The share of the answers that are the most probable term |
| `accuracy_lift` | 0.341 | The accuracy minus the accuracy of the most frequent term (W) |
| `brier` | 0.195 | The mean squared error of the probabilities. Lower is better |
| `elpd_loo` | −3,959 (se 197) | Leave one chip out, on the fit of all 29 speakers. 17 chips have a high Pareto k |

Compare `elpd_loo` only between fits with the same speakers and the same `fit.mode`. The value
of a fit on 20 speakers is not on the same scale as the value of a fit on 29 speakers.

### 6.6 The numbers of the notebook and of this port

| Quantity | Notebook | This port |
|---|---|---|
| Intercept sd between the speakers: W, N, F | 0.25 to 0.47 | 0.25, 0.36, 0.47 |
| Intercept sd between the speakers: B | About 2.6 | 2.64 |
| Held-out `log_score`, pooled, CIELAB | −0.392 | −0.392 |
| Held-out `log_score`, grid with hue as a line, minus CIELAB | −0.034 | −0.034 |
| Held-out `log_score`, grid with hue as a circle, minus CIELAB | −0.004 | −0.004 |
| Held-out `log_score`, pooled and hierarchical | −0.389 and −0.398 (part G, 150 chips for each speaker) | −0.392 and −0.403 (all 330 chips) |
| Effective categories of one speaker | About 2 | 1.92 (90% range 1.86 to 1.98) |
| Effective categories of the language | Lower than for one speaker | 1.84 (90% range 1.77 to 1.90) |
| Chips where a new speaker gives the top term with probability below 0.7 | 71 | 71 |
| Chips with a majority term that is not sure (less than 0.95) | 26 | 27 |
| Safest chip of B | D35, 11% (4% to 21%) | D35, 11% (3% to 21%) |
| Safest chips of W and N | G23 (0.99) and E4 | G23 (0.99) and E4 (0.98) |

The last four rows come from the draws of `p_term`. **Fields** shows the same values for each
chip. The held-out scores of the pooled and grid fits come from three fits of the spec with
`kind: pooled`, one for each value of `features`.

Your numbers can be slightly different if the sampler or its version is different.

### 6.7 Make the decisions

Use these outputs to make the decisions of the notebook:

1. **CIELAB or the grid.** Fit the spec again with `kind: pooled` and each value of
   `features`. Compare `log_score` of the held-out speakers. In the fits for this tutorial,
   `cielab` scores −0.392 nats for each answer, the grid with hue as a line −0.426, and the grid
   with hue as a circle −0.396. Hue as a line costs approximately 100 nats over the 2,970
   held-out answers. Keep `cielab`. It knows that hue is a circle, and its distances are
   approximately perceptual.
2. **Pooled or hierarchical.** Look at the sds of section 6.2. A large intercept sd (B) or
   large linear sds (W, N) show that the speakers do not name the same way. Use the
   hierarchical model to describe the speakers. For the share of all speakers on one chip, the
   pooled model is as good and slightly better on `log_score`.
3. **Two uncertainties.** For the variation between the speakers, use the predictions for new
   speakers (step 7, mode Simulate). For the uncertainty about the language, use the **Std.
   dev.** of `p_term` in **Fields**, and the draw panels of **Colour swatches**. In this fit, a new
   speaker gives the most probable term with a probability below 0.7 on 71 chips. But the
   majority term of the language is not sure on only 27 chips.
4. **How many categories.** Use `k_eff_speaker` and `k_eff_language` (step 7). A language with
   k equally used, sharp categories scores k. Nafaanra has 4 major terms, but it acts like 2
   sharp categories.
5. **Best example or safest chip.** The model gives the chip that most speakers agree on. It
   does not give the focus. The dataset has no foci, so make this comparison in the notebook.

## 7. Score the model

The fitted version uses all 29 speakers. A score needs a dataset that has the dims `speaker`
and `chip`. Send the dataset that you fitted.

### 7.1 Predict: the speakers and the effective categories

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**. Select `wcs_nafaanra`.
4. Open **Advanced**. Set **Seed (optional)** to `77`. Type
   `p_name, k_eff_speaker, k_eff_language` in **Variables (optional)**.
5. Click **Predict**.

The request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`) is:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://linguistics/nafaanra-colour-naming@latest",
  "mode": "predict",
  "data": "dataset://linguistics/wcs_nafaanra@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["p_name", "k_eff_speaker", "k_eff_language"]
  },
  "seed": 77
}
```

The result has `p_name(speaker, chip, term)`: the probability that each of the 29 speakers
gives each term to each chip. Each fitted speaker keeps their own effects. The outputs `k_eff_speaker` and `k_eff_language` are the same
for each speaker, because they describe the language. In this fit, `k_eff_speaker` is 1.92
(approximately 1.87 to 1.98) and `k_eff_language` is 1.83 (approximately 1.77 to 1.90). The
value for the language is lower because the speakers do not agree on all chips.

!!! warning "Do not select No new data"
    Select **Use a dataset**. With **No new data**, the request has no `data`, and the run
    stops with the message "the score request has no data".

### 7.2 Simulate: new speakers

`simulate` gives each speaker of the dataset new speaker effects. Thus the result is 29 new
Nafaanra speakers, as in the animation of the notebook.

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**. Select `wcs_nafaanra`.
3. Open **Advanced**. Set **Quantity** to **Predictive**. Set **Draws** to `20`.
4. Type `p_name` in **Variables (optional)**.
5. Click **Simulate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://linguistics/nafaanra-colour-naming@latest",
  "mode": "simulate",
  "data": "dataset://linguistics/wcs_nafaanra@latest",
  "output": {
    "kind": "predictive",
    "summary": ["mean", "hdi_90"],
    "draws": 20,
    "vars": ["p_name"]
  },
  "seed": 77
}
```

With **Predictive**, each draw holds one sampled term for each speaker and chip (a one-hot
vector over the terms). Thus each draw is one complete naming of the palette by a new speaker.
In this score, 37% of the simulated speakers never use B, and one simulated speaker uses B for
142 chips. In the data, 16 of the 29 speakers (55%) never use B, and no speaker uses it for more
than 24 chips. The numbers of chips for each term also vary more between the simulated speakers
(sd 12.5 for W) than between the real speakers (sd 8.9). The notebook finds the same weakness:
a shift of one shared shape does not describe a term that only some speakers have.

- To score a speaker that the fit did not see, upload a dataset with a new speaker label (for
  example `new01`) and the same chips. **Predict** then gives that speaker new effects in each
  draw. The mean over the draws is the prediction for a new speaker.
- **Evaluate** needs a dataset with answers. It scores them with `log_score`, `accuracy`,
  `accuracy_lift` and `brier`.
- A dataset with a chip that the fit did not see stops the score with `UNSEEN_LEVELS`.
- The model has no `forecast` mode.

If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same requests.

## 8. Run a workflow recipe

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does the checks of a Bayesian
workflow on any model with draws. It fits the model with prior predictive draws and plots
them. A convergence gate needs the largest R-hat below **Rhat max** (1.01) and the smallest
bulk ESS at or above **Min ess** (400). Only then does it make the posterior predictive check
and run the governance checks `sensitivity.prior` and `recovery.simulated`. Then it writes a
report with the plots.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Bayesian workflow audit** and click **Run on my data**.
3. In **1. Your data**, select `wcs_nafaanra` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.colour_naming@^1` in **Model**.
5. Open **Fine-tune**. In **Settings**, type `{"kind": "hierarchical", "features": "cielab",
   "new_speakers": 20}`. In **Bind**, type `{"vars": {"naming": "response", "lab_l": "cie_l",
   "lab_a": "cie_a", "lab_b": "cie_b", "grid_row": "wcs_row", "grid_col": "wcs_col"}}`.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Decisions** shows the convergence gate and the value that it read. **Outputs**
shows the plots and the report. The model version link opens the Model Explorer.

!!! warning "Give the recipe the binding"
    Without **Bind**, the recipe binds only the target `response` to `naming`. It cannot bind
    the five chip variables, because they all have the role `covariate`, and the fit stops.

!!! warning "The default gate can stop this audit"
    In the fit of this tutorial, the largest R-hat was 1.019 and the smallest bulk ESS was
    278. Thus the default gate (R-hat below 1.01, bulk ESS 400 or more) takes the else arm,
    and the recipe skips the checks after the gate. Increase **Tune** and **Draws** to `2000`
    in **Fine-tune**, then run the recipe again.

!!! note
    The checks after the gate refit the model. `sensitivity.prior` fits it one time for each
    value in **Prior scales** (two by default), and `recovery.simulated` fits it **N sims**
    times (20 by default). Thus the recipe takes many times as long as one fit.

The recipes `sci_spatial_ladder`, `sci_network_ladder`, `sci_phylo_signal`,
`sci_calibrate_then_design` and `sci_image_model_check` are for other science models. They do
not apply to this model.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/linguistics
    mapi datasets upload wcs_nafaanra.nc --name wcs_nafaanra
    mapi check nafaanra-colour-naming.yaml --remote @     # the server's static validation
    mapi fit nafaanra-colour-naming.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model nafaanra-colour-naming -s params -s diagnostics

    cat > new-speakers.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://linguistics/nafaanra-colour-naming@latest
    mode: simulate
    data: dataset://linguistics/wcs_nafaanra@latest
    output: {kind: predictive, summary: [mean, hdi_90], draws: 20, vars: [p_name]}
    seed: 77
    EOF
    mapi score new-speakers.yaml --remote @ --model <model id> --version 1 -o new-speakers.zarr.zip

    mapi workflows init bayes_workflow_audit --name nafaanra-audit
    mapi workflows apply nafaanra-audit.yaml -m "E77 audit"
    mapi workflows run nafaanra-audit -i data=dataset://linguistics/wcs_nafaanra@latest \
      -p model=sci.colour_naming@^1 \
      -p 'settings={"kind": "hierarchical", "features": "cielab", "new_speakers": 20}' \
      -p 'bind={"vars": {"naming": "response", "lab_l": "cie_l", "lab_a": "cie_a", "lab_b": "cie_b", "grid_row": "wcs_row", "grid_col": "wcs_col"}}' --watch
    ```

    `nafaanra-colour-naming.yaml` is the spec of step 4. The CLI has no `models` command. Take
    the model id from the run (`mapi runs get <run>`) or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/linguistics")
    version = api.datasets.upload("wcs_nafaanra.nc", dataset="wcs_nafaanra")
    print(version.ref, version.status.value)

    with open("nafaanra-colour-naming.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "nafaanra-colour-naming")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://linguistics/nafaanra-colour-naming@latest",
            "mode": "predict",
            "data": "dataset://linguistics/wcs_nafaanra@latest",
            "output": {
                "kind": "expected",
                "summary": ["mean", "hdi_90"],
                "vars": ["k_eff_speaker", "k_eff_language"],
            },
            "seed": 77,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["k_eff_speaker"].sel(stat="mean").values)
    print(summary["k_eff_language"].sel(stat="mean").values)
    ```

    To run the audit recipe:

    ```python
    workflow = api.workflows.create(template="bayes_workflow_audit")
    run = api.workflows.run(
        workflow.id,
        inputs={"data": "dataset://linguistics/wcs_nafaanra@latest"},
        params={
            "model": "sci.colour_naming@^1",
            "settings": {
                "kind": "hierarchical",
                "features": "cielab",
                "new_speakers": 20
            },
            "bind": {
                "vars": {
                    "naming": "response",
                    "lab_l": "cie_l",
                    "lab_a": "cie_a",
                    "lab_b": "cie_b",
                    "grid_row": "wcs_row",
                    "grid_col": "wcs_col"
                }
            }
        },
        wait=True,
    )
    ```

## 10. Differences from the notebook

The port keeps the naming model of parts B and C (pooled and hierarchical, CIELAB and the two
grid feature sets) and the held-out speakers of part G. It leaves out these parts:

- **The other languages (parts E and F).** The model fits one language. To compare languages,
  export each language with `from_challenges(path, language=<WCS number>)` in Python, and fit
  one spec for each. The web app does not calculate the similarity of two partitions (NMI) or
  the hue-rotation null.
- **The clusters of centroids and foci across 110 languages (part F).** The notebook uses a
  Bayesian bootstrap over the speakers, not this model.
- **The foci.** The dataset has no best examples. Thus the port cannot compare the safest chip
  with the focus of each term.
- **The per-speaker predictive check and the lineup (part C and part D).** The web app has no
  plot for them. Calculate them from the draws of a `simulate` score (step 7.2): for each term,
  count the chips of each simulated speaker, and compare the sd over the speakers with the data.
- **The prior predictive check of part B.** The default `w_sd: 3` is the scale that the
  notebook chose after that check.
- **The glyph maps, the boundary widths and the disc charts of part D.** **Fields** and
  **Colour swatches** show the same quantities in a different form.
- **The adaptive choice of the next chip (part G).** The port does not update one speaker from
  some answers, and it does not calculate the expected information gain.

**Next steps:**

- Fit the pooled model and the three feature sets, and compare them with `elpd_loo` and the
  held-out metrics.
- Fit a language with more terms, for example one of the five languages of part E.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the audit recipe.
