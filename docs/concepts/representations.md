# Representations and summaries

"One output shape for every model" sounds tidy and is quietly wrong. A conformal interval is
not a distribution; per-cell quantiles cannot be added up; a point prediction has no
uncertainty to summarise. So every model declares its **representation**, and the platform
uses it to decide which summaries and aggregations it will compute.

| Representation | Shape | Typical models | Summaries | Aggregate over cells |
|---|---|---|---|---|
| `draws` | `(chain, draw, …)` joint samples | PyMC (MCMC, VI), bootstrap | `mean`, `sd`, `median`, `hdi_<p>`, `eti_<p>`, `q<p>` | **any**, per draw |
| `quantiles` | `(quantile, …)` per cell | xgboost / sklearn quantile regression | `median`, `q<p>`, `eti_<p>`, `point` (interpolated, never extrapolated) | none |
| `interval` | `(bound, …)` + `alpha` | split conformal | `interval`, `point` | none |
| `point` | `(…)` | ridge, random forest, classifiers | `mean`, `point` | `sum`/`mean`, **flagged** |
| `moments` | `(moment, …)` mean and sd | analytic Gaussian processes | `mean`, `sd`, `eti_<p>`, `point` | none |

## Why aggregation depends on it

With **joint draws** the total over 40 stores is easy and exact: add the stores within each
draw, then summarise. With **quantiles** you cannot do that: the sum of 40 90% quantiles is
not the 90% quantile of the sum (it overstates it, often badly), and the quantiles say nothing
about how stores co-vary. **Conformal intervals** guarantee per-cell coverage only; a "summed
interval" would claim a coverage nobody measured.

The rules are data, in `mapi_spec`, and the summariser and the API both use them:

```python
--8<-- "concepts_representations.py:rules"
```

Asking for something the representation cannot support fails before any compute:

```text
CAPABILITY_UNSUPPORTED - cannot aggregate 'quantiles' output: Quantiles are marginal per
cell; summing quantiles is not a quantile of the sum.
```

## Asking for outputs

A score request's `output` block is an `OutputSelector`:

```yaml
output:
  kind: expected              # expected (the mean response) or predictive (with noise)
  summary: [mean, hdi_90]     # default depends on the representation
  draws: 200                  # also return 200 joint draws (draws representation only)
  aggregate: {over: [store]}  # reduce before summarising, if the representation allows
  units: original             # or: model (skip inverse transforms)
  names: dataset              # or: canonical (skip reverse binding)
```

Summary names are parsed, not looked up: `hdi_90`, `eti_80`, `q5`, `q97_5`, `median`, `sd`.
When you leave `summary` unset you get the representation's default (`mean, hdi_90` for draws,
`median, q5, q95` for quantiles, adapted to the fitted quantile grid).

## Expected vs predictive

- `expected` returns the model's mean response (for PyMC models, a `Deterministic` the plugin
  names), without observation noise. Use it for "what is the effect of the plan?".
- `predictive` returns draws of the observed variable, with noise. Use it for "what range of
  sales should we plan for?".

## Draws are thinned once, for the whole bundle

When a request asks for draws, they come from a **bundle-wide thinning index**: the same
`(chain, draw)` pairs for every output and every derived quantity. Contributions, response
curves and predictions taken from the same draws stay jointly consistent, so a Guided-mode
slider that combines them keeps honest uncertainty bands.

## Counterfactuals share their noise

In `mode: counterfactual` the platform scores a baseline and a scenario and returns `baseline/`
and `delta/` groups. Bayesian plugins use **common random numbers**: the predictive noise of the
two runs is the same draw, so the delta reflects the intervention, not two independent noise
samples.

Next: [Bundles](bundles.md).
