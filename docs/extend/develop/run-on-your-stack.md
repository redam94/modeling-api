---
description: Install your own modeling-api plugin on a stack you run - in the platform checkout, as an editable out-of-tree package on a dev stack, or in a custom runner image - and get the catalog described again.
---

# Run your plugin on your stack

There are three ways to put a plugin where the runner finds it. All three are for a trusted
operator: the plugin runs inside every sandboxed run of that stack, with the rights of the
built-in plugins.

| Way | Stack | Command | When |
|---|---|---|---|
| (a) In the platform checkout | `make dev` | `make plugin-new NAME=acme` | Your plugin lives with the platform code |
| (b) Out of tree, editable | `make dev` | `make plugin-dev PLUGIN=../acme` | You develop the plugin in its own repository |
| (c) A custom runner image | Docker Compose, Cloud Run | `make plugin-image BASE=base-bayes NAME=acme WHEELS=...` | A deployment that runs images |

!!! tip "Not the operator? Upload it instead"
    From v0.19 you don't need to run a stack. A tenant admin, or an editor that a tenant admin
    confirmed, can push a wheel to a shared deployment with `mapi plugin push` (or
    `make plugin-push PLUGIN=../acme`). The platform checks it in the sandbox, you use it
    privately, and a second admin approves it for the team. See
    [Upload your own code](../upload.md).

## How the catalog finds your plugin

The control plane never imports plugin code. A **describe** run in the runner loads every
entry point and exports the catalog: models, steps, recipes, workflow nodes, workflow
templates and the diagnostics. The control plane keys that catalog by an **image digest**
and caches it. A new digest means a new describe run, which starts by itself when the server
starts.

So each way below makes sure that installing or changing your plugin changes the digest.
Then restart the stack.

## (a) In the platform checkout

```bash
make plugin-new NAME=acme KIND="model recipe node"
make install
make dev
```

`make plugin-new` runs `mapi plugin init` with `--dir plugins` (`KIND` takes one or more of
`model`, `step`, `recipe`, `node`, `template`; `BASE` picks the base class of a model).
`plugins/*` is a member of the uv workspace, so `make install` (`uv sync --all-packages`)
adds it to the lock file and installs it into `.venv`, which is what makes its entry points
exist. (`uv lock` alone is not enough: `uv run` and `make dev` only install the packages the
workspace root depends on, and a new member is not one of them.) The digest of a source checkout hashes `uv.lock` and every file under `packages/*/src` and
`plugins/*/src`, so each change to your code describes the catalog again at the next start.

## (b) Out of tree, while you develop it

```bash
make plugin-dev PLUGIN=../acme
make dev                          # restart: the digest changed, describe runs again
make plugin-doctor                # is it installed, loaded and in the digest?
```

`make plugin-dev`:

1. installs the package in editable mode into `.venv` (`uv pip install --no-deps -e`). Add
   `DEPS=1` when your plugin needs a package that the platform does not have;
2. adds the folder to `.local/extra-plugins.txt`. The digest of a source checkout hashes the
   `src/` of each folder in that list, so every edit to your plugin describes the catalog
   again at the next start of `make dev`.

`make plugin-undev PLUGIN=../acme` uninstalls the package and removes it from the list.

!!! warning "`make install` removes editable plugins"
    `make install` runs `uv sync`, which removes packages that are not in the lock file,
    such as your editable plugin. Run `make plugin-dev PLUGIN=../acme` again after it.
    (`uv run` and `make dev` keep it.)

## (c) A custom runner image

Use this for the Docker and Cloud Run executors. `images/extend/Dockerfile` adds your wheels
to a platform runner image:

```bash
cd ../acme && uv build --wheel && cd -
make plugin-image BASE=base-bayes NAME=acme WHEELS="../acme/dist/*.whl" TAG=0.1.0
```

- `BASE` is a platform runner image: `base-bayes` means `mapi/base-bayes:<platform
  version>`; a name with `/` or `:` is used as it is. Pin it by digest in production.
- `WHEELS` is one or more wheels, or a directory with a hash-pinned `requirements.txt`. For
  wheels, the target writes that file for you (`name==version --hash=sha256:...`, one line
  for each wheel) under `.local/plugin-image/<NAME>/`.
- The build installs exactly those wheels with no network and no dependency resolution
  (`--no-deps --no-index --require-hashes`), fails when a wheel changes a package of the base
  image, and exports the catalog with `--fail-on-diagnostics`: a plugin that does not load
  fails the build instead of disappearing from the catalog.
- The image keeps `requirements.txt` as `/opt/mapi/extensions.lock`.

Then tell the control plane, in this order:

1. **The digest.** Copy the lock out of the image and mount it read-only into the `api` and
   `worker` containers, with `MAPI_EXTENSIONS_LOCK_PATH` set to its path in both. The digest
   folds the lock in, so a new image gives a new catalog.

    ```bash
    docker run --rm --entrypoint cat mapi/acme:0.1.0 /opt/mapi/extensions.lock > .local/extensions.lock
    ```

2. **The routing.** The describe run uses the default runner image, so your image must be
   the default as well as the image of your namespace:
    - Docker executor: `MAPI_DOCKER_IMAGE=mapi/acme:0.1.0` and
      `MAPI_DOCKER_IMAGES='{"acme": "mapi/acme:0.1.0"}'`;
    - Cloud Run: the same image as `MAPI_CLOUDRUN_DEFAULT_BASE` and in
      `MAPI_CLOUDRUN_PLUGIN_BASES`.

    A plugin that is only in the image of its namespace is not in the catalog.

3. **Restart** the `api` and `worker`.

More detail, including how the build checks the wheels: `images/extend/README.md`.

## Describe again now

A new or changed plugin changes the digest, and the next start describes the catalog by
itself. If the describe run for the current digest failed (and you fixed the cause without a
new digest), an operator can ask for it again:

```bash
make catalog-refresh              # POST /v1/admin/catalog:describe on MAPI_URL
```

`make catalog-refresh` signs in with `MAPI_TOKEN`, or, on a dev stack, as the dev IdP's
operator `admin@dev.local`. When a describe run for the digest is queued, running or already
succeeded, the server returns that run and queues nothing. Operators also find **Describe the
catalog again** in Catalog › Yours › Problems.

## Check it

```bash
mapi plugin doctor --server       # or: make plugin-doctor SERVER=1
```

`--server` compares your local catalog with the server's: if the server still serves the
catalog of an older digest, you see it here. Then open **Catalog › Yours** in the app. Your
entries show "Installed by your team"; a tenant admin sees anything that did not load under
**Problems**, with the reason.
