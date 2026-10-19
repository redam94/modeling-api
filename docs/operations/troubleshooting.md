---
description: Fixes for the problems you are most likely to meet running modeling-api - port conflicts, a stale stack, PostgreSQL, migrations, the web app and plots, scoring errors, workflows and science models, Docker Compose.
---

# Troubleshooting <span class="mapi-badge">v0.2</span>

Start with `make status`. It shows which services of a `make dev` stack operate, and if the
embedded PostgreSQL runs. The **Settings** page of the web app shows the readiness checks of
the API. `curl -s $MAPI_API_URL/readyz` shows the same data from a shell.

## Port conflicts

If a different process uses a port that `make dev` needs, `make dev` does not start:

```text
cannot start - fix these first
  - 127.0.0.1:8000 is already in use
```

The port can belong to a different `make dev` (in a different terminal or worktree), a
compose stack, or a program that is not related. Find the process. Then stop it, or move
the platform:

```bash
lsof -iTCP:8000 -sTCP:LISTEN          # who has it?
make stop                              # if it is another make dev of this checkout
```

To move the platform, export the addresses **in each shell** that connects to it. Then start
again. All components (`make dev`, `make seed`, `make e2e`, the CLI, the Python client, the
build of the web app) read the same variables:

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

A state directory is for one stack at a time. To run two stacks at the same time, also give
each stack its own `MAPI_DATA_DIR`. The Playwright smoke suite (`pnpm --filter @mapi/web test`)
serves its own build on 5173 and 5174. Thus run it only while `make dev` is stopped. Docker
Compose publishes the same four ports as `make dev`, on purpose. Run only one of the two.

## The local stack

`cannot start - fix these first`
:   The supervisor does a check of each necessary item before it starts a service. In addition
    to ports, it can show these errors:

    - **`module 'mapi_server.__main__' not found`**: your environment is not current. Run
      `uv sync --all-packages` (or `make install`).
    - **`exe 'pnpm' not found`** or **`run pnpm install first`**: install pnpm
      (`corepack enable`) and the JavaScript dependencies (`pnpm install`). You can also skip
      the front end with `make dev WITHOUT=web,plots`.

`already running (supervisor pid N)`
:   A previous `make dev` still runs, possibly in a different terminal. Run `make stop`. The
    process can be gone while its state file stays (for example, after `kill -9`). Then the
    next start finds the dead pid and ignores the file.

`plots exited unexpectedly with status 143; stopping the stack`
:   A process outside the stack sent `SIGTERM` to the service (for example a `pkill vite`).
    The supervisor never starts services again without a message. It stops all services and
    stops itself. Start it again. The reconciler recovers the runs that were in progress as a
    new attempt.

`postgres | failed to start`
:   Read `.local/pg/log`. If the data directory came from a different PostgreSQL major
    version, or is corrupt, run `make clean-local`. This deletes the local database.
    `make stop` stops an orphaned server from an earlier crash.

`migrate exited with status 1`
:   The output of the migration is immediately above this line. Repair the migration, or
    reset the database with `make stop && make clean-local`. Then run `make dev` again.

`api not ready after 60s`
:   The API process runs, but `GET /healthz` never succeeded. Look at the `api |` lines. The
    usual causes are a wrong `DATABASE_URL`, or a GCS shim that the API cannot connect to.

Validation says the catalog is unavailable
:   At the first start against a new environment (and after each change to `uv.lock`), the
    worker puts a sandboxed *describe* run in the queue to export the catalog:
    `no catalog for sha256:…: queued describe run`. This takes some seconds. If it fails, its
    run page under **Runs** shows the reason.

The server refuses `MAPI_PROFILE`
:   `MAPI_PROFILE` is also the config-profile variable of the CLI. If you exported a client
    profile name, unset it in the shell that runs `make dev`. The server accepts only
    `single-tenant`, `multi-tenant` or `dev`.

Leftover processes after a crash
:   Each service runs in its own process group. You can find it with
    `ps -o pid,pgid,command`. If the supervisor does not respond in 30 s, `make stop` also
    stops the process groups in `.local/dev/supervisor.json`.

## The web app and plots

**API unreachable** in the header, or the app cannot load data
:   The app calls the API at the `VITE_API_BASE` that it started with. `make dev` sets it
    from `MAPI_API_URL`. If you start Vite yourself, also set it. The API must also permit
    the origin of the app. It permits `MAPI_WEB_ORIGIN`, so the two must agree.

Plots stay blank
:   Make sure of these three conditions:

    - `MAPI_WEB_ORIGIN` and `MAPI_PLOTS_ORIGIN` are different origins.
    - The plot sandbox runs (`make status`).
    - The sandbox permits the origin of the app (`VITE_APP_ORIGINS`, set by `make dev`).

    The browser console names the blocked origin.

Uploads fail in the browser
:   The browser sends files with PUT directly to `MAPI_GCS_ENDPOINT`. The shim must permit
    the origin of the app (`--cors-origin`, which `make dev` sets from `MAPI_WEB_ORIGIN`). The
    CSP of the app must permit the storage origin (`VITE_STORAGE_ORIGINS`).

## Runs and scoring

A run is `stalled`
:   No progress came for some time. This is a warning, not a failure. The status of the
    executor decides if the run is alive. A long PyTensor compile on the first fit of a new
    machine can cause it.

`DEV_MODE_ISOLATION` on every run
:   This is expected on a laptop. The local executor runs the model code as your own user,
    without a separate uid or `no_new_privs`. Refer to the
    [security model](../concepts/security.md).

`SIDE_TABLE_COVERAGE` when scoring
:   You must give a side table with `score_policy: supplied` (in the demo, `fx`) in the score
    request (`side_tables: {fx: dataset://default/fx_daily@latest}`). It must include the
    scored dates. To skip it, enter the pipeline after the step that uses it
    (`inputs_stage: weekly`).

`MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `HORIZON_EXCEEDS_FIT`, …
:   These are typed errors from the static checks. The request asks for an answer that the
    model cannot give honestly. The `detail` and the JSON pointer of the problem tell what is
    wrong. [Error codes](../reference/generated/error-codes.md) lists the codes.

`RESOURCE_EXHAUSTED`
:   The run did not have sufficient memory or scratch space. **Retry** on the run page
    suggests the next resource class. Under compose, increase `MAPI_WORKER_MEMORY` and
    `MAPI_WORKER_TMPFS`.

`INTEGRITY_FAILURE` when downloading
:   The signed manifest of the bundle does not verify. Thus the API does not mint download
    links. Locally, this occurs when somebody deleted `.local/keys/`. You cannot trust the
    old model versions after this. Fit again.

`SCHEMA_INVALID`: "the score request has no data"
:   The request has no data, no scenario dataset and no forecast horizon. The server refuses
    it before a run starts. Give a dataset, a scenario dataset or a horizon. The Score page
    shows **No new data** only for **Forecast**, or when a scenario dataset gives the values.

`SETTINGS_INVALID` before the fit starts
:   The settings of the model break a rule that uses more than one field. The harness reads
    the settings before it transforms or splits the data, so this error comes early. The
    message tells which settings to change. For example, `sci.storm_localisation` refuses a
    render of more than 50 million values.

## Workflows and science models <span class="mapi-badge">v0.9</span> { #workflows-and-science-models }

`EXPRESSION_ERROR` in a branch node
:   The condition could not be calculated. Usually, it compares a value that is null or not
    there, for example the R-hat of a fit with one chain. Wrap the reference in
    `coalesce(...)` or `exists(...)`, or fit two or more chains.

`PLOT_UNSAFE` on a workflow node
:   The `plot` output of the node did not pass the plot sanitizer, so the node failed. The
    server does a check of each plot output when it collects it, and again when it serves
    it. Look at the recipe and the settings of the `plot.render` node.

The tests of the science plugin skip
:   The tests that read the real data of pymc-challenges run only when
    `MAPI_PYMC_CHALLENGES_DATA` gives the `data/` directory of a local copy. Refer to
    [Science data](local-development.md#science-data).

`make docs-screenshots-science` stops at an upload (`ENOENT`)
:   The script reads `scotland_lip.nc`, `heat_plate_e51.nc` and `lazega_coworkers.nc` from
    `.local/science`, or from the folder that `MAPI_SCIENCE_DATA` gives. Export them first
    with `python -m mapi_plugin_science.export`.

The recipe monitor screenshot shows **Reused**
:   `make docs-screenshots-science` ran before on the same stack. The server used the earlier
    results of the recipe again. To capture a run in progress, use a new stack (a new
    `MAPI_DATA_DIR`).

## Docker and compose

`error: the Docker daemon is not reachable`
:   `make docker-build` and `make compose-up` first do a check with `docker info`. Start
    Docker Desktop, Colima or `dockerd`. You can also use `make dev`, which needs no Docker.

`required variable POSTGRES_PASSWORD is missing a value`
:   Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`. Compose reads `.env` from the
    repository root.

`image check failed: forbidden distribution 'numpy' is installed` (server build)
:   A control-plane dependency now adds a science package. To see the closure, run
    `uv run python scripts/lock_closure.py "mapi-server[docker]" --lacks numpy`. Then find
    the dependency that adds `numpy`.

`Contracts: 1 broken` in the `contracts` stage
:   Server code imports a forbidden module. `make lint` (`lint-imports`) shows the same
    failure locally, with the import chain.

`PyTensor found no C++ compiler` (base-bayes / base-forecast build)
:   `g++` is missing from `MAPI_APT_PACKAGES`. The PyMC images also need it at run time.

The `api` container is unhealthy
:   Run `make compose-logs SERVICE=api`. These are the usual causes:

    - `migrate` failed. Look at its logs.
    - The `DATABASE_URL` password does not agree with the database volume. This occurs after
      you change `POSTGRES_PASSWORD`. Then reset with `make compose-down VOLUMES=1`. This
      deletes the data.

`no bundle-signing key at /var/lib/mapi/keys/... and MAPI_SIGNING_KEY_CREATE is off`
:   `api` or `worker` started before `migrate` made the key, or without the `keys` volume.
    Do a check with `make compose-logs SERVICE=migrate`. Never point the two services at
    different keys. If you do, the API refuses each bundle that the worker signs, with
    `INTEGRITY_FAILURE`.

`400 Invalid host header` from the API, or nginx closes the connection
:   The stack answers only known host names (a DNS-rebinding guard). You can connect to it
    with a name other than `127.0.0.1`, `localhost` or `api`. Then add that name to
    `MAPI_ALLOWED_HOSTS` (the API) and `MAPI_WEB_SERVER_NAMES` (the nginx of the web image).

`mapi-entrypoint: /tmp/mapi-cache is not writable`
:   The container runs with a read-only root filesystem, but without a writable `/tmp`. Add
    `--tmpfs /tmp` (compose already does this).

Port already allocated on `docker compose up`
:   A `make dev` stack, or a different process, uses 4443, 8000, 5173 or 5174. Stop it with
    `make stop`. The two stacks use the same ports on purpose, so that the same URLs work
    with the two stacks.

nginx exits with `host not found` or `invalid number of arguments in "set"`
:   `MAPI_API_UPSTREAM` is empty or malformed. It must be a URL without a path, for example
    `http://api:8000`.
