# E80 gravitational lens pixel by pixel (also the tree/HDF5 path)

This tutorial does the main part of the pymc-challenges notebook `E80_strong_lensing.py` in the
web app. You use the model `sci.strong_lens@1`. You do not write PyMC or JAX code. The
tutorial also shows the tree path: the same image as a NetCDF file with HDF5 groups, with the
PSF in its own node.

**You need:**

- The platform (start it with `make dev`) and a project that you can upload datasets to. This
  tutorial uses the project `astronomy`. If your project has a different name, change the
  project part of each `dataset://` and `model://` reference.
- A local copy of pymc-challenges. The export command reads one file from its `data/`
  directory.
- About 40 minutes. On a laptop, the fit took 13 minutes: 3 minutes to find the mode and
  10 minutes to sample.

## 1. The question

A massive galaxy bends the light of a more distant galaxy. When the two galaxies are almost on
one line, the distant galaxy becomes a ring around the near galaxy: an **Einstein ring**. The
radius of the ring gives the mass inside the ring, dark matter included.

The notebook uses a Hubble Space Telescope image of the lens **SDSS J1627-0053** (ACS, filter
F814W). The lens galaxy is at redshift 0.2076 and the ring galaxy is at redshift 0.5241. The
notebook predicts each of the 6,561 pixels of an 81 × 81 cutout. The prediction has the light
of the lens galaxy, the lensed source galaxy, the blur of the telescope (the PSF) and the
noise. The notebook asks three questions:

1. How much mass is inside the ring, and how sure are we? (Notebook parts C and F.)
2. What does the source galaxy look like without the lens? (Notebook part D.)
3. What can an image not tell us? (Notebook part E.)

The notebook ends in this decision: **report the Einstein radius and the mass inside it, with
an error from the model choices, not from the posterior alone.** The posterior gives
θ_E = 1.2231 ± 0.0012 arcsec and M(<θ_E) = 2.258 ± 0.004 × 10¹¹ solar masses. That range is
0.1% to 0.2%. A different source model moves θ_E by two posterior sds, and the published
models (1.21 and 1.23 arcsec) are 17 sds apart. Thus the true error is about 1% to 2%. The
image alone does not give the slope of the mass profile or the size of the source: the
mass-sheet degeneracy changes them and keeps the image the same.

**What you will get**, and the notebook part that each item replaces:

| In the web app | Notebook |
|---|---|
| The dataset `astronomy/slacs_j1627_f814w`: the image, a noise map and a PSF from the stars | Part A: the noise in each pixel and the PSF |
| The posterior of the 23 lens, light and source parameters and the noise scale (**Parameters**) | Part C: the posterior with NUTS |
| The predicted image and the source without the lens (**Fields**) | Part C, the posterior predictive check, and part D, the source with its uncertainty |
| The residual maps and the predicted images next to the real image (**Plots**) | Part C: the residual panels |
| `einstein_radius` and `einstein_mass` with a 90% range (**Score**) | Part F: the mass in the ring |
| Simulated images with noise (**Score**, mode Simulate) | Part B: the prior predictive check, here from the posterior |
| The derived group `critical_curves` | Part F: the critical curves and caustics of 60 draws |

The port does not do parts D (pixelated source) and E (mass-sheet degeneracy). See
[Differences from the notebook](#10-differences-from-the-notebook).

## 2. Get the data

The export command reads `slacs_j1627_f814w.npz` and writes a NetCDF file that the platform
can read. It prepares the image as the notebook does:

- It cuts the 81 × 81 pixels (4.05 arcsec) around the lens.
- It measures the sky noise in an empty patch of the image.
- It makes the noise map from the sky noise and the photon counts. `weight` is
  1 / noise².
- It makes the PSF from the median of the stars that are not near saturation (31 × 31).

Run the command from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e80 \
    --challenges <path to pymc-challenges>/data \
    --out slacs_j1627.nc --spec slacs_j1627.yaml
```

The command writes:

```text
wrote slacs_j1627.nc (variables image, weight, psf on row=81, col=81, psf_row=31, psf_col=31)
upload it as dataset astronomy/slacs_j1627_f814w and fit sci.strong_lens
wrote the suggested spec to slacs_j1627.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `row`, `col` | 81, 81 | integer, fixed | The pixels of the cutout. Row 0 is the bottom row |
| `psf_row`, `psf_col` | 31, 31 | integer, fixed, −15 to 15 | The pixels of the PSF kernel |
| `image` | `row, col` | float64, electron/s, target | The HST image |
| `weight` | `row, col` | float64, s²/electron², weight | The inverse variance of each pixel |
| `psf` | `psf_row, psf_col` | float64, constant | The PSF. Its sum is 1 |

The attributes of the file keep the constants of the image: `pixel_scale` 0.05 arcsec,
`noise_correlation` 0.229, `sky_sd` 0.0059 electron/s, `exptime` 2224 s, `z_lens` 0.2076 and
`z_source` 0.5241. The attribute `mapi_schema` gives each variable its role, so you do not map
columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes a simulated lens from the model's own forward model: a 24 × 24 image
    with 0.1 arcsec pixels, a Gaussian PSF and a known Einstein radius of 0.7 arcsec. Use it
    to try the steps. The numbers in this tutorial are for the real image. For the simulated
    lens, use the settings of `LENS_SPEC_YAML` in `mapi_plugin_science.lens_data`.

    ```bash
    uv run python -m mapi_plugin_science.export e80 --synthetic --out lens_synthetic.nc
    ```

### 2.1 Optional: the same data as a tree (HDF5 groups)

An instrument often writes the image and the PSF in different groups of one HDF5 file. The
platform reads such a file as a **tree dataset**. Each group becomes a node. `sci.strong_lens`
reads a tree: it takes the image from one node and the PSF from the node `/psf`.

To make the tree file from the export, run this Python script in the same directory:

```python
import xarray as xr
from mapi_plugin_science import lens_data
from mapi_plugin_science.export import write_export

ds = xr.load_dataset("slacs_j1627.nc")
ds.attrs.pop("mapi_schema", None)  # the tree file gets its own tree schema
tree = lens_data.to_tree(ds)  # /image: image and weight; /psf: psf
write_export(tree, "slacs_j1627_tree.nc", schema=lens_data.LENS_TREE_SCHEMA)
```

The file `slacs_j1627_tree.nc` is NetCDF-4, that is HDF5 with one group for each node:

| Node | Variables | Dims |
|---|---|---|
| `/` | none (the attributes only) | none |
| `/image` | `image` (target), `weight` (weight) | `row` 81, `col` 81 |
| `/psf` | `psf` (constant) | `psf_row` 31, `psf_col` 31 |

Steps 3 and 4 show the plain dataset first and then the changes for the tree. The fit and the
results are the same.

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `slacs_j1627_f814w` in **Name**. Type a description, for example "HST F814W image of
   the lens SDSS J1627-0053, with a noise map and a PSF".
4. Click **Create dataset**. The upload panel opens.
5. Click **Choose files** and select `slacs_j1627.nc`.
6. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
7. Click **Preview structure**. The panel shows **Structure of the file**. Make sure that it
   shows `image` and `weight` on `row` and `col`, and `psf` on `psf_row` and `psf_col`.
8. Optional: type a message in **Version message (optional)**.
9. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `image` has the role
`target`, `weight` the role `weight` and `psf` the role `constant`. **Missingness** shows no
missing values.

### 3.1 The tree file

To upload the tree file, do the same steps with these changes:

1. Type `slacs_j1627_tree` in **Name**.
2. Select `slacs_j1627_tree.nc`. The format is also **netCDF (or HDF5 written by xarray)**.
3. Click **Preview structure**. The panel shows the groups `/image` and `/psf` with their
   arrays. A file with groups becomes a tree of nodes.
4. Do not open "Put this file in a node of a tree (optional)". The groups of the file become
   the nodes below `/`.
5. Click **Upload and ingest**.

The version page of a tree dataset shows the tabs Nodes, Missingness and About. **Nodes**
shows `/`, `/image` and `/psf`. Click **Explore this node** to see one node in the Data
Explorer.

!!! note "A raw HDF5 file"
    An HDF5 file from an instrument (written with h5py) has arrays but no dim names. Select
    the format **raw HDF5 (map the arrays)** and click **Upload and list the arrays**. In
    **Variables**, give each array a name, a node and its dims: `image` and `weight` in node
    `/image` on `row, col`, `psf` in node `/psf` on `psf_row, psf_col`. Set the roles
    `target`, `weight` and `constant`. Refer to
    [Tree datasets](../../guides/tree-data.md#raw-hdf5-inspect-then-map) for the mapping file.

### 3.2 Explore the data

Look at the data before you build the spec.

1. On the version page, click **Explore this version**. The Data Explorer opens. The
   sections under *Pictures of the data* come first.
2. Click **Images and fields**.

- The viewer shows `image` and `weight` (81 × 81 pixels) and `psf` (31 × 31 pixels). Look
  for the arc of the lensed galaxy around the lens.
- The statistics of each image give the fraction of saturated pixels.
- **Correlations** compares `image` and `weight` pixel by pixel.
- For the tree file, click **Explore this node**. The node `/image` has the two images of
  81 × 81 pixels. The node `/psf` has the image of 31 × 31 pixels. The explorer finds the
  sections of each node separately.

Refer to [Scientific data](../../guides/data-explorer.md#scientific-data).

## 4. Build the spec

1. Open **Build** and click **New spec**. The editor opens with the geo MMM template.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Open the **Datasets** section and select `slacs_j1627_f814w`. The toast "Dataset set" shows.
5. Type `lens` in the search field. Under **Models**, select **Gravitational lens from an
   image** (`sci.strong_lens@^1`). The toast "Model set" shows.
6. Select all the text in the editor and paste the full spec below. The template has other
   blocks (a pipeline, a split and the MMM plots), so the spec must replace all of it.
7. Click **Validate**. The status shows `valid`.
8. Click **Save spec**. The spec gets its name `slacs-j1627-lens` from `metadata.name`.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: slacs-j1627-lens
  project: astronomy
data:
  dataset: dataset://astronomy/slacs_j1627_f814w@latest
model:
  use: sci.strong_lens@^1
  settings:
    pixel_scale: 0.05
    subsample: 2
    lens_light_components: 2
    external_shear: true
    noise_correlation: 0.2
    fit_noise_scale: true
    theta_e_median: 1.0
    theta_e_log_sd: 0.5
    n_starts: 5
    source_grid: 30
    source_half_width: 0.45
    curve_draws: 100
    z_lens: 0.2076
    z_source: 0.5241
  fit:
    mode: train_only
    seed: 80
    tune: 400
    draws: 600
    chains: 4
    target_accept: 0.9
outputs:
  plots:
    - core.convergence
    - core.posterior_forest
    - sci.image_residuals
    - sci.ppc_image_grid
```

The settings come from the suggested spec of the export (`slacs_j1627.yaml`). The spec adds
some defaults so that you can see them. This table tells what each setting means for this
image:

| Setting | Value | Meaning for this question |
|---|---|---|
| `pixel_scale` | 0.05 | The pixel size of the drizzled ACS image in arcsec. All angles (θ_E, the radii) are in arcsec |
| `subsample` | 2 | The model traces 2 × 2 rays through each pixel. The ring is thin, so one ray for each pixel is too coarse |
| `lens_light_components` | 2 | Two Sérsic profiles for the light of the lens galaxy, as in the notebook. The second one is a broad halo |
| `external_shear` | true | Add the tidal pull of the galaxies near the lens. The notebook finds a shear of about 0.026 |
| `noise_correlation` | 0.2 | Drizzle correlates the noise of neighbour pixels. The file attribute gives 0.23. With 0, the posterior sds are about 1.4 times too small (notebook "Failure 2") |
| `fit_noise_scale` | true | Multiply the noise map by a free factor. The notebook finds 0.885: the noise map is too large by 13% |
| `theta_e_median`, `theta_e_log_sd` | 1.0, 0.5 | The log-normal prior of θ_E: 1 arcsec, times or divided by about 1.6 |
| `n_starts` | 5 | The starts of the search for the mode. Lens posteriors have wrong modes (notebook part C) |
| `source_grid`, `source_half_width` | 30, 0.45 | The grid of the `source` output: 30 × 30 pixels over ±0.45 arcsec around the source |
| `curve_draws` | 100 | The posterior draws that get a critical curve and a caustic |
| `z_lens`, `z_source` | 0.2076, 0.5241 | The SLACS redshifts. With them, the model gives `einstein_mass` (flat ΛCDM, `h0` 70, `omega_m` 0.3) |

- `fit.mode: train_only` fits on all of the pixels. Without it, the validator shows the
  warning `SPLIT_INVALID` ("There is no holdout for the evaluation").
- `target_accept: 0.9` is the value of the notebook. At 0.8 the notebook got some divergences.
- The spec has no `evaluate` block. Metrics need a holdout (`data.split`), and this fit uses
  all of the pixels. The recipe of step 8 measures the in-sample coverage.
- The model has no pointwise log likelihood (`loo_valid` is false), so `elpd_loo` is not
  available.

!!! note "The plots"
    The fit makes only the plots in `outputs.plots`. The model declares `core.convergence`,
    `core.posterior_forest` and `sci.field_map`. The spec keeps the first two and adds the two
    image checks `sci.image_residuals` and `sci.ppc_image_grid`. In the **Insert** panel, the
    **Plots** section adds a plot for you.

    Do not add `sci.field_map`. The fit draws that plot from the first posterior
    variable with two dims, `e_light`, and not from the image. To see the predicted image as
    a map, use the **Fields** section of the Model Explorer (step 6.3).

### 4.1 The spec for the tree dataset

For the tree dataset, change the `data` block and add one setting:

```yaml
data:
  dataset: dataset://astronomy/slacs_j1627_tree@latest
  nodes:
    image: {path: /image}
model:
  use: sci.strong_lens@^1
  settings:
    psf_source: tree
    # ... the other settings as above
```

- `data.nodes` selects the node `/image`. The pipeline reads `image` and `weight` from it. In
  the **Insert** panel, the section **Tree nodes** adds a node. The toast "Node added" shows.
- `psf_source: tree` tells the model to read the PSF from the node `/psf`. The flat input then
  does not need `psf`.
- If you do not set `psf_source: tree`, the validator shows three `SCHEMA_INVALID` errors, for
  example "the model needs variable 'psf', but the pipeline does not make it". Another
  solution is to also select the node `/psf` in `data.nodes`.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the steps of the fit. The **Events** card shows the
   stages of the run and, at the end, the diagnostics. The **Logs** card stays empty when
   the model writes no output.
4. When the run is done, the **Result** card shows **Inspect
   model://astronomy/slacs-j1627-lens@v1**. Click it.

The fit has four stages. The progress messages tell you the stage:

1. "finding the mode in stages". The model fits the lens light first, with the ring masked.
   It reads the Einstein radius off the ring in the image without the lens light. Then it
   starts the full model from `n_starts` source positions and keeps the best. The notebook
   does the same staged start in part C, because starts from random prior draws end in wrong
   modes.
2. "sampling the posterior". NUTS samples near the mode. A Laplace approximation at the mode
   removes the strong correlations before the warm-up. That is the job of nutpie's low-rank
   mass matrix in the notebook.
3. "drawing the light amplitudes". The four brightness amplitudes are linear, so the fit
   integrates them out. After the fit, each draw gets its amplitudes from their exact
   conditional distribution.
4. "tracing the critical curves". The model calculates the critical curve and the caustic of
   `curve_draws` draws.

After the fit, the run calculates the plots and the explorer views. You can also find the run
under **Runs**. Filter it with the kind `fit`.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer (group "Can you trust it?").

Expect these results:

| Code | Value | Message |
|---|---|---|
| `RHAT` | 1.009 | "The independent sampler runs agree with each other." |
| `ESS_BULK` | 352 | "There is too little independent information about 'n_light' (about 352 effective draws). The averages are not stable." |
| `ESS_TAIL` | 309 | The same message for the intervals |
| `DIVERGENCES` | 6 | "The sampler had problems in 6 of 2400 steps. The results can be biased." |

These warnings agree with the notebook. The notebook had one divergence in 2,400 draws and
an ESS of "several hundred" for the index of the second light profile, `n_light[2]`. That
profile is at the lower limit of its index (0.5, a Gaussian). It is a broad halo of the lens
light, and its parameters are not of interest. The parameters of the lens have a high ESS.
If you want fewer warnings, fit with `tune: 1000` and `draws: 1000`.

On the model version page, the **Plots** tab has `core.convergence`. Its caption names the
parameters that did not converge: `n_light[2]` and `e_light[1, e1]`.

### 6.2 What did the model learn?

Open **Parameters** (group "What it learned"). The posterior has these variables:

| Variable | Meaning |
|---|---|
| `theta_E` | The Einstein radius in arcsec |
| `e_mass` (`e1`, `e2`) | The ellipticity of the mass |
| `xy_mass` (`x`, `y`) | The centre of the mass in arcsec, from the centre of the cutout |
| `shear` (`e1`, `e2`) | The external shear |
| `xy_light`, `R_light`, `n_light`, `e_light` | The centre, the half-light radii, the Sérsic indices and the ellipticities of the lens light |
| `R_src`, `n_src`, `e_src`, `xy_src` | The half-light radius, the Sérsic index, the ellipticity and the position of the source |
| `noise_scale` | The factor on the noise map |
| `amplitude` | The brightness of each light component and of the sky |

The posterior mean and sd of the main parameters, with the notebook values:

| Parameter | This fit | Notebook | Meaning |
|---|---|---|---|
| `theta_E` | 1.2230 ± 0.0011 | 1.2231 ± 0.0012 | The Einstein radius (arcsec) |
| `e_mass` `e1` | −0.087 ± 0.004 | −0.087 | The mass is flat: axis ratio q ≈ 0.84 |
| `shear` `e1`, `e2` | −0.026, 0.005 | about 0.026 in total | A small external shear |
| `xy_mass` | (−0.002, −0.006) | within 0.01 of the light | The mass centre is on the light centre, less than a fifth of a pixel away |
| `R_src` | 0.138 ± 0.004 | 0.14 | The half-light radius of the source (arcsec) |
| `n_src` | 0.87 ± 0.04 | 0.87 | The Sérsic index of the source: almost an exponential disc |
| `xy_src` `y` | −0.155 | 0.15 south | The source is 0.15 arcsec south of the lens centre |
| `n_light[2]` | 0.52 ± 0.01 | 0.5 (at the limit) | The second light profile is a broad halo |
| `noise_scale` | 0.884 ± 0.008 | 0.885 | The noise map is too large by 13% |

The model does not force the mass centre onto the light centre, but the data put it there.
The Laplace approximation at the mode gives an sd of 0.00114 arcsec for `theta_E`. NUTS gives
almost the same value. The fit stores it in the learned values as `laplace_sd_theta_E`.

### 6.3 The images (Fields)

Open **Fields** (group "What it learned"). The text says "The model has 2 outputs on a
grid." Select the output in **Output**:

- `model_image` is the predicted image on `row` and `col`, in a grey scale. Select
  **Statistic** **Mean**. It must look like the HST image: the lens galaxy at the centre and
  the ring around it. **Std. dev.** is small everywhere. **Draws** and **Play the draws**
  show that the draws are almost the same.
- `source` is the source galaxy without the lens, on the source-plane grid
  (`source_row`, `source_col`). This is the notebook's "unlensed galaxy". **Mean** shows a
  small, smooth galaxy. **Mean ÷ sd** shows where its shape is certain.

Move the pointer over the map to read the values of one pixel.

On the model version page, open **Plots**:

| Plot | What to look for |
|---|---|
| `core.convergence` | R-hat near 1. A low ESS only for the second light profile (step 6.1) |
| `core.posterior_forest` | Very narrow intervals for `theta_E`, `xy_mass` and `e_mass`. Wider intervals for the source and the second light profile |
| `sci.image_residuals` | The real image, the mean prediction, the residual and the normalised residual. The normalised residual must look like noise with sd near 1, with no ring structure. A wrong lens leaves a pattern along the ring |
| `sci.ppc_image_grid` | The real image next to predicted images with noise, on one colour scale. You must not see which image is the real one |

In this fit, the caption of `sci.image_residuals` says: "The normalised residuals have an sd
of 1.07 and 7% are outside ±2." A perfect model gives 1 and 5%. Thus the model is close, but
it leaves a small structure. Look at the residual panel for structure along the ring. In this
fit, about 88% of the pixels are in their 90% predictive range.

### 6.4 How much mass is in the ring?

`einstein_radius` and `einstein_mass` are outputs of a score. Do step 7 to get them. These are
the results:

| Output | Mean | 90% interval | Notebook |
|---|---|---|---|
| `einstein_radius` (arcsec) | 1.2230 | 1.2212 to 1.2249 | 1.2231 ± 0.0012 |
| `einstein_mass` (solar masses) | 2.259 × 10¹¹ | 2.252 to 2.265 × 10¹¹ | 2.258 ± 0.004 × 10¹¹ |

The published values are 1.23 arcsec (Bolton et al. 2008, with a 2% error) and 1.21 arcsec,
2.22 × 10¹¹ solar masses (Koopmans et al. 2006). The fit is between the two. The two published
values are 0.02 arcsec apart. That is about 17 posterior sds. Thus the posterior range is not
the real error of θ_E.

**The decision.** Report θ_E and the mass inside it. These two numbers are robust: the
mass-sheet degeneracy does not change them. Do not report the posterior range alone. Use an
error of about 2% from the model choices, as the SLACS papers do. Do not use this fit for the
slope of the mass profile or the true size of the source. One image cannot give them.

## 7. Score the model

### 7.1 The Einstein radius and the mass

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**.
   Then select `slacs_j1627_f814w`, the dataset of the fit.
4. Open **Advanced**. Set **Seed (optional)** to `80`. Type
   `einstein_radius, einstein_mass` in **Variables (optional)**.
5. Click **Predict**.

The **Results** card shows the mean and the 90% interval (`hdi_90`) of each output.

This is the request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`):

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://astronomy/slacs-j1627-lens@latest",
  "mode": "predict",
  "data": "dataset://astronomy/slacs_j1627_f814w@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["einstein_radius", "einstein_mass"]
  },
  "seed": 80
}
```

### 7.2 Simulated images

`simulate` makes new images from the posterior: each draw gives an image with correlated
noise, at the noise level of the fit.

1. In **What do you want to do?**, select **Simulate**.
2. In **Values to score**, select **Use a dataset**.
   Then select `slacs_j1627_f814w`, the dataset of the fit.
3. Open **Advanced**. Set **Quantity** to **Predictive**.
4. Type `model_image` in **Variables (optional)**. Set **Draws** to `20`.
5. Click **Simulate**.

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://astronomy/slacs-j1627-lens@latest",
  "mode": "simulate",
  "data": "dataset://astronomy/slacs_j1627_f814w@latest",
  "output": {
    "kind": "predictive",
    "summary": ["mean", "hdi_90"],
    "draws": 20,
    "vars": ["model_image"]
  },
  "seed": 80
}
```

The **Results** card shows the mean and the 90% interval of each pixel, and the result has 20
simulated images. The mean is the predicted image. In this fit, the sd of the simulated
images is about 0.011 electron/s for each pixel: the noise of the image plus the small spread
of the posterior. Compare one simulated image with the real image. It must look like a second
exposure of the same lens.

- To get a part of the image, add `"select": {"row": [40, 41, 42], "col": [40, 41, 42]}`.
- `"kind": "predictive"` in a `predict` request also adds the noise.
- The score always uses the PSF that the fit stored. The tree dataset is not necessary to
  score.

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
3. In **1. Your data**, select `slacs_j1627_f814w` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.strong_lens@^1` in **Model**.
5. Open **Fine-tune (optional: the defaults are correct for most data)**. Set these values:
    - **Settings**: the settings of step 4 as JSON, for example
      `{"pixel_scale": 0.05, "subsample": 2, "lens_light_components": 2, "noise_correlation": 0.2, "z_lens": 0.2076, "z_source": 0.5241}`.
    - **Draws** `600`, **Tune** `400` and **Seed** `80`.
    - **Var**: `model_image`. The model has two outputs on a grid, and the check is for the
      image.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Outputs** shows the parameter table, the two plots, the coverage of the 50% and
90% ranges and the report.

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does all the checks of a
Bayesian workflow on any model with draws: a convergence gate, the posterior predictive check,
`sensitivity.prior` and `recovery.simulated`. Run it in the same way with the model
`sci.strong_lens@^1`. Set **Prior predictive** to `{}`, because this model does not sample its
priors. The gate needs the smallest bulk ESS at or above **Min ess** (400). In step 6, the bulk
ESS of `n_light` is 352, so the default gate can stop the audit. Increase **Draws**, or set
**Min ess** to `300`.

!!! note
    The checks of `bayes_workflow_audit` that fit the model again (`sensitivity.prior` and
    `recovery.simulated`) each take as long as one fit. With 20 simulated datasets, the audit
    of the lens takes some hours. Set **N sims** to `5` for a first run.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/astronomy
    mapi datasets upload slacs_j1627.nc --name slacs_j1627_f814w
    mapi check slacs-j1627-lens.yaml --remote @     # the server's static validation
    mapi fit slacs-j1627-lens.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model slacs-j1627-lens -s params -s diagnostics

    cat > lens-score.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://astronomy/slacs-j1627-lens@latest
    mode: predict
    data: dataset://astronomy/slacs_j1627_f814w@latest
    output: {kind: expected, summary: [mean, hdi_90], vars: [einstein_radius, einstein_mass]}
    seed: 80
    EOF
    mapi score lens-score.yaml --remote @ --model <model id> --version 1 -o lens.zarr.zip

    mapi workflows init sci_image_model_check --name lens-check
    mapi workflows apply lens-check.yaml -m "E80 image check"
    mapi workflows run lens-check -i data=dataset://astronomy/slacs_j1627_f814w@latest \
      -p model=sci.strong_lens@^1 -p var=model_image \
      -p 'settings={"pixel_scale": 0.05, "subsample": 2, "lens_light_components": 2, "noise_correlation": 0.2, "z_lens": 0.2076, "z_source": 0.5241}' \
      -p draws=600 -p tune=400 -p seed=80 --watch
    ```

    The tree file uploads in the same way:
    `mapi datasets upload slacs_j1627_tree.nc --name slacs_j1627_tree`. The ingest makes one
    node for each group. A local `mapi check spec.yaml --data <file>.tree.zip` reads the tree
    schema of a tree dataset.

    The CLI has no `models` command. Take the model id from the run (`mapi runs get <run>`)
    or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/astronomy")
    version = api.datasets.upload("slacs_j1627.nc", dataset="slacs_j1627_f814w")
    print(version.ref, version.status.value)

    with open("slacs-j1627-lens.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "slacs-j1627-lens")
    result = api.models.score(
        model.id,
        model.latest_version,
        {
            "model": "model://astronomy/slacs-j1627-lens@latest",
            "mode": "predict",
            "data": "dataset://astronomy/slacs_j1627_f814w@latest",
            "output": {"summary": ["mean", "hdi_90"], "vars": ["einstein_radius", "einstein_mass"]},
            "seed": 80,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["einstein_radius"].values, summary["einstein_mass"].values)
    ```

    `api.datasets.upload` also takes an `xr.DataTree`. Thus you can upload the tree of step
    2.1 without a file: `api.datasets.upload(tree, dataset="slacs_j1627_tree")`.

## 10. Differences from the notebook

The port keeps the forward model and the posterior of notebook part C, and the mass of part
F. It leaves out these parts:

- **The tests of the deflection with automatic differentiation (part B).** The package tests
  of the plugin do the same checks.
- **The prior predictive check (part B).** The model does not sample its priors. `simulate`
  gives images from the posterior instead.
- **The failures (part C).** You can do "Failure 2" again: fit with `noise_correlation: 0` and
  compare the sd of `theta_E`. "Failure 1" (no PSF) needs a dataset with a 1 × 1 PSF.
- **The full whitening of the drizzle noise.** The notebook whitens the noise in Fourier space
  with the measured correlation. The model uses one lag-one correlation along the rows and the
  columns.
- **The pixelated source and the Bayesian evidence (part D).** The `source` output is the
  parametric Sérsic source of each draw on a grid, not a free pixel source. Thus the model
  does not give the second value θ_E = 1.2253 arcsec of the notebook.
- **The mass-sheet degeneracy (part E).** The platform does not show it. The decision of step
  6.4 keeps its result: use θ_E and the mass inside it, not the slope.
- **The velocity dispersion (part F).** The model does not give σ_SIE (273 km/s in the
  notebook).
- **The plots of the critical curves and the caustics.** The fit stores them in the derived
  group `critical_curves`, but no plot recipe draws them yet.

**Next steps:**

- Fit the spec again with `external_shear: false` and compare the shape of the mass with the
  published shear-free models (axis ratio 0.91 to 0.92).
- Fit another SLACS lens. Make the same three variables (`image`, `weight`, `psf`) from its
  MAST image (notebook "Try it yourself", item 2).
- Read the [model reference](../../reference/models.md) for the other science models.
- Read [Tree datasets](../../guides/tree-data.md) for more about nodes, HDF5 and files with
  several nodes.
