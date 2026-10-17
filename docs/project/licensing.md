# Licensing

modeling-api uses a **split licence**. People who write models and integrations are never
pulled into copyleft; the hosted platform itself is protected.

| Component | Where | Licence |
|---|---|---|
| **SDK and contracts**: `mapi-spec`, `mapi-sdk`, `mapi-pymc`, `mapi-sklearn` | `packages/spec`, `packages/sdk`, `packages/sdk-pymc`, `packages/sdk-sklearn` | Apache-2.0 |
| **Built-ins**: steps, plots, model plugins, workflow runtime, conformance corpus | `packages/steps-core`, `packages/plots-core`, `plugins/*`, `packages/workflows`, `conformance/` | Apache-2.0 |
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
ArviZ, scikit-learn, xgboost and skops. The marketing-mix models run on mmm-framework
(Apache-2.0), which brings NumPyro and JAX (Apache-2.0) and nutpie (MIT). The time-series
models (`ts.sarimax`, `ts.var`, `ts.ets` and the rest) run on statsmodels (BSD-3-Clause).
The plot recipes use Altair and Plotly, and the visual workflow builder in the web app uses
@xyflow/react (MIT). The API server talks to PostgreSQL through psycopg (LGPL-3.0, used
unmodified as a separately installed library). Each library keeps its own licence.

`THIRD-PARTY-NOTICES.md` in the repository root is the full list: every Python and
JavaScript package the container images ship, with its version and SPDX licence. It also
reproduces the NOTICE files of Apache-2.0 packages, and the licence texts of the JavaScript
packages compiled into the web app and the plot sandbox. The file is generated from `uv.lock`
and `pnpm-lock.yaml`: run `make notices` after changing a lock file. `make release-check`
and the test suite fail when it is out of date. Every image carries it, with the licence
files above, in `/usr/share/doc/mapi/`.

## Contributions

Contributions are accepted under the Contributor Licence Agreement (`CONTRIBUTOR-LICENSE.md`),
which lets the project keep offering the platform under both licences. See
[Contributing](contributing.md).
