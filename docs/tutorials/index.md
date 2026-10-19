---
description: Step-by-step tutorials - your first MMM in Guided mode, science models from pymc-challenges, complex workflows, a forecast that tests and checks itself, null tests before a model goes live, Google Sheets, embedded charts, webhook receivers, and budget optimisation on mmm-framework.
---

# Tutorials

Each tutorial makes one thing work from start to finish. It gives the screens or the code
that you need. The code is not only an example. Each Python snippet runs in the test suite.
The Apps Scripts run against substitutes for the services of Google.

<div class="grid cards" markdown>

-   :material-compass-outline:{ .lg .middle } **Your first MMM in Guided mode**

    ---

    Start with a spreadsheet of weekly spend and revenue. Get the ROI for each channel, a
    sanity scorecard and a budget scenario, without code. It takes about ten minutes.

    *For analysts.* [:octicons-arrow-right-24: Start](../guides/guided-tutorial.md)

-   :material-google-spreadsheet:{ .lg .middle } **Connect Google Sheets**

    ---

    An Apps Script writes the forecast of a model into a sheet, with its likely range. It
    refreshes the forecast each morning.

    *For analysts and integrators.* [:octicons-arrow-right-24: Start](google-sheets.md)

-   :material-chart-box-plus-outline:{ .lg .middle } **Embed a chart in Notion, Slack or BI**

    ---

    Make a read-only link to the chart of a model, for a doc, a dashboard or a thread. The
    link expires. You also get a PNG for tools that cannot show live content.

    *For analysts.* [:octicons-arrow-right-24: Start](embed-a-chart.md)

-   :material-webhook:{ .lg .middle } **Receive webhooks**

    ---

    Make a FastAPI or Flask receiver. It verifies the signature of each delivery and ignores
    duplicates. It does an action when a model is ready or when a run fails.

    *For developers.* [:octicons-arrow-right-24: Start](../guides/webhook-receivers.md)

-   :material-target:{ .lg .middle } **Optimise a budget**

    ---

    Ask a fitted marketing-mix model how to divide a budget across channels. Learn about
    bounds, groups, cautious objectives, the uplift, the probability that it helps, and the
    efficient frontier.

    *For data scientists.* [:octicons-arrow-right-24: Start](../tutorials/optimise-a-budget.md)

-   :material-graph-outline:{ .lg .middle } **A forecast that tests itself** <span class="mapi-badge">v0.5</span>

    ---

    Make one workflow with unit-root tests, a differencing decision and an AR/MA order
    search. Add a rolling-origin backtest against naive forecasts, and governance. Run it
    with `--watch` and schedule it for Monday mornings.

    *For data scientists.* [:octicons-arrow-right-24: Start](../guides/workflow-cookbook.md#readiness-to-arimax)

-   :material-shield-check-outline:{ .lg .middle } **Prove that a model does not fit noise** <span class="mapi-badge">v0.5</span>

    ---

    Write a policy with null tests and validate a model version. Get the approval of a
    colleague and export the evidence pack. Then set the endpoint to refuse all versions
    that are not validated.

    *For data scientists and reviewers.* [:octicons-arrow-right-24: Start](../guides/governance.md)

-   :material-flask-outline:{ .lg .middle } **Science models from pymc-challenges**

    ---

    Fifteen case studies, from disease maps and networks to images, fields and directions.
    Each page gets the data, fits the model in the web app, reads the results, scores the
    model and runs a workflow recipe. It compares the numbers with the notebook.

    *For data scientists.* [:octicons-arrow-right-24: Start](pymc-challenges/index.md)

-   :material-sitemap-outline:{ .lg .middle } **Complex workflows**

    ---

    Build model ladders, convergence gates, posterior predictive checks, governance checks,
    calibrate-then-design runs, plots in reports and sub-workflows in the workflow editor.

    *For data scientists.* [:octicons-arrow-right-24: Start](complex-workflows.md)

-   :material-magnify-scan:{ .lg .middle } **Explore your data, then choose a model** <span class="mapi-badge">v0.5</span>

    ---

    Read the quality warnings, stationarity views and lead-lag views of the Data Explorer.
    Then answer some questions in *Help me choose*. It gives a shortlist with reasons.

    *For analysts.* [:octicons-arrow-right-24: Start](../guides/data-explorer.md) ·
    [Choosing a model](../guides/choosing-a-model.md)

</div>

## Before you start

- **A platform to talk to.** All tutorials work against a local stack: `make install && make
  dev && make seed` ([Quickstart](../getting-started/quickstart.md)). The Google Sheets and
  public-embed tutorials need a deployment that the internet can get to. The reason is that
  Apps Script loads from the machines of Google, and embedded charts load from the machines
  of your viewers.
- **A fitted model.** The Guided tutorial makes one. `make seed` loads a demo marketing-mix
  spec that you can fit from the web app.
- **An endpoint**, for Sheets and other tools. To put a model live, use the Guided *Share*
  step or the [Integrations hub](../guides/integrations-hub.md#promoting-through-the-gate).

For more guides about tasks, see [Guides](../guides/web-ui.md). For the ideas behind them, see
[Concepts](../concepts/index.md). [Use cases](../use-cases.md) shows which tutorials apply to
which problem.
