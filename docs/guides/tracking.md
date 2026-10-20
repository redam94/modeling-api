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

The run page in the web app shows all of them (the **Metrics**, **Params**, **Logs** and
**Notes** tabs). You can search for runs by their metrics, params and tags.

| See also | |
|---|---|
| [HTTP API: Run tracking](../reference/http-api.md#run-tracking) | The endpoints, the DTOs and the search grammar |
| [Run protocol: Tracking events](../reference/run-protocol.md#tracking-events) | The `metrics`, `params` and `tags` events and the budgets |
| [Python SDK](../reference/sdk.md#run-tracking) | `FitContext.log_metric` and the other methods |

## What the platform tracks for you

You do not have to write code for these items.

**Params of the spec.** Each fit logs these params:

| Param | Value |
|---|---|
| `model` | The model and its version, for example `mmm.bayesian@1.2.0` |
| `fit.engine`, `fit.draws`, `fit.tune`, `fit.chains`, `fit.target_accept`, `fit.seed`, `fit.mode` | Only the fields that the spec sets |
| `data.split` | The kind of split, when the spec has a split |
| `settings.<path>` | One param for each value of the model settings, for example `settings.adstock.l_max`. A list or a dict becomes compact JSON (a maximum of 500 characters). A maximum of 200 settings params. |

**Live chains.** PyMC models (with nutpie or the PyMC NUTS sampler) and `mmm.bayesian` send
the progress of each chain while they sample. The run page shows the draws, the divergences
and the step size of each chain.

**Step metrics of samplers and optimisers.**

| Source | Keys | Labels | Step |
|---|---|---|---|
| nutpie (PyMC models, `mmm.bayesian`) | `step_size`, `num_steps` (leapfrog steps of the latest draw), `divergences` (cumulative) | `chain` | The draws that the chain finished, with tuning (starts at 1) |
| PyMC NUTS sampler (PyMC models, `mmm.bayesian` with `nuts_sampler: pymc`) | `step_size`, `tree_depth`, `num_steps`, `divergences` (cumulative) | `chain` | The draw index, with tuning (starts at 1). For a long chain, each k-th draw and the last draw, k = ceil(n / 5000). |
| ADVI | `loss` (negative ELBO) | none | The optimisation step (starts at 1). For a long fit, each k-th step, k = ceil(n / 5000). |
| The evaluation fit of `evaluate_then_refit` | All of the above | also `fit=evaluation` | As above. The params of the evaluation fit get the prefix `evaluation.`, for example `evaluation.n_obs`. |

**End-of-run metrics.** The holdout metrics of a fit (for example `rmse` and `elpd_loo`)
become scalar series.

**scikit-learn.** A fit of a `sklearn.*` model logs the hyperparameters of the estimator as
`estimator.<name>` params (only numbers, booleans and text).

**Workflows.** The `search.grid` node logs each grid point and its scores. The
`model.backtest` node logs the metrics of each fold. See [Workflow nodes](#workflow-nodes).

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
  different value is an error.
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
