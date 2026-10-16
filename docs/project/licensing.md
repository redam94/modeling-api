# Licensing

modeling-api uses a **split licence**. People who write models and integrations are never
pulled into copyleft; the hosted platform itself is protected.

| Component | Where | Licence |
|---|---|---|
| **SDK and contracts**: `mapi-spec`, `mapi-sdk`, `mapi-pymc`, `mapi-sklearn` | `packages/spec`, `packages/sdk`, `packages/sdk-pymc`, `packages/sdk-sklearn` | Apache-2.0 |
| **Built-ins**: steps, plots, model plugins, conformance corpus | `packages/steps-core`, `packages/plots-core`, `plugins/*`, `conformance/` | Apache-2.0 |
| **Tools**: CLI, Python client, GCS emulator | `packages/cli`, `packages/client`, `packages/gcs-shim` | Apache-2.0 |
| **Platform**: API server, run harness, web apps, images, infrastructure | `packages/server`, `packages/runner`, `apps/*`, `images/`, `infra/`, `ops/`, `security/` | AGPLv3 **or** a commercial licence |
| **Documentation** | `docs/` | CC BY 4.0 |

Each package declares its SPDX expression in its `pyproject.toml` (`Apache-2.0`, or
`AGPL-3.0-only OR LicenseRef-mapi-commercial`) and carries its own `LICENSE` file. The full
texts are in the repository root: `LICENSE.md`, `LICENSE-APACHE.txt`, `LICENSE-AGPL.txt` and
`COMMERCIAL-LICENSE.md`.

## What this means in practice

!!! success "Plugins you write are yours"
    A step, model or plot recipe written against `mapi-sdk`, `mapi-pymc` or `mapi-sklearn` uses
    Apache-2.0 libraries. License it however you like, including proprietary.

!!! success "Applications that call the platform are not derivative works"
    Calling the platform over HTTP, with the Python or TypeScript client, through webhooks or
    MCP, does not make your application subject to the AGPL.

!!! warning "Modifying and hosting the platform"
    If you modify the platform components (server, runner, apps) and offer them as a network
    service, the AGPLv3 requires you to publish your modifications, unless you hold a
    commercial licence. Contact mattreda@mattreda.pro for commercial terms.

## Third-party libraries

The SDK builds on xarray, numpy, pandas, zarr and Pydantic; the model packages on PyMC,
ArviZ, scikit-learn, xgboost and skops; the marketing-mix models on mmm-framework (Apache-2.0); the plot
recipes on Altair and Plotly. Each keeps its own licence.

## Contributions

Contributions are accepted under the Contributor Licence Agreement (`CONTRIBUTOR-LICENSE.md`),
which lets the project keep offering the platform under both licences. See
[Contributing](contributing.md).
