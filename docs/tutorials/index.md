---
description: Step-by-step tutorials for v0.4 - your first MMM in Guided mode, Google Sheets, embedded charts, webhook receivers, budget optimisation with Atlas and importing a BayesInsight model.
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

-   :material-target:{ .lg .middle } **Optimise a budget with Atlas**

    ---

    Turn an endpoint into an Atlas model and let an optimiser split a fixed budget across
    channels, with batching, caching and back-off.

    *For data scientists.* [:octicons-arrow-right-24: Start](../guides/atlas.md)

-   :material-import:{ .lg .middle } **Import a BayesInsight model**

    ---

    Bring a BayesInsight `model_def.json` and its MFF data: check the import, fit it on your
    laptop, then run it on the platform.

    *For data scientists.* [:octicons-arrow-right-24: Start](bayesinsight-import.md)

</div>

## Before you start

- **A platform to talk to.** Everything works against a local stack: `make install && make
  dev && make seed` ([Quickstart](../getting-started/quickstart.md)). The Google Sheets and
  public-embed tutorials need a deployment that the internet can reach, since Apps Script and
  embedded charts are loaded from Google's and your viewers' machines.
- **A fitted model.** The Guided tutorial makes one; `make seed` loads a demo marketing-mix
  spec you can fit from the web app.
- **An endpoint**, for Sheets and Atlas: put a model live from the Guided *Share* step or the
  [Integrations hub](../guides/integrations-hub.md#promoting-through-the-gate).

More task-oriented guides are under [Guides](../guides/web-ui.md); the ideas behind them are in
[Concepts](../concepts/index.md); and [Use cases](../use-cases.md) shows which tutorials fit
which problem.
