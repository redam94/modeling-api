---
description: Optimise a marketing budget against a modeling-api endpoint with Atlas - mapi_client.atlas.RemoteModel scores candidate allocations in batches, caches them and backs off on rate limits.
---

# Optimise a budget with Atlas <span class="mapi-badge">v0.4</span>

[Atlas](https://github.com/redam94/optimizer-framework) optimises budget allocations
against any model that has its model interface. `mapi_client.atlas.RemoteModel` gives an
[endpoint](endpoints-and-webhooks.md) that interface: each candidate allocation the optimiser
tries is scored by the endpoint's current model version, so the fitted posterior - not a
local copy - decides where the next euro goes, and promoting a better model changes the
optimiser's answers without touching its code.

The examples come from `docs/snippets/atlas_budget.py`, which runs offline against a stand-in
endpoint with a concave response whose optimum is known in closed form, so the page can
check that the optimiser finds it.

## Install

```bash
pip install 'mapi-client[atlas]'                         # the client and the xarray runtime
pip install git+https://github.com/redam94/optimizer-framework   # Atlas itself
```

The `atlas` extra installs only what the adapter needs (xarray and the SDK's wire format).
Atlas is installed separately: it is AGPL-licensed, so the Apache-2.0 client never imports
it - `RemoteModel` matches Atlas's interface *structurally* (`predict(x) -> xr.DataArray`,
`contributions(x) -> xr.Dataset`, `model_type`, `required_dimensions`) and works with any
code written against `atlas.core.interfaces.ModelProtocol`.

Give the program a token that can only score this endpoint:

```bash
mapi tokens create budget-optimiser -s score:budget-planner --rate-limit 600
```

## Wrap the endpoint

```python
--8<-- "atlas_budget.py:model"
```

```text
8634.97622707262
```

- **KPI.** `predict` returns the endpoint's output variable - the only one, or the one
  named like the target; set `kpi=` (e.g. `"y_hat"`) when there are several - summed over
  every other dim (`reduce="sum"`; `"mean"` and `"none"` too). It reads one summary
  statistic, chosen by the endpoint's representation: the `mean` of a Bayesian (draws),
  point or moments model, the `median` of a quantile model such as `tabular.regressor`,
  the `point` of an interval model (`stat=` overrides it). Summing per-cell medians is an
  approximation; prefer endpoints with draws for totals across many cells. With
  `output="draws"` (and `draws=100`, at most 300) a draws endpoint returns posterior draws
  along a `sample` dim instead, for risk-aware objectives (a quantile, an expected
  shortfall).
- **Allocations.** `allocation({"tv": 4000, ...})` builds the payload from a budget per
  channel (`spend_var=` and `channel_dim=` name them; by default the endpoint's only spend
  variable along `channel`). With `base=` - a dataset holding the other inputs (controls,
  prices, the weeks of the plan) - each channel's flighting across weeks and markets is kept
  and rescaled to its budget, and every input the allocation lacks is sent from the base.
- **Scoring.** Every payload is first transposed to the dim order of the endpoint's input
  schema (the server checks each variable's dims in order; `Client.endpoint(alias)` does the
  same unless `conform=False`). Candidates go inline as XrJSON in the call's `data` (`payload_field="scenario"`
  for counterfactual endpoints); `call={"mode": "forecast", "horizon": {...}}` adds any other
  `EndpointScoreCall` field. Each call waits with `Prefer: wait=60` and collects slow runs
  with the same idempotency key.
- **`contributions(x)`** returns every output variable of the endpoint for `x`, unreduced -
  including the decomposition variables an MMM returns.

## Batching, caching and rate limits

An optimiser evaluates many nearby allocations - finite differences, line searches,
populations. `RemoteModel` keeps that cheap:

- **Batching.** `predict_batch(xs)` scores a list of allocations. With `batch_dim=` - a dim of
  the endpoint's input that accepts new coordinates (a categorical dim with policy
  `new_levels`, or an integer/index dim with `extend`) and that the model scores
  independently, such as a `scenario` dim - up to `max_batch` (64) candidates are stacked
  along it into **one** score request and split again from the result. Without `batch_dim`
  each candidate is its own request. Only batch along a dim the model really treats
  independently: a hierarchical model's `geo` dim does not qualify (new markets are pooled).
  The endpoint's schema is checked first, and a dim it cannot extend is refused.
- **Caching.** Results are cached by the exact payload (an LRU of `cache_size=1024`), so
  re-evaluating a point costs nothing; `cache_info()` reports hits, misses and requests.
- **Rate limits.** The client already waits out short `429 RATE_LIMITED` answers. When the
  server asks for a longer wait, `RemoteModel` retries up to `rate_limit_retries` (5) times
  with exponential backoff (`backoff=1` s, doubling, capped at `max_backoff=60` s, honouring
  `Retry-After`) and the same idempotency key. `QUOTA_EXCEEDED` is raised at once: waiting
  will not help until the quota window resets.

## Optimise

Atlas's `OptimizerProtocol` is `optimize(model, constraints, config) -> OptimizationResult`.
This SLSQP optimiser keeps the total budget fixed and scores each iteration's point and its
central finite differences in one batched request:

```python
--8<-- "atlas_budget.py:optimizer"
```

```text
{'tv': 5750, 'search': 2500, 'social': 1750}
CacheInfo(hits=0, misses=71, requests=11, size=71)
```

71 candidate allocations cost 11 score requests. The optimum matches the closed form
(`a_c / (b_c + x_c)` equal across channels). With a real endpoint, each request is one
sandboxed score run, so the wall time is dominated by run start-up; batching is what keeps a
gradient optimiser to a few minutes.

## With Atlas installed

Code that checks `isinstance(model, AbstractModel)` accepts `RemoteModel` once it is
registered as a virtual subclass (this imports Atlas, so call it only where Atlas is
installed):

```python
--8<-- "atlas_budget.py:atlas"
```

Atlas optimisers can then return Atlas's own `OptimizationResult`. In this repository,
`make test-atlas` runs the adapter's tests with Atlas installed from a sibling checkout
(`ATLAS_DIR=../optimizer-framework`) into a throwaway environment overlay - Atlas is never a
locked dependency of the platform.

Against a running stack (a `tabular.regressor` fitted on 1,800 rows of the same concave
response, promoted to an endpoint), this optimiser found `tv 5534, search 2233, social 2233`
- the closed form is `5750 / 2500 / 1750`; the gap is the gradient-boosted model's own
error - from 98 candidate allocations in 14 batched score requests, in about 140 seconds.

!!! note "Optimising in the sandbox"
    `RemoteModel` drives the optimiser from your machine, one score run per batch. For long
    optimisations the plan's in-sandbox `optimize` job (M4) runs Atlas next to the model,
    without a network round trip per batch.
