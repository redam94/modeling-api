# Conformance corpus

The corpus is how the platform defines "supported". It is a set of small, realistic
modelling problems. These problems are ports of pymc-challenges and of the examples of the
plan. They must continue to work, from start to end, after each change. Each fixture goes
through exactly the same process as the model of a user:

1. **validate**: The runner checks the spec statically against the step, model and plot
   catalogs. The API does the same before it accepts a run.
2. **fit**: The runner does a `fit` run on the in-process executor. It obeys `fit.mode`
   (also `evaluate_then_refit`).
3. **score**: The runner does each request in `score_requests.yaml` as a `score` run. For
   requests that must fail, it checks the typed error code (for example
   `HORIZON_EXCEEDS_FIT` or `UNSEEN_LEVELS`).
4. **evaluate**: The runner scores the holdout through the score path. The metrics must be
   equal to the metrics that the fit calculated.
5. **contract**: The runner does the full `check_model` contract kit.
6. **plots**: The runner renders the `outputs.plots` of the spec from real outputs.
7. **expectations**: The runner does the own checks of the fixture (for example, recovered
   effects or the terminal state).

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

The runner shows one row for each fixture. If a step failed, the exit status is not zero:

```text
fixture           model                           validate  fit  score   evaluate  contract  plots   expectations  ...  result
----------------  ------------------------------  --------  ---  ------  --------  --------  ------  ------------       ------
e02               tabular.hierarchical_glm@1.0.0  ok        ok   ok 6/6  ok        ok 13/13  ok 2/2  ok                 PASS
c08               ts.hsgp_trend@1.0.0             ok        ok   ok 6/6  ok        ok 13/13  ok 3/3  ok                 PASS
e24               ts.gaussian_hmm@1.0.0           ok        ok   ok 5/5  ok        ok 15/15  ok 2/2  ok                 PASS
xgb_store_demand  tabular.regressor@1.0.0         ok        ok   ok 6/6  ok        ok 16/16  ok 3/3  ok                 PASS
e33               mmm.bayesian@1.0.0              ok        ok   ok 5/5  ok        ok 17/17  ok 4/4  ok                 PASS
mmm_frequentist   mmm.frequentist@1.0.0           ok        ok   ok 4/4  ok        ok 18/18  ok 1/1  ok                 PASS
```

`--size tiny` is the default, and `pytest -m conformance` uses it. It keeps the data small
and the MCMC short. `--size full` uses the own fit settings of the spec and larger data.

### Through a stack that runs

The default runner does not use the control plane. `--stack` sends the same fixtures through
the API of a stack that runs (`make dev`), as a user does: spec → **ingest** → fit → score.

1. The runner uploads the data, the side tables and the score payloads as dataset versions.
   It uses a signed PUT, and then a sandboxed ingest run.
2. The spec points at the uploads. The platform validates it, creates it and fits it through
   the dispatcher, the runner and FINALIZING.
3. Each score request runs against the registered model. An expected typed error can come
   from the failed run. It can also come from the API, if the API refuses the request at the
   start.
4. The runner checks the holdout metrics and the stored plots on the model version.
5. The runner downloads the bundle through signed URLs and verifies each hash. Then the
   bundle goes through the `check_fit` of the fixture.

```bash
python -m mapi_conformance run --stack http://127.0.0.1:8000 --fixture e33 --fixture xgb_store_demand
uv run pytest -m e2e tests/e2e/test_corpus_stack.py   # E33, xgboost and sklearn (`make e2e`)
```

The contract kit does not run in this mode. It needs the plugin in-process.

## The fixtures

| Fixture | Model | Exercises |
|---|---|---|
| `e02` | `tabular.hierarchical_glm` | Long layout, shuffled county labels, new counties from the hyperprior, LOO, CRN counterfactual |
| `c08` | `ts.hsgp_trend` | Sentinel codes, `max_extension` from anticipated coords, `HORIZON_EXCEEDS_FIT`, contiguity |
| `e24` | `ts.gaussian_hmm` | Padded data with a mask, terminal state, forecast and filter, relabelling, LOO that is not valid |
| `xgb_store_demand` | `tabular.regressor` | Pooled new stores, refused quantile aggregation, covariates that must have future values |
| `sklearn_store_demand` | `sklearn.tabular_conformal` | The same panel with plain scikit-learn (histogram boosting, skops): split-conformal intervals, refused interval aggregation, pooled new stores |
| `e33` | `mmm.bayesian` | FX (`supplied`) and holiday (`pinned`) side tables. Split after `resample`. mmm-framework owns scaling and carry-over (history added before the data at score time). New market from the hyperprior. Weekly EUR plan through `inputs_stage`. CRN counterfactual (intervention and baseline-vs-scenario). Derived quantities in EUR. |
| `mmm_frequentist` | `mmm.frequentist` | The ridge estimator of mmm-framework with bootstrap draws. A model that `owns` scaling, carry-over and units. A score window is equal to the same weeks of the full series. |
| `e84_lite` | `choice.multinomial_logit` | A ragged household × trip panel with a text target. `last_per_group` split. Products off the shelf. New households from the fitted spread. A price-rise counterfactual. Next trips. Categorical holdout metrics (hit rate against the naive guess, log score, Brier). |
| `guided_mmm` | `mmm.bayesian` | The draft of the `geo_mmm` Guided template with business priors (ROI, contribution share, baseline share). The fit translates the priors, and a lift test is in the likelihood. Both agree with the simulation. ROI, baseline share and lift are checked against the truth. A forecast and the channel-off preset. |
| `sm_arimax` | `ts.sarimax` | Weekly sales for each geo with price and promotion drivers. An ARIMAX model for each series (statsmodels) |
| `sm_vecm_macro` | `ts.vecm` | US real GDP, consumption and investment in log levels. A VECM with the Johansen cointegration rank |
| `e07_lgcp` | `sci.lgcp_grid` | Earthquake counts on a space-time grid. A log-Gaussian Cox process with a 2-D HSGP map |
| `e18_areal` | `sci.areal_bym2` | A disease map on a graph of areas (BYM2). The polygons stay for the map |
| `e50_kpp` | `sci.fisher_kpp` | A scratch assay as a Fisher-KPP reaction-diffusion PDE that the model solves |
| `e51_heat_plate` | `sci.heat_plate` | A heated plate, calibrated with the 2-D steady heat equation that the model solves |
| `e60_traction` | `sci.traction_force` | Traction force microscopy. A Gaussian inverse problem in Fourier space |
| `e80_lens` | `sci.strong_lens` | A galaxy-scale strong lens. A forward model of each pixel of a mock image |
| `e61_turing` | `sci.turing_pattern` | Schnakenberg kinetics from early images of a Turing pattern |
| `e58_storm` | `sci.storm_localisation` | Molecule positions in raw STORM frames (a Gaussian PSF with Poisson counts) |
| `e31_ibp` | `sci.ibp_features` | Binary features of small images. An Indian buffet process |
| `e77_colour_naming` | `sci.colour_naming` | Colour names of one language as a softmax over CIELAB values |
| `e79_cmb` | `sci.cmb_field` | A masked CMB map. A Gibbs sampler for the sky behind the mask and the bandpowers |
| `e21_network` | `sci.network_latent_space` | The ties of a graph, with sociality and a latent space |
| `e19_ggm` | `sci.graphical_model` | A Gaussian graphical model. Partial correlations and edge probabilities of proteins |
| `e23_phylo` | `sci.phylo_regression` | A regression across primate species, with residuals that the phylogeny correlates |
| `e81_circular` | `sci.circular_regression` | Wind directions on the circle. A von Mises regression |

### Deferred

The corpus of the plan also lists fixtures that need plugins or run kinds that do not exist
yet. `mapi_conformance.fixtures.DEFERRED` lists them with the reason. A test makes sure that
the list of the plan, the fixtures and the deferrals agree:

| Plan fixture | Waits for |
|---|---|
| E13 state-space | A state-space plugin with the `filter` mode (vetted plugins, planned) |
| E82-slim GP Bayesian optimisation | The `recommend` mode and Campaign runs (planned) |
| E45 | A plugin for its model (vetted plugins, planned) |

## Time to port

The M0 exit criteria ask for the time to port each fixture. The target is less than one day
for each fixture, for the model plugin and the fixture. This time is a measure of human
effort. Thus you cannot get it from the code or from commit timestamps. Record it here when
you port a fixture, or port it again.

| Fixture | Time to port | Recorded |
|---|---|---|
| `e02` | not measured yet | – |
| `c08` | not measured yet | – |
| `e24` | not measured yet | – |
| `xgb_store_demand` | not measured yet | – |
| `sklearn_store_demand` | not measured yet | – |
| `e33` | not measured yet | – |
| `mmm_frequentist` | not measured yet | – |

## Add a fixture

1. Create a package `conformance/src/mapi_conformance/fixtures/<id>/`.
2. Put `<id>` in the list in `fixtures/__init__.py`.
3. Add the files below to the package.

**`data.py`** makes the data:

```python
def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    """The dataset and its side tables (name -> dataset)."""

def schema() -> DataSchema:            # optional: the raw schema (else inferred)
    ...

def payload_new_store(data, seed, size) -> xr.Dataset:   # optional payload builders
    ...
```

**`spec.yaml`** is a usual ModelSpec with the fit settings of a full run.

**`score_requests.yaml`** lists the requests to score. The runner fills in `apiVersion`,
`kind` and `model` for you:

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

**`expectations.py`** (optional) adds checks for the fixture. To make a check fail, raise
`AssertionError`:

```python
TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}       # fit overrides at size tiny
TINY_SETTINGS = {"m": 20}                                  # model-settings overrides
MODEL_CHECKS = None                                        # or a subset of check_model checks

def check_fit(bundle, data): ...
def check_score(name, tree, bundle, data): ...
def check_evaluate(tree, bundle, data): ...
```

A hook that declares a keyword `ctx` parameter also receives a `FixtureContext`. The context
holds the seed, the size, the registry, the side tables and `results`. `results` holds the
trees of the score requests that succeeded before this one. Use it to compare requests. E33
does this. It checks that a weekly EUR plan forecasts exactly the same as the raw daily path:

```python
def check_score(name, tree, bundle, data, *, ctx):
    if name == "weekly_eur_plan" and "forecast_8w" in ctx.results:
        ...
```

A side table can have `score_policy: supplied`. Then each score request that replays its
step must name the side table (`request.side_tables`). The runner supplies the data with
that name.

From Python, `mapi_conformance.run_fixture("e24")` returns a `FixtureReport`.
`run_corpus()` returns a `CorpusReport` with `.table()` and `.to_json()`.
