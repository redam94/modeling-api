---
description: Step-by-step tutorials - your first MMM in Guided mode, a forecast that tests and checks itself, null tests before a model goes live, Google Sheets, embedded charts, webhook receivers, and budget optimisation on mmm-framework.
---

# Tutorials

Each tutorial gets one thing working from start to finish, with the screens or the code you
need. The code is not illustrative: every Python snippet runs in the test suite, and the
Apps Scripts run against stand-ins for Google's services.

<div class="grid cards" markdown>

-   :material-compass-outline:{ .lg .middle } **Your first MMM in Guided mode**

    ---

    From a spreadsheet of weekly spend and revenue to ROI per channel, a sanity scorecard
    and a budget scenario, without code. About ten minutes.

    *For analysts.* [:octicons-arrow-right-24: Start](../guides/guided-tutorial.md)

-   :material-google-spreadsheet:{ .lg .middle } **Connect Google Sheets**

    ---

    An Apps Script that writes a model's forecast into a sheet, with its likely range, and
    refreshes it every morning.

    *For analysts and integrators.* [:octicons-arrow-right-24: Start](google-sheets.md)

-   :material-chart-box-plus-outline:{ .lg .middle } **Embed a chart in Notion, Slack or BI**

    ---

    A read-only, expiring link to a model's chart for a doc, a dashboard or a thread, and a
    PNG for tools that cannot show live content.

    *For analysts.* [:octicons-arrow-right-24: Start](embed-a-chart.md)

-   :material-webhook:{ .lg .middle } **Receive webhooks**

    ---

    A FastAPI or Flask receiver that verifies every delivery's signature, ignores duplicates
    and reacts when a model is ready or a run fails.

    *For developers.* [:octicons-arrow-right-24: Start](../guides/webhook-receivers.md)

-   :material-target:{ .lg .middle } **Optimise a budget**

    ---

    Ask a fitted marketing-mix model how to split a budget across channels: bounds, groups,
    cautious objectives, the uplift and the chance it helps, and the efficient frontier.

    *For data scientists.* [:octicons-arrow-right-24: Start](../tutorials/optimise-a-budget.md)

-   :material-graph-outline:{ .lg .middle } **A forecast that tests itself** <span class="mapi-badge">v0.5</span>

    ---

    Unit-root tests, a differencing decision, an AR/MA order search, a rolling-origin
    backtest against naive forecasts and governance, as one workflow you run with `--watch`
    and schedule for Monday mornings.

    *For data scientists.* [:octicons-arrow-right-24: Start](../guides/workflow-cookbook.md#readiness-to-arimax)

-   :material-shield-check-outline:{ .lg .middle } **Prove a model is not fitting noise** <span class="mapi-badge">v0.5</span>

    ---

    Write a policy with null tests, validate a model version, have a colleague approve it,
    export the evidence pack and make the endpoint refuse anything unvalidated.

    *For data scientists and reviewers.* [:octicons-arrow-right-24: Start](../guides/governance.md)

-   :material-magnify-scan:{ .lg .middle } **Explore your data, then choose a model** <span class="mapi-badge">v0.5</span>

    ---

    Read the Data Explorer's quality warnings, stationarity and lead-lag views, then let
    *Help me choose* turn a few answers into a shortlist with reasons.

    *For analysts.* [:octicons-arrow-right-24: Start](../guides/data-explorer.md) ·
    [Choosing a model](../guides/choosing-a-model.md)

</div>

## Before you start

- **A platform to talk to.** Everything works against a local stack: `make install && make
  dev && make seed` ([Quickstart](../getting-started/quickstart.md)). The Google Sheets and
  public-embed tutorials need a deployment that the internet can reach, since Apps Script and
  embedded charts are loaded from Google's and your viewers' machines.
- **A fitted model.** The Guided tutorial makes one; `make seed` loads a demo marketing-mix
  spec you can fit from the web app.
- **An endpoint**, for Sheets and other tools: put a model live from the Guided *Share* step or the
  [Integrations hub](../guides/integrations-hub.md#promoting-through-the-gate).

More task-oriented guides are under [Guides](../guides/web-ui.md); the ideas behind them are in
[Concepts](../concepts/index.md); and [Use cases](../use-cases.md) shows which tutorials fit
which problem.
