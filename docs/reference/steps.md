# Built-in steps (`core.*`)

The `mapi-steps` package contains the preprocessing steps. Each spec can use these steps, and
you do not write code. Each step is a small, typed transform that has a version. Each step has
these parts:

* A **settings** model. The settings get two validations. The API validates them against the
  exported JSON Schema. The runner validates them with Pydantic.
* A **fitted state**, for steps that learn something (scales, levels, a Fourier origin). A step
  learns its state from the training data only. At score time, the step uses the same state
  again. It does not fit the state again.
* A **static schema effect**. With this effect, the API can do a check of a full pipeline in
  milliseconds (names, dims, units, the split stage). It does not run step code.
* A **context need**, for steps that look back in time (lags, carry-over). Thus, when you score
  a forecast, the step sees the same history that the model used for the fit.

To refer to a step in a spec, give its id and a semver range, for example
`use: core.mean_scale@^1`.

!!! tip "Where the data is after each step"
    The API shows the schema at each stage (`POST /specs:validate`). Use it before you fit a
    model. It shows the variables, dims and units that each step makes.

## At a glance

| Step | Kind | Learns state | Invertible | Looks back | What it does |
|---|---|---|---|---|---|
| `core.resample` | reshape | – | – | – | Aggregates a time dim to a coarser frequency (`W-SUN`, `MS`, …). Removes partial periods |
| `core.count_events` | reshape | – | – | – | Counts the rows of an event table for each period (and each group) on a dense grid |
| `core.filter_coords` | reshape | – | – | – | Keeps or removes labels of a dim, or keeps an inclusive date range |
| `core.to_long` | reshape | – | – | – | Changes padded ragged data → one row for each observation, plus `*_idx` index variables |
| `core.to_padded` | reshape | – | – | – | Changes long ragged rows → a padded `(group, position)` grid with a mask |
| `core.stack` / `core.unstack` | reshape | – | – | – | Combines dims into one dim, and changes them back |
| `core.fx_convert` | value | – | ✓ | – | Converts currency with rates for each period from a side table |
| `core.mean_scale` | value | ✓ | ✓ | – | Divides by the mean absolute value over the named dims |
| `core.max_abs_scale` | value | ✓ | ✓ | – | Divides by the maximum absolute value over the named dims |
| `core.standardize` | value | ✓ | ✓ | – | `(x - mean) / sd` over the named dims |
| `core.log1p` | value | – | ✓ (non-linear) | – | `log(1 + x)` |
| `core.logit` | value | – | ✓ (non-linear) | – | `log(p / (1 - p))` for shares |
| `core.impute` | value | ✓ | – | – | Fills gaps: `mean`, `median`, `constant` or forward fill (`ffill`) |
| `core.replace_sentinel` | value | – | – | – | Changes codes such as `-99` (or values below or above a bound) to missing values |
| `core.clip` | value | – | – | – | Limits values to `[lower, upper]` |
| `core.fourier` | feature | ✓ (origin) | – | – | Seasonal `sin`/`cos` features |
| `core.trend` | feature | ✓ (origin, span) | – | – | A polynomial time trend, scaled to the fitted range |
| `core.calendar` | feature | – | – | – | Day of week, ISO week, month, quarter, and flags for weekend and month end |
| `core.holidays` | feature | – | – | – | Holiday days for each period, from a side table |
| `core.lag` | feature | – | – | ✓ | Lagged copies `{v}_lagged(…, lag)` in groups |
| `core.lag_tensor` | feature | – | – | ✓ | Replaces `v` with its lags `0 … l_max-1` (for carry-over in the model) |
| `core.one_hot` | feature | ✓ (levels) | – | – | Indicator columns for the categories that the fit saw |
| `core.encode_levels` | feature | ✓ (levels) | – | – | Integer codes for categories or dim labels |
| `core.missing_indicator` | feature | – | – | – | `{v}_missing` flags |
| `core.formula` | feature | – | – | – | A new variable from an arithmetic expression |
| `core.adstock` | value | – | – | ✓ | Carry-over with fixed parameters (point models) |
| `core.saturation` | value | – | – | – | Diminishing returns with fixed parameters (point models) |

**Reshape** steps change the structure. The runner never inverts them. Predictions go back
through the value steps until the last reshape. For example, an E33 model that has a fit on
weekly EUR data gives weekly EUR predictions. The runner inverts **value** steps that are
invertible when the predictions go out. For non-linear inverses (`log1p`, `logit`), point
predictions have the label `median`, or they use `retransform: smearing`.

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

1. `to_eur` multiplies the daily revenue and spend in local currency by the rate of the day.
   The `currencies` setting gives the currency of each market. Markets that are not in the list
   use `default_currency`. Thus, you can score a new market. Markets that are already in `EUR`
   use a rate of 1. The rates must cover each necessary day. If they do not, the step raises
   `SIDE_TABLE_COVERAGE`.
2. `weekly` adds the days into weeks that end on Sunday. It removes the partial first and last
   weeks. This is the default split stage, and the holdout is the last weeks.
3. The `scale_*` steps learn `scale(geo, channel)` and `scale(geo)` on the training weeks. A
   market that the fit did not see gets the average scale (`unseen_levels: pooled`).
4. `lags` changes `spend(geo, channel, time)` to `spend(geo, channel, time, lag)`. At fit time,
   it keeps the last 6 **weekly, scaled** rows for each market. When you forecast, the step puts
   those rows before the new rows. Thus, the first forecast week sees real history. A new
   market has no stored history. Thus, the step pads its first lags with its first value
   (`pad: first`).
5. `season` learns the origin week one time. Forecasts continue the same seasonal phase.
6. `hols` counts the holiday days in each week from the daily `holidays` table.

## Settings that need care

### Unseen levels

Some steps learn a state for each level (`mean_scale`, `max_abs_scale`, `standardize`,
`impute`, `one_hot`, `encode_levels`). These steps take `unseen_levels`. This setting tells
the step what to do when a score request has a level that was not in the training data:

| Policy | Scale and impute steps | `one_hot` | `encode_levels` |
|---|---|---|---|
| `error` (default) | `UNSEEN_LEVELS` (422) | `UNSEEN_LEVELS` | `UNSEEN_LEVELS` |
| `pooled` | The average of the learned values | The training frequencies of each level | One shared "new" code |
| `fill` | `fill_value` (neutral by default) | All zeros | `fill_code` |

### Looking back: `lag`, `lag_tensor`, `adstock`

`along` is the time dim. `within` gives the group dims. On long data, it gives row variables,
for example a household id. Each step keeps the last `n` rows for each group **at its own input
stage**. A forecast can start immediately after the fitted range. Then the step puts the stored
rows before the new rows. The result is the same as a transform of the full series. If the
request skips periods, the step raises `NON_CONTIGUOUS_EXTENSION`.

`core.lag` of a **target** is a recursive feature. The API rejects a forecast horizon that is
longer than the smallest lag with `RECURSIVE_UNSUPPORTED`. This rule does not apply if the
model does the recursion itself.

### Forward fill

`core.impute` with `method: ffill` moves the last observation forward along `along` in each
group. At fit time, the step also keeps the last observed value of each series. Some data
starts immediately after the fitted range (a holdout or a forecast). This data continues the
fitted series exactly, for all gap lengths. `limit` gives the maximum number of consecutive
missing values that the step fills. This number includes the missing rows at the end of the
training data.

### Full periods

`core.resample` with `full_periods_only: true` (the default) keeps a period only if the source
data covers each day of the period. The step compares with the source frequency (for example
`D`). Thus, a short February is also a full month.

## `core.formula`

```yaml
- {id: margin, use: core.formula@^1,
   with: {out: margin, expr: "(price - cost) / price", dims: [store, sku, time], units: "1"}}
```

The expression language has numbers, variable names and `'strings'` (for dim names). It has
these operators:

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

The result is `float64` and must have the declared `dims`. If the result has fewer dims, the
step broadcasts it. The language has no attribute lookups, indexing, imports or other calls.
The length and the nesting of expressions have limits.

!!! note
    The formula language makes specs easier to write. It is not a security boundary. Like all
    step code, it runs only in the runner sandbox.

## Precompute for point models: `adstock` and `saturation`

Bayesian models learn carry-over and saturation in the model. Point models (sklearn, ridge) get
them as preprocessing with fixed parameters. Each parameter is a number, or a mapping for each
level of `by`. For example, you can give a different decay to each channel:

```yaml
- {id: carry, use: core.adstock@^1,
   with: {vars: [spend], along: time, within: [geo], kind: geometric, l_max: 8,
          alpha: {tv: 0.7, search: 0.2, social: 0.4}, by: channel, normalize: true}}
- {id: sat, use: core.saturation@^1, with: {vars: [spend], kind: hill, slope: 1.2, half: 80}}
```

| `adstock` kind | Weights over lags `l = 0 … l_max-1` | Parameters |
|---|---|---|
| `geometric` | `alpha ** l` | `alpha` in [0, 1) |
| `delayed` | `alpha ** ((l - theta) ** 2)` | `alpha`, `theta` ≥ 0 |
| `weibull_pdf` | The Weibull density at `l + 1`, scaled to [0, 1] | `lam`, `k` > 0 |
| `weibull_cdf` | The cumulative survival, which starts at 1 | `lam`, `k` > 0 |

| `saturation` kind | Curve | Parameters |
|---|---|---|
| `logistic` | `(1 - exp(-lam x)) / (1 + exp(-lam x))` | `lam` |
| `hill` | `x^slope / (half^slope + x^slope)` | `slope`, `half` |
| `michaelis_menten` | `alpha x / (lam + x)` | `alpha`, `lam` |
| `tanh` | `alpha tanh(x / (alpha c))` | `alpha`, `c` |

## Splits

A spec sets its train and holdout split in `data.split`. The runner applies the split at the
**split stage**, after the stateless prefix. In E33, the split stage is after the weekly
resample, because the `week` dim starts to exist only there. The built-in kinds also have
catalog identities. Thus, the UI and the docs can describe them:

| Split | Use it for |
|---|---|
| `core.time_holdout` | The last `last` periods, a `fraction`, or all periods `after` a label |
| `core.as_of` | Backtests: train up to `as_of`, skip `gap` periods, and evaluate the remaining periods |
| `core.leave_group_out` | New markets or stores: hold out full groups |
| `core.last_per_group` | Panels and choice data: the last rows of each group that has a minimum of `min_len` rows |
| `core.mask_block` | Random blocks of cells over many dims (the model must accept missing targets) |

## The catalog the API uses

```bash
python -m mapi_steps.catalog            # Catalog JSON: steps, settings schemas, effects
python -m mapi_steps.catalog --splits   # … plus the split catalog
```

When the build of a runner image occurs, the build makes this JSON. The API validates specs
against the JSON, and it does not import step code. The package also contains the same file as
`mapi_steps/catalog.json`.

In a notebook, `mapi_steps.registry()` returns a registry with all the steps. The `mapi.steps`
entry point also gives the steps to `mapi_sdk.default_registry()`.
