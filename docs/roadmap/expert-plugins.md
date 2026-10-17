# Expert plugins and MCP <span class="mapi-badge mapi-badge--soon">next: v0.6</span>

!!! abstract "Status: next (milestone M4)"
    v0.4 opened the hosted platform to declarative specs and built-in models. v0.5 added
    workflows, governance, the explorers and budget optimisation in the sandbox
    ([what's new](../whats-new-0.5.md)). In v0.6, **allow-listed experts can upload their own
    model plugins**. v0.6 also gives AI assistants and agents a curated **MCP server**.
    Sweeps and simulation-based calibration become single parent runs. Public code upload
    for all users comes after v0.6, with its own controls (see the [roadmap](index.md#after-v06)).

## Plugin upload for allow-listed experts

Now, a data scientist can run their own `PyMCModel` or `SklearnModel` only on a deployment
that they control. They install it into the environment of the runner. v0.6 makes this a
product feature of a shared deployment. It is available to tenants that an operator put on
the allow list:

```mermaid
flowchart LR
    A["mapi plugin push<br/>(your package)"] --> B["Overlay build<br/>source + pre-resolved wheels,<br/>pinned by hash"]
    B --> C["Describe run<br/>catalog entry, schema sanitizer,<br/>contract kit report"]
    C --> D{{"Kit passes?"}}
    D -->|yes| E["Runnable in your project<br/>on the pinned base image"]
    D -->|no| F["Readable report<br/>of what to fix"]
```

- **Overlays, not images.** The platform builds a plugin into a hash-pinned overlay on top
  of one base-image version. The overlay has the source and wheels that were resolved
  before, including native wheels from a curated mirror. Runs never install software.
- **The contract kit runs on upload.** The kit does the same checks as `mapi plugin test`
  on your computer. These checks decide if a version becomes runnable. The platform keeps
  the report with the version, next to its SBOM.
- **Schema sanitizer.** The platform sanitizes the settings schemas of a plugin before the
  web app makes forms from them.
- **Dev overlays** for fast iteration. The target is less than five minutes from `push` to
  a runnable version.
- **A vetted tier.** We ported the sibling marketing-mix and HMM models as plugins. They
  are the proof that real models agree with the contract.

## An MCP server for agents

The MCP server gives a curated set of tools to AI assistants and agents. For example, the
tools list and describe models, score an endpoint, read a run and get a plot. The server
returns each string from a user or a plugin as clearly delimited untrusted text. Thus a model
name cannot put hidden instructions into an agent. Tokens and scopes work the same as for
other clients.

## Sweeps as jobs, and what v0.5 already delivered

- **`optimize` runs** (delivered in v0.5). The sandbox optimises budgets *next to the
  model* with the planning module of mmm-framework. It uses the response curves of the
  posterior ([Optimise a budget](../tutorials/optimise-a-budget.md)).
- **Backtests** (delivered in v0.5) run as workflow nodes on rolling-origin and
  expanding-window plans ([cookbook](../guides/workflow-cookbook.md)).
- **`Sweep` and `SBC`** as one parent run that fans out and aggregates the results. For
  example, a sensitivity sweep over a prior, or simulation-based calibration of a new
  plugin. (The governance of v0.5 has a parametric-bootstrap version, `recovery.simulated`.)

## Exit criteria

- Three sibling models are packaged as plugins and pass the contract kit.
- A dev overlay is pushed and becomes runnable in less than five minutes.
- An as-of backtest and a sensitivity sweep each run as a single parent.

We cut a release when it passes its exit criteria. Read the
[changelog](../project/changelog.md).
