# Track metrics, params and tags <span class="mapi-badge">v0.11</span>

Each run keeps a log of what happened in it. Model code, workflow nodes and you (from a
notebook, a script or the command line) can add to this log. The log has these parts:

- **Metrics.** A metric is a series of points, for example the loss at each step of an
  optimiser. A point without a step is a scalar, for example a business KPI.
- **Params.** A param is a value that does not change, for example the number of draws.
- **Tags.** A tag is a text value that you can change at any time, for example
  `stage=candidate`.
- **The name and the note.** A display name and a free-text note for the run.
- **Structured log lines.** Log lines with a level (`debug`, `info`, `warning` or `error`)
  and fields.
- **Images and tables** <span class="mapi-badge">v0.14</span>. Images (PNG or JPEG),
  matplotlib figures and small tables, for example a sample of predictions at each step.

The run page in the web app shows all of them (the **Metrics**, **Params**, **Media**,
**Logs** and **Notes** tabs). You can search for runs by their metrics, params and tags. To
compare runs and to group them into experiments, see
[Compare runs and use experiments](run-comparison.md).

| See also | |
|---|---|
| [Run tracking and experiments](../concepts/run-tracking.md) | The concepts: what a run records, and how the platform stores and searches it |
| [HTTP API: Run tracking](../reference/http-api.md#run-tracking) | The endpoints, the DTOs and the search grammar |
| [Run protocol: Tracking events](../reference/run-protocol.md#tracking-events) | The `metrics`, `params` and `tags` events and the budgets |
| [Python SDK](../reference/sdk.md#run-tracking) | `FitContext.log_metric` and the other methods |
| [Compare runs and use experiments](run-comparison.md) | The runs table, saved views, the compare page and experiments |

## What the platform tracks for you

You do not have to write code for these items.

**Params of the spec.** Each fit logs these params:

| Param | Value |
|---|---|
| `model` | The model and its version, for example `mmm.bayesian@1.2.0` |
| `fit.engine`, `fit.draws`, `fit.tune`, `fit.chains`, `fit.target_accept`, `fit.seed`, `fit.mode` | Only the fields that the spec sets |
| `data.split` | The kind of split, when the spec has a split |
| `settings.<path>` | One param for each value of the model settings, for example `settings.adstock.l_max`. A list or a dict becomes compact JSON (a maximum of 500 characters). A maximum of 200 settings params. |

**Live chains.** PyMC models (with nutpie or the PyMC NUTS sampler) and the MMMs
(`mmm.bayesian`, `mmm.nested` and `mmm.multivariate`) send the progress of each chain while
they sample. The run page shows the draws, the divergences
and the step size of each chain.

**Step metrics of samplers and optimisers.**

| Source | Keys | Labels | Step |
|---|---|---|---|
| nutpie (PyMC models, `mmm.bayesian`, `mmm.nested`, `mmm.multivariate`) | `step_size`, `num_steps` (leapfrog steps of the latest draw), `divergences` (cumulative) | `chain` | The draws that the chain finished, with tuning (starts at 1) |
| PyMC NUTS sampler (PyMC models, the MMMs with `nuts_sampler: pymc`) | `step_size`, `tree_depth`, `num_steps`, `divergences` (cumulative) | `chain` | The draw index, with tuning (starts at 1). For a long chain, each k-th draw and the last draw, k = ceil(n / 5000). |
| ADVI | `loss` (negative ELBO) | none | The optimisation step (starts at 1). For a long fit, each k-th step, k = ceil(n / 5000). |
| The evaluation fit of `evaluate_then_refit` | All of the above | also `fit=evaluation` | As above. The params of the evaluation fit get the prefix `evaluation.`, for example `evaluation.n_obs`. |

**End-of-run metrics.** The holdout metrics of a fit (for example `rmse` and `elpd_loo`)
become scalar series.

**scikit-learn.** A fit of a `sklearn.*` model logs the hyperparameters of the estimator as
`estimator.<name>` params (only numbers, booleans and text).

**Marketing-mix models.** Each MMM fit logs a summary of its result. See
[What an MMM fit logs](#mmm).

**Causal models and `ts.bsts`.** Each fit logs the effect of the change and the fit before
the change. See [What a causal fit logs](#what-a-causal-fit-logs).

**Neural networks.** The `dl` models log their training curves, a log line for each epoch
and images of the predictions. See [What a network fit logs](#what-a-network-fit-logs).

**Workflows.** The `search.grid` node logs each grid point and its scores. The
`model.backtest` node logs the metrics of each fold. See [Workflow nodes](#workflow-nodes).

## What an MMM fit logs <span class="mapi-badge">v0.12</span> { #mmm }

At the end of a fit, each marketing-mix model (`mmm.bayesian`, `mmm.frequentist`,
`mmm.nested` and `mmm.multivariate`) logs params and scalar metrics (no step). The metrics
come from 200 thinned draws of the posterior (or of the bootstrap). Thus, you can sort and
filter runs by the return of a channel.

**Params.**

| Param | Models | Value |
|---|---|---|
| `mmm.framework_version` | all | The version of mmm-framework |
| `mmm.n_channels` | all | The number of channels |
| `mmm.n_periods` | all | The number of periods of the fit window |
| `mmm.n_cells` | all | The number of panel cells (geo and product combinations; 1 for national data) |
| `mmm.link` | all | `identity`, `log` (`poisson`, `negative_binomial`) or `logit` (`beta`) |
| `mmm.time_varying` | `mmm.bayesian` | The time-varying channels, separated by commas (empty when there are none) |
| `mmm.mediators` | `mmm.nested` | The mediators, separated by commas |
| `mmm.outcomes` | `mmm.multivariate` | The outcomes, separated by commas |
| `mmm.cross_effects` | `mmm.multivariate` | The number of cross-effects |

A param value has a maximum of 6000 characters. If a list of names is longer, the model cuts
it and adds `...` at the end. The fit does not stop because of this.

**Metrics.**

| Metric | Labels | Value |
|---|---|---|
| `roi` | `channel` | The posterior mean of the return on spend: the contribution of the channel over the fit window divided by its spend |
| `roi_p05`, `roi_p95` | `channel` | The 5th and the 95th percentile of the return on spend |
| `mroi` | `channel` | The marginal return: the extra KPI for each extra unit of spend at the current spend (a central difference at 1 ± 1%) |
| `contribution_share` | `channel` | The part of the total KPI that the channel gives |
| `baseline_share` | none | The part of the total KPI with no media |

A channel with no spend gets no `roi`. The label value is the channel name with characters
other than letters, digits, `_`, `.` and `-` changed to `_` (`TV spend` becomes `TV_spend`).
For `mmm.multivariate`, the metrics use the weighted total of the outcomes. It also logs a
`roi` for each outcome, with the labels `channel` and `outcome`.

Find the runs where TV returns more than 2:

```bash
mapi runs ls --filter "metric.roi{channel=tv} > 2" --column "metric.roi{channel=tv}"
```

## What a causal fit logs <span class="mapi-badge">v0.13</span> { #what-a-causal-fit-logs }

At the end of a fit, `ts.bsts` and the causal models (`causal.impact`,
`causal.synthetic_control` and `causal.did`) log params and scalar metrics (no step). Their
samplers also send live chains and step metrics, as the other PyMC models do. The causal
metrics come from the `impact` group of the bundle (a maximum of 500 draws), in the units of
the target.

**Params of `ts.bsts` and `causal.impact`.**

| Param | Value |
|---|---|
| `bsts.trend` | `level`, `local_linear` or `smooth` |
| `bsts.seasonality` | The seasonal periods, separated by commas. `x3` gives the harmonics, for example `12,52.18x3`. |
| `bsts.regressors` | The drivers (the control series of `causal.impact`), separated by commas |
| `bsts.n_states` | The number of states of the model |
| `bsts.n_periods`, `bsts.missing_periods` | The number of periods of the fit, and the number of periods without a value |

**Metrics of `ts.bsts`.**

| Metric | Value |
|---|---|
| `sigma_obs`, `sigma_level` | The posterior means of the observation noise and of the level innovations, in the units of the target |
| `one_step_rmse` | The error of the one-step-ahead predictions in the fitted range |
| `one_step_coverage_90` | The part of the observed values in the 90% interval of the one-step-ahead predictions |

**Params of the causal models.**

| Param | Models | Value |
|---|---|---|
| `causal.intervention_start`, `causal.intervention_end` | all | The dates (or period numbers) of the change. The end is empty when the change did not stop. |
| `causal.n_pre`, `causal.n_post` | all | The number of periods before the start and in the effect window |
| `causal.n_treated` | all | The number of treated units (1 for `causal.impact`) |
| `causal.n_donors`, `causal.weights` | `causal.synthetic_control` | The number of donor markets, and `simplex` or `horseshoe` |
| `causal.design`, `causal.staggered`, `causal.leads`, `causal.lags` | `causal.did` | The design, if the units got the change at different times, and the event window |

**Metrics of the causal models.**

| Metric | Labels | Value |
|---|---|---|
| `effect_total` | none (`unit` with two or more treated units) | The mean of the total effect over the effect window |
| `effect_total_sd`, `effect_total_p05`, `effect_total_p95` | none | The sd, the 5th and the 95th percentile of the total effect |
| `effect_average` | none | The mean effect in one period |
| `effect_relative` | none (`unit` with two or more treated units) | The total effect divided by the total of the values without the change |
| `p_effect_positive` | none (`unit` with two or more treated units) | The probability that the total effect is more than zero |
| `pre_rmse`, `pre_mape` | none | The error of the model before the change |
| `donor_weight` | `unit`, `donor` | `causal.synthetic_control`: the posterior mean of the weight, for the 10 largest donors of each treated unit |
| `att`, `att_p05`, `att_p95` | none | `causal.did`: the average effect after the change, with its interval |
| `event_coef` | `event_time` | `causal.did`: the posterior mean of the coefficient for each period relative to the change |

For the models, see [Causal models and Bayesian structural time series](../models/causal.md).
For the procedures, see [Measure the effect of a change](causal.md).

Find the geo-lift tests with an effect that is more than zero with a probability of 95% or
more:

```bash
mapi runs ls --filter "metric.p_effect_positive >= 0.95" --column metric.effect_total
```

## What a network fit logs <span class="mapi-badge">v0.15</span> { #what-a-network-fit-logs }

The `dl` models of `mapi-jax` log the training while it runs:

- `loss` and `accuracy` (classification) or `rmse` and `mae` (regression), with the label
  `split=train` (batch values) and `split=val` (at the end of each evaluation);
- `lr`, `grad_norm` and `epoch`;
- one log line for each epoch, with the fields `epoch`, `train_loss`, `val_loss` and
  `images_per_second`;
- the params `dl.arch.*`, `dl.train.*`, `dl.backend`, `dl.device` and `dl.parameters`;
- for a classification, the media `val_predictions` and `confusion` (label `split=val`) at a
  maximum of 20 evaluations.

See [Deep learning](deep-learning.md#read-the-training).

## Keys, labels and values

Metric keys match `^[a-z][a-z0-9_]{0,63}$`. Write qualifiers as labels, not in the key:

| Do not write | Write |
|---|---|
| `train/loss` | key `loss`, labels `{"split": "train"}` |
| `rmse_fold_3` | key `rmse`, labels `{"fold": "3"}` |
| `chain0.step_size` | key `step_size`, labels `{"chain": "0"}` |

These rules apply:

- A series is one key and one set of labels. `loss` and `loss` with `{"split": "val"}` are
  two series.
- A series has a maximum of 8 labels. A label key matches `^[a-z][a-z0-9_]{0,31}$`. A label
  value matches `^[A-Za-z0-9_.\-]{1,63}$`.
- A metric value must be a finite number. The SDK ignores NaN and infinity.
- A param key and a tag key can have letters, digits, `_`, `.`, `-` and `/` (for example
  `settings.adstock.l_max`). A param value is a boolean, a number or a text of a maximum of
  6000 characters.
- A param does not change. If you log a param again with the same value, nothing occurs. A
  different value through the API is an error (409). A different value from the runner is
  dropped, and the first value stays.
- A tag value is a text of a maximum of 1000 characters. The last value is kept. Tag keys
  that start with `mapi.` are for the platform only.
- A step is an integer, 0 or more. For a stepped series, the first value of a step is kept.
  For a scalar series (no step), the newest value is kept.

## Log from a model plugin

`FitContext` and `ScoreContext` have the logging methods. The SDK does a check of each key
and each label when you call the method, also in a notebook without a run. Thus a bad key
fails at the line that has the error.

```python
class MyModel(CustomModel):
    def fit(self, data, ctx):
        ctx.log_param("solver", "lbfgs")
        ctx.set_tag("family", "gaussian")
        for step in range(1, n_steps + 1):
            loss = self._update()
            ctx.log_metric("loss", loss, step=step)
        ctx.log_metrics({"rmse": rmse, "mae": mae}, labels={"split": "train"})
        ctx.log_line("refit done", level="info", fields={"iterations": n_steps})
        ...
```

| Method | What it does |
|---|---|
| `log_metric(key, value, step=None, labels=None)` | Log one point |
| `log_metrics(values, step=None, labels=None)` | Log some metrics with the same step and labels |
| `log_param(key, value)`, `log_params(params)` | Log params |
| `set_tag(key, value)`, `set_tags(tags)` | Set tags |
| `log_line(message, level="info", fields=None)` | Write a structured log line (a maximum of 16 fields) |
| `chains(chains)` | Report the progress of each chain (`mapi_spec.run.ChainProgress`) |
| `tracking` | `True` when the platform records the values. Use it to skip metrics that take a long time to calculate. |

The methods are thread-safe. A sampler callback on a different thread can call them.

Inside a run, the values go to the platform each second. The fit also writes them into the
bundle: the model group `metrics_history` keeps the stepped series, the scalars, the params
and the tags. The `core.loss_curve` plot reads this group. For the layout, see
[Bundles](../concepts/bundles.md#metrics-history).

## Log images, figures and tables <span class="mapi-badge">v0.14</span> { #log-images-figures-and-tables }

A run can log images, matplotlib figures and small tables. Each item has a key, and can have
a step, labels and a caption, as a metric point has. Use the same key for the same picture at
different steps, for example the predictions on a sample at each epoch.

```python
class MyModel(CustomModel):
    def fit(self, data, ctx):
        for epoch in range(1, n_epochs + 1):
            ...
            if ctx.tracking_media:  # skip the work when nothing records the images
                ctx.log_image("samples", grid, step=epoch, labels={"split": "val"},
                              caption=f"Predictions after epoch {epoch}")
        fig = plot_residuals(...)
        ctx.log_figure("residuals", fig)
        ctx.log_table("confusion_counts", counts_df, caption="Holdout counts per class")
```

| Method | Accepts |
|---|---|
| `log_image(key, image, step=None, labels=None, caption=None)` | A numpy array: `(height, width)` gray, `(height, width, 1)`, `(height, width, 3)` RGB or `(height, width, 4)` RGBA. `uint8` values as they are, booleans as 0 and 255, other integers from 0 to 255, floats from 0 to 1 (NaN becomes 0). Also PNG or JPEG bytes, a Pillow image or an `xarray.DataArray` with the dims `(y, x)` or `(y, x, channel)`. |
| `log_figure(key, figure, step=None, labels=None, caption=None)` | A matplotlib figure. The SDK saves it as a PNG file (100 dpi). |
| `log_table(key, table, step=None, labels=None, caption=None)` | A pandas DataFrame, a dict of columns or a list of records. Numbers, booleans, text and empty values. NaN and infinity become empty values. Dates become ISO text. |
| `tracking_media` | `True` when the platform records images. A figure takes time to draw: use this property to skip it. |

The SDK does a check of the key, the labels, the step and the caption at each call, also
without a run. A bad key fails at the line that has the error. Media keys follow the rules of
metric keys. When nothing records the media (a notebook without a run, or a run that has no
media upload), the call does not encode the image.

Limits:

| Limit | Value |
|---|---|
| Size of one image | 8 MiB, a maximum of 4096 pixels on each side |
| Size of one table | 1 MiB, a maximum of 10 000 rows, 64 columns and 200 000 cells. A cell has a maximum of 1000 characters. |
| Caption | 500 characters |
| Items in the run | 1000 |
| Bytes in the run | 256 MiB |

The limits count the items of all attempts of the run and the items logged through the
API. If a run logs more, the platform does not keep the extra items and each attempt gets one
`MEDIA_BUDGET` warning. The run does not fail. An item that replaces another item (the same
key, labels and step) frees the bytes of the old item.

**What the platform does with an image.** The worker writes the file in its scratch
directory. The supervisor reads the file, calculates its SHA-256 and does a check of the
format and the size. Then it uploads the file. The server downloads the file again, does the
same checks, and keeps a copy in the bundles bucket. The platform accepts only PNG and JPEG
images (no SVG, no GIF). For the details, see
[Run protocol: Media](../reference/run-protocol.md#media).

**Where you see the media.**

- **Web app.** The **Media** tab of the run shows each key. A slider selects the step. A
  select shows one value of a label. A table shows its first 200 rows. The
  [compare page](run-comparison.md#compare-runs) shows the images of several runs side by
  side.
- **Python.** `api.runs.media(run_id)` lists the items. `api.runs.download_media(run_id,
  item)` gets the bytes and does a check of the size, the CRC32C and the SHA-256.
- **Command line.** `mapi runs media RUN` lists the items. `--download DIR` writes the files.

**Log media from a notebook.** You can add an image or a table to a run that already exists,
also after it finished. One request has a maximum of 4 MiB.

```python
api.runs.log_image(run_id, "holdout_map", "map.png", caption="Holdout error by region")
api.runs.log_table(run_id, "lift_by_region", {"region": ["north", "south"], "lift": [0.04, 0.02]})
```

```bash
mapi runs log-image run_01… holdout_map map.png --step 3 --label split=val --caption "Holdout error"
```

## Log from a workflow node { #workflow-nodes }

The `NodeContext` of a workflow node has the same methods. The values go to the run of the
node.

`search.grid` logs these items in its search run. The search run is the first item in the
`child_runs` of the node. The `run_id` of the node is the refit of the winner, which does
not have these items.

- The params `grid.metric`, `grid.direction`, `grid.points` and `grid.nested`.
- One param for each value of each grid point: `trial.<i>.<pointer>`, for example
  `trial.3.adstock/l_max = 4`.
- The scores of grid point `i` as metrics with step `i` (`mae`, `rmse`, `mape`, `smape`,
  `mase`, `coverage_90`, when they are present). A nested search adds the labels
  `{"stage": "inner", "fold": "<j>"}` to the inner scores. The outer scores have the label
  `{"stage": "outer"}` and step `j`.
- The scalar `best_<metric>` (for example `best_rmse`) and the tag `grid.best_point`.
- A `warning` log line for each grid point that failed.

`model.backtest` logs the metrics of each fold as scalars with the label `{"fold": "<j>"}`.
The inner fits of a backtest do not log step metrics.

## Log from a notebook or a script

You can add metrics, params and tags to a run that already exists, also after it finished.
For example, log the revenue lift that you measured after you deployed the model. You need
the runner role. An API token needs the `runs:write` scope.

```python
from mapi_client import Client

api = Client()  # MAPI_URL, MAPI_TOKEN and MAPI_PROJECT, or a profile
run_id = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"

api.runs.log_metric(run_id, "revenue_lift", 0.042, labels={"region": "north"})
api.runs.log_params(run_id, {"analyst": "kim", "holdout_weeks": 8})
api.runs.set_tags(run_id, {"stage": "production", "old_tag": None})  # None deletes a tag
api.runs.update(run_id, name="weekly sales, adstock 4", note="Holiday weeks removed.")

with api.runs.track(run_id) as t:      # sends the points in batches
    for week, value in weekly_kpi:
        t.log_metric("weekly_lift", value, step=week)
```

`runs.log_metrics` sends a maximum of 1000 points in each request. It divides a larger
list. `runs.track` keeps the points and sends them when 500 wait and when the `with` block
ends.

The same with the command line:

```bash
mapi runs log-metric run_01… revenue_lift 0.042 --label region=north
mapi runs log-param run_01… analyst kim
mapi runs tag run_01… stage=production --delete old_tag
mapi runs rename run_01… "weekly sales, adstock 4"
mapi runs note run_01… "Holiday weeks removed."
```

The same with HTTP: `POST /v1/runs/{run_id}/metrics`, `POST /v1/runs/{run_id}/params` and
`PATCH /v1/runs/{run_id}` (see [HTTP API](../reference/http-api.md#run-tracking)).

## Read the tracking data

- **Web app.** Open the run. The **Metrics** tab shows a chart for each key. Use **Colour
  by** to give each chain (or each other label) a colour. The **Params** tab shows the params
  and the tags. The **Logs** tab has a level filter. The **Notes** tab changes the name, the
  tags and the note.
- **Python.** `api.runs.metrics(run_id)` lists the series with their summaries (`last`,
  `min`, `max`, `count`). `api.runs.metric_history(run_id, "step_size", max_points=500)`
  gets the points. `api.runs.params(run_id)` lists the params.
- **Command line.** `mapi runs metrics RUN`, `mapi runs metrics RUN --key loss` and
  `mapi runs params RUN`.
- **Several runs.** The [compare page](run-comparison.md#compare-runs) shows the curves of
  2 to 10 runs on one chart.

The server downsamples a long series to `max_points` (the default is 1000). It keeps the
shape of the curve, the first point and the last point.

## Search runs

`GET /v1/runs`, `api.runs.list(...)` and `mapi runs ls` can filter, sort and show runs by
their tracking data:

```bash
mapi runs ls --filter 'metric.rmse < 12' --filter 'param.fit.engine = nutpie' \
  --order-by metric.rmse --column metric.rmse --column param.settings.adstock.l_max
```

```python
runs = api.runs.list(
    filters=["metric.loss{split=val}.min < 0.3", "tag.stage = candidate"],
    order_by="-metric.elpd_loo",
    columns=["metric.elpd_loo", "param.fit.draws"],
)
```

A metric filter compares the summary of one exact series. Add `.min`, `.max` or `.count` to
compare a value different from the last value. A run that does not have the series does not
match. For the full grammar, see [Search runs](../reference/http-api.md#search-runs).

In the web app, the runs table shows tracked columns and has a filter bar. You can save a
search as a view. See [Compare runs and use experiments](run-comparison.md#find-runs).

## Budgets

Each run has budgets for its tracking data. The defaults are:

| Budget | Default |
|---|---|
| Points in one series | 10 000 |
| Points in the run | 500 000 |
| Series in the run | 1000 |
| Params | 5000 |
| Tags | 100 |
| Structured log lines | 10 000 lines, 2 MiB |
| Images and tables <span class="mapi-badge">v0.14</span> | 1000 items, 256 MiB |

If a run logs more, the platform does not keep the extra points and the run gets one
`TRACKING_BUDGET` warning. The run does not fail. A worker that sends more than 64 MiB of
tracking data fails with `SANDBOX_VIOLATION`. An API write that is more than the budget gets
`429 QUOTA_EXCEEDED`.

!!! note
    Log curves with a step that grows slowly, for example each 10th step. The chart shows a
    maximum of 1000 points for each series.

## Retention

An operator can set `MAPI_TRACKING_RETENTION_DAYS`. Then the server deletes the metric points
and the structured log lines of runs that finished more days ago than the setting. The server
keeps the summaries, the params, the tags, the name and the note. It also keeps the value of
each metric without a step. Thus search continues to work for old runs. If you log more points
to a series after this, the server adds them to the summary that it kept (count, minimum,
maximum and the last value). See [Configuration](../operations/configuration.md).

The retention setting does not delete images and tables. The server keeps them until you
delete the tenant <span class="mapi-badge">v0.14</span>.
