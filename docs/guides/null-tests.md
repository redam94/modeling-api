---
description: Why a model must beat itself fitted on shuffled results and give fake noise drivers no credit - governance and null tests explained in plain words, then the statistics behind each check.
---

# Can you trust this model? <span class="mapi-badge">v0.5</span>

Any flexible model will find *something* in any data. Give a marketing-mix model ten channels
and two years of weekly sales and it will happily tell you what each channel returned - even if
the sales were random. The fit statistics will look fine. The charts will look convincing. The
only way to know whether the model learned something real is to **show it data where there is
nothing to learn, and check that it notices**.

That is what the platform's null tests do, and why a governance policy can require them before
anyone uses a model. The first half of this page explains them without formulas; the second
half gives the statistics. The how-to (policies, validations, approvals, evidence packs) is in
[Validate models with governance policies](governance.md).

## In plain words

### Shuffled results: does the model beat luck?

Take the drivers the model uses - weekly spend, prices - and shuffle them in time, so that each
week's spend lands on some other week while sales stay exactly as they were. Now the
relationship between spend and sales is destroyed, by construction, but the sales' own trend
and season are untouched. Fit the same model to the shuffled data and measure how well it
predicts weeks it was not fitted on. Do that twenty or forty times. (A model without drivers -
a forecast from the series' own past - is tested on the series itself: its week-to-week
changes are shuffled and added up again, which keeps how much it wanders but removes anything
predictable.)

If the model really learned how sales depend on the drivers, it predicts the real held-out weeks
**much better** than any of the shuffled versions predict theirs. If one shuffled fit in twenty
does as well as the real one, the model's apparent skill is the kind of skill you get from
noise. The check reports this as a *p-value*: the share of shuffled fits that did at least as
well (with a small correction so it is never exactly zero).

| What you see | What it means |
|---|---|
| "lower than all 39 fits on shuffled results (p = 0.025)" | The model's skill is very unlikely to be luck. |
| "1 of 20 fits on shuffled results did as well (p = 0.095)" | Borderline: real signal, but weak, or too few shuffles to tell. |
| "9 of 20 did as well (p = 0.48)" | The model is fitting noise. Do not use it to make decisions. |

Drivers are shuffled **in blocks** (a run of consecutive weeks moves together), so their own
flighting patterns survive; only the link between drivers and result is broken. Because the
sales are not shuffled, a model that wins only because of a trend or a season does exactly as
well on the shuffled copies, and fails the check.

![The evidence a reviewer sees: the real model's error (left, marked) against the errors of 19 fits on shuffled results](../assets/screenshots/governance-evidence.png){ .mapi-shot }

Persistence is the honest limit of this test. On a series that wanders (a random walk), a
model that differences it gets most of its skill from "next week is like this week", and
blocks of shuffled weeks keep much of that, so shuffled fits can forecast nearly as well. The
cookbook's ARIMAX on such a series beats naive by 25% on unseen weeks and still sees 2 of 20
shuffled fits tie it ([the output](workflow-cookbook.md#run-it-with-the-python-client)): the
test is right that most of the skill is persistence. To measure what the *drivers* add on
such series, require the fake-driver and shifted-driver tests below as well.

### Fake drivers: does the model credit anything it is given?

Add three drivers that are pure noise - copies of a real spend column scrambled in time, or
random numbers with the same average and spread - and fit the model again. A trustworthy model
gives them almost **nothing**: a few hundredths of a percent of sales, with a range that
comfortably includes zero.

If a channel made of random numbers gets 8% of sales, the model hands out credit too easily, and
the credit it gave your real channels is suspect too. The usual causes are too many drivers for
the amount of history, priors that are too loose, or drivers that move together.

### Shifted drivers: is it the timing, or just the season?

Move every driver six months away from when it actually happened and refit. Real advertising
effects should **vanish** - spend in March cannot drive sales in September. If the model still
finds most of the effect, it is picking up the season or the trend, and calling it advertising.

### The other checks, briefly

- **Beats naive**: on weeks it never saw, the model must be better than "same as last week" and
  "same as this week last year". A forecast that cannot beat that is not worth its complexity.
- **Nothing left in the errors**: what the model gets wrong should look like noise; a pattern in
  the mistakes means the model missed something predictable.
- **Stable over time**: refitted on different stretches of history, the key numbers should not
  swing wildly.
- **Signs make sense**: spend should not *reduce* sales; prices should not *raise* demand
  (unless you say so).
- **Assumptions do not decide the answer** (Bayesian models): widen and narrow the priors; the
  answer should barely move.
- **Finds known answers**: simulate data from the fitted model, refit, and check that the ranges
  contain the truth about 90% of the time.
- **The fit finished properly**, **the error on unseen data is below your limit**, and **the
  data is good enough** (no long gaps, no constant drivers, no negative spend).

### What the platform does with the results

A **policy** lists the checks a kind of model must pass, how strict each one is
(`blocking` stops validation, `warning` is shown to the reviewer, `info` is evidence only),
and whether a second person must approve. When you validate a model version, the checks run in
the sandbox, every result is stored with its evidence, and the version becomes `validated`,
`awaiting_approval` or `checks_failed`. Endpoints can be told to serve only validated
versions. Everything is in the evidence pack an auditor can read without access to the platform.

```bash
mapi validate model model://growth/geo-mmm@v7     # runs the checks, shows every result
mapi approve "$VALIDATION" --comment "Null tests convincing"   # a second person
mapi validations pack "$VALIDATION" --out evidence.html
```

## For statisticians

Notation: `y` the target over `n` periods, `ŷ` the model's predictions, `H` the held-out periods
of the model's data split, `L` the check's loss (RMSE by default). Every refit uses the model's
own spec and pipeline through the SDK harness (the same code path as a fit), under a refit
budget (`RefitBudget`: at most `max_refits` refits; Bayesian refits are capped at 200 draws and
200 tuning steps on 2 chains).

### `null.permutation_target` - a permutation test with a matching null

The statistic is scale-free: the held-out **skill** of a refit against the null forecast on
the same dataset, `T = 1 − L(model) / L(null forecast)`, where the null forecast is the last
training value of an integrated series (a random walk) and the training mean otherwise. Raw
held-out errors are not comparable across shuffles of the result: their spread comes from
which levels a shuffle lands in the test periods, not from the model (in simulation, a
level-shuffling test with raw RMSE rejected 35% of random walks and 33% of real AR
alternatives - no size and no power).

The null depends on what the model is meant to have learned:

- **Models with drivers** (ARIMAX, marketing mix, regressions on time): `y` is left alone and
  the drivers are block-permuted jointly (`ℓ = max(1, n // 20)` periods per block; the `block`
  parameter changes it). H0: the drivers carry no information about `y` beyond the model's
  trend, season, controls and dynamics. A model whose refit ignores the drivers (the first
  shuffled fit scores exactly the same) is tested as a model without drivers.
- **Models without drivers** (ARIMA, ETS, Theta, UC): the `d`-th differences of `y` are
  permuted i.i.d. and re-integrated from the original first values (`d` from the model's
  ARIMA order, else 1 when ADF keeps a unit root or KPSS rejects stationarity). This keeps
  the integration order and the distribution of the increments but destroys predictable
  dynamics; in simulation (ARIMA(1,1,0), 12 held-out periods, `B = 19`) it keeps its size on
  random walks and rejects AR dynamics far more often.
- **Data without a time order**: the classic row permutation of `y`, on the held-out loss.

With `T_b` the statistic of the `b`-th shuffled refit, `b = 1..B`, the Monte-Carlo p-value is

```text
p = (1 + #{b : T_b >= T0}) / (B + 1)
```

which is exact - valid at every `B` - under exchangeability. The smallest attainable p is
`1 / (B + 1)`: with the default `B = 20` it is 0.048, so a 5% test is barely possible. Use
`B >= 39` for a 2.5% resolution (the policy parameter is `n_permutations`).

Thresholds are on p: pass ≤ 0.05, warn ≤ 0.10, fail above.

### `null.noise_regressors` - placebo regressors measured by ablation

`K` placebo drivers (`n_noise`, default 3) are added - a real driver block-permuted in time
(`kind: shuffled_driver`, which keeps its distribution and short-run autocorrelation) or
Gaussian noise with its mean and variance - as new channels of a marketing-mix model, or new
variables of other models. Placebos get the model's default prior: a real channel's
per-channel prior or calibration (a business-prior or lift-test ROI) is never copied to them,
or a well-calibrated model would fail because of its prior; carry-over and saturation shapes
are copied. After the refit each placebo's **effect share** is measured through the score
path, not read from a coefficient, as a share of the real drivers' total effect:

```text
s_k = Σ_t |ŷ_t(full) - ŷ_t(placebo k off)|  /  Σ_t |ŷ_t(full) - ŷ_t(real drivers off)|
```

(of `Σ_t |ŷ_t(full)|` when the drivers' effect cannot be measured), so it works for non-linear
models (saturation, adstock, trees) alike and does not depend on the size of the baseline.
The check reports `max_k s_k` (pass ≤ 2%, warn ≤ 5%). For Bayesian models a placebo is flagged
when its share is above the warn limit with at least 90% posterior probability,
`P(s_k > 2%) ≥ 0.9` per draw: a "90% interval excludes zero" rule cannot work for a marketing
mix model, whose channel effects are positive in every draw. For frequentist models placebo
p-values are judged with Holm's correction for the `K` placebos (uncorrected, a good model is
flagged about once in seven runs with `K = 3`). A flag turns a nominal pass into a warning
(a failure when the share is above the fail limit). Models whose in-sample predictions follow
the stored history (the statsmodels plugins) use `|β_k| · Σ_t |z_k,t − z_k,t(off)|` instead.

### `null.placebo_time_shift`

Each driver is circularly shifted by every `δ` in `shifts` (default ±26 periods) and the model
refitted; the statistic is the placebo effect as a share of the real effect,
`max_δ Σ|Δ_δ ŷ| / Σ|Δ ŷ|` (pass ≤ 0.25, warn ≤ 0.5). It targets confounding by seasonality and
trend: a half-year shift deliberately misaligns annual seasons, so an effect that survives it is
not the driver's.

### `baseline.beats_naive` - out-of-sample skill

On `H`, with the training-only fit, `skill = 1 − MAE(model) / min_j MAE(benchmark_j)` over naive
(last value), seasonal naive (same period last season), mean and drift forecasts made from the
training history; MASE is reported as evidence. Pass ≥ 0.05, warn ≥ 0. For a formal test of
the difference on backtest folds, use the `compare.diebold_mariano` workflow node:

```text
d_t  = L(e1_t) - L(e2_t)                       (squared or absolute loss)
DM   = mean(d) / sqrt(lrv(d) / n)              lrv: autocovariances up to lag h - 1
HLN  = DM * sqrt((n + 1 - 2h + h(h - 1)/n) / n)
p    = 2 * P(T_{n-1} > |HLN|)
```

With one `h`-step error per backtest fold and folds `step` periods apart, only
`ceil(h / step) − 1` neighbouring folds share test periods, so that many autocovariances enter
`lrv` (and `h` in the correction is replaced by that number plus one); the node reads the
spacing from the folds. It needs more paired errors than that; with too few folds it says so
instead of returning a number.

### `residuals.whiteness`

Ljung-Box `Q = n(n + 2) Σ_{k=1..m} ρ̂_k² / (n − k)` on the in-sample residuals of every series,
with the start-up residuals of a diffuse-initialised fit removed (differencing, unobserved
components: the fit's own burn-in) and the degrees of freedom reduced by the ARMA terms only
(`m − p − q − P − Q`, never the constant, drivers or variance); the smallest p-value across
series counts (pass ≥ 0.05, warn ≥ 0.01).
Mind the multiplicity: with many series some rejections are expected, so a panel-wide policy may
prefer `severity: warning`.

### `stability.rolling_params`

The model is refitted on rolling windows of history (`window`, default about half the sample);
for each key estimate the coefficient of variation across windows is reported, scaled by the
estimate's own standard error when the estimate is near zero (otherwise a coefficient that
wobbles around zero would look unstable by construction). Pass ≤ 25%, warn ≤ 50%.

### `sensitivity.prior` and `recovery.simulated`

Prior sensitivity writes out every prior the model uses - its settings with the plugin's
defaults (for `mmm.bayesian` the default ROI prior `LogNormal(0, 1)`, `seasonality.prior_sigma`,
`trend.growth_prior_sigma`) and business priors translated at fit time - multiplies every prior
width (`sigma`, `scale`, `sd`, `tau` under a `prior…` key, and keys named `…prior_sigma` or
`…_sigma`; never Beta or Gamma `beta`/`b` parameters) by each of `scales`, and reports the
largest relative move of a key posterior mean (pass ≤ 20%). Recovery is
a parametric-bootstrap version of simulation-based calibration: `y* = E[y] + ε*` with
block-bootstrapped residuals, `S` refits, and the coverage of the 90% intervals for the fitted
values (target 90%, pass within 10 points). It is skipped for autoregressive models, where
resampled residuals do not reproduce the model's own dynamics.

### Interpreting a set of results

A policy is a multiple-testing procedure: with a dozen checks at their own thresholds, a sound
model will warn on something now and then. That is why severities exist. Make the checks that
protect the decision `blocking` (for a marketing-mix model the null tests and the signs, for a
forecast the naive benchmark and holdout accuracy) and leave diagnostics as `warning` or `info`
for the reviewer. Each `CheckResult` carries `value`, `threshold`, `message_plain`, an expert
message, a `suggested_action` and the evidence (the real score next to every shuffled score,
each placebo's share and interval), so the reviewer can see *how* close a call was.

## Try it

=== "CLI"

    ```bash
    --8<-- "cli_v05.sh:governance"
    ```

=== "Python"

    ```python
    --8<-- "client_v05_more.py:policies"
    ```

    ```python
    --8<-- "client_v05_more.py:validate"
    ```

=== "Policy file"

    ```yaml
    --8<-- "forecast-policy.yaml"
    ```

A workflow can run the same checks as a node (`governance.policy` with `submit: true`) and
record the validation itself - see the [cookbook](workflow-cookbook.md#readiness-to-arimax).
