---
description: Find runs by their metrics, params and tags, save the search as a view, compare runs side by side (curves, metric bars, param differences, plots and images), and group runs into experiments.
---

# Compare runs and use experiments <span class="mapi-badge">v0.14</span>

When you try different settings of a model, you get many runs. This guide tells you how to
find the runs that you need, how to compare them, and how to keep related runs together.

- **Find runs.** The runs table shows metrics, params and tags as columns. You can filter and
  sort the table by these values.
- **Saved views.** A saved view keeps a search of the runs table (the filters, the sort order
  and the columns) under a name. All members of the project can use it.
- **Compare runs.** The compare page shows 2 to 10 runs side by side: metric curves, metric
  bars, the params that are different, the plots and the images.
- **Experiments.** An experiment is a named group of runs, for example "adstock length
  study".

The runs must log their metrics, params and tags first. See
[Track metrics, params and tags](tracking.md).

| See also | |
|---|---|
| [HTTP API: Comparison, experiments and saved views](../reference/http-api.md#v014) | The endpoints and the DTOs |
| [Search runs](../reference/http-api.md#search-runs) | The grammar of filters, sort orders and columns |
| [Images, figures and tables](tracking.md#log-images-figures-and-tables) | Log images that the compare page shows |

## Find runs

Open **Runs** in the web app. The toolbar has these controls:

- **Experiment**: show only the runs of one experiment.
- **State** and **Kind**: show only the runs in one state or of one kind.
- **Name**: show only the runs with this text in their name.
- **Filters**: type a filter and push Enter. Each filter shows as a chip. Click the cross on
  a chip to remove the filter.
- **Columns**: add metrics, params and tags as columns of the table.
- **Views**: apply, save, rename or delete a saved view.

The address of the page holds the search. Thus you can copy the address and send it to a
colleague, or reload the page.

### Filters

A filter compares a value of each run with a value that you type. Examples:

| Filter | Shows the runs where |
|---|---|
| `metric.rmse < 3` | The last value of the series `rmse` is less than 3 |
| `metric.loss{split=val}.min < 0.3` | The minimum of the `loss` series with the label `split=val` is less than 0.3 |
| `param.lr = 0.01` | The param `lr` has the text `0.01` |
| `param.fit.draws >= 1000` | The param `fit.draws` is a number of 1000 or more |
| `tag.team = growth` | The tag `team` has the value `growth` |
| `name ~ adstock` | The name contains `adstock` (case is ignored) |

A run that does not have the metric, the param or the tag does not match. The web app does a
check of each filter when you type it. If the server does not accept a filter, its message
shows below the filter bar. For the full grammar, see
[Search runs](../reference/http-api.md#search-runs). A search has a maximum of 8 filters.

### Columns

Click **Columns** to open the column picker. The picker lists the metric series, the param
keys and the tag keys of the runs of the project (or of the experiment), the most frequent
first. Type to find a key.

- For a metric, select the value to show: **last** (the default), **min**, **max** or
  **count**.
- A metric series with labels is one column, for example `metric.loss{split=val}`.
- The table has a maximum of 16 tracked columns.

Click the header of a tracked column to sort the runs by it. Click again to change the
direction. A third click removes the sort. Runs that do not have the value go to the end.
Click **Load more runs** to show more runs.

!!! note
    Params and tags are text from the runs. The table shows them as text only.

## Saved views

A saved view keeps the search of the runs table: the experiment, the state, the kind, the
name, the tags, the filters, the sort order and the columns.

1. Set the search in the runs table.
2. Click **Views**, then **Save as a new view**.
3. Type a name. Names are unique in a project (case is ignored). The name of a view in an
   experiment must be unique in the experiment.
4. Click **Save**.

To apply a view, click **Views** and select the view. The app writes the search of the view
in the address. To change a view, change the search, then click **Views** and **Save the
changes to the view**.

A project has a maximum of 200 saved views. The server does a check of each filter, sort
order and column when you save the view. When you delete an experiment, the server also
deletes the views of the experiment.

## Compare runs

1. In the runs table, select 2 to 10 runs with the check boxes.
2. Click **Compare**.

The compare page (`/runs/compare?runs=...`) has a chip for each run in a fixed colour. Click
a chip to open the run. Click the cross on a chip to remove the run. Click **Add run** to add
a run. The page has these sections:

| Section | What it shows |
|---|---|
| **Curves** | One chart for each metric key, with one line for each run. Select a label (for example `split`) to draw each value of the label with a different dash. The default keys are the 12 keys that most of the runs have. Use the key picker to change them. Select the log scale for values that cover a large range. |
| **Metric bars** | A bar for each run for each metric, with the value that you select: last, min or max. The best run has a mark. For keys such as `loss`, `error`, `rmse`, `mae`, `mape` and `brier`, the best value is the minimum. For other keys, it is the maximum. The text below the chart tells which rule it used. |
| **Params** | A table with one row for each param key and one column for each run. **Show only differences** (on by default) hides the params that have the same value in all runs. An empty cell is a param that the run did not log. |
| **Plots** | The plots that two or more of the runs have, side by side. One row for each plot, one column for each run. |
| **Media** | The images that the runs logged. Select a media key. Move the step slider: each run shows its image at the largest step that is not more than the slider. |

The server sends a maximum of 300 points for each curve. It keeps the shape of the curve, the
first point and the last point.

### Compare runs from Python and the command line

```python
from mapi_client import Client

api = Client()
cmp = api.runs.compare(["run_01…", "run_02…"], keys=["loss", "rmse"], media_keys=["samples"])
for p in cmp.params:
    if p.differs:
        print(p.key, p.values)
for metric in cmp.metrics:
    for s in metric.series:
        print(metric.key, s.run_id, s.labels, s.values[-1])
```

```bash
mapi runs compare run_01… run_02… --key loss --key rmse --agg min
```

The command shows a table of the metrics (one row for each key, one column for each run).
Then it shows the params that are different. `--all-params` shows all params.

## Experiments

An experiment is a named group of runs in a project. A run is in one experiment or in no
experiment. Use an experiment for a study: for example, the runs that compare three adstock
lengths, and the benchmark model that you compare them with.

### Why an experiment and not the spec

The runs table can also filter by spec. But the runs of a study often do not have the same
spec:

- Score runs, workflow nodes, `search.grid` trials and runs that you log from a notebook
  have no spec, or a different spec.
- To compare two models (an MMM and a benchmark), you compare runs of two specs.
- An experiment has a name and a description. It also has its own saved views.
- You can move a run into an experiment or out of it at any time. You cannot change the spec
  of a run.

Tags are not a good replacement: a tag has no description, no list of all groups, and a typo
makes a new group.

### Make an experiment and add runs

In the web app:

1. Open **Experiments**.
2. Click **New experiment**.
3. Type the name and, if necessary, a description. Click **Create**.
4. In the runs table, select the runs. Click **Add to experiment** and select the
   experiment.

The experiment page shows the description, the runs table of the experiment and its saved
views. To move one run, open the run, go to the **Notes** tab and select the experiment.

From the command line:

```bash
mapi experiments create "adstock length study" --description "Adstock 2, 4 and 8 weeks."
mapi experiments add "adstock length study" run_01… run_02… run_03…
mapi fit spec.yaml --remote @ --experiment "adstock length study"
mapi runs ls --experiment "adstock length study" --column metric.elpd_loo
mapi experiments ls
```

From Python:

```python
exp = api.experiments.create("adstock length study")
api.experiments.add_runs(exp.id, ["run_01…", "run_02…"])
run = api.runs.fit(spec_id, revision, experiment_id=exp.id)
runs = api.runs.list(experiment_id=exp.id, columns=["metric.elpd_loo"])
```

### Rules

- Experiment names are unique in a project (case is ignored). A name has a maximum of 200
  characters.
- A workflow node run goes into the experiment of its workflow run. Name the experiment when
  you start the workflow run: `experiment_id` in `POST /v1/workflows/{workflow_id}/runs`,
  `experiment_id=` in `api.workflows.run`, or `mapi workflows run --experiment`. If you move
  a workflow run later, the node runs that it already started stay where they are.
- A retry goes into the experiment of the run that it retries.
- An archived experiment does not accept new runs. You can still list, filter and compare its
  runs. To add runs again, unarchive it. This includes retries: the server refuses to retry a
  run of an archived experiment (`409`).
- When you delete an experiment, its runs stay. They are then in no experiment. The server
  deletes the saved views of the experiment.
- One request adds or removes a maximum of 200 runs. The server ignores ids that are not runs
  of the project and lists them in `missing`.

### Permissions

Experiments and saved views use the permissions of runs. A viewer can see them. To make,
change or delete them, you need the runner role (or a higher role), or an API token with the
`runs:write` scope. See [Tenancy and authentication](../concepts/tenancy-and-auth.md).

## Sweeps

The `Sweep` document is retired in v0.14. The server does not accept new `Sweep` documents.
Use a workflow with a `search.grid` node. Each grid point of the node logs its scores as
metrics with the step of the point (see [Workflow nodes](tracking.md#workflow-nodes)). Open
the search run to compare the points in the **Metrics** tab, or compare the runs of two
searches on the compare page.
