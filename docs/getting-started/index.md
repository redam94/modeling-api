# Getting started

You can start in five ways. All five use the same spec format, steps, models and bundles.

<div class="grid cards" markdown>

-   :material-compass-outline: **Guided mode, no code** <span class="mapi-badge">v0.4</span>

    ---

    This way is for analysts. Select a template, upload a spreadsheet and answer some
    questions. You get a report, a sanity scorecard and what-if sliders. You need a platform
    that runs: the platform of your team, or `make dev` below.

    [:octicons-arrow-right-24: Your first MMM in Guided mode](../guides/guided-tutorial.md)

-   :material-rocket-launch-outline: **Run the platform** <span class="mapi-badge">v0.2</span>

    ---

    Start the API, the job queue, the sandboxed runner and the web app on your laptop with
    `make dev`. Then fit and score from the browser, the CLI, Python or HTTP.

    [:octicons-arrow-right-24: Quickstart: zero to first model in 10 minutes](quickstart.md)

-   :material-graph-outline: **Automate an analysis** <span class="mapi-badge">v0.5</span>

    ---

    Start a gallery workflow. Examples are unit-root tests to a backtested, governed ARIMAX,
    VAR/VECM, a forecast bake-off and a governed MMM. Run it from the browser, with
    `mapi workflows run --watch` or with Python.

    [:octicons-arrow-right-24: Workflow cookbook](../guides/workflow-cookbook.md)

-   :material-atom-variant: **Fit a science model** <span class="mapi-badge">v0.9</span>

    ---

    Do the analysis of a pymc-challenges notebook in the web app. Export the data with
    `python -m mapi_plugin_science.export`, upload it, fit a `sci.*` model and run the recipe
    of the case. Images, fields, maps and graphs are available.

    [:octicons-arrow-right-24: pymc-challenges in the web app](../tutorials/pymc-challenges/index.md)

-   :material-language-python: **Use the library in your own process**

    ---

    Use `import mapi_sdk` in a notebook, a script or a test suite. You can validate specs,
    fit, score and write bundles in-process. You can also test your own plugins with the
    contract kit.

    [:octicons-arrow-right-24: Your first model in Python](first-model.md)

</div>

The library and the platform both need Python 3.12 and [uv](https://docs.astral.sh/uv/). The
platform also needs Node.js 22+ with pnpm for the web app. Docker is optional
([Deployment](../operations/index.md)).

## Install

=== "From a clone (today)"

    We develop the packages together in one uv workspace. Clone the repository. Then sync
    all the packages and the developer tools:

    ```bash
    git clone <repository-url> modeling-api
    cd modeling-api
    uv sync --all-packages        # or: make install (also installs the web app)
    uv run python -c "import mapi_sdk, mapi_spec; print(mapi_sdk.__version__)"
    ```

    To open a notebook in the same environment, use `uv run --with jupyterlab jupyter lab`.

=== "Into your own project (once published)"

    Each package is a separate distribution. Install only the packages that you need:

    ```bash
    uv add mapi-sdk mapi-steps                 # the runtime and the built-in steps
    uv add mapi-pymc                           # PyMC models
    uv add mapi-sklearn                        # scikit-learn and xgboost models
    uv add "mapi-plots[recipes]"               # plot recipes (Altair, Plotly)
    uv add mapi-plugin-mmm mapi-plugin-ts mapi-plugin-tabular   # the built-in models
    uv add mapi-plugin-statsmodels "mapi-workflows[runtime]"    # statsmodels models, workflows
    uv add mapi-plugin-science                 # science models (sci.*) and the export helper
    ```

    !!! note "Not on PyPI yet"
        We will publish the distributions with the first tagged release. Until then, you
        install from the workspace, as the first tab shows.

## What each package does

| Package | Import | What it gives you | Heavy dependencies |
|---|---|---|---|
| `mapi-spec` | `mapi_spec` | The contracts: `DataSchema`, `ModelSpec`, `ScoreRequest`, capabilities, error codes, static validation and JSON Schemas | none (pure Pydantic) |
| `mapi-sdk` | `mapi_sdk` | The runtime: steps, pipelines, coord plans, summaries, bundles, the in-process executor and the contract kit | xarray, numpy, zarr |
| `mapi-steps` | `mapi_steps` | 28 built-in preprocessing steps and the split catalog | – |
| `mapi-pymc` | `mapi_pymc` | `PyMCModel`: you write `build()`. You get engines, diagnostics, new levels and counterfactuals. | PyMC |
| `mapi-sklearn` | `mapi_sklearn` | `SklearnModel`: you write `estimator()`. You get stacking, safe persistence and uncertainty. | scikit-learn, xgboost, skops |
| `mapi-plots` | `mapi_plots` | The JSON-only plot sanitizer and templates. Recipes come with the `recipes` extra. | Altair, Plotly (extra) |
| `mapi-plugin-*` | `mapi_plugin_*` | Built-in models: marketing mix (`mapi-plugin-mmm`, on mmm-framework), time series, statsmodels time series (`mapi-plugin-statsmodels`), tabular and science (`mapi-plugin-science`: 15 `sci.*` models for images, fields, maps and graphs) | as above; mmm-framework, statsmodels, scipy |
| `mapi-workflows` | `mapi_workflows` | The workflow engine, the node implementations and a local runner for notebooks ([guide](../guides/workflows.md#run-it-locally)) | The engine has none (pure Python, safe for the control plane). The node implementations are the `runtime` extra (numpy, xarray, scipy, statsmodels and the plot recipes of `mapi-plots[recipes]`). |
| `mapi-conformance` | `mapi_conformance` | The conformance corpus and its runner | all of the above |
| `mapi-client` | `mapi_client` | A typed Python client for the HTTP API ([guide](../guides/python-client.md)) | httpx (xarray optional) |
| `mapi-cli` | `mapi_cli` | The `mapi` command line ([guide](../guides/cli.md)) | typer, rich |
| `mapi-server` | `mapi_server` | The control plane: API, orchestrator and executors (AGPL or commercial) | FastAPI, SQLAlchemy; **no** numpy |
| `mapi-runner` | `mapi_runner` | The sandboxed run harness: supervisor and worker (AGPL or commercial) | the SDK, in the worker only |
| `mapi-gcs-shim` | `mapi_gcs_shim` | A local emulator of the Cloud Storage features that the platform uses | Starlette |

Plugins register themselves through Python entry points (`mapi.steps`, `mapi.models`,
`mapi.plots`). Thus `mapi_sdk.default_registry()` finds all the installed plugins.

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

-   :material-rocket-launch-outline: **[Quickstart](quickstart.md)**

    This page shows the platform from start to end. Use `make dev` and `make seed`. Then fit,
    examine and score from the browser. It takes about ten minutes.

-   :material-language-python: **[Your first model in Python](first-model.md)**

    Make a labelled dataset, write a spec and fit it. Then forecast a new store and look at
    the typed errors that protect you. It takes about ten minutes.

-   :material-play-network-outline: **[Runs and executors](executor.md)**

    Submit the same work as *runs* to the in-process executor. This is the protocol that the
    executors of the platform use.

-   :material-book-open-variant: **[Concepts](../concepts/index.md)**

    Dims and policies, stages and context tails, representations and bundles.

-   :material-puzzle-outline: **[Port a model](../guides/port-a-model.md)**

    Bring your own PyMC, scikit-learn or hand-written model.

-   :material-connection: **[Tutorials](../tutorials/index.md)**

    Google Sheets, embedded charts, webhook receivers, budget plans, complex workflows and
    the science models of pymc-challenges.

</div>
