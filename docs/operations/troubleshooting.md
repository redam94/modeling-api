---
description: Fixes for the problems you are most likely to meet running modeling-api - port conflicts, a stale stack, PostgreSQL, migrations, the web app and plots, scoring errors, Docker Compose.
---

# Troubleshooting <span class="mapi-badge">v0.2</span>

Start with `make status`: it shows which services of a `make dev` stack are up and whether
the embedded PostgreSQL is running. The web app's **Settings** page shows the API's
readiness checks, and `curl -s $MAPI_API_URL/readyz` shows the same from a shell.

## Port conflicts

`make dev` refuses to start when a port it needs is taken:

```text
cannot start - fix these first
  - 127.0.0.1:8000 is already in use
```

Another `make dev` (in another terminal or worktree), a compose stack, or an unrelated
program owns the port. Find it, and either stop it or move the platform:

```bash
lsof -iTCP:8000 -sTCP:LISTEN          # who has it?
make stop                              # if it is another make dev of this checkout
```

To move the platform, export the addresses **in every shell** that talks to it, then start
again. Every component (`make dev`, `make seed`, `make e2e`, the CLI, the Python client, the
web app's build) reads the same variables:

```bash
export MAPI_API_URL=http://127.0.0.1:18000
export MAPI_GCS_ENDPOINT=http://127.0.0.1:14443
export MAPI_WEB_ORIGIN=http://127.0.0.1:15173
export MAPI_PLOTS_ORIGIN=http://127.0.0.1:15174
make dev
```

| Port | Variable | Service |
|---|---|---|
| 8000 | `MAPI_API_URL` | API |
| 4443 | `MAPI_GCS_ENDPOINT` | GCS shim (signed URLs point here) |
| 5173 | `MAPI_WEB_ORIGIN` | Web app |
| 5174 | `MAPI_PLOTS_ORIGIN` | Plot sandbox |

A state directory belongs to one stack at a time; to run two stacks side by side, also give
each its own `MAPI_DATA_DIR`. The Playwright smoke suite (`pnpm --filter @mapi/web test`) serves
its own build on 5173 and 5174, so run it while `make dev` is stopped. Docker Compose
publishes the same four ports as `make dev`, on purpose: run one or the other.

## The local stack

`cannot start - fix these first`
:   The supervisor checks every requirement before starting anything. Besides ports:

    - **`module 'mapi_server.__main__' not found`**: your environment is stale. Run
      `uv sync --all-packages` (or `make install`).
    - **`exe 'pnpm' not found`** or **`run pnpm install first`**: install pnpm
      (`corepack enable`) and the JavaScript dependencies (`pnpm install`), or skip the front
      end with `make dev WITHOUT=web,plots`.

`already running (supervisor pid N)`
:   A previous `make dev` is still alive, perhaps in another terminal. Run `make stop`. If that
    process is gone but its state file stayed behind (after `kill -9`, for example), the next
    start detects the dead pid and ignores the file.

`plots exited unexpectedly with status 143; stopping the stack`
:   Something outside the stack sent the service `SIGTERM` (a `pkill vite`, for example). The
    supervisor never restarts services silently: it stops everything and exits. Start it
    again; runs that were in flight are recovered by the reconciler as a new attempt.

`postgres | failed to start`
:   Read `.local/pg/log`. If the data directory came from a different PostgreSQL major version,
    or is corrupt, run `make clean-local`; this deletes the local database. An orphaned server
    from an earlier crash is stopped by `make stop`.

`migrate exited with status 1`
:   The migration's own output is printed just above this line. Fix the migration, or reset the
    database with `make stop && make clean-local`, then run `make dev` again.

`api not ready after 60s`
:   The API process is running but `GET /healthz` never succeeded. Look at the `api |` lines. A
    wrong `DATABASE_URL`, or a GCS shim that is not reachable, are the usual causes.

Validation says the catalog is unavailable
:   On the first start against a new environment (and after every change to `uv.lock`) the
    worker queues a sandboxed *describe* run to export the catalog: `no catalog for sha256:…:
    queued describe run`. It takes a few seconds. If it fails, its run page under **Runs**
    shows why.

The server refuses `MAPI_PROFILE`
:   `MAPI_PROFILE` is also the CLI's config-profile variable. If you exported a client profile
    name, unset it in the shell that runs `make dev` (the server accepts only
    `single-tenant` or `multi-tenant`).

Leftover processes after a crash
:   Each service runs in its own process group, which you can find with
    `ps -o pid,pgid,command`. `make stop` also kills the process groups recorded in
    `.local/dev/supervisor.json` when the supervisor itself does not respond within 30 s.

## The web app and plots

**API unreachable** in the header, or the app cannot load anything
:   The app calls the API at the `VITE_API_BASE` it was started with. `make dev` sets it from
    `MAPI_API_URL`; if you start Vite yourself, set it too. The API must also allow the app's
    origin: it allows `MAPI_WEB_ORIGIN`, so both must agree.

Plots stay blank
:   `MAPI_WEB_ORIGIN` and `MAPI_PLOTS_ORIGIN` must be different origins, the plot sandbox must
    be running (`make status`), and the sandbox must allow the app's origin
    (`VITE_APP_ORIGINS`, set by `make dev`). The browser console names the blocked origin.

Uploads fail in the browser
:   The browser PUTs files straight to `MAPI_GCS_ENDPOINT`. The shim must allow the app's
    origin (`--cors-origin`, set by `make dev` from `MAPI_WEB_ORIGIN`), and the app's CSP must
    allow the storage origin (`VITE_STORAGE_ORIGINS`).

## Runs and scoring

A run is `stalled`
:   No progress arrived for a while. It is a warning, not a failure: the executor's own status
    decides whether the run is alive. A long PyTensor compile on the first fit of a new
    machine can cause it.

`DEV_MODE_ISOLATION` on every run
:   Expected on a laptop: the local executor runs the model code as your own user, without a
    separate uid or `no_new_privs`. See the [security model](../concepts/security.md).

`SIDE_TABLE_COVERAGE` when scoring
:   A side table with `score_policy: supplied` (in the demo, `fx`) must be given in the score
    request (`side_tables: {fx: dataset://default/fx_daily@latest}`) and cover the scored
    dates. Enter the pipeline after the step that uses it (`inputs_stage: weekly`) to skip it.

`MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `HORIZON_EXCEEDS_FIT`, …
:   Typed errors from the static checks: the request asks for something the model cannot
    answer honestly. The problem's `detail` and JSON pointer say what; the codes are listed
    in [Error codes](../reference/generated/error-codes.md).

`RESOURCE_EXHAUSTED`
:   The run ran out of memory or scratch space. **Retry** on the run page suggests the next
    resource class. Under compose, raise `MAPI_WORKER_MEMORY` and `MAPI_WORKER_TMPFS`.

`INTEGRITY_FAILURE` when downloading
:   The bundle's signed manifest no longer verifies, so the API refuses to mint download links.
    Locally this happens when `.local/keys/` was deleted: the old model versions cannot be
    trusted any more. Fit again.

## Docker and compose

`error: the Docker daemon is not reachable`
:   `make docker-build` and `make compose-up` check `docker info` first. Start Docker Desktop,
    Colima or `dockerd`, or use `make dev`, which needs no Docker.

`required variable POSTGRES_PASSWORD is missing a value`
:   Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`. Compose reads `.env` from the
    repository root.

`image check failed: forbidden distribution 'numpy' is installed` (server build)
:   A control-plane dependency started pulling in a science package. Run
    `uv run python scripts/lock_closure.py "mapi-server[docker]" --lacks numpy` to see the
    closure, then find which dependency brings `numpy` in.

`Contracts: 1 broken` in the `contracts` stage
:   Server code imports a forbidden module. `make lint` (`lint-imports`) shows the same failure
    locally, with the import chain.

`PyTensor found no C++ compiler` (base-bayes / base-forecast build)
:   `g++` is missing from `MAPI_APT_PACKAGES`. The PyMC images need it at run time too.

The `api` container is unhealthy
:   `make compose-logs SERVICE=api`. Typical causes are that `migrate` failed (look at its
    logs), or that the `DATABASE_URL` password does not match the database volume, which happens
    after you change `POSTGRES_PASSWORD`. In that case reset with `make compose-down VOLUMES=1`;
    this deletes the data.

`no bundle-signing key at /var/lib/mapi/keys/... and MAPI_SIGNING_KEY_CREATE is off`
:   `api` or `worker` started before `migrate` created the key, or without the `keys` volume.
    Check `make compose-logs SERVICE=migrate`. Never point the two at different keys: the API
    would reject every bundle the worker signs with `INTEGRITY_FAILURE`.

`400 Invalid host header` from the API, or nginx closes the connection
:   The stack answers only known host names (a DNS-rebinding guard). If you reach it under a
    name other than `127.0.0.1`, `localhost` or `api`, add that name to `MAPI_ALLOWED_HOSTS`
    (the API) and `MAPI_WEB_SERVER_NAMES` (the web image's nginx).

`mapi-entrypoint: /tmp/mapi-cache is not writable`
:   The container runs with a read-only root filesystem but without a writable `/tmp`. Add
    `--tmpfs /tmp` (compose already does).

Port already allocated on `docker compose up`
:   A `make dev` stack, or another process, holds 4443, 8000, 5173 or 5174. Stop it with
    `make stop`. Both stacks use the same ports on purpose, so the same URLs work with either.

nginx exits with `host not found` or `invalid number of arguments in "set"`
:   `MAPI_API_UPSTREAM` is empty or malformed. It must be a URL without a path, for example
    `http://api:8000`.
