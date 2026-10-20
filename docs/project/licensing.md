# Licensing

modeling-api uses a **split licence**. The copyleft licence does not apply to persons who
write models and integrations. The copyleft licence protects the hosted platform.

| Component | Where | Licence |
|---|---|---|
| **SDK and contracts**: `mapi-spec`, `mapi-sdk`, `mapi-pymc`, `mapi-sklearn`, `mapi-jax` | `packages/spec`, `packages/sdk`, `packages/sdk-pymc`, `packages/sdk-sklearn`, `packages/sdk-jax` | Apache-2.0 |
| **Built-ins**: steps, plots, model plugins, workflow runtime, conformance corpus | `packages/steps-core`, `packages/plots-core`, `plugins/*`, `packages/workflows`, `conformance/` | Apache-2.0 |
| **Tools**: CLI, Python client, GCS emulator | `packages/cli`, `packages/client`, `packages/gcs-shim` | Apache-2.0 |
| **Platform**: API server, run harness, web apps, images, infrastructure | `packages/server`, `packages/runner`, `apps/*`, `images/`, `infra/`, `ops/`, `security/` | AGPLv3 **or** a commercial licence |
| **Documentation** | `docs/` | CC BY 4.0 |

Each package gives its SPDX expression in its `pyproject.toml` (`Apache-2.0`, or
`AGPL-3.0-only OR LicenseRef-mapi-commercial`). Each package also has its own `LICENSE` file.
The full texts are in the repository root: `LICENSE.md`, `LICENSE-APACHE.txt`,
`LICENSE-AGPL.txt` and `COMMERCIAL-LICENSE.md`.

## What this means in practice

!!! success "Plugins you write are yours"
    A step, model or plot recipe that you write with `mapi-sdk`, `mapi-pymc`,
    `mapi-sklearn` or `mapi-jax` uses Apache-2.0 libraries. You can use the licence that you want for it,
    and a proprietary licence is also possible.

!!! success "Applications that call the platform are not derivative works"
    Your application can call the platform over HTTP, with the Python or TypeScript client,
    through webhooks or through MCP. These calls do not make your application subject to the
    AGPL.

!!! warning "If you modify and host the platform"
    If you modify the platform components (server, runner, apps) and offer them as a network
    service, the AGPLv3 says that you must publish your modifications. This does not apply if you
    hold a commercial licence. For commercial terms, send an email to mattreda@mattreda.pro.

## Third-party libraries

The SDK uses xarray, numpy, pandas, zarr and Pydantic. The model packages use PyMC, ArviZ,
scikit-learn, xgboost and skops. The marketing-mix models run on mmm-framework (Apache-2.0).
mmm-framework adds NumPyro and JAX (Apache-2.0) and nutpie (MIT). The time-series models
(`ts.sarimax`, `ts.var`, `ts.ets` and the other time-series models) run on statsmodels
(BSD-3-Clause). The plot recipes use Altair and Plotly. From v0.13, `ts.bsts`, the causal
models (`mapi-plugin-causal`, Apache-2.0) and the Pathfinder engine use pymc-extras
(Apache-2.0). From v0.14, the runner decodes images with Pillow (MIT-CMU), and the
classifiers of the `gbm` extra use LightGBM (MIT) and CatBoost (Apache-2.0). From v0.15, the deep learning models
(`mapi-jax`) use JAX, Flax and Optax (Apache-2.0). The `base-dl` image also has the NVIDIA
CUDA runtime wheels from pip, which have the NVIDIA licence (NVIDIA Software License
Agreement). `THIRD-PARTY-NOTICES.md` lists them.

The visual workflow builder in the web app uses @xyflow/react (MIT). The API server connects
to PostgreSQL through psycopg (LGPL-3.0). The server uses psycopg without modification, as a
library that you install separately. Each library keeps its own licence.

`THIRD-PARTY-NOTICES.md` in the repository root is the full list. It gives each Python and
JavaScript package in the container images, with its version and SPDX licence. It also
contains the NOTICE files of Apache-2.0 packages. It also contains the licence texts of the
JavaScript packages that the build compiles into the web app and the plot sandbox.

A script makes this file from `uv.lock` and `pnpm-lock.yaml`. After you change a lock file,
run `make notices`. If the file is not current, `make release-check` and the test suite fail.
Each image contains this file and the licence files above, in `/usr/share/doc/mapi/`.

## Contributions

The project accepts contributions under the Contributor Licence Agreement
(`CONTRIBUTOR-LICENSE.md`). With this agreement, the project can continue to offer the
platform under the two licences. Refer to [Contributing](contributing.md).
