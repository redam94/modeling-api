# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# mapi/<name> - a runner image with your own plugins on top of a platform image (v0.18 D14 (c),
# "custom image"). Trusted operators only: whatever the wheels contain runs in every sandboxed
# run of this image.
#
# The build context is a directory with your wheels and a hash-pinned requirements.txt, one
# `name==version --hash=sha256:<hex>` requirement per wheel (no sdists, no index):
#
#   wheels/
#     requirements.txt
#     acme_lab-0.1.0-py3-none-any.whl
#
#   docker build -f images/extend/Dockerfile \
#     --build-arg BASE=mapi/base-bayes:0.18.0 --build-arg MAPI_IMAGE_NAME=acme-bayes \
#     -t mapi/acme-bayes:0.1.0 wheels/
#
# (`make plugin-image BASE= NAME= WHEELS=` runs the same.) The build:
#   1. installs exactly the pinned wheels with `uv pip install --no-deps --no-index --offline
#      --require-hashes --no-build` (no network, no dependency resolution, no setup.py);
#   2. fails if anything already in the base environment changed (a wheel may only add);
#   3. checks the added distributions are installed (images/common/assert_dists.py);
#   4. re-exports the catalog with warm.py --fail-on-diagnostics (a plugin that does not load
#      fails the build instead of disappearing from the catalog) and re-warms the caches;
#   5. keeps requirements.txt as /opt/mapi/extensions.lock.
#
# The control plane keys the catalog by an image digest it derives itself, never a literal:
# mount the same extensions.lock into the api and worker containers and point
# MAPI_EXTENSIONS_LOCK_PATH at it (images/extend/README.md). `describe` runs in the default
# runner image only, so set this image as MAPI_DOCKER_IMAGE (docker executor) or as the
# default base (Cloud Run) as well as in MAPI_DOCKER_IMAGES[<namespace>].

# The uv binary (build-time only; pinned like the platform images, refresh with `make docker-pin`).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.13@sha256:ea61e006cfec0e8d81fae901ad703e09d2c6cf1aa58abcb6507d124b50286f9f
# The platform image to extend (required, no default): a runner image (mapi/base-bayes,
# mapi/base-ml, mapi/base-forecast, mapi/base-dl) or the compose worker image, v0.18 or later.
# Pin it by digest in production: mapi/base-bayes:0.18.0@sha256:<hex>.
ARG BASE

FROM ${UV_IMAGE} AS uv

FROM ${BASE} AS runtime
ARG BASE
ARG MAPI_IMAGE_NAME=extend
ARG MAPI_DESCRIPTION="modeling-api runner with operator-installed extensions"
ARG MAPI_LICENSES="AGPL-3.0-only OR LicenseRef-mapi-commercial"
ARG VERSION=0.0.0-dev
ARG VCS_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
LABEL org.opencontainers.image.title="mapi/${MAPI_IMAGE_NAME}" \
      org.opencontainers.image.description="${MAPI_DESCRIPTION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="${MAPI_LICENSES}" \
      org.opencontainers.image.vendor="modeling-api" \
      org.opencontainers.image.base.name="${BASE}" \
      io.mapi.image.user="10001" \
      io.mapi.catalog="/opt/mapi/catalog.json" \
      io.mapi.extensions.lock="/opt/mapi/extensions.lock"
USER 0:0
# The context is mounted read-only for this one step; uv is bind-mounted, not installed.
RUN --mount=type=bind,source=.,target=/ext,readonly \
    --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    set -eu \
 && export LC_ALL=C \
 && test -f /ext/requirements.txt \
 && uv pip freeze --python /opt/mapi/venv/bin/python | sort > /tmp/before.txt \
 && UV_NO_CACHE=1 uv pip install --python /opt/mapi/venv/bin/python \
      --no-deps --no-index --offline --require-hashes --no-build \
      --find-links /ext -r /ext/requirements.txt \
 && uv pip freeze --python /opt/mapi/venv/bin/python | sort > /tmp/after.txt \
 && changed="$(comm -23 /tmp/before.txt /tmp/after.txt)" \
 && if [ -n "${changed}" ]; then \
      echo "extend: the wheels replace packages of the base image:" >&2; \
      echo "${changed}" >&2; exit 1; \
    fi \
 && added="$(comm -13 /tmp/before.txt /tmp/after.txt | sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p')" \
 && if [ -z "${added}" ]; then echo "extend: requirements.txt adds nothing" >&2; exit 1; fi \
 && python -I /opt/mapi/bin/assert_dists.py --present ${added} \
 && install -m 0644 /ext/requirements.txt /opt/mapi/extensions.lock \
 && rm -f /tmp/before.txt /tmp/after.txt
# Export the catalog again (now with the extensions) and re-warm the baked caches.
RUN PYTENSOR_FLAGS=base_compiledir=/opt/mapi/cache/pytensor \
    NUMBA_CACHE_DIR=/opt/mapi/cache/numba HOME=/tmp/build-home \
    python -I /opt/mapi/bin/warm.py --out /opt/mapi --image "${MAPI_IMAGE_NAME}" \
      --fail-on-diagnostics \
 && rm -rf /tmp/build-home \
 && chmod -R a+rX,go-w /opt/mapi
ENV MAPI_EXTENSIONS_LOCK_PATH=/opt/mapi/extensions.lock
USER 10001:10001
# ENTRYPOINT, CMD and HEALTHCHECK are the base image's (the runner, or the compose worker).
