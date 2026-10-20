---
description: What a run records about itself - metrics, params, tags, notes, structured log lines, images and tables - how the platform stores and searches them, and how experiments, saved views and the compare page group and compare runs.
---

# Run tracking and experiments <span class="mapi-badge">v0.11</span>

Each run keeps a log of what happened in it. Model code, workflow nodes and you (from a
notebook, a script or the command line) can add to this log. The platform keeps the log next
to the run, not in the bundle only. Thus you can search, sort and compare runs by their
results, also runs that made no model version.

This page tells about the parts of the log and how they fit together. The guides give the
procedures:

| Guide | What it tells |
|---|---|
| [Track metrics, params and tags](../guides/tracking.md) | How to log from model code, a workflow node, a notebook or the command line. What the built-in models log. |
| [Compare runs and use experiments](../guides/run-comparison.md) | The runs table, saved views, the compare page and experiments |
| [HTTP API: Run tracking](../reference/http-api.md#run-tracking) | The endpoints, the DTOs and the search grammar |
| [Run protocol: Tracking events](../reference/run-protocol.md#tracking-events) | The events of the sandbox, the budgets and the media path |

## The parts of the log

| Part | What it is | Can it change? |
|---|---|---|
| **Metric** | A series of points: one key, a set of labels and a value for each step. A point without a step is a scalar. | The platform adds points. For a stepped series, the first value of a step stays. For a scalar series, the newest value stays. |
| **Param** | A value that describes the run, for example `fit.draws` | No. A param is written one time. The same value again does nothing. Through the API, a different value is an error (409). From the runner, a different value is dropped and the first value stays. |
| **Tag** | A text value, for example `stage=candidate` | Yes. The last value stays. Keys that start with `mapi.` are for the platform only. |
| **Name and note** | A display name (200 characters) and a free-text note (20 000 characters) | Yes |
| **Structured log line** <span class="mapi-badge">v0.11</span> | A log line with a level (`debug`, `info`, `warning` or `error`) and fields | No |
| **Media item** <span class="mapi-badge">v0.14</span> | A PNG or JPEG image, a matplotlib figure (kept as PNG) or a small JSON table | The same bytes at the same key, labels and step are kept one time. Other bytes replace the item. |

**Labels, not long keys.** A metric key matches `^[a-z][a-z0-9_]{0,63}$`. The qualifiers go in
labels, a maximum of 8. For example, write the key `loss` with the label `split=val`, not
`val_loss`. A series is one key and one set of labels. Thus `loss{split=train}` and
`loss{split=val}` are two series of one key. The charts colour the lines by a label, and the
search can select one series.

## Who writes to the log

| Writer | How | Source |
|---|---|---|
| Model code in a fit or a score run | `ctx.log_metric`, `log_params`, `set_tag`, `log_line`, `log_image`, `log_figure`, `log_table` on `FitContext` and `ScoreContext` | `runner` |
| A workflow node | The same methods on `NodeContext` | `runner` |
| The platform | The params of the spec, the holdout metrics, the progress of each chain, the step metrics of the samplers and the summaries of the built-in models | `runner` |
| You | `api.runs.log_metric`, `log_params`, `set_tags`, `log_image`, `log_table` (Python), `mapi runs log-metric`, `log-param`, `tag`, `log-image` (command line), or the HTTP API | `api` |

You can write to a run also after it finished. For example, a script can log a business KPI
to the fit run of a model version some weeks after the deployment. To write through the API,
you need the runner role or a token with the `runs:write` scope
([Tenancy and authentication](tenancy-and-auth.md#scopes)).

The methods of the contexts do a check of each key and label when you call them, also in a
notebook without a run. Thus a key that is not correct fails in your notebook, not later on
the platform.

## How the platform stores the log

The sandbox does not write to the database. The worker sends its points to the supervisor.
The supervisor sends signed `metrics`, `params`, `tags` and `media` events, as it does for
progress. The orchestrator verifies each event. Then it writes the values to the tracking
tables, not to the event list of the run. The event stream of the run sends a `tracking` frame
when the values change. Thus the run page updates while the run continues.

| Table | Holds |
|---|---|
| `run_metrics` | Each metric point: run, attempt, key, labels, step, time and value. A scalar point has the step `-1`. |
| `run_metric_summary` | One row for each series of the newest attempt that wrote it: the count, the first and last time, the last value and step, the minimum and the maximum. Search and sort use this table. |
| `run_params` | Each param, as text and, if it is a number, also as a number |
| `run_tags` | The tags of each run |
| `run_media` <span class="mapi-badge">v0.14</span> | One row for each media item. The bytes are in the bundles bucket. |
| `run_logs` | Also the `user` log lines, with their `level` and `fields` |

All these tables use the row-level security of each tenant. Their rows go with their run.

A fit also writes its logged values into the bundle, in the group `/model/metrics_history`
([Bundles](bundles.md#metrics-history)). The plot `core.loss_curve` reads this group. Thus a
model version keeps its training curves also outside the platform.

### Media go through the same checks as outputs { #media }

A logged image or table does not go into the event. The supervisor uploads the bytes through
a create-only POST policy on the `media/` prefix of the attempt. Then it sends a `media` event
with the SHA-256, the size and the format. The server downloads the object and calculates the
SHA-256 again. It also does a check of the size and the format: it reads the PNG or JPEG
header, but it does not decode the pixels. Then it copies the bytes create-only to the bundles
bucket. If a check fails, the server keeps nothing and writes the warning `MEDIA_REJECTED`.
See [Security model](security.md#run-media-and-uploads).

## Budgets and retention

Each run has budgets for its tracking data (`RunLimits.tracking`):

| Budget | Default |
|---|---|
| Points in one series | 10 000 |
| Points in the run | 500 000 |
| Series in the run | 1000 |
| Params | 5000 |
| Tags | 100 |
| Structured log lines | 10 000 lines, 2 MiB |
| Media items <span class="mapi-badge">v0.14</span> | 1000 items, 256 MiB, 8 MiB for one item |

A run that logs more does not fail. The platform does not keep the extra values and writes
one `TRACKING_BUDGET` (or `MEDIA_BUDGET`) warning. A worker that sends more than 64 MiB of
tracking data stops with `SANDBOX_VIOLATION`, as for a flood of log lines. An API write that
is more than a budget gets `429 QUOTA_EXCEEDED`.

An operator can set `MAPI_TRACKING_RETENTION_DAYS`. The server then deletes the metric points
and the structured log lines of old runs. It keeps the summaries, the scalar values, the
params, the tags, the name and the note. Thus search continues to work for old runs. The
setting does not delete media. The platform keeps media until you delete the tenant.

## Search and columns

The summaries make the runs searchable. `GET /v1/runs`, `api.runs.list` and `mapi runs ls`
take filters, a sort order and columns:

```text
metric.rmse < 12
metric.loss{split=val}.min < 0.3
param.fit.engine = nutpie
tag.stage = candidate
```

A metric filter compares the summary of one exact series. Add `.min`, `.max` or `.count` to
compare a value that is not the last value. A run that does not have the series does not
match. A request that is not correct gets `422 SCHEMA_INVALID`. For the full grammar, see
[Search runs](../reference/http-api.md#search-runs).

## Experiments, saved views and the compare page <span class="mapi-badge">v0.14</span> { #experiments }

Three items help you to work with many runs:

- An **experiment** is a named group of runs in a project. It has a description and its own
  saved views. A run is in one experiment or in no experiment. You can move a run into an
  experiment or out of it at any time.
- A **saved view** is a search of the runs table under a name: the filters, the sort order,
  the columns and the tags. The views are shared in the project, a maximum of 200 in a
  project. A view can belong to an experiment.
- The **compare page** (`GET /v1/run-compare`) shows 2 to 10 runs side by side: their
  summaries, the curves of their metrics, the params that are different and their media.

An experiment is not a property of the spec, because the runs of one study often have
different specs or no spec. For example, a study can compare an MMM with a benchmark model,
and add the runs of a `search.grid` node and runs that you logged from a notebook.

These rules keep the groups correct:

- A workflow node run goes into the experiment of its workflow run.
- A retry goes into the experiment of the run that it retries.
- An archived experiment accepts no new runs and no retries. You can still read and compare
  its runs.
- When you delete an experiment, its runs stay. They are then in no experiment. The saved
  views of the experiment are deleted.
- Experiments and saved views use the permissions of runs. A viewer can read them. A runner
  (or a token with `runs:write`) can make, change and delete them.

!!! note "The `Sweep` document is retired"
    From v0.14, the server refuses new `Sweep` documents (`422 SCHEMA_INVALID` at `/kind`).
    Stored `Sweep` documents still load. Use a workflow with a `search.grid` node. Each grid
    point logs its scores as metrics, with the number of the point as the step
    ([Workflows](workflows.md#tracking-in-a-workflow)).

## Known gaps

- A fit without a run (for example `fit_spec` in a notebook) keeps no logged media. To keep
  the items in memory, give the fit a recorder: `FitHooks(tracker=MetricRecorder())`.
- Workflow schedules and triggers start their runs in no experiment. To group such a run,
  move it with `PATCH /v1/runs/{run_id}` before it starts its nodes.
- numpyro and blackjax samplers do not send the progress of each chain.
- The platform keeps media until you delete the tenant. There is no retention setting for
  media.

Next: [Image data and collections](image-collections.md).
