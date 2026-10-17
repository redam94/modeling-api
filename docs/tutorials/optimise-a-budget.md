# Optimise a budget

You have a fitted marketing-mix model (`mmm.bayesian` or `mmm.frequentist`) and next
quarter's budget to split. The platform answers with mmm-framework's planning module,
straight from the model's posterior: which channel should get what, how sure that is, and
what each extra euro would buy. No external optimiser, no copy of the model elsewhere.

**You need:** a ready model version of an MMM (the [Guided tutorial](../guides/guided-tutorial.md)
or the [marketing-mix guide](../guides/mmm.md) gets you one) and a token with
`runs:create` on the project.

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

- `budget_change_pct: 0` reallocates today's total (or give `total_budget`, or
  `mode: "free"` with `value_per_kpi` to let the plan choose the total).
- Bounds keep the plan inside what the model has seen: here TV may move between half and
  one and a half times its current spend. Unlisted channels move within `default_bounds`
  (0 to 2x).
- `objective: "p10"` would plan for a cautious outcome (the 10th percentile of the KPI
  across posterior draws) instead of the expected one.

=== "Python"

    ```python
    --8<-- "client_v05_more.py:optimize"
    ```

=== "Command line"

    ```bash
    --8<-- "cli_v05.sh:optimize"
    ```

    The plan prints as a table (current and recommended spend, change, the next unit's
    return, a note when a channel goes beyond the spend the model has seen), then the
    frontier; `--json` gives the whole `Optimization`.

The answer is an `Optimization`: `200` with the plan when the sandboxed run finished
within `Prefer: wait`, else `202` with the run - poll `GET /v1/optimizations/{run_id}`
until `done` (the client and the CLI do this for you, resending with the same
`Idempotency-Key` so the plan is computed once).

## 2. Read the plan

The `result` of a real plan - the demo's two-channel model from the
[Python client guide](../guides/python-client.md), `objective: "mean"`, five frontier points,
rounded and shortened - computed in 9 seconds of sandbox time on a laptop:

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

On this model the honest answer is "move a little from search to TV, but do not expect
much": the plan adds about 10,000 EUR on average, but the uplift's 90% range runs from -61,000
to +70,000 and only 62.5% of the posterior draws prefer it. At the plan both channels return
the same 3.48 per extra euro, which is what an optimum looks like.

- **`uplift`** is the plan's KPI minus today's plan's, per posterior draw;
  **`prob_positive_uplift`** the chance the change helps at all.
- **`marginal_roi`** is what the next euro on each channel returns at the plan; at a good
  plan the channels that are not at a bound return about the same.
- **`stability`** is the share of posterior draws whose own best plan moves the channel
  the same way - low values mean the data cannot tell.
- **`expected_regret`** is the KPI left on the table by committing to one plan under
  uncertainty.
- **`shadow_price`** is what the next unit of money would add to the KPI at this plan;
  **`extrapolated`** marks a channel pushed beyond the spend range seen in the data.
- **`frontier`** shows the best plan at other totals: invest while its marginal ROI stays
  above what a euro is worth to you.
- **`notes_plain`** says, for example, when a channel is pushed beyond the spend the model
  has seen (its numbers then extrapolate the response curve).

The run's plot, `mmm.budget_plans`, compares today's allocation and expected KPI with the
plan's (`GET /v1/runs/{run_id}/plots`).

## 3. Check it with a forecast

A plan is a model's answer, not a measurement. Before you commit, score the plan's weekly
spend as a forecast (`mode: forecast`, `inputs_stage: weekly`) and, better, test the
biggest move with a lift test - then add the result to the model's `calibration` block so
the next plan knows.
