# API reference

Generated from the docstrings of the Apache-2.0 packages with
[mkdocstrings](https://mkdocstrings.github.io/). The pages list the modules a model author,
data scientist or integrator uses; private helpers (names starting with `_`) are left out.

| Package | Import | Use it for |
|---|---|---|
| [`mapi-spec`](spec.md) | `mapi_spec` | Contracts: schemas, specs, requests, capabilities, errors, static validation |
| [`mapi-sdk`](sdk.md) | `mapi_sdk` | The runtime: fit and score, steps, models, pipelines, bundles, coord plans, the contract kit |
| [`mapi-pymc`](pymc.md) | `mapi_pymc` | `PyMCModel`, `BuildContext`, engines, fast scoring |
| [`mapi-sklearn`](sklearn.md) | `mapi_sklearn` | `SklearnModel` and its variants, the estimator registry |
| [`mapi-steps`](steps.md) | `mapi_steps` | The built-in step registry and catalog export |
| [`mapi-plots`](plots.md) | `mapi_plots` | The sanitizer, templates, converters and recipes |

!!! tip "Stable surface"
    Import from the package root (`from mapi_sdk import fit_spec`) where a name is re-exported
    there; submodule paths below are documented for completeness and may move between minor
    versions before 1.0.
