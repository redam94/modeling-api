# Labelled data

Each model on modeling-api reads **one `xr.Dataset`**. The dataset has variables with named
dims, and dims with coordinate labels. Nothing depends on position. Thus the platform can
safely reorder, subset and extend the data, and add labels. You can also score data that has
its stores in a different sequence than at fit time.

A **`DataSchema`** describes such a dataset. It tells what each dim is and what can occur to
it at score time. It also tells what each variable means to a model.

## A schema in one look

```yaml
layout: dense                       # or: long (one row per observation + *_idx variables)
dims:
  market:  {kind: categorical, policy: new_levels, new_level_strategy: hyperprior}
  channel: {kind: categorical, policy: fixed}
  date:    {kind: time, policy: extend, freq: D}
vars:
  revenue: {dims: [market, date], dtype: float64, role: target, units: EUR}
  spend:   {dims: [market, channel, date], dtype: float64, role: spend, units: EUR,
            future_required: true}
```

## Dims and their policies

The **policy** is the most important part of the contract. It tells what a score request can
do with a dim.

| Policy | For | At score time | Typical errors |
|---|---|---|---|
| `fixed` | channels, products, states | Only labels from the fit (any subset, any sequence) | `UNSEEN_LEVELS` |
| `extend` | time, observation index | New labels **after** the fitted range, without gaps, up to `max_extension` | `NON_CONTIGUOUS_EXTENSION`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT` |
| `new_levels` | markets, stores, counties, households | New labels are permitted. `new_level_strategy` controls them. | `UNSEEN_LEVELS` when the strategy is `error` |

`new_level_strategy` tells what a new level becomes:

| Strategy | Meaning | Example |
|---|---|---|
| `hyperprior` | A new draw from the fitted population distribution, for each posterior draw | A new market in a hierarchical MMM: the uncertainty is honest and wider |
| `pooled` | The population average (or an average of fitted levels, weighted by frequency) | A new store in an xgboost demand model |
| `zero` | The level adds nothing | A new category in a one-hot model |
| `error` | The platform refuses the level | The default |

Other `DimSpec` fields:

- `kind`: `categorical`, `time`, `integer`, `float` or `index`.
- `freq`: a pandas frequency for time dims (`D`, `W-SUN`, `MS`). With it, the platform can
  make forecast coordinates and do a check for gaps.
- `max_extension`: the maximum number of periods that a forecast can go past the fit without a
  new fit.
- `ordered`: tells if a categorical dim has a sequence that has a meaning.

!!! note "Policies only tighten"
    The schema template of a model declares the least strict policy that the model can use.
    The schema of your data and the `dim_policies` of the spec can only make it stricter
    (`fixed` is the strictest). If you try to make it less strict, you get
    `DIM_POLICY_LOOSENED`.

## Variables and roles

| Field | Meaning |
|---|---|
| `dims`, `dtype` | The shape contract (`float64`, `int64`, `bool`, `str`, `datetime64[ns]`, …) |
| `role` | What the variable means: `target`, `covariate`, `media`, `spend`, `control`, `index`, `mask`, `constant`, `weight`, `exposure` |
| `units` | A string in pint style (`EUR`, `units`, `1`). Steps update it (`fx_convert` → `EUR`, scaling → `1`), and outputs report it |
| `missing` | `forbid` (default), `allow` (NaN is permitted) or `mask` (a mask variable shows the valid cells) |
| `missing_sentinel` | A code, for example `-99.99`, that means "missing". `core.replace_sentinel` changes it to NaN |
| `future_required` | You must supply values over a forecast horizon (planned prices, promotions, budgets) |
| `index_of` | Long layout: this integer variable is the index of that dim (`county_idx(obs)` → `county`) |
| `bounds` | The permitted range of values. The platform does a check and reports in plain language |

With roles, plugins and plot templates work across datasets. For example, "each `spend`
variable" or "the `target` over time, one line for each geo".

## Two layouts for ragged data

Real panels are frequently ragged. For example, households have different numbers of trips,
and animals have tracks of different lengths. A schema uses one of two canonical forms. The
built-in steps `core.to_long` and `core.to_padded` change data from one form to the other.

=== "Long"

    One row for each observation along an `obs` dim, plus index variables:

    ```yaml
    layout: long
    dims:
      obs:    {kind: integer, policy: extend}
      county: {kind: categorical, policy: new_levels, new_level_strategy: hyperprior}
    vars:
      log_radon:  {dims: [obs], dtype: float64, role: target}
      county_idx: {dims: [obs], dtype: int64, role: index, index_of: county}
    ```

=== "Padded"

    A dense cube plus a mask variable:

    ```yaml
    dims:
      animal: {kind: categorical, policy: new_levels, new_level_strategy: pooled}
      step:   {kind: integer, policy: extend}
    vars:
      y:    {dims: [animal, step], dtype: float64, role: target, missing: mask}
      mask: {dims: [animal, step], dtype: bool, role: mask}
    ragged:
      - {group_dim: animal, within_dim: step, form: padded, mask_var: mask}
    ```

## Draft a schema from data

`infer_schema` gives you a schema to start with. Time dims become `extend` with a frequency
that the function finds. Other dims become `fixed`. Variables become `covariate`, unless you
give a different role.

```python
--8<-- "concepts_data.py:infer"
```

## Data checks in plain language

`validate_dataset` does a check of a dataset against its schema. It describes each problem for
a reader who is not an expert. The fit and the score do the same checks and stop if they find
errors.

```python
--8<-- "concepts_data.py:validate"
```

## Binding: your names, the model's names

Steps and models use **canonical names** (`geo`, `time`, `y`, `spend`). The `data.bind` of a
spec maps these names to the names of your dataset. The bundle stores the binding. The
platform **reverses the binding on each output**. Thus the results use your names.

```python
--8<-- "concepts_data.py:binding"
```

```yaml
data:
  bind:
    dims: {geo: market, time: date}     # canonical: yours
    vars: {y: revenue, spend: spend}
```

## Side tables

**Side tables** are small constant datasets that steps read (FX rates, holiday calendars). The
spec declares each side table with a policy for score time:

| `score_policy` | At score time |
|---|---|
| `pinned` | The exact version from the fit (holidays) |
| `latest` | The latest version of the table |
| `supplied` | The score request must name the table (`request.side_tables`), for example FX rates for the forecast period |

If a step needs a table that does not have sufficient coverage, the step fails with
`SIDE_TABLE_COVERAGE`.

A side table that is a tree dataset reads one node, which `node` names (v0.8):
`fx: {ref: dataset://growth/reference@latest, node: /fx}`.

## Tree datasets <span class="mapi-badge">v0.8</span> { #tree-datasets }

A dataset version can also hold an `xr.DataTree`: a tree of **nodes**, each with its own
variables. Node paths identify the nodes (`/` is the root, `/instrument/psf` a group below
`/instrument`). NetCDF-4 and HDF5 files with groups give trees, and so do versions made from
several files. A node sees the coords of the nodes above it. Thus a dim that a node shares
with a node above it has the same labels and the same `DimSpec`.

A **`TreeSchema`** describes a tree: one `DataSchema` for each node. A node without variables
(a pure group) has an empty schema.

```yaml
nodes:
  /:                                # the shared coords, no variables
    dims:
      market: {kind: categorical}
      date:   {kind: time, policy: extend, freq: W-SUN}
  /sales:                           # each node declares the dims that its variables use
    dims:
      market: {kind: categorical}
      date:   {kind: time, policy: extend, freq: W-SUN}
    vars:
      revenue: {dims: [market, date], dtype: float64, role: target, units: EUR}
  /media:
    dims:
      market:  {kind: categorical}
      channel: {kind: categorical}
      date:    {kind: time, policy: extend, freq: W-SUN}
    vars:
      spend: {dims: [market, channel, date], dtype: float64, role: spend, units: EUR}
```

A model still reads one flat `xr.Dataset`. The spec selects nodes in `data.nodes`, and the
platform merges their variables into that dataset before the pipeline
(`TreeSchema.flat` statically, `mapi_sdk.tree.select_nodes` on the arrays). A model that
declares `capabilities.input = "tree"` also gets the whole tree. Refer to
[Tree datasets, HDF5 and multi-file versions](../guides/tree-data.md).

## Wire formats

| Format | Use | API |
|---|---|---|
| XrJSON | Small payloads, curl, TypeScript: the `Dataset.to_dict()` shape with explicit dtypes and limits | `mapi_sdk.wire.to_xrjson` / `from_xrjson` |
| zarr zip | Datasets and trees as single objects (zarr v3, deterministic bytes, zstd/blosc only). A tree dataset version is a `data.tree.zip` | `write_zarr_zip`, `read_zarr_zip`, `write_datatree_zip`, `read_datatree_zip`, `mapi_sdk.tree.write_tree_zip` / `read_tree_zip` (with the tree limits) |
| Arrow IPC | Tidy tables for UI views and plots | `mapi_sdk.wire.to_arrow_table` |
| pickle | **Never**, across all boundaries | – |

Next: [Pipelines and stages](pipelines.md).
