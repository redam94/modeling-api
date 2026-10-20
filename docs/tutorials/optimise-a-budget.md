# Optimise a budget

You have a fitted marketing-mix model (`mmm.bayesian`, `mmm.frequentist`, or from v0.12
`mmm.nested` or `mmm.multivariate`). You must divide the budget for the next quarter. The
platform answers with the planning module of
mmm-framework, directly from the posterior of the model. It tells you how much each channel
must get and how sure that is. It also tells you what each extra euro would buy. You do not
need an external optimiser or a copy of the model in a different location.

**You need:** a ready model version of an MMM and a token with `runs:create` on the project.
To get a model version, use the [Guided tutorial](../guides/guided-tutorial.md) or the
[marketing-mix guide](../guides/mmm.md).

## 1. Ask for a plan

```bash
curl -s -X POST "$API/v1/models/$MODEL/versions/$VERSION:optimize" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: $PROJECT" \
  -H "Prefer: wait=60" -H "Content-Type: application/json" -d '{
    "budget_change_pct": 0,
    "channels": {"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
    "objective": "mean",
    "frontier": {"n_points": 8}
  }'
```

- `budget_change_pct: 0` divides the current total again. Alternatively, give
  `total_budget`. To let the plan select the total, give `mode: "free"` with
  `value_per_kpi`.
- Bounds keep the plan inside the data that the model has seen. Here, TV can move between
  half and one and a half times its current spend. Channels that are not in the list move
  inside `default_bounds` (0 to 2x).
- `objective: "p10"` makes a plan for a cautious result instead of the expected result. The
  cautious result is the 10th percentile of the KPI across posterior draws.

=== "Python"

    ```python
    --8<-- "client_v05_more.py:optimize"
    ```

=== "Command line"

    ```bash
    --8<-- "cli_v05.sh:optimize"
    ```

    The command shows the plan as a table, and then the frontier. The table gives the
    current and recommended spend, the change and the return of the next unit. It also
    gives a note when a channel goes above the spend that the model has seen. `--json`
    gives the full `Optimization`.

The answer is an `Optimization`. If the sandboxed run finished within `Prefer: wait`, you
get `200` with the plan. If not, you get `202` with the run. Then poll
`GET /v1/optimizations/{run_id}` until `done`. The client and the CLI do this for you. They
send the request again with the same `Idempotency-Key`, so that the platform calculates the
plan only one time.

## 2. Read the plan

This is the `result` of a real plan. It is for the two-channel demo model from the
[Python client guide](../guides/python-client.md), with `objective: "mean"` and five frontier
points. We rounded and shortened the values. The plan took 9 seconds of sandbox time on a
laptop:

```json
{
  "format": "mapi.optimize/v1",
  "objective_label": "expected KPI (posterior mean)",
  "kpi": "revenue_local", "kpi_units": "EUR", "spend_units": "EUR",
  "total_budget": 639176, "current_total": 639176,
  "allocation": [
    {"channel": "tv", "current_spend": 370845, "optimal_spend": 417971,
     "change_pct": 12.7, "marginal_roi": 3.48, "stability": 0.705, "extrapolated": false,
     "expected_contribution": {"mean": 1782506, "sd": 191376, "quantiles": {"0.05": 1479072, "0.95": 2104367}}},
    {"channel": "search", "current_spend": 268331, "optimal_spend": 221205,
     "change_pct": -17.6, "marginal_roi": 3.48, "stability": 0.705, "extrapolated": false,
     "expected_contribution": {"mean": 963528, "sd": 222208, "quantiles": {"0.05": 645055, "0.95": 1335924}}}
  ],
  "expected_kpi": {"mean": 16588448, "sd": 62359},
  "current_kpi": {"mean": 16578641, "sd": 43247},
  "uplift": {"mean": 9807, "sd": 42867, "quantiles": {"0.05": -60803, "0.5": 12370, "0.95": 70045}},
  "prob_positive_uplift": 0.625,
  "expected_regret": 45817,
  "shadow_price": 3.48,
  "frontier": [
    {"total_budget": 255670, "expected_kpi": 15069639, "kpi_p5": 14864907, "kpi_p95": 15285515,
     "marginal_roi": 4.12, "allocation": {"tv": 185422, "search": 70248}},
    "…"
  ],
  "n_draws": 200,
  "notes_plain": ["Frontier optimizes the expected KPI (posterior mean) at each budget; …"]
}
```

For this model, the honest answer is "move a small amount from search to TV, but do not
expect much". On average, the plan adds about 10,000 EUR. But the 90% range of the uplift is
from -61,000 to +70,000. Only 62.5% of the posterior draws prefer the plan. With the plan,
the two channels return the same 3.48 for each extra euro. This is what an optimum looks
like.

- **`uplift`** is the KPI of the plan minus the KPI of the current plan, for each posterior
  draw. **`prob_positive_uplift`** is the probability that the change helps.
- **`marginal_roi`** is the return of the next euro on each channel, with the plan. In a good
  plan, the channels that are not at a bound return about the same.
- **`stability`** is the fraction of posterior draws whose own best plan moves the channel
  in the same direction. Low values tell you that the data cannot give an answer.
- **`expected_regret`** is the KPI that you lose because you select one plan when there is
  uncertainty.
- **`shadow_price`** is the KPI that the next unit of money would add, with this plan.
  **`extrapolated`** marks a channel that the plan pushes above the spend range in the data.
- **`frontier`** shows the best plan at other totals. Invest while its marginal ROI stays
  above the value that a euro has for you.
- **`notes_plain`** gives notes. For example, it tells you when the plan pushes a channel
  above the spend that the model has seen. Then the numbers of that channel extrapolate the
  response curve.

For `mmm.multivariate`, the KPI of the plan is the weighted total of the outcomes. Set the
weights in `outcomes[].weight` of the spec. A note of the plan gives the weights. See
[The multivariate model](../models/mmm-framework.md#multivariate).

The plot of the run, `mmm.budget_plans`, compares the current allocation and expected KPI
with those of the plan (`GET /v1/runs/{run_id}/plots`).

## 3. Check it with a forecast

A plan is the answer of a model. It is not a measurement. Before you commit, do these steps:

1. Score the weekly spend of the plan as a forecast (`mode: forecast`,
   `inputs_stage: weekly`).
2. If possible, test the largest move with a lift test.
3. Add the result to the `calibration` block of the model, so that the next plan uses it.
