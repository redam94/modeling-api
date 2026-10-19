# E19 protein signalling graphical model

This tutorial does the work of the pymc-challenges notebook `E19_graphical_models.py` in the
web app. You use the model `sci.graphical_model@1`. You do not write PyMC code.

**You need:**

- The platform (start it with `make dev`) and a project that you can upload datasets to. This
  tutorial uses the project `biology`. If your project has a different name, change the project
  part of each `dataset://` and `model://` reference, and `metadata.project` in the spec.
- A local copy of pymc-challenges. The export command reads two files from its `data/`
  directory.
- About 15 minutes. The fit takes about 1 minute of sandbox time.

## 1. The question

Sachs et al. (2005) measured the phosphorylation of 11 proteins and lipids in thousands of
single T cells: Raf, Mek, PLCγ, PIP2, PIP3, Erk, Akt, PKA, PKC, p38 and Jnk. The notebook uses
only the **853 cells with the plain CD3/CD28 stimulus**. These cells got no inhibitor or
activator, so they come from one system.

The notebook asks these questions:

1. **Which pairs of molecules are directly linked?** Two molecules can have a correlation
   because one acts on the other, or because both follow a third molecule. A direct link is a
   partial correlation that is not zero: the correlation that stays when all the other
   molecules are held constant. (Notebook sections 2 and 3.)
2. **How sure are you of each link?** A posterior partial correlation is continuous. Thus an
   edge needs a threshold. The notebook uses `|partial correlation| > 0.1`, and the edge
   probability is `P(|ρ| > 0.1 | data)`. (Section 3.)
3. **When does a sparse prior help?** With 100 cells instead of 853, the dense model is not
   sure of most pairs. (Sections 4 and 5.)
4. **How many of the 20 edges of the literature network do the data show?** (Section 7.)

The notebook ends with these decisions:

- With 853 cells, six edges are certain, and all six are in the consensus network. Two more
  pairs (p38–Jnk and PLC–PIP3) have a probability above 0.5. About 12 of the 20 consensus
  edges do not show as linear partial correlations in unperturbed cells.
- With many cells, the dense model and the sparse model give the same graph. With few cells,
  the sparse (horseshoe) model gives a better decision. It declares few edges, and a larger
  experiment confirms almost all of them.
- Do not pool the nine conditions of the experiment. Each inhibitor changes the system.
- An undirected graph does not give the direction of a link. That is the subject of E20.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The partial correlation of each pair, with mean, sd and draws (**Fields**, output `pcor`) | Section 2, the partial correlation matrix, and section 3, the posterior of ρ |
| The edge probability `P(|ρ| > 0.1)` of each pair (**Fields**, output `edge_prob`) | Section 3, the edge probabilities |
| The graph of the links, next to the consensus network (`net.graph`) | Section 7, "The graphs" |
| The learned values `edges`, `undecided_pairs` and `pcor_sd_mean` | The counts in sections 3 and 4 |
| The neighbourhood model with the AND, OR or ρ rule (`kind: neighbourhood`) | Section 5, model B |
| A fit on 100 cells (a spec with a split) | Section 4, the small experiment |
| New simulated cells (**Score**, mode Simulate) | Section 3, the posterior predictive check |

See [Differences from the notebook](#10-differences-from-the-notebook) for the parts that the
port leaves out.

## 2. Get the data

The export command reads `sachs2005.txt` and `sachs2005_consensus.txt`. It keeps the 853 cells
with only the CD3/CD28 stimulus. It reads the 20 directed edges of the consensus file as an
undirected network. Run the command from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e19 \
    --challenges <path to pymc-challenges>/data \
    --out sachs_cd3cd28.nc --spec sachs_cd3cd28.yaml
```

The command writes:

```text
wrote sachs_cd3cd28.nc (variables level, consensus on cell=853, protein=11, protein_=11)
upload it as dataset biology/sachs_cd3cd28 and fit sci.graphical_model
wrote the suggested spec to sachs_cd3cd28.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `cell` | 853 | integer, policy `new_levels`, strategy `pooled` | The cells. The cells are exchangeable, so you can score new cells |
| `protein` | 11 | categorical, policy `fixed` | The molecules, `raf` to `jnk` |
| `protein_` | 11 | categorical, policy `fixed` | The molecules again: the second molecule of a pair |
| `level` | `cell, protein` | float64, target, units `log_intensity` | The fluorescence intensity of the molecule in the cell, as `log(x + 10)` |
| `consensus` | `protein, protein_` | int64, constant | The consensus network of Sachs et al.: 1 is an edge. It has 20 edges |

The dataset has no missing values. The attribute `mapi_schema` gives each variable its role,
so you do not map columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes 300 cells of 6 variables (`g1` to `g6`) from a Gaussian graphical
    model with 4 known edges. The true network is in `consensus`. Use it to try the steps. The
    numbers in this tutorial are for the Sachs cells.

    ```bash
    uv run python -m mapi_plugin_science.export e19 --synthetic --out protein_network.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `sachs_cd3cd28` in **Name**. Type a description, for example "Flow cytometry of 853
   T cells with the CD3/CD28 stimulus (Sachs et al. 2005)".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `sachs_cd3cd28.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Optional: click **Preview structure**. The panel shows **Structure of the file**. Make sure
   that it shows `level` on `cell` and `protein`, and `consensus` on `protein` and `protein_`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `level` has the role
`target`, and `consensus` has the role `constant`.

The file is one plain dataset. You do not need the tree fields ("Put this file in a node of a
tree (optional)") or the HDF5 path.

### 3.1 Explore the data

Look at the data before you build the spec.

1. On the version page, click **Explore this version**. The Data Explorer opens. The
   sections under *Pictures of the data* come first.
2. Click **Network**.

- The network shows `consensus`, the graph of the 11 proteins from the literature, as a
  matrix and as a node-link graph.
- **Correlations** uses the 11 proteins as columns and the 853 cells as rows. Compare the
  strong correlations with the edges of the network.
- **Group comparison** and the time sections are in **Not for this data**. The cells are not
  groups, and the data has no time dim.

Refer to [Scientific data](../../guides/data-explorer.md#scientific-data).

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `sachs_cd3cd28`. The toast "Dataset set" shows.
5. Type `graphical` in the search field. Under **Models**, select `sci.graphical_model`. The
   toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `sachs-cd3cd28-ggm` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: sachs-cd3cd28-ggm, project: biology}
data:
  dataset: dataset://biology/sachs_cd3cd28@latest
  bind:
    dims: {var: protein, var_: protein_}
    vars: {value: level, reference: consensus}
  split: {kind: leave_group_out, dim: cell, n: 170, seed: 2005}
model:
  use: sci.graphical_model@^1
  settings: {kind: dense, threshold: 0.1, lkj_eta: 2.0, reference: true}
  fit:
    mode: evaluate_then_refit
    seed: 2005
    tune: 1000
    draws: 1000
    chains: 4
    engine: nutpie
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, crps, coverage_90, elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, net.graph, sci.field_map]
```

The model calls the variables `var` and `var_`, the target `value` and the known network
`reference`. The `bind` block connects them to the names in the dataset. The results come back
with the names of the dataset: `protein`, `protein_` and `level`.

The split holds out 170 random cells (20%). `evaluate_then_refit` fits on the other 683 cells
and scores the held-out cells. Then it fits again on all 853 cells, as the notebook does. The
suggested spec of the export (`sachs_cd3cd28.yaml`) is the same spec without
`core.posterior_forest` in the plots.

The settings for this question:

| Setting | Value | What it means here |
|---|---|---|
| `kind` | `dense` | One multivariate normal over the 11 molecules, with an LKJ prior on the correlations. This is model A of the notebook. It also predicts and simulates new cells |
| `threshold` | 0.1 | An edge is a pair with `|partial correlation| > 0.1`. A partial correlation of 0.1 explains 1% of the remaining variance. This is the δ of the notebook |
| `lkj_eta` | 2.0 | The LKJ shape. A value above 1 prefers weaker correlations. The notebook uses 2 |
| `reference` | true | The dataset has a known network (`consensus`). The fit keeps it, and `net.graph` draws it below the learned edges |
| `standardise` | true (default) | The model centres and scales each molecule with the fit data. Partial correlations do not change with the scale |
| `sd_rate`, `mean_sd` | 1, 1 (default) | The Exponential rate of the sd prior and the sd of the mean prior, on the standardised scale |
| `log_likelihood` | `auto` (default) | `joint` for `dense`: the fit keeps `log p(x)` for each cell for `elpd_loo` |
| `expected_neighbours`, `slab_alpha`, `slab_beta`, `edge_rule` | 2, 2, 2, `and` (default) | Only for `kind: neighbourhood` (section 6.6) |

!!! note "Settings that do not agree"
    **Validate** does a check of each setting by itself. It does not compare two settings, and
    it does not compare a setting with the split. Two examples show `valid` but stop the run:

    - `kind: neighbourhood` with `log_likelihood: joint` stops with `SETTINGS_INVALID`.
    - `kind: neighbourhood` with a split stops with `MODE_NOT_SUPPORTED`, because the
      neighbourhood model cannot score held-out cells.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the progress of the sampler. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://biology/sachs-cd3cd28-ggm@v1**. Click it.

The run does two fits: one on the 683 training cells for the metrics of the held-out cells,
and one on all 853 cells. On a laptop, the two fits with nutpie took about 40 seconds.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer, or the plot **Sampler convergence** on the
**Plots** tab. In this fit, all 583 parameters look converged: the largest R-hat is 1.004, the
smallest bulk effective sample size is about 2,700, and there are no divergent
transitions. The notebook reports the same values.

The diagnostics also show one warning, `PARETO_K_HIGH`: one cell has a high Pareto k in
`elpd_loo`. This cell is far from the other cells. The notebook finds the same problem: the
measured values have long right tails, and the model is Gaussian.

The plot **Posterior forest** shows the raw parameters of the model: the standardised means
`mu` and the elements of the Cholesky factor `chol`. These numbers are not easy to read. Use
**Fields** for the partial correlations.

### 6.2 The partial correlations and the edge probabilities

Open **Fields** in the Model Explorer. The text says "The model has 2 outputs on a grid." Each
grid has the 11 molecules as rows (`protein`) and as columns (`protein_`).

1. In **Output**, select `pcor`. The cells show the partial correlation of each pair. The
   colour scale is diverging: one colour for positive values and another colour for negative
   values. The diagonal is 1.
2. Select **Mean** in **Statistic**. Look for the cells far from zero.
3. Select **Std. dev.**. In this fit, the sd of a partial correlation is about 0.03
   everywhere (`pcor_sd_mean` is 0.033). With 853 cells, the data fix each value well.
4. In **Output**, select `edge_prob`. Select **Mean** in **Statistic**. Each cell now shows
   the edge probability `P(|ρ| > 0.1)`.
5. Move the pointer over a cell to read its values.

The plot **Field map** on the **Plots** tab shows `pcor` as two panels (mean and sd).

In this fit, the pairs with the highest edge probabilities are:

| Pair | Edge probability | Mean partial correlation | In the consensus |
|---|---|---|---|
| Erk–Akt | 1.00 | +0.85 | yes |
| Raf–Mek | 1.00 | +0.70 | yes |
| PKC–p38 | 1.00 | +0.68 | yes |
| PIP2–PIP3 | 1.00 | +0.37 | yes |
| PKC–Jnk | 1.00 | −0.28 | yes |
| Akt–PKA | 1.00 | +0.25 | yes |
| p38–Jnk | 0.86 | +0.14 | no |
| PLC–PIP3 | 0.61 | +0.11 | yes |
| Mek–Jnk | 0.33 | −0.09 | no |
| Raf–Jnk | 0.18 | +0.07 | no |
| Mek–Erk | 0.12 | +0.06 | yes |

Read the table as the notebook does:

- Six pairs are certain, and all six are consensus edges.
- p38–Jnk is a real partial correlation (0.14) that the consensus does not have. Both molecules
  depend on PKC, in opposite directions. This masks the link in the marginal correlation.
- Mek–Erk is the kinase step of the MAPK cascade. Its partial correlation is not zero, but it
  is smaller than the threshold. Thus its edge probability is only 0.12.

### 6.3 The graph

The plot **Network of pcor** (`net.graph`) has two panels:

- **Graph** draws the 11 molecules and the 8 links with `|pcor| > 0.1`. Thicker lines are
  stronger links. Blue lines are positive and red lines are negative. Move the pointer to the
  middle of a link to see the share of draws above the threshold.
- **Matrix** shows the mean partial correlation of every pair.

Dotted lines and open squares show the links of `reference`, that is the consensus network.
Compare them with the learned links. Most of the consensus links from PKA and PKC have no
learned link. These two kinases are hubs of the literature network. The experiment changed
them in the other conditions. In unperturbed cells, their effects are weak or do not show.

!!! note
    The graph uses the posterior **mean** of `pcor`, not the edge probability. For the
    decision, use `edge_prob` in **Fields** or the learned value `edges`.

### 6.4 The learned values and the held-out cells

The **Overview** tab shows the learned values of the fit:

| Learned value | This fit | Meaning |
|---|---|---|
| `edges` | 8 | The pairs with an edge probability above 0.5 |
| `undecided_pairs` | 2 | The pairs with an edge probability between 0.2 and 0.8 |
| `pcor_sd_mean` | 0.033 | The mean posterior sd of a partial correlation |

The **Metrics** card of the **Overview** tab scores the 170 held-out cells (1,870 values):

| Metric | This fit |
|---|---|
| `rmse` | 0.524 |
| `crps` | 0.284 |
| `coverage_90` | 0.917 |
| `elpd_loo` (all 853 cells) | −5827.8 |

The coverage of the 90% intervals is near 0.9. The model gets the spread of each molecule
right, but it does not get the shape: the measured values are more peaked and have longer
tails than a Gaussian.

### 6.5 The numbers of the notebook and of this port

| Quantity | Notebook | This port |
|---|---|---|
| Pairs with P = 1.00 | 6, all in the consensus | 6, the same pairs |
| P(p38–Jnk) | 0.86 | 0.86 |
| P(PLC–PIP3) | 0.60 | 0.61 |
| P(Mek–Erk) | 0.12 | 0.12 |
| Edges with P > 0.5 | 8, 7 in the consensus | 8, 7 in the consensus |
| Undecided pairs (0.2 < P < 0.8) | 2 | 2 |
| Mean posterior sd of ρ | 0.033 | 0.033 |
| Largest R-hat, divergences | 1.004, 0 | 1.004, 0 |
| Average precision against the consensus | 0.63 | 0.634 |
| Neighbourhood model: undecided pairs | 1 | 1 |
| Neighbourhood model: divergences, largest R-hat | 2, 1.012 | 1, 1.012 |
| 100 cells, dense: mean sd of ρ | 0.094 | 0.095 |
| 100 cells, dense: undecided pairs | 43 | 45 |

The 100-cell rows use a random set of 100 cells. The notebook and the port use different sets,
so these numbers can be slightly different. Your numbers can also be slightly different if the
sampler or its version is different.

### 6.6 Make the decisions

Use these outputs to make the decisions of the notebook:

1. **Which edges.** Take the pairs with an edge probability above 0.5 (`edges`). With 853
   cells, the graph has 8 edges: the six certain edges, p38–Jnk and PLC–PIP3.
2. **How sure.** Look at `undecided_pairs`. With 853 cells, only 2 pairs are not sure. The
   threshold, not the data, decides these two pairs. To see the effect of the threshold, fit
   again with `threshold: 0.05` and `threshold: 0.2`.
3. **Dense or sparse.** Fit the neighbourhood model (model B of the notebook) with this spec.
   It has no split, because this model cannot score held-out cells:

    ```yaml
    apiVersion: mapi/v1
    kind: ModelSpec
    metadata: {name: sachs-cd3cd28-neighbourhood, project: biology}
    data:
      dataset: dataset://biology/sachs_cd3cd28@latest
      bind:
        dims: {var: protein, var_: protein_}
        vars: {value: level, reference: consensus}
    model:
      use: sci.graphical_model@^1
      settings: {kind: neighbourhood, threshold: 0.1, expected_neighbours: 2.0, edge_rule: and,
                 reference: true}
      fit:
        mode: train_only
        seed: 2005
        tune: 1000
        draws: 1000
        chains: 4
        target_accept: 0.99
        engine: nutpie
        keep: {log_likelihood: true}
    evaluate: {metrics: [elpd_loo]}
    outputs:
      plots: [core.convergence, core.posterior_forest, net.graph, sci.field_map]
    ```

    In this fit, the neighbourhood model finds 6 edges, all in the consensus: the six certain
    edges of the dense model (Akt–PKA has 0.92). p38–Jnk has 0.27 and PLC–PIP3 has 0.13. The
    sampler had 1 divergent transition and a largest R-hat of 1.012. The notebook reports 2
    divergences and the same R-hat. With 853 cells, the two models agree on the strong edges.
    Do not compare the two `elpd_loo` values: they are not the same log-likelihood. To compare
    them, set `log_likelihood: conditional` on both specs.

4. **The small experiment.** Fit the dense model on 100 cells. Change the split and the fit
   mode of the spec of step 4:

    ```yaml
      split: {kind: leave_group_out, dim: cell, n: 753, seed: 2005}
    ```

    and `mode: train_only`. The fit then uses the 100 cells that the split keeps. In this fit,
    the mean sd of ρ goes from 0.033 to 0.095, and `undecided_pairs` goes from 2 to 45. The
    dense prior says "maybe" to almost every pair. This is the case where the notebook prefers
    the sparse model.
5. **The consensus.** Do not use the consensus network as the truth. It is a summary of the
   literature for the whole pathway, with links that show only when the experiment changes the
   pathway. About 12 of its 20 edges do not show in these cells, with any of the models.

## 7. Score the model

The fitted version uses all 853 cells. The dense model predicts and simulates cells. A score
needs a dataset with the dims `cell` and `protein`. Send the dataset that you fitted.

!!! caution "Use the names of the model in Variables"
    Type the names of the model in **Variables (optional)**: `pcor`, `edge_prob` and `value`.
    The result uses the names of the dataset: `value` comes back as `level`. If you type
    `level`, the score stops with `REF_UNRESOLVED`.

### 7.1 Predict: the graph and the mean of each molecule

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**. Select `sachs_cd3cd28`.
4. Open **Advanced**. Set **Seed (optional)** to `19`. Type `pcor, edge_prob` in **Variables
   (optional)**.
5. Click **Predict**.

The request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`) is:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biology/sachs-cd3cd28-ggm@latest",
  "mode": "predict",
  "data": "dataset://biology/sachs_cd3cd28@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["pcor", "edge_prob"]
  },
  "seed": 19
}
```

The result has `pcor(protein, protein_)` and `edge_prob(protein, protein_)`, with the mean and
the 90% interval. The mean of `edge_prob` is the edge probability of section 6.2. With no
**Variables**, the result also has `level(cell, protein)`: the mean of each molecule.

### 7.2 Simulate: new cells

`simulate` gives new cells from the model. Use them for a posterior predictive check, as in
section 3 of the notebook.

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**. Select `sachs_cd3cd28`.
3. Open **Advanced**. Set **Quantity** to **Predictive**. Set **Draws** to `20`.
4. Type `value` in **Variables (optional)**.
5. Click **Simulate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biology/sachs-cd3cd28-ggm@latest",
  "mode": "simulate",
  "data": "dataset://biology/sachs_cd3cd28@latest",
  "output": {
    "kind": "predictive",
    "summary": ["mean", "hdi_90"],
    "draws": 20,
    "vars": ["value"]
  },
  "seed": 19
}
```

The result has `level(cell, protein)` with 20 draws. Each draw is a new set of 853 cells.
Compare the histogram of each molecule with the data. Raf agrees well. PKA, Erk and Akt do not
agree: the data are more peaked and have a longer right tail than any draw.

- **Evaluate** needs a dataset with the measured values. Set **Quantity** to **Predictive**.
  With **Expected**, the 90% interval holds only the uncertainty of the mean, and
  `coverage_90` is near 0.05.
- A score of the neighbourhood model (section 6.6) returns only `pcor` and `edge_prob`.
  Simulate, Evaluate and **Predictive** stop with `MODE_NOT_SUPPORTED`.
- The model has no `forecast` mode.

If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same requests.

## 8. Run a workflow recipe

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does the checks of a Bayesian
workflow on any model with draws. It fits the model with prior predictive draws. A convergence
gate needs the largest R-hat below `rhat_max` (1.01) and the smallest bulk ESS at or above
`min_ess` (400). Only then does it make the posterior predictive check and run the governance
checks `sensitivity.prior` and `recovery.simulated`. Then it writes a report with the plots. The prior predictive shows the
point of section 3 of the notebook: a mild LKJ prior on the correlations is a prior for a dense
graph.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Bayesian workflow audit** and click **Run on my data**.
3. In **1. Your data**, select `sachs_cd3cd28` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.graphical_model@^1` in **Model**.
5. Open **Fine-tune**. In **Settings**, type
   `{"kind": "dense", "threshold": 0.1, "lkj_eta": 2.0, "reference": true}`. In **Bind**, type
   `{"dims": {"var": "protein", "var_": "protein_"}, "vars": {"value": "level", "reference": "consensus"}}`.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Decisions** shows the convergence gate and the value that it read. **Outputs**
shows the plots and the report. The model version link opens the Model Explorer.

!!! caution "Give the recipe the binding"
    Without **Bind**, the recipe cannot bind the dims `protein` and `protein_` to the dims
    `var` and `var_` of the model, and the fit stops.

!!! note
    The checks after the gate refit the model. `sensitivity.prior` fits it one time for each
    value in **Prior scales** (two by default), and `recovery.simulated` fits it **N sims**
    times (20 by default). Thus the audit takes much longer than one fit.

The recipe `sci_network_ladder` is for the latent-space network model of E21
(`sci.network_latent_space`). It does not apply to this model. The recipes
`sci_spatial_ladder`, `sci_phylo_signal`, `sci_calibrate_then_design` and
`sci_image_model_check` are for other science models.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/biology
    mapi datasets upload sachs_cd3cd28.nc --name sachs_cd3cd28
    mapi check sachs-cd3cd28-ggm.yaml --remote @     # the server's static validation
    mapi fit sachs-cd3cd28-ggm.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model sachs-cd3cd28-ggm -s params -s diagnostics

    cat > new-cells.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://biology/sachs-cd3cd28-ggm@latest
    mode: simulate
    data: dataset://biology/sachs_cd3cd28@latest
    output: {kind: predictive, summary: [mean, hdi_90], draws: 20, vars: [value]}
    seed: 19
    EOF
    mapi score new-cells.yaml --remote @ --model <model id> --version 1 -o new-cells.zarr.zip

    mapi workflows init bayes_workflow_audit --name sachs-audit
    mapi workflows apply sachs-audit.yaml -m "E19 audit"
    mapi workflows run sachs-audit -i data=dataset://biology/sachs_cd3cd28@latest \
      -p model=sci.graphical_model@^1 \
      -p 'settings={"kind": "dense", "threshold": 0.1, "lkj_eta": 2.0, "reference": true}' \
      -p 'bind={"dims": {"var": "protein", "var_": "protein_"}, "vars": {"value": "level", "reference": "consensus"}}' \
      --watch
    ```

    `sachs-cd3cd28-ggm.yaml` is the spec of step 4. The CLI has no `models` command. Take the
    model id from the run (`mapi runs get <run>`) or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/biology")
    version = api.datasets.upload("sachs_cd3cd28.nc", dataset="sachs_cd3cd28")
    print(version.ref, version.status.value)

    with open("sachs-cd3cd28-ggm.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "sachs-cd3cd28-ggm")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://biology/sachs-cd3cd28-ggm@latest",
            "mode": "predict",
            "data": "dataset://biology/sachs_cd3cd28@latest",
            "output": {
                "kind": "expected",
                "summary": ["mean", "hdi_90"],
                "vars": ["pcor", "edge_prob"],
            },
            "seed": 19,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    prob = summary["edge_prob"].sel(stat="mean").to_pandas()
    pcor = summary["pcor"].sel(stat="mean").to_pandas()
    names = list(prob.index)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if prob.loc[a, b] > 0.5:
                print(f"{a}-{b}: P={prob.loc[a, b]:.2f} pcor={pcor.loc[a, b]:+.2f}")
    ```

    To run the audit recipe:

    ```python
    workflow = api.workflows.create(template="bayes_workflow_audit")
    run = api.workflows.run(
        workflow.id,
        inputs={"data": "dataset://biology/sachs_cd3cd28@latest"},
        params={
            "model": "sci.graphical_model@^1",
            "settings": {"kind": "dense", "threshold": 0.1, "lkj_eta": 2.0, "reference": True},
            "bind": {"dims": {"var": "protein", "var_": "protein_"},
                     "vars": {"value": "level", "reference": "consensus"}},
        },
        wait=True,
    )
    ```

## 10. Differences from the notebook

The port keeps the dense model A (section 3), the neighbourhood model B with the AND, OR and ρ
rules (section 5), and the edge probabilities with a stated threshold. It leaves out these
parts:

- **The marginal correlations (section 2).** The model gives only the partial correlations.
  Calculate the marginal correlations from the dataset in Python.
- **The naive horseshoe on the precision matrix (section 4).** The notebook shows that this
  model is broken: most of its prior is not positive definite. The port does not include it.
- **The graphical lasso (section 6).** The port has no frequentist reference model.
- **The precision-recall curves and the replicate study (section 7).** The web app does not
  calculate the average precision. Calculate it from `edge_prob` and `consensus` in Python.
  The study on eight sets of 100 cells needs eight datasets or eight splits.
- **The pooled conditions (section 8).** The export keeps only the CD3/CD28 cells. To get all
  7,466 cells, use `from_challenges(path, observational=False)` from
  `mapi_plugin_science.ggm_data` in Python. Do not fit the model to them: each condition is a
  different system.
- **The direction of the links.** An undirected graph cannot give it. See E20 in the
  notebooks.
- **The Student-t likelihood and the Cholesky parametrisation ("Try it yourself").** The port
  has only the Gaussian likelihood.

**Next steps:**

- Fit the neighbourhood model with `edge_rule: or` and `edge_rule: rho`, and compare the edges.
- Fit the dense model on 100 cells, then the neighbourhood model on the same 100 cells. Export
  the 100 cells as a separate dataset for the neighbourhood model, because it takes no split.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the audit recipe.
