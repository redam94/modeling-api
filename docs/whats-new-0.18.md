---
description: v0.18 makes modeling-api yours - add your own models, prep steps, charts and workflows with no code or as Python plugins, learn the app with tours, a checklist, a sample project and playgrounds, all in the bolder Studio look.
---

# What's new in 0.18 <span class="mapi-badge">v0.18</span>

**Make it yours.** You can now add your own models, preprocessing steps, charts and
workflows. Analysts build them in the app with no code. Developers write them as Python
plugins and run them on their own stack. New people learn the app with a short tour, a
checklist and a real sample project. And the app has a new, bolder look.

<div class="grid cards" markdown>

-   :material-puzzle-outline:{ .lg .middle } **Make your own, no code**

    ---

    Model recipes (a declarative model with your priors and signs, or a preset over any
    catalog model), prep presets with slots and formula steps, chart templates, and team
    workflow templates. Share them with your project or your whole organisation.

    [:octicons-arrow-right-24: Make it yours](extend/index.md)

-   :material-language-python:{ .lg .middle } **A plugin kit for every kind**

    ---

    `mapi plugin init --kind model|step|recipe|node|template` scaffolds a package with About
    cards and contract-kit tests. `mapi plugin validate` and `mapi plugin doctor` check it.
    Plugin workflow nodes and templates are new.

    [:octicons-arrow-right-24: Develop a plugin](extend/develop/index.md)

-   :material-server-plus:{ .lg .middle } **Run it on your stack**

    ---

    `make plugin-dev` for an editable plugin on a dev stack, `make plugin-image` for a
    Docker or Cloud Run runner image. The catalog is described again when your plugin
    changes.

    [:octicons-arrow-right-24: Run on your stack](extend/develop/run-on-your-stack.md)

-   :material-map-marker-path:{ .lg .middle } **Tours, a checklist and a sample**

    ---

    A two-minute welcome tour, seven tours in all, a checklist that ticks itself, and a
    one-click sample project: a real marketing-mix fit on example data.

    [:octicons-arrow-right-24: Start here](guides/start-here.md)

-   :material-tune-variant:{ .lg .middle } **Playgrounds**

    ---

    A what-if slider on Home and in the Catalog, and **Try it** for every chart: its
    settings form next to its example, with the YAML to copy.

    [:octicons-arrow-right-24: The playgrounds](guides/start-here.md#the-playgrounds)

-   :material-palette-outline:{ .lg .middle } **The Studio look**

    ---

    Deep ink-indigo blocks, a lime highlighter, coral for what needs your attention, a new
    display face and illustrations made from chart parts. Tables and forms stay calm and
    easy to read. Share links and client portals follow the new colours.

</div>

## Your own pieces, no code

**Make your own** (in the navigation) has four tabs:

- **Models.** Build a [declarative model](extend/your-model.md): pick the outcome in plain
  words ("a count", "yes / no"), add drivers with a transform, a prior (the little curve shows
  it) and a sign, and give groups their own level. It compiles to the new built-in model
  `tabular.declarative_glm`. Or save a **preset** over any catalog model, with the few
  settings others may change.
- **Prep.** Save steps as a [prep preset](extend/prep-presets.md) with slots. A `core.formula`
  step is your own column with no code.
- **Charts.** A [chart template](extend/chart-templates.md) gives a platform chart's data your
  own look. It shows on run and model pages under "Your charts", never in shared pages.
- **Workflow templates.** "Save as team template" in the workflow editor; the gallery shows
  [team templates](extend/workflow-templates.md) under "From your team".

Each item is a JSON document with revisions. Trusted code compiles it to things the platform
already runs; nothing a tenant writes runs as code. Specs and workflows made from an item
carry a `mapi.library/*` label, so you can always see where they came from.

## Plugins for every kind

- `mapi plugin init NAME --kind model|step|recipe|node|template` (repeatable) writes a
  package with one module, entry point and contract test for each kind.
- `mapi plugin validate` loads only that package and runs every check; `mapi plugin doctor`
  checks every installed extension, the routing and the digest, and with `--server` compares
  with the server.
- New public kits: `mapi_plots.testing.check_recipe`, `mapi_workflows.kit.check_node` and
  `check_template`, next to `check_model` and `check_step`.
- Plugin **workflow nodes** (`NodeExport` in `mapi.workflow_nodes`) reach the editor palette
  and run in the sandbox. Plugin **workflow templates** (`mapi.workflow_templates`) show in
  the gallery under "Installed".
- Nothing an entry point exports is dropped in silence: catalog **diagnostics** show in
  `mapi plugin doctor` and, for tenant admins, in Catalog › Yours › Problems.
- Plugin entries carry a `provider` and show "Installed by your team".

!!! warning "For plugin authors: reserved namespaces"
    Plugins may no longer register ids in the built-in namespaces (`core`, `mmm`, `ts`,
    `tabular`, `sci`, `dl`, `net`, `demo`, `examples`, ...) or in the platform's (`mapi`,
    `lib`, `team`, `tenant`, `t_...`, ...). Such an id is refused with `namespace_reserved`.
    See the [extension points reference](reference/generated/extension-points.md#namespaces).

## Learn the app

- A **welcome** card on your first visit, and seven **tours** that you can replay from your
  menu or open with `?tour=<id>`.
- A **checklist** of eight first steps on Home and in Guided mode, stored for you on the
  server.
- A **sample project** for each organisation: a real fit on two years of example data, with a
  report, what-if sliders and a dashboard to try. It comes with one model recipe, one prep
  preset and one chart template.
- **Playgrounds:** a what-if slider on example data, and **Try it** for each chart.

## Also in this release

- Plot recipes publish a settings schema (`settings_schema` on catalog entries), tags and a
  pack label. The plots catalog page and the playground use them.
- New settings: `MAPI_SAMPLES_ENABLED`, `MAPI_PLOT_PREVIEW_ENABLED` and the library and
  preview limits ([server settings](reference/generated/server-settings.md)); operators can
  ask for a new describe run with `make catalog-refresh`.

Read the full list in the [changelog](project/changelog.md).
