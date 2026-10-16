---
description: Import a BayesInsight project (model_def.json, metadata.json and its MFF data) into modeling-api - choose the dedicated mmm.bayesinsight model or the built-in geo MMM, read the import report, check and fit it on a laptop, then run it on the platform.
---

# Import a BayesInsight model <span class="mapi-badge">v0.4</span>

A BayesInsight project is three files: `model_def.json` (the model), `metadata.json` (the
keys of the data) and the data itself as an MFF long table. modeling-api imports them as a
**draft**: an ingest mapping for the MFF table and a `ModelSpec`, plus a report of every
feature that did not carry over exactly. Nothing is dropped silently.

In this tutorial you import a project, read the report, check the draft and fit it on your
laptop - where you can see that it recovers the parameters the data was simulated with - and
then run it on the platform. The code is `docs/snippets/bayesinsight_import.py` in the
repository, which the test suite runs.

## The example project

The files come from `mapi_plugin_mmm.bayesinsight_examples`: 60 weeks of sales for two
geographies (US, DE) × two products (Phones, Watches), driven by TV (delayed carry-over,
Hill curve) and search (geometric carry-over, Hill curve), a price control with a negative
sign, an annual season and Normal noise. The MFF table looks like this:

```text
Period,Geography,Product,Outlet,Campaign,Creative,VariableName,VariableValue
2023-01-02,DE,Phones,Total,Total,Total,Price,8.904
2023-01-02,DE,Phones,Total,Total,Total,Sales,240.633
2023-01-02,DE,Phones,Total,Total,Total,Search,26.58
2023-01-02,DE,Phones,Total,Total,Total,TV,102.21
```

and `model_def.json` names its variables BayesInsight's way, `TV_Total_Total_Total`, with
priors in the KPI's own units (a TV coefficient around 30 sales per unit of the curve).

## 1. Choose a target

| | `mmm.bayesinsight` | `mmm.hierarchical_geo` (default) |
|---|---|---|
| What it fits | BayesInsight's model **as `model_def.json` defines it**: likelihoods (Normal, log-normal, Poisson, negative binomial, zero-inflated), priors in KPI units, Hill/linear/S-shaped curves, delayed, geometric or Weibull carry-over, enforced signs, random effects per row id, seasons, local trends | The platform's hierarchical geo MMM, with every BayesInsight feature that has an equivalent mapped onto it |
| Panel dims | Every row id besides `Period` is its own dim (`geo`, `product`) | One `geo` dim; several row ids are joined (`US|Phones`) |
| Priors | Kept, in KPI units | Coefficient priors are **not** carried over (they would mean something else on the mean-scaled KPI): state them as [business priors](../guides/guided-modelling.md) instead |
| Guided extras | Lift tests (under the model's own likelihood) | Business priors, lift tests, what-if grids |
| Use it when | You want BayesInsight's results, reproduced and served | You want to move to the platform's geo MMM and its Guided tools |

## 2. Import

The import is pure JSON in, JSON out, the same function the API runs, so you can call it
offline:

```python
--8<-- "bayesinsight_import.py:import"
```

```text
info    /model_def/variable_details/2/adstock: Search_Total_Total_Total: BayesInsight's code cannot run 'geometric' c…
info    /model_def/variable_details/3/sign: Price_Total_Total_Total: the negative sign is enforced (BayesInsight d…
['date', 'geo', 'product'] ['price', 'sales', 'search', 'tv']
```

Each issue has a severity, a JSON pointer into your files and a message:

- **info**: faithful but perhaps surprising. Here, geometric carry-over is fitted as
  documented although BayesInsight's own code cannot run it, and the price sign is enforced
  (BayesInsight declares signs without enforcing them).
- **warning**: something fell back to BayesInsight's default or has no equivalent. Read
  every one before you fit.
- **error**: the project cannot be imported (no single KPI, no media or control, names that
  do not match the row ids); the API answers `422 IMPORT_UNSUPPORTED`.

The same project imported for the geo MMM warns about what does not carry over:

```python
--8<-- "bayesinsight_import.py:compare"
```

```text
warning /model_def/variable_details: mmm.hierarchical_geo uses one adstock kind for every channel; 'geometr…
warning /model_def/variable_details/2/coeff_prior: media coefficient priors are in KPI units and are not carried over; st…
warning /model_def/variable_details/3/sign: Price_Total_Total_Total: sign constraints on controls have no equivale…
```

The full mapping table is in
[From a spreadsheet to a Guided draft](../guides/guided-data.md#import-a-bayesinsight-model).

## 3. Check the draft

Validate the spec against the installed catalog and the schema the mapping promises, as the
API does when you save it:

```python
--8<-- "bayesinsight_import.py:validate"
```

```text
valid
```

`draft.yaml` is the spec as YAML, the file you would review in a pull request:
`model.settings.model_def` carries your `model_def.json`, `variables` maps each analytic name
(`TV_Total_Total_Total`) to its variable, and a `core.lag_tensor` step with 12 lags feeds the
carry-over.

## 4. Fit it on your laptop

Before you spend platform compute, fit the draft in-process with tiny settings. The dataset
comes from the sandboxed ingest's own code applied to the MFF table:

```python
--8<-- "bayesinsight_import.py:fit"
```

```text
{'tv': 25.9, 'search': 17.5, 'price': -7.6}
holdout MAPE %: 1.9
```

The data was simulated with coefficients of 30 (TV), 20 (search) and −8 (price): 100 draws on
two chains, well under a minute on a laptop, land close, and the eight held-back weeks are
forecast within 2%. Keep the draft's own fit settings (500 tuning steps and 500 draws on 4
chains) for results you act on.

## 5. Run it on the platform

The same import through the API (here with the Python client; the answer is the same draft):

```python
--8<-- "bayesinsight_import.py:api"
```

Then upload the MFF table with the draft's mapping, save the spec and fit:

```python
version = api.datasets.upload("sales_mff.csv", dataset="bi_mff", mapping=remote.mapping)
revision = api.specs.upsert(remote.yaml)
run = api.runs.wait(api.runs.fit(revision.spec_id, revision.revision).id,
                    raise_on_failure=True)
print(run.output_model_version_id)
```

The upload is checked in a sandbox with the `bayesinsight` preset: only the `allowed_*` key
values and the `start_period`/`end_period` range of `metadata.json` are kept, and the
`necessary_variables` must be present. With `curl`, the import is
`POST /v1/guided/import/bayesinsight` ([the guide](../guides/guided-data.md#import-a-bayesinsight-model)).

The fitted version gets the same sanity scorecard as every model; promote it to an endpoint
from the [Integrations hub](../guides/integrations-hub.md), and Atlas, Sheets and embeds work
with it like with any other model.

## Next

- [The `mmm.bayesinsight` model reference](../reference/models.md#mmmbayesinsight-a-bayesinsight-model_defjson):
  every likelihood, curve and carry-over, and how contributions are split.
- [Lift tests for `mmm.bayesinsight`](../guides/guided-modelling.md): calibration under the
  model's own likelihood.
- [Optimise a budget with Atlas](../guides/atlas.md) against the promoted endpoint.
