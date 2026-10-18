# Container images <span class="mapi-badge">v0.2</span>

The platform has six images (and a `worker` target). The build makes them from
`images/<name>/Dockerfile`, with the repository root as the build context. The images have
three purposes:

- To run the exact production artifacts locally ([Docker Compose](docker.md)).
- To support the docker executor.
- To become the Cloud Run Jobs in [v0.3](../roadmap/cloud.md).

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
| `mapi/worker` | `base-bayes` with the `ts.*` plugins and `mapi-server` (target `worker` of the base-bayes Dockerfile). Only for compose | `10001` | `mapi-server worker` |
| `mapi/server` | The control plane: `mapi-server`, `mapi-spec`, the JSON-only `mapi-plots` core, docker-py. **No science dependencies.** | `10001` | `mapi-server api` (or `worker`, `migrate`) |
| `mapi/gcs-shim` | The local Cloud Storage emulator | `10001` | `mapi-gcs-shim` |
| `mapi/web` | nginx that serves `apps/web` (port 8080) and `apps/plot-sandbox` (port 8081) | `101` | nginx |

`make docker-build IMAGES="server web"` builds a subset. `make docker-build` builds all
images. It also builds `mapi/worker` when `base-bayes` is in the set. Each image has the OCI
labels `title`, `version`, `revision` (git commit) and `created`.

### How the Python images are built

Each Python image uses the same multi-stage pattern:

1. **`manifests`** copies the build context. It deletes all files except the
   `pyproject.toml` and `uv.lock` files. Thus its output changes only when dependencies
   change.
2. **`builder`** installs the third-party dependencies of the package set of the image. It
   uses only those manifests (`uv sync --frozen --no-install-workspace`, one time for each
   package, into `/opt/mapi/venv`). BuildKit keeps this layer in a cache. Thus, when you edit
   source code, the build does not install PyMC again. The builder then copies the sources
   and installs the workspace packages, non-editable. Each version comes from `uv.lock`.
3. **`runtime`** starts again from the same `python:3.12-slim` base. It adds `tini` (and
   `g++` for the PyMC images) and makes user `10001`. Then it copies in only the virtual
   environment.

    The images that run `mapi-server` (`mapi/server` and `mapi/worker`) also copy `uv.lock`
    to `/opt/mapi/uv.lock` and set `MAPI_UV_LOCK_PATH` to it. Its SHA-256 is the *image
    digest*. The API and the orchestrator use this digest as the cache key of the step and
    model catalog. `make dev` calculates the same value. The build makes the two images from
    one lock, so `api` and `worker` agree.

The package set is the `MAPI_PACKAGES` build argument. `images/common/install_packages.py`
changes it into `uv sync` calls. The build never installs the xgboost dependency
`nvidia-nccl-cu13` (`MAPI_NO_INSTALL`). This is a 300 MB wheel that only multi-GPU training
uses.

### Build-time guards

If an image is not what it claims to be, the build fails:

- **Package assertions.** `images/common/assert_dists.py` does a check of
  `MAPI_ASSERT_PRESENT` and `MAPI_ASSERT_ABSENT` against the real environment. For example,
  `base-ml` must not contain `pymc`, `pytensor` or `numba`. `server` must not contain
  `numpy`, `xarray`, `pandas`, `scipy`, `pyarrow`, PyMC, `mapi_sdk`, `mapi_runner` or a
  plugin.
- **Import contracts in the control-plane image.** The `contracts` stage of
  `images/server/Dockerfile` installs import-linter (hash-pinned from `uv.lock`) into a
  temporary copy of the environment. It runs the contracts of the repository against the
  packages exactly as they are installed. The runtime stage depends on that stage, so
  BuildKit cannot skip it.
- **Catalog export.** The runner images load each installed step, model and plot plugin. They
  write `/opt/mapi/catalog.json` with its digest (`/opt/mapi/catalog.digest`). If no steps or
  models are registered, the build fails.

We also prove the same claims without Docker. `tests/devops/test_container_invariants.py`
calculates the dependency closure of each image from `uv.lock` (`scripts/lock_closure.py`).
Then it does a check of the closure against the assertions.

### Compile caches and the read-only root filesystem

PyTensor compiles C at run time, and Numba keeps machine code in a cache. During the build, a
small hierarchical model compiles and fills the two caches. The build then puts the caches
read-only into `/opt/mapi/cache`. PyTensor needs a *writable* compile directory, because it
takes a lock and adds new modules.

Thus, at start-up, the entry point (`images/common/runner-entrypoint.sh`) builds a symlink
farm of the cache under `/tmp/mapi-cache`. The farm has real directories, with files that are
symlinks to the objects in the cache. This uses some KiB of RAM. This is important on the
in-memory filesystem of Cloud Run. PyTensor accepts the moved entries and does not compile
them again.

`HOME`, `PYTENSOR_FLAGS`, `NUMBA_CACHE_DIR` and `MPLCONFIGDIR` all point under `/tmp`. The
first three are on the worker-environment allow-list of the runner
(`mapi_spec.run.WORKER_ENV_ALLOWLIST`). Thus plugin code sees them after the supervisor
removes the other variables from the environment. As a result, each image runs with
`--read-only` and a tmpfs on `/tmp`.

`docker run mapi/base-bayes` runs the runner. The image gives arguments that start with `-`
to the runner. All other commands replace the runner
(`docker run mapi/base-bayes python -c ...`).

## The web image and the plot sandbox

`mapi/web` builds the two Vite apps with pnpm. It uses `--frozen-lockfile` and
`--ignore-scripts`, so no package lifecycle scripts run. One unprivileged nginx with two
server blocks serves the apps. `NGINX_ENVSUBST_TEMPLATE_DIR` selects which blocks nginx
uses: `/etc/nginx/templates/all` (the default), `web` or `plots`. Origins are runtime
settings. At start-up, nginx puts them into the headers.

| | App (`:8080`, `MAPI_WEB_ORIGIN`) | Plot sandbox (`:8081`, `MAPI_PLOTS_ORIGIN`) |
|---|---|---|
| CSP | `default-src 'self'`; `script-src 'self'` (no `unsafe-eval`, no inline scripts); `frame-src` = the plots origin; `connect-src` = self, `MAPI_API_URL`, `MAPI_GCS_ENDPOINT`; `frame-ancestors 'none'` | `default-src 'none'`; scripts, styles and images only from its own origin; `connect-src` = only its own `/topojson/`; `frame-ancestors` = the app origin |
| Other headers | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, COOP and CORP `same-origin`, Trusted Types in report-only mode | `nosniff`, `no-referrer`, `Access-Control-Allow-Origin: *` |
| API access | nginx sends `/v1/` to `MAPI_API_UPSTREAM`, unbuffered, with a read timeout of one hour for SSE | None: no proxy, and the CSP blocks it |

The app frames the sandbox documents with `sandbox="allow-scripts"` and no same-origin
access. Thus they have an **opaque origin**, and the browser gets their module scripts,
styles and topojson in CORS mode. For this reason, the sandbox answers with
`Access-Control-Allow-Origin: *`. The assets are static and public, and there are no cookies.
For the same reason, its CSP lists the plots origin explicitly next to `'self'`.

`MAPI_WEB_SCRIPT_SRC_EXTRA` (empty by default) adds sources to the `script-src` of the app.
Use it only after a review, for example if a library needs `'wasm-unsafe-eval'`. Trusted
Types stay in report-only mode until the SPA has its policy.

## Base images and pinning

Each base image is a build argument with a readable tag *and* a digest:

```dockerfile
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e3…11b1
```

The Dockerfile frontend (`# syntax=`) and the compose `postgres` image are pinned in the same
way. `make docker-pin` resolves each tag again through the registry API. It needs no Docker
daemon. Examine the diff that it makes as you examine each dependency update.
`make docker-pin-check` fails if an image is not pinned. CI and `make release-check` also run
it.

## Licences in the images

Each image has the licence files of the project and `THIRD-PARTY-NOTICES.md` in
`/usr/share/doc/mapi/`. The notices list each third-party Python and JavaScript package in
the image set, with its version and licence. They also contain the NOTICE files, and the
licence texts of the JavaScript packages that the build compiles into the web app. When
`uv.lock` or `pnpm-lock.yaml` changes, run `make notices` to make the file again. While the
file is not current, the test suite and `make release-check` fail. Refer to
[Licensing](../project/licensing.md).

## CI

`.github/workflows/ci.yml` builds each image, and the `worker` target, on each push and pull
request. It uses the GitHub Actions layer cache. CI never pushes the images. A separate job
does a check of the digest pins. It also validates the two compose files with
`docker compose config`.

You can do a smoke test when you have built the images and a Docker daemon runs. Then
`uv run pytest tests/devops/test_compose_smoke.py -m docker` starts `postgres`, `gcs-shim`,
`migrate`, `api` and `worker` under a temporary project name. It publishes no host ports.
Then it makes sure that `api` and `worker` started. It also makes sure that they agree on the
values that bundle verification needs: the image digest and the bundle-signing key.
