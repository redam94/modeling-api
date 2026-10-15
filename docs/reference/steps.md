# Built-in steps (`core.*`)

The `mapi-steps` package ships the preprocessing steps every spec can use without writing
code. Each step is a small, versioned, typed transform with:

* a **settings** model (validated twice: by the API against the exported JSON Schema, and by
  the runner with Pydantic);
* a **fitted state** for steps that learn something (scales, levels, a Fourier origin). States
  are learned on the training data only and replayed — never refitted — at score time;
* a **static schema effect**, so the API can check a whole pipeline (names, dims, units, the
  split stage) in milliseconds without running any step code;
* a **context need** for steps that look back in time (lags, carry-over), so scoring a forecast
  sees exactly the history the model was fitted with.

Reference a step in a spec by id and semver range, e.g. `use: core.mean_scale@^1`.

!!! tip "Where the data is after each step"
    The API shows the schema at every stage (`POST /specs:validate`). Use it to see which
    variables, dims and units each step produces before you fit anything.

## At a glance

| Step | Kind | Learns state | Invertible | Looks back | What it does |
|---|---|---|---|---|---|
| `core.resample` | reshape | – | – | – | Aggregate a time dim to a coarser frequency (`W-SUN`, `MS`, …); drops partial periods |
| `core.count_events` | reshape | – | – | – | Count rows of an event table per period (and per group) on a dense grid |
| `core.filter_coords` | reshape | – | – | – | Keep/drop labels of a dim, or an inclusive date range |
| `core.to_long` | reshape | – | – | – | Padded ragged data → one row per observation plus `*_idx` index variables |
| `core.to_padded` | reshape | – | – | – | Long ragged rows → padded `(group, position)` grid with a mask |
| `core.stack` / `core.unstack` | reshape | – | – | – | Combine dims into one, and back |
| `core.fx_convert` | value | – | ✓ | – | Currency conversion with per-period rates from a side table |
| `core.mean_scale` | value | ✓ | ✓ | – | Divide by the mean absolute value over named dims |
| `core.max_abs_scale` | value | ✓ | ✓ | – | Divide by the maximum absolute value over named dims |
| `core.standardize` | value | ✓ | ✓ | – | `(x - mean) / sd` over named dims |
| `core.log1p` | value | – | ✓ (non-linear) | – | `log(1 + x)` |
| `core.logit` | value | – | ✓ (non-linear) | – | `log(p / (1 - p))` for shares |
| `core.impute` | value | ✓ | – | – | Fill gaps: `mean`, `median`, `constant` or forward fill (`ffill`) |
| `core.replace_sentinel` | value | – | – | – | Turn codes such as `-99` (or values below/above a bound) into missing |
| `core.clip` | value | – | – | – | Cap values to `[lower, upper]` |
| `core.fourier` | feature | ✓ (origin) | – | – | Seasonal `sin`/`cos` features |
| `core.trend` | feature | ✓ (origin, span) | – | – | Polynomial time trend scaled to the fitted range |
| `core.calendar` | feature | – | – | – | Day of week, ISO week, month, quarter, weekend/month-end flags |
| `core.holidays` | feature | – | – | – | Holiday days per period from a side table |
| `core.lag` | feature | – | – | ✓ | Lagged copies `{v}_lagged(…, lag)` within groups |
| `core.lag_tensor` | feature | – | – | ✓ | Replace `v` by its lags `0 … l_max-1` (for in-model carry-over) |
| `core.one_hot` | feature | ✓ (levels) | – | – | Indicator columns for categories seen at fit |
| `core.encode_levels` | feature | ✓ (levels) | – | – | Integer codes for categories or dim labels |
| `core.missing_indicator` | feature | – | – | – | `{v}_missing` flags |
| `core.formula` | feature | – | – | – | A new variable from an arithmetic expression |
| `core.adstock` | value | – | – | ✓ | Carry-over with fixed parameters (point models) |
| `core.saturation` | value | – | – | – | Diminishing returns with fixed parameters (point models) |

**Reshape** steps change structure and are never inverted: predictions come back through value
steps as far as the last reshape (an E33 model fitted on weekly EUR returns weekly EUR).
**Value** steps that are invertible are undone on the way out. Non-linear inverses (`log1p`,
`logit`) label point predictions as `median` or use `retransform: smearing`.

## The E33 pipeline, step by step

```yaml
pipeline:
  - {id: to_eur,      use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, to: EUR,
                                                     currency_dim: geo, currencies: {UK: GBP},
                                                     default_currency: EUR}}
  - {id: weekly,      use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y,     use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags,        use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 6, pad: first}}
  - {id: season,      use: core.fourier@^1,    with: {along: time, period: 52.18, order: 3}}
  - {id: hols,        use: core.holidays@^1,   with: {table: holidays, along: time}}
```

1. `to_eur` multiplies daily local-currency revenue and spend by the day's rate. Markets map
   to currencies with `currencies`, and anything unlisted uses `default_currency`, so a new
   market can still be scored. Markets already in `EUR` use a rate of 1. Rates must cover
   every needed day; otherwise the step raises `SIDE_TABLE_COVERAGE`.
2. `weekly` sums days into Sunday-ending weeks and drops the partial first and last weeks.
   This is the default split stage, and the holdout is the last weeks.
3. `scale_*` learn `scale(geo, channel)` and `scale(geo)` on the training weeks. A market that
   wasn't seen at fit gets the average scale (`unseen_levels: pooled`).
4. `lags` turns `spend(geo, channel, time)` into `spend(geo, channel, time, lag)`. It stores
   the last 6 **weekly, scaled** rows per market at fit time. When you forecast, those rows are
   prepended, so the first forecast week sees real history. A brand-new market has no stored
   history, so its earliest lags are padded with its first value (`pad: first`).
5. `season` learns the origin week once. Forecasts continue the same seasonal phase.
6. `hols` counts holiday days per week from the daily `holidays` table.

## Settings that need care

### Unseen levels

Steps that learn per-level state (`mean_scale`, `max_abs_scale`, `standardize`, `impute`,
`one_hot`, `encode_levels`) take `unseen_levels`. It decides what happens when a score request
contains a level that wasn't in the training data:

| Policy | Scaling/impute steps | `one_hot` | `encode_levels` |
|---|---|---|---|
| `error` (default) | `UNSEEN_LEVELS` (422) | `UNSEEN_LEVELS` | `UNSEEN_LEVELS` |
| `pooled` | average of the learned values | training frequencies of each level | one shared "new" code |
| `fill` | `fill_value` (neutral by default) | all zeros | `fill_code` |

### Looking back: `lag`, `lag_tensor`, `adstock`

`along` is the time dim. `within` lists the group dims; on long data it lists row variables
such as a household id. Each step stores the last `n` rows per group **at its own input
stage**. When a forecast starts right after the fitted range, the stored rows are prepended
and the result is identical to transforming the whole series. If the request skips periods,
the step raises `NON_CONTIGUOUS_EXTENSION`.

`core.lag` of a **target** is a recursive feature: a forecast horizon longer than the smallest
lag is rejected with `RECURSIVE_UNSUPPORTED` unless the model handles recursion itself.

### Forward fill

`core.impute` with `method: ffill` carries the last observation along `along` within groups.
At fit time it also stores the last observed value per series. Data that starts right after
the fitted range (a holdout or a forecast) therefore continues the fitted series exactly, for
any gap length. `limit` caps how many consecutive missing values are filled, counting the
missing rows at the end of the training data.

### Full periods

`core.resample` with `full_periods_only: true` (the default) keeps a period only if the source
data covers every day of it. It checks this against the source frequency (for example `D`),
so a short February still counts as a full month.

## `core.formula`

```yaml
- {id: margin, use: core.formula@^1,
   with: {out: margin, expr: "(price - cost) / price", dims: [store, sku, time], units: "1"}}
```

The expression language has numbers, variable names and `'strings'` (for dim names). It
supports these operators:

* `+ - * / **`
* comparisons `< <= > >= == !=`
* `& | ~`
* parentheses

| Functions | |
|---|---|
| element-wise | `log log1p exp expm1 sqrt abs sign floor ceil sin cos tanh expit logit` |
| two arguments | `minimum maximum fillna` |
| three arguments | `clip(x, lo, hi)`, `where(cond, a, b)` |
| missing values | `isnull notnull` |
| reductions | `sum mean max min` — `sum(spend, 'channel')` |
| names that are not identifiers | `var('unit-price')` |

The result is `float64` and must have the declared `dims`; it is broadcast when it has fewer.
There are no attribute lookups, indexing, imports or other calls, and expressions are limited
in length and nesting.

!!! note
    The formula language exists to make specs easier to write. It is not a security boundary:
    like all step code, it only ever runs inside the sandboxed runner.

## Precompute for point models: `adstock` and `saturation`

Bayesian models learn carry-over and saturation inside the model. Point models (sklearn,
ridge) get them as preprocessing with fixed parameters. Each parameter is either a number or a
mapping per level of `by`, for example a different decay per channel:

```yaml
- {id: carry, use: core.adstock@^1,
   with: {vars: [spend], along: time, within: [geo], kind: geometric, l_max: 8,
          alpha: {tv: 0.7, search: 0.2, social: 0.4}, by: channel, normalize: true}}
- {id: sat, use: core.saturation@^1, with: {vars: [spend], kind: hill, slope: 1.2, half: 80}}
```

| `adstock` kind | weights over lags `l = 0 … l_max-1` | parameters |
|---|---|---|
| `geometric` | `alpha ** l` | `alpha` in [0, 1) |
| `delayed` | `alpha ** ((l - theta) ** 2)` | `alpha`, `theta` ≥ 0 |
| `weibull_pdf` | Weibull density at `l + 1`, scaled to [0, 1] | `lam`, `k` > 0 |
| `weibull_cdf` | cumulative survival, starting at 1 | `lam`, `k` > 0 |

| `saturation` kind | curve | parameters |
|---|---|---|
| `logistic` | `(1 - exp(-lam x)) / (1 + exp(-lam x))` | `lam` |
| `hill` | `x^slope / (half^slope + x^slope)` | `slope`, `half` |
| `michaelis_menten` | `alpha x / (lam + x)` | `alpha`, `lam` |
| `tanh` | `alpha tanh(x / (alpha c))` | `alpha`, `c` |

## Splits

A spec chooses its train/holdout split in `data.split`. The split is applied at the **split
stage**, after the stateless prefix. In E33 that is after weekly resampling, because the `week`
dim only exists from there on. The built-in kinds also have catalog identities, so the UI and
the docs can describe them:

| Split | Use it for |
|---|---|
| `core.time_holdout` | the last `last` periods, a `fraction`, or everything `after` a label |
| `core.as_of` | backtests: train up to `as_of`, skip `gap` periods, evaluate the rest |
| `core.leave_group_out` | new markets or stores: hold out whole groups |
| `core.last_per_group` | panels and choice data: the last rows of each group with at least `min_len` rows |
| `core.mask_block` | random blocks of cells over several dims (the model must accept missing targets) |

## The catalog the API uses

```bash
python -m mapi_steps.catalog            # Catalog JSON: steps, settings schemas, effects
python -m mapi_steps.catalog --splits   # … plus the split catalog
```

Runner images generate this JSON when they are built, and the API validates specs against it
without importing step code. The same file ships inside the package as
`mapi_steps/catalog.json`.

In a notebook, `mapi_steps.registry()` returns a registry with every step. The `mapi.steps`
entry point also makes them available to `mapi_sdk.default_registry()`.
