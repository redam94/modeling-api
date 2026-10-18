# API reference

[mkdocstrings](https://mkdocstrings.github.io/) makes these pages from the docstrings of the
Apache-2.0 packages. The pages show the modules that model authors, data scientists and
integrators use. The pages do not show private helpers (names that start with `_`).

| Package | Import | Use it for |
|---|---|---|
| [`mapi-spec`](spec.md) | `mapi_spec` | Contracts: schemas, specs, requests, capabilities, errors, static validation |
| [`mapi-sdk`](sdk.md) | `mapi_sdk` | The runtime: fit and score, steps, models, pipelines, bundles, coord plans, the contract kit |
| [`mapi-pymc`](pymc.md) | `mapi_pymc` | `PyMCModel`, `BuildContext`, engines, fast scoring |
| [`mapi-sklearn`](sklearn.md) | `mapi_sklearn` | `SklearnModel` and its variants, the estimator registry |
| [`mapi-steps`](steps.md) | `mapi_steps` | The built-in step registry and catalog export |
| [`mapi-plots`](plots.md) | `mapi_plots` | The sanitizer, templates, converters and recipes |
| [`mapi-client`](client.md) | `mapi_client` | The Python client: every API call, sync and async, workflows, governance, explorers |

!!! tip "Stable surface"
    If the package root exports a name again, import the name from the package root
    (`from mapi_sdk import fit_spec`). This page also shows the paths of the submodules.
    Before version 1.0, these paths can change between minor versions.
