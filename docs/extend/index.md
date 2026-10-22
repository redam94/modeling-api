---
description: Add your own models, preprocessing steps, charts and workflows to modeling-api - with no code in the app, or as a Python plugin on your own stack.
---

# Make it yours <span class="mapi-badge">v0.18</span>

Missing a model, a prep step, a chart or a workflow? You can add your own, and there are two
ways to do it:

- **No code.** Build it in the app, under **Make your own**. You pick from forms, see a live
  preview, and share the result with your project or your whole team. Every analyst can do
  this, on every tier.
- **With Python.** Write a plugin package with one command to start from, test it with the
  contract kit, and install it on a stack that you run. This is for developers and the
  operators of a deployment.

New here? Start with the tutorial: [Your first extensions](../tutorials/your-first-extensions.md).
It makes one of each in the app, then the same kinds as a plugin, and ends with all of them in
the app.

## What you can add, and how

| What | No code (Make your own) | With Python (a plugin) | Entry point | Contract kit | Scaffold |
|---|---|---|---|---|---|
| **Model** | [Model recipe](your-model.md): a declarative model or a preset over any catalog model | [Model class](develop/models.md) (`CustomModel`, `PyMCModel`, `SklearnModel`) | `mapi.models` | `check_model` | `--kind model` |
| **Preprocessing step** | [Prep preset](prep-presets.md): saved steps with slots, a formula step for your own column | [Step class](develop/steps.md) | `mapi.steps` | `check_step` | `--kind step` |
| **Chart** | [Chart template](chart-templates.md): a new look for the data of a platform chart | [Plot recipe](develop/plot-recipes.md) | `mapi.plots` | `check_recipe` | `--kind recipe` |
| **Workflow node** | (use the built-in nodes) | [Workflow node](develop/workflow-nodes.md) | `mapi.workflow_nodes` | `check_node` | `--kind node` |
| **Workflow template** | [Team template](workflow-templates.md): "Save as team template" in the workflow editor | [Plugin template](develop/workflow-templates.md) | `mapi.workflow_templates` | `check_template` | `--kind template` |

The scaffold column is the option of `mapi plugin init`. The
[extension points reference](../reference/generated/extension-points.md) lists every entry
point group, namespace rule and limit, generated from the code.

## Who can do what

| Path | Who | Where it runs | Review |
|---|---|---|---|
| Make your own (library items) | Editors make project items. Tenant admins make tenant items and promote project items. Viewers use them. | Trusted built-in code compiles each item. Nothing that a tenant writes runs as code. | None needed: an item is data, not code. |
| Plugin in the platform checkout (`make plugin-new`) | The operator of the deployment | In the runner sandbox, like the built-in plugins | Your own code review |
| Out-of-tree plugin on a dev stack (`make plugin-dev`) | A developer on their own `make dev` stack | In the runner sandbox of that stack | Your own code review |
| Custom runner image (`make plugin-image`) | The operator of a Docker or Cloud Run deployment | In the runner sandbox of that image | Your own code review |
| Upload a plugin to a shared deployment | Not in v0.18 | — | [Coming in 0.19](upload-roadmap.md): a second person approves |

!!! info "Nothing in v0.18 accepts code from a tenant"
    A library item is a JSON document. Pure code in `mapi_spec` checks and compiles it to
    things the platform already runs: a spec pipeline and a model block, a list of steps, a
    sanitized chart, or a workflow document. Only an operator installs plugin code, and it
    runs in the runner sandbox. See [Run your plugin on your stack](develop/run-on-your-stack.md).

## Where your extensions show up

- **Catalog › Yours** lists the library items of your project and your tenant under "From
  your team", and plugin entries under "Installed by your team". Tenant admins also see
  "Problems": what the catalog could not load, and why.
- **Build** offers model recipes and prep presets in the palette ("From your team"), next to
  the built-in models and steps.
- **Run and model pages** show your chart templates under "Your charts".
- **Workflows** show team templates and plugin templates in the gallery, and plugin nodes in
  the editor palette.
- **Add your own** panels on Catalog, the plot catalog, the Build palette and the workflow
  gallery show both paths side by side: a link to the no-code builder, and the exact
  `mapi plugin init` command to copy.

Your own items never rank among the built-ins in Help me choose. They show in their own
rows, so a choice is always yours.

## Pages in this section

<div class="grid cards" markdown>

-   :material-puzzle-outline:{ .lg .middle } **No code**

    ---

    [Your model](your-model.md) · [Prep presets](prep-presets.md) ·
    [Chart templates](chart-templates.md) · [Workflow templates](workflow-templates.md)

-   :material-language-python:{ .lg .middle } **With Python**

    ---

    [Develop a plugin](develop/index.md) · [Models](develop/models.md) ·
    [Steps](develop/steps.md) · [Plot recipes](develop/plot-recipes.md) ·
    [Workflow nodes](develop/workflow-nodes.md) ·
    [Workflow templates](develop/workflow-templates.md) ·
    [Contract kits](develop/contract-kits.md) ·
    [Run on your stack](develop/run-on-your-stack.md)

</div>
