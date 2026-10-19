---
description: Complete, tested workflow recipes - unit roots, differencing, AR/MA order search, a rolling-origin backtest against naive, selection and governance - with the Python client and the CLI commands to run them, plus VAR/VECM cointegration, a forecast bake-off, a Bayesian workflow audit and an image model check.
---

# Workflow cookbook <span class="mapi-badge">v0.5</span>

This page gives complete analyses. Each analysis is one YAML document. You can save,
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
| [Can I trust this Bayesian model?](#bayesian-workflow-audit) | Do the priors make sense? Do the chains agree? Does the model make data like my data? Do the priors set the results? Does the model find known values? |
| [Where does my image model go wrong?](#image-model-check) | Do the predicted images look like the observed image? Where on the image are the residuals large? |

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

## Bayesian workflow audit

**In plain words.** A Bayesian model gives ranges, not only numbers. Before you use the
ranges, do the checks of a careful Bayesian workflow:

- Can the model make data like your data before it sees the data (the priors)?
- Do the chains of the sampler agree (R-hat and the effective sample size)?
- Can the fitted model make data like your data (the posterior predictive check)?
- Do the results change much when the priors are wider or narrower?
- Does the model find known values in data that it simulated?

This workflow does all five checks on one model and writes one report with the plots. It
works with each model that gives posterior draws. You give the model, its settings and the
binding as parameters, the same as in a model spec.

**For the statistician.** The fit keeps `prior_predictive.draws` prior predictive draws
(500 by default) for `core.prior_predictive`. `model.summary` gives the rank-normalised
split R-hat and the bulk ESS of all the posterior values. The branch `converged` needs R-hat
under `rhat_max` (1.01) and the ESS at or over `min_ess` (400, that is 100 for each of 4
chains). Only then do the other checks run:

- `evaluate.ppc`: the coverage of the 50% and 90% ranges, the PIT distance and the p-value
  of `statistic` (sd by default).
- `governance.check` with `sensitivity.prior`: refits with the prior widths times each of
  `prior_scales`.
- `governance.check` with `recovery.simulated`: `n_sims` refits on targets that the fit
  simulated.

The branch `trusted` needs `ppc.ok` and no `fail` or `error` from the two refit checks. A
check that does not apply to the model (`skipped`) does not block. The plots use
`on_failure: continue`. If a recipe cannot read the model, the report tells why, and the
audit continues. The defaults are for real use: 4 chains of 1000 draws after 1000 tuning
steps. The recovery check fits the model `n_sims` more times, at the refit budget of the
governance run.

### The document

```yaml
--8<-- "cookbook_bayes_workflow_audit.yaml"
```

!!! note
    A model that cannot sample its priors (for example `sci.areal_bym2`, because of its
    ICAR prior) fails the fit when the run asks for prior draws. For such a model, set
    `prior_predictive` to `{}`. The branch `has_prior` then skips the prior plot.

### Run it with the CLI

```bash
mapi workflows init bayes_workflow_audit --name radon-audit   # writes radon-audit.yaml
mapi workflows apply radon-audit.yaml -m "audit of the radon model"
mapi workflows run radon-audit \
  --input data=dataset://environment/radon_homes@latest \
  --param model=tabular.hierarchical_glm@^1 \
  --param 'settings={"group_dim": "county", "covariates": ["floor"], "group_covariates": ["log_uranium"]}' \
  --param 'bind={"dims": {"obs": "home"}, "vars": {"y": "log_radon"}}' \
  --param 'vars=["intercept", "beta", "sigma"]' \
  --watch
RUN=$(mapi --json workflows runs radon-audit -n 1 | jq -r '.[0].run_id')
mapi workflows status "$RUN"                  # each decision and what each check found
mapi workflows node "$RUN" summary            # R-hat, ESS and the range of each parameter
mapi workflows node "$RUN" recovery           # the recovery check and its evidence
```

### Run it with the Python client

```python
from mapi_client import Client

api = Client()
workflow = api.workflows.create(template="bayes_workflow_audit")
run = api.workflows.run(
    workflow.id,
    inputs={"data": "dataset://environment/radon_homes@latest"},
    params={
        "model": "tabular.hierarchical_glm@^1",
        "settings": {"group_dim": "county", "covariates": ["floor"],
                     "group_covariates": ["log_uranium"]},
        "bind": {"dims": {"obs": "home"}, "vars": {"y": "log_radon"}},
        "vars": ["intercept", "beta", "sigma"],
    },
    wait=True,
)
nodes = {n.node_id: n for n in run.nodes}
print(nodes["converged"].result["arm"])        # "then": the chains agree
print(nodes["ppc"].result["verdict_plain"])    # the posterior predictive check in words
print(nodes["sensitivity"].result["message_plain"])
print(nodes["recovery"].result["message_plain"])
print(nodes["trusted"].result["arm"])          # "then": no check failed
report = run.outputs["report"].inline          # mapi.report/v1 with the plots in it
```

### Read the outputs

| Output | What it tells you |
|---|---|
| `has_prior`, `converged`, `trusted` | The three decisions, with the values that each condition read. The report starts with their plain words. |
| `summary` (table) | The mean, sd, 94% HDI, R-hat and bulk ESS of each parameter (the first 200 values). The result also has `max_rhat`, `min_ess` and `n_divergent`. |
| `prior_plot` | The data that the priors can make (grey) and your data (black). If your data is far from the grey curves, the priors are on the wrong scale. |
| `pair_plot` | The draws of two parameters at a time. Red points are divergent transitions. Look at funnels and strong correlations. |
| `ppc`, `ppc_plot` | The share of the observations in the 50% and 90% ranges, the PIT distance, and the p-value of the statistic. `ok` is true when all of them pass. |
| `sensitivity` | The largest relative change of a key estimate when the priors change. Above 0.2 is `warn`, above 0.5 is `fail`. `skipped` if the settings do not set the priors. |
| `recovery` | How often the 90% ranges contained the known values in the simulated data (target 90%). |
| `estimates` | The posterior forest of the parameters. It shows only when no check failed. |

If the chains do not agree, the workflow skips the checks and the report tells you to fit with
more draws and tuning steps, or to change the model. A run on a laptop audited the radon model
(8 counties, 64 homes, 2 chains of 100 draws for the test). R-hat was below 1.07 and 91% of
the homes were in the 90% ranges. The sensitivity check gave `warn` (the uranium effect moved
by 41% with priors two times narrower), recovery gave `pass` and the branch `trusted` took the
`then` arm.

## Image model check

**In plain words.** When a model predicts a full image, a table of numbers does not show
where the model is wrong. This workflow fits the model and puts its predicted images next to
the observed image. It shows the residuals as maps, so that you can see the areas where the
model does not agree with the data. Use it for a sky map (`sci.cmb_field`), a lensed galaxy
(`sci.strong_lens`), a Turing pattern (`sci.turing_pattern`) or a different model that
predicts an image.

**For the statistician.** The plots read the in-sample predictions of the model
(`source: predictions`) with the observed image as `observed_data`:

- `sci.image_residuals` shows the observed image, the posterior predictive mean, the residual
  and the normalised residual (obs − mean) / √(sd² + σ²), with a histogram against N(0, 1).
- `sci.ppc_image_grid` shows the observed image next to `n_draws` predicted images on one
  colour scale. With `shuffle: true` the observed image has a random position, and the
  caption gives the answer.
- `evaluate.ppc` pools all the pixels: the coverage of the 50% and 90% ranges, the PIT
  distance and a p-value. It needs 10 or more predictive draws. If it fails, the report tells
  why and the plots stay.

The parameters `var` and `frame` select the image variable and the dim of the slider when the
recipes cannot find them.

### The document

```yaml
--8<-- "cookbook_sci_image_model_check.yaml"
```

### Run it with the CLI

```bash
mapi workflows init sci_image_model_check --name lens-check   # writes lens-check.yaml
mapi workflows apply lens-check.yaml
mapi workflows run lens-check \
  --input data=dataset://astronomy/mock_lens@latest \
  --param model=sci.strong_lens@^1 \
  --param 'settings={"pixel_scale": 0.1, "subsample": 1, "lens_light_components": 1, "external_shear": false}' \
  --param shuffle=true \
  --watch
RUN=$(mapi --json workflows runs lens-check -n 1 | jq -r '.[0].run_id')
mapi workflows node "$RUN" ppc                # coverage, PIT distance and the p-value
mapi workflows output "$RUN" residuals plot --out residuals.json   # the plot as a file
```

### Run it with the Python client

```python
workflow = api.workflows.create(template="sci_image_model_check")
run = api.workflows.run(
    workflow.id,
    inputs={"data": "dataset://science/cmb_synthetic@latest"},
    params={"model": "sci.cmb_field@^1", "var": "sky",
            "settings": {"pixel_arcmin": 7.5, "beam_fwhm_arcmin": 5.0, "border": 4}},
    wait=True,
)
nodes = {n.node_id: n for n in run.nodes}
print(nodes["residuals"].result["title"])      # "Residuals of sky"
print(nodes["ppc"].result["coverage_90"])      # the share of the pixels in the 90% ranges
report = run.outputs["report"].inline          # the table, the scores and the two plots
```

### Read the outputs

| Output | What it tells you |
|---|---|
| `residuals` | Four maps on the image grid. The normalised residuals must look like noise with sd near 1. A pattern (a ring, an edge, a blob) shows structure that the model does not have. |
| `grid` | The observed image and the predicted images. If the model fits, the observed image looks like one of them. |
| `ppc` | The coverage of the 50% and 90% ranges over all the pixels, the PIT distance and `ok`. |
| `summary` | The parameters, R-hat and the bulk ESS. |

A run on a laptop checked the mock lens of `sci.strong_lens` (24 × 24 pixels, 2 chains of 100
draws for the test). 92% of the pixels were in the 90% ranges, the PIT distance was 0.03
(limit 0.06) and `ok` was true.

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
