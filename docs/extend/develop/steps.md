---
description: Write a preprocessing step plugin for modeling-api - a Step class with fit and transform, registered through mapi.steps and checked by check_step.
---

# Develop a preprocessing step

A step changes the data before the model sees it. A stateful step learns something on the
training data only (`fit`) and applies it to any data (`transform`). Start from the scaffold:

```bash
mapi plugin init acme --kind step
```

## The scaffold

`src/acme/steps.py` caps extreme values at quantiles that it learns on the training data:

```python
--8<-- "extend_pkg/src/acme/steps.py"
```

The rules that matter:

- `transform` never learns. The output of `transform(train, fit(train))` must equal the
  output at fit time, and a subset of the data must give the same subset of the output.
- `var_params` names the settings that hold variable names, so the control plane checks them
  against the data before a run.
- `kind` and, when the step changes the schema, `effect_template` describe the output. The
  control plane validates specs with them and never imports your code.
- `transform` skips variables that are absent, such as the target at score time.

More rules (steps that look back, unseen levels, inverse transforms) are in
[Write a step or model plugin](../../guides/plugins.md#a-preprocessing-step).

## The entry point

```toml
[project.entry-points."mapi.steps"]
acme = "acme.steps:STEPS"
```

## The contract kit

```python
--8<-- "extend_pkg/tests/test_step.py"
```

`check_step` checks that fitting is deterministic, that `transform` is pure, that the state
survives a bundle round trip, that the declared effect matches the output, that an absent
target is accepted and that the About card is complete.

## Use it

In a spec pipeline:

```yaml
pipeline:
  - {id: cap, use: acme.winsorize@^0.1, with: {vars: [sales], upper: 0.99}}
```

Once installed, the step is in the pipeline palette of **Build** with its About card, and a
workflow can run it in a `transform.steps` node. Analysts can put it in a
[prep preset](../prep-presets.md) with slots, with no code.
