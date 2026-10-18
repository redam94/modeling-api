# Deploying modeling-api

This directory is the entry point for running the platform anywhere other than a developer
laptop. In v0.2 (milestone M1, the local vertical slice) the supported targets are:

| Target | Status | How |
|---|---|---|
| Laptop, no Docker | Supported | `make dev` ([docs/operations/local-development.md](../docs/operations/local-development.md)) |
| Single host with Docker | Supported, single tenant | `docker-compose.yml` ([docs/operations/docker.md](../docs/operations/docker.md)) |
| Google Cloud (Cloud Run Jobs, Cloud SQL, GCS) | v0.3, multi-tenant; validated and tested offline, not yet applied | Terraform under `infra/terraform`, scripts under [`deploy/gcp`](gcp) ([docs/operations/gcp-deployment.md](../docs/operations/gcp-deployment.md)) |

Nothing here is multi-tenant yet. Every table and object key already carries `tenant_id` and
`project_id` (always `default` in v0.2), so v0.3 can add authentication and row-level security
without migrating data.

## Artifacts

All images are built from `images/<name>/Dockerfile` with the repository root as the build
context, and are tagged `mapi/<name>:<version>` by `make docker-build`.

| Image | Role | Notes |
|---|---|---|
| `mapi/server` | Control plane: `mapi-server api`, `worker`, `migrate` | No science dependencies (enforced at build time) |
| `mapi/base-bayes` | Runner: PyMC models (`mmm.*`, `tabular.*`, `sci.*`) | Entry point `mapi-runner`; target `worker` = `mapi/worker` |
| `mapi/base-ml` | Runner: scikit-learn / xgboost models (`sklearn.*`) | No PyMC (enforced) |
| `mapi/base-forecast` | Runner: time-series models (`ts.*`) | |
| `mapi/gcs-shim` | Local Cloud Storage emulator | Development and tests only |
| `mapi/web` | nginx: the web app (`:8080`) and the plot sandbox (`:8081`) | The two must be served from different origins |

Every base image is pinned by digest (`make docker-pin-check`). Runner images bake their static
catalog to `/opt/mapi/catalog.json`, with its digest in `/opt/mapi/catalog.digest`.

## Single host with docker compose

```bash
cp .env.example .env                  # set POSTGRES_PASSWORD (required)
make docker-build                     # or: docker compose build
make compose-up                       # waits until every service is healthy
make seed                             # optional: the E33 demo data and spec
```

Then open <http://127.0.0.1:5173>. Ports are published on `127.0.0.1` only. To expose the stack
beyond the host, put a TLS-terminating reverse proxy in front of it, and set `MAPI_WEB_ORIGIN`,
`MAPI_PLOTS_ORIGIN`, `MAPI_API_URL` and `MAPI_GCS_ENDPOINT` to the public URLs. Note that **v0.2
has no authentication**, so do not expose it to untrusted networks.

### Operations checklist

- **Configuration:** `.env` (documented in `.env.example`). Compose builds `DATABASE_URL` from
  `POSTGRES_PASSWORD`.
- **State:** the `pgdata` volume (PostgreSQL) and the `gcsdata` volume (objects). Back up both.
  `make compose-down` keeps them; `make compose-down VOLUMES=1` deletes them.
- **Migrations:** the one-shot `migrate` service runs before `api` and `worker` start, and they
  only start if it exits with status 0.
- **Health:** every service has a health check (`docker compose ps`). The API serves `/healthz`
  (process) and `/readyz` (dependencies).
- **Logs:** `make compose-logs` (`SERVICE=api` for one service). Runner processes log through
  the worker.
- **Upgrades:** rebuild the images, then `make compose-up`; migrations run automatically. Pin
  `MAPI_VERSION` in `.env` to run a specific build.
- **Executor:** local-process inside the worker container by default. To run each job in its own
  container instead, use the docker executor in `compose.override.example.yml`, keeping in mind
  that the socket mount is root-equivalent on the host.

## Google Cloud

```bash
deploy/gcp/bootstrap.sh --env dev ...        # APIs, state bucket, backend.hcl
deploy/gcp/deploy.sh --env dev --foundation  # everything but the Cloud Run workloads
deploy/gcp/build-push.sh --env dev           # images, pinned by digest
deploy/gcp/deploy.sh --env dev               # plan, confirm, migrate, apply, smoke test
deploy/gcp/rollback.sh --env dev --previous  # or --traffic-only in an incident
```

Every script takes `--help` and `--dry-run`. `.github/workflows/deploy.yml` runs the same flow
from GitHub Actions (manual dispatch, Workload Identity Federation, required reviewers before
the apply). The step-by-step guide is
[docs/operations/gcp-deployment.md](../docs/operations/gcp-deployment.md); the controls and the
M0 verification checklist are in
[docs/operations/security-hardening.md](../docs/operations/security-hardening.md).

## Release gate

`make release-check` fails unless:

- one version is used everywhere (all `pyproject.toml` files, `__version__` strings,
  `package.json` files and the compose image tag);
- `CHANGELOG.md` has a dated entry for that version;
- no plan files are tracked by git;
- every base image is pinned by digest.

CI also builds every image, without pushing, on each pull request.

## Troubleshooting

See the troubleshooting sections of
[docs/operations/docker.md](../docs/operations/docker.md#troubleshooting) and
[docs/operations/local-development.md](../docs/operations/local-development.md#troubleshooting).
