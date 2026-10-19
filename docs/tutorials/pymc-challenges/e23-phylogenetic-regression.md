# E23 primate phylogenetic regression

This tutorial does the work of the pymc-challenges notebook `E23_phylogenetic_regression.py` in
the web app. You use the model `sci.phylo_regression@1`. You do not write PyMC code.

**You need:**

- A running platform (`make dev`) and a project that you can upload datasets to. This tutorial
  uses the project `biology`. If your project has a different name, change the project part
  of each `dataset://` and `model://` reference, and `metadata.project` in the spec.
- A local copy of pymc-challenges. The export command reads two files from its `data/`
  directory.
- About 20 minutes. One fit samples for about 10 seconds on a laptop. The run takes longer
  because the sandbox must start and load the dataset.

## 1. The question

Do primates that live in larger groups have larger brains for their body size? This is the
"social brain" hypothesis. The usual test is a regression across species: log brain volume on
log body mass and log group size.

That test is not correct, because species are not independent data points. Related species
share most of their history. If a large brain and a large group came once in a common
ancestor, the regression counts that one event once for each descendant species. The notebook
puts the shared history into the model. The residuals of two species are correlated by the
length of the branches that they share on the phylogeny.

The notebook asks these questions:

1. **Are the species independent?** (Notebook sections 3 and 4.)
2. **Does the slope of group size stay clear of zero** when the model includes the tree?
   (Sections 4 to 6.)
3. **Which model of trait evolution agrees best with the data**: no tree, Brownian motion,
   Pagel's lambda or Ornstein-Uhlenbeck? (Sections 5 and 7.)

The notebook ends with these decisions:

- Do not use the i.i.d. regression. Its slope of group size (0.12) comes from some deep splits
  of the tree that it counts once for each species.
- Use Pagel's lambda. It has the best leave-one-out score, and lambda is about 0.95.
- The data do not show a clear effect of group size. With the tree, the slope is about 0.03
  and its interval touches zero.
- Brownian motion and Ornstein-Uhlenbeck agree with the data equally well. Report the Brownian
  rate, not the half-life of the pull to an optimum.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The tree and the brain volume of each species, observed and predicted from its relatives (`sci.phylogeny`) | Section 2, the tree next to the traits |
| The slopes, `sigma` and `lam` (**Parameters**, `core.posterior_forest`) | Sections 3, 4 and 6, the summaries and the slope plot |
| `elpd_loo` from the leave-one-out density of each species given the others (**Metrics** card) | Section 7, the conditional LOO |
| The four kinds fitted and compared in one run (recipe `sci_phylo_signal`) | Sections 3 to 7 |
| A prediction of the brain volume of 35 species without a value, from their relatives (**Score**) | Section 9 (a different gap, see section 10) |

See [Differences from the notebook](#10-differences-from-the-notebook) for the parts that the
port leaves out.

## 2. Get the data

The export command reads the trait table `primates301.csv` and the tree `primates301_tree.rda`.
The tree is an R object (`ape::phylo`). The command reads it without R. It keeps the 186
species that have a body mass and a group size. It prunes the tree to these species. Run the
command from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e23 \
    --challenges <path to pymc-challenges>/data \
    --out primates301.nc --spec primates301.yaml
```

The command writes:

```text
wrote primates301.nc (variables parent, branch_length, is_tip, tip_label, brain, body, group_size on node=452, species=186)
upload it as dataset biology/primates301 and fit sci.phylo_regression
wrote the suggested spec to primates301.yaml
```

The file holds one dataset with two dims. The traits are on `species`. The tree is on `node`:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `species` | 186 | categorical, policy `fixed` | The species, for example `Allenopithecus_nigroviridis`. Each label is a tip label of the tree |
| `node` | 452 | integer, policy `fixed` | Each node of the tree: 186 tips and 266 internal nodes |
| `brain` | `species` | float64, target, unit `cc`, missing values allowed | The brain volume. 151 species have a value. 35 species have no value |
| `body` | `species` | float64, covariate, unit `g` | The body mass |
| `group_size` | `species` | float64, covariate | The mean size of the social group |
| `parent` | `node` | int64, constant | The position of the parent node on `node`. The root has -1 |
| `branch_length` | `node` | float64, constant, unit `Myr` | The length of the branch above the node, in millions of years |
| `is_tip` | `node` | bool, constant | True for a tip (a species) |
| `tip_label` | `node` | str, constant | The species label of each tip. Internal nodes have an empty label |

The 151 species with a brain volume are the complete cases of the notebook (sections 3 to 8).
The tree is ultrametric: each tip is 73.0 million years from the root. The pruned tree keeps
the nodes with one child, so the shared branch lengths do not change. The attribute
`mapi_schema` gives each variable its role, so you do not map columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes 40 species on a random tree, with traits from the model's own process
    and known parameters. 4 species have no brain volume. Use it to try the steps. The numbers
    in this tutorial are for the primates.

    ```bash
    uv run python -m mapi_plugin_science.export e23 --synthetic --out primates_synthetic.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `primates301` in **Name**. Type a description, for example "Brain, body mass and group
   size of 186 primates, with the 10kTrees phylogeny".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `primates301.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Optional: click **Preview structure**. The panel shows **Structure of the file**. Make sure
   that it shows three variables on `species` and four variables on `node`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `brain` has the role
`target`, and the four tree variables have the role `constant`. **Missingness** shows the 35
species without a brain volume. The model accepts them. It does not use them in the
likelihood, and it predicts their brain volume from their relatives.

The tree is part of the same plain dataset, on the dim `node`. You do not need the tree fields
("Put this file in a node of a tree (optional)") or the HDF5 path.

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `primates301`. The toast "Dataset set" shows.
5. Type `phylo` in the search field. Under **Models**, select **Traits of related species**
   (`sci.phylo_regression@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `primate-social-brain` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: primate-social-brain, project: biology}
data:
  dataset: dataset://biology/primates301@latest
  bind:
    vars: {trait: brain}
model:
  use: sci.phylo_regression@^1
  settings: {kind: lambda, predictors: [body, group_size]}
  fit: {mode: train_only, seed: 2017, tune: 1000, draws: 1000, chains: 4, engine: nutpie}
evaluate: {metrics: [elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.phylogeny]
```

The model calls its target `trait`. The `bind` block connects it to `brain`. The predictors,
the tree variables and the dims have the same names in the model and in the dataset.

The spec has no split. The notebook compares the models with leave-one-out, and the model
calculates the leave-one-out density of each species itself. Thus `mode: train_only` fits once
on all species. The suggested spec of the export (`primates301.yaml`) is the same spec without
`mode: train_only`. Without it, **Validate** shows a warning, because the default mode
`evaluate_then_refit` needs a split.

The settings for this question:

| Setting | Value | What it means here |
|---|---|---|
| `kind` | `lambda` | Pagel's lambda: the residual is a tree part plus a part for each species. `iid` ignores the tree, `brownian` uses only the tree, and `ou` is Ornstein-Uhlenbeck on the distance through the tree |
| `predictors` | `[body, group_size]` | The variables on `species` that explain the brain volume. These are the names in the dataset |
| `log_target`, `log_predictors` | true, true (default) | The model uses log brain, log body mass and log group size, as the notebook does |
| `standardise` | true (default) | The model centres and scales each variable on the 151 species with a brain volume. A slope is then in sd of log brain for each sd of the predictor, as in the notebook |
| `intercept_sd`, `slope_sd` | 1, 0.5 (default) | The normal priors of the notebook |
| `sigma_rate` | 1 (default) | The exponential prior of `sigma` of the notebook |
| `lambda_alpha`, `lambda_beta` | 1, 1 (default) | A flat Beta prior on `lam` |
| `ou_parameterisation` | `rate` (default) | Only for `ou`: the prior is on the Brownian rate `eta2 rho`, the fix of notebook section 5 |
| `rate_sd`, `rho_sd`, `eta2_sd` | 1, 5, 5 (default) | Only for `ou`: the half-normal priors of the notebook |
| `jitter` | 1e-6 (default) | Only for `ou`: a small value on the diagonal of the covariance |

The fit block has the sampler settings of the notebook: nutpie, 4 chains of 1000 draws and the
seed 2017.

!!! note "The other three kinds"
    To compare the kinds as the notebook does, save three more specs. Change `kind` and the
    name, for example `kind: iid` with the name `primate-brain-iid`. For `kind: ou`, also add
    `target_accept: 0.95` to the fit block, as the notebook does. The recipe of step 8 fits
    all four kinds in one run.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the progress of the sampler. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://biology/primate-social-brain@v1**. Click it.

After the sampler, the run calculates two items for each draw. These are the prediction of each
species from its relatives and the leave-one-out density of each species with a brain volume.
Then it makes the plots and the explorer views.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer. Use
the sections **Parameters** and **Diagnostics**. The model has no output on a grid and no time
dim, so **Fields** and the time sections do not apply to it.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer, or the plot **Sampler convergence** on the **Plots**
tab. In this fit, the largest R-hat is 1.003, the smallest bulk ESS is 1,849, the smallest tail
ESS is 2,347, and there are no divergences. The fit is good.

### 6.2 Are the species independent?

**Parameters** (or the plot **Posterior forest**) shows `intercept`, `slope` for `body` and
`group_size`, `sigma` and `lam`. In this fit:

| Parameter | Mean | sd |
|---|---|---|
| `slope[body]` | 0.767 | 0.031 |
| `slope[group_size]` | 0.027 | 0.020 |
| `lam` | 0.954 | 0.018 |
| `sigma` | 0.276 | 0.022 |
| `intercept` | -0.116 | 0.119 |

`lam` near 1 shows that the residual of brain size is almost all phylogenetic. It is not 1,
so a small part of each species is its own. If the species were independent, `lam` would be
near 0. The interval of `slope[group_size]` includes zero.

The intercept is now the expected log brain size at the root of the tree, 73 million years
ago. Its sd is about 7 times the sd of the `iid` fit, because a small number of deep lineages
give it.

### 6.3 The tree next to the traits

Open the plot **Phylogeny with tip estimates** (`sci.phylogeny`) on the **Plots** tab.

- The left panel is the tree. The x axis is the distance from the root in millions of years.
  The colour of each tip is the predicted brain volume.
- The right panel shows, for each species, the prediction from its relatives (mean and 90%
  interval) and the observed value (an open circle).
- The plot has 186 tips, so it does not show the labels. Move the pointer over a tip to read
  the name of the species.

Look for these features, which the notebook also shows:

- Brain size comes in blocks of related species: the small lemurs, mouse lemurs and marmosets,
  and the large apes and baboons.
- The 35 species without a brain volume have a prediction but no circle. Their predictions
  follow their relatives.
- For a fitted species, the prediction leaves that species out. Thus a circle far from its
  interval is a species that its relatives do not predict well.

### 6.4 Which model agrees best with the data?

The **Metrics** card of the **Overview** tab shows `elpd_loo`. In this fit, it is 129.9 (se
8.7, p_loo 4.5). A larger value is better.

The model calculates `elpd_loo` from the density of each species given all the other species
(notebook section 7, "Right"). It does not use the marginal density of each species, which
puts the models in the opposite order. Fit the other three kinds (see the note in step 4) and compare
their `elpd_loo`. These are the results of the four kinds with the spec of step 4:

| `kind` | `elpd_loo` (se) | `slope[group_size]` | `slope[body]` | Notes |
|---|---|---|---|---|
| `iid` | 14.9 (8.9) | 0.123 ± 0.023 | 0.893 ± 0.023 | |
| `brownian` | 91.3 (24.1) | -0.013 ± 0.020 | 0.701 ± 0.037 | 2 species with a Pareto k above 0.7 |
| `lambda` | **129.9** (8.7) | 0.027 ± 0.020 | 0.767 ± 0.031 | `lam` 0.954 |
| `ou` | 90.9 (22.6) | -0.008 ± 0.021 | 0.738 ± 0.042 | `rate` 0.184, 2 species with a Pareto k above 0.7 |

### 6.5 The numbers of the notebook and of this port

| Quantity | Notebook | This port |
|---|---|---|
| `lambda`: `slope[body]` | 0.766 | 0.767 |
| `lambda`: `slope[group_size]` | 0.026 | 0.027 |
| `lambda`: `lam` | 0.954 (89% interval 0.92 to 0.98) | 0.954 ± 0.018 |
| `lambda`: `sigma` | 0.277 | 0.276 |
| `lambda`: `intercept` | -0.121 | -0.116 |
| `iid`: `slope[group_size]` | 0.123 | 0.123 |
| `brownian`: `slope[group_size]` | -0.013 | -0.013 |
| `ou`: `rate` | 0.185 | 0.184 |
| `elpd_loo`: `lambda` | 130 | 129.9 |
| `elpd_loo`: `brownian`, `ou` | about 90 | 91.3, 90.9 |
| `elpd_loo`: `iid` | last, more than 100 below `lambda` | 14.9 |

Your numbers can be slightly different if the sampler or its version is different.

### 6.6 Make the decisions

Use these outputs to make the decisions of the notebook:

1. **Are the species independent?** No. `lam` is 0.954, and `elpd_loo` of `iid` is about 115
   below `lambda`. Do not use the `iid` slope of 0.12.
2. **Which covariance?** Keep `lambda`. It has the best `elpd_loo` by about 39. `brownian` and
   `ou` are almost equal. Their large `p_loo` (about 13) and the two species with a high Pareto
   k come from sister species that predict each other almost exactly. The species part of
   `lambda` (1 - `lam`, about 0.05) stops this overconfidence.
3. **Does the social-brain slope survive?** No. In each tree model, `slope[group_size]` is
   within 0.03 of zero, and its interval includes zero. The 90% HDI of the `lambda` slope is
   -0.007 to 0.060 (step 7.1).
4. **Brownian motion or Ornstein-Uhlenbeck?** You cannot tell them apart. Report the Brownian
   rate `rate` of the `ou` fit (0.184), not `rho` or the half-life.
5. **Do the intervals always become wider?** No. Compare the sds of the `iid` and `lambda` fits.
   The sd of the intercept is about 7 times larger. The sd of `slope[body]` is about 35% larger.
   The sd of `slope[group_size]` becomes about 13% smaller, because close relatives give
   contrasts that are almost independent.

## 7. Score the model

The fitted version keeps the tree, the predictors and the observed brain volumes. A score needs
a dataset with the dim `species` and the predictors `body` and `group_size`. Send the dataset
that you fitted. A score request names the outputs with the names of the model: `trait` and
`slope`. The result uses the names of your dataset, so `trait` comes back as `brain`.

### 7.1 Predict: each species from its relatives

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**. Select `primates301`.
4. Open **Advanced**. Set **Seed (optional)** to `2017`. Type `trait, slope` in **Variables
   (optional)**.
5. Click **Predict**.

The request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`) is:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biology/primate-social-brain@latest",
  "mode": "predict",
  "data": "dataset://biology/primates301@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["trait", "slope"]
  },
  "seed": 2017
}
```

The result has `brain(species)` and `slope(predictor)`:

- `slope[group_size]` has the mean 0.027 and the 90% HDI -0.007 to 0.060. `slope[body]` has
  0.767 and 0.712 to 0.816.
- For a species with a brain volume, `brain` is the leave-one-out prediction from all other
  species. The expected value is the conditional median in cc.
- For the 35 species without a brain volume, `brain` is the prediction from all fitted species.
  For example, *Allocebus trichotis* gets 2.3 cc and *Macaca thibetana* gets 110.1 cc.

Set **Quantity** to **Predictive** to add the conditional noise. Then the 90% intervals hold
the observed brain volume of 137 of the 151 species (91%). The notebook pair of section 7 is a
good example. *Cercocebus torquatus* has 106.0 cc, but its relatives predict 88.8 cc (75.0 to
102.8). Its subspecies *C. t. atys* has 94.7 cc and gets 111.1 cc (93.7 to 128.1). The
subspecies is the heavier of the two but has the smaller brain.

!!! warning "Do not select No new data"
    Select **Use a dataset**. With **No new data**, the request has no `data`, and the run
    stops with the message "the score request has no data".

### 7.2 Simulate: brain volumes without the relatives

`simulate` draws new brain volumes for the species from the fitted parameters and the tree. It
does not use the observed brain volumes.

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**. Select `primates301`.
3. Open **Advanced**. Set **Quantity** to **Predictive**. Set **Draws** to `20`.
4. Type `trait` in **Variables (optional)**.
5. Click **Simulate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biology/primate-social-brain@latest",
  "mode": "simulate",
  "data": "dataset://biology/primates301@latest",
  "output": {
    "kind": "predictive",
    "summary": ["mean", "hdi_90"],
    "draws": 20,
    "vars": ["trait"]
  },
  "seed": 2017
}
```

Each draw is one possible set of brain volumes of the 186 species, correlated by the tree. The
intervals are much wider than the predictions of step 7.1. For example, *Cercocebus torquatus*
gets 27.8 to 101.5 cc. Use the draws to make a predictive check of your own, for example the
correlation of the residuals of each species and its closest relative (notebook section 3).

- **Evaluate** needs a dataset with brain volumes. It compares them with the predictions from
  the relatives.
- To change the predictors of a fitted species, upload a dataset with the new `body` or
  `group_size` values. The prediction then moves by the change in the regression mean of the
  species.
- A species that the fit did not see stops the score with `UNSEEN_LEVELS`. The dim `species`
  is fixed, because each species must be a tip of the fitted tree.
- The model has no `forecast` mode.

If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same requests.

## 8. Run a workflow recipe

The recipe `sci_phylo_signal` does sections 3 to 7 of the notebook in one run. It fits
`sci.phylo_regression` four times (`iid`, `brownian`, `lambda` and `ou`) and ranks the fits by
`elpd_loo`. It gives the stacking weights and keeps the best fit. Then it reads the slope of
one predictor (`focus`) and tests if its HDI excludes zero. Only if it does, the recipe does a
predictive check of the winner. The report has the tables and the plot of the tree.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Trait regression across species, with and without the tree** and click
   **Run on my data**.
3. In **1. Your data**, select `primates301` in **Dataset**.
4. Optional: open **Fine-tune (optional: the defaults are correct for most data)**. The
   defaults are correct for this dataset: **Predictors** is `["body", "group_size"]`, **Focus**
   is `group_size`, **Hdi prob** is 0.94, and each fit has 4 chains of 1000 draws and the seed
   2017.
5. Click **Run this recipe**.

The recipe binds the target `brain` to `trait` automatically, because the dataset has one
target. The tree variables have the names of the model, so you do not give `bind`.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. A local run with the defaults took 3.3 minutes on a laptop and gave these results:

| Node | Result |
|---|---|
| `iid`, `brownian`, `lambda`, `ou` | `elpd_loo` 15.0, 90.5, 129.9 and 91.2 |
| `weights` | `lambda` 1.00. The other fits get no weight |
| `best` | "Pagel's lambda has the best elpd_loo (130) of 4 candidate(s)." |
| `slope` | `slope[group_size]` 0.027, 94% range -0.010 to 0.066, `p_positive` 0.91 |
| `clear` | The else arm: the range of the slope includes zero. `ppc` does not run |

**Decisions** shows the branch `clear` and its message: "The range of the slope includes zero.
The data do not show a clear effect of the predictor when the model includes the tree." This is
the decision of the notebook. **Outputs** shows the comparison, the weights, the slopes, the
plot of the tree and the report. The model version link opens the Model Explorer of the winner.

!!! note
    The recipe fits the model four times. The `ou` fit is the slowest. The fits use the
    default engine of the model (`pymc`) and the default `target_accept`, not nutpie and 0.95.
    Thus the numbers are slightly different from those of step 6.

The recipe `bayes_workflow_audit` also applies to this model. It adds a prior predictive
check, a convergence gate and the governance checks `sensitivity.prior` and
`recovery.simulated`. Type `sci.phylo_regression@^1` in **Model**. In **Fine-tune**, type
`{"kind": "lambda", "predictors": ["body", "group_size"]}` in **Settings** and `{}` in **Prior
predictive**: this model keeps no prior draws, so the prior plot of the audit cannot render. The recipes
`sci_spatial_ladder`, `sci_network_ladder`, `sci_calibrate_then_design` and
`sci_image_model_check` are for other science models.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/biology
    mapi datasets upload primates301.nc --name primates301
    mapi check primate-social-brain.yaml --remote @     # the server's static validation
    mapi fit primate-social-brain.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model primate-social-brain -s params -s diagnostics

    cat > relatives.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://biology/primate-social-brain@latest
    mode: predict
    data: dataset://biology/primates301@latest
    output: {kind: expected, summary: [mean, hdi_90], vars: [trait, slope]}
    seed: 2017
    EOF
    mapi score relatives.yaml --remote @ --model <model id> --version 1 -o relatives.zarr.zip

    mapi workflows init sci_phylo_signal --name primate-brains   # writes primate-brains.yaml
    mapi workflows apply primate-brains.yaml -m "E23 phylogenetic signal"
    mapi workflows run primate-brains -i data=dataset://biology/primates301@latest \
      -p 'predictors=["body","group_size"]' -p focus=group_size --watch
    RUN=$(mapi --json workflows runs primate-brains -n 1 | jq -r '.[0].run_id')
    mapi workflows node "$RUN" compare   # the four fits, the best elpd_loo first
    mapi workflows node "$RUN" slope     # the slopes of the winner and the focus value
    ```

    `primate-social-brain.yaml` is the spec of step 4. The CLI has no `models` command. Take
    the model id from the run (`mapi runs get <run>`) or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/biology")
    version = api.datasets.upload("primates301.nc", dataset="primates301")
    print(version.ref, version.status.value)

    with open("primate-social-brain.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "primate-social-brain")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://biology/primate-social-brain@latest",
            "mode": "predict",
            "data": "dataset://biology/primates301@latest",
            "output": {
                "kind": "expected",
                "summary": ["mean", "hdi_90"],
                "vars": ["trait", "slope"],
            },
            "seed": 2017,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["slope"].to_pandas())  # mean and 90% HDI of each slope
    print(summary["brain"].sel(species="Allocebus_trichotis").values)
    ```

    To run the recipe, write `primate-brains.yaml` with `mapi workflows init sci_phylo_signal`.
    Then save it and start it:

    ```python
    from pathlib import Path

    workflow, revision = api.workflows.upsert(Path("primate-brains.yaml"))
    run = api.workflows.run(
        workflow.id,
        inputs={"data": "dataset://biology/primates301@latest"},
        params={"predictors": ["body", "group_size"], "focus": "group_size"},
        wait=True,
    )
    nodes = {n.node_id: n for n in run.nodes}
    print(nodes["compare"].result["ranking"])  # ['lambda', 'ou', 'brownian', 'iid']
    focus = nodes["slope"].result["focus"]
    print(focus["mean"], focus["hdi_low"], focus["hdi_high"], focus["p_positive"])
    print(nodes["clear"].result["arm"])  # "else": the range of the slope includes zero
    ```

## 10. Differences from the notebook

The port keeps the four regressions of sections 3 to 6 (`iid`, `brownian`, `lambda` and `ou`
with the rate parameterisation) and the conditional leave-one-out of section 7. It leaves out
these parts:

- **The R reader and the Newick parser (section 1).** The export command does this work. In
  Python, `mapi_plugin_science.phylo_data.read_tree` reads an R file or a Newick file, and
  `mapi_plugin_science.phylo.to_newick` writes a tree as Newick.
- **The check of the nearest relative (sections 3 and 7).** The web app has no plot for the
  correlation of the residuals of each species and its closest relative. Calculate it from the
  draws of a `simulate` score (step 7.2).
- **The ridge of the `ou` posterior (section 5).** Fit `kind: ou` with
  `ou_parameterisation: variance` to see it. The web app has no pair plot of `eta2` and `rho`
  on the log scale. The default `rate` removes the ridge.
- **The two wrong LOO calculations (section 7).** The port stores only the correct conditional
  density. The difference of `elpd_loo` for each species is not in the web app.
- **Whitening and the sparse tree precision (section 8).** The model uses a dense covariance
  for each draw. This is fast for 151 species, but slow for some thousand species.
- **The imputation of group size on the tree (section 9).** A predictor must have no missing
  values. Thus the dataset has the 186 species with a body mass and a group size. The 31 species
  of the notebook with a brain volume but no group size are not in it. The port predicts the
  other gap: the brain volume of the 35 species without one.
- **The tree in tree order next to the traits (section 2).** The plot `sci.phylogeny` shows the
  tree and the predicted brain volume. It does not show the three traits side by side.

**Next steps:**

- Fit `kind: ou` with `ou_parameterisation: variance` and compare the ESS of `rho` with the
  default parameterisation.
- Try the exercises of the notebook ("Try it yourself") in Python with
  `mapi_plugin_science.phylo`, for example a tree that is not ultrametric.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the recipe, for example to add a fifth
  kind or a different `focus`.
