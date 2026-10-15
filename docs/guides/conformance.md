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
```

The runner prints one row per fixture and exits non-zero if any step failed:

```text
fixture           model                           validate  fit  score   evaluate  contract  plots   expectations  ...  result
----------------  ------------------------------  --------  ---  ------  --------  --------  ------  ------------       ------
e02               tabular.hierarchical_glm@1.0.0  ok        ok   ok 6/6  ok        ok 10/10  ok 2/2  ok                 PASS
c08               ts.hsgp_trend@1.0.0             ok        ok   ok 6/6  ok        ok 10/10  ok 3/3  ok                 PASS
e24               ts.gaussian_hmm@1.0.0           ok        ok   ok 5/5  ok        ok 12/12  ok 2/2  ok                 PASS
xgb_store_demand  tabular.regressor@1.0.0         ok        ok   ok 6/6  ok        ok 12/12  ok 3/3  ok                 PASS
e33               mmm.hierarchical_geo@1.3.0      ok        ok   ok 4/4  ok        ok 12/12  ok 4/4  ok                 PASS
pymc_marketing    mmm.pymc_marketing@1.0.0        ok        ok   ok 4/4  ok        ok 12/12  ok 2/2  ok                 PASS
```

`--size tiny` (the default, used by `pytest -m conformance`) keeps data small and MCMC
short; `--size full` uses the spec's own fit settings and larger data.

## The fixtures

| Fixture | Model | Exercises |
|---|---|---|
| `e02` | `tabular.hierarchical_glm` | long layout, shuffled county labels, hyperprior new counties, LOO, CRN counterfactual |
| `c08` | `ts.hsgp_trend` | sentinel codes, `max_extension` from anticipated coords, `HORIZON_EXCEEDS_FIT`, contiguity |
| `e24` | `ts.gaussian_hmm` | padded + mask, terminal state, forecast and filter, relabelling, invalid LOO |
| `xgb_store_demand` | `tabular.regressor` | pooled new stores, quantile aggregation refused, future-required covariates |
| `e33` | `mmm.hierarchical_geo` | FX (`supplied`) and holiday (`pinned`) side tables, split after `resample`, per-market lag context, new market from the hyperprior, weekly EUR plan via `inputs_stage`, CRN counterfactual, derived quantities in EUR |
| `pymc_marketing` | `mmm.pymc_marketing` | a model that `owns` scaling, carry-over and units; a scoring window equals the same weeks of the full series |

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
