# Getting started

modeling-api v0.1 runs in your own Python process: a notebook, a script or a test suite. You
need Python 3.12 and [uv](https://docs.astral.sh/uv/).

## Install

=== "From a clone (today)"

    The packages are developed together in one uv workspace. Clone the repository and sync
    every package plus the developer tools:

    ```bash
    git clone <repository-url> modeling-api
    cd modeling-api
    uv sync --all-packages        # or: make install
    uv run python -c "import mapi_sdk, mapi_spec; print(mapi_sdk.__version__)"
    ```

    Open a notebook in the same environment with `uv run --with jupyterlab jupyter lab`.

=== "Into your own project (once published)"

    Each package is a separate distribution, so you install only what you need:

    ```bash
    uv add mapi-sdk mapi-steps                 # the runtime and the built-in steps
    uv add mapi-pymc                           # PyMC models
    uv add mapi-sklearn                        # scikit-learn and xgboost models
    uv add "mapi-plots[recipes]"               # plot recipes (Altair, Plotly)
    uv add mapi-plugin-mmm mapi-plugin-ts mapi-plugin-tabular   # the built-in models
    ```

    !!! note "Not on PyPI yet"
        The distributions are published with the first tagged release. Until then, install
        from the workspace as shown in the first tab.

## What each package does

| Package | Import | What it gives you | Heavy dependencies |
|---|---|---|---|
| `mapi-spec` | `mapi_spec` | The contracts: `DataSchema`, `ModelSpec`, `ScoreRequest`, capabilities, error codes, static validation, JSON Schemas | none (pure Pydantic) |
| `mapi-sdk` | `mapi_sdk` | The runtime: steps, pipelines, coord plans, summaries, bundles, the in-process executor and the contract kit | xarray, numpy, zarr |
| `mapi-steps` | `mapi_steps` | 28 built-in preprocessing steps and the split catalog | – |
| `mapi-pymc` | `mapi_pymc` | `PyMCModel`: write `build()`, get engines, diagnostics, new levels and counterfactuals | PyMC |
| `mapi-sklearn` | `mapi_sklearn` | `SklearnModel`: write `estimator()`, get stacking, safe persistence and uncertainty | scikit-learn, xgboost, skops |
| `mapi-plots` | `mapi_plots` | The JSON-only plot sanitizer and templates; recipes with the `recipes` extra | Altair, Plotly (extra) |
| `mapi-plugin-*` | `mapi_plugin_*` | Built-in models: marketing mix, time series, tabular | as above |
| `mapi-conformance` | `mapi_conformance` | The conformance corpus and its runner | all of the above |

Plugins register themselves through Python entry points (`mapi.steps`, `mapi.models`,
`mapi.plots`), so `mapi_sdk.default_registry()` finds everything that is installed.

## Check your installation

```bash
uv run python -m mapi_conformance list          # the reference problems
make conformance FIXTURE=xgb_store_demand       # one of them, end to end (a few seconds)
```

```text
fixture           model                    validate  fit  score   evaluate  contract  plots   expectations  metrics                                  time  result
----------------  -----------------------  --------  ---  ------  --------  --------  ------  ------------  ---------------------------------------  ----  ------
xgb_store_demand  tabular.regressor@1.0.0  ok        ok   ok 6/6  ok        ok 12/12  ok 3/3  ok            coverage_80=0.825, mape=25.3, rmse=4.59  3.3s  PASS
----------------  -----------------------  --------  ---  ------  --------  --------  ------  ------------  ---------------------------------------  ----  ------
1/1 fixtures passed in 3.3s
```

## Where next

<div class="grid cards" markdown>

-   :material-rocket-launch-outline: **[Your first model](first-model.md)**

    Build a labelled dataset, write a spec, fit it, forecast a new store and see the typed
    errors that protect you. About ten minutes.

-   :material-play-network-outline: **[Runs and executors](executor.md)**

    Submit the same work as *runs* to the in-process executor: the protocol the hosted
    platform will use.

-   :material-book-open-variant: **[Concepts](../concepts/index.md)**

    Dims and policies, stages and context tails, representations, bundles.

-   :material-puzzle-outline: **[Port a model](../guides/port-a-model.md)**

    Bring your own PyMC, scikit-learn or hand-written model.

</div>
