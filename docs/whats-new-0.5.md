---
description: v0.5 turns single models into trustworthy, automated analyses - workflows that test, decide, fit and check, governance with null tests and second-person approval, Data and Model Explorers, mmm-framework and statsmodels, and About cards with Help me choose.
---

# What's new in 0.5 <span class="mapi-badge">v0.5</span>

**v0.4 made one model easy to fit, read and share. v0.5 makes the full analysis around the
model repeatable and trustworthy.** A weekly forecast is not "fit an ARIMA and hope" now. It
is a versioned workflow. The workflow does these steps:

1. It tests the series for unit roots.
2. It decides the differencing.
3. It searches the AR and MA orders.
4. It backtests the model on rolling origins against naive forecasts.
5. It keeps the model only if the model wins.
6. It checks the model against shuffled data and fake drivers.
7. It records the evidence.
8. It waits for the sign-off of a colleague.

The workflow also runs again each Monday morning automatically.

<div class="grid cards" markdown>

-   :material-graph-outline:{ .lg .middle } **Workflows**

    ---

    A workflow is a versioned YAML graph. It can contain statistical tests, transforms,
    branches, train/test plans, order searches, model fits, evaluations, governance checks
    and reports. You get live node status, a cache for unchanged steps, schedules and
    dataset triggers.

    [:octicons-arrow-right-24: Workflow cookbook](guides/workflow-cookbook.md)

-   :material-shield-check-outline:{ .lg .middle } **Governance and null tests**

    ---

    Policies tell which checks a model must pass and who must approve. For example, the
    model must be better than the same model fitted on shuffled results. It must give no
    credit to fake noise drivers, and it must be better than naive forecasts. Promotion
    refuses models that are not validated. Evidence packs are available for auditors.

    [:octicons-arrow-right-24: Can you trust this model?](guides/null-tests.md)

-   :material-magnify-scan:{ .lg .middle } **Data and Model Explorers**

    ---

    The Data Explorer has thirteen sections, for example profiles, stationarity,
    cross-correlation with the KPI, STL, VIF and quality warnings. The Model Explorer has
    eleven sections, for example parameters, residual tests, IRF/FEVD, forecasts, backtests
    and side-by-side comparisons with Diebold-Mariano tests.

    [:octicons-arrow-right-24: Explore a dataset](guides/data-explorer.md) ·
    [Explore a model](guides/model-explorer.md)

-   :material-chart-bell-curve-cumulative:{ .lg .middle } **mmm-framework marketing-mix models**

    ---

    `mmm.bayesian` is national or pooled over geos and products. `mmm.frequentist` is a
    ridge model with bootstrap ranges. You also get business priors, lift tests, response
    curves and budget optimisation with risk objectives and an efficient frontier.

    [:octicons-arrow-right-24: Marketing-mix models](models/mmm-framework.md)

-   :material-chart-timeline-variant:{ .lg .middle } **Twelve statsmodels time-series models**

    ---

    The models are SARIMAX, ARIMA, ETS, Theta, unobserved components, ARDL, error
    correction, VAR, VECM, VARMAX, dynamic factors and Markov switching. They support future
    drivers, prediction intervals, impulse responses, Granger and cointegration tests, and
    panels for each series.

    [:octicons-arrow-right-24: statsmodels models](models/statsmodels.md)

-   :material-help-circle-outline:{ .lg .middle } **About cards and Help me choose**

    ---

    Each model, step, node, check and template has an About card in plain words. The card
    tells what the item does, when to use it and when not to use it. It also tells what the
    item needs and what it gives. Help me choose changes eight answers into a ranked
    shortlist with the reasons.

    [:octicons-arrow-right-24: Choosing a model](guides/choosing-a-model.md)

</div>

## For analysts: answers you can defend

- **Help me choose.** Possibly you do not know if you need an ARIMAX, an exponential
  smoothing model or a marketing-mix model. Answer a maximum of eight questions, for example
  "What do you want to do?", "What does your data look like?" and "Do you need a range?".
  You get the best fits. Each fit has the reasons, the cautions and its About card. You can
  use Help me choose in the web app, in the terminal (`mapi choose`) or in Python.
- **Templates that think like a statistician.** Start a workflow from the gallery, for
  example *Is my weekly KPI ready to forecast?*, *Do these series move together?*, *A
  governed marketing-mix model* or *Forecast bake-off*. Read the result as a report. Each
  test has a plain verdict ("The series wanders: consider differencing it."). Each decision
  shows the numbers that it read. Each comparison says "is possibly luck" or "It is not luck".
- **A model must earn its place.** Before a person uses a model, the platform shuffles the
  results that the model was fitted on. Then it fits the model again, many times. A model
  that learned something real must be clearly better on the real data. The platform also
  adds drivers that are pure noise. These drivers must get almost no credit. The
  [null tests explained](guides/null-tests.md) page shows why this is important, without
  formulas.
- **Explore before you model.** The Data Explorer shows gaps, constant columns, outliers,
  collinear drivers and series that wander. It also shows which drivers move with the KPI,
  and at which lag.

## For data scientists: the statistics, end to end

- **A DAG with typed ports.** The port types are `dataset`, `test_result`, `split_plan`,
  `model_version`, `metrics`, `table`, `evidence` and `report`. The server validates the DAG
  statically with JSON pointers before a run starts. Branches use a safe expression language
  (never `eval`) and record the values that they read. Alternatives
  (`[diff.data, inputs.data]`) take the first input that exists.
- **Seventeen statistical tests.** We checked these tests against statsmodels/scipy/arch
  reference values. The tests are:
    - ADF, KPSS, Phillips-Perron, Zivot-Andrews, Johansen and Engle-Granger
    - Ljung-Box, Jarque-Bera, Breusch-Pagan, White and ARCH-LM
    - Granger, ACF/PACF, seasonal strength, VIF, CUSUM and Chow.
- **Split plans and searches.** There are seven split plans: holdout, rolling origin,
  expanding window, blocked k-fold, purged k-fold, geo holdout and nested. There are
  ARMA/SARIMA/VAR-lag/cointegration-rank searches and grid searches on backtests. There is
  also Diebold-Mariano with the Harvey-Leybourne-Newbold correction.
- **Honest backtests.** Each candidate and each naive benchmark use the same folds. A gap
  allows for late data. The backtest scores each horizon separately.
- **Governance with real statistics.** Each check gives the value, the threshold and the
  evidence. The checks change for Bayesian, frequentist and machine-learning models. The
  checks are:
    - block-permutation tests of the target
    - placebo noise regressors, measured by ablation, and time-shifted placebos
    - out-of-sample skill against naive and seasonal-naive forecasts
    - Ljung-Box whiteness and rolling-window stability
    - prior sensitivity, parametric-bootstrap recovery, R-hat and holdout accuracy.
- **All of it from Python and the terminal.** The Python client has `api.workflows`,
  `api.governance`, `api.validations`, `api.explorations`, `api.models.optimize` and
  `api.catalog.recommend`. The CLI has `mapi workflows run --watch`, `mapi validate model`,
  `mapi approve`, `mapi explore`, `mapi optimize` and `mapi choose`.

## The canonical example, three ways

The [cookbook](guides/workflow-cookbook.md) shows each node of the example. This is a short
version:

=== "Python"

    ```python
    --8<-- "client_v05.py:run"
    ```

=== "Command line"

    ```bash
    --8<-- "cli_v05.sh:workflow"
    ```

=== "HTTP"

    ```bash
    api POST /v1/workflows -d "$(jq -Rs '{text: .}' weekly-readiness.yaml)"
    api POST "/v1/workflows/$WF/runs" -H "Idempotency-Key: $(uuidgen)" -d '{
      "inputs": {"data": "dataset://growth/weekly_sales@latest"},
      "params": {"drivers": ["price"]}}'
    curl -N -H "Accept: text/event-stream" "$API/v1/workflow-runs/$RUN/events"
    ```

The Python walkthrough printed this output on a live local stack. The data was three years of
a weekly KPI that wanders, with a price driver. We made the event stream shorter. The run
took about two minutes on a laptop. Most of the time was for sandboxed fits.

```text
23 nodes, queued
  adf: queued
  kpss: queued
  …
  adf: succeeded
  kpss: succeeded
  …
  wanders: took the then arm
  seasonal: took the else arm
  sdiff: skipped
  …
  governance: succeeded
  trusted: took the then arm
run succeeded
The series wanders: consider differencing it.
wanders then {'nodes.pp.reject': False, 'nodes.za.reject': False, 'nodes.adf.reject': False, 'nodes.kpss.reject': True}
seasonal else {'nodes.seas.seasonal': False}
trusted then {'nodes.governance.passed': True}
ARIMA(1, 1, 1)
The difference between backtest and naive at horizon 1 is possibly luck.
backtest has the best average error (1.83) and beats every benchmark (the strongest, naive, scored 1.87).
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

This output shows that the platform is honest. It does not flatter the model:

- All four unit-root tests agree that the series wanders. Thus the workflow differences the
  series one time.
- On weeks that it did not see, ARIMAX is 25% better than "same as last week".
- ARIMAX forecasts better than all twenty fits with the price driver shuffled in time. Thus
  the driver has real information.
- But Diebold-Mariano says that the one-week-ahead advantage over naive *is possibly luck*.
- The AR coefficient changes across windows of history.

The default forecasting policy uses stability as information only. Thus the version is
`validated`, and the failure stays on the record for the reviewer. A stricter policy makes
the failure blocking. Each validation makes its own random shuffles. Thus the p-value of the
shuffled-driver test changes a little between validations.

The tests of the documentation run this walkthrough, without changes, against a real server.

## Upgrading from 0.4

- **mmm-framework is the only marketing-mix engine.** We removed the earlier marketing-mix
  plugins. Specs move to `mmm.bayesian@^1`. They do not have their own scaling and lag steps
  now, because the model controls scaling, carry-over and units. For budget optimisation, use
  `POST …:optimize` (`api.models.optimize`, `mapi optimize`). See
  [Marketing-mix models](models/mmm-framework.md) and the *Removed* section of the CHANGELOG.
- **Contract version 0.2.** This version adds:
    - resources: `workflow`, `policy` and `tenant_policy`
    - actions: `validate` and `approve`
    - scopes: `workflows:write`, `workflows:run`, `models:validate` and `policies:manage`
    - the `reviewer` capability on memberships.

    An API token cannot approve.
- **Migrations `0011_workflows` and `0012_governance`.** These migrations add workflow and
  governance tables under forced row-level security.
- **Promotion can now need validation.** If a policy has `require_validated_for_promotion`,
  endpoints refuse unvalidated versions with `409 NOT_VALIDATED`. The default policies that
  the platform seeds on first use do *not* gate promotion. You must turn on this gate
  yourself.
- **Client and CLI.** All of the items above are new API surface. No existing signature
  changed. `mapi_client` exports `WorkflowSnapshot`, `WorkflowEventFrame` and `WorkflowEnd`
  for the workflow event stream.

## What is not in 0.5 yet

- `mmm.nested` and `mmm.multivariate` (the extensions of mmm-framework) have About cards but
  no plugin. The multiplicative specification, time-varying effects and price/promotion
  levers are not available.
- statsmodels models do not have the counterfactual, simulate or filter score modes. A
  what-if is a forecast with changed future drivers.
- `evaluate.residuals` and `model.forecast` nodes make tables, not plots.
- We did not run 0.5 on real Google Cloud or in Docker. See the release notes.
