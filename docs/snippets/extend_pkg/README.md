# acme

Extensions for modeling-api, scaffolded by `mapi plugin init`. Every id lives in the
`acme` namespace.

## What is inside

- **Model** `acme.group_mean` in `src/acme/model.py` (`GroupMeanModel`, entry point
  `mapi.models`), checked by `tests/test_model.py` (`mapi_sdk.testing.check_model`).
- **Preprocessing step** `acme.winsorize` in `src/acme/steps.py` (`WinsorizeStep`,
  entry point `mapi.steps`), checked by `tests/test_step.py` (`mapi_sdk.testing.check_step`).
- **Plot recipe** `acme.weekly_total` in `src/acme/plots.py` (`weekly_total`, entry point
  `mapi.plots`), checked by `tests/test_recipe.py` (`mapi_plots.testing.check_recipe`).
- **Workflow node** `test.acme_zero_share` in `src/acme/nodes.py` (`ZERO_SHARE`, entry point
  `mapi.workflow_nodes`), checked by `tests/test_node.py` (`mapi_workflows.kit.check_node`).
- **Workflow template** `acme_weekly_check` in `src/acme/templates.py` (`WEEKLY_CHECK`,
  entry point `mapi.workflow_templates`), checked by `tests/test_template.py`
  (`mapi_workflows.kit.check_template`).

Each kind has its own module, entry point and contract-kit test. Replace the example code
with yours and keep `mapi plugin test` green.

## Test it

```bash
mapi plugin test                 # the contract kits (tests/), with this interpreter
mapi plugin validate             # load the entry points, run every check, print the catalog
mapi --json plugin validate      # the same as one JSON document (for CI)
```

`validate` exits 0 when every check passes, 1 when a check fails and 2 when an entry point
cannot be loaded.

## See it on your stack (3 ways)

1. **In the platform checkout** (a workspace member): copy this folder to `plugins/`, run
   `make install` (`uv sync --all-packages`: it locks the new member and installs it, so its
   entry points exist), then `make dev`. The catalog is described again on the next start.
2. **Out of tree, while you develop it:** `make plugin-dev PLUGIN=path/to/acme` installs
   it in editable mode and adds it to the image digest, so `make dev` describes it again
   after every change. `make plugin-undev PLUGIN=...` removes it.
3. **A custom runner image:** build a wheel here (`uv build --wheel`), then, from the
   modeling-api checkout (`plugin-image` is a target of its Makefile),
   `make plugin-image BASE=base-bayes NAME=acme WHEELS="path/to/acme/dist/*.whl"`
   and route its namespace to the image with
   `MAPI_DOCKER_IMAGES='{"acme": "<image>"}'`.

Then run `mapi plugin doctor --server`: it lists every installed extension, the load
problems, the image routing and whether the server has described the current code.

## Use it

The model in a spec:

```yaml
model:
  use: acme.group_mean@^0.1
  settings: {shrinkage: 0.1}
```

The step in a spec pipeline:

```yaml
pipeline:
  - {id: cap, use: acme.winsorize@^0.1, with: {vars: [sales], upper: 0.99}}
```

The plot on a model (rendered after every fit):

```yaml
outputs:
  plots: [acme.weekly_total@^1]
```

The node in a workflow (branch conditions can read `share`, `reject` and `verdict_plain`):

```yaml
nodes:
  - {id: zeros, use: test.acme_zero_share, with: {var: sales, threshold: 0.1}, inputs: {data: inputs.data}}
```

Start a workflow from the template:

```bash
mapi workflows create --template acme_weekly_check
```

## See it in the app

- **Catalog › Yours** lists `acme.group_mean` with its About card and an "Installed by your
  team" badge; **Build** offers it in the model picker, and fitted versions open in the
  Model Explorer and in dashboard presets.
- **Build › pipeline palette** offers `acme.winsorize` with its About card; workflows can run it
  in a `transform.steps` node.
- **Catalog › Plots** shows the `acme.weekly_total` card with its example; **Try it** opens the
  playground with its settings form. Run pages list it under the model's plots, and
  workflows can draw it in a `plot.render` node.
- **Workflows › editor palette** lists `test.acme_zero_share` under its kind with its About card;
  the runner sandbox runs it like a built-in node.
- **Workflows › gallery** lists `acme_weekly_check` under "Installed"; start a workflow from
  it there.

If something is missing, a tenant admin sees why in Catalog › Yours (or run
`mapi plugin doctor`): nothing an entry point exports is dropped silently.
