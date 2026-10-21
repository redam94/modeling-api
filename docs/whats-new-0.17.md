---
description: v0.17 makes client dashboards work for any model type - a layout for each model family, panels that analysts can add and move, try a case, actuals tracking with the right accuracy for each family, scoring a file of new cases, and an automatic move of the old dashboards to the new layout.
---

# What's new in 0.17 <span class="mapi-badge">v0.17</span>

**Client dashboards now work for any model type, not only for marketing-mix models.** A
forecast, a classifier, a regression, a model of a change (causal impact), a map of a
science model or an image model each get a layout that fits it. The analyst can add,
remove and move the parts of the page. Clients always see a clean readout in plain words.

Clients can now do more on a dashboard, if the analyst allows it:

- **Try a case**: change a few values and see the prediction with its likely range.
- **Check accuracy**: send the actual results and see how right the model was.
- **Score a file**: send a CSV file of new cases and download the predictions.

The model never changes because of an input from a client. Only an analyst refits it.

<div class="grid cards" markdown>

-   :material-view-grid-plus-outline:{ .lg .middle } **A layout for each model family**

    ---

    The app looks at the model and suggests a preset, with the reasons. Forecasts show the
    next periods. Classifiers show the chance of an outcome. Models of a change show the
    effect. Science models show a map. Marketing-mix models keep the media planner.

    [:octicons-arrow-right-24: Build a client dashboard](guides/client-dashboards.md)

-   :material-drag-variant:{ .lg .middle } **Panels that you can edit**

    ---

    Add, remove and move key numbers, charts, tables, notes and the plots of the model
    version. The app checks each panel against what the model really writes.

    [:octicons-arrow-right-24: Panels](concepts/client-dashboards.md#panels)

-   :material-flask-outline:{ .lg .middle } **Try a case**

    ---

    The analyst picks the values that a client can change and sets their ranges. The server
    checks each value. A value out of range is refused, never changed in silence.

    [:octicons-arrow-right-24: Scenario rows](concepts/client-dashboards.md#scenario-rows)

-   :material-bullseye-arrow:{ .lg .middle } **Actuals tracking for each family**

    ---

    Errors for forecasts and regressions, right predictions and calibration for
    classifiers, and the updated effect for a causal impact model. A history shows how the
    accuracy changes over time.

    [:octicons-arrow-right-24: Accuracy](concepts/client-dashboards.md#accuracy)

-   :material-file-table-outline:{ .lg .middle } **Score a file**

    ---

    Up to 5000 new cases in one CSV file. The client gets a summary and a predictions file
    that only they can download.

    [:octicons-arrow-right-24: Batch scoring](concepts/client-dashboards.md#batch)

-   :material-swap-horizontal:{ .lg .middle } **An automatic move to the new layout**

    ---

    Your v0.16 dashboards move to the new layout by themselves. Their links, grants and
    history stay. The old readouts keep their old look.

    [:octicons-arrow-right-24: The move to the new layout](#the-move-to-the-new-layout)

</div>

## The move to the new layout { #the-move-to-the-new-layout }

When the worker of v0.17 starts, it moves each v0.16 dashboard (layout 1) to the new layout
(layout 2). You do not have to do anything.

- A marketing-mix dashboard becomes the **Media planner** preset, with the same tiles,
  channels, scenarios and uploads.
- A forecast dashboard that used a placeholder channel becomes the **Forecast** preset,
  without channels.
- The move adds a new published revision on the **same** model version, with the same
  run-as owner. It also starts a new baseline readout.
- The server never changes a stored row. The old revisions and the old readouts stay as
  they were. **History** shows the old readouts with the old look.
- A **Media planner** dashboard keeps its share links, client grants, saved scenarios and
  refit requests as they were.
- A dashboard that moves to another preset (for example **Forecast**) keeps its client
  grants, share links and refit requests, but it has no channel what-ifs: the grid, the
  what-if and the saved scenarios answer `409 DASHBOARD_INPUT_DISABLED`. A share link that
  allowed only what-ifs becomes read-only. Uploads stay on only if the new preset allows
  them.
- Drafts move to the new layout too. An unfinished draft that cannot move stays on the old
  layout (the builder shows why); the published revision moves without it.
- If the server cannot read the catalog of the new release yet, it tries again every
  minute; it never picks a preset without it.

If the app cannot move a dashboard by itself, the dashboard stays on the old layout.
Clients see no change. The builder shows the problems and a **Use the new layout** button,
so that an editor can finish the move. The setting `MAPI_DASHBOARD_AUTO_UPGRADE=false` stops
the automatic move.

New dashboards always use the new layout.

### What clients see

The first time a client opens a moved dashboard, the page shows this note:

> This dashboard has a new layout. The numbers come from the same model.

If the dashboard shows likely ranges of results, the note also says: "The likely ranges now
show where one new result will probably be. They are wider than before." The client can
close the note. For a share link, the note can show again in a new browser session. The
note goes away when the analyst publishes the next change.

### Wider likely ranges

The new layout shows **where one new result will probably be** (the predictive range). The
old layout showed **where the average will probably be**. One new result varies more than
the average, so the new range is wider. This applies to moved marketing-mix dashboards too.
The middle value does not change. Tell your clients before the move if they compare ranges.

## Presets

| Preset | For | Clients see | Client inputs |
|---|---|---|---|
| **Media planner** | Marketing-mix models | The v0.16 planner: tiles, channels, trends, plan scenarios | What-ifs, actuals, refit requests |
| **Forecast** | Time series models with a forecast | The next period, the likely range, the typical error | Periods ahead, actuals by date |
| **Predictions** (regression) | A value for each case | The prediction with its likely range | Try a case, actuals by case, score a file |
| **Classifier** | The chance of an outcome | "Chance of …", how often the model is right | Try a case, actuals by case, score a file |
| **Effect of a change** | Causal models | The total effect, the effect in percent, the chance of an effect | Actuals (causal impact: the updated effect; other causal models: a refit request) |
| **Map** | Science models with a grid or areas | A map, area bars and key numbers with a range | Periods ahead (some models) |
| **Image model** | Image classifiers | The accuracy from the fit and the plots of the model | None for clients in v0.17 |
| **General** | Any other model | One key number and one chart of the outputs | As the model allows |

Image dashboards do not let clients or share links score a file in v0.17. Analysts can
still score image collections in the app.

## Other changes

- New readout kinds `prediction` (a tried case) and `batch` (a scored file). Both are
  private to the person or link that made them. A client can **Keep** a tried case: an
  editor then decides if it goes into the history.
- New limits for scored files: 3 files for each dashboard each day
  (`limits.batch_runs_per_day`), 2 for each share link (`MAPI_SHARE_BATCH_PER_DAY`), 4 for
  each client user (`MAPI_CLIENT_BATCH_PER_DAY`) and 20 000 rows for each dashboard
  (`MAPI_BATCH_ROWS_PER_DAY`).
- The read-only tool surface has six more tools: `list_panels`, `get_panel`,
  `get_accuracy`, `list_inputs`, `get_prediction` and `get_batch_summary`. The v0.16 tools
  keep their names and arguments.
- The Python client has `presets()`, `sources()`, `upgrade()`, `predict()`,
  `keep_readout()`, `batch_template()`, `score_batch()`, `download_predictions()` and
  `asset()` on `client.dashboards`, and the same inputs on a share link.
- A forecast whose model needs future values of its drivers (a SARIMAX with exogenous
  inputs, a BSTS with a regressor) builds its baseline with the drivers held at their last
  values (`future_covariates: carry_last`), as a forecast row and a conditioned upload do.
- In a scored file, each row is scored at its own levels: when the rows give another dim
  too (`sku`), the summary and the CSV hold only each row's own cell, and the size check
  counts every id × level the model scores, so a file that is too large is refused before
  a run.
- A picture panel that was still being drawn when a readout was built shows the picture
  as soon as the render ends.
- Migration `0020_any_model_dashboards`: new readout kinds and triggers, a larger snapshot
  cap (384 KiB for the new layout) and a table for the pictures of a revision.

The section `[0.17.0]` of `CHANGELOG.md`, in the root of the repository, gives the full list.
