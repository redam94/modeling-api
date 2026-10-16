---
description: v0.5 turns single models into trustworthy, automated analyses - workflows that test, decide, fit and check, governance with null tests and second-person approval, Data and Model Explorers, mmm-framework and statsmodels, and About cards with Help me choose.
---

# What's new in 0.5 <span class="mapi-badge">v0.5</span>

**v0.4 made one model easy to fit, read and share. v0.5 makes the whole analysis around it
repeatable and trustworthy.** A weekly forecast is no longer "fit an ARIMA and hope": it is
a versioned workflow that tests the series for unit roots, decides the differencing, searches
the AR and MA orders, backtests the model on rolling origins against naive forecasts, keeps it
only if it wins, checks it against shuffled data and fake drivers, records the evidence and
waits for a colleague's sign-off - and it runs again every Monday morning by itself.

<div class="grid cards" markdown>

-   :material-graph-outline:{ .lg .middle } **Workflows**

    ---

    Versioned YAML graphs of statistical tests, transforms, branches, train/test plans,
    order searches, model fits, evaluations, governance checks and reports. Live node status,
    caching of unchanged steps, schedules and dataset triggers.

    [:octicons-arrow-right-24: Workflow cookbook](guides/workflow-cookbook.md)

-   :material-shield-check-outline:{ .lg .middle } **Governance and null tests**

    ---

    Policies say which checks a model must pass - it must beat itself fitted on shuffled
    results, give fake noise drivers no credit, beat naive forecasts - and who must approve.
    Promotion refuses models that are not validated. Evidence packs for auditors.

    [:octicons-arrow-right-24: Can you trust this model?](guides/null-tests.md)

-   :material-magnify-scan:{ .lg .middle } **Data and Model Explorers**

    ---

    Thirteen data sections (profiles, stationarity, cross-correlation with the KPI, STL,
    VIF, quality warnings) and eleven model sections (parameters, residual tests, IRF/FEVD,
    forecasts, backtests, side-by-side comparisons with Diebold-Mariano tests).

    [:octicons-arrow-right-24: Explore a dataset](guides/data-explorer.md) ·
    [Explore a model](guides/model-explorer.md)

-   :material-chart-bell-curve-cumulative:{ .lg .middle } **mmm-framework marketing-mix models**

    ---

    `mmm.bayesian` (national or pooled over geos and products) and `mmm.frequentist`
    (ridge with bootstrap ranges), business priors, lift tests, response curves and budget
    optimisation with risk objectives and an efficient frontier.

    [:octicons-arrow-right-24: Marketing-mix models](models/mmm-framework.md)

-   :material-chart-timeline-variant:{ .lg .middle } **Twelve statsmodels time-series models**

    ---

    SARIMAX, ARIMA, ETS, Theta, unobserved components, ARDL, error correction, VAR, VECM,
    VARMAX, dynamic factors and Markov switching - with future drivers, prediction
    intervals, impulse responses, Granger and cointegration tests, panels per series.

    [:octicons-arrow-right-24: statsmodels models](models/statsmodels.md)

-   :material-help-circle-outline:{ .lg .middle } **About cards and Help me choose**

    ---

    Every model, step, node, check and template explains itself in plain words: what it does,
    when to use it and when not, what it needs and gives. Eight questions turn into a ranked
    shortlist with the reasons.

    [:octicons-arrow-right-24: Choosing a model](guides/choosing-a-model.md)

</div>

## For analysts: answers you can defend

- **Help me choose.** Not sure whether you need an ARIMAX, an exponential smoothing model or
  a marketing-mix model? Answer up to eight questions ("What do you want to do?", "What does
  your data look like?", "Do you need a range?") and get the best fits, each with the reasons
  and the cautions, and its About card. In the web app, in the terminal (`mapi choose`) or
  in Python.
- **Templates that think like a statistician.** Start a workflow from the gallery - *Is my
  weekly KPI ready to forecast?*, *Do these series move together?*, *A governed
  marketing-mix model*, *Forecast bake-off* - and read the result as a report: every test
  with a plain verdict ("The series wanders: consider differencing it."), every decision with
  the numbers it read, every comparison with "may be luck" or "beyond luck".
- **A model must earn its place.** Before anyone uses a model, the platform shuffles the
  results it was fitted on and fits it again, many times: a model that learned something real
  must do clearly better on the real data. It adds drivers made of pure noise: they must get
  next to no credit. The [null tests explained](guides/null-tests.md) shows why that
  matters, without formulas.
- **Explore before you model.** The Data Explorer flags gaps, constant columns, outliers,
  collinear drivers and series that wander, and shows which drivers move with the KPI and at
  what lag.

## For data scientists: the statistics, end to end

- **A DAG with typed ports** - `dataset`, `test_result`, `split_plan`, `model_version`,
  `metrics`, `table`, `evidence`, `report` - validated statically with JSON pointers before
  anything runs. Branches use a safe expression language (never `eval`) and record the values
  they read; alternatives (`[diff.data, inputs.data]`) take the first input that exists.
- **Seventeen statistical tests** checked against statsmodels/scipy/arch reference values
  (ADF, KPSS, Phillips-Perron, Zivot-Andrews, Johansen, Engle-Granger, Ljung-Box,
  Jarque-Bera, Breusch-Pagan, White, ARCH-LM, Granger, ACF/PACF, seasonal strength, VIF,
  CUSUM, Chow), seven split plans (holdout, rolling origin, expanding window, blocked and
  purged k-fold, geo holdout, nested), ARMA/SARIMA/VAR-lag/cointegration-rank searches, grid
  searches on backtests and Diebold-Mariano with the Harvey-Leybourne-Newbold correction.
- **Honest backtests**: every candidate and every naive benchmark on the same folds, with a
  gap for late data and several horizons scored separately.
- **Governance with real statistics**: block-permutation tests of the target, placebo noise
  regressors measured by ablation, time-shifted placebos, out-of-sample skill against naive
  and seasonal-naive, Ljung-Box whiteness, rolling-window stability, prior sensitivity,
  parametric-bootstrap recovery, R-hat, holdout accuracy - each with the value, the threshold
  and the evidence, adapted to Bayesian, frequentist and machine-learning models.
- **All of it from Python and the terminal**: `api.workflows`, `api.governance`,
  `api.validations`, `api.explorations`, `api.models.optimize`, `api.catalog.recommend`;
  `mapi workflows run --watch`, `mapi validate model`, `mapi approve`, `mapi explore`,
  `mapi optimize`, `mapi choose`.

## The canonical example, three ways

The [cookbook](guides/workflow-cookbook.md) walks through it node by node. In short:

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

What the Python walkthrough printed on a live local stack, on three years of a wandering
weekly KPI with a price driver (the event stream shortened; the run took about two minutes on
a laptop, most of it sandboxed fits):

```text
23 nodes, queued
  adf: started
  kpss: started
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
The difference between backtest and naive at horizon 1 may be luck.
backtest has the best average error (1.83) and beats every benchmark (the strongest, naive, scored 1.87).
passed
validated
data.quality                 pass    No data problems found (156 periods of history).
convergence                  pass    The fit reported no convergence problems.
baseline.beats_naive         pass    On the 13 held-out periods the model's error is 25% lower than 'same as last period' (the strongest simple benchmark).
holdout.accuracy             pass    On data it was not fitted on, the model's average percentage error is 1.8% (limit 15%).
residuals.whiteness          pass    No pattern is left in the errors (Ljung-Box p = 0.39 at 10 lags).
null.permutation_target      fail    2 of 20 fits on shuffled results did as well as the real model (p = 0.143): it may be fitting noise rather than signal.
stability.rolling_params     fail    ar.L1 changes a lot across windows of history (variation 1205%): the relationship may not be stable over time.
```

This is the platform being honest rather than flattering. All four unit-root tests agree the
series wanders, so it is differenced once; ARIMAX beats "same as last week" by 25% on weeks
it never saw, yet Diebold-Mariano says the one-week-ahead edge over naive *may be luck*, two
shuffled fits in twenty did as well, and the AR coefficient moves across windows of history.
The default forecasting policy treats the shuffled-result test as a warning and stability as
information, so the version is `validated` - with every warning on the record for the
reviewer. A stricter policy makes either one blocking. (Every validation draws its own
random shuffles, so the number of shuffled fits that tie the real one varies between
validations.)

The documentation's tests run this walkthrough, unchanged, against a real server.

## Upgrading from 0.4

- **mmm-framework is the only marketing-mix engine.** The earlier marketing-mix plugins were
  removed; specs move to `mmm.bayesian@^1` and drop their own scaling and lag steps (the model owns scaling, carry-over and units). Budget optimisation is
  `POST …:optimize` (`api.models.optimize`, `mapi optimize`). See
  [Marketing-mix models](models/mmm-framework.md) and the CHANGELOG's *Removed* section.
- **Contract version 0.2.** New resources (`workflow`, `policy`, `tenant_policy`), actions
  (`validate`, `approve`), scopes (`workflows:write`, `workflows:run`, `models:validate`,
  `policies:manage`) and the `reviewer` capability on memberships. No API token can approve.
- **Migrations `0011_workflows` and `0012_governance`** add workflow and governance tables
  under forced row-level security.
- **Promotion can now require validation.** A policy with
  `require_validated_for_promotion` makes endpoints refuse unvalidated versions with
  `409 NOT_VALIDATED`. The default policies seeded on first use do *not* gate promotion;
  turn it on deliberately.
- **Client and CLI**: everything above is new API surface; nothing existing changed
  signature. `mapi_client` exports `WorkflowSnapshot`, `WorkflowEventFrame` and
  `WorkflowEnd` for the workflow event stream.

## What is not in 0.5 yet

- `mmm.nested` and `mmm.multivariate` (mmm-framework's extensions) have About cards but no
  plugin; the multiplicative specification, time-varying effects and price/promotion levers
  are not exposed.
- statsmodels models do not offer counterfactual, simulate or filter score modes; what-ifs
  are forecasts with changed future drivers.
- `evaluate.residuals` and `model.forecast` nodes produce tables, not plots.
- Nothing in 0.5 was run on real Google Cloud or in Docker; see the release notes.
