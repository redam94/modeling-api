# E61 Turing patterns

This tutorial does part A of the pymc-challenges notebook
`E61_turing_patterns_kinetic_proofreading.py` in the web app. You use the model
`sci.turing_pattern@1`. You do not write PyMC code.

**You need:**

- The platform (start it with `make dev`) and a project that you can upload datasets to. This
  tutorial uses the project `biology`. If your project has a different name, change the project
  part of each `dataset://` and `model://` reference.
- The modeling-api repository. Part A of the notebook has no data file: the notebook makes its
  images with a simulation. The export command does the same simulation, so you do not need a
  copy of pymc-challenges.
- About 20 minutes. The fit takes about 5 minutes of sandbox time.

## 1. The question

Two chemicals that react and diffuse at different rates can change a uniform tissue into spots
or stripes. This is a Turing pattern. The notebook uses the Schnakenberg system: an activator
`u` and a substrate `v`, with the supply rates `a` and `b`, the diffusion ratio `d` and the
reaction scale `γ` (`g` in the platform).

The notebook asks what images can tell you about these rates:

1. A settled pattern gives one length: its wavelength. Many sets of rates give the same
   wavelength. (Notebook section 3.)
2. Four early images, taken while the pattern starts to grow, have an exact likelihood. Do they
   identify all the rates? (Notebook section 4.)
3. Do the rates satisfy the Turing conditions, and how large is the region of supply rates that
   makes a pattern? (Notebook section 5, the Schnakenberg part.)

The decision at the end of part A is this: **a steady state gives a length scale, and the
approach to it gives the rates.** To measure the kinetics, record the start of the pattern
(or disturb a settled pattern and record its recovery). Do not fit the settled pattern. With
four early images, the 89% intervals of `d` and `γ` are about ±12% and ±9%, and they contain
the true values.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The posterior of `s`, `rho`, `d`, `g`, `s0`, `sigma`, `a` and `b` (**Parameters**) | Section 4, the mode-growth fit and its table |
| The dispersion relation `growth(wavenumber)` with bands (**Plots**, `sci.profile_bands`) | Section 4, the left panel ("Dispersion relation"), blue band |
| `wavelength` and `max_growth` of the mode that grows fastest | Section 4, the printed "fastest growth rate 90%" line |
| `p_turing`, the probability that the Turing conditions hold | Section 5, the Turing conditions for the fitted system |
| The map `turing_space(b_supply, a_supply)` (**Fields**) | Section 5, the left panel ("Turing space in (a, b)") |
| The observed images next to predicted images (**Plots**, `sci.ppc_image_grid`, `sci.image_residuals`) | Not in the notebook. A check of the linear likelihood |
| New patterns from the nonlinear solver (**Score**, **Simulate**) | Section 2, the spectral solver |

Notebook part B (kinetic proofreading) and the Nodal/Lefty calculation of section 5 are not in
the port. See [Differences from the notebook](#10-differences-from-the-notebook).

## 2. Get the data

The export command does the simulation of notebook section 4 and writes a NetCDF file that the
platform can read. Run it from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e61 \
    --challenges <path to pymc-challenges>/data \
    --out e61_turing.nc --spec e61_turing.yaml
```

The command writes:

```text
wrote e61_turing.nc (variables activator on t=4, y=128, x=128)
upload it as dataset biology/e61_turing_early_images and fit sci.turing_pattern
wrote the suggested spec to e61_turing.yaml
```

The case reads no file, so `--challenges` is optional. Without it, the command writes the same
file.

The simulation is the notebook's simulation:

- A 128 × 128 periodic square of side 100. The activator diffuses at a rate of 1 in these
  units.
- The truth: `a = 0.1`, `b = 0.9` (thus `s = 1` and `rho = 0.1`), `d = 20` and `g = 1`.
- The uniform state plus white noise of sd 0.001 (seed 1). The spectral solver uses a step of
  0.005.
- Images at t = 4, 8, 12 and 16, with white camera noise of sd 0.0005 (seed 613).

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `t` | 4 | float, `extend` | The time since the start: 4, 8, 12 and 16 |
| `y`, `x` | 128, 128 | integer, `fixed` | The pixel rows and columns |
| `activator` | `t, y, x` | float64, target | The activator concentration in each pixel |

The images are near the uniform state 1. Their sd is 0.0006 at t = 4 and 0.006 at t = 16. The
largest deviation from the mean is 2.3%, so the linear likelihood is correct. The attributes of
the file keep the truth (`truth_s`, `truth_rho`, `truth_d`, `truth_g`), `box_length` 100 and
`camera_noise_sd` 0.0005. The attribute `mapi_schema` gives each variable its role, so you do
not map columns.

!!! tip "A smaller dataset"
    `--synthetic` writes a 32 × 32 version of the same simulation (seed 0). A fit on it is
    much faster. Use it to try the steps. The numbers in this tutorial are for the
    128 × 128 images.

    ```bash
    uv run python -m mapi_plugin_science.export e61 --synthetic --out turing_synthetic.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `e61_turing_early_images` in **Name**. Type a description, for example "Four early
   images of a Schnakenberg pattern, 128 × 128, t = 4 to 16".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `e61_turing.nc`.
6. Click **Preview structure**. The panel shows **Structure of the file**. Make sure that it
   shows `activator` on `t`, `y` and `x`.
7. Optional: type a message in **Version message (optional)**.
8. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `activator` has the role
`target`. **Missingness** must show no missing values. The model needs a value in each pixel
of a square raster.

The file is one plain dataset. You do not need the tree fields ("Put this file in a node of a
tree (optional)") or the HDF5 path.

### 3.1 Explore the data

Look at the data before you build the spec.

1. On the version page, click **Explore this version**. The Data Explorer opens. The
   sections under *Pictures of the data* come first.
2. Click **Images and fields**.

- The viewer shows `activator`, images of 128 × 128 cells. The frame slider goes through the
  4 times of `t`. Look at the pattern of spots in each frame.
- **Over time** shows one series: the mean of all cells at each time. The explorer does not
  make a series for each cell.
- The time patterns are in **Not for this data**: `t` is an ordered sequence, not time.
- **Data quality** does not give the warning `SHORT_HISTORY`.

Refer to [Scientific data](../../guides/data-explorer.md#scientific-data).

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `e61_turing_early_images`. The toast "Dataset set" shows.
5. Type `turing` in the search field. Under **Models**, select **Turing patterns from early
   images** (`sci.turing_pattern@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `e61-turing-mode-growth` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: e61-turing-mode-growth
  project: biology
data:
  dataset: dataset://biology/e61_turing_early_images@latest
model:
  use: sci.turing_pattern@^1
  settings:
    box_length: 100.0
    k2_max: 1.5
    max_extension: 2
    d_median: 10.0
    d_log_sd: 1.0
    initial_noise_median: 0.001
    camera_noise_median: 0.001
    field_draws: 40
    simulate_draws: 4
    settle_time: 300.0
    precondition: true
  fit:
    mode: train_only
    seed: 61
    tune: 1500
    draws: 1000
    chains: 4
    target_accept: 0.95
    engine: pymc
    keep:
      log_likelihood: true
evaluate:
  metrics: [elpd_loo]
outputs:
  plots:
    - core.convergence
    - core.posterior_forest
    - sci.field_map
    - sci.ppc_image_grid
    - sci.profile_bands
    - sci.image_residuals
```

The settings are the defaults of the model and the values of the notebook. The `--spec` file
of the export has the short form of the same spec. This table tells what each setting means
for the question:

| Setting | Value | Meaning for this question |
|---|---|---|
| `box_length` | 100 | The side of the square, in the length unit of the model. It sets the wavenumbers of the Fourier modes. Use the side of your images in units of `sqrt(D_u / k)` |
| `k2_max` | 1.5 | Use only the modes with `k²` less than 1.5. Faster modes decay below the camera noise before the first image. 581 modes in 138 groups remain |
| `max_extension` | 2 | A prediction can go to 2 image times after t = 16, that is t = 20 and 24. The linear theory is correct only while the pattern is small |
| `d_median`, `d_log_sd` | 10, 1 | The prior of the diffusion ratio: `LogNormal(log 10, 1)`, as in the notebook |
| `initial_noise_median`, `camera_noise_median` | 0.001, 0.001 | The prior medians of `s0` and `sigma` (log-normal, log sd 1.5) |
| `field_draws` | 40 | The posterior draws that each score uses for `pattern`, `growth` and `turing_space` |
| `simulate_draws` | 4 | The nonlinear runs for each `simulate` score. Each run is a full PDE solution |
| `settle_time` | 300 | The time of the `settled` pattern in `simulate`. The notebook also uses 300 |
| `precondition` | true | Find the mode first, then let NUTS sample near it with a Laplace preconditioner. The model does not change |

- `fit.mode: train_only` fits on all four images. The model needs all the images, so the spec
  has no split. Without this line, the validator shows the warning `SPLIT_INVALID` ("there is
  no holdout for the evaluation").
- `tune: 1500` and `target_accept: 0.95` are the sampler settings of the notebook.
- `keep.log_likelihood: true` keeps the log-likelihood of each Fourier mode. The modes are
  independent, so `elpd_loo` over the modes is valid.

!!! note "The plots"
    The model declares `core.convergence`, `core.posterior_forest`, `sci.field_map`,
    `sci.ppc_image_grid` and `sci.profile_bands`. The fit makes only the plots in
    `outputs.plots`. Thus the spec names them, and it adds `sci.image_residuals`. The
    **Plots** section of the **Insert** panel adds a plot for you.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows "finding the mode" first, then the NUTS progress. The
   **Events** card shows the stages of the run and, at the end, the diagnostics. The **Logs**
   card stays empty when the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://biology/e61-turing-mode-growth@v1**. Click it.

The fit samples 6 numbers. The model calculates the FFT of the images once. Then it groups the
modes by their integer `k²`, so each gradient needs only 2 × 2 algebra for each of the 138
groups. Before NUTS starts, the model finds the posterior mode from 4 starts. After the fit,
the run calculates the plots and the explorer views from 40 posterior draws.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer (group "Can you trust it?").

Expect four warnings: `RHAT` (largest R-hat 1.016), `ESS_BULK` (smallest bulk ESS 166),
`ESS_TAIL` (152) and `DIVERGENCES` (5). The slow parameters are `rho` and `g`. The notebook
also found that this posterior is curved, not only correlated. Its fit had 0 divergences and a
bulk ESS above 500. The posterior means and intervals agree with the notebook (step 6.5).
Thus the warnings tell you to get more draws, not that the model is wrong. To get a larger
ESS, set `draws: 2000` and fit again.

The diagnostic `NONLINEAR_REGIME` must not show. It shows when the images are more than 10%
from their mean. These images are at most 2.3% from their mean.

### 6.2 What the early images identify

Open **Parameters** (group "What it learned"). The table has `s`, `rho`, `d`, `g`, `s0`,
`sigma`, and the derived `a`, `b`, `wavelength`, `max_growth` and `p_turing`.

- `d` is 20.9, with a 90% interval from 17.8 to 24.8. The truth is 20.
- `g` is 0.99 (0.91 to 1.07). The truth is 1.
- `b` is 0.94 (0.84 to 1.03) and `a` is 0.096 (0.060 to 0.130). The truth is 0.9 and 0.1.
  `a` is the parameter with the widest relative interval, as in the notebook.
- `s0` and `sigma` are at their true values, 0.001 and 0.0005.

On the model version page, open **Plots** and select **Profiles with bands**. The title is
"Profiles of growth". The line is the median growth rate of each wavenumber, with 50% and 90%
bands. This is the blue band of the notebook's left panel:

- Near the peak, the band is very narrow. The largest growth rate is 0.262 to 0.264 (truth
  0.263), at the wavenumber 0.485.
- At small wavenumbers, the band is wide (at `k = 0`, from −0.20 to −0.08). These modes decay
  below the camera noise in the first images, so the data say little about them.

### 6.3 Is it Turing?

- `p_turing` is 1 in every draw. Thus the posterior probability that the Turing conditions
  hold is 1.00. At `s = 1` and `rho = 0.1`, the smallest diffusion ratio for a pattern is about
  8.6. All draws of `d` are far above it.
- `wavelength` is 12.95 in almost all draws. This is `2π / 0.485`. The model finds the peak on
  a grid of 600 values of `k²`, so the interval of `wavelength` has almost no width.

Open **Fields** (group "What it learned"). The text says "The model has 2 outputs on a grid."
Select `turing_space` in **Output**.

- **Statistic** **Mean** shows P(Turing) for each pair of supply rates. `a_supply` is on the
  horizontal axis and `b_supply` on the vertical axis. The region where the mean is near 1 is
  the Turing wedge of the notebook's left panel in section 5, for the posterior `d` (about 21).
- The point `a = 0.1`, `b = 0.9` of the simulation is inside the wedge.
- **Std. dev.** is larger than 0 only along the edge of the wedge. There, the uncertainty of
  `d` moves the edge.

Move the pointer over the map to read the values of one cell.

### 6.4 Do the predicted images look like the data?

The notebook does not do this check. It is a good check of the linear likelihood.

In **Fields**, select `pattern` in **Output**. Use the slider `t` to show each image time.
**Mean** shows the activator field given the fitted images. **Draws** and **Play the draws**
show possible fields.

On the model version page, open **Plots**:

| Plot | What to look for |
|---|---|
| Sampler convergence (`core.convergence`) | R-hat and ESS of the 6 parameters. `rho` and `g` have the lowest ESS |
| Posterior forest (`core.posterior_forest`) | The intervals of step 6.2. `a` and `rho` have the widest relative intervals |
| Field map (`sci.field_map`) | The title is "Field map of pattern". The mean and the sd of `pattern` for each time. Use the slider to show a different `t` |
| Predictive image grid (`sci.ppc_image_grid`) | The title is "Predictive images of pattern". The observed image is the first panel, then 5 predicted images on one colour scale. The observed image must look like one more draw |
| Profiles with bands (`sci.profile_bands`) | The dispersion relation of step 6.2 |
| Image residuals (`sci.image_residuals`) | The title is "Residuals of pattern". The normalised residuals must look like noise, with an sd near 1 and about 5% outside ±2. Here the sd is 1.02 |

The predicted images have the same sd as the data at each time (0.0006 at t = 4 and 0.006 at
t = 16).

### 6.5 The decision and the numbers

Make the notebook's decision with these outputs:

1. Look at the intervals of `d`, `g`, `a` and `b` in **Parameters**. All four contain the
   truth, and `d` and `g` are known to about ±12% and ±9%. Thus four early images identify the
   kinetics.
2. Look at the dispersion relation. The data fix the peak and the band of unstable
   wavenumbers. A settled pattern gives only the position of the peak (notebook section 3).
3. Thus, to measure the rates of a reaction-diffusion system, record the start of the pattern
   while it is small. Use the `NONLINEAR_REGIME` diagnostic to make sure that the images are
   early enough.

We fitted the spec above on the exported images (seed 61, 4 chains of 1000 draws). The table
gives the 89% intervals, as the notebook does:

| Quantity | Truth | Notebook | Platform |
|---|---|---|---|
| `d` | 20 | 17.7 to 25.2 | 17.8 to 24.7 (mean 20.9) |
| `g` (γ) | 1 | 0.90 to 1.08 | 0.91 to 1.07 (mean 0.99) |
| `b` | 0.9 | 0.84 to 1.04 | 0.85 to 1.03 (mean 0.94) |
| `a` | 0.1 | 0.054 to 0.133 | 0.060 to 0.129 (mean 0.096) |
| `s0` | 0.001 | at the true value | 0.00092 to 0.00101 |
| `sigma` | 0.0005 | at the true value | 0.00049 to 0.00051 |
| Largest growth rate, 90% | 0.263 | 0.262 to 0.264 | 0.262 to 0.264 |
| Its wavenumber | 0.485 | the truth | 0.485 (`wavelength` 12.95) |
| P(Turing conditions hold) | 1 | not given | 1.00 |
| `elpd_loo` (581 modes) | | not given | 28,473.5 |
| Sampler | | 0 divergences, R-hat up to 1.013, bulk ESS above 500, 93 s | 5 divergences, R-hat up to 1.016, bulk ESS 166, about 5 minutes |

Your numbers can be a little different if your sandbox has a different version of PyMC.

## 7. Score the model

The scores ignore the values of any dataset that you send. The predictions are given the
fitted images.

**Predict the summaries:**

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**.
   Then select `e61_turing_early_images`, the dataset of the fit.
4. Open **Advanced**. Type `p_turing, wavelength, growth` in **Variables (optional)**. Set
   **Seed (optional)** to `61`.
5. Click **Predict**.

The **Results** region shows `p_turing` (mean 1.0), `wavelength` (12.94, `hdi_90` 12.95 to
12.95) and the dispersion relation `growth` with its `hdi_90` for each wavenumber.

This is the request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`):

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biology/e61-turing-mode-growth@latest",
  "mode": "predict",
  "data": "dataset://biology/e61_turing_early_images@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "draws": 0,
    "units": "original",
    "names": "dataset",
    "vars": ["p_turing", "wavelength", "growth"]
  },
  "seed": 61
}
```

**Simulate new patterns:**

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**.
   Then select `e61_turing_early_images`, the dataset of the fit.
3. In **Advanced**, type `settled, p_turing, wavelength` in **Variables (optional)**.
4. Click **Simulate**.

The model takes 4 posterior draws (`simulate_draws`). For each draw, it grows a new pattern
from new random noise with the nonlinear spectral solver to t = 300. `settled` is the pattern
at t = 300: spots, with values from about 0.2 to 3.2. The 4 runs took about 30 s in our
sandbox. The request is the same as above, with `"mode": "simulate"` and
`"vars": ["settled", "p_turing", "wavelength"]`. The output `pattern` of a simulate score is a
new pattern at the times of the dataset, with camera noise.

- `"kind": "predictive"` adds camera noise with sd `sigma` to `pattern` in `predict`.
- To predict later images, send a dataset with new `t` values, for example 20 and 24. The model
  accepts at most 2 times after t = 16 (`max_extension`). Its predictions are correct only
  while the pattern is small.
- `forecast` and `counterfactual` give `MODE_NOT_SUPPORTED`.

!!! tip "Try it on an endpoint"
    If you promote the version to an endpoint, open **Integrations**, then the endpoint, then
    the **Schema and Try it** tab. Paste the same request there, without the `model` field,
    and click **Score**.

## 8. Run a workflow recipe

The recipe `sci_image_model_check` (**Image model check**) is for models that predict an
image. It fits the model, makes a table of the parameters with R-hat and ESS, renders the
residual maps (`sci.image_residuals`) and the predictive image grid (`sci.ppc_image_grid`),
does a predictive check, and writes a report.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Image model check** and click **Run on my data**.
3. In **1. Your data**, select `e61_turing_early_images` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.turing_pattern@^1` in **Model**.
5. Open **Fine-tune (optional: the defaults are correct for most data)**. In **Settings**,
   type `{"box_length": 100, "k2_max": 1.5}`. Set **Tune** to `1500` and **Seed** to `61`.
   Type `t` in **Frame**.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Outputs** shows the two plots and the report. The model version link opens the
Model Explorer. The predictive check compares the sd of the observed images with the sd of the
predicted images. If it fails, the workflow continues.

!!! note
    The dataset names are the names of the model (`t`, `y`, `x` and `activator`). Thus you do
    not need **Bind**. If your images have other names, give them in **Bind**, for example
    `{"dims": {"t": "minutes"}, "vars": {"activator": "intensity"}}`.

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) also applies to this model. It
adds a prior predictive check, a convergence gate, a prior sensitivity check and a recovery
check on simulated data. The gate needs the largest R-hat below **Rhat max** (default 1.01)
and the smallest bulk ESS at or above **Min ess** (default 400). In step 6.1, R-hat is 1.016
and the bulk ESS is 166, so the default gate stops the audit. In **Fine-tune**, increase
**Draws** and **Tune**, or set **Rhat max** to `1.02` and **Min ess** to `150`. The checks
after the gate fit the model again: two times for the prior sensitivity check and **N sims**
times (20 by default) for the recovery check.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    # get the data and the suggested spec
    uv run python -m mapi_plugin_science.export e61 \
        --out e61_turing.nc --spec e61_turing.yaml

    # upload, check and fit on the platform (e61-turing-mode-growth.yaml is the spec of step 4)
    mapi use <tenant>/biology
    mapi datasets upload e61_turing.nc --name e61_turing_early_images
    mapi check e61-turing-mode-growth.yaml --remote @
    mapi fit e61-turing-mode-growth.yaml --remote @
    mapi explore model e61-turing-mode-growth -s params -s diagnostics -s fields

    # the summaries of step 7
    cat > turing-score.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://biology/e61-turing-mode-growth@latest
    mode: predict
    data: dataset://biology/e61_turing_early_images@latest
    output: {kind: expected, summary: [mean, hdi_90], vars: [p_turing, wavelength, growth]}
    seed: 61
    EOF
    mapi score turing-score.yaml --remote @ --model <model id> --version 1 -o turing.zarr.zip

    # the recipe
    mapi workflows init sci_image_model_check --name turing-check
    mapi workflows apply turing-check.yaml
    mapi workflows run turing-check \
        -i data=dataset://biology/e61_turing_early_images@latest \
        -p model=sci.turing_pattern@^1 \
        -p settings='{"box_length": 100, "k2_max": 1.5}' \
        -p tune=1500 -p seed=61 -p frame=t --watch
    ```

    The CLI has no `models` command. Take the model id from the run (`mapi runs get <run>`)
    or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client()  # MAPI_URL, the `mapi login` sign-in (or MAPI_TOKEN) and MAPI_PROJECT

    version = api.datasets.upload("e61_turing.nc", dataset="e61_turing_early_images")
    print(version.ref, version.status.value)

    with open("e61-turing-mode-growth.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])

    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "e61-turing-mode-growth")
    mv = api.models.version(model.id, model.latest_version)
    print(mv.ref, mv.metrics)

    result = api.models.score(
        model.id,
        mv.version,
        {
            "model": mv.ref,
            "mode": "predict",
            "data": "dataset://biology/e61_turing_early_images@latest",
            "output": {
                "kind": "expected",
                "summary": ["mean", "hdi_90"],
                "vars": ["p_turing", "wavelength", "growth"],
            },
            "seed": 61,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["p_turing"].values, summary["wavelength"].values)
    print(summary["growth"].sel(stat="mean").max().values)  # the largest growth rate
    ```

    To compare with the notebook, load its printed estimates with
    `mapi_plugin_science.turing_data.challenges_reference()`.

The CLI and the client send the same spec and the same score request as the web app.

## 10. Differences from the notebook

The port keeps the kinetics, the priors and the exact mode-growth likelihood of notebook
section 4. It leaves out these parts:

- **The settled-pattern posterior (section 3).** The notebook calibrates the wavelength of
  settled patterns against the linear theory and uses importance sampling from the prior. The
  platform does not fit a settled pattern. `simulate` gives settled patterns, but the platform
  does not measure their wavelength. The grey bands and clouds of the section 4 figure are not
  in the platform.
- **The pair plots of section 4.** The Model Explorer does not show draws of two parameters
  against each other. Download the bundle and use the posterior draws, or add `core.pair` to
  `outputs.plots`.
- **Nodal and Lefty (section 5).** The probability that the measured Nodal/Lefty diffusion
  ratios exceed the critical ratio is a different model. It is not in the port.
- **The fraction of Turing space against `d` (section 5, middle panel).** The platform gives
  the map `turing_space` for the posterior `d` only.
- **Part B, kinetic proofreading (sections 6 to 10).** Not in the port.
- **The sampler.** The platform finds the mode first and samples with a Laplace
  preconditioner. The notebook samples the parameters directly. To do the same, set
  `precondition: false`. Then you need a long warm-up, for example `tune: 1500`.

**Next steps:**

- Do "Try it yourself" item 2 of the notebook: fit only two images, or add an image at t = 30.
  At t = 30 the pattern is large. Look for the `NONLINEAR_REGIME` diagnostic and for
  normalised residuals with an sd far from 1.
- Fit your own time-lapse images. Measure `box_length` in units of `sqrt(D_u / k)`, or fit
  several values and compare `elpd_loo`.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the image recipe.
