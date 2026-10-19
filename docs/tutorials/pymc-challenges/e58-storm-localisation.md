# E58 STORM localisation microscopy

This tutorial does the main part of the pymc-challenges notebook
`E58_storm_localization_microscopy.py` in the web app. You use the model
`sci.storm_localisation@1`. You do not write PyMC code. The model reads the raw camera frames,
finds the isolated spots and gives the position of each molecule with an error bar in nm.

**You need:**

- The platform (start it with `make dev`) and a project that you can upload datasets to. This
  tutorial uses the project `microscopy`. If your project has a different name, change the
  project part of each `dataset://` and `model://` reference.
- A local copy of pymc-challenges. The export command reads one file from its `data/`
  directory.
- About 20 minutes. On a laptop, the fit of 200 spots took less than 2 minutes with nutpie.

## 1. The question

A light microscope cannot separate two points that are closer than about 250 nm. This is the
diffraction limit. A single fluorescent molecule shows as a blob of about that width: the
**point spread function (PSF)**. Single-molecule localisation microscopy (STORM, PALM and
dSTORM) uses molecules that **blink**. In each camera frame, only a small number of molecules
are bright, and they are far apart. The centre of an isolated blob is much more precise than
its width. The positions of all the blinks make a super-resolved image.

The notebook uses 2,000 raw frames of a dSTORM recording of the synaptic protein Unc-13 at a
*Drosophila* neuromuscular junction (Dannhäuser et al. 2022). Each frame is a 40 × 40 pixel
crop with 127 nm pixels and an exposure of 10 ms. The camera is an EMCCD. The notebook asks
these questions:

1. What does the camera do to the photons? (Notebook section 1: the photon-transfer curve.)
2. Where is each molecule, and how sure are we? (Notebook sections 2 and 3.)
3. How precise can a position be? (Notebook section 4: the Cramér-Rao bound.)
4. Is a blob one molecule or more? (Notebook sections 5 and 6.)
5. How do we make an image from uncertain points? (Notebook section 7.)
6. How many molecules made a cluster of blinks? (Notebook section 8.)

The notebook ends in this decision for the real recording: **keep each localisation with an
error bar of less than 30 nm, and draw each one as a Gaussian with its own error bar.** On the
real frames, a model with a Gaussian PSF on a flat background adds faint emitters around bright
spots. The evidence for the number of emitters is only as good as the forward model. Thus the
notebook does not trust the emitter counts of bright blobs. The notebook keeps 4,506 of 8,342
localisations, with a median error bar of 9.5 nm and a median of about 2,300 photons. One
isolated spot (blob 12 in frame 5) has a position error of about 7 nm.

**What you will get**, and the notebook section that each item replaces:

| In the web app | Notebook |
|---|---|
| The dataset `microscopy/dannhauser2022_unc13_dstorm`: the raw frames in ADU | Section 1: the raw frames |
| The camera calibration as two settings (`offset_adu`, `scale_adu`) | Section 1: the photon-transfer curve |
| The posterior of the position, photons, background and PSF sd of 200 isolated spots (**Parameters**) | Section 3: one spot in PyMC, here for 200 spots in one model |
| The positions with 90% error ellipses over the super-resolved image (**Plots**, `sci.point_pattern`) | Section 7: the render with each error bar |
| The data, the fit and the residuals of each spot (**Plots**, `sci.image_residuals`) | Section 3: the residuals of one spot |
| The positions in nm with a 90% interval for each spot (**Score**) | Section 3: the position in nm |
| A comparison of a flat and a sloped background by `elpd_loo` | "Try it yourself", item 1 |

The port does not do sections 4, 5, 6 and 8. See
[Differences from the notebook](#10-differences-from-the-notebook).

## 2. Get the data

The export command reads `dannhauser2022_unc13_dstorm_roi.npz` and writes a NetCDF file that
the platform can read. It does these steps:

- It keeps the 2,000 frames of 40 × 40 pixels as integers in ADU (the camera units).
- It numbers the frames, rows and columns from 0, as the notebook does.
- It calculates the camera calibration from the photon-transfer curve of the full 200 × 200
  field, as notebook section 1 does. The file keeps the result in its attributes.

Run the command from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e58 \
    --challenges <path to pymc-challenges>/data \
    --out unc13_dstorm.nc --spec unc13_dstorm.yaml
```

The command writes:

```text
wrote unc13_dstorm.nc (variables frames on frame=2000, row=40, col=40)
upload it as dataset microscopy/dannhauser2022_unc13_dstorm and fit sci.storm_localisation
wrote the suggested spec to unc13_dstorm.yaml
```

The file holds one dataset of about 13 MB:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `frame` | 2000 | integer, fixed | The camera frames, 0 to 1999 |
| `row`, `col` | 40, 40 | integer, fixed | The camera pixels of the crop, 0 to 39 |
| `frames` | `frame, row, col` | int32, ADU, target | The raw camera counts. The values are from 783 to 40,176 |

The attributes of the file keep the constants of the recording:

| Attribute | Value | Meaning |
|---|---|---|
| `pixel_nm` | 127 | The size of one camera pixel in the sample (nm) |
| `offset_adu` | 457.04 | The camera offset (ADU) from the photon-transfer curve |
| `scale_adu` | 13.86 | ADU for each effective count. This is twice the EM gain of 6.93 ADU for each photon |
| `exposure_s` | 0.01 | The exposure of one frame (s) |
| `first_frame`, `roi_row`, `roi_col` | 3000, 25, 50 | The position of the crop in the full recording |

The attribute `mapi_schema` gives the variable its role, so you do not map columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes a movie that the model's own forward model simulates: 60 frames of
    24 × 24 pixels with known blinks, on the camera of the real recording (offset 457 ADU,
    gain 6.9). Use it to try the steps. The numbers in this tutorial are for the real frames.
    For the simulated movie, use the settings of `STORM_SPEC_YAML` in
    `mapi_plugin_science.storm_data`.

    ```bash
    uv run python -m mapi_plugin_science.export e58 --synthetic --out storm_synthetic.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `dannhauser2022_unc13_dstorm` in **Name**. Type a description, for example "Raw dSTORM
   frames of Unc-13 at a Drosophila synapse, 2,000 frames of 40 × 40 pixels".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `unc13_dstorm.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Click **Preview structure**. The panel shows **Structure of the file**. Make sure that it
   shows `frames` on `frame`, `row` and `col`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `frames` has the role
`target` and the units `ADU`. The three dims are `integer` and `fixed`. **Missingness** shows
no missing values. The model needs each pixel of each frame.

The file is a plain dataset, not a tree. Do not open "Put this file in a node of a tree
(optional)".

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `dannhauser2022_unc13_dstorm`. The toast "Dataset set" shows.
5. Type `storm` in the search field. Under **Models**, select **Molecule positions in STORM
   images** (`sci.storm_localisation@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `unc13-dstorm-localisation` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: unc13-dstorm-localisation
  project: microscopy
data:
  dataset: dataset://microscopy/dannhauser2022_unc13_dstorm@latest
model:
  use: sci.storm_localisation@^1
  settings:
    pixel_nm: 127.0
    offset_adu: 457.04184713091655
    scale_adu: 13.861648333299126
    kind: flat
    max_spots: 200
    threshold_sd: 5
    smooth_px: 1
    patch_px: 11
    photons_median: 1500
    psf_sd_px: 1.0
    position_sd_px: 2
    score_draws: 40
    ppc_draws: 20
    render_px_nm: 16
  fit:
    mode: train_only
    seed: 58
    tune: 1000
    draws: 1000
    chains: 4
    engine: nutpie
    keep:
      log_likelihood: true
evaluate:
  metrics: [elpd_loo, rmse, mae, coverage_90]
outputs:
  plots:
    - core.convergence
    - sci.point_pattern
    - sci.image_residuals
    - sci.field_map
```

The settings `pixel_nm`, `offset_adu`, `scale_adu` and `max_spots` come from the suggested
spec of the export (`unc13_dstorm.yaml`). Copy the two camera values with all their digits.
With 457.04 and 13.86, the detector finds 4,193 blobs, not the 4,192 of the notebook. The
spec adds some defaults so that you can see them. This table tells what each setting means for
this recording:

| Setting | Value | Meaning for this question |
|---|---|---|
| `pixel_nm` | 127.0 | The pixel size in the sample. The model gives all positions and the PSF sd in nm |
| `offset_adu`, `scale_adu` | 457.04, 13.86 | The camera calibration of notebook section 1. The model changes ADU to effective counts: (ADU − 457.04) / 13.86. These counts are Poisson with a mean of half the photons. Set the two values together |
| `kind` | `flat` | One background value in each patch, as in the notebook. `plane` adds a slope in x and y (notebook "Try it yourself", item 1) |
| `max_spots` | 200 | The number of isolated spots to fit, evenly spaced in frame order. The notebook fits one spot with NUTS |
| `threshold_sd`, `smooth_px` | 5, 1 | The detector of notebook section 3: smooth each frame with a Gaussian of 1 px and keep the regions above 5 robust noise sds |
| `patch_px` | 11 | Each spot gets a patch of 11 × 11 pixels (1.4 µm). The value must be odd |
| `photons_median` | 1500 | The prior median of the photons of a spot, as in the notebook. The prior sd on the log scale is 1 (`photons_log_sd`) |
| `psf_sd_px` | 1.0 | The prior median of the PSF sd: one pixel (127 nm), with a log sd of 0.2. A 1.49 NA objective at 670 nm gives about this width |
| `position_sd_px` | 2 | The prior sd of the position around the centre of the detected blob (px) |
| `score_draws` | 40 | The posterior draws that each score uses, and the draws of the render |
| `ppc_draws` | 20 | The predictive patch draws that the fit keeps for `sci.image_residuals` |
| `render_px_nm` | 16 | The pixel size of the super-resolved image `render`. The render is 318 × 318 pixels |

- `fit.mode: train_only` fits on all of the frames. The model has no split, because the
  detector needs each frame. Without this line, the validator shows the warning
  `SPLIT_INVALID` ("There is no holdout for the evaluation"). The suggested spec of the export
  does not have this line, so add it.
- `engine: nutpie` is the sampler of the notebook. The default engine of the model is `pymc`.
  It gives the same posterior, but it is slower.
- `keep.log_likelihood: true` keeps the pointwise log likelihood of each patch pixel. The fit
  then calculates `elpd_loo`. You need it to compare `flat` and `plane` (step 6.5).
- The fit does not calculate `rmse`, `mae` and `coverage_90`, because there is no holdout. The
  **Evaluate** score of step 7.2 uses them.

!!! note "Settings that the validator does not check"
    **Validate** checks the type and the range of each setting. It does not check the rules
    between settings. For example, `offset_adu` without `scale_adu`, or an even `patch_px`,
    is valid in the editor. The fit then stops at once with `SETTINGS_INVALID`, before the
    detector starts.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the steps of the fit. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://microscopy/unc13-dstorm-localisation@v1**. Click it.

The first progress message tells you what the detector found: "200 spots of 4192 blobs
selected". The detector finds 4,192 blobs, as in the notebook. 1,503 of them are isolated in
their patch and have 13 to 39 pixels. The fit uses 200 of the isolated blobs. Then nutpie
samples the 1,000 parameters (5 for each spot) in one model. The last message is "posterior
ready".

After the fit, the run calculates the plots and the explorer views. You can also find the run
under **Runs**. Filter it with the kind `fit`.

!!! note "The warning EVALUATION_VIEW_FAILED"
    After the fit, the run tries to score all 2,000 frames for the plots of the predictions.
    That score has more than 50 million values, so the model refuses it. The run shows the
    warning `EVALUATION_VIEW_FAILED`, and the fit is correct. The four plots of the spec do
    not need that score. The fit stores the super-resolved image and the patch draws in the
    model version.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer (group "Can you trust it?").

Expect these results:

| Code | Value | Message |
|---|---|---|
| `RHAT` | 1.018 | "The independent sampler runs do not agree about 'logN'. Its estimates are not reliable at this time." |
| `ESS_BULK` | 426 | "There are enough effective draws for stable averages." |
| `ESS_TAIL` | 389 | "There is too little independent information about 'logs' (about 389 effective draws). The intervals are not stable." |
| `DIVERGENCES` | 0 | "The sampler had no problems with the model." |
| `PARETO_K_HIGH` | 47 | "Some data points have a strong effect on the model, so the leave-one-out score is only approximate." |

The warnings are for three of the 200 spots: 18, 56 and 66. On the **Plots** tab, the caption
of `core.convergence` says "16 of 2000 parameters have not converged". It names `photons[66]`,
`logN[66]` and `x[66]`. Spot 66 is the brightest spot, with about 9,800 photons, and its error
bar is 24 nm. A blob this bright is possibly two molecules (notebook section 7). Most of the
other spots have a bulk ESS of more than 1,000. If you want fewer warnings, fit with
`tune: 2000`.

The model has its own check, `PATCH_MISFIT`. It shows when the Pearson χ² for each patch pixel
is more than 1.5 at the posterior mean. The flat model does not show it on this recording.

### 6.2 What did the model learn?

Open **Parameters** (group "What it learned"). The posterior has these variables, each with one
value for each spot:

| Variable | Meaning |
|---|---|
| `x_px`, `y_px` | The position in the patch (px). These are the parameters of the notebook table |
| `logN`, `logb`, `logs` | The log of the photons, of the background and of the PSF sd (px) |
| `x`, `y` | The position in the movie (nm): the column or row number times `pixel_nm` |
| `photons` | The photons that the molecule emitted in the frame |
| `background` | The background at the centre of the patch (photons for each pixel) |
| `psf_sd` | The PSF sd (nm) |

The table shows the posterior mean, the sd and the 90% interval of each value. It has 2,000
rows, so it does not show all of them. The summary of the 200 spots:

| Quantity | This fit | Notebook |
|---|---|---|
| Blobs found by the detector | 4,192 | 4,192 |
| EM gain (ADU for each photon) | 6.93 | 6.9 |
| Median background (photons for each pixel) | 115 | about 110 |
| Median PSF sd | 137 nm | 125 nm for blob 12 |
| Median photons of a spot | 2,862 | about 2,300 (the 4,506 kept localisations) |
| Median error bar of a position | 7.5 nm | 9.5 nm (the 4,506 kept localisations) |
| Spots with an error bar of less than 30 nm | 198 of 200 (99%) | 4,506 of 8,342 (54%) |

The error bar of a spot is the posterior sd of x and y (the root mean square of the two). The
fit uses only isolated blobs of 13 to 39 pixels. These are brighter and cleaner than the
localisations of the notebook, which include the faint extra emitters of the count model. Thus the median error bar is smaller, and almost all spots pass
the 30 nm filter.

### 6.3 The plots

On the model version page, open **Plots**:

| Plot | What to look for |
|---|---|
| `core.convergence` | R-hat near 1. Some warnings for faint spots (step 6.1) |
| `sci.point_pattern` | Each dot is one spot at its posterior mean, with an ellipse that has the true position with 90% probability. The background is the mean of `render`. Small ellipses are bright spots. A large ellipse is a spot that you must not trust |
| `sci.image_residuals` | For each spot: the observed patch, the mean of 20 predictive draws, the residual and the normalised residual. The normalised residual must look like noise. A ring or a second peak shows a second molecule or a wrong PSF |
| `sci.field_map` | The mean and the sd of `render`, the super-resolved image of the 200 spots. The sd is high where the positions are not certain |

Use the slider or **Play** in `sci.image_residuals` to see each spot. In this fit, the caption
says: "The normalised residuals have an sd of 1.07 and 6% are outside ±2." A perfect model
gives 1 and 5%. Thus the flat Gaussian model is close for these isolated spots. The caption of
`sci.point_pattern` says: "The median sd of a coordinate is 7.25."

!!! caution "The Fields section"
    The Model Explorer section **Fields** shows the grid outputs of a model. For this model, it
    needs an in-sample score of the full movie, and the model refuses that score (the note in
    step 5). If **Fields** shows "The server cannot calculate this view for this data or
    model.", use the plots `sci.field_map` and `sci.image_residuals`. They show the same
    outputs from the model version.

### 6.4 The decision

**The decision.** Keep each localisation with an error bar of less than 30 nm. Draw each one as
a Gaussian with its own error bar. Do not use the count of molecules in a bright blob.

To make the decision from these outputs:

1. Score `x` and `y` with their sd (step 7.1). The error bar of a spot is
   √((sd_x² + sd_y²) / 2).
2. Remove the spots with an error bar of 30 nm or more. In this fit, two spots go (spots 40
   and 54, with 47 nm and 74 nm).
3. Look at `sci.point_pattern`. The large ellipses are the spots that the filter removes.
4. Look at the residuals of the brightest spots in `sci.image_residuals`. If you see a ring
   or a slope in the residual, the flat Gaussian model does not describe the spot. Then fit
   `kind: plane` (step 6.5) or remove the spot.

The render `render` is the mean of 40 posterior draws of the positions. Thus a spot with a
large error bar spreads over many pixels and is faint. That is the image of notebook
section 7.

### 6.5 One spot as in the notebook, and a sloped background

**One spot.** To fit the spot of notebook section 3 (blob 12 in frame 5), save a copy of the
spec as `unc13-one-spot` and change two settings:

```yaml
    max_spots: 1
    amplitude_band: [0.45, 0.55]
```

`amplitude_band` keeps the isolated blobs whose amplitude is between the 45% and 55%
quantiles: a typical spot. The fit takes some seconds. These are the results:

| Parameter | This fit | Notebook |
|---|---|---|
| `x_px` | 4.460 ± 0.053 | 4.461 ± 0.056 |
| `y_px` | 4.399 ± 0.052 | 4.399 ± 0.053 |
| sd of `x`, `y` | 6.8 nm, 6.7 nm | 7.1 nm, 6.7 nm |
| `photons` | 2,161 | 2,157 |
| `background` | 112 | 112 |
| `psf_sd` | 125 nm | 125 nm |

R-hat is 1.002 and there are no divergences.

**A sloped background.** Fit the spec again with `kind: plane` and compare the two versions by
`elpd_loo` (**Compare** on the model version page). Both versions use the same 200 patches. On
this recording, the plane model does not converge with the default `gradient_sd` (R-hat 1.76)
and it shows `PATCH_MISFIT`. Its `elpd_loo` is lower (−94,436 against −85,943). Keep `kind:
flat` for these isolated spots.

## 7. Score the model

A score uses only the spots that the fit found. It cannot find spots in new frames. Each score
also makes `frames_hat`, the camera image of each scored frame. For all 2,000 frames,
`frames_hat` has more than 50 million values, and the model refuses the score with
`PAYLOAD_TOO_LARGE`. Thus you score a short part of the movie.

Make a dataset with the first 100 frames. Run this Python script in the directory of the
export:

```python
import xarray as xr
from mapi_plugin_science import storm_data
from mapi_plugin_science.export import write_export

ds = xr.load_dataset("unc13_dstorm.nc")
window = ds.isel(frame=slice(0, 100)).copy()
window.attrs.pop("mapi_schema", None)  # write_export adds the schema again
write_export(window, "unc13_dstorm_first100.nc", schema=storm_data.STORM_SCHEMA)
```

Upload `unc13_dstorm_first100.nc` as the dataset `unc13_dstorm_first100`, as in step 3.

### 7.1 The positions with their error bars

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**. Select `unc13_dstorm_first100` in
   **Dataset**.
4. Open **Advanced**. Set **Seed (optional)** to `58`. Type `x, y, photons, psf_sd` in
   **Variables (optional)**.
5. Click **Predict**.

The **Results** card shows the mean and the 90% interval (`hdi_90`) of each output for each of
the 200 spots. The frames of the dataset do not change these values. For example, spot 0 is at
x = 2,386 nm (90% interval 2,379 to 2,389 nm) with 4,975 photons.

!!! note "No new data"
    **No new data** does not work for this model. The request then has no `data`, and the
    server refuses it with "the score request has no data" before a run starts. Do not select the full movie
    `dannhauser2022_unc13_dstorm`. The model then refuses the score with
    `PAYLOAD_TOO_LARGE`.

This is the request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`):

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://microscopy/unc13-dstorm-localisation@latest",
  "mode": "predict",
  "data": "dataset://microscopy/unc13_dstorm_first100@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "sd", "hdi_90"],
    "vars": ["x", "y", "photons", "psf_sd"]
  },
  "seed": 58
}
```

The `sd` of `x` and `y` gives the error bar of step 6.4. To get the camera image of the fit,
type `frames_hat` in **Variables (optional)**. Set **Quantity** to **Predictive** to add
Poisson noise to the background and to the patches.

### 7.2 Evaluate the fit on the frames

1. In **What do you want to do?**, select **Evaluate**.
2. In **Values to score**, select **Use a dataset**. Select `unc13_dstorm_first100`.
3. Open **Advanced**. Set **Quantity** to **Predictive** and **Seed (optional)** to `58`.
4. Click **Evaluate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://microscopy/unc13-dstorm-localisation@latest",
  "mode": "evaluate",
  "data": "dataset://microscopy/unc13_dstorm_first100@latest",
  "output": {"kind": "predictive"},
  "seed": 58
}
```

The metrics compare `frames_hat` with the frames of the dataset. In this fit: `rmse` 469 ADU,
`mae` 138 ADU and `coverage_90` 0.81. Spots that the fit did not use are misfit, because the
model shows only the background there. Thus the coverage is less than 0.9. Use the metrics to
compare two fits of the same frames, not as a measure of one spot.

- The model has no `simulate` mode. To make new frames from known blinks, use
  `storm_data.simulate_storm()` in Python.
- If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and
  Try it** sends the same requests.

## 8. Run a workflow recipe

The recipe `sci_image_model_check` (**Image model check**) is for models that predict an
image. It fits the model,
makes the residual maps and the image grid, does a posterior predictive check and writes a
report.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Image model check** and click **Run on my data**.
3. In **1. Your data**, select `dannhauser2022_unc13_dstorm` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.storm_localisation@^1` in **Model**.
5. Open **Fine-tune (optional: the defaults are correct for most data)**. Set these values:
    - **Settings**: the settings of step 4 as JSON, for example
      `{"pixel_nm": 127.0, "offset_adu": 457.04184713091655, "scale_adu": 13.861648333299126, "max_spots": 200}`.
    - **Draws** `1000`, **Tune** `1000` and **Seed** `58`.
    - **Var**: `patch_fit`. The image check is for the fitted patches, one image for each spot.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Outputs** shows the parameter table, the plots `sci.image_residuals` and
`sci.ppc_image_grid` of `patch_fit`, and the report.

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does all the checks of a
Bayesian workflow on any model with draws: prior predictive draws, a convergence gate, the
posterior predictive check, `sensitivity.prior` and `recovery.simulated`. Run it in the same
way with the model `sci.storm_localisation@^1`. The gate needs the largest R-hat below
**Rhat max** (1.01) and the smallest bulk ESS at or above **Min ess** (400). The R-hat of
`logN` in step 6 is 1.018, so the default gate stops the audit before the checks. Increase
**Draws** and **Tune**, or make the gate less strict.

!!! caution "The posterior predictive check of a long movie"
    If a step of a recipe scores all 2,000 frames, the model refuses it with
    `PAYLOAD_TOO_LARGE`. Then run the recipe on a shorter movie. A dataset of the first 500
    frames is below the limit with `score_draws: 40`. Make it with the script of step 7 and
    `slice(0, 500)`.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/microscopy
    mapi datasets upload unc13_dstorm.nc --name dannhauser2022_unc13_dstorm
    mapi datasets upload unc13_dstorm_first100.nc --name unc13_dstorm_first100
    mapi check unc13-dstorm-localisation.yaml --remote @   # the server's static validation
    mapi fit unc13-dstorm-localisation.yaml --remote @     # create or revise the spec, fit, follow
    mapi explore model unc13-dstorm-localisation -s params -s diagnostics

    cat > storm-score.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://microscopy/unc13-dstorm-localisation@latest
    mode: predict
    data: dataset://microscopy/unc13_dstorm_first100@latest
    output: {kind: expected, summary: [mean, sd, hdi_90], vars: [x, y, photons, psf_sd]}
    seed: 58
    EOF
    mapi score storm-score.yaml --remote @ --model <model id> --version 1 -o storm.zarr.zip

    mapi workflows init sci_image_model_check --name storm-check
    mapi workflows apply storm-check.yaml -m "E58 image check"
    mapi workflows run storm-check \
      -i data=dataset://microscopy/dannhauser2022_unc13_dstorm@latest \
      -p model=sci.storm_localisation@^1 -p var=patch_fit \
      -p 'settings={"pixel_nm": 127.0, "offset_adu": 457.04184713091655, "scale_adu": 13.861648333299126, "max_spots": 200}' \
      -p seed=58 --watch
    ```

    For a local check without the server, export the data as a zarr zip
    (`--out unc13_dstorm.zarr.zip`). Then
    `mapi check unc13-dstorm-localisation.yaml --data unc13_dstorm.zarr.zip` validates the
    spec against the schema of the file.

    The CLI has no `models` command. Take the model id from the run (`mapi runs get <run>`)
    or from **Models** in the web app.

=== "Python"

    ```python
    import numpy as np
    from mapi_client import Client

    api = Client(project="<tenant>/microscopy")
    api.datasets.upload("unc13_dstorm.nc", dataset="dannhauser2022_unc13_dstorm")
    api.datasets.upload("unc13_dstorm_first100.nc", dataset="unc13_dstorm_first100")

    with open("unc13-dstorm-localisation.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "unc13-dstorm-localisation")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://microscopy/unc13-dstorm-localisation@latest",
            "mode": "predict",
            "data": "dataset://microscopy/unc13_dstorm_first100@latest",
            "output": {"summary": ["mean", "sd"], "vars": ["x", "y"]},
            "seed": 58,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    sd_x = summary["x"].sel(stat="sd").values
    sd_y = summary["y"].sel(stat="sd").values
    error_nm = np.sqrt((sd_x**2 + sd_y**2) / 2)
    keep = error_nm < 30  # the quality filter of the notebook
    print(f"{keep.sum()} of {keep.size} spots pass, median error {np.median(error_nm):.1f} nm")
    ```

    `api.datasets.upload` also takes an `xr.Dataset`. Thus you can upload the 100-frame window
    of step 7 without a file: `api.datasets.upload(window, dataset="unc13_dstorm_first100")`.

## 10. Differences from the notebook

The port keeps the camera model of notebook sections 1 and 2 and the Bayesian localisation of
section 3, for many spots at once. It leaves out these parts:

- **The photon-transfer curve of the full field.** The recording has no dark frames. The
  notebook fits the curve on the statistics of the full 200 × 200 field. The export does the
  same and gives the result as `offset_adu` and `scale_adu`. If you do not set them, the model
  fits the curve on the 40 × 40 crop. The crop is over one bright structure, so that
  calibration is less good.
- **The simulated camera check (section 2).** The package tests of the plugin do the same
  check.
- **The least squares and maximum likelihood fits, and the Cramér-Rao bound (section 4).** The
  model gives only the Bayesian posterior.
- **The emitter count (sections 5 and 6).** The model fits one emitter in each patch. It uses
  only isolated blobs of 13 to 39 pixels. It does not do the Laplace evidence, the SMC check or
  the comparison with a simple threshold at two densities.
- **The Laplace fit of all spots (section 7).** The notebook localises all 8,342 spots with a
  Laplace approximation. The model samples up to `max_spots` (at most 5,000) isolated spots
  with NUTS.
- **The Fourier ring correlation (section 7).** The platform does not calculate it.
- **The count of molecules from their blinks (section 8).** The platform does not have the
  negative-binomial blink model.

**Next steps:**

- Fit the spec again with `max_spots: 1000` and compare the share of spots with an error bar
  of less than 30 nm.
- Fit the simulated movie of step 2 (`--synthetic`). In Python,
  `storm_data.simulate_storm()` returns the same movie and its true blinks, so you can measure
  the error of each position.
- Read the [model reference](../../reference/models.md) for the other science models.
