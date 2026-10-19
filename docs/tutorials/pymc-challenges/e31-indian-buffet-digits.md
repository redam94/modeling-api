# E31 Indian buffet process on handwritten digits

This tutorial does the PyMC part of the pymc-challenges notebook
`E31_indian_buffet_process.py` in the web app. You use the model `sci.ibp_features@1`. You do
not write PyMC code. The model finds a small set of parts (features) that many digit images
share. It also completes the bottom half of digits from writers that it did not see.

**You need:**

- A running platform (`make dev`) and a project that you can upload datasets to. This tutorial
  uses the project `vision`. If your project has a different name, change the project part
  of each `dataset://` and `model://` reference.
- A local copy of pymc-challenges. The export command reads two files from its `data/`
  directory.
- About 30 minutes. On a laptop, the fit took 11 minutes with the `pymc` engine.

## 1. The question and what you get

A handwritten 9 has a closed loop at the top and a stroke down the right side. A 4 also has
the stroke down the right side. The **Indian buffet process** (IBP) is a prior for this type
of data: each image owns a set of binary features, and many images share each feature. The
prior does not fix the number of features or how the images share them.

The notebook uses the UCI "optdigits" digits. Each digit is 8 × 8 pixels of ink counts from 0
to 16. The notebook removes the two blank border columns (8 × 6 pixels) and scales the ink to
0 to 1. It uses 300 training digits by 30 writers and 200 test digits by 13 other writers. The
model is linear and Gaussian: each image is the average image plus the images of the features
that it owns, plus noise with sd `sigma_X`.

The notebook asks three questions:

1. How many features do the digits need, and what sets that number? (Notebook parts 4 and 5.)
2. Which features are stable, and which change from chain to chain? (Notebook parts 5 and 6.)
3. Do the features complete a half-seen digit better than a Gaussian model? (Notebook part 8.)

The notebook ends in this decision: **choose the resolution yourself and say why. Report what
the chains agree on, not one chain.** The noise level `sigma_X`, not the IBP mass `alpha`,
sets the number of features. The notebook fixes `sigma_X = 0.25`: one pixel of pen movement
in the 32 × 32 original changes up to 4 of the 16 counts. The class templates (for example "a
0", "a 6") are stable. The shared strokes change from chain to chain. With `K = 8` features,
the truncation is too small (the last feature probability is 0.30, not near 0). The features
give the best point completion of the bottom halves (RMSE 0.26 to 0.28), but not the best
predictive density.

**What you will get**, and the notebook part that each item replaces:

| In the web app | Notebook |
|---|---|
| The dataset `vision/optdigits_e31`: 500 digits, with the bottom half of the 200 test digits hidden by a mask | Part 1: the data |
| The posterior of `alpha`, the feature probabilities and the feature images (**Parameters**) | Part 7: the same model in PyMC, `K = 8` |
| The convergence warnings and the truncation check (**Diagnostics**) | Part 7: chains that do not agree, and the last stick `pi_8` |
| The feature images and the completed digits (**Fields**) | Part 6 (the feature images) and part 8 (the completion) |
| The real digits next to replicated digits, and the residual maps (**Plots**) | Part 8: the posterior predictive check |
| The held-out score of the test bottom halves (`learned.heldout`) | Part 8: held-out writers, IBP (PyMC, `K = 8`) |
| `owns` and `feature_prob` (**Score**) | Part 7: `Z` recovered after the sampler, and the features that each class owns |
| `elpd_loo` for each digit | Not in the notebook. Use it to compare `noise_sd`, `kind` and `n_features` |

The port does not do the collapsed Gibbs sampler (parts 3 to 6), the split-merge moves or the
PPCA baselines. See [Differences from the notebook](#10-differences-from-the-notebook).

## 2. Get the data

The export command reads `optdigits_train.csv` and `optdigits_test.csv` and writes a NetCDF
file that the platform can read. It prepares the digits as the notebook does:

- It draws 30 training digits of each class, then 20 test digits of each class, from
  `numpy.random.default_rng(2005)`. Thus you get the same 500 digits as the notebook.
- It removes the two border columns and divides the counts by 16.
- It hides the bottom 4 rows of each test digit with the mask `visible`. The pixel values stay
  in the file, so the fit can score its completion of the hidden pixels.

Run the command from the root of the modeling-api repository:

```bash
uv run python -m mapi_plugin_science.export e31 \
    --challenges <path to pymc-challenges>/data \
    --out optdigits_e31.nc --spec optdigits_e31.yaml
```

The command writes:

```text
wrote optdigits_e31.nc (variables pixels, visible on digit=500, row=8, col=6)
upload it as dataset vision/optdigits_e31 and fit sci.ibp_features
wrote the suggested spec to optdigits_e31.yaml
```

The file holds one dataset:

| Name | Dims | Type | Meaning |
|---|---|---|---|
| `digit` | 500 | categorical, fixed | `train-000` to `train-299`, then `test-000` to `test-199` |
| `row`, `col` | 8, 6 | integer, fixed | The pixels. Row 0 is the top row |
| `pixels` | `digit, row, col` | float64, ink, target | The ink of each pixel, from 0 (blank) to 1 (full). No value is missing |
| `visible` | `digit, row, col` | bool, mask | False for the bottom 4 rows of each test digit (4,800 pixels) |

The coordinates `label(digit)` (the class, 50 digits of each class) and `writers(digit)`
(`train` or `test`) keep the class and the file. The model does not use them. They help you
read the results. The attribute `mapi_schema` gives each variable its role, so you do not map
columns.

!!! tip "No pymc-challenges copy?"
    `--synthetic` writes 60 simulated 8 × 6 images from the model's own forward process. The
    images have three known stroke features with probabilities 0.6, 0.45 and 0.3, and noise
    with sd 0.15. All pixels are visible. Use it to try the steps. The numbers in this
    tutorial are for the real digits. For the simulated images, use the settings of
    `IBP_SPEC_YAML` in `mapi_plugin_science.ibp_data` (`n_features: 4`, `noise_sd: 0.2`).

    ```bash
    uv run python -m mapi_plugin_science.export e31 --synthetic --out ibp_synthetic.nc
    ```

## 3. Upload the dataset

1. Open **Datasets** in the sidebar.
2. Click **New dataset**.
3. Type `optdigits_e31` in **Name**. Type a description, for example "UCI optdigits, 8 × 6
   pixels, 300 training and 200 test digits, bottom halves of the test digits hidden".
4. In the upload panel, click **Choose files** and select `optdigits_e31.nc`.
5. Make sure that the format is **netCDF (or HDF5 written by xarray)**.
6. Click **Preview structure**. The panel shows **Structure of the file**. Make sure that it
   shows `pixels` and `visible` on `digit`, `row` and `col`.
7. Optional: type a message in **Version message (optional)**.
8. Click **Upload and ingest**.

The ingest run reads the file in the sandbox. When it is done, the version page shows the tabs
Schema, Coordinates, Missingness, Preview and About. On **Schema**, `pixels` has the role
`target` and `visible` the role `mask`. **Missingness** shows no missing values. The hidden
pixels are not missing: the mask hides them.

The file is one flat dataset, so the tree path (HDF5 groups and nodes) does not apply here.

## 4. Build the spec

1. Open **Build**. Start a new spec.
2. Set the **Editor view** to **YAML**.
3. Open the **Insert** panel on the right.
4. Under **Datasets**, select `optdigits_e31`. The toast "Dataset set" shows.
5. Type `parts` in the search field. Under **Models**, select **Shared parts of small images**
   (`sci.ibp_features@^1`). The toast "Model set" shows.
6. Type the `bind` block, the settings, the fit block and the plots of the YAML below.
7. Click **Validate**. The status shows `valid`.
8. Type `optdigits-ibp` as the name and click **Save spec**.

This is the full spec:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: optdigits-ibp
  project: vision
data:
  dataset: dataset://vision/optdigits_e31@latest
  bind:
    dims: {y: row, x: col}
    vars: {ink: pixels, shown: visible}
model:
  use: sci.ibp_features@^1
  settings:
    n_features: 8
    kind: fixed_noise
    noise_sd: 0.25
    shown_mask: true
    start: digits
    score_draws: 200
  fit:
    mode: train_only
    seed: 2005
    tune: 1000
    draws: 1000
    chains: 4
    keep:
      log_likelihood: true
      posterior_predictive: {thin: 100}
evaluate:
  metrics: [elpd_loo]
outputs:
  plots:
    - core.convergence
    - core.posterior_forest
    - sci.field_map
    - sci.ppc_image_grid
    - sci.image_residuals
```

The spec is the suggested spec of the export (`optdigits_e31.yaml`) with three changes. It
shows the defaults `kind`, `start` and `score_draws`, and it adds the plot
`sci.image_residuals`.

**The binding.** The model names its inputs `ink(digit, y, x)` and `shown(digit, y, x)`. The
dataset uses `pixels`, `visible`, `row` and `col`. `data.bind` maps the names of the model to
the names of the dataset. Outputs keep the dataset names (`row` and `col`).

This table tells what each setting means for this question:

| Setting | Value | Meaning for this question |
|---|---|---|
| `n_features` | 8 | The truncation `K`: the largest number of features. The model sums over the `2^K` = 256 patterns of each image. Each extra feature doubles the time, so the limit is 10 |
| `kind` | `fixed_noise` | Keep the pixel noise at `noise_sd`, as the notebook does. `free_noise` learns it. The notebook shows that a free noise falls to 0.10 and the number of features grows to 90 |
| `noise_sd` | 0.25 | The pixel noise `sigma_X`, in ink units. It is the resolution: a difference smaller than one pixel of pen movement is noise, not a feature. A smaller value makes more features useful |
| `shown_mask` | true | Read `shown` and hide the pixels where it is false. The fit does not see the bottom halves of the test digits, and it scores its completion of them |
| `start` | `digits` | Start each chain, with no jitter, at features equal to half of 8 random digits, with `nu = 0.8`. With `default`, some chains go to a trap where all features are off (notebook part 7) |
| `score_draws` | 200 | The posterior draws that each score uses, and that the held-out score uses. The notebook uses 200 |
| `alpha_shape`, `alpha_rate` | 1, 1 (defaults) | The Gamma(1, 1) prior of the IBP mass `alpha`, as in the notebook |
| `feature_scale_sd` | 1 (default) | The HalfNormal scale of `sigma_A`, the size of the feature images |
| `truncation_warn` | 0.05 (default) | The warning `TRUNCATION_BINDS` shows when the mean of the last feature probability is more than this value |

- `fit.mode: train_only` fits on all of the digits. Without it, the validator shows the
  warning `SPLIT_INVALID` ("There is no holdout for the evaluation"). The mask, not a split,
  makes the holdout of this question.
- `tune: 1000`, `draws: 1000` and `chains: 4` are the values of the notebook.
- `keep.log_likelihood: true` keeps the log-likelihood of each digit. Then `elpd_loo` is
  available, and you can compare variants of the model (step 6.5).
- `keep.posterior_predictive: {thin: 100}` keeps 40 replicated sets of the 500 digits for
  `sci.ppc_image_grid` and `sci.image_residuals`.

!!! warning "Do not forget `shown_mask: true`"
    If you bind `shown` but leave out `shown_mask: true`, the spec is still valid. But the
    model then ignores the mask and fits the full test digits. The bottom halves are then not
    held out, and the fit does not give `learned.heldout`. If you set `shown_mask: true` and
    do not bind `shown`, the validator shows the error `SCHEMA_INVALID`: "the model needs
    variable 'shown', but the pipeline does not make it".

!!! note "The plots"
    The fit makes only the plots in `outputs.plots`. The model declares `core.convergence`,
    `core.posterior_forest`, `sci.field_map`, `sci.ppc_image_grid` and
    `sci.image_residuals`. The spec keeps all five. In the **Insert** panel, the **Plots**
    section adds a plot for you. `sci.field_map` draws the feature images (the posterior
    variable `features`), one feature for each position of the slider.

## 5. Fit and watch the run

1. Click **Fit**. The app opens the run page.
2. The badge shows **Live** while the run sends events.
3. The **Progress** card shows the steps of the fit. The **Logs** card shows the messages of
   the sandbox. Click **Follow** to keep the latest line in view.
4. When the run is done, the **Result** card shows **Inspect
   model://vision/optdigits-ibp@v1**. Click it.

The sampler takes most of the time: about 10 minutes with the `pymc` engine. Then the
progress shows "pointwise log-likelihood": the model calculates the log-likelihood of each
digit for each draw. The last message is "posterior ready". After the fit, the run calculates
the plots and the explorer views. You can also find the run under **Runs**. Filter it with the
kind `fit`.

!!! tip "A faster engine"
    Add `engine: nutpie` to the `fit` block if your platform has nutpie. The notebook used
    nutpie, and its `K = 8` fit took less than one minute. The `digits` start works with all
    engines.

## 6. Read the result

On the model version page, the tabs are **Overview**, **Diagnostics**, **Plots**, **Data
tree**, **Preprocessing** and **Provenance**. Click **Explore** to open the Model Explorer.

### 6.1 Can you trust the fit?

Open **Diagnostics** in the Model Explorer (group "Can you trust it?").

Expect these results:

| Code | Value | Message |
|---|---|---|
| `RHAT` | 2.62 | "The independent sampler runs do not agree about 'logpi'. Its estimates are not reliable at this time." |
| `ESS_BULK` | 5 | "There is too little independent information about 'logpi' (about 5 effective draws). The averages are not stable." |
| `ESS_TAIL` | 11 | The same message for the intervals |
| `DIVERGENCES` | 26 | "The sampler had problems in 26 of 4000 steps. The results can be biased." |
| `TRUNCATION_BINDS` | 0.20 | "The model uses all the features it has. More features would possibly describe the images better." |
| `PARETO_K_HIGH` | 15 | "Some data points have a strong effect on the model, so the leave-one-out score is only approximate." |

These warnings are the result of the notebook, not a fault of the fit. The posterior of the
feature images has many modes, as in a mixture model. Each chain finds a different set of
features, so the feature probabilities `logpi` do not agree between the chains. The notebook
found R-hat values from 1.15 to 1.9 and a bulk ESS from 6 to 17 for the same quantities. The
quantities that do not depend on the labels of the features agree: `alpha` has an R-hat of
1.00 and an ESS of more than 2000. More draws do not correct this. Do not report one feature
probability as a result.

The divergences are in one chain (26 of its 1,000 draws). The notebook had none. Look at
`core.convergence` in **Plots**. Its caption says that 394 of 402 parameters have not
converged, for example `features[0, 2, 3]` and `logpi[1]`.

### 6.2 What did the model learn?

Open **Parameters** (group "What it learned"). The posterior has these variables:

| Variable | Meaning |
|---|---|
| `alpha` | The IBP mass. A larger value gives more features |
| `nu` (`feature`) | The sticks. `pi_k = nu_1 × … × nu_k` |
| `logpi` (`feature`) | The log of the feature probabilities `pi_k` |
| `sigma_A` | The scale of the feature images |
| `features` (`feature`, `row`, `col`) | The feature images `A`: the change of ink that each feature adds to the average image |

The posterior means, with the notebook values:

| Parameter | This fit | Notebook | Meaning |
|---|---|---|---|
| `alpha` | 3.49 ± 1.17 | 4.19 | The IBP mass |
| `sigma_A` | 0.186 | 0.16 | The size of the feature images |
| `logpi` | −0.87 to −1.59 | −0.69 to −1.15 | The log feature probabilities, feature 1 to feature 8 |
| `pi_8`, by chain | 0.19 to 0.22 | 0.30 to 0.33 | The last feature probability: the truncation check |

The chains of this fit found different modes from the chains of the notebook. Thus the
feature probabilities are lower. Both fits fail the truncation check by a large amount: a
ninth feature would be owned by about one digit in five.

### 6.3 The images (Fields)

Open **Fields** (group "What it learned"). The text says "The model has 2 outputs on a
grid." Select the output in **Output**:

- `features` is the image of each feature, in a diverging scale. Red pixels get more ink than
  the average digit, blue pixels get less ink. The slider `feature` selects the feature.
  Select **Statistic** **Mean**. A feature that is a class template looks like a digit minus
  the average digit. For example, the 0 template has red pixels on a ring and blue pixels at
  the centre.
- `ink_hat` is the expected image of each digit, in a grey scale. Where the fit saw a pixel,
  it is the denoised image. Where the fit did not see a pixel, it is the completion. The
  slider `digit` selects the digit. The details table shows 24 of the 500 digits, evenly
  spaced. Select a test digit, for example `test-000`, and look at the bottom half. **Std.
  dev.** is larger in the hidden bottom half than in the top half.

Move the pointer over the map to read the values of one pixel.

On the model version page, open **Plots**:

| Plot | What to look for |
|---|---|
| `core.convergence` | Large R-hat values for `logpi`, `nu` and `features`, and R-hat near 1 for `alpha` (step 6.1) |
| `core.posterior_forest` | The intervals of `alpha`, `sigma_A` and `logpi`. The intervals of `logpi` are wide because the chains do not agree |
| `sci.field_map` | The feature images (mean, sd, mean ÷ sd, 5% and 95%). A feature with a large sd in all its pixels is a feature that changes from chain to chain |
| `sci.ppc_image_grid` | The real digit next to 5 replicated digits, on one colour scale. Use the slider to go through the digits |
| `sci.image_residuals` | The real digit, the mean of the 40 replicated digits, the residual and the normalised residual |

In this fit, the caption of `sci.image_residuals` says: "The normalised residuals have an sd
of 0.82 and 3% are outside ±2." A perfect model gives 1 and 5%. The model noise of 0.25 is
too large for most pixels. This is the misfit that the notebook finds in part 8: the real ink
has many exact zeros and some full pixels, and the Gaussian noise makes smooth values, some
of them negative. In `sci.ppc_image_grid`, the replicated digits have fewer blank pixels than
the real digit. The model describes the structure at a resolution that you chose. It is not a
generative model of ink.

### 6.4 Which digits own which features?

`owns(digit, feature)` is the probability that each digit owns each feature. Get it with a
score (step 7). Then average it over the digits of each class (the coordinate `label`). In
this fit:

| Feature | Owned by most digits of | Also owned by |
|---|---|---|
| 4 | 0 (100%) | 9 (21%) |
| 3 | 1 (96%) | 4 (64%) |
| 7 | 6 (93%) | 4 (66%) |
| 5 | 7 (88%) | 3 (45%) |
| 1 | 5 (88%) | 6 (52%), 2 (51%) |
| 6 | 9 (87%), 3 (75%) | 5 (41%) |
| 2 | 2 (81%) | 1 (56%), 9 (43%) |
| 0 | 7 (79%) | 3 (54%), 5 (53%) |

`owns` averages over the draws of all chains. A feature that has the same image in all chains
gives clear values near 0 or 1. Feature 4 (the 0 template) is in all digits 0 and in almost no
other digit, as in the notebook. Features with values between 0.3 and 0.6 for many classes are
broad corrections, or features that change from chain to chain. The notebook says the same:
with only 8 features, each feature must do more work, and it is shared across classes.

### 6.5 Make the decision

The notebook makes four statements. This is how to make each one from the outputs:

1. **Choose the resolution.** `noise_sd` is a setting that you choose, not a result. To see
   its effect, save a revision with `noise_sd: 0.35` and one with `noise_sd: 0.15`. Fit both.
   A smaller value keeps more features in use: the last `pi_k` gets larger. Do not compare
   `noise_sd` values with `elpd_loo` alone: a smaller noise always explains the training
   digits better (notebook part 4).
2. **K = 8 is too small.** `TRUNCATION_BINDS` shows `pi_last` = 0.20, much more than 0.05.
   The notebook finds 16 to 21 features at the same noise with its Gibbs sampler. Each extra
   feature doubles the time of this model, so `n_features: 10` is the limit. Thus this model
   is correct only when a small number of features is sufficient.
3. **Report what the chains agree on.** `alpha`, `sigma_A`, the completions and `elpd_loo`
   agree between the chains. The feature probabilities and most shared features do not. Use
   the class templates (for example feature 4 for the 0s), not one shared stroke.
4. **The completion.** `learned.heldout` on the **Overview** tab gives the score of the 200
   hidden bottom halves: an RMSE of 0.284 and a log predictive density of −0.70 nats for each
   digit. The notebook gives 0.28 for this model, and 0.275 to 0.31 for the Gaussian
   baselines. Thus the features give a good point completion. The notebook also shows that a
   full-covariance Gaussian gives a better predictive density, by about 4 nats for each digit.

To compare variants of the model, use `elpd_loo` (the metric of this fit is −615.3, se 124.8,
with `p_loo` 749.9). Select two model versions in **Models** and click **Compare**. Because 15
digits have a high Pareto k, the leave-one-out values are approximate.

### 6.6 The numbers of the notebook and of this port

| Quantity | This fit | Notebook |
|---|---|---|
| Held-out RMSE of the bottom halves | 0.284 | 0.283 |
| Held-out log density for each digit | −0.70 | −0.3 |
| `alpha` | 3.49 | 4.19 |
| Last stick `pi_8` (mean for each chain) | 0.19 to 0.22 | 0.30 to 0.33 |
| Largest R-hat of `logpi` | 2.6 | 1.9 |
| Divergences | 26 | 0 |
| Fit time | 10.5 min (`pymc`) | less than 1 min (nutpie) |

The log density is different because the two fits use different data. The notebook fits the
300 training digits only. This spec also fits the top halves of the 200 test digits. A
separate fit on the 300 training digits only, as in the notebook, gives 0.283 and −0.34 nats.
The RMSE agrees.

## 7. Score the model

### 7.1 Which digits own which features

1. On the model version page, click **Score**.
2. In **What do you want to do?**, select **Predict**.
3. In **Values to score**, select **Use a dataset**.
   Then select `optdigits_e31`, the dataset of the fit.
4. Open **Advanced**. Set **Seed (optional)** to `31`. Type `feature_prob, owns` in
   **Variables (optional)**.
5. Click **Predict**.

The **Results** card shows the mean and the 90% interval (`hdi_90`) of each output. In this
fit, `feature_prob` is 0.42, 0.37, 0.29, 0.25, 0.23, 0.23, 0.22 and 0.20.

This is the request that the page sends (`POST /v1/models/{model_id}/versions/{v}:score`):

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://vision/optdigits-ibp@latest",
  "mode": "predict",
  "data": "dataset://vision/optdigits_e31@latest",
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["feature_prob", "owns"]
  },
  "seed": 31
}
```

### 7.2 The completion of one test digit

The **Score** page has no field to select digits. To get only some digits, send the request
from the CLI, the Python client or **Schema and Try it** of an endpoint, with a `select`
block:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://vision/optdigits-ibp@latest",
  "mode": "predict",
  "data": "dataset://vision/optdigits_e31@latest",
  "select": {"digit": ["test-000", "test-001"]},
  "output": {
    "kind": "expected",
    "summary": ["mean", "hdi_90"],
    "vars": ["ink_hat", "owns"]
  },
  "seed": 31
}
```

`ink_hat` has the dims `digit`, `row` and `col`. Rows 4 to 7 are the completion. The two digits
are 0s. In this fit, both own feature 4 (the 0 template) with a probability of 1.00. The
completion of `test-000` closes the loop of the 0: rows 4 to 7 have ink in columns 1 and 4,
and the bottom row has ink in columns 2 and 3. The mean sd of the hidden pixels is 0.044,
and of the seen pixels 0.033.

To get completed digits with noise, set `"kind": "predictive"` and `"draws": 20`. Each draw
then has a pattern of features and pixel noise with sd `noise_sd`.

- A score uses the pixels that the fit stored, never the target values in the score data.
  Thus a hidden pixel cannot get into its own prediction.
- `digit` is fixed. A score can use only the digits of the fit. If you select a digit that the
  fit did not see, the score fails with `UNSEEN_LEVELS`. To complete a new digit, add it
  to the dataset with its unseen pixels hidden by `visible`, and fit again.
- The model has no `simulate` mode. Use `predict` with `"kind": "predictive"`.

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
3. In **1. Your data**, select `optdigits_e31` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.ibp_features@^1` in **Model**.
5. Open **Fine-tune (optional: the defaults are correct for most data)**. Set these values:
    - **Settings**: `{"n_features": 8, "noise_sd": 0.25, "shown_mask": true}`.
    - **Bind**: `{"dims": {"y": "row", "x": "col"}, "vars": {"ink": "pixels", "shown": "visible"}}`.
      Without the binding, the fit does not find `ink`.
    - **Draws** `1000`, **Tune** `1000` and **Seed** `2005`.
    - **Var**: `ink_hat`. **Frame**: `digit`. The model has two outputs on a grid, and the
      check is for the digits.
6. Click **Run this recipe**.

The monitor of the run has the tabs **Graph**, **Steps**, **Decisions**, **Outputs** and
**Events**. **Outputs** shows the parameter table, the two plots, the coverage of the 50% and
90% ranges and the report.

A short run of the recipe (`draws` 300, `tune` 300) gave these results:

- **Predictions against the data:** 63% of the pixels are in their 50% range, and 94.6% in
  their 90% range. The check fails (`ok` is false): the ranges are too wide for the many blank
  pixels. This is the misfit of step 6.3.
- **Residual maps:** the normalised residuals have an sd of 3.81. The recipe reads the
  expected images (`source: predictions`), with no pixel noise, so the normalised residuals
  are too large. For this model, use `sci.image_residuals` of the fit (step 6.3). It reads the
  replicated digits with their noise.

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does all the checks of a
Bayesian workflow on any model with draws: a convergence gate, the posterior predictive check, `sensitivity.prior` and
`recovery.simulated`. Run it in the same way with the model `sci.ibp_features@^1`, the same
**Settings** and **Bind**, and **Prior predictive** set to `{}`. For this model, the gate stops the
audit: the R-hat of `logpi` (2.6) is more than **Rhat max** (1.01). **Decisions** shows "R-hat
is too large or the effective sample size is too small, so the chains do not agree." That is
the correct result for a posterior with many modes (step 6.1). Do not increase **Rhat max** to
pass the gate.

## 9. The same with the CLI and the Python client

=== "CLI"

    ```bash
    mapi use <tenant>/vision
    mapi datasets upload optdigits_e31.nc --name optdigits_e31
    mapi check optdigits-ibp.yaml --remote @     # the server's static validation
    mapi fit optdigits-ibp.yaml --remote @       # create or revise the spec, fit, follow
    mapi explore model optdigits-ibp -s params -s diagnostics

    cat > digits-score.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://vision/optdigits-ibp@latest
    mode: predict
    data: dataset://vision/optdigits_e31@latest
    select: {digit: [test-000, test-001]}
    output: {kind: expected, summary: [mean, hdi_90], vars: [ink_hat, owns]}
    seed: 31
    EOF
    mapi score digits-score.yaml --remote @ --model <model id> --version 1 -o digits.zarr.zip

    mapi workflows init sci_image_model_check --name digits-check
    mapi workflows apply digits-check.yaml -m "E31 image check"
    mapi workflows run digits-check -i data=dataset://vision/optdigits_e31@latest \
      -p model=sci.ibp_features@^1 -p var=ink_hat -p frame=digit \
      -p 'settings={"n_features": 8, "noise_sd": 0.25, "shown_mask": true}' \
      -p 'bind={"dims": {"y": "row", "x": "col"}, "vars": {"ink": "pixels", "shown": "visible"}}' \
      -p seed=2005 --watch
    ```

    The CLI has no `models` command. Take the model id from the run (`mapi runs get <run>`)
    or from **Models** in the web app.

=== "Python"

    ```python
    from mapi_client import Client

    api = Client(project="<tenant>/vision")
    version = api.datasets.upload("optdigits_e31.nc", dataset="optdigits_e31")
    print(version.ref, version.status.value)

    with open("optdigits-ibp.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "optdigits-ibp")
    mv = api.models.version(model.id, model.latest_version)
    print(mv.ref, mv.metrics)

    result = api.models.score(
        model.id,
        mv.version,
        {
            "model": mv.ref,
            "mode": "predict",
            "data": "dataset://vision/optdigits_e31@latest",
            "select": {"digit": ["test-000", "test-001"]},
            "output": {"summary": ["mean", "hdi_90"], "vars": ["ink_hat", "owns"]},
            "seed": 31,
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["owns"].sel(stat="mean").round(2).values)  # (digit, feature)
    print(summary["ink_hat"].sel(stat="mean", digit="test-000").round(2).values)  # (row, col)
    ```

The CLI and the client send the same spec and the same score request as the web app.

## 10. Differences from the notebook

The port keeps the PyMC model of notebook part 7 (truncated stick-breaking, `Z` summed out over
`2^K` patterns, the `digits` start) and the held-out completion of part 8. It leaves out these
parts:

- **The IBP prior simulations (part 2).** The customers-and-dishes simulation, the Poisson
  law of `K+` and the comparison of the finite beta-Bernoulli model are not in the port.
- **The collapsed Gibbs sampler (parts 3 to 6).** The port has only the NUTS model with `Z`
  summed out. Thus it has no unbounded number of features, no brute-force test of the sampler
  and no runs with free `sigma_X` and free `alpha` at 90 features. `kind: free_noise` learns
  the noise, but with at most 10 features.
- **The split-merge moves (part 5).** The notebook shows that random split-merge moves are
  never accepted.
- **The label-free summaries (part 6).** The port does not give the shared-feature matrix
  `E[Z Zᵀ]` or the match of features across draws. `owns` gives the probability that each
  digit owns each feature, averaged over all chains.
- **The timings of `K` (part 7).** The port stops at `n_features: 10`.
- **The baselines (part 8).** The port does not fit PPCA or a full-covariance Gaussian. Thus
  you cannot make the density comparison of the notebook in the platform.
- **The data of the fit.** The notebook fits the 300 training digits. The export also puts
  the top halves of the 200 test digits into the fit (step 6.6).

**Next steps:**

- Fit the spec again with `start: default` and look for `FEATURES_COLLAPSED`. This is the
  "no features at all" trap of notebook part 7.
- Fit with `n_features: 10`. The fit takes about four times as long. Look at the last `pi_k`
  in `TRUNCATION_BINDS`.
- Give each pixel its own noise (notebook "Try it yourself", item 1). The model does not do
  this, so it needs a custom model.
- Read the [model reference](../../reference/models.md) for the other science models.
