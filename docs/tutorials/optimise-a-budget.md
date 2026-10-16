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

```json
{
  "allocation": [
    {"channel": "tv", "current_spend": 600000, "optimal_spend": 450000,
     "change_pct": -25, "marginal_roi": 1.4, "stability": 0.82},
    {"channel": "search", "current_spend": 400000, "optimal_spend": 550000,
     "change_pct": 37.5, "marginal_roi": 1.4, "stability": 0.9}
  ],
  "uplift": {"mean": 120000, "sd": 60000, "quantiles": {"0.05": 25000, "0.95": 220000}},
  "prob_positive_uplift": 0.97,
  "expected_regret": 8000,
  "frontier": [{"total_budget": 400000, "expected_kpi": 3100000}, "..."]
}
```

- **`uplift`** is the plan's KPI minus today's plan's, per posterior draw;
  **`prob_positive_uplift`** the chance the change helps at all.
- **`marginal_roi`** is what the next euro on each channel returns at the plan; at a good
  plan the channels that are not at a bound return about the same.
- **`stability`** is the share of posterior draws whose own best plan moves the channel
  the same way - low values mean the data cannot tell.
- **`expected_regret`** is the KPI left on the table by committing to one plan under
  uncertainty.
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
