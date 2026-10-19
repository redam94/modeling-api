# E50: a scratch assay as a reaction-diffusion PDE

A sheet of PC-3 prostate cancer cells has a wound scratched through it. The cells move in from
the two sides and divide, and the wound closes in about two days. How fast do the cells move,
and how fast do they divide? This tutorial answers that question with the model
`sci.fisher_kpp@1` in the web app. It uses the same real data as part one of the
pymc-challenges notebook `E50_reaction_diffusion_signaling.py`.

**You need:**

- A running platform (`make dev`) and an account that can upload datasets and fit models.
- A local copy of pymc-challenges, with its `data/` directory.
- About 30 minutes. Each fit takes some minutes.

## 1. The question and what you get

The notebook fits the **Fisher-KPP** equation to cell counts:

`du/dt = a(t) (D d²u/dx² + lam u (1 - u))`

- `u(x, t)` is the cell density as a part of the carrying capacity `K`.
- `D` (µm²/h) is the diffusivity: how fast the cells move at random.
- `lam` (1/h) is the growth rate: how fast the cells divide when they have space.
- `a(t) = tanh(beta t)` is a start-up delay. The cells move and divide slowly immediately
  after the scratch. `a(t) = 1` when there is no delay.
- The count in each 50 µm column is Poisson with the mean `K u(x, t)`.

The model solves the PDE in the model graph. Each column is a finite volume, the edges have
zero flux and time moves in explicit Euler steps. The front of the wound moves at the
**wave speed** `2 sqrt(D lam)`.

The notebook ends in two decisions:

1. **Keep the start-up delay.** The model with the delay fits much better (PSIS-LOO), and the
   model without it gives a diffusivity that is about four times too small.
2. **Report the wave speed first.** The data identify the wave speed better than `D`. `lam`
   trades off against the capacity `K` and the delay `beta`, so it is a model parameter, not a
   direct measurement of the cell cycle.

You do the same work in the platform. You get these items:

| Notebook section | In the platform |
|---|---|
| 1. The scratch assay | The dataset `biology/scratch_assay_pc3`: `cells(hours, position)`, 5 × 38 counts |
| 2. Fisher-KPP by the method of lines | `sci.fisher_kpp@1`. The model does the method of lines and the stability check |
| 3. Two models: with and without a start-up delay | Two fits (`delay: true` and `delay: false`) and **Compare** |
| 4. What is identified, and the invasion wave | The **Parameters** section, the posterior forest, `wave_speed`, and the **Fields** section (the kymograph) |
| 4. The wound closing, hour by hour | A forecast past 48 h on the **Score** page |
| 5 to 8 (morphogen gradient, FRAP, positional information) | Not in the platform. See [Differences from the notebook](#10-differences-from-the-notebook) |

## 2. Get the data

The export command reads `scratch_assay_pc3.csv` from pymc-challenges. It writes a NetCDF
dataset that has the raw schema of the model. Run it from the root of the modeling-api
repository:

```bash
uv run python -m mapi_plugin_science.export e50 \
    --challenges <path to pymc-challenges>/data \
    --out scratch_assay_pc3.nc --spec scratch_assay_pc3.yaml
```

The command prints this output:

```text
wrote scratch_assay_pc3.nc (variables cells on hours=5, position=38)
upload it as dataset biology/scratch_assay_pc3 and fit sci.fisher_kpp
wrote the suggested spec to scratch_assay_pc3.yaml
```

The dataset has two dims and one variable:

| Name | Kind | Values |
|---|---|---|
| `hours` | dim, integer, policy `extend` | 0, 12, 24, 36, 48 |
| `position` | dim, float, policy `fixed` | 25, 75, …, 1875 (the centres of 38 columns of 50 µm) |
| `cells(hours, position)` | variable, `int64`, role `target`, units `cells` | 0 to 127 cells in each column |

!!! tip "No copy of pymc-challenges?"
    Use `--synthetic` in place of `--challenges`. The command then writes a scratch assay that
    the model itself simulates, with known parameters (`D` = 1500, `lam` = 0.06, `K` = 120,
    no delay). That dataset has 16 columns of 100 µm and the times 0, 8, 16, 24 and 32 h:

    ```bash
    uv run python -m mapi_plugin_science.export e50 --synthetic --out assay.nc
    ```

## 3. Upload the dataset

1. In the sidebar, click **Datasets**.
2. Click **New dataset**.
3. In **Name**, type `scratch_assay_pc3`. Then click **Create dataset**.
4. In the upload panel, click **Choose files** and select `scratch_assay_pc3.nc`.
5. Click **Preview structure**. The panel shows **Structure of the file**: the dims `hours` (5)
   and `position` (38) and the variable `cells`.
6. Click **Upload and ingest**.

The ingest reads the raw schema from the file (the `mapi_schema` attribute). Thus `cells` gets
the role `target` and you do not map the columns. When the ingest is done, the version page
shows the tabs **Schema**, **Coordinates**, **Missingness**, **Preview** and **About**. Click
**Copy reference** to copy `dataset://<project>/scratch_assay_pc3@latest`.

The dataset is one plain array. It is not a tree, and you do not need the HDF5 path.

## 4. Build the spec

1. In the sidebar, click **Build**. Start a new spec.
2. Set the editor view to **YAML**.
3. Open the **Insert** panel. In the search box, type `fisher`.
4. Under **Models**, click `sci.fisher_kpp`. The toast **Model set** shows.
5. Under **Datasets**, click `scratch_assay_pc3`. The toast **Dataset set** shows.
6. Complete the YAML as below. Then click **Validate**.
7. When the status shows `valid`, click **Save spec**.

This is the full spec. It starts from the spec that the export command wrote. It adds the
priors of the notebook, `keep: {log_likelihood: true}` and the `sci.profile_bands` plot:

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: scratch-assay-pc3-kpp, project: biology}
data:
  dataset: dataset://biology/scratch_assay_pc3@latest
  bind:
    dims: {t: hours, x: position}
    vars: {count: cells}
  split: {kind: time_holdout, dim: t, last: 1}
model:
  use: sci.fisher_kpp@^1
  settings:
    steps_per_hour: 10
    delay: true
    max_extension: 4
    diffusivity_median: 1000
    growth_median: 0.05
    capacity_mu: 122
    capacity_sigma: 15
  fit:
    mode: evaluate_then_refit
    seed: 50
    tune: 1000
    draws: 1000
    chains: 4
    engine: nutpie
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, mae, coverage_90, elpd_loo]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.profile_bands, sci.field_map]
```

!!! warning "Keep the log-likelihood"
    Keep `keep: {log_likelihood: true}` in the spec. The spec that the export command writes
    does not have it. Without it, the fit does not calculate `elpd_loo`, and the metrics show
    "the model did not store a log_likelihood group". You need `elpd_loo` to compare the two
    models.

!!! warning "Use the name of your project"
    Change `biology` in `metadata.project` and in `data.dataset` to the name of your project.
    If you do not, the spec does not validate: the platform reads datasets only from the
    project that you work in. The **Insert** panel writes the correct `data.dataset` for you.

The `bind` block connects the names in the dataset to the names of the model. The model calls
time `t`, position `x` and the counts `count`. The split keeps the last time (48 h) back. The
fit with `evaluate_then_refit` first fits on 0 to 36 h and scores the 48 h counts. Then it fits
again on all the data.

| Setting | Value | What it means for this question |
|---|---|---|
| `steps_per_hour` | 10 | Euler steps of 0.1 h, as in the notebook (480 steps for 48 h). With 50 µm columns, the steps are stable for `D` up to 12,500 µm²/h |
| `delay` | true | Multiply the movement and the growth by `tanh(beta t)`. This is the notebook's second model. Set `false` for the first model |
| `max_extension` | 4 | A forecast can go 4 periods (4 × 12 h) after the last fitted time, that is to 96 h |
| `diffusivity_median` | 1000 | The prior median of `D` (µm²/h). With `diffusivity_log_sd` 1, the prior covers about 100 to 10,000 µm²/h |
| `growth_median` | 0.05 | The prior median of `lam` (1/h): doubling times from about 5 hours to some days |
| `capacity_mu`, `capacity_sigma` | 122, 15 | The prior of the capacity `K`. A full column has about 122 cells. The model estimates `K`, it does not fix it |
| `delay_rate_median` | 0.1 (default) | The prior median of `beta` (1/h). The start-up time is about `1/beta` |
| `initial_floor` | 0.1 (default) | The smallest initial count. An empty column can then fill |
| `cfl_quantile` | 0.99 (default) | The model makes sure that the Euler steps are stable at this quantile of the prior of `D` |

The first time (0 h) is the initial condition. The PDE starts from the counts at that time, and
the likelihood does not use them. Each later time minus the first time must be a whole number
of Euler steps.

!!! note "When the steps are not stable"
    If the Euler steps are not stable at the `cfl_quantile` of the prior of `D`, **Validate**
    does not find the problem. The fit then stops with `SETTINGS_INVALID`. The message gives
    the smallest `steps_per_hour` that is stable.

For the notebook's first model, save a second spec. Use the same YAML with
`name: scratch-assay-pc3-kpp-no-delay` and `delay: false`.

## 5. Fit and watch the run

1. On the spec page, click **Fit**. The button is available when the spec is saved and valid.
2. The run page opens. The badge shows **Live** while the page gets events.
3. Look at the **Progress** card. The fit goes through the evaluation fit (0 to 36 h), the
   score of the 48 h counts and the refit on all the data. Then the run renders the plots.
4. Look at the **Logs** card for warnings. Click **Follow** to stay at the end of the log.
5. When the run is done, click **Inspect model://…@v1**.

Do the same steps for the spec without the delay.

!!! tip "Faster fits"
    Each fit runs the 480 Euler steps for each gradient. On a laptop with `nutpie`, the full
    fit (the evaluation fit and the refit) took about 2.5 minutes with the delay and about
    1 minute without it. For a quick first look, set `tune: 300` and `draws: 300`.

## 6. Read the result

### Model Explorer

On the model version page, click **Explore**. These sections are the most important for this
question:

- **Overview** (*At a glance*): the holdout metrics on the 48 h counts (`rmse`, `mae`,
  `coverage_90`) and the About card of the model.
- **Parameters**: `D`, `lam`, `K`, `beta` and `wave_speed`, with their intervals, R-hat and ESS.
- **Diagnostics**: the convergence of the sampler.
- **Fields**: the page says "The model has 1 output on a grid." The output is
  `density(hours, position)`, the expected cells in each column. Time is on the rows and
  position is on the columns, so the map is a **kymograph**: the wound is the dark band in the
  middle, and it becomes narrow from the bottom (0 h) to the top (48 h). Use the **Statistic**
  buttons:
    - **Mean** shows the expected density.
    - **Std. dev.** shows where the model is not sure. It is largest at the edges of the wound.
    - **Draws** shows single posterior draws. Click **Play the draws** to see them one after
      the other.

  Move the pointer over the map to read the value of a cell.

### Plots

On the model version page, open the **Plots** tab. The left list has one entry for each plot of
the spec:

| Plot | What to look for |
|---|---|
| Sampler convergence (`core.convergence`) | R-hat near 1.00 and a large ESS for `D`, `lam`, `K` and `beta` |
| Posterior forest (`core.posterior_forest`) | The rows of `D`, `lam`, `K`, `beta` and `wave_speed`. Relative to its value, the interval of `wave_speed` is narrower than the interval of `D`. The plot also shows rows of `mu`, the expected count of each cell |
| Profiles with bands (`sci.profile_bands`) | The title is "Profiles of mu". Each line is the median count across the wound at one time, with 50% and 90% bands and the actual counts. Use the slider to show one time. With the delay, the bands follow the 12 h and 24 h profiles. Without the delay, they do not |
| Field map (`sci.field_map`) | The title is "Field map of mu". It shows the mean and the sd of the expected count for each time and column: the same kymograph as the **Fields** section |

### The decision

Make the notebook's two decisions with these outputs:

1. **Delay or no delay.** Click **Compare** on the model version page. Select the two
   versions (with and without the delay), then click **Compare**. Look at `elpd_loo` (larger
   is better) and at the holdout metrics. Then compare `D` in the two posterior forests.
   The model with the delay has an `elpd_loo` that is about 110 larger, many times the
   standard errors (11 and 24). Its `D` is about four and a half times the `D` of the model
   without the delay. Thus keep the delay, and do not use the `D` of the model without it.
   Its interval is narrow, but it is wrong.
2. **What to report.** In **Parameters**, compare the relative widths (sd ÷ mean) of `D`,
   `lam` and `wave_speed`. Report the wave speed with its interval. Report `D` and `lam` with
   the note that `lam`, `K` and `beta` trade off.

### Numbers: the notebook and the platform

We fitted both specs on the real data with `nutpie` (seed 50, 4 chains of 1000 draws). The
table shows the posterior means of the refit on all five times. The 90% intervals are in
brackets. The notebook gives most values in its text, so they are approximate.

| Quantity | Notebook | Platform, with delay | Platform, no delay |
|---|---|---|---|
| `D` (µm²/h) | about 1,260 (with delay), about 280 (no delay) | 1,274 [986, 1,632] | 284 [212, 377] |
| `lam` (1/h) | doubling time `ln 2 / lam` about 5 h | 0.147 [0.124, 0.177], doubling time 4.8 h | 0.103 [0.095, 0.109] |
| `K` (cells for each column) | about 114 | 113.4 [110.2, 116.8] | 108.5 [106.1, 111.0] |
| `beta` (1/h) | 0.03 to 0.06 | 0.043 [0.033, 0.054], start-up time `1/beta` 23 h | — |
| `wave_speed` (µm/h) | about 27 | 27.3 [23.1, 33.1] | 10.7 [9.6, 12.0] |
| Correlation of log `D` and log `lam` | 0.41 | 0.35 | — |
| Correlation of `lam` and `K` | −0.46 | −0.47 | — |
| Correlation of `lam` and `beta` | −0.81 | −0.81 | — |
| `elpd_loo` (152 counts) | delay better by many standard errors | −536.4 (se 11.0) | −646.4 (se 23.9) |
| Holdout at 48 h: `rmse`, `mae`, `coverage_90` | not calculated | 9.2, 8.3, 1.00 | 12.6, 10.4, 0.79 |

The two fits had no diagnostic warnings. With the delay, the largest R-hat is 1.004, the
smallest bulk ESS is 783 and there are no divergences. The **Diagnostics** section shows these
values. The correlations are not in the
Model Explorer. To get them, download the bundle and use the posterior draws.

## 7. Score: a forecast past 48 hours

The notebook replays the assay between the observations. The platform continues the PDE past
the last fitted time. It starts from the initial profile that it keeps with the model version.

1. On the model version page, click **Score**.
2. Under **What do you want to do?**, select **Forecast**.
3. Under **How far ahead?**, type `2` in **Periods**. One period is 12 h, so the forecast is at
   60 h and 72 h. The model accepts at most 4 periods (`max_extension`).
4. Under **Values to score**, select **No new data**.
5. Click **Forecast**.

The **Results** region shows `density` at 60 h and 72 h and `wave_speed`. At 60 h, the centre
columns have about 111 cells. At 72 h, all columns are at the capacity, about 113 cells. Thus
the model says that the wound closes between 60 h and 72 h. The request that the page sends is:

```json
{
  "apiVersion": "mapi/v1",
  "kind": "ScoreRequest",
  "model": "model://biology/scratch-assay-pc3-kpp@latest",
  "mode": "forecast",
  "horizon": {"dim": "t", "periods": 2},
  "output": {"kind": "expected", "summary": ["mean", "q5", "q95"]}
}
```

For Poisson draws of new counts in place of the expected density, set
`"output": {"kind": "predictive"}`. The model also does `predict` (in-sample, all five times)
and `evaluate` (a dataset with new counts). It does not do `simulate` or `counterfactual`.

!!! tip "Try it on an endpoint"
    If you promote the version to an endpoint, open **Integrations**, then the endpoint, then
    the **Schema and Try it** tab. Paste the same request there, without the `model` field,
    and click **Score**.

## 8. Run the workflow recipe

The recipe `bayes_workflow_audit` (**Bayesian workflow audit**) does the checks of a Bayesian
workflow for any model in one run. It fits the model with prior predictive draws and plots
them. A convergence gate needs the largest R-hat below `rhat_max` (1.01) and the smallest bulk
ESS at or above `min_ess` (400). Only then does it do a posterior predictive check, a prior
sensitivity check and a recovery check on simulated data. It writes a report with the plots.

1. Set the header to **Guided** and open **Recipes**. In **Expert** mode, open **Workflows**
   and then the **Templates** tab.
2. Find the card **Bayesian workflow audit** and click **Run on my data**.
3. In **1. Your data**, select `scratch_assay_pc3` in **Dataset**.
4. In **2. What the recipe needs to know**, type `sci.fisher_kpp@^1` in **Model**.
5. Open **Fine-tune**. In **Settings**, type the settings of step 4 as JSON:
   `{"steps_per_hour": 10, "delay": true, "max_extension": 4, "diffusivity_median": 1000, "growth_median": 0.05, "capacity_mu": 122, "capacity_sigma": 15}`. In **Bind**, type `{"dims": {"t": "hours", "x": "position"}, "vars": {"count": "cells"}}`.
6. Click **Run this recipe**. The run monitor shows the tabs **Graph**, **Steps**,
   **Decisions**, **Outputs** and **Events**.

In **Decisions**, see if the convergence gate passed. In **Outputs**, open the report.

!!! note
    Without **Bind**, the recipe cannot find the dims `t` and `x` of the model, and the fit
    stops. Without **Settings**, the fit uses the default settings of the model, not the
    settings of step 4. `sci.fisher_kpp` is not an image model, so `sci_image_model_check`
    does not apply to it.

## 9. The same steps with the CLI and the Python client

=== "CLI"

    ```bash
    # get the data and the suggested spec
    uv run python -m mapi_plugin_science.export e50 \
        --challenges <path to pymc-challenges>/data \
        --out scratch_assay_pc3.nc --spec scratch_assay_pc3.yaml

    # upload, check and fit on the platform
    mapi datasets upload scratch_assay_pc3.nc --name scratch_assay_pc3
    mapi check scratch_assay_pc3.yaml --remote @
    mapi fit scratch_assay_pc3.yaml --remote @

    # a forecast to 72 h
    cat > forecast.yaml <<'EOF'
    apiVersion: mapi/v1
    kind: ScoreRequest
    model: model://biology/scratch-assay-pc3-kpp@latest
    mode: forecast
    horizon: {dim: t, periods: 2}
    output: {kind: expected, summary: [mean, q5, q95]}
    EOF
    mapi score forecast.yaml --remote @ --model <model id> --version 1 -o forecast.zarr.zip

    # the Model Explorer and the comparison
    mapi explore model model://biology/scratch-assay-pc3-kpp@v1 -s params -s fields
    mapi explore compare model://biology/scratch-assay-pc3-kpp@v1 \
        model://biology/scratch-assay-pc3-kpp-no-delay@v1

    # the recipe
    mapi workflows init bayes_workflow_audit --name kpp-audit
    mapi workflows apply kpp-audit.yaml
    mapi workflows run kpp-audit -i data=dataset://biology/scratch_assay_pc3@latest \
        -p model=sci.fisher_kpp@^1 \
        -p 'settings={"steps_per_hour": 10, "delay": true, "max_extension": 4, "diffusivity_median": 1000, "growth_median": 0.05, "capacity_mu": 122, "capacity_sigma": 15}' \
        -p 'bind={"dims": {"t": "hours", "x": "position"}, "vars": {"count": "cells"}}' --watch
    ```

=== "Python"

    ```python
    from mapi_client import Client

    api = Client()  # MAPI_URL, the `mapi login` sign-in (or MAPI_TOKEN) and MAPI_PROJECT

    version = api.datasets.upload("scratch_assay_pc3.nc", dataset="scratch_assay_pc3")
    print(version.ref, version.status.value)

    with open("scratch_assay_pc3.yaml", encoding="utf-8") as fh:
        spec_yaml = fh.read()
    check = api.specs.validate(spec_yaml)
    print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])

    revision = api.specs.upsert(spec_yaml)
    run = api.runs.fit(revision.spec_id, revision.revision)
    run = api.runs.wait(run.id, raise_on_failure=True)

    model = next(m for m in api.models.list() if m.name == "scratch-assay-pc3-kpp")
    mv = api.models.version(model.id, model.latest_version)
    print(mv.ref, mv.metrics)

    result = api.models.score(
        model.id,
        mv.version,
        {
            "model": mv.ref,
            "mode": "forecast",
            "horizon": {"dim": "t", "periods": 2},
            "output": {"kind": "expected", "summary": ["mean", "q5", "q95"]},
        },
    )
    summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
    print(summary["wave_speed"].values)
    print(summary["density"].sel(stat="mean").round(0).values)  # (hours, position)
    ```

The CLI and the client send the same spec and the same score request as the web app.

## 10. Differences from the notebook

- **Part two is not in the platform.** The notebook's sections 5 to 8 use simulated morphogen
  images and FRAP data. They have the exact eigenbasis solution of a linear PDE, the decay
  length `sqrt(D/k)` and the positional information of a cell. No `sci.*` model does this yet.
- **The holdout.** The notebook fits all five times. The spec keeps the 48 h counts back for
  the holdout metrics, then fits again on all the data. The parameters of the refit are
  comparable with the notebook.
- **The model comparison.** The notebook compares the two models with `az.compare` on 152
  pointwise terms (4 times × 38 columns). It also counts the Pareto k values above 0.7. The
  platform gives `elpd_loo` for each version on the same 152 terms, and **Compare** shows the
  versions side by side. It does not give `elpd_diff` and its standard error.
- **The correlations and the posterior predictive plots.** The notebook plots `D` against
  `lam` and `lam` against `K`. The platform gives the marginal intervals. Add `core.pair` to
  `outputs.plots` to see the pairs.
- **The animation.** The notebook draws the density each hour from 200 posterior draws. The
  platform gives the density at the observation times (**Fields**) and at forecast times
  (**Score**). Between the observation times, it gives no values.
- **The sampler.** The spec uses `nutpie`. The notebook uses `pm.sample`. The numbers agree to
  within the Monte Carlo error.

## Next steps

- Do the notebook's "Try it yourself" exercise 1 in part: change the priors (for example
  `capacity_sigma`) and compare the versions. See which conclusions about `D` and `lam` stay.
- Read the reference of the model: [`sci.fisher_kpp`](../../reference/models.md).
- Read [Model Explorer](../../guides/model-explorer.md) and
  [Workflows](../../guides/workflows.md).
