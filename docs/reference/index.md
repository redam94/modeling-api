---
title: Reference
description: Reference for developers and operators of modeling-api. Look up the spec format, the catalogs of models, steps, plots and workflow nodes, the HTTP API, the CLI, error codes and settings.
---

# Reference

This reference is for developers and operators who know the task and need the exact detail.
The catalogs and the settings pages come from the code at each build.

<div class="mono-tiles mono-tiles--3 mono-tiles--compact" markdown>

-   ![](../assets/brand/icons/reference.webp){ .mono-icon }

    ## Spec format

    Each field of a ModelSpec, with examples.

    [Read the spec format](spec-format.md){ .mono-more }

-   ![](../assets/brand/icons/developer.webp){ .mono-icon }

    ## HTTP API

    Each route, with its request and its response.

    [Read the HTTP API](http-api.md){ .mono-more }

-   ![](../assets/brand/icons/error-codes.webp){ .mono-icon }

    ## Error codes

    Each stable error code and its meaning.

    [Find an error code](generated/error-codes.md){ .mono-more }

</div>

## Spec

<div class="mono-links" markdown>

- [**Spec format** The parts of a ModelSpec in prose, with examples.](spec-format.md)
- [**Spec schema** Each field of the spec documents, from the JSON Schema. *Generated*](generated/spec-schema.md)

</div>

## Catalogs

<div class="mono-links" markdown>

- [**Built-in steps** How the preprocessing steps (`core.*`) work together.](steps.md)
- [**Steps catalog** Each step with its parameters. *Generated*](generated/steps-catalog.md)
- [**Built-in models** How to use the models that come with the platform.](models.md)
- [**Models catalog** Each model with its settings, outputs and score modes. *Generated*](generated/models-catalog.md)
- [**Marketing-mix models (mmm-framework)** The MMM models and their settings.](../models/mmm-framework.md)
- [**Causal models and Bayesian structural time series** Three models of a change and one forecast model.](../models/causal.md)
- [**Workflow nodes** Each node with its inputs, outputs and parameters. *Generated*](workflow-nodes.md)
- [**Governance checks** Each check that a governance policy can require. *Generated*](generated/governance-checks.md)
- [**Plots and the sanitizer** How `mapi-plots` makes a figure and keeps it safe.](plots.md)
- [**Plot recipes catalog** Each plot recipe with an example picture. *Generated*](generated/plots-catalog.md)
- [**Extension points** Each place where your code or your templates connect. *Generated*](generated/extension-points.md)

</div>

## Interfaces

<div class="mono-links" markdown>

- [**HTTP API (v1)** Each route of the `/v1` API, with its request and its response.](http-api.md)
- [**OpenAPI explorer** The OpenAPI 3.1 document of the API, in a viewer.](openapi.md)
- [**CLI and client guide** The `mapi` CLI and `mapi_client`, by task.](cli.md)
- [**CLI commands** Each `mapi` command with its help text. *Generated*](generated/cli-reference.md)
- [**Python SDK** The runtime between a spec and a fitted model.](sdk.md)
- [**Run protocol** How a run moves from the API to a sandbox and back.](run-protocol.md)
- [**Runner** The process that executes one run in a sandbox.](runner.md)
- [**GCS shim** The local emulator of Google Cloud Storage.](gcs-shim.md)
- [**Web app and plot sandbox** The two browser apps and their servers.](web-app.md)
- [**Error codes** Each stable error code and its meaning. *Generated*](generated/error-codes.md)

</div>

## Settings

<div class="mono-links" markdown>

- [**Permissions** The actions that each role and each token scope can do. *Generated*](generated/permissions.md)
- [**Server settings** Each setting of `mapi-server`. *Generated*](generated/server-settings.md)
- [**Environment variables** Each `MAPI_*` variable and the components that use it. *Generated*](generated/environment-variables.md)

</div>

## Python API

These pages come from the docstrings of the Apache-2.0 packages.

<div class="mono-links" markdown>

- [**Python API** The packages on these pages and how to read them.](api/index.md)
- [**mapi_spec** Specs, manifests, errors and the models of the API.](api/spec.md)
- [**mapi_sdk** The base classes of models and steps, pipelines and bundles.](api/sdk.md)
- [**mapi_pymc** The base class for PyMC models.](api/pymc.md)
- [**mapi_sklearn** The adapter for scikit-learn estimators.](api/sklearn.md)
- [**mapi_steps** The built-in preprocessing steps.](api/steps.md)
- [**mapi_plots** Plot recipes and the helpers for figures.](api/plots.md)
- [**mapi_runner** The supervisor and the worker of a run.](api/runner.md)
- [**mapi_client** The typed client of the HTTP API.](api/client.md)

</div>
