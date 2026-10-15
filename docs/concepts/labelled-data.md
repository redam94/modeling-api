# Labelled data

Every model on modeling-api reads **one `xr.Dataset`**: variables with named dims, and dims
with coordinate labels. Nothing is positional, so the platform can reorder, subset, extend and
add labels safely, and you can score data whose stores are in a different order than at fit
time.

A **`DataSchema`** describes such a dataset: what each dim is and what may happen to it at
score time, and what each variable means to a model.

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

The **policy** is the heart of the contract: it says what a score request may do with a dim.

| Policy | For | At score time | Typical errors |
|---|---|---|---|
| `fixed` | channels, products, states | Only labels seen at fit (any subset, any order) | `UNSEEN_LEVELS` |
| `extend` | time, observation index | New labels appended **after** the fitted range, without gaps, up to `max_extension` | `NON_CONTIGUOUS_EXTENSION`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT` |
| `new_levels` | markets, stores, counties, households | Unseen labels allowed, handled by `new_level_strategy` | `UNSEEN_LEVELS` when the strategy is `error` |

`new_level_strategy` decides what an unseen level becomes:

| Strategy | Meaning | Example |
|---|---|---|
| `hyperprior` | A fresh draw from the fitted population distribution, per posterior draw | A new market in a hierarchical MMM: honest, wider uncertainty |
| `pooled` | The population average (or a frequency-weighted average of fitted levels) | A new store in an xgboost demand model |
| `zero` | The level contributes nothing | A new category in a one-hot model |
| `error` | Rejected | The default |

Other `DimSpec` fields:

- `kind`: `categorical`, `time`, `integer`, `float` or `index`;
- `freq`: a pandas frequency for time dims (`D`, `W-SUN`, `MS`), so the platform can generate
  forecast coordinates and check contiguity;
- `max_extension`: how many periods a forecast may reach past the fit without a refit;
- `ordered`: whether a categorical dim has a meaningful order.

!!! note "Policies only tighten"
    A model's schema template declares the loosest policy it can handle; your data's schema and
    the spec's `dim_policies` may only make it tighter (`fixed` is the tightest). Trying to
    loosen it is `DIM_POLICY_LOOSENED`.

## Variables and roles

| Field | Meaning |
|---|---|
| `dims`, `dtype` | The shape contract (`float64`, `int64`, `bool`, `str`, `datetime64[ns]`, …) |
| `role` | What the variable means: `target`, `covariate`, `media`, `spend`, `control`, `index`, `mask`, `constant`, `weight`, `exposure` |
| `units` | A pint-style string (`EUR`, `units`, `1`). Steps update it (`fx_convert` → `EUR`, scaling → `1`), and outputs report it |
| `missing` | `forbid` (default), `allow` (NaN is fine) or `mask` (a mask variable marks valid cells) |
| `missing_sentinel` | A code such as `-99.99` that means "missing" and is turned into NaN by `core.replace_sentinel` |
| `future_required` | Must be supplied over a forecast horizon (planned prices, promotions, budgets) |
| `index_of` | Long layout: this integer variable indexes that dim (`county_idx(obs)` → `county`) |
| `bounds` | Allowed value range, checked in plain language |

Roles let plugins and plot templates work across datasets: "every `spend` variable", "the
`target` over time, one line per geo".

## Two layouts for ragged data

Real panels are often ragged: households have different numbers of trips, animals different
track lengths. A schema uses one of two canonical forms, and the built-in steps `core.to_long`
and `core.to_padded` convert between them.

=== "Long"

    One row per observation along an `obs` dim, plus index variables:

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

`infer_schema` gives you a starting point: time dims become `extend` with an inferred
frequency, other dims `fixed`, variables `covariate` unless you say otherwise.

```python
--8<-- "concepts_data.py:infer"
```

## Data checks in plain language

`validate_dataset` checks a dataset against its schema and describes every problem for a
non-expert reader. Fitting and scoring run the same checks and stop on errors.

```python
--8<-- "concepts_data.py:validate"
```

## Binding: your names, the model's names

Steps and models are written against **canonical names** (`geo`, `time`, `y`, `spend`). A
spec's `data.bind` maps them to your dataset's names. The binding is stored in the bundle and
**reversed on every output**, so results come back in your vocabulary.

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

Small constant datasets that steps read (FX rates, holiday calendars) are **side tables**,
declared in the spec with a score-time policy:

| `score_policy` | At score time |
|---|---|
| `pinned` | The exact version used at fit time (holidays) |
| `latest` | The latest version of the table |
| `supplied` | The score request must name the table (`request.side_tables`), e.g. FX rates for the forecast period |

A step that needs a table with too little coverage fails with `SIDE_TABLE_COVERAGE`.

## Wire formats

| Format | Use | API |
|---|---|---|
| XrJSON | Small payloads, curl, TypeScript: the `Dataset.to_dict()` shape with explicit dtypes and caps | `mapi_sdk.wire.to_xrjson` / `from_xrjson` |
| zarr zip | Datasets and trees as single objects (zarr v3, deterministic bytes, zstd/blosc only) | `write_zarr_zip`, `read_zarr_zip`, `write_datatree_zip`, `read_datatree_zip` |
| Arrow IPC | Tidy tables for UI views and plots | `mapi_sdk.wire.to_arrow_table` |
| pickle | **Never**, across any boundary | – |

Next: [Pipelines and stages](pipelines.md).
