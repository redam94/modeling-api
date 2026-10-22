---
description: Make your own model with no code - a declarative model with your priors and signs, or a preset over any catalog model - and share it with your team.
---

# Your model, no code <span class="mapi-badge">v0.18</span>

A **model recipe** is a model that you describe instead of program. There are two kinds:

- **A declarative model.** You say what to explain, which drivers move it, what you believe
  about each driver (a prior and a sign) and which groups have their own level. The platform
  fits it with the trusted built-in model `tabular.declarative_glm`.
- **A preset over a catalog model.** You fix the settings of any model in the catalog and
  pick the few settings that others may change when they use it. Use it to share "the way
  our team fits weekly forecasts".

You make both in the app, under **Make your own › Models**. Each recipe is a JSON document
with revisions. This page shows the same document as YAML, which is what the builder's
**YAML** tab shows.

## A declarative model

```yaml
--8<-- "extend_model_recipe.yaml"
```

In the builder, this is:

1. **Data.** Pick a dataset. The builder lists its columns and their roles.
2. **Outcome.** `sales` is "A count that varies a lot", so the family is
   `negative_binomial`. Each plain label maps to one of the ten families: "A number"
   (`normal`), "A number with outliers" (`student_t`), "A positive amount" (`gamma`), "A
   count" (`poisson`), "A count with many zeros" (`zero_inflated_poisson`), "Yes / no"
   (`bernoulli`), "A share between 0 and 1" (`beta`), "A rating" (`ordinal`) and two more
   for counts. The [extension points reference](../reference/generated/extension-points.md#library-documents-no-code)
   lists them all.
3. **Terms.** One row for each driver:
    - **Transform** chips apply built-in steps to the driver first: `log1p`, `standardize`,
      `max_abs_scale`, `mean_scale`, `adstock`, `saturation`, `impute`.
    - **Prior** is one of `normal`, `student_t`, `laplace`, `half_normal`, `exponential` or
      `lognormal`, with numbers for its parameters. The small curve next to it shows which
      values the prior thinks are likely.
    - **Sign** is `any`, `positive` or `negative`. A sign cuts the prior at zero, so the
      model can never give a driver the wrong direction.
    - **Varies by** gives a driver its own slope in each level of a group.
4. **Groups.** Each group (up to three) gets its own intercept, partly pooled toward the
   others.
5. **Review.** A summary in words and the YAML. Then **Save**, **Use in Build** or **Fit
   now**.

There are no expressions in a declarative model: every choice comes from a closed list. This
is why anybody may make one, on any tier.

### What it compiles to

When you use the recipe, the platform compiles it to normal spec parts. This is the code the
server runs (`mapi_spec.model_recipes`), here with the catalog of this checkout and the schema
of a weekly table with one row for each week and store:

```python
--8<-- "extend_library.py:load"
```

```python
--8<-- "extend_library.py:schema"
```

```python
--8<-- "extend_library.py:model"
```

The pipeline first turns the week × store table into one row for each observation
(`core.to_long`), then applies the transforms of each term. The model block uses
`tabular.declarative_glm@^1`. The label `mapi.library/model` records which recipe and which
revision made the spec. A later revision of the recipe never changes a spec that you made
before.

!!! tip "If your data has the wrong shape"
    The builder checks the shape when you pick a dataset. If the platform cannot make one row
    for each observation (for example, the outcome and a driver have different dims), you get
    `LIBRARY_DATA_SHAPE` with a hint of what to change.

## A preset over a catalog model

```yaml
--8<-- "extend_model_preset.yaml"
```

`base` names any catalog model with a version range. `settings` are fixed. `exposed` lists
the settings that a person who uses the preset may change. The **Use** form shows only these
settings, made from the base model's settings schema:

```python
--8<-- "extend_library.py:preset"
```

In the app, you can also save a preset from the model panel of the spec editor: **Save as
preset**.

## Use a recipe

- **Build:** the palette lists your recipes under "From your team". Pick one, then pick a
  dataset.
- **Make your own › Models:** open a recipe, then **Use in Build** (a new draft spec) or
  **Fit now**.
- **Catalog › Yours:** "From your team" lists every recipe of your project and your tenant.

## Share a recipe

A new recipe belongs to your project. Every member of the project can use it. A tenant admin
can **promote** it: the latest revision is copied into a tenant item that every project of the
tenant can use. Items are never deleted, only archived, so a spec that used one still says
where it came from.

| Who | Can do |
|---|---|
| Viewer | Use project and tenant items |
| Editor | Make, revise and archive project items |
| Tenant admin | All of that, plus make and promote tenant items |

## The HTTP API

The app uses these routes; you can use them too (see the
[OpenAPI explorer](../reference/openapi.md)):

| Route | What it does |
|---|---|
| `GET /v1/library?kind=model_recipe` | List items |
| `POST /v1/library` | Make an item (`{"document": {...}}` or `{"text": "<YAML>"}`, `scope`) |
| `POST /v1/library/{item_id}/revisions` | Add a revision (`If-Match: "r<n>"`) |
| `POST /v1/library:validate` | Check a document, optionally against a dataset |
| `POST /v1/library/{item_id}:use` | Compile an item: a spec fragment for a model recipe |
| `POST /v1/library/{item_id}:promote` · `:archive` | Promote to the tenant, archive |

## What next

- Add your own prep steps to a recipe: [Prep presets](prep-presets.md).
- Need a model that a recipe cannot describe? Write one in Python:
  [Develop a model plugin](develop/models.md).
