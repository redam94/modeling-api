# Conformance corpus

The corpus is how the platform defines "supported": a set of small, realistic modelling
problems (ports of pymc-challenges and of the plan's examples) that must keep working, end to
end, on every change. Each fixture goes through exactly what a user's model goes through:

1. **validate** – the spec is checked statically against the step, model and plot catalogs,
   as the API does before accepting a run;
2. **fit** – a `fit` run on the in-process executor, honouring `fit.mode`
   (`evaluate_then_refit` included);
3. **score** – every request in `score_requests.yaml` as a `score` run; requests that must
   fail are checked for their typed error code (`HORIZON_EXCEEDS_FIT`, `UNSEEN_LEVELS`, ...);
4. **evaluate** – the holdout scored through the score path; the metrics must equal the
   ones computed at fit time;
5. **contract** – the full `check_model` contract kit;
6. **plots** – the spec's `outputs.plots` rendered from real outputs;
7. **expectations** – the fixture's own checks (recovered effects, terminal state, ...).

## Run it

```bash
python -m mapi_conformance list
python -m mapi_conformance run                         # every fixture at size tiny
python -m mapi_conformance run --fixture e24 --fixture c08
python -m mapi_conformance run --size full --json corpus.json
make conformance                                       # the same as `run` at size tiny
make conformance FIXTURE=e33                           # one fixture
make conformance-full                                  # every fixture at size full
python -m mapi_conformance list --deferred             # plus the plan fixtures not ported yet
```

The runner prints one row per fixture and exits non-zero if any step failed:

```text
fixture           model                           validate  fit  score   evaluate  contract  plots   expectations  ...  result
----------------  ------------------------------  --------  ---  ------  --------  --------  ------  ------------       ------
e02               tabular.hierarchical_glm@1.0.0  ok        ok   ok 6/6  ok        ok 13/13  ok 2/2  ok                 PASS
c08               ts.hsgp_trend@1.0.0             ok        ok   ok 6/6  ok        ok 13/13  ok 3/3  ok                 PASS
e24               ts.gaussian_hmm@1.0.0           ok        ok   ok 5/5  ok        ok 15/15  ok 2/2  ok                 PASS
xgb_store_demand  tabular.regressor@1.0.0         ok        ok   ok 6/6  ok        ok 16/16  ok 3/3  ok                 PASS
e33               mmm.hierarchical_geo@1.3.0      ok        ok   ok 5/5  ok        ok 16/16  ok 4/4  ok                 PASS
pymc_marketing    mmm.pymc_marketing@1.0.0        ok        ok   ok 4/4  ok        ok 16/16  ok 2/2  ok                 PASS
```

`--size tiny` (the default, used by `pytest -m conformance`) keeps data small and MCMC
short; `--size full` uses the spec's own fit settings and larger data.

### Through a running stack

The default runner skips the control plane. `--stack` drives the same fixtures through the
API of a running stack (`make dev`), as a user does: spec → **ingest** → fit → score.

1. The data, side tables and score payloads are uploaded as dataset versions (signed PUT,
   then a sandboxed ingest run).
2. The spec, pointed at the uploads, is validated, created and fitted through the
   dispatcher, the runner and FINALIZING.
3. Every score request runs against the registered model. An expected typed error may come
   from the failed run or from the API refusing the request up front.
4. The holdout metrics and stored plots are checked on the model version.
5. The bundle is downloaded through signed URLs with every hash verified, and goes through
   the fixture's `check_fit`.

```bash
python -m mapi_conformance run --stack http://127.0.0.1:8000 --fixture e33 --fixture xgb_store_demand
uv run pytest -m e2e tests/e2e/test_corpus_stack.py   # E33, xgboost and sklearn (`make e2e`)
```

The contract kit does not run in this mode; it needs the plugin in-process.

## The fixtures

| Fixture | Model | Exercises |
|---|---|---|
| `e02` | `tabular.hierarchical_glm` | long layout, shuffled county labels, hyperprior new counties, LOO, CRN counterfactual |
| `c08` | `ts.hsgp_trend` | sentinel codes, `max_extension` from anticipated coords, `HORIZON_EXCEEDS_FIT`, contiguity |
| `e24` | `ts.gaussian_hmm` | padded + mask, terminal state, forecast and filter, relabelling, invalid LOO |
| `xgb_store_demand` | `tabular.regressor` | pooled new stores, quantile aggregation refused, future-required covariates |
| `sklearn_store_demand` | `sklearn.tabular_conformal` | the same panel with plain scikit-learn (histogram boosting, skops): split-conformal intervals, interval aggregation refused, pooled new stores |
| `e33` | `mmm.hierarchical_geo` | FX (`supplied`) and holiday (`pinned`) side tables, split after `resample`, per-market lag context, new market from the hyperprior, weekly EUR plan via `inputs_stage`, CRN counterfactual (intervention and baseline-vs-scenario), derived quantities in EUR |
| `pymc_marketing` | `mmm.pymc_marketing` | a model that `owns` scaling, carry-over and units; a scoring window equals the same weeks of the full series |
| `e84_lite` | `choice.multinomial_logit` | a ragged household × trip panel with a text target, `last_per_group` split, products off the shelf, new households from the fitted spread, a price-rise counterfactual, next trips, categorical holdout metrics (hit rate against the naive guess, log score, Brier) |
| `guided_mmm` | `mmm.hierarchical_geo` | the `geo_mmm` Guided template's draft with business priors (ROI, contribution share, baseline share) translated at fit time and a lift test in the likelihood, both true to the simulation; ROI, baseline share and lift checked against the truth; a forecast and the channel-off preset |

### Deferred

The plan's corpus also lists fixtures that need plugins or run kinds that do not exist yet.
They are listed in `mapi_conformance.fixtures.DEFERRED` with the reason, and a test keeps the
plan's list, the fixtures and the deferrals in step:

| Plan fixture | Waits for |
|---|---|
| E13 state-space | a state-space plugin with the `filter` mode (vetted plugins, M4) |
| E82-slim GP Bayesian optimisation | the `recommend` mode and Campaign runs (M6) |
| E45 | a plugin for its model (vetted plugins, M4) |

## Porting time

The M0 exit criteria ask for the porting time of every fixture (target: under one day each,
model plugin plus fixture). It measures human effort, so it cannot be derived from the code or
from commit timestamps; record it here when a fixture is ported or re-ported.

| Fixture | Porting time | Recorded |
|---|---|---|
| `e02` | not measured yet | – |
| `c08` | not measured yet | – |
| `e24` | not measured yet | – |
| `xgb_store_demand` | not measured yet | – |
| `sklearn_store_demand` | not measured yet | – |
| `e33` | not measured yet | – |
| `pymc_marketing` | not measured yet | – |

## Add a fixture

Create a package `conformance/src/mapi_conformance/fixtures/<id>/` and list `<id>` in
`fixtures/__init__.py`.

**`data.py`** generates the data:

```python
def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    """The dataset and its side tables (name -> dataset)."""

def schema() -> DataSchema:            # optional: the raw schema (else inferred)
    ...

def payload_new_store(data, seed, size) -> xr.Dataset:   # optional payload builders
    ...
```

**`spec.yaml`** is an ordinary ModelSpec with the fit settings of a full run.

**`score_requests.yaml`** lists the requests to score. `apiVersion`, `kind` and `model` are
filled in for you:

```yaml
- name: forecast_with_new_store
  request: {mode: forecast, horizon: {dim: time, periods: 4}}
  payload: new_store          # none | fit_data | "tail:<n>" | a payload_<name> builder
  drop_targets: true          # remove target variables from the payload (default)
  side_tables: true           # pass the fixture's side tables to the run (default)
  expect: {vars: [y_hat], sizes: {week: 4}, issues: [UNSEEN_LEVELS], groups: [summary]}
- name: quantiles_are_not_summed
  request: {mode: forecast, horizon: {dim: time, periods: 4}, output: {aggregate: {over: [store_id]}}}
  payload: new_store
  expect_error: CAPABILITY_UNSUPPORTED
```

**`expectations.py`** (optional) adds fixture-specific checks; raise `AssertionError` to
fail:

```python
TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}       # fit overrides at size tiny
TINY_SETTINGS = {"m": 20}                                  # model-settings overrides
MODEL_CHECKS = None                                        # or a subset of check_model checks

def check_fit(bundle, data): ...
def check_score(name, tree, bundle, data): ...
def check_evaluate(tree, bundle, data): ...
```

A hook that declares a keyword `ctx` parameter also receives a `FixtureContext`: the seed,
size, registry, side tables and `results`, the trees of the score requests that succeeded
before this one. Use it to compare requests, as E33 does when it checks that a weekly EUR
plan forecasts exactly what the raw daily path forecasts:

```python
def check_score(name, tree, bundle, data, *, ctx):
    if name == "weekly_eur_plan" and "forecast_8w" in ctx.results:
        ...
```

A side table with `score_policy: supplied` must be named in each score request that
replays its step (`request.side_tables`); the runner supplies the data under that name.

From Python, `mapi_conformance.run_fixture("e24")` returns a `FixtureReport` and
`run_corpus()` a `CorpusReport` with `.table()` and `.to_json()`.
