# Representations and summaries

Each model declares its **representation**. The platform uses the representation to decide
which summaries and aggregations it will calculate. One output shape for all models is not
correct. A conformal interval is not a distribution. You cannot add per-cell quantiles. A point
prediction has no uncertainty to summarise.

| Representation | Shape | Typical models | Summaries | Aggregate over cells |
|---|---|---|---|---|
| `draws` | `(chain, draw, …)` joint samples | PyMC (MCMC, VI), bootstrap, the `dl.*` networks (Laplace or MC dropout, v0.15) | `mean`, `sd`, `median`, `hdi_<p>`, `eti_<p>`, `q<p>` | **any**, per draw |
| `quantiles` | `(quantile, …)` per cell | xgboost / sklearn quantile regression | `median`, `q<p>`, `eti_<p>`, `point` (interpolated, never extrapolated) | none |
| `interval` | `(bound, …)` + `alpha` | split conformal | `interval`, `point` | none |
| `point` | `(…)` | ridge, random forest, classifiers | `mean`, `point` | `sum`/`mean`, **flagged** |
| `moments` | `(moment, …)` mean and sd | analytic Gaussian processes | `mean`, `sd`, `eti_<p>`, `point` | none |

## Why aggregation depends on it

The representation controls which aggregations are correct. With **joint draws**, the total
over 40 stores is easy and exact. Add the stores in each draw, then summarise. With
**quantiles**, you cannot do this. The sum of 40 90% quantiles is not the 90% quantile of the
sum. The sum is too large, frequently by a large quantity. Also, the quantiles do not tell how
the stores change together.

**Conformal intervals** guarantee the coverage of each cell only. A "summed interval" would
claim a coverage that nobody measured.

The rules are data in `mapi_spec`. The summariser and the API both use these rules:

```python
--8<-- "concepts_representations.py:rules"
```

If you ask for an output that the representation cannot supply, the request fails before
the platform starts compute:

```text
CAPABILITY_UNSUPPORTED - cannot aggregate 'quantiles' output: Quantiles are marginal per
cell; summing quantiles is not a quantile of the sum.
```

## Asking for outputs

The `output` block of a score request is an `OutputSelector`:

```yaml
output:
  kind: expected              # expected (the mean response) or predictive (with noise)
  summary: [mean, hdi_90]     # default depends on the representation
  draws: 200                  # also return 200 joint draws (draws representation only)
  aggregate: {over: [store]}  # reduce before summarising, if the representation allows
  units: original             # or: model (skip inverse transforms)
  names: dataset              # or: canonical (skip reverse binding)
```

The platform parses the summary names. It does not look them up in a list. Examples are
`hdi_90`, `eti_80`, `q5`, `q97_5`, `median` and `sd`. If you do not set `summary`, you get the
default of the representation. For draws, the default is `mean, hdi_90`. For quantiles, the
default is `median, q5, q95`, adapted to the fitted quantile grid.

## Expected vs predictive

- `expected` returns the mean response of the model, without observation noise. For PyMC
  models, this is a `Deterministic` that the plugin names. Use it for the question "what is
  the effect of the plan?".
- `predictive` returns draws of the observed variable, with noise. Use it for the question
  "what range of sales must we plan for?".

## Draws are thinned once, for the whole bundle

When a request asks for draws, the draws come from a **bundle-wide thinning index**. This
index uses the same `(chain, draw)` pairs for all outputs and all derived quantities.
Contributions, response curves and predictions from the same draws stay jointly consistent.
Thus, a Guided-mode slider that combines them keeps correct uncertainty bands.

## Counterfactuals share their noise

In `mode: counterfactual`, the platform scores a baseline and a scenario. It returns the
`baseline/` and `delta/` groups. Bayesian plugins use **common random numbers**. The
predictive noise of the two runs is the same draw. Thus, the delta shows the effect of the
intervention, not the difference between two independent noise samples.

## Outputs that are differences <span class="mapi-badge">v0.13</span> { #differences }

Some outputs are a difference between two values, for example the effect `y - y_hat` of
`causal.impact`. The inverse transform of a difference is not the difference of the inverse
transforms. For example, `exp(a - b)` is not `exp(a) - exp(b)`. Thus the model sets the attr
`difference_of` on the scored array. The attr names the reference output. With
`output.units: original`, the harness changes the level and the reference to the output
units, then subtracts them. This applies to draws and to point outputs. See
[Measure the effect of a change](causal-effects.md#differences).

## Circular outputs

An angle wraps at one turn. Thus a summary on a line is not correct for an angle. For draws
at 350 and 10 degrees, the linear mean is 180 degrees, but the correct mean is 0 degrees. A
model can declare an output as circular (`OutputDecl.circular`, see
[Capabilities](capabilities.md#outputs-on-a-grid-or-a-circle)). The model then sets a `period`
attr on the scored array, for example 360 for degrees or 24 for hours.

For a circular output with `draws`, the summariser uses these rules:

- `mean` is the circular mean of the draws.
- `sd` is the circular standard deviation, `sqrt(-2 ln R)`.
- `median`, each quantile and each interval (`hdi_<p>`, `eti_<p>`) come from the deviations
  of the draws from the circular mean. Thus each interval is centred on the circular mean.
- Each value is in `[0, period)`. If `lower` is more than `upper`, the interval crosses the
  seam (for example from 350 to 10 degrees).
- In `mode: counterfactual`, `delta/` is wrapped to `[-period/2, period/2)`.

The holdout metrics of a circular output use the same rules. Each table of `summary.json`
(`ScoreSummaryTable`) for a circular output has the field `period`. Thus a reader knows that
the statistics are on the circle.

The summary stays on a line if the scored array has no valid `period` attr, or if the request
aggregates the output over a dim. A sum of angles is not an angle. The result then has a
warning (`CAPABILITY_UNSUPPORTED`, severity `warning`).

Next: [Bundles](bundles.md).
