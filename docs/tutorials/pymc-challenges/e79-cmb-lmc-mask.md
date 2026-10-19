# E79 CMB sky behind the LMC mask

This tutorial does the main part of the pymc-challenges notebook `E79_cmb_field_level.py` in
the web app. You use the model `sci.cmb_field@1`. You do not write NumPy or PyMC code.

**You need:**

- The platform (start it with `make dev`) and a project that you can upload datasets to. This
  tutorial uses the project `astronomy`. If your project has a different name, change the
  project part of each `dataset://` and `model://` reference.
- A local copy of pymc-challenges. The export command reads two files from its `data/`
  directory.
- About 30 minutes. On a laptop, the fit took about 2 minutes.

## 1. The question

The cosmic microwave background (CMB) is the oldest light in the universe. Its temperature
changes by about 100 µK from one part of the sky to another. These spots are a **Gaussian
random field**. The **power spectrum** gives the strength of the spots at each angular scale
(multipole ℓ ≈ 180° / angle).

The notebook uses a 16 × 16 degree patch of the Planck 2018 SMICA map. The patch is around the
**Large Magellanic Cloud (LMC)**. The dust and the gas of the LMC are brighter than the CMB, so
the Planck mask removes a hole of about 4 degrees. The mask also removes some small holes
around bright radio sources. In total, 16.5% of the pixels have no data.

The notebook makes each pixel of the sky a parameter (**field-level inference**). It samples
the sky together with the power spectrum in 26 bins (**bandpowers**). It asks two questions:

1. What does the CMB look like behind the LMC, and how sure are we of each pixel? (Notebook
   part F.)
2. Does the visible part of the patch agree with the Planck spectrum? (Notebook parts F and
   H.)

The notebook ends in this result: **the LMC patch agrees with the Planck spectrum.** The
amplitude of the patch is within 1.4% of Planck (log amplitude −0.014 ± 0.023), and the tilt
is −0.009 ± 0.030. Behind the LMC, the posterior mean is not a plausible sky. It is too
smooth. Each posterior draw is a plausible sky, and each draw fills the hole differently. Thus
show the draws and the sd map, not one "true" sky behind the mask.

**What you will get**, and the notebook part that each item replaces:

| In the web app | Notebook |
|---|---|
| The dataset `astronomy/planck_smica_lmc`: the map with the mask, a noise map, the Planck spectrum and the pixel window | Part A: the data, the mask and the noise from the half-mission maps |
| A fit with the Wandelt-Eriksen Gibbs sampler (**Runs**) | Part E: the Gibbs sampler |
| The posterior of the 26 bandpowers, the amplitude and the tilt (**Parameters**, `sci.power_spectrum`) | Part F, "Bandpowers", and part H, the amplitude-and-tilt model |
| Sky maps with mean, sd and draws (**Fields**) | Part F: the posterior sky, the sd map and the animation |
| The observed map next to posterior skies (`sci.ppc_image_grid`) | Part F: the posterior mean map and four posterior skies |
| `log_amplitude`, `tilt` and `bandpower_ratio` with a 90% range (**Score**) | Part H: amplitude and tilt on the real patch |
| Skies of new patches with the same spectrum (**Score**, mode Simulate) | Part B: "Is the real sky just a random draw?" |

The port does not do parts C (the naive spectrum estimate), D (PyMC and HMC) and G (the
coverage on simulated skies). See
[Differences from the notebook](#10-differences-from-the-notebook).

## 2. Get the data

The export command reads `planck_smica_patches.npz` and `planck_theory_tt.txt`. It writes a
NetCDF file that the platform can read. It prepares the map as the notebook does:

- It averages the 3.75 arcmin pixels to 7.5 arcmin pixels: 128 × 128 pixels.
- A pixel is usable only if all of its samples are inside the Planck common mask. The other
  pixels get NaN.
- The noise sd of each pixel is the local rms of the half-mission half-difference map,
  smoothed over 1 degree.
- The reference spectrum is the Planck 2018 best-fit TT spectrum, with the HEALPix pixel
  window of the source map.

Run the command from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e79 \
    --challenges <path to pymc-challenges>/data \
    --out planck_smica_lmc.nc --spec planck_smica_lmc.yaml
```

The command writes:

```text
wrote planck_smica_lmc.nc (variables temperature, noise_sd, reference_dl, pixel_window on row=128, col=128, multipole=4097)
upload it as dataset astronomy/planck_smica_lmc and fit sci.cmb_field
wrote the suggested spec to planck_smica_lmc.yaml
```

The file holds one dataset (about 370 KB):

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `row`, `col` | 128, 128 | integer, fixed | The pixels of the patch. Row 0 is the bottom row |
| `multipole` | 4097 | integer, fixed, 0 to 4096 | The multipole ℓ of the spectrum |
| `temperature` | `row, col` | float64, µK, target | The SMICA temperature. NaN in 2,703 masked pixels (16.5%) |
| `noise_sd` | `row, col` | float64, µK, weight | The noise sd of each pixel: 1.5 µK to 3.4 µK |
| `reference_dl` | `multipole` | float64, µK², constant | The Planck best-fit spectrum D_ℓ = ℓ(ℓ+1)C_ℓ/2π |
| `pixel_window` | `multipole` | float64, constant | The HEALPix pixel window (Nside 2048) |

The attributes of the file keep the constants of the map: `pixel_arcmin` 7.5,
`beam_fwhm_arcmin` 5.0, `pixel_subsamples` 8, `patch` `lmc` and `centre_lb_deg` (280, −35). The
attribute `mapi_schema` gives each variable its role, so you do not map columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes a simulated patch from the model's own prior: a 32 × 32 map with a
    hole, a test spectrum and a known amplitude (1.15) and tilt (−0.1). Use it to try the
    steps. The numbers in this tutorial are for the real patch. For the simulated patch, use
    the settings of `CMB_SPEC_YAML` in `mapi_plugin_science.cmb_data`. With `--synthetic`,
    the `--spec` file has the bandpower bins of that spec (`bin_edges`), because the
    simulated patch is too small for the first default bin.

    ```bash
    uv run python -m mapi_plugin_science.export e79 --synthetic --out cmb_synthetic.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `planck_smica_lmc` in **Name**. Type a description, for example "Planck 2018 SMICA
   patch around the LMC, with the common mask and a noise map".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `planck_smica_lmc.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Click **Preview structure**. The panel shows **Structure of the file**. Make sure that it
   shows `temperature` and `noise_sd` on `row` and `col`, and `reference_dl` and
   `pixel_window` on `multipole`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `temperature` has the role
`target`, `noise_sd` the role `weight`, and `reference_dl` and `pixel_window` the role
`constant`. **Missingness** shows 2,703 missing values in `temperature`. These are the masked
pixels. The model does not fill them before the fit: a masked pixel has no data term.

!!! note
    The file has no groups, so the upload makes a flat dataset. Do not open "Put this file in
    a node of a tree (optional)".

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `planck_smica_lmc`. The toast "Dataset set" shows.
5. Type `cmb` in the search field. Under **Models**, select **Sky map behind a mask (CMB)**
   (`sci.cmb_field@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `planck-smica-lmc` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: planck-smica-lmc
  project: astronomy
data:
  dataset: dataset://astronomy/planck_smica_lmc@latest
model:
  use: sci.cmb_field@^1
  settings:
    pixel_arcmin: 7.5
    beam_fwhm_arcmin: 5.0
    pixel_subsamples: 8
    pixel_window: true
    projection: gnomonic
    border: 8
    field_draws: 40
  fit:
    mode: train_only
    engine: gibbs
    seed: 79
    tune: 16
    draws: 64
    chains: 4
outputs:
  plots:
    - core.convergence
    - sci.power_spectrum
    - sci.field_map
    - sci.image_residuals
    - sci.ppc_image_grid
```

The settings come from the suggested spec of the export (`planck_smica_lmc.yaml`). This table
tells what each setting means for this patch:

| Setting | Value | Meaning for this question |
|---|---|---|
| `pixel_arcmin` | 7.5 | The side of a map pixel in arcmin. With 128 pixels, the patch is 16 degrees wide |
| `beam_fwhm_arcmin` | 5.0 | The SMICA beam: a Gaussian with a FWHM of 5 arcmin. The model compares the data with the sky as Planck sees it, after the beam |
| `pixel_subsamples` | 8 | Each 7.5 arcmin pixel is the mean of 8 × 8 point samples (a box window) |
| `pixel_window` | true | Use the variable `pixel_window`: the HEALPix pixels of the source map also smooth the sky |
| `projection` | `gnomonic` | Correct the multipoles for the mean stretch of the tangent-plane projection (about 1%). `flat` turns the correction off |
| `border` | 8 | 8 pixels (1 degree) without data on each side. The sky is periodic on a 144 × 144 torus, but the real patch is not periodic. Without the border, the seam makes false small-scale power |
| `field_draws` | 40 | The number of sky maps that each score draws. The notebook animation also uses 40 skies |

These settings keep their defaults, which are the values of the notebook:

- `bin_edges`: the 26 bins of the notebook, from ℓ = 0 to ℓ = 2100.
- `ell_dense: 300`: the dense preconditioner of CG for the 681 modes below ℓ = 300.
- `cg_tol: 1e-5`: the stop rule of CG. A looser stop rule makes the large-scale bins too low
  (notebook part G).
- `rescale: true`: the non-centred rescaling move. It helps in the bins where the noise is
  larger than the sky.
- `tilt_pivot: 500` and `tilt_max_ell: 1450`: the pivot of the tilt, and the three top bins
  that the amplitude-and-tilt summary leaves out (notebook part H).

Notes about the other blocks:

- `fit.mode: train_only` fits on all of the pixels. The suggested spec of the export has
  `evaluate: {metrics: [rmse, mae, coverage_90]}` and no split. With that spec, the validator
  shows the warning `SPLIT_INVALID` ("There is no holdout for the evaluation"). Remove the
  `evaluate` block and add `mode: train_only`.
- `tune: 16` and `draws: 64` give 4 chains of 80 Gibbs iterations, as in the notebook. The
  first 16 iterations of each chain are burn-in.
- `engine: gibbs` is the only engine of this model. The model has no NUTS.
- The model has no pointwise log likelihood (`loo_valid` is false), because all of the pixels
  share one latent sky. Thus `elpd_loo` is not available. Compare two fits by their
  bandpowers, amplitude and tilt.

!!! note "The plots"
    The fit makes only the plots in `outputs.plots`. The spec keeps all five plots that the
    model declares. `sci.field_map`, `sci.image_residuals` and `sci.ppc_image_grid` use the
    in-sample sky maps that the run makes after the fit. In the **Insert** panel, the **Plots**
    section adds a plot for you.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows "Gibbs iteration N of 80". The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://astronomy/planck-smica-lmc@v1**. Click it.

All four chains run together. Each Gibbs iteration has three steps:

1. A constrained realisation: a draw of the full sky (20,736 pixels on the torus) for the
   current bandpowers. Preconditioned conjugate gradients (CG) solve one linear system. In
   our fit, CG took about 370 iterations for each solve.
2. A draw of each bandpower from its inverse-gamma distribution, given the sky.
3. The rescaling move for each bin. It is almost always rejected in the large-scale bins and
   accepted about half of the time in the top bins. The notebook found the same.

After the fit, the run calculates the in-sample sky maps, the plots and the explorer views.
You can also find the run under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer (group "Can you trust it?").

With 4 chains of 64 kept draws, expect these results:

| Code | Value | Message |
|---|---|---|
| `RHAT` | 1.10 (error) | "The independent sampler runs do not agree about 'q'. Its estimates are not reliable at this time." |
| `ESS_BULK` | 42 | "There is too little independent information about 'q' (about 42 effective draws). The averages are not stable." |
| `ESS_TAIL` | 59 | The same message for the intervals |

These values agree with the notebook. The notebook reports "r_hat is at most 1.11 and bulk
ESS runs from 28 to about 200 out of 256 draws - enough for bandpowers". The worst bins are
in the middle of the spectrum (ℓ ≈ 600 and ℓ ≈ 1400). The amplitude and the tilt converge
better: R-hat 1.01 and a bulk ESS of about 250.

If the diagnostic `CG_NOT_CONVERGED` shows, a CG solve stopped at `cg_max_iter`. Then the
large-scale bandpowers can be too low. Increase `cg_max_iter` or `ell_dense`.

!!! tip "A longer fit"
    The `RHAT` error comes from the short chains of the notebook, not from a wrong model. Set
    `tune: 40` and `draws: 240` and fit again. On a laptop, this fit took about 4 minutes. It
    gave R-hat 1.04 (a warning) and a bulk ESS of 100. The amplitude and the tilt changed by
    less than 0.2 posterior sd.

On the model version page, the **Plots** tab has `core.convergence`. Its caption names the
parameters that did not converge. The caption uses the limits 1.01 and 400 effective draws,
so with 256 draws it lists all 80 values of `q`, `bandpower` and `bandpower_ratio`.

### 6.2 What did the model learn?

Open **Parameters** (group "What it learned"). The posterior has these variables:

| Variable | Meaning |
|---|---|
| `q` (`bin`) | The log ratio of the bandpower to the Planck reference in each of the 26 bins |
| `bandpower` (`bin`) | The bandpower D_ℓ of each bin in µK² |
| `bandpower_ratio` (`bin`) | `exp(q)`: the bandpower divided by the reference |
| `log_amplitude` | The log amplitude of the bandpowers relative to Planck |
| `tilt` | The tilt of the bandpowers relative to Planck, at ℓ = 500 |

The `bin` dim has the coords `ell` (the centre of the bin), `ell_low` and `ell_high`.

The main results, with the notebook values:

| Quantity | This fit | Notebook | Meaning |
|---|---|---|---|
| `log_amplitude` | −0.018 ± 0.022 | −0.014 ± 0.023 | The patch is within 2% of the Planck amplitude |
| `tilt` | 0.001 ± 0.030 | −0.009 ± 0.030 | No tilt relative to Planck |
| `bandpower_ratio` at ℓ ≈ 80 | 1.39 ± 0.33 | 40% high, 25% uncertainty | The largest departure. A 16-degree patch has few large-scale modes (cosmic variance) |
| `bandpower_ratio` at ℓ ≈ 1950 | 1.17 ± 0.12 | 18% high | The top bin |
| Masked fraction | 16.5% | 16.5% (`masked_fraction` in the learned values) | The LMC hole and the small holes |

The amplitude and the tilt are the exact Gaussian posterior of the notebook's part H model,
`q_b = log A + tilt · log(ℓ_b / 500)`, with the bins below ℓ = 1450. The fit gives one draw of
them for each Gibbs draw. The learned values also keep `log_amplitude_mean`,
`log_amplitude_sd`, `tilt_mean` and `tilt_sd`.

On the model version page, open **Plots** and select `sci.power_spectrum`. Its caption says
"The line is the median of bandpower in each bin of ell, from 256 draws of posterior. The
shaded bands are the 50% and 90% intervals." The line must follow the acoustic peaks of the
Planck spectrum, with wide bands at small ℓ and narrow bands at large ℓ. The plot does not
draw the Planck reference. To compare with Planck, read `bandpower_ratio` in **Parameters** or
score it (step 7.1). Each bin must be near 1.

### 6.3 The sky behind the LMC (Fields)

Open **Fields** (group "What it learned"). The text says "The model has 2 outputs on a grid."
Select the output in **Output**:

- `sky` is the sky as Planck sees it (after the beam), on `row` and `col`, in diverging
  colours. The view uses 40 posterior draws.
- `temperature_hat` is the same map. It adds noise only in a predictive score, so here it is
  equal to `sky`.

Select `sky` and do these steps:

1. Select **Statistic** **Mean**. Outside the mask, the map is the SMICA map. In the LMC hole,
   the mean continues the spots that cross the edge of the hole for a fraction of a degree.
   Then it fades to a smooth, almost flat level. The mean is the best single guess, but it is
   not a plausible sky.
2. Select **Std. dev.** The sd is near the noise level (about 2.5 µK) where there are data.
   In the LMC hole, it rises to about 90 µK in the middle. That is almost the prior sd of a
   pixel: the data tell nothing there. The small holes around the radio sources are between,
   at about 37 µK.
3. Select **Draws** and click **Play the draws**. This is the animation of the notebook. The
   pixels that stand still are known. The pixels that flicker are not known. Each draw fills
   the hole with spots of the correct size and contrast, and each draw is different.

Move the pointer over the map to read the values of one pixel.

The fit also keeps the derived group `sky_moments`: the mean and the sd of each pixel over all
256 kept Gibbs draws. The **Fields** view uses the 40 draws of the in-sample score, so its sd
is a little noisier. In our fit, the mean sd over the LMC hole was 80 µK, with a maximum of
108 µK.

On the model version page, open **Plots**:

| Plot | What to look for |
|---|---|
| `core.convergence` | R-hat and ESS of the bins (step 6.1) |
| `sci.power_spectrum` | The bandpowers with their 50% and 90% bands (step 6.2) |
| `sci.field_map` | The mean and the sd of `sky`. The sd map shows the LMC hole as a large bright area and the small holes as dots |
| `sci.image_residuals` | The observed `temperature`, the mean of `sky`, the residual and the normalised residual |
| `sci.ppc_image_grid` | The observed map next to 5 posterior skies on one colour scale. The observed map has an empty hole. Each sky fills the hole differently |

!!! note "The residual plot of this model"
    In our fit, the caption of `sci.image_residuals` says: "The normalised residuals have an
    sd of 0.28 and 0% are outside ±2 … Here the residuals are too small: the uncertainty is
    too large." For this model, that is not a sign of a bad fit. On this map, the sky is
    stronger than the noise at almost all the scales of the grid. Thus each sky draw follows
    the data closely, and the residual is small (about 0.7 µK). The sd of the draws is about
    the noise level (2.5 µK). The residual plot cannot separate the noise from the sky here.
    The notebook does the calibration check with simulated skies (part G), not with the
    residuals.

### 6.4 The decision

**The decision.** The visible part of the patch agrees with the Planck spectrum. Report the
amplitude and the tilt with their posterior sd: −0.018 ± 0.022 and 0.001 ± 0.030 in this fit.
Both are within one sd of zero. No bin is more than about 1.7 sd from Planck (in `q`).

For the sky behind the LMC, show the posterior draws and the sd map, not the mean alone. The
mean is too smooth inside the hole. The draws are plausible skies, but no draw is "the"
answer. The sd map shows where the data stop: inside the hole, the map knows almost nothing
1 to 2 degrees from the nearest data.

Remember that one 16-degree patch is 0.6% of the sky. The error bars are large. A systematic
error of 1% to 2% (for example the projection correction) is small here, but it would be many
sds on the full sky. Do step 10, "Next steps", to measure one of these systematic errors.

## 7. Score the model

Each score draws new sky maps from the posterior. A score needs a dataset. Send the dataset
that you fitted. The model uses its grid and its noise map.

### 7.1 The amplitude, the tilt and the bandpowers

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**.
4. Type or select `dataset://astronomy/planck_smica_lmc@latest` in **Dataset**.
5. Open **Advanced**. Set **Seed (optional)** to `79`. Type
   `log_amplitude, tilt, bandpower_ratio` in **Variables (optional)**.
6. Click **Predict**.

The **Results** card shows the mean and the 90% interval (`hdi_90`) of each output.

!!! note "No new data"
    The page does not show **No new data** for this model. The model cannot forecast, so a
    request without a dataset has no forecast horizon, and the server refuses it. A note under
    **Values to score** gives this reason.

This is the request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`):

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://astronomy/planck-smica-lmc@latest",
  "mode": "predict",
  "data": "dataset://astronomy/planck_smica_lmc@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["log_amplitude", "tilt", "bandpower_ratio"]
  },
  "seed": 79
}
```

In our fit, the result was `log_amplitude` −0.018 (90% interval −0.055 to 0.005) and `tilt`
0.003 (−0.034 to 0.057). A score uses `field_draws` (40) posterior draws, not all 256. Thus
its numbers are a little different from the **Parameters** section.

- To get the sky maps, type `sky` in **Variables (optional)**. Each draw is a constrained
  realisation, so the result is the mean and the 90% interval of each pixel.
- To get a part of the map, add `"select": {"row": [60, 61, 62], "col": [60, 61, 62]}`.
- `"kind": "predictive"` adds white noise with `noise_sd` to `temperature_hat`. It does not
  change `sky`.

### 7.2 Simulate new patches

`simulate` makes skies of a new patch with the spectrum of each posterior draw, plus noise.
The data do not constrain these skies. The notebook asks the same question in part B: does the
real sky look like a random draw with this spectrum?

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**, with the same dataset.
3. Open **Advanced**. Set **Quantity** to **Predictive**.
4. Type `temperature_hat` in **Variables (optional)**. Set **Draws** to `8`.
5. Click **Simulate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://astronomy/planck-smica-lmc@latest",
  "mode": "simulate",
  "data": "dataset://astronomy/planck_smica_lmc@latest",
  "output": {
    "kind": "predictive",
    "summary": ["mean", "sd"],
    "draws": 8,
    "vars": ["temperature_hat"]
  },
  "seed": 79
}
```

The result has 8 simulated maps of 128 × 128 pixels. The sd over the maps is about 96 µK in
each pixel, the same as the rms of the real map (96 µK). A simulated map has no hole and no
LMC. Compare it with the real map outside the mask. It must have spots of the same size and
contrast.

If you promote the version to an endpoint, **Integrations** → the endpoint → **Schema and Try
it** sends the same requests.

## 8. Run a workflow recipe

The recipe `sci_image_model_check` (**Image model check**) is for models that predict an
image. It fits the model,
makes the residual maps and the image grid, does a posterior predictive check and writes a
report.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Image model check** and click **Run on my data**.
3. In **1. Your data**, select `planck_smica_lmc` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.cmb_field@^1` in **Model**.
5. Open **Fine-tune (optional: the defaults are correct for most data)**. Set these values:
    - **Settings**: the settings of step 4 as JSON:
      `{"pixel_arcmin": 7.5, "beam_fwhm_arcmin": 5.0, "pixel_subsamples": 8, "pixel_window": true, "projection": "gnomonic", "border": 8, "field_draws": 40}`.
    - **Draws** `64`, **Tune** `16`, **Chains** `4` and **Seed** `79`.
    - **Var**: `sky`.
6. Click **Run this recipe**.

!!! caution "Set draws and tune"
    Set **Draws** and **Tune** before you run the recipe. The defaults are 1000 and 1000. For
    this model, that is 2,000 Gibbs iterations, about 25 times as long as the fit of step 5.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Outputs** shows the parameter table, the two plots, the coverage of the 50% and
90% ranges and the report. For this model, expect a coverage near 100% and the same note as
in step 6.3. The sky draws follow the data closely, so the in-sample check cannot find a
problem with the noise model.

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does all the checks of a
Bayesian workflow on any model with draws: a convergence gate, the posterior predictive check, `sensitivity.prior` and
`recovery.simulated`. Run it in the same way with the model `sci.cmb_field@^1` and these
changes:

- Set **Prior predictive** to `{}`. This model does not sample its priors.
- Set **Draws** `240` and **Tune** `40`. Set **Rhat max** to `1.1` and **Min ess** to `50`. With the
  defaults (1.01 and 400), the gate stops the audit for this model, because the Gibbs chains
  are short (step 6.1).
- Set **N sims** to `5` for a first run.

!!! note
    The checks of `bayes_workflow_audit` that fit the model again (`sensitivity.prior` and
    `recovery.simulated`) each take as long as one fit or longer. With 5 simulated datasets,
    expect the audit to take half an hour or more.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/astronomy
    mapi datasets upload planck_smica_lmc.nc --name planck_smica_lmc
    mapi check planck-smica-lmc.yaml --remote @     # the server's static validation
    mapi fit planck-smica-lmc.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model planck-smica-lmc -s params -s diagnostics -s fields

    cat > cmb-score.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://astronomy/planck-smica-lmc@latest
    mode: predict
    data: dataset://astronomy/planck_smica_lmc@latest
    output: {kind: expected, summary: [mean, hdi_90], vars: [log_amplitude, tilt, bandpower_ratio]}
    seed: 79
    EOF
    mapi score cmb-score.yaml --remote @ --model <model id> --version 1 -o cmb.zarr.zip

    mapi workflows init sci_image_model_check --name cmb-check
    mapi workflows apply cmb-check.yaml -m "E79 image check"
    mapi workflows run cmb-check -i data=dataset://astronomy/planck_smica_lmc@latest \
      -p model=sci.cmb_field@^1 -p var=sky -p draws=64 -p tune=16 -p seed=79 \
      -p 'settings={"pixel_window": true, "projection": "gnomonic", "border": 8}' \
      --watch
    ```

    The CLI has no `models` command. Take the model id from the run (`mapi runs get <run>`)
    or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/astronomy")
    version = api.datasets.upload("planck_smica_lmc.nc", dataset="planck_smica_lmc")
    print(version.ref, version.status.value)

    with open("planck-smica-lmc.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "planck-smica-lmc")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://astronomy/planck-smica-lmc@latest",
            "mode": "predict",
            "data": "dataset://astronomy/planck_smica_lmc@latest",
            "output": {"summary": ["mean", "hdi_90"], "vars": ["log_amplitude", "tilt"]},
            "seed": 79,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["log_amplitude"].values, summary["tilt"].values)
    ```

## 10. Differences from the notebook

The port keeps the data of part A, the Gibbs sampler of part E, the posterior sky and the
bandpowers of part F and the amplitude and tilt of part H. It leaves out these parts:

- **The Gaussian random field lessons (part B).** The port has no plots of the Hartley modes
  or of the phases. `simulate` gives skies with the posterior spectrum instead.
- **The naive spectrum estimate (part C).** The platform has no masked-map estimate to
  compare with. The notebook shows that such an estimate is biased at high ℓ.
- **The PyMC model with NUTS (part D).** The centred and non-centred models, and the failure
  of HMC on a masked map, are not in the port. The model always uses the Gibbs sampler.
- **The coverage on simulated skies (part G).** The notebook fits 8 simulated skies with a
  known, non-Planck spectrum and finds that 83% of the truths are in the 90% intervals. The
  platform does not do this check for you. The `recovery.simulated` check of
  `bayes_workflow_audit` (step 8) is the nearest replacement.
- **The amplitude-and-tilt model in PyMC (part H).** The port calculates the same posterior
  exactly, inside the fit, as `log_amplitude` and `tilt`. It does not refit it on the 8
  simulated skies.
- **The comparison with the SMICA inpainted map (part F).** The dataset does not have the
  inpainted map. In the notebook, the inpainted map is within one posterior sd of the mean in
  67% of the hole pixels. With `cmb_data.challenges_reference()` in Python, our fit gave 66%.
- **The Planck full-sky bandpowers and the theory curve in the plots.** `sci.power_spectrum`
  shows the bandpowers of the patch only.
- **The north patch.** The export reads only the LMC patch. In Python,
  `cmb_data.from_challenges(path, patch="north")` reads the other patch.

**Next steps:**

- Fit the spec again with `projection: flat` and compare `log_amplitude`. The notebook finds
  a change of 1.4% (about 0.6 sd). This is the systematic error of step 6.4.
- Fit the north patch. Make the dataset in Python with `patch="north"`. It has almost no
  mask and about two times the noise.
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Model Explorer](../../guides/model-explorer.md) for more about the **Fields** section.
