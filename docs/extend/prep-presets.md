---
description: Save preprocessing steps as a reusable prep preset with slots, including a formula step for your own derived column - no code needed.
---

# Prep presets <span class="mapi-badge">v0.18</span>

A **prep preset** is a list of preprocessing steps that you save once and use again in any
spec. Its **slots** are the blanks that you fill when you use it: which columns to fill, which
dim is the week. So "fill the gaps in the spend columns, then add the promo price" works on
next month's dataset too.

Want a step of your own with no code? Add a `core.formula` step to a preset. It makes a new
column from an expression, such as `where(promo > 0, price, 0)`.

## Make one

There are two ways:

- **Make your own › Prep › New prep preset.** Pick the steps from the same palette as in
  Build (or with the keyboard), set their settings, and mark a setting as a slot.
- **Build › Save steps as preset.** In the visual builder, save the steps of the spec that
  you have open.

**Try it on a dataset** fills the slots and checks the steps with `POST /v1/specs:validate`,
so you see what each stage of the data looks like before you save.

## The document

```yaml
--8<-- "extend_prep_preset.yaml"
```

- `slots` names each blank. `kind` is `var` (one column), `var_list` (one or more columns) or
  `dim` (one dim). `label` is what the person who uses the preset reads. `role` says which
  role the columns have (for example `spend`). `required: false` makes a slot optional.
- A `with` value is a slot when the whole value is `"$slot:<name>"`, or when an item of a list
  is. A `var_list` slot in a list is spliced into the list.
- Step ids get a prefix when you use the preset (`p_fill`), so a preset never clashes with the
  steps of your spec.

## Use it

When you add the preset to a spec, you fill its slots. This is the code that the server runs
(`mapi_spec.prep_presets.apply_prep_preset`), with the catalog of this checkout:

```python
--8<-- "extend_library.py:prep"
```

The app checks the result before you save the spec: each slot names a column or dim that the
data has, each step exists in the catalog, and the effects of the steps fit together (a step
never reads a column that an earlier step removed). An issue points at the slot or the
setting that causes it, for example `/slots/gaps` or `/steps/1/with/expr`.

The spec records the preset and its revision with the label `mapi.library/prep`.

## Formula steps

A `core.formula` step adds one column, `out`, from `expr`:

- Arithmetic `+ - * / **`, comparisons, and `& | ~` for logic.
- Functions with one argument: `log`, `log1p`, `exp`, `expm1`, `sqrt`, `abs`, `sign`,
  `floor`, `ceil`, `sin`, `cos`, `tanh`, `expit`, `logit`, `isnull`, `notnull`. With two:
  `minimum`, `maximum`, `fillna`. With three: `clip`, `where`.
- Reductions over a named dim: `sum(x, 'week')`, `mean`, `max`, `min`.
- `var('odd-name')` for a column whose name is not a plain identifier.

A formula names its columns, so a slot cannot change them. When you save a preset, the
platform checks only the length (at most 4000 characters) and that the text is plain ASCII.
The full check runs in the sandbox when the model is fitted: if the expression is wrong, the
fit fails with a plain message that names the step.

## Share it

Prep presets follow the same rules as [model recipes](your-model.md#share-a-recipe): project
items for editors, tenant items for tenant admins, archive instead of delete. A model recipe
can name a prep preset (`prep: {item: lib_..., revision: latest}`), so the recipe and its
preprocessing travel together.

## What next

- Need a step that a formula cannot express, with state learned on the training data? Write
  one in Python: [Develop a step](develop/steps.md).
