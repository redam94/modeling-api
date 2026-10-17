---
description: Validate a model before it is used - policies, null tests against random noise, approvals by a second person, the evidence pack and the promotion requirement.
---

# Validate models with governance policies <span class="mapi-badge">v0.5</span>

A model that informs decisions should prove it learned something real. This guide shows how
to say which checks a model must pass (a **policy**), run them on a model version (a
**validation**), have a second person **approve** it, export the **evidence pack** for an
auditor, and make endpoints refuse models that are not validated. The concepts and the full
state machine are in [Governance](../concepts/governance.md); every operation is in the
[HTTP API reference](../reference/http-api.md#m35).

Everything here can also be done in the web app: see [In the web app](#in-the-web-app).
Why the null tests matter, in plain words and in statistics, is in
[Can you trust this model?](null-tests.md). From Python, use `api.governance` and
`api.validations` ([client guide](python-client.md#governance)); from a terminal,
`mapi governance`, `mapi validate model`, `mapi validations` and `mapi approve`
([CLI guide](cli.md#workflows-governance-and-explorers)):

```bash
--8<-- "cli_v05.sh:governance"
```

The examples below use `curl` with an API token (see [Endpoints and webhooks](endpoints-and-webhooks.md)
for the `api` helper):

```bash
api() { curl -sS -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: $MAPI_PROJECT" \
  -H 'Content-Type: application/json' "$@"; }
```

## What the checks mean, in plain words

Every check has an About card (`GET /v1/catalog/governance-checks`). In short:

| Check | It asks | How it is measured |
|---|---|---|
| `null.permutation_target` | Does the model beat versions of itself fitted on **shuffled data**? | A model with drivers keeps its result and gets its drivers shuffled together in blocks of time, so trend, season and controls cannot pass on their own; a model without drivers (or one that ignores them) gets the period-to-period changes of its result shuffled and added up again from the same start (the plain values for a series that does not wander), which keeps how much it wanders but removes anything predictable. The model is refitted `n_permutations` times and scored on its held-out skill against the null forecast (the last value, or the mean), which does not depend on the level a shuffle lands on. The p-value is the share of shuffled fits that did as well as the real one, `(1 + #as good) / (n + 1)`. With 20 shuffles the smallest possible p is 0.048, so use at least 19 for a 5% test. |
| `null.noise_regressors` | Does the model give **fake drivers made of pure noise** almost no credit? | `n_noise` placebo drivers are added - a real driver shuffled in time, or Gaussian noise with its mean and spread - as new channels of a marketing-mix model or new variables of any other model, and the model is refitted. Placebos get the model's default prior: the real channels' priors and calibration (business priors, lift tests) are not copied. Each placebo's share of the real drivers' total effect is measured by switching it off and scoring again (of the result when the drivers' effect cannot be measured). A Bayesian model is flagged when a placebo's share is above the warn limit with at least 90% posterior probability - not when its interval misses zero, which a marketing mix model's positive-only effects always do; frequentist models' placebo p-values are judged with Holm's correction for the number of placebos. |
| `null.placebo_time_shift` | Do the effects **vanish when the drivers are moved** months away? | The drivers are shifted (`shifts`, default ±26 periods, wrapped around) and the model refitted; the check reports the placebo effect as a share of the real one. A model that still finds most of the effect is picking up trend or season, not the drivers. |
| `baseline.beats_naive` | Is the model better than **simple benchmarks** on held-out periods? | The train-only model is scored on the held-out periods next to "same as last period", "same as last season", the mean or the drift line, forecast from the training history. Skill is `1 - error(model) / error(best benchmark)`; MASE is reported too. |
| `residuals.whiteness` | Is there **no pattern left** in the errors? | Ljung-Box on the in-sample residuals of every series (the smallest p-value counts; ARMA orders reduce the degrees of freedom). |
| `stability.rolling_params` | Are the key estimates **stable over time**? | The model is refitted on rolling windows of history; the check reports the largest coefficient of variation of the key estimates across windows. |
| `plausibility.signs` | Do effects **point the expected way**? | By default media and spend must raise the result (switch a channel off and the prediction must fall, per posterior draw for Bayesian models). List other expectations in `expected`, e.g. `{"price": "negative"}`. |
| `sensitivity.prior` | Bayesian models: are the answers **driven by the data**, not the assumptions? | Every prior width the model uses - including the plugin's defaults that the settings leave out (for `mmm.bayesian`: the default ROI prior `LogNormal(0, 1)`, `seasonality.prior_sigma`, `trend.growth_prior_sigma`, `hierarchy.media_geo_sigma`) and business priors translated at fit time - is multiplied by each of `scales` (`sigma`, `scale`, `sd`, `tau` under a `prior…` key, and keys named `…prior_sigma` or `…_sigma`; never Beta/Gamma `beta` or `b` parameters, which are not widths); the check reports the largest relative move of a key posterior mean. |
| `recovery.simulated` | Does the model **find known answers**? | Data is simulated from the fit (its expected values plus block-bootstrapped residuals), the model is refitted `n_sims` times, and the check counts how often the 90% intervals contain the fitted values (target 90%, a parametric-bootstrap version of simulation-based calibration). |
| `convergence` | Did the **fit finish properly**? | Bayesian: the largest split R-hat (divergences and effective sample size as evidence; any divergence makes a pass a warning). Others: the optimiser's converged flag. |
| `holdout.accuracy` | Is the error on **unseen data** below your limit? | The fit's holdout metric (`mape` by default, as a fraction). For `rmse` or `mae` set thresholds in the KPI's units. |
| `data.quality` | Is the **data good enough**? | The Data Explorer's quality warnings that are not just information: missing shares above `max_missing`, fewer than `min_history_periods`, constant drivers, negative spend, values outside declared bounds, duplicated periods, long gaps, collinear drivers. |

Every result carries the measured value, the threshold, a plain-language `message_plain`, a
`suggested_action` when it did not pass, and evidence (tables, numbers) for the auditor.

### Model families

Checks adapt to how a model is estimated (`mapi_spec.governance.model_kind`):

- **Bayesian** (MCMC, VI, SMC): refits use a budget of at most 200 draws and 200 tuning
  steps on 2 chains (`RefitBudget`), intervals are highest-density intervals, and
  `sensitivity.prior` applies.
- **Frequentist** (statsmodels, MAP, Kalman, ridge): refits run as configured; intervals are
  confidence intervals from standard errors; `convergence` reads the optimiser flag.
- **Machine learning** (scikit-learn, xgboost, quantile and conformal models): no parameter
  intervals, so `recovery.simulated` and `stability.rolling_params` are skipped when the model
  has no coefficients; the null tests and benchmarks apply unchanged.

A check that does not apply is reported as `skipped` with the reason (for example "the model
has no test period (add a data split and refit)"); a check that could not be computed is
`error`, which blocks validation when the check is blocking. A governance run refits at most
`max_refits` times (120 by default).

## The default policies

The first time a project uses governance the platform seeds three **tenant-wide** policies.
They decide what a validation checks, but they **do not gate promotion and do not require
approval**: an admin turns those on.

| Policy | Families | Blocking | Warning | Information |
|---|---|---|---|---|
| Marketing mix models | `mmm` | data quality, convergence, holdout accuracy (MAPE ≤ 15% warn / 30% fail), shuffled results (20), fake drivers (3), effect signs | beats benchmarks, time-shift placebo, prior sensitivity | residual whiteness |
| Forecasting models | `ts`, `forecast`, `ts_multivariate`, `regime` | data quality, convergence, beats benchmarks, holdout accuracy | residual whiteness, shuffled results | parameter stability |
| Regression and machine learning | `tabular`, `choice`, `sklearn` | data quality, holdout accuracy, shuffled results | fake drivers | convergence |

The thresholds are the catalog defaults: shuffled-result p-value ≤ 0.05 passes (≤ 0.10
warns), fake-driver share ≤ 2% passes (≤ 5% warns), placebo effect ≤ 25% passes (≤ 50%
warns), benchmark skill ≥ 0.05 passes (≥ 0 warns), Ljung-Box p ≥ 0.05 passes (≥ 0.01 warns),
rolling variation ≤ 25% (≤ 50%), wrong-sign share ≤ 10% (≤ 25%), prior sensitivity ≤ 20%
(≤ 50%), interval coverage within 10 points of 90% (25 points warns), R-hat ≤ 1.01 (≤ 1.05),
holdout MAPE ≤ 15% (≤ 30%), and no data problems (up to 2 warns).

## Write a policy

Project admins add policies to a project; tenant admins add tenant-wide ones, which every
project inherits as a floor (a project can tighten a threshold, never loosen it):

```bash
api -X POST "$API/v1/governance/policies" -d '{
  "name": "Forecasts we publish",
  "applies_to": {"families": ["ts"]},
  "checks": [
    {"check": "baseline.beats_naive"},
    {"check": "null.permutation_target", "params": {"n_permutations": 20}},
    {"check": "residuals.whiteness", "severity": "warning"},
    {"check": "holdout.accuracy", "thresholds": {"warn": 0.10, "fail": 0.20}}
  ],
  "require_approval": true,
  "approvals_required": 1,
  "require_validated_for_promotion": true
}'
```

`PUT …/policies/{policy_id}` saves the next **version** (send `expected_version` to refuse a
concurrent edit with `409`); `GET …/versions` lists every version; `DELETE` retires the
policy but keeps its versions for audit. `GET /v1/governance/policies` lists the policies in
force in the project, tenant-wide first.

## Validate a model version

```bash
api -X POST "$API/v1/models/$MODEL_ID/versions/7/validations" -d '{"note": "Q4 refresh"}'
```

The answer (`202`) is a validation in `checks_running` with the checks it is held to and the
governance run (`workflow_run_id`) that measures them in the sandbox. A validation is always
held to **every** policy in force for the model (the tenant-wide floor and the project's own);
`policy_ids` can only add more active policies on top of them, never leave one out. The
version is locked while a request is checked, so two simultaneous requests (or a request and
a workflow's submission) make one validation; the other gets `409
VALIDATION_TRANSITION_INVALID`. Follow the run like any
other (`GET /v1/runs/{run_id}`, or its event stream). When it finishes the validation moves
on by itself:

```text
checks_running -> checks_passed -> validated              (no approval required)
checks_running -> checks_passed -> awaiting_approval      (approval required)
checks_running -> checks_failed                           (a blocking check failed or errored)
```

`GET /v1/validations/{validation_id}` shows the status, every result and the history;
`GET …/evidence` pages through the results with their evidence. After a failure (or a
rejection) fix the model or the data and `POST /v1/validations/{id}:resubmit`: a new round
starts under the policies in force now. `:cancel` stops running checks (back to `draft`).

## Approve as a second person

When a policy requires approval, someone other than the submitter who holds the **reviewer**
capability (or is a project admin) decides. Grant it on the membership:

```bash
api -X PATCH "$API/v1/tenants/acme/projects/growth/members/$USER_ID" \
  -d '{"capabilities": ["reviewer"]}'
```

Reviewers find their queue at `GET /v1/validations?status=awaiting_approval` and decide in a
signed-in session (API tokens can never approve):

```bash
curl -X POST "$API/v1/validations/$VALIDATION_ID/approvals" -d '{"decision": "approve",
  "comment": "Null tests are convincing; benchmarks beaten by 30%."}'
```

The submitter is refused (`403 SELF_APPROVAL_FORBIDDEN`); each reviewer decides once per
round; `approvals_required` approvals validate the version; one rejection (which needs a
comment) rejects it. A validated version that should no longer be used is retired with
`:deprecate` and a reason.

## Promotion requires validation

When any policy in force says `require_validated_for_promotion`, promoting a model version to
an endpoint needs its latest validation to be `validated` **and** to have covered every such
policy as it stands now: either it names the policy at its current version, or it was held to
each of the policy's checks at least as strictly (severity, limits and settings) and had the
reviewers the policy asks for. A version validated before a gating policy was added or made
stricter is reported as *validated under other or older policies*; request a new validation
(allowed in that case). Anything else is refused with `409 NOT_VALIDATED`, and no
administrator override changes that; the refusal is audited as
`endpoint.promote_not_validated`.

## Export the evidence pack

```bash
api -X POST "$API/v1/validations/$VALIDATION_ID/evidence-pack" -d '{"format": "html"}'
```

The answer is the pack as JSON (`format: mapi.evidence-pack/v1`): the validation with every
result, its evidence, approvals and history, the policies at the versions it was held to, the
model's lineage and sanity scorecard, and a `digest` (sha256 of the canonical content).
With `format: html` it also carries `download`, a signed link to a static, self-contained HTML
rendering: inline styles only, no scripts, no external assets (a Content-Security-Policy
forbids them), every value escaped. Exports are audited (`model.evidence_export`).

## In the web app

**Governance** in the sidebar (++g++ then ++v++) has three tabs: **In force here** (the
tenant-wide policies first, then the project's own, with what each covers, its number of
checks, whether it needs a reviewer and whether it gates promotion), **Tenant-wide** and the
**Check catalog**, where every check has its About card, what it measures and its default
limits.

### Editing a policy

Open a policy (project admins edit project policies, tenant admins tenant-wide ones; everyone
else sees them read-only) or press **New policy**:

- **Covers**: model families, model ids and Guided templates; empty lists match every model.
- **Required checks**: add a check from the catalog - its About card shows before you add it
  - then set its severity (*Blocking*, *Warning*, *Information*), switch on **Custom** limits
  to set its warn and fail values (the page refuses a fail limit that is looser than the warn
  limit, as the server does) and adjust its settings, a form generated from the check's
  parameters (for example the number of shuffles).
- **Sign-off and promotion**: **Require approval by a reviewer** (and how many),
  **Only validated models can be promoted to an endpoint**, and **Active**.

**Save version N** stores the next version; a concurrent edit is refused and kept on screen.
**Versions** lists every version and compares two side by side. **Retire policy** keeps its
versions for audit.

### Validating a model version

A model version's page has a **Validation** button. It lists the version's validations and
requests a new one: the policies it is held to (every policy in force always applies; ticking
others adds them), optionally the data to check against, and a note. The validation page then shows:

- **Status**: where the validation is on its path (draft → checks running → checks passed →
  awaiting approval → validated, or checks failed, rejected, deprecated) and the history of
  every transition with who made it and their note.
- **Checks**: failures first, each with its plain-language message, **What to do** when it
  did not pass, the measured value against its limit and its evidence: small tables, numbers
  and, for the shuffled-result test, the distribution of the shuffled fits' errors with the
  real model's error marked (drawn in the plot sandbox).
- **Approvals**, **Evidence pack** and **Held to** (the policy versions).

Depending on its state the page offers **Cancel checks**, **Resubmit** (after failed checks or
a rejection) and **Deprecate** (with a reason, for a validated version; project admins).
**Export evidence pack** saves the readable HTML page or the JSON as a file; the HTML is never
opened inside the app.

### Reviewing

Reviewers find **Review queue** on the Governance page (with the number waiting), or
**Validations** filtered by *Awaiting approval*. The approval form appears only to members who
may approve: project admins, and editors or runners who hold the **reviewer** capability.
Rejecting needs a comment. On your own submission the form says that a second person has to
review it and its buttons are disabled; the server refuses it anyway
(`SELF_APPROVAL_FORBIDDEN`).

Project admins grant and revoke the capability in the **Reviewer** column of **Members**
(admins hold it implicitly; viewers cannot hold it).

### Promotion in the app

When a policy gates promotion, promoting a version that is not validated from **Integrations**
is refused with *A governance policy requires a validated model*, the version's current
validation status, and links to its validation (or to request one). No override is offered:
none passes this rule.

## Audit and webhooks

Every policy change (`policy.create`, `policy.update`, `policy.delete`), request
(`model.validation_request`), transition (`model.validation_transition`, with from, to and the
actor), decision (`model.approve`, `model.reject`) and deprecation is in the audit log.
Subscribe webhooks to `model.validation.checks_passed`, `model.validation.checks_failed`,
`model.validation.awaiting_approval`, `model.validated`, `model.rejected` and
`model.deprecated` to drive review queues and release pipelines.
