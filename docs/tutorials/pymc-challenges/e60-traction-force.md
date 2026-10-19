# E60 traction force microscopy

This tutorial does the real-data part of the pymc-challenges notebook
`E60_traction_force_microscopy.py` in the web app. You use the model `sci.traction_force@1`.
You do not write PyMC code.

**You need:**

- A running platform (`make dev`) and a project that you can upload datasets to. This tutorial
  uses the project `biophysics`. If your project has a different name, change the project part
  of each `dataset://` and `model://` reference.
- A local copy of pymc-challenges. The export command reads one file from its `data/`
  directory.
- Some time for the fit. The model estimates three minutes of sandbox time. On a busy laptop,
  our fit took 45 minutes.

## 1. The question

A cell on a soft gel pulls on the gel surface. Beads in the gel move. Traction force
microscopy (TFM) calculates the forces of the cell from the bead movement. This is an inverse
problem: many force maps agree with the measured movement. Thus each TFM method adds an
assumption. In a Bayesian model, that assumption is the prior.

The notebook asks two questions about a colony of 7 cells on a 49 kPa gel (pyTFM example
data, Bauer et al. 2021):

1. Which prior on the traction field do the data support? (Notebook section 7.)
2. How hard does the colony pull, with an honest range? The notebook reports the strain
   energy (fJ) and the contractility (µN), and compares them with pyTFM. (Notebook section 8.)

The decision at the end of the notebook is this: use the Matérn prior, and report the strain
energy and the contractility with a range that includes the uncertainty of the gel stiffness.
The range from the displacement noise alone is less than 1%. A 15% error in Young's modulus
moves the forces by about −21% to +25%.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The posterior of the noise sd and of the three Matérn hyperparameters (**Parameters**) | Section 7, the ML-II fits and the full-Bayes fit of section 8 |
| The `NOISE_SPECTRUM_MISMATCH` diagnostic (**Diagnostics**) | Section 7, the right panel of the spectrum plot |
| Traction maps with mean, sd and draws (**Fields**, `sci.vector_field`) | Section 8, the map of the posterior mean and the map of mean ÷ sd |
| `strain_energy` and `contractility` with a 90% range (**Score**) | Section 8, the table of numbers |
| A second fit with `young_cv: 0.15` | Section 8, the row "Matérn posterior, E ± 15%" |

Notebook sections 1 to 6 use a simulated cell with known forces. The port does not repeat
them. See [Differences from the notebook](#10-differences-from-the-notebook).

## 2. Get the data

The export command reads `pytfm_colony_ko04.npz` and writes a NetCDF file that the platform
can read. Run it from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e60 \
    --challenges <path to pymc-challenges>/data \
    --out pytfm_colony.nc --spec pytfm_colony.yaml
```

The command writes:

```text
wrote pytfm_colony.nc (variables displacement, force_mask on component=2, row=189, col=193)
upload it as dataset biophysics/pytfm_colony_ko04 and fit sci.traction_force
wrote the suggested spec to pytfm_colony.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `component` | 2 | categorical, `x` and `y` | The direction of the displacement |
| `row`, `col` | 189, 193 | integer | The PIV grid. `y` goes along `row`, `x` along `col` |
| `displacement` | `component, row, col` | float64, µm, target | The PIV displacement of the gel surface |
| `force_mask` | `row, col` | bool, mask | The region that pyTFM sums the forces over (7,218 pixels) |

The attributes of the file keep the gel constants: `grid_um` 2.117, `young_pa` 49000,
`poisson` 0.49, `thickness_um` 300 and `piv_window_um` 20. The attribute `mapi_schema` gives
each variable its role, so you do not map columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes simulated data from the model's own prior: a 32 × 32 grid with a
    known traction field. Use it to try the steps. The numbers in this tutorial are for the
    real colony.

    ```bash
    uv run python -m mapi_plugin_science.export e60 --synthetic --out tfm_synthetic.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `pytfm_colony_ko04` in **Name**. Type a description, for example "PIV displacement
   under a colony of 7 cells, 49 kPa gel".
4. In the upload panel, click **Choose files** and select `pytfm_colony.nc`.
5. Click **Preview structure**. The panel shows **Structure of the file**. Make sure that it
   shows `displacement` on `component`, `row` and `col`, and `force_mask` on `row` and `col`.
6. Optional: type a message in **Version message (optional)**.
7. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `displacement` has the role
`target` and `force_mask` the role `mask`. **Missingness** must show no missing values: the
model needs a value at each pixel.

The file is one plain dataset. You do not need the tree fields ("Put this file in a node of a
tree (optional)") or the HDF5 path.

## 4. Build the spec

1. Open **Build**. Start a new spec.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Under **Datasets**, select `pytfm_colony_ko04`. The toast "Dataset set" shows.
5. Type `traction` in the search field. Under **Models**, select **Cell forces from gel
   movement** (`sci.traction_force@^1`). The toast "Model set" shows.
6. Type the settings, the fit block and the plots of the YAML below.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: pytfm-colony-traction
  project: biophysics
data:
  dataset: dataset://biophysics/pytfm_colony_ko04@latest
model:
  use: sci.traction_force@^1
  settings:
    grid_um: 2.117341064232256
    young_pa: 49000.0
    poisson: 0.49
    young_cv: 0.0
    prior: matern
    noise_um: 0.003
    traction_pa: 1000.0
    lengthscale_um: 20.0
    field_draws: 200
    force_mask: true
  fit:
    mode: train_only
    seed: 60
    tune: 1000
    draws: 1000
    chains: 4
outputs:
  plots:
    - core.convergence
    - core.posterior_forest
    - sci.vector_field
```

The settings come from `challenges_settings()` (the `--spec` file of the export has the same
values). This table tells what each one means for the colony:

| Setting | Value | Meaning for this question |
|---|---|---|
| `grid_um` | 2.117 | The PIV step in µm (20 µm windows that move by 2.1 µm). It sets the wavenumbers |
| `young_pa` | 49000 | Young's modulus of the gel. The tractions scale exactly with it |
| `poisson` | 0.49 | The Poisson ratio of the gel (almost incompressible) |
| `young_cv` | 0 | The relative error of `young_pa`. 0 gives the notebook row "E known". Step 6.4 uses 0.15 |
| `prior` | `matern` | The prior spectrum of the tractions. Section 7 of the notebook selects it. `white` is Tikhonov and `smooth` is a squared exponential |
| `noise_um` | 0.003 | The prior median of the noise sd: 3 nm. The notebook found 3.4 nm |
| `traction_pa` | 1000 | The prior median of the traction scale in Pa |
| `lengthscale_um` | 20 | The prior median of the Matérn length scale, near the PIV window. The notebook found 23 µm |
| `field_draws` | 200 | The number of traction fields that each score draws. The notebook also uses 200 |
| `force_mask` | true | Sum the strain energy and the contractility inside `force_mask` only, as pyTFM does |

- `fit.mode: train_only` fits on all of the data. The model needs the full raster, so it has
  no split. Without this line, the validator shows the note `SPLIT_INVALID`
  ("there is no holdout for the evaluation").
- The spec has no `evaluate` block. Without a split, the fit skips the metrics `rmse` and
  `mae` ("no data.split is declared").
- The model has no pointwise log likelihood, so `elpd_loo` is not available.

!!! note "The plots"
    The model declares `core.convergence`, `core.posterior_forest` and `sci.vector_field`.
    The fit makes only the plots in `outputs.plots`, so the spec names them. The **Plots**
    section of the **Insert** panel adds a plot for you.

    The `--spec` file of the export also names `sci.field_map`. Remove it. For the traction
    field, that recipe needs the settings `y` and `x`, and the fit skips it with the warning
    `PLOT_SKIPPED`. The **Fields** section of the Model Explorer shows the same maps.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the NUTS progress. The **Logs** card shows the messages of the
   sandbox. Click **Follow** to keep the latest line in view.
4. When the run is done, the **Result** card shows **Inspect
   model://biophysics/pytfm-colony-traction@v1**. Click it.

The fit samples only 4 numbers (`noise_sd`, `traction_sd`, `lengthscale` and `smoothness`).
The model integrates the traction field out, with one closed-form likelihood for each of the
72,952 Fourier modes. Each gradient uses all modes, so the fit takes some minutes. After the
fit, the run calculates the plots and the explorer views. For these it draws 200 traction
fields.

You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer (group "Can you trust it?").

- R-hat and ESS are for the 4 hyperparameters. Our fit had a largest R-hat of 1.003, a
  smallest bulk ESS of 1,205 and no divergent transitions.
- Expect the warning `NOISE_SPECTRUM_MISMATCH`. The plain message is "The noise in the
  displacements is not the same at all scales. The ranges of the tractions are possibly too
  narrow."

The warning is correct, and the notebook finds the same problem. Neighbour PIV vectors come
from windows that overlap by 90%. Thus their errors are correlated, and the finest Fourier
modes have much less power than white noise gives. The diagnostic compares the two in the
finest quarter of the modes. A value below 0.5 starts the warning. Our fit gave 0.33. The consequence: the
ranges with `E` known are too narrow. Use them only to compare priors, not as the error bar of
the colony.

**Fit vs actual** compares `displacement_hat` with the PIV field. The fit is almost exact. A
small residual is not a sign of a good model here, because many force maps give the same
displacement.

### 6.2 Which prior do the data support?

Open **Parameters** (group "What it learned").

| Parameter | Mean | 89% interval | Notebook (ML-II) |
|---|---|---|---|
| `noise_sd` (µm) | 0.00335 | 0.00331 to 0.00339 | 3.4 nm |
| `traction_sd` (Pa) | 1,799 | 1,495 to 2,191 | not given |
| `lengthscale` (µm) | 23.8 | 18.9 to 30.0 | 23 |
| `smoothness` (α) | 0.937 | 0.920 to 0.955 | 0.94 |

The intervals are the 5.5% and 94.5% quantiles of the 4,000 draws, as in the notebook. The
Model Explorer shows the interval that you select in **Explore with options**.

The notebook finds the Matérn spectrum with ML-II: ℓ = 23 µm, α = 0.94 and 3.4 nm of noise.
Its log marginal likelihood is 2,157 above the squared exponential and 14,104 above the white
prior. The platform does not show the marginal likelihood (see section 10). To compare the
priors, fit the spec again with `prior: white` and `prior: smooth`, and compare:

- `noise_sd`. With the white prior, the notebook finds that the noise goes to zero, that is
  no regularisation. The `noise_um` prior of the spec keeps the noise sd above zero. A noise
  sd much smaller than 3 nm tells you that the prior explains the noise as force.
- The `NOISE_SPECTRUM_MISMATCH` value on **Diagnostics**.
- The strain energy (step 6.4). It moves with the prior by more than its range.

### 6.3 The traction maps

Open **Fields** (group "What it learned"). The text says "The model has 2 outputs on a grid."
Select `traction` in **Output**.

- **Statistic** **Mean** shows the posterior mean of the traction length. Select **Show the
  arrows** to see the direction. The colony pulls at its edge, with the largest tractions
  (about 1 kPa) at its left, top and lower right edges. The cells at the edges of the image
  show too.
- **Std. dev.** is almost flat. A stationary prior gives the same uncertainty at each pixel.
  This map does not show where the model is wrong.
- **Mean ÷ sd** is the notebook's right panel. Inside the colony, the tractions are many sd
  from zero.
- **Draws** and **Play the draws** show 200 possible force maps that agree with the data.
- Artefacts along the image edges come from the periodic FFT. They are outside the force
  mask.

Move the pointer over the map to read the values of one pixel.

On the model version page, open **Plots**:

| Plot | What to look for |
|---|---|
| `core.convergence` | R-hat near 1 and a high ESS for the 4 hyperparameters |
| `core.posterior_forest` | Narrow intervals. With 72,952 modes, the data fix the hyperparameters well |
| `sci.vector_field` | The heatmap of the traction length with arrows of the mean vector, and its sd. Compare it with the pyTFM map in notebook section 8 |

### 6.4 How hard does the colony pull?

The strain energy and the contractility are outputs of a score. Do step 7 to get them. These
are the results:

| Output | Our fit: mean, 89% interval | Notebook: Matérn posterior, E known, 89% | pyTFM |
|---|---|---|---|
| `strain_energy` (fJ) | 271.3, 270.9 to 271.6 | 271.0 to 271.6 | 246 (252 with the notebook formula) |
| `contractility` (µN) | 2.173, 2.168 to 2.179 | 2.167 to 2.180 | 2.13 |

The contractility agrees with pyTFM to within 2%. The strain energy is about 8% higher than
pyTFM. pyTFM smooths its tractions with a Gaussian filter, and a smoother field has less energy.
The web app shows `hdi_90`, so its interval is a little wider than the 89% interval here.

Then do the stiffness part of the decision:

1. Open the spec in **Build**.
2. Change `young_cv: 0.0` to `young_cv: 0.15`.
3. Click **Save revision**, then **Fit**.
4. Score the new version as in step 7.

`young_cv` gives each posterior draw a lognormal draw of Young's modulus. The displacements
carry no information about it, so its posterior is its prior. Each draw scales its traction
field, its strain energy and its contractility.

| Output | Our fit: mean, 89% interval | Notebook: E ± 15%, 89% |
|---|---|---|
| `strain_energy` (fJ) | 271.0, 211.4 to 340.8 | 213 to 338 |
| `contractility` (µN) | 2.171, 1.689 to 2.733 | 1.71 to 2.70 |
| `young_pa` (Pa, in **Parameters**) | 49,379, 38,331 to 62,518 | (the prior) |

The hyperparameters do not change: the same seed gives the same draws, and only the scale of
each field changes.

**The decision.** The data support the Matérn prior. With the stiffness known, the ranges
are less than 1% wide. With a 15% stiffness error, they are about 100 to 200 times wider. Thus
report the second set of ranges for the colony. In a real study, the gel stiffness causes most
of the uncertainty, then the selection of the prior, and last the displacement noise.

## 7. Score the model

Each score draws new traction fields from the posterior. A score needs a dataset, but the
model does not use its values: the field is the posterior for the fitted data. The dataset
only selects the pixels. Send the dataset that you fitted.

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**. Select `pytfm_colony_ko04`.
4. Open **Advanced**. Set **Seed (optional)** to `60`. Type `strain_energy, contractility` in
   **Variables (optional)**.
5. Click **Predict**.

The **Results** card shows the mean and the 90% interval (`hdi_90`) of each output.

!!! warning "Do not select No new data"
    Select **Use a dataset**. With **No new data**, the request has no `data`, and the run
    stops with the message "the score request has no data".

This is the request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`):

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biophysics/pytfm-colony-traction@latest",
  "mode": "predict",
  "data": "dataset://biophysics/pytfm_colony_ko04@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["strain_energy", "contractility"]
  },
  "seed": 60
}
```

- To get the map of one area, add `"select": {"row": [90, 91, 92], "col": [100, 101]}` and
  `"vars": ["traction"]`.
- `"kind": "predictive"` adds white noise with sd `noise_sd` to `displacement_hat`. It does not
  change `traction`.
- The model has no `simulate` mode. To make new data from known forces, use
  `traction_data.simulate_traction()` in Python.

If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same request.

## 8. Run a workflow recipe

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does the checks of a Bayesian
workflow on any model with draws. It fits the model with prior predictive draws. A convergence
gate needs the largest R-hat below `rhat_max` (1.01) and the smallest bulk ESS at or above
`min_ess` (400). Only then does it make the posterior predictive check and run the governance
checks `sensitivity.prior` and `recovery.simulated`. Then it writes a report with the plots.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Bayesian workflow audit** and click **Run on my data**.
3. In **1. Your data**, select `pytfm_colony_ko04` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.traction_force@^1` in **Model**.
5. Open **Fine-tune**. In **Settings**, type the `settings` of step 4 as JSON:
   `{"grid_um": 2.117341064232256, "young_pa": 49000.0, "poisson": 0.49, "young_cv": 0.0, "prior": "matern", "noise_um": 0.003, "traction_pa": 1000.0, "lengthscale_um": 20.0, "field_draws": 200, "force_mask": true}`. If you do not, the model uses `young_pa: 10000`, and the forces are 4.9 times too
   small. Set **N sims** to `5` (see the note below).
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Decisions** shows the convergence gate and the value that it read. **Outputs**
shows the plots and the report. The model version link opens the Model Explorer.

!!! note "The audit takes a long time on the full colony"
    The checks after the gate refit the model. `sensitivity.prior` fits it one time for each
    value in **Prior scales** (two by default), and `recovery.simulated` fits it **N sims**
    times (20 by default). One fit of the full colony takes from 3 minutes to more than 40
    minutes, as a function of the load of the machine. Thus decrease **N sims** before you
    start the audit.

The image recipe `sci_image_model_check` is for models with images of a scalar field. It does
not apply to this model.

## 9. The same with the CLI and the Python client

=== "CLI"

    Save the spec of step 4 as `pytfm-colony-traction.yaml`.

    ```bash
    mapi use <tenant>/biophysics
    mapi datasets upload pytfm_colony.nc --name pytfm_colony_ko04
    mapi check pytfm-colony-traction.yaml --remote @     # the server's static validation
    mapi fit pytfm-colony-traction.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model pytfm-colony-traction -s params -s diagnostics

    cat > traction-score.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://biophysics/pytfm-colony-traction@latest
    mode: predict
    data: dataset://biophysics/pytfm_colony_ko04@latest
    output: {kind: expected, summary: [mean, hdi_90], vars: [strain_energy, contractility]}
    seed: 60
    EOF
    mapi score traction-score.yaml --remote @ --model <model id> --version 1 -o traction.zarr.zip

    mapi workflows init bayes_workflow_audit --name traction-audit
    mapi workflows apply traction-audit.yaml -m "E60 audit"
    mapi workflows run traction-audit -i data=dataset://biophysics/pytfm_colony_ko04@latest \
      -p model=sci.traction_force@^1 -p n_sims=5 \
      -p 'settings={"grid_um": 2.117341064232256, "young_pa": 49000.0, "poisson": 0.49, "young_cv": 0.0, "prior": "matern", "noise_um": 0.003, "traction_pa": 1000.0, "lengthscale_um": 20.0, "field_draws": 200, "force_mask": true}' \
      --watch
    ```

    The CLI has no `models` command. Take the model id from the run (`mapi runs get <run>`)
    or from **Models** in the web app.

=== "Python"

    The file `pytfm-colony-traction.yaml` holds the spec of step 4.

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/biophysics")
    version = api.datasets.upload("pytfm_colony.nc", dataset="pytfm_colony_ko04")
    print(version.ref, version.status.value)

    with open("pytfm-colony-traction.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "pytfm-colony-traction")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://biophysics/pytfm-colony-traction@latest",
            "mode": "predict",
            "data": "dataset://biophysics/pytfm_colony_ko04@latest",
            "output": {"summary": ["mean", "hdi_90"], "vars": ["strain_energy", "contractility"]},
            "seed": 60,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["strain_energy"].values, summary["contractility"].values)
    ```

    To compare with pyTFM's own map and numbers, load them with
    `mapi_plugin_science.traction_data.challenges_reference("<path>/data")`.

## 10. Differences from the notebook

The port keeps the physics and the model of notebook sections 7 and 8. It leaves out these
parts:

- **The simulated cell (sections 1 to 6).** The `--synthetic` data come from the model's own
  Matérn prior. They are not the notebook's cell with 16 focal adhesions.
- **The L-curve and the discrepancy principle (section 3).** The platform fits the
  hyperparameters with NUTS and does not offer a scan of λ.
- **The marginal likelihood as a number.** The notebook compares the priors by log marginal
  likelihood. The platform does not show it, and `elpd_loo` is not valid for this model.
  Compare the priors with the noise sd, the diagnostic and the strain energy (step 6.2).
- **The radial power spectrum plot (section 7).** The diagnostic `NOISE_SPECTRUM_MISMATCH`
  keeps one number from it: the power ratio in the finest modes.
- **The footprint and horseshoe priors (section 6).** The model has only stationary priors.
  The forces are not limited to the cells, and sharp adhesions become smooth.
- **A finite gel thickness.** The model uses an infinite half-space, as the notebook does.
  pyTFM uses a finite thickness. The difference is only in the longest wavelengths.
- **A model of the correlated PIV noise.** The noise is white, so the ranges with `E` known
  are too narrow (notebook "Try it yourself", item 2).
- **pyTFM's map.** The web app does not show pyTFM's tractions. Use
  `challenges_reference()` in Python.

**Next steps:**

- Fit the two wild-type and two knock-out colonies of the pyTFM example data. Compare their
  contractility with one shared `young_cv` (notebook "Try it yourself", item 1).
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Workflows](../../guides/workflows.md) to change the audit recipe.
