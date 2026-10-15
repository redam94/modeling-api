# Licensing for modeling-api

modeling-api uses a **split licence** so that people who write models and integrations are never forced
into copyleft, while the hosted platform itself is protected.

| Component | Paths | Licence |
|---|---|---|
| **SDK, contracts, built-in steps/plots/plugins, CLI, Python client, GCS shim, conformance corpus** | `packages/spec`, `packages/sdk`, `packages/sdk-pymc`, `packages/sdk-sklearn`, `packages/steps-core`, `packages/plots-core`, `packages/cli`, `packages/client`, `packages/gcs-shim`, `plugins/*`, `conformance/` | **Apache License 2.0** — see [LICENSE-APACHE.txt](LICENSE-APACHE.txt) |
| **Platform** (control plane, run harness, web apps, images, infrastructure, ops) | `packages/server`, `packages/runner`, `apps/*`, `images/`, `infra/`, `ops/`, `security/` | **Dual licence**: GNU AGPLv3 ([LICENSE-AGPL.txt](LICENSE-AGPL.txt)) **or** a commercial licence ([COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)) |
| **Documentation** | `docs/` | CC BY 4.0 |

Each package directory contains its own `LICENSE` file and declares its SPDX expression in `pyproject.toml`
(`Apache-2.0` or `AGPL-3.0-only OR LicenseRef-mapi-commercial`).

**What this means in practice**

- Plugins you write against `mapi-sdk` / `mapi-pymc` / `mapi-sklearn` are yours; licence them however you like.
- Applications that call the platform over HTTP (with `mapi-client`, the TypeScript client, webhooks or MCP) are not
  derivative works of the platform.
- If you modify and offer the platform (`server`, `runner`, `apps`) as a network service, the AGPLv3 requires you to
  publish your modifications — unless you hold a commercial licence. Contact mattreda@mattreda.pro.

Contributions are accepted under the [Contributor Licence Agreement](CONTRIBUTOR-LICENSE.md).
