# images/extend: a runner image with your own plugins

`images/extend/Dockerfile` adds your plugin wheels to a platform runner image. It is path (c) of
"run your plugin on your stack" (`docs/guides/extend/`): use it when your models, steps, plot
recipes, workflow nodes or workflow templates must run under the docker or Cloud Run executor.
For a source checkout (`make dev`), `make plugin-dev PLUGIN=../acme` is simpler.

Only a trusted operator builds this image. The wheels run inside every sandboxed run of the
image, with the same rights as the built-in plugins. Nothing here accepts code from tenants.

## 1. Collect the wheels

Build each plugin as a wheel and write a `requirements.txt` that pins every wheel by version and
hash. One requirement per wheel; no source distributions, no index:

```bash
mkdir -p wheels
uv build --wheel --out-dir wheels ../acme-lab
cd wheels
for w in *.whl; do
  name=$(echo "$w" | cut -d- -f1 | tr _ -)
  version=$(echo "$w" | cut -d- -f2)
  echo "$name==$version --hash=sha256:$(shasum -a 256 "$w" | cut -d' ' -f1)"
done > requirements.txt
```

`--no-deps` is used: the wheels may only need what the base image already has. A plugin that
needs another third-party package must vendor it as one more pinned wheel.

## 2. Build

```bash
docker build -f images/extend/Dockerfile \
  --build-arg BASE=mapi/base-bayes:0.18.0 \
  --build-arg MAPI_IMAGE_NAME=acme-bayes \
  --build-arg VERSION=0.1.0 \
  -t mapi/acme-bayes:0.1.0 wheels/
```

or `make plugin-image BASE=mapi/base-bayes:0.18.0 NAME=acme-bayes WHEELS=wheels/`.

`BASE` has no default. Pin it by digest in production (`mapi/base-bayes:0.18.0@sha256:…`).
The build fails when:

- a requirement has no hash, or a wheel does not match its hash;
- a wheel replaces or changes a package of the base image (it may only add);
- a plugin does not load, or loads with a catalog diagnostic (reserved namespace, id
  collision, unsafe settings schema, incomplete About card, invalid node contract or
  template). Run `mapi plugin validate ../acme-lab` to see the same checks before you build.

The build has no network access to package indexes (`--no-index --offline`), and the image
does not need any at run time.

## 3. Tell the control plane

The catalog is keyed by an image digest that the control plane derives itself (never a literal
`MAPI_IMAGE_DIGEST`). The image keeps your `requirements.txt` as `/opt/mapi/extensions.lock`;
the api and the worker fold the same file into the digest:

```bash
docker run --rm --entrypoint cat mapi/acme-bayes:0.1.0 /opt/mapi/extensions.lock \
  > .local/extensions.lock
```

Mount `.local/extensions.lock` read-only into the `api` and `worker` containers and set
`MAPI_EXTENSIONS_LOCK_PATH` to its path in both (a compose override does this). A new lock
changes the digest, so the next restart describes the image again; `make catalog-refresh`
(operator-only `POST /v1/admin/catalog:describe`) asks for it at once.

## 4. Route runs to the image

`describe` runs in the default runner image only, so the extension image must be the default as
well as the image of its namespace:

- docker executor: `MAPI_DOCKER_IMAGE=mapi/acme-bayes:0.1.0` and
  `MAPI_DOCKER_IMAGES={"acme": "mapi/acme-bayes:0.1.0"}`;
- Cloud Run: the same image as the default base and for the namespace.

A plugin whose image is routed by namespace only is not in the catalog.

## 5. See it in the app

Open **Catalog**. Your entries show "Installed by your team"; tenant admins see any load
problems under **Catalog → Yours → Diagnostics** (`GET /v1/catalog/diagnostics`, and
`mapi plugin doctor --server`).
