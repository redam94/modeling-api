---
description: Where modeling-api runs - a laptop without Docker, a single host with Docker Compose, or Google Cloud - and how the pieces fit together.
---

# Deployment <span class="mapi-badge">v0.3</span>

The whole platform runs on one machine in two ways that start the same services on the same
ports, so URLs, the CLI, the Python client and the tests work with either. v0.3 adds Google
Cloud: Terraform for every project and scripts that deploy, migrate and roll back. All three
run the same multi-tenant code; only the executor, the store and the identity provider
change.

| | [Local, without Docker](local-development.md) | [Docker Compose](docker.md) | [Google Cloud](gcp-deployment.md) (v0.3) |
|---|---|---|---|
| Command | `make dev` | `make docker-build && make compose-up` | `deploy/gcp/deploy.sh --env ENV` |
| PostgreSQL | embedded (`pgserver`) or your `DATABASE_URL` | the official `postgres:16` image | Cloud SQL |
| Object storage | the GCS shim, in `.local/gcs` | the GCS shim image, in a volume | Cloud Storage |
| Runs execute as | `mapi-runner` subprocesses of the worker | subprocesses of the worker container, or one container per run | Cloud Run Jobs, one fresh sandbox per run |
| Code | your working tree, with hot reload | the [container images](images.md) | the same images |
| Sign-in | the development IdP | the development IdP | Google or another OpenID provider |
| Use it for | development, demos, the end-to-end tests | checking the production images end to end | a multi-tenant private beta: start with the [[verify M0] checklist](gcp-deployment.md#before-you-start-the-verify-m0-checklist) |

```mermaid
flowchart LR
    browser(("browser")) -->|":5173"| web["web app"]
    browser -->|":5174"| plots["plot sandbox<br/>(separate origin)"]
    browser -->|":8000 /v1"| api
    browser -->|":4443 signed URLs"| shim
    cli["mapi CLI ·<br/>Python client"] -->|"/v1"| api
    api["api<br/>(mapi-server api)"] --> pg[("PostgreSQL")]
    api --> shim["GCS shim"]
    worker["worker<br/>(dispatcher · reconciler ·<br/>finalizer · events)"] --> pg
    worker --> shim
    worker -->|"starts"| runner["mapi-runner<br/>supervisor + worker"]
    runner -->|"signed URLs only"| shim
```

Whichever you choose:

- **Sign-in is for development only.** Both run the servers in the `dev` profile: anyone who
  can reach the API can sign in as one of the seeded accounts through the built-in identity
  provider (`MAPI_STACK_PROFILE=single-tenant` turns sign-in off). The API listens on
  `127.0.0.1`, and compose publishes every port on `127.0.0.1` only. Do not expose a local stack to untrusted
  networks; see the [security model](../concepts/security.md#local-development-vs-production).
- **State is the database plus the object store.** Back up both (`.local/pg` and
  `.local/gcs` locally; the `pgdata` and `gcsdata` volumes under compose), and the bundle
  signing key (`.local/keys/`) if you want existing model versions to stay downloadable.
- **Migrations run first.** `make dev` and compose both run `mapi-server migrate` before the
  API and the worker start, and stop if it fails.

## In this section

- [Local, without Docker](local-development.md): what `make dev` starts, everyday commands,
  working on one component, where state lives.
- [Docker Compose](docker.md): the compose stack, its network layout and the executor choice.
- [Container images](images.md): the six images, how they are built and the guards that keep
  them honest.
- [Deploying to Google Cloud](gcp-deployment.md): the [verify M0] checklist, then the Terraform and
  deploy scripts, step by step, to the first tenant.
- [Security hardening on Google Cloud](security-hardening.md): what each control does, and the
  M0 verification checklist.
- [Authentication and isolation](authentication.md): sign-in, API tokens, operators,
  row-level security and Pub/Sub push.
- [Cloud Run Jobs executor](cloud-run.md): the v0.3 execution backend.
- [Observability](observability.md): metrics, logs, traces, and the dashboards, alerts and
  runbooks in `ops/`.
- [Security suite](security-suite.md): the sandbox-escape probes and what each isolation level
  must block.
- [M2 verification](m2-verification.md): what was verified of the M2 exit criteria without a
  cloud project, the measured numbers, and the steps left for a real one.
- [M3 verification](m3-verification.md): the Guided journey and the integrations through the
  live stack, the unaided-tester timings and fixes, and the protocol for the five testers.
- [Configuration reference](configuration.md): every environment variable.
- [Troubleshooting](troubleshooting.md): port conflicts and everything else.
