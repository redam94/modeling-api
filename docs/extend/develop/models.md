---
description: Write a model plugin for modeling-api - a CustomModel, PyMCModel or SklearnModel registered through the mapi.models entry point and checked by check_model.
---

# Develop a model

A model plugin is a class with `fit` and `score`. The platform does the rest: it binds the
dataset's names to your canonical names, splits the data, runs the pipeline, keeps the
coordinates, writes the bundle and makes the summaries. Start from the scaffold:

```bash
mapi plugin init acme --kind model                # a CustomModel (numpy, xarray)
mapi plugin init acme --kind model --base pymc    # a PyMCModel (you write build())
mapi plugin init acme --kind model --base sklearn # an SklearnModel (an estimator factory)
```

## The scaffold

`src/acme/model.py` (the `custom` base) is a complete, small model: each group's mean of the
target, optionally shrunk toward the global mean.

```python
--8<-- "extend_pkg/src/acme/model.py"
```

What to keep when you replace the example with your model:

- `id` is `<your namespace>.<name>` and `version` is semver. A spec names it as
  `acme.group_mean@^0.1`.
- `Settings` is a Pydantic model. Its JSON Schema becomes the settings form in the app, so it
  must pass the [schema-safety rules](../../reference/generated/extension-points.md#settings-and-parameter-schemas).
- `about` is the card that analysts read in the Catalog and in Help me choose.
- `schema_template` declares the data that the model accepts, in canonical names, with a
  policy for each dim (here: new `geo` levels are pooled; `time` may extend).
- `outputs` lists each variable that `score` returns, with its dims.
- `MODELS` is what the `mapi.models` entry point exports.

The full rules, with PyMC and scikit-learn examples, are in
[Write a step or model plugin](../../guides/plugins.md) and
[Port a model in 15 minutes](../../guides/port-a-model.md).

## The entry point

`pyproject.toml` registers the module:

```toml
[project.entry-points."mapi.models"]
acme = "acme.model:MODELS"
```

Each kind has its own entry point and module, so a model that fails to import never hides
your plots or nodes.

## The contract kit

```python
--8<-- "extend_pkg/tests/test_model.py"
```

`check_model` fits the model on a small panel, then checks bundle round trips (no pickle),
scoring in another order, on a subset and with new levels, typed errors for every dim
policy, the evaluate path, output units, seed determinism and the About card. See
[Contract kits](contract-kits.md).

## Use it

In a spec:

```yaml
model:
  use: acme.group_mean@^0.1
  settings: {shrinkage: 0.1}
```

Once it is [installed on your stack](run-on-your-stack.md), the model is in **Catalog ›
Yours** with an "Installed by your team" badge, in the model picker of **Build**, in the
Model Explorer and in dashboard presets. Analysts can also make a
[preset](../your-model.md#a-preset-over-a-catalog-model) over it with no code.
