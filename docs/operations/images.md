# Container images <span class="mapi-badge">v0.2</span>

The platform ships as six images (plus a `worker` target), built from
`images/<name>/Dockerfile` with the repository root as the build context. They exist to run
the exact production artifacts locally ([Docker Compose](docker.md)), to back the
docker executor, and to become the Cloud Run Jobs in [v0.3](../roadmap/cloud.md).

```bash
make docker-build                      # every image, tagged mapi/<name>:<version>
make docker-build IMAGES="server web"  # a subset
make docker-pin-check                  # every base image pinned by digest?
```

## The images

| Image | Contents | Runs as | Entry point |
|---|---|---|---|
| `mapi/base-bayes` | `mapi-runner`, SDK, steps, plot recipes, `mapi-pymc`, the `mmm.*` and `tabular.*` plugins, a C++ compiler for PyTensor | `10001` | `mapi-runner` |
| `mapi/base-ml` | `mapi-runner`, SDK, steps, plot recipes, `mapi-sklearn` (`sklearn.*` plugins). **No PyMC.** | `10001` | `mapi-runner` |
| `mapi/base-forecast` | `mapi-runner`, SDK, steps, plot recipes, `mapi-pymc`, the `ts.*` plugins | `10001` | `mapi-runner` |
| `mapi/worker` | `base-bayes` plus the `ts.*` plugins and `mapi-server` (target `worker` of the base-bayes Dockerfile); for compose only | `10001` | `mapi-server worker` |
| `mapi/server` | The control plane: `mapi-server`, `mapi-spec`, the JSON-only `mapi-plots` core, docker-py. **No science dependencies.** | `10001` | `mapi-server api` (or `worker`, `migrate`) |
| `mapi/gcs-shim` | The local Cloud Storage emulator | `10001` | `mapi-gcs-shim` |
| `mapi/web` | nginx serving `apps/web` (port 8080) and `apps/plot-sandbox` (port 8081) | `101` | nginx |

`make docker-build IMAGES="server web"` builds a subset. `make docker-build` builds them all,
plus `mapi/worker` whenever `base-bayes` is in the set. Every image is labelled with the OCI
`title`, `version`, `revision` (git commit) and `created` keys.

### How the Python images are built

Every Python image uses the same multi-stage pattern:

1. **`manifests`** copies the build context and deletes everything except `pyproject.toml` and
   `uv.lock` files. Its output changes only when dependencies change.
2. **`builder`** installs the third-party dependencies of the image's package set from those
   manifests alone (`uv sync --frozen --no-install-workspace`, once per package, into
   `/opt/mapi/venv`). BuildKit caches this layer, so editing source code does not reinstall
   PyMC. The builder then copies the sources and installs the workspace packages themselves,
   non-editable. Every version comes from `uv.lock`.
3. **`runtime`** starts again from the same `python:3.12-slim` base, adds `tini` (and `g++` for
   the PyMC images), creates user `10001`, and copies in only the virtual environment.
   The images that run `mapi-server` (`mapi/server` and `mapi/worker`) also copy `uv.lock` to
   `/opt/mapi/uv.lock` and set `MAPI_UV_LOCK_PATH` to it. Its SHA-256 is the *image digest*
   that the API and the orchestrator cache the step and model catalog under, the same value
   `make dev` computes. Both images are built from one lock, so `api` and `worker` agree.

The package set is the `MAPI_PACKAGES` build argument. `images/common/install_packages.py` turns
it into `uv sync` calls. The xgboost dependency `nvidia-nccl-cu13`, a 300 MB wheel used only for
multi-GPU training, is never installed (`MAPI_NO_INSTALL`).

### Build-time guards

An image that is not what it claims to be fails to build:

- **Package assertions.** `images/common/assert_dists.py` checks `MAPI_ASSERT_PRESENT` and
  `MAPI_ASSERT_ABSENT` against the real environment. For example, `base-ml` must not contain
  `pymc`, `pytensor` or `numba`, and `server` must not contain `numpy`, `xarray`, `pandas`,
  `scipy`, `pyarrow`, PyMC, `mapi_sdk`, `mapi_runner` or any plugin.
- **Import contracts in the control-plane image.** The `contracts` stage of
  `images/server/Dockerfile` installs import-linter (hash-pinned from `uv.lock`) into a
  throwaway copy of the environment, and runs the repository's contracts against the packages
  exactly as installed. The runtime stage depends on that stage, so BuildKit cannot skip it.
- **Catalog export.** The runner images load every installed step, model and plot plugin and
  write `/opt/mapi/catalog.json` with its digest (`/opt/mapi/catalog.digest`). If no steps or
  models are registered, the build fails.

The same claims are also proven without Docker: `tests/devops/test_container_invariants.py`
computes each image's dependency closure from `uv.lock` (`scripts/lock_closure.py`) and checks it
against the assertions.

### Compile caches and the read-only root filesystem

PyTensor compiles C at run time, and Numba caches machine code. Both caches are warmed during the
build by compiling a small hierarchical model, then baked read-only into `/opt/mapi/cache`.
PyTensor needs a *writable* compile directory, because it takes a lock and adds new modules. So
at start-up the entry point (`images/common/runner-entrypoint.sh`) builds a symlink farm of the
baked cache under `/tmp/mapi-cache`: real directories, with files that are symlinks to the
baked objects. This costs a few KiB of RAM, which matters on Cloud Run's in-memory filesystem,
and PyTensor accepts the relocated entries without recompiling.

`HOME`, `PYTENSOR_FLAGS`, `NUMBA_CACHE_DIR` and `MPLCONFIGDIR` all point under `/tmp`. The first
three are on the runner's worker-environment allow-list (`mapi_spec.run.WORKER_ENV_ALLOWLIST`),
so plugin code sees them after the supervisor scrubs the environment. The result is that every
image runs with `--read-only` and a tmpfs on `/tmp`.

`docker run mapi/base-bayes` runs the runner. Arguments starting with `-` are passed to the
runner, and any other command replaces it (`docker run mapi/base-bayes python -c ...`).

## The web image and the plot sandbox

`mapi/web` builds both Vite apps with pnpm, using `--frozen-lockfile` and `--ignore-scripts`, so no
package lifecycle scripts run. It serves them from one unprivileged nginx with two server
blocks. `NGINX_ENVSUBST_TEMPLATE_DIR` selects which blocks are rendered:
`/etc/nginx/templates/all` (the default), `web` or `plots`. Origins are runtime settings rendered
into the headers at start-up.

| | App (`:8080`, `MAPI_WEB_ORIGIN`) | Plot sandbox (`:8081`, `MAPI_PLOTS_ORIGIN`) |
|---|---|---|
| CSP | `default-src 'self'`; `script-src 'self'` (no `unsafe-eval`, no inline scripts); `frame-src` = the plots origin; `connect-src` = self, `MAPI_API_URL`, `MAPI_GCS_ENDPOINT`; `frame-ancestors 'none'` | `default-src 'none'`; scripts, styles and images from its own origin only; `connect-src` = its own `/topojson/` only; `frame-ancestors` = the app origin |
| Other headers | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, COOP and CORP `same-origin`, Trusted Types in report-only mode | `nosniff`, `no-referrer`, `Access-Control-Allow-Origin: *` |
| API access | `/v1/` is proxied to `MAPI_API_UPSTREAM`, unbuffered with a one-hour read timeout for SSE | None: no proxy, and the CSP blocks it |

The sandbox documents are framed with `sandbox="allow-scripts"` and no same-origin access, so
they have an **opaque origin**. Their module scripts, styles and topojson are therefore fetched in
CORS mode. That is why the sandbox answers with `Access-Control-Allow-Origin: *`; the assets are
static and public, and no cookies are involved. It is also why its CSP lists the plots origin
explicitly next to `'self'`.

`MAPI_WEB_SCRIPT_SRC_EXTRA` (empty by default) adds sources to the app's `script-src`. Use it
only after a review, for example if a library needs `'wasm-unsafe-eval'`. Trusted Types are in
report-only mode until the SPA ships its policy.

## Base images and pinning

Every base image is a build argument with a readable tag *and* a digest:

```dockerfile
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e3…11b1
```

The Dockerfile frontend (`# syntax=`) and the compose `postgres` image are pinned the same way.
`make docker-pin` re-resolves every tag through the registry API. It needs no Docker daemon, and
you should review the resulting diff like any dependency bump. `make docker-pin-check` (also run
in CI and by `make release-check`) fails if anything is unpinned.

## Licences in the images

Every image carries the project's licence files and `THIRD-PARTY-NOTICES.md` in
`/usr/share/doc/mapi/`. The notices list every third-party Python and JavaScript package
the image set ships, with its version and licence, and reproduce the NOTICE files and the
licence texts of the JavaScript packages compiled into the web app. Regenerate the file with
`make notices` whenever `uv.lock` or `pnpm-lock.yaml` changes; the test suite and
`make release-check` fail while it is stale. See [Licensing](../project/licensing.md).

## CI

`.github/workflows/ci.yml` builds every image, plus the `worker` target, on each push and pull
request, using the GitHub Actions layer cache. Images are never pushed. A separate job checks the
digest pins and validates both compose files with `docker compose config`.

With the images built and a Docker daemon running,
`uv run pytest tests/devops/test_compose_smoke.py -m docker` boots `postgres`, `gcs-shim`,
`migrate`, `api` and `worker` under a throw-away project name. It publishes no host ports. It
then checks that `api` and `worker` started and that they agree on the values bundle
verification depends on: the image digest and the bundle-signing key.

