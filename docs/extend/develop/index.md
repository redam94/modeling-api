---
description: Develop a modeling-api plugin in Python - scaffold every extension kind with mapi plugin init, check it with mapi plugin validate and diagnose an installation with mapi plugin doctor.
---

# Develop a plugin <span class="mapi-badge">v0.18</span>

A **plugin** is a Python package that adds models, preprocessing steps, plot recipes,
workflow nodes and workflow templates to the catalog. The runner finds them through entry
points; the control plane reads only the catalog that the runner exports, never your code.

This page is the workflow: scaffold, test, validate, install, diagnose. The pages for each
kind explain the code. For a guided walk through all of it, follow part 2 of
[Your first extensions](../../tutorials/your-first-extensions.md).

!!! info "Who installs plugins"
    Plugin code runs in the runner sandbox with the same rights as the built-in plugins. In
    v0.18 only the operator of a deployment installs it, on a stack they run
    ([three ways](run-on-your-stack.md)). Analysts who want their own pieces with no code use
    [Make your own](../index.md). Upload to a shared deployment is
    [coming in 0.19](../upload-roadmap.md).

## 1. Scaffold

```bash
--8<-- "extend_cli.sh:init"
```

`mapi plugin init NAME` writes a package with one module, one entry point and one
contract-kit test for each `--kind` (repeat the option for more kinds):

| `--kind` | Module | Id | Exported as |
|---|---|---|---|
| `model` | `model.py` | `acme.group_mean` | `MODELS` in `mapi.models` |
| `step` | `steps.py` | `acme.winsorize` | `STEPS` in `mapi.steps` |
| `recipe` | `plots.py` | `acme.weekly_total` | `RECIPES` in `mapi.plots` |
| `node` | `nodes.py` | `test.acme_zero_share` | `NODES` in `mapi.workflow_nodes` |
| `template` | `templates.py` | `acme_weekly_check` | `TEMPLATES` in `mapi.workflow_templates` |

Other options: `--base custom|pymc|sklearn` picks the base class of the model, `--namespace`
sets the id namespace (default: the module name), `--dir` the parent directory and `--force`
writes into a directory that is not empty.

The package has a filled-in About card for each kind, a tiny `examples/weekly.csv` and a
README that ends with "See it in the app". The pages of this section show its files from
`docs/snippets/extend_pkg` in the repository: a docs test keeps that copy equal to what
`mapi plugin init` writes.

## 2. Test

```bash
--8<-- "extend_cli.sh:test"
```

`mapi plugin test [PATH]` runs the package's tests with the interpreter of the `mapi` CLI
(extra arguments go to pytest; `--contract-only` runs only the tests marked
`mapi_contract`). Each kind has its [contract kit](contract-kits.md): it checks the rules
that the platform depends on, before anybody else runs your code.

## 3. Validate

```bash
--8<-- "extend_cli.sh:validate"
```

`mapi plugin validate [PATH]` loads only this package's entry points, in a subprocess with
the package's `src/` first on the path, so the package need not be installed. It prints each
check and the catalog fragment that the package adds:

```text
[PASS] entry_point mapi.models:acme: loads
[PASS] model acme.group_mean: registers
[PASS] model acme.group_mean: namespace
[PASS] model acme.group_mean: about
...
[PASS] node test.acme_zero_share: contract
[PASS] template acme_weekly_check: template
catalog: 1 models, 1 steps, 1 plots, 1 workflow nodes, 1 workflow templates
all checks passed
```

The exit code is 0 when every check passes, 1 when a check fails and 2 when an entry point
cannot be loaded. `--json` is a global option of the CLI, so it goes before `plugin`.

## 4. Install

Pick one of the [three ways to run it on your stack](run-on-your-stack.md). On a source
checkout, the short one is:

```bash
make plugin-dev PLUGIN=../acme
```

Then restart `make dev`: the new image digest makes the runner describe the catalog again.

## 5. Diagnose

```bash
--8<-- "extend_cli.sh:doctor"
```

`mapi plugin doctor` (or `make plugin-doctor`) lists every installed non-built-in entry
point in the six groups, the catalog diagnostics, the image routing of each namespace and
whether the image digest covers each extension. `--server` (`make plugin-doctor SERVER=1`)
also compares your local catalog with the server's. Exit codes: 0 when nothing is wrong, 1
when it found a problem, 2 when the checks or the server could not run.

Nothing that an entry point exports is dropped in silence. A tenant admin sees the same
problems in **Catalog › Yours › Problems**.

## Namespaces

Every id lives in your namespace: `acme.group_mean`, `acme.winsorize`, `acme.weekly_total`.
Workflow nodes keep a two-part id that starts with their kind, `test.acme_zero_share`, and
name the namespace in `contract.plugin`. Workflow templates are `acme_weekly_check`.

Some namespaces are not yours to use: the built-in ones (`core`, `mmm`, `ts`, `tabular`,
`sci`, ...) and the ones the platform keeps (`mapi`, `lib`, `team`, `tenant`, every
`t_...`). A plugin that tries is refused with `namespace_reserved`. The
[extension points reference](../../reference/generated/extension-points.md#namespaces) has
the full lists.

## About cards

Each model, step, recipe, node and template has an About card: what it does, when to use it
and when not to, what it needs, what it assumes, what it gives you, how hard it is and how
long it takes. Analysts read it in the Catalog, in Help me choose and next to each picker.
The kits check it with `require_about=True`, and the catalog marks an incomplete card with
`about_incomplete`.

Write it for an analyst, in plain words, as the scaffold does. See
[About cards and choosing](../../concepts/about-and-choosing.md).

## The pages for each kind

- [Models](models.md) · [Steps](steps.md) · [Plot recipes](plot-recipes.md) ·
  [Workflow nodes](workflow-nodes.md) · [Workflow templates](workflow-templates.md)
- [Contract kits](contract-kits.md) · [Run on your stack](run-on-your-stack.md)
- [Extension points reference](../../reference/generated/extension-points.md)
