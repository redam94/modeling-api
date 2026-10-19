---
description: Complete, tested workflow recipes - unit roots, differencing, AR/MA order search, a rolling-origin backtest against naive, selection and governance - with the Python client and the CLI commands to run them, plus VAR/VECM cointegration, a forecast bake-off and two scientific recipes (a regression across species on a tree, and calibrate-then-design for a heated plate).
---

# Workflow cookbook <span class="mapi-badge">v0.5</span>

This page gives five complete analyses. Each analysis is one YAML document. You can save,
run and schedule it, and give it to a colleague. The tests of the documentation validate each
document on this page against the node catalog and run it from start to end. Thus the
documents that you copy operate correctly. For the format, see
[Workflows](../concepts/workflows.md). For all nodes, see the
[node reference](../reference/workflow-nodes.md). For short parts with one pattern each, see
the [workflows guide](workflows.md#cookbook).

| Recipe | The question it answers |
|---|---|
| [Is my weekly KPI ready to forecast?](#readiness-to-arimax) | Does the series need differencing? Which ARIMA orders are correct? Is ARIMAX better than a naive forecast on honest backtests? Can we use it? |
| [Do these series move together?](#varvecm-cointegration) | Do some series have a long-run relation (VECM), or a relation only in their changes (VAR)? Does one series help to predict a different series? |
| [Which forecasting model?](#forecast-bake-off) | Which of SARIMAX, ARIMA, ETS and Theta forecasts best on the same folds? Is the advantage of the winner more than chance? |
| [Does an effect across species stay when the tree is in the model?](#trait-regression-across-species) | Which correlation of relatives (none, Brownian motion, Pagel's lambda, Ornstein-Uhlenbeck) predicts best? Is the slope of the predictor clear of zero in the winner? |
| [Can I trust a calibration to design with it?](#calibrate-then-design) | What are the material and the losses of a heated plate? Does the calibration predict a run that it did not see? What temperature field does a new heater layout give? |

## Readiness to ARIMAX

**In plain words.** Before you trust a forecast, you must know four things:

- If the series "wanders". If it does, you must model its changes, not its levels.
- How much of the past is important.
- If the model forecasts better than the obvious rule "next week is like this week".
- If the model found a real effect, not noise.

This workflow answers each question with the standard statistical tool. It records each
decision with the numbers that caused it. It keeps the model only if the model is better than
the benchmarks. It also records the governance evidence.

**For the statistician.** ADF, KPSS, Phillips-Perron and Zivot-Andrews (one endogenous break)
decide `d` with a conservative rule: difference the series unless the evidence for
stationarity is clear on the two sides. Seasonal strength (STL) decides `D`. The ARMA orders
come from a grid of information criteria over `p ≤ max_p`, `q ≤ max_q`. The workflow fits
each point as ARIMA(p, d, q) on the levels. Thus the model estimates `d`, and does not apply
it to data that is already differenced. The report shows ACF/PACF for the reader.

The backtest is a rolling origin with a fixed training window, a gap and some horizons. The
workflow scores ARIMAX and the naive and seasonal-naive benchmarks on the same folds. A
Diebold-Mariano test with the Harvey-Leybourne-Newbold correction tells if the difference in
squared errors at horizon 1 is significant. `select.best` keeps the model only if its MAE is
better than each benchmark in `must_beat`.

```mermaid
flowchart LR
  T["adf · kpss · pp · za<br/>seasonal strength"] --> B{"wanders?<br/>seasonal?"}
  B -- then --> D["difference /<br/>seasonal difference"]
  T --> O["AR/MA order<br/>search (AIC)"]
  B --> O
  O --> BT["rolling-origin backtest<br/>ARIMAX"]
  F["folds: window 104,<br/>gap 1, h = 1, 4, 8"] --> BT
  F --> N["naive and<br/>seasonal naive"]
  BT --> C["compare +<br/>Diebold-Mariano"]
  N --> C
  C --> S{"select:<br/>beats naive?"}
  FIT["fit on all data<br/>(13-week holdout first)"] --> S
  S --> G["governance policy<br/>(null tests, benchmarks...)"]
  G --> P["evidence pack<br/>when it passed"]
  S --> R["report"]
```

### The document

```yaml
--8<-- "cookbook_readiness_arimax.yaml"
```

Copy these three details into your own workflows:

- **Alternatives keep the graph honest.** `diff` reads `[sdiff.data, inputs.data]`. This is
  the seasonally differenced series if that node ran. If not, it is the raw series. `acf`
  uses the series with the most processing that exists. The workflow calculates nothing two
  times. If the workflow does not take a branch, nothing breaks: skipped nodes are
  *skipped*, not failed.
- **Decisions move as values.** `$expr: "[nodes.order.best_p, if(nodes.wanders.value, 1, 0), nodes.order.best_q]"`
  changes the search result and the branch decision into the `order` of the model. The
  server parses the expression language and never uses `eval` on it. The run records each
  value that it read.
- **The fit has its own holdout.** `split.holdout` keeps the last 13 weeks out of the first
  fit (`mode: evaluate_then_refit`). Thus the governance checks that need unseen data have
  it. These checks are the null tests, the naive benchmark and the holdout accuracy. Then the
  workflow fits the model again on all the data.

### Run it with the CLI

```bash
--8<-- "cli_v05.sh:workflow"
```

`mapi workflows init` writes the `ts_readiness_arimax` template of the gallery with your
name and project. This template is the same analysis with an expanding-window backtest. To
get the rolling origin and the evidence pack, save the document above as
`weekly-readiness.yaml` instead. `--watch` shows the status of each node during the run. On a
terminal, it draws the status again in the same place. In a log, it writes one line for each
change. At the end, it shows the summary.

The output below is from a real run on a local stack. It is the second run on this data. The
unit-root tests, the holdout split and the fit did not change after the first run. Thus the
run **used them again from its cache** and did not calculate them again (the output is
shortened):

```text
started workflow run run_01m53hawnnyn7cyeptj70cxkk0 of workflow://growth/weekly-readiness
following workflow run run_01m53hawnnyn7cyeptj70cxkk0 (23 nodes)
  run started
  adf cached
  kpss cached
  pp cached
  za cached
  seas cached
  folds queued
  holdout cached
  wanders took the then arm
  seasonal took the else arm
  sdiff skipped: branch 'seasonal' took the else path
  …
  backtest succeeded
  compare succeeded
  select succeeded
  dm succeeded
  governance succeeded
  trusted took the then arm
  pack succeeded
  run succeeded

 node       ┃ kind       ┃ status      ┃ took  ┃ found
━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 adf        │ test       │ ✓ cached    │ 0.0s  │ The series wanders: consider differencing it. (cached
            │            │             │       │ from run_01m53h6m9np4gtfzvrkwrr4bh9)
 za         │ test       │ ✓ cached    │ 0.0s  │ The series wanders even allowing for one break:
            │            │             │       │ consider differencing it. (cached from …)
 seas       │ test       │ ✓ cached    │ 0.0s  │ No clear repeating pattern was found. (cached from …)
 wanders    │ branch     │ ✓ succeeded │ 0.0s  │ took the then arm
 order      │ search     │ ✓ succeeded │ 22.9s │ {"best_p": 1, "best_q": 1}
 sdiff      │ transform  │ – skipped   │ 0.0s  │ branch 'seasonal' took the else path
 dm         │ compare    │ ✓ succeeded │ 18.7s │ The difference between backtest and naive at
            │            │             │       │ horizon 1 is possibly luck.
 acf        │ test       │ ✓ succeeded │ 13.4s │ No past period is related to the present beyond
            │            │             │       │ chance.
 select     │ select     │ ✓ succeeded │ 0.0s  │ backtest has the best average error (1.78) and beats
            │            │             │       │ every benchmark (the strongest, naiv…
 governance │ governance │ ✓ succeeded │ 48.5s │ Every blocking check passed (7 check(s) ran);
            │            │             │       │ warnings on null.permutation_target, stabil…
 …
Decisions
 branch   ┃ arm  ┃ condition                                   ┃ read
━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 wanders  │ then │ (not nodes.adf.reject or nodes.kpss.reject) │ {"nodes.pp.reject": false,
          │      │ and not (nodes.pp.reject and                │ "nodes.za.reject": false,
          │      │ nodes.za.reject)                            │ "nodes.adf.reject": false,
          │      │                                             │ "nodes.kpss.reject": true}
 seasonal │ else │ nodes.seas.seasonal                         │ {"nodes.seas.seasonal": false}
 trusted  │ then │ nodes.governance.passed                     │ {"nodes.governance.passed": true}
Outputs
 output   ┃ type          ┃ where
━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 model    │ model_version │ model://growth/wf-weekly-readiness-fit@v1
 …
```

The full run took 1.6 minutes on a laptop. The live board goes to standard error, and the
summary goes to standard output. Thus `mapi workflows run … --watch > summary.txt` keeps only
the summary.

Then examine the results, download them and run the workflow automatically:

```bash
--8<-- "cli_v05.sh:inspect"
```

```bash
--8<-- "cli_v05.sh:automate"
```

### Run it with the Python client

Do these steps:

1. Validate the file on the server. The server saves nothing.
2. Make the workflow, or save a new revision.
3. Run the workflow.
4. Follow the live events.

```python
--8<-- "client_v05.py:validate"
```

```python
--8<-- "client_v05.py:run"
```

Read the results. They include the verdict of each test, each branch with the values that it
read, the selected orders and the Diebold-Mariano verdict. They also tell why the workflow
selected the model or did not select it:

```python
--8<-- "client_v05.py:inspect"
```

The governance node measured the selected model against the policy that applies. Because
`submit: true` is set, it recorded a validation of that model version:

```python
--8<-- "client_v05.py:governance"
```

This is the output on a live local stack, from the first run of the walkthrough on this data:

```text
passed
validated
data.quality                 pass    No data problems found (156 periods of history).
convergence                  pass    The fit reported no convergence problems.
baseline.beats_naive         pass    On the 13 held-out periods the model's error is 25% lower than 'same as last period' (the strongest simple benchmark).
holdout.accuracy             pass    On data it was not fitted on, the model's average percentage error is 1.8% (limit 15%).
residuals.whiteness          pass    No pattern is left in the errors (Ljung-Box p = 0.39 at 10 lags).
null.permutation_target      pass    The model did better on held-out data (skill 0.236) than all 20 fits with the drivers price shuffled in blocks of 7 (the result untouched) (p = 0.048).
stability.rolling_params     fail    ar.L1 changes a lot across windows of history (variation 1205%): the relationship may not be stable over time.
```

Read the output as a reviewer does. On unseen weeks, the model is 25% better than naive. Its
errors have no pattern. It forecasts better than each fit with the price driver shuffled in
time. But the AR coefficient is not stable across windows.

That check *failed*, but the version is still `validated`. The status of a check and its
severity are different things. The default forecast policy sets stability to *information*,
so the check does not block. The reviewer can see it in the record and in the evidence pack.
A stricter policy (see [governance](governance.md)) makes the check block. Then this model
needs more history or a simpler specification. This is the purpose: the evidence is written,
and the decision is explicit.

!!! note "What the shuffle test measures on a random walk"
    The model has a driver. Thus the test shuffles the driver in blocks and does not change
    the result. It scores each fit by its skill against the null forecast on the held-out
    weeks. Persistence ("next week is like this week") helps the shuffled fits exactly as much
    as it helps the real fit. Thus the test measures what the driver adds to persistence. For
    a model without drivers, the test uses shuffled changes of the series
    ([null tests explained](null-tests.md)).

Then keep the workflow up to date:

```python
--8<-- "client_v05.py:automate"
```

## VAR/VECM cointegration

**In plain words.** Some series wander but stay together. Examples are the prices of close
substitutes, sales and distribution, and income and consumption. If they have a relation, a
model that knows the relation (an error-correction model) forecasts better. It also tells you
how fast a gap closes. If they do not have a relation, model their changes. In the two cases,
find out if one series helps to predict a different series.

**For the statistician.** The Johansen trace test (at 5%) on the levels decides the branch.
If the series have a relation, `search.coint_rank` selects the rank. Then the workflow fits a
VECM with `k_ar_diff: 1` on the levels. If the series do not have a relation, the workflow
differences them. Then AIC selects the VAR lag order up to `maxlags`, and the workflow fits a
VAR on the changes. If the workflow took the changes (the first alternative), it tests Granger
causality on them. If not, it tests on the levels. The forecast node uses the model that
exists, with the related data.

```yaml
--8<-- "cookbook_var_vecm.yaml"
```

The data keeps the series along one dimension, with one value for each series and period
(`value(series, quarter)`). This is the layout of `ts.var` and `ts.vecm`. To ingest a wide
sheet, use the `wide` mapping. Run the workflow as the first recipe, with the two parameters
that it needs:

```bash
mapi workflows apply macro-system.yaml
mapi workflows run macro-system -i data=dataset://growth/us_macro@latest \
  -p target=realcons -p driver=realgdp --watch
```

```python
run = api.workflows.run(
    wf.id,
    inputs={"data": "dataset://growth/us_macro@latest"},
    params={"target": "realcons", "driver": "realgdp", "maxlags": 4},
    wait=True,
)
nodes = {n.node_id: n for n in run.nodes}
print(nodes["johansen"].result["verdict_plain"])  # "The series share at least one long-run relationship."
print(nodes["granger"].result["verdict_plain"])   # "Past values of the driver help predict the result."
print([n.node_id for n in run.nodes if n.status.value == "skipped"])  # the branch not taken
```

A run on a laptop used three simulated weekly series. Two of the series have the same
stochastic trend (`target=a`, `driver=b`). The run printed the two verdicts above and
`['diff', 'lags', 'var']`. The Johansen trace statistic (69.1) set the rank to 1, and the VECM
branch ran. Granger found that `b` helps to predict `a` (p = 0.0015). See
[Macro and VAR analysis](../use-cases.md#macro-and-var-analysis).

The [Model Explorer](model-explorer.md) shows the impulse responses, the variance
decomposition, the Granger table and the Johansen table of the fitted model
(`mapi explore model … -s irf -s fevd`).

## Forecast bake-off

**In plain words.** If you do not know which forecast method is correct for your data, let
the methods compete fairly. They use the same history, the same cut-off points, the same
horizons and the same naive benchmarks. Keep the winner only if it is clearly better than a
simple method.

**For the statistician.** The plan is an expanding window with `n_folds` origins, one each 4
periods. The workflow scores each horizon. SARIMAX(1,1,1)(0,1,0)₅₂, ARIMA(0,1,1),
damped-trend ETS and Theta compete against naive, seasonal-naive and drift. MASE puts them in
sequence. MASE has no scale, so you can compare series of different sizes. Two
Diebold-Mariano tests (absolute loss, horizon 1) compare the leaders with the benchmark.
`select.best` needs a 5% MASE improvement over each benchmark. Only then does the branch fit
the winner again.

```yaml
--8<-- "cookbook_forecast_bakeoff.yaml"
```

```bash
mapi workflows apply forecast-bakeoff.yaml
mapi workflows run forecast-bakeoff -i data=dataset://growth/weekly_sales@latest \
  -p 'horizons=[1,4]' -p n_folds=6 --watch
mapi workflows node "$RUN" compare      # the league table
mapi workflows node "$RUN" best         # who won, and whether they beat naive by 5%
```

On a short series with much noise, the usual result is the honest result: *"ets is the best
model (0.0456) but does not beat naive.drift by the required 5%: no model is selected."* The
workflow skips the `refit` node, and the report tells why. Thus nobody ships a model that only
looks clever.

## Trait regression across species

**In plain words.** Close relatives are similar because they share most of their history. If
you use each species as an independent data point, an effect can look clear only because
related species are near each other. This recipe fits the same regression four times. The
first fit ignores the tree. The other three fits let relatives share part of their residual.

The workflow keeps the fit that predicts best. Then it tells you if the effect that you ask
about is clear of zero in that fit. It reports a clear effect only with a check that the fit
agrees with the data.

**For the statistician.** `sci.phylo_regression` regresses the log of the trait on the log of
each predictor (standardised). The four fits differ only in the residual `kind`:

- `iid`: no tree, `K = σ²I`.
- `brownian`: Brownian motion on the tree, `K = σ²R`. `R` is the shared branch length of two
  tips divided by the depth of the tree.
- `lambda`: Pagel's lambda, `K = σ²(λR + (1 − λ)I)`.
- `ou`: Ornstein-Uhlenbeck on the patristic distance.

Each fit stores the leave-one-out density of each species given the other species. Thus
`elpd_loo` compares the four kinds correctly. `compare.models` ranks them by `elpd_loo`,
`compare.stacking` gives the stacking weights and `select.best` keeps the highest `elpd_loo`.
`model.summary` with `focus` reads the slope of one predictor of the winner. The branch tests
if the HDI of that slope excludes zero. Only then does `evaluate.ppc` compare the predictions
of the winner with the observed traits.

```mermaid
flowchart LR
  D["traits and<br/>the tree"] --> F["four fits: iid, brownian,<br/>lambda, ou (elpd_loo)"]
  F --> C["compare +<br/>stacking weights"]
  C --> S["select the best<br/>elpd_loo"]
  S --> M["slopes of the winner<br/>(focus: group_size)"]
  M --> B{"range of the slope<br/>excludes zero?"}
  B -- then --> P["predictive check"]
  S --> T["trait on the tree<br/>(sci.phylogeny)"]
  B --> R["report"]
  P --> R
  T --> R
```

### The document

```yaml
--8<-- "cookbook_sci_phylo_signal.yaml"
```

The recipe reads one dataset. The traits are on a `species` dim. The tree is on a `node` dim:
`parent`, `branch_length`, `is_tip` and `tip_label`. The tip labels are the labels of
`species`. The workflow binds the target of the dataset (for example `brain`) to the `trait`
of the model. The `predictors` are names of variables in your dataset. The `focus` is one of
them.

To try the recipe, export the E23 case study and upload it as `primates301` in the project
`biology`:

```bash
python -m mapi_plugin_science.export e23 --challenges ~/pymc-challenges/data --out primates301.nc
# or the simulated data of the model (you do not need pymc-challenges)
python -m mapi_plugin_science.export e23 --synthetic --out primates301.nc
mapi datasets upload primates301.nc --name primates301
```

### Run it with the CLI

```bash
mapi workflows init sci_phylo_signal --name primate-brains   # writes primate-brains.yaml
mapi workflows apply primate-brains.yaml
mapi workflows run primate-brains -i data=dataset://biology/primates301@latest \
  -p 'predictors=["body","group_size"]' -p focus=group_size --watch
RUN=$(mapi --json workflows runs primate-brains -n 1 | jq -r '.[0].run_id')
mapi workflows node "$RUN" compare   # the four fits, the best elpd_loo first
mapi workflows node "$RUN" slope     # the slopes of the winner and the focus value
```

### Run it with the Python client

```python
from pathlib import Path

from mapi_client import Client

api = Client()  # MAPI_URL, the `mapi login` sign-in (or MAPI_TOKEN) and MAPI_PROJECT
workflow, revision = api.workflows.upsert(Path("primate-brains.yaml"))
run = api.workflows.run(
    workflow.id,
    inputs={"data": "dataset://biology/primates301@latest"},
    params={"predictors": ["body", "group_size"], "focus": "group_size"},
    wait=True,
)
nodes = {n.node_id: n for n in run.nodes}
print(nodes["compare"].result["ranking"])  # ['lambda', 'ou', 'brownian', 'iid']
print(nodes["weights"].result["weights"])  # the stacking weight of each fit
focus = nodes["slope"].result["focus"]
print(focus["mean"], focus["hdi_low"], focus["hdi_high"], focus["p_positive"])
print(nodes["clear"].result["arm"])  # "then": the range of the slope excludes zero
```

### Read the outputs

A local run (`LocalRunner`) of the recipe with its default settings (4 chains of 1000 draws)
on the 301 primates of E23 took 3.3 minutes on a laptop. It gave these results:

| Node | Result |
|---|---|
| `iid`, `brownian`, `lambda`, `ou` | `elpd_loo` 15.0, 90.5, 129.9 and 91.2. The notebook gives 130 for lambda. |
| `weights` | lambda 1.00. The other fits get no weight. |
| `best` | "Pagel's lambda has the best elpd_loo (130) of 4 candidate(s)." |
| `slope` | `slope[body]` 0.766 (94% range 0.710 to 0.823), as the notebook. `slope[group_size]` 0.027 (94% range −0.010 to 0.066), `p_positive` 0.91. `max_rhat` 1.001, no divergences. |
| `clear` | The else arm: the range of the slope includes zero. `ppc` is skipped. |

Read the results in this sequence:

1. **The ranking.** A large gap between `iid` and the other fits shows that relatives are
   similar. Here the tree adds 115 to `elpd_loo`.
2. **The weights.** If one fit gets all the weight, the winner is clear. If the weight is
   divided, more than one correlation agrees with the data. Then the slope of the winner is
   only one of some possible answers.
3. **The decision.** The `focus` value gives the slope, its range and `p_positive`. In E23 the
   effect of group size is positive in 91% of the draws, but the 94% range includes zero.
   Thus the data do not show a clear effect of group size on brain size when the model
   includes the tree.
4. **The plot.** The `tree` output (`sci.phylogeny`) shows the tree with the observed and the
   predicted trait of each species. Look for clades where the predictions are far from the
   observations.

The `report` output has the decision, the three tables, the plot and the predictive check
(when it ran).

## Calibrate, then design

**In plain words.** A heated plate conducts heat in its plane and loses heat through its faces
and its edges. Some calibration runs with a known heater and some thermocouples tell how much
heat the plate conducts and loses. The calibrated model can then predict the temperature of
each point of the plate for a heater layout that you did not build.

Before you trust such a prediction, test the calibration on a run that it did not see. This
recipe holds out one calibration run, fits the other runs and predicts the run held out. It
predicts the new layout only if that error is small and the sampler converged.

**For the statistician.** `sci.heat_plate` solves the 2-D steady heat equation
`A(θ) u = η q` on the finite-volume grid of the data. `θ` is the conductivity `k`, the face
and edge loss coefficients `h` and `h_edge`, and the heater efficiency `η`. `split.holdout`
holds out one random run (`leave_group_out`, `n: 1`). `model.fit` with
`mode: evaluate_then_refit` fits the other runs and scores the run held out (`rmse`, `mae`,
`coverage_90`). Then it fits all the runs again. `model.summary` gives the parameters and
`max_rhat`. The branch needs `rmse ≤ max_error` and `max_rhat ≤ max_rhat`. The field is
linear in the heater power, so `model.score` (`predict`) gives the posterior draws of the
expected field of each new run. These draws have no sensor noise. `plot.render` draws them
with `sci.field_map`, one frame for each run.

```mermaid
flowchart LR
  D["calibration runs"] --> H["hold out<br/>one run"]
  H --> F["fit, score the run held out,<br/>fit all runs again"]
  F --> S["plate parameters<br/>and R-hat"]
  F --> RES["calibration residuals<br/>(sci.image_residuals)"]
  S --> B{"error ≤ max_error<br/>and R-hat ≤ max_rhat?"}
  F --> B
  L["new layout"] --> P
  B -- then --> P["predict the field<br/>of the layout"]
  P --> M["field map<br/>(sci.field_map)"]
  B --> R["report"]
  RES --> R
  M --> R
```

### The document

```yaml
--8<-- "cookbook_sci_calibrate_then_design.yaml"
```

The recipe reads two datasets:

- `data`: the calibration runs. `heater_power(run, y, x)` in W and `temperature(run, y, x)`
  in degC on a regular grid in cm. `temperature` has no value where there is no thermocouple.
  The `run` dim needs the policy `new_levels`, because the fit does not see the run held out.
  Use three runs or more.
- `layout` (optional): the new heater layouts. `heater_power(run, y, x)` on the same grid,
  with new run labels. If you do not give a layout, the workflow predicts the calibration
  runs.

If the names in your datasets are different, give `bind`. The split and the map read the
names of the `run`, `y` and `x` dims from `bind`. To try the recipe, export the E51 case
study and make a layout with two 5 W heaters:

```bash
python -m mapi_plugin_science.export e51 --out heat_plate_e51.nc
mapi datasets upload heat_plate_e51.nc --name heat_plate_e51
```

```python
import xarray as xr

cal = xr.load_dataset("heat_plate_e51.nc")  # heater_w(run, y_cm, x_cm), 3 runs, 24 x 12 grid
power = xr.zeros_like(cal["heater_w"].isel(run=[0])).assign_coords(run=["two_heaters"])
power[0, 3, 5] = power[0, 8, 18] = 5.0  # two 5 W heaters (row, column)
xr.Dataset({"heater_w": power}).to_netcdf("heat_plate_layout.nc")
```

Then upload `heat_plate_layout.nc` with `mapi datasets upload heat_plate_layout.nc --name
heat_plate_layout`.

### Run it with the CLI

```bash
mapi workflows init sci_calibrate_then_design --name heat-plate-design
mapi workflows apply heat-plate-design.yaml
mapi workflows run heat-plate-design \
  -i data=dataset://engineering/heat_plate_e51@latest \
  -i layout=dataset://engineering/heat_plate_layout@latest \
  -p 'bind={"dims": {"y": "y_cm", "x": "x_cm"}, "vars": {"heater_power": "heater_w", "temperature": "reading_c"}}' \
  --watch
RUN=$(mapi --json workflows runs heat-plate-design -n 1 | jq -r '.[0].run_id')
mapi workflows node "$RUN" fit     # the error on the run held out
mapi workflows node "$RUN" plate   # k, h, h_edge, eta, sigma and the spread length
```

### Run it with the Python client

```python
from pathlib import Path

from mapi_client import Client

api = Client()
workflow, revision = api.workflows.upsert(Path("heat-plate-design.yaml"))
run = api.workflows.run(
    workflow.id,
    inputs={
        "data": "dataset://engineering/heat_plate_e51@latest",
        "layout": "dataset://engineering/heat_plate_layout@latest",
    },
    params={
        "bind": {
            "dims": {"y": "y_cm", "x": "x_cm"},
            "vars": {"heater_power": "heater_w", "temperature": "reading_c"},
        }
    },
    wait=True,
)
nodes = {n.node_id: n for n in run.nodes}
print(nodes["fit"].result["metrics"])  # rmse, mae and coverage_90 on the run held out
print(nodes["plate"].result["params"]["k"])  # the conductivity, with its range and R-hat
print(nodes["good"].result["arm"])  # "then": the workflow predicted the layout
```

### Read the outputs

A local run (`LocalRunner`) of the recipe with its default settings (4 chains of 1000 draws,
the `pymc` engine) on the E51 calibration and the layout above took 14 minutes on a busy
laptop. It gave these results:

| Node | Result |
|---|---|
| `holdout` | The fit did not see the run `cal1`. |
| `fit` | On `cal1`: `rmse` 0.12 degC, `mae` 0.09 degC, `coverage_90` 1.0. The thermocouples have a noise of 0.2 degC. |
| `plate` | `k` 59.2 W/m/K (94% range 53.7 to 63.9), `h` 9.05, `eta` 0.91, `sigma` 0.19 degC and `spread_length` 12.8 cm. The notebook gives 59.3, 9.07, 0.91, 0.19 and 12.9 cm. `max_rhat` 1.002, 8 divergences. |
| `good` | The then arm: the error and R-hat are in their limits. |
| `design`, `field` | The expected field of `two_heaters`. The hottest point is 33.3 degC, at x = 30.8 cm and y = 14.2 cm. |

Read the results in this sequence:

1. **The error on the run held out.** This is the error of a prediction for a heater layout
   that the fit did not see. That is the same task as the design. An `rmse` near the noise
   of the thermocouples shows that the calibration predicts new layouts well.
2. **The parameters.** `k`, `h` and `eta` are strongly correlated, because a stronger heater
   and a stronger loss can give the same readings. The range of `spread_length` is narrower
   (1% of its value, against 5% for `k`). The data fix how far the heat spreads, and that
   controls the field.
3. **Convergence.** The branch reads only `max_rhat`. `n_divergent` in the `plate` result
   shows the divergent transitions (8 here, 2 in the notebook). If there are many, set a
   higher `target_accept` in the `fit` node.
4. **The maps.** The `residuals` plot shows the calibration readings against the fit. The
   `field` plot shows the mean and the sd of the predicted field, one frame for each new
   run. A large sd far from the thermocouples shows where more sensors help.

If the branch takes the else arm, the workflow skips `design` and `field`. The report tells
you to add calibration runs or thermocouples.

## Make your own

1. Start from the recipe that is nearest to your analysis. Use
   `mapi workflows init <template>`, or copy a document above.
2. Find nodes with `mapi workflows nodes-catalog --kind test`. To see the About card, ports
   and parameters of one node, use `--about test.kpss`.
3. Validate the document after each change. Before anything runs,
   `mapi workflows validate my.yaml` shows each problem with a JSON pointer.
4. Save the workflow (`apply`).
5. Run the workflow with `--watch`.
6. Read the summary.
7. Add a schedule or a dataset trigger to run the workflow automatically.
8. Add a governance gate to the workflow.
