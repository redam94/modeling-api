---
description: Write a workflow node plugin for modeling-api - a JSON contract plus the function the runner sandbox calls, exported as NodeExport through mapi.workflow_nodes and checked by check_node.
---

# Develop a workflow node

A workflow node is one box in a workflow: a statistical test, a transform, a split, an
evaluation, a chart or a report. A plugin node has two parts:

- **A contract** (`NodeImplementation`): the id, ports, a closed JSON Schema for the
  parameters, the result fields that branch conditions can read and an About card. The
  workflow editor, the validator and the control plane read only this JSON. They never
  import your code.
- **A function** that the runner sandbox calls with a `NodeContext`.

Start from the scaffold:

```bash
mapi plugin init acme --kind node
```

## The scaffold

`src/acme/nodes.py` counts the share of zeros of a variable and gives a pass or fail verdict:

```python
--8<-- "extend_pkg/src/acme/nodes.py"
```

The rules (checked by the loader and by `check_node`):

- The id is `<kind>.<namespace>_<name>` (`test.acme_zero_share`) and `plugin` is your
  namespace. Node ids always have two parts.
- The kind is one of `test`, `transform`, `split`, `search`, `evaluate`, `plot`, `report`,
  `governance`. Never `branch`, `compare`, `select`, `model` or `subworkflow`: those run in
  the control plane or start fits.
- The node runs in the sandbox. Its ports use the closed port types (`dataset`,
  `test_result`, `metrics`, `table`, ...).
- The parameter schema passes the
  [schema-safety rules](../../reference/generated/extension-points.md#settings-and-parameter-schemas).
  `mapi_spec.node_authoring` (`node`, `about`, `string`, `num`, ...) builds a contract that
  does.
- The About card is complete.

An invalid contract does not reach the palette. It shows as `node_contract_invalid` in
`mapi plugin doctor` and in Catalog › Yours › Problems. A job whose node version is not the
installed contract's version is refused by the sandbox (`NODE_UNKNOWN`), so a workflow never
runs code that it was not validated against.

!!! note "The old `{id: fn}` export"
    A `mapi.workflow_nodes` entry point that exports a dict of functions still works, with a
    `deprecated` diagnostic. Export `NodeExport` objects: only they reach the editor palette.

## The entry point

```toml
[project.entry-points."mapi.workflow_nodes"]
acme = "acme.nodes:NODES"
```

## The contract kit

```python
--8<-- "extend_pkg/tests/test_node.py"
```

`check_node` checks the contract, runs the node on your inputs, then checks the outputs
against their port types, the result fields, the size of the result and, for a cacheable
node, that two runs agree. Import it from `mapi_workflows.kit`.

## Use it

```yaml
nodes:
  - {id: zeros, use: test.acme_zero_share, with: {var: sales, threshold: 0.1}, inputs: {data: inputs.data}}
```

A `branch` node can read `zeros.result.share`, `reject` and `verdict_plain`. Once installed,
the node is in the palette of the **Workflows** editor under its kind, with its About card.
Team templates and [plugin templates](workflow-templates.md) can use it.
