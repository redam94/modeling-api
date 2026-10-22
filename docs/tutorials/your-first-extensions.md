---
description: "Tutorial: your first custom model, preprocessing step, chart and workflow in modeling-api - first with no code in the app, then as a Python plugin on your own stack, ending with all of it in the app."
---

# Your first extensions <span class="mapi-badge">v0.18</span>

Let's make the platform yours. In this tutorial you'll add your own **model**,
**preprocessing step**, **chart** and **workflow**, twice:

- **Part 1, no code** (about 20 minutes). You build each one in the app, on the sample
  project's data, and share it with your team. Any analyst with the editor role can do this.
- **Part 2, with Python** (about 20 minutes). You scaffold a plugin package with all five
  extension kinds, test it, install it on your own dev stack and use it in the app. This part
  is for developers.

You don't need Part 1 for Part 2, or the other way round. Pick the part you need.

## Before you start

- **Part 1:** an account with the editor role in a project, and the **sample project**. On
  Home, find the sample card under **Start here** and pick **Make the sample project**. It
  fits a real marketing-mix model on example data, which takes a minute or two. You can carry
  on while it runs.
- **Part 2:** a source checkout of modeling-api with `make install` done, a stack from
  `make dev`, and the checkout's environment active (`source .venv/bin/activate`), so that
  `mapi` is on your path.

---

## Part 1: no code

### 1. A model recipe

You'll build a model that explains weekly revenue with the average price, with its own level
for each region. You believe that a higher price never raises revenue, and you'll tell the
model so.

1. Open **Make your own** in the navigation, then **Models › New model recipe**.
2. **Data:** pick the sample dataset, `sample-geo-mmm`.
3. **Outcome:** pick `revenue_gbp`. A few weeks are far off the rest, so pick **A number with
   outliers**.
4. **Terms:** add `avg_price`. Turn on the **standardize** transform. For the prior, keep
   **normal** and set sigma to 0.5: watch the little curve get narrower. Set the sign to
   **negative**.
5. **Groups:** add `region`. Each region gets its own level.
6. **Review:** read the summary in words, then open the **YAML** tab. It looks like this:

    ```yaml
    --8<-- "extend_tutorial_recipe.yaml"
    ```

7. Pick **Save**, then **Fit now**.

When the fit is done, open its run. The posterior forest shows the effect of price below
zero: the sign you set is a promise the model keeps. Your recipe is now in **Catalog ›
Yours** under "From your team", and in the Build palette.

??? info "What the platform does with your recipe"
    It compiles the recipe to normal spec parts with trusted code: a step that makes one row
    for each week and region, your transforms, and the built-in model
    `tabular.declarative_glm`. This is that code, on the sample's schema:

    ```python
    --8<-- "extend_tutorial.py:sample"
    ```

    ```python
    --8<-- "extend_tutorial.py:part1-recipe"
    ```

### 2. A prep preset with your own step

Next, a column of your own: the total media spend of each week. A **formula step** makes it,
and a **prep preset** saves it for next time.

1. **Make your own › Prep › New prep preset.**
2. Add the step **Fill gaps** (`core.impute`). Set the method to `constant` and the
   value to 0. Mark its columns as a **slot**: whoever uses the preset picks the columns.
3. Add the step **New column from a formula** (`core.formula`). Set `out` to `total_spend` and `expr` to
   `sum(spend, 'channel')`: the spend of every channel, added up.
4. Under **Try it on a dataset**, choose `sample-geo-mmm` and pick `spend` for the slot.
   Nothing runs: the server checks each step and shows the data after it, with the new
   `total_spend`.
5. **Save.**

```yaml
--8<-- "extend_tutorial_prep.yaml"
```

Your preset is in the Build palette under "From your team". Add it to any spec and fill the
slot.

??? info "What happens when you use it"
    ```python
    --8<-- "extend_tutorial.py:part1-prep"
    ```

### 3. A chart template

The fit in step 1 draws `core.fit_vs_actual`: one line for the prediction and dots for the
actual values. Let's give its data a new look, with one small panel for each region.

1. Open **Catalog › Plots**, find **Predicted vs actual** and pick **Make a chart template**.
2. Pick the starter **Predicted against actual, one panel per group**. The preview draws it on
   example data.
3. Give it a title and a caption that a reader understands, such as "Dots near the diagonal
   are good predictions."
4. **Save.**

Now open the run of your fit from step 1. Your chart is under **Your charts**, drawn on the
real data of that fit. Chart templates are for your team: share links and client portals
never show them.

### 4. A team workflow template

Last, share a workflow. Open **Workflows**, open a workflow that validates (or start one from
a template in the gallery), then pick **Save as team template** from the editor menu. Give it
a title and say what it is for. Your teammates now find it in the gallery under **From your
team**, and **Start from this** gives each of them their own copy.

That's Part 1. Everything you made is in **Catalog › Yours**. A tenant admin can promote any
of it, so every project in your organisation can use it.

---

## Part 2: with Python

### 5. Scaffold a plugin

In the folder that holds your modeling-api checkout (one level up from it):

```bash
--8<-- "extend_cli.sh:init"
```

You get a package called `acme` with one module, one entry point and one contract-kit test
for each kind:

| Kind | Id | File |
|---|---|---|
| Model | `acme.group_mean` | `src/acme/model.py` |
| Preprocessing step | `acme.winsorize` | `src/acme/steps.py` |
| Plot recipe | `acme.weekly_total` | `src/acme/plots.py` |
| Workflow node | `test.acme_zero_share` | `src/acme/nodes.py` |
| Workflow template | `acme_weekly_check` | `src/acme/templates.py` |

Each one works as it is, with a filled-in About card. Have a look at them now; the
[Develop pages](../extend/develop/index.md) explain each file. Later, you replace the example
code with yours.

### 6. Test and validate

```bash
--8<-- "extend_cli.sh:test"
```

The contract kits run your extensions the way the platform will. All twelve tests pass.

```bash
--8<-- "extend_cli.sh:validate"
```

`validate` loads only your package's entry points and checks each one, as the runner's
loader will. It ends with:

```text
catalog: 1 models, 1 steps, 1 plots, 1 workflow nodes, 1 workflow templates
all checks passed
```

### 7. Check it against the platform, before you install

You can already see what the platform will make of your plugin. This code loads the package
by path, registers it next to the built-ins and checks a spec that uses your step, model and
recipe on the sample's data:

```python
--8<-- "extend_tutorial.py:registry"
```

```yaml
--8<-- "extend_tutorial_spec.yaml"
```

```python
--8<-- "extend_tutorial.py:spec"
```

### 8. Install it on your dev stack

From the checkout:

```bash
make plugin-dev PLUGIN=../acme
```

This installs `acme` in editable mode and adds its folder to `.local/extra-plugins.txt`, so
the image digest changes. Now stop `make dev` (Ctrl+C) and start it again. At the start, the
runner describes the catalog again, now with `acme`.

```bash
make plugin-doctor
```

The doctor lists your five entry points and says whether anything is wrong. If something
did not load, it says why. (Want details? [Run your plugin on your stack](../extend/develop/run-on-your-stack.md).)

### 9. See it in the app

- **Catalog › Yours:** "Installed by your team" lists `acme.group_mean`, `acme.winsorize` and
  `acme.weekly_total`, each with its About card.
- **Catalog › Plots:** the **Weekly totals** card has its example picture. **Try it** opens
  the playground: change the title and the chart changes at once. Change `var` and the chart
  is drawn again on the server, which takes a few seconds.
- **Build:** make a new spec in the sample project, paste the spec from step 7, pick
  **Validate**, then **Fit**. The run page shows the Weekly totals chart with the fit's
  predictions.
- **Workflows:** the editor palette has **Share of zeros** (`test.acme_zero_share`) under the
  test nodes. The gallery has **Weekly data check** under **Installed**: pick it to start a
  workflow.

### 10. Mix both paths

Your plugin and the no-code library work together. An analyst can make a team preset over
your model, a prep preset with your step, a chart template over your recipe, and a team
workflow template with your node, all in Make your own. This is what the platform checks when
they do:

```python
--8<-- "extend_tutorial.py:preset"
```

```python
--8<-- "extend_tutorial.py:prep"
```

```python
--8<-- "extend_tutorial.py:chart"
```

```python
--8<-- "extend_tutorial.py:workflow"
```

### 11. Clean up (optional)

```bash
make plugin-undev PLUGIN=../acme
```

Then restart `make dev`. Your library items from Part 1 stay: they are data, not code.

## Where next

- Replace the example code with your own: [Develop a plugin](../extend/develop/index.md).
- Put your plugin into a Docker or Cloud Run deployment:
  [a custom runner image](../extend/develop/run-on-your-stack.md#c-a-custom-runner-image).
- Each no-code kind in depth: [Make it yours](../extend/index.md).
- What's next for plugins: [Coming in 0.19](../extend/upload-roadmap.md).
