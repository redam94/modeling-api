# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# mapi/server - the control plane (plan §8.1): `mapi-server api | worker | migrate`.
#
# Control plane ONLY: mapi-server, mapi-spec and the JSON-only mapi-plots core. It must never
# contain array libraries or plugin/runtime code (plan §12: "the API image contains no science
# dependencies"). Two independent checks fail the build otherwise:
#   * the `contracts` stage runs import-linter (the root pyproject's contracts) against the
#     packages exactly as installed in this image;
#   * the final stage asserts numpy, xarray, pandas, PyMC, mapi_sdk, mapi_runner, ... are
#     neither installed nor importable.
# The `docker` extra (docker-py) is included so `mapi-server worker` can drive the docker
# executor through a mounted socket. Build from the repository root:
#
#   docker build -f images/server/Dockerfile -t mapi/server:0.8.0 .
#
# See docs/operations/docker.md.

# Base images, pinned by digest (resolved 2026-10-15; refresh with `make docker-pin`).
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.13@sha256:ea61e006cfec0e8d81fae901ad703e09d2c6cf1aa58abcb6507d124b50286f9f

ARG MAPI_PACKAGES="mapi-server[docker]"
ARG MAPI_ASSERT_PRESENT="mapi-server mapi-spec mapi-plots mapi-workflows jsonschema fastapi uvicorn sqlalchemy alembic psycopg procrastinate docker"
ARG MAPI_ASSERT_ABSENT="numpy xarray pandas scipy pyarrow zarr pymc pytensor nutpie numba scikit-learn xgboost altair plotly mapi-sdk mapi-runner mapi-steps mapi-pymc mapi-sklearn mapi-plugin-mmm mapi-plugin-ts mapi-plugin-tabular mapi-plugin-statsmodels mapi-plugin-science statsmodels mapi-conformance"
ARG VERSION=0.0.0-dev
ARG VCS_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS python-base
ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM python-base AS manifests
COPY . /src
RUN find /src -type f ! -name pyproject.toml ! -name uv.lock -delete \
 && find /src -depth -type d -empty -delete

FROM python-base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/mapi/venv
WORKDIR /src
COPY images/common/install_packages.py /usr/local/lib/mapi/install_packages.py
COPY --from=manifests /src /src
ARG MAPI_PACKAGES
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    python3 /usr/local/lib/mapi/install_packages.py --deps-only ${MAPI_PACKAGES}
COPY . /src
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    python3 /usr/local/lib/mapi/install_packages.py ${MAPI_PACKAGES}

# Import contracts, checked against the installed control plane. import-linter (and its
# dependencies, hash-pinned from uv.lock) is added to a throwaway copy of the environment;
# the runtime stage copies the venv from `builder`, so none of it ships.
FROM builder AS contracts
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    python3 /src/scripts/lock_closure.py import-linter --requirements > /tmp/import-linter.txt \
 && uv pip install --python /opt/mapi/venv/bin/python --require-hashes --no-deps \
      -r /tmp/import-linter.txt \
 && /opt/mapi/venv/bin/lint-imports --config /src/pyproject.toml \
 && echo "import contracts kept" > /tmp/import-contracts.ok

FROM python-base AS runtime
ARG MAPI_ASSERT_PRESENT
ARG MAPI_ASSERT_ABSENT
ARG VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="mapi/server" \
      org.opencontainers.image.description="modeling-api control plane: API, orchestrator worker and migrations (no science dependencies)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="AGPL-3.0-only OR LicenseRef-mapi-commercial" \
      org.opencontainers.image.vendor="modeling-api" \
      org.opencontainers.image.base.name="python:3.12-slim-trixie" \
      io.mapi.image.role="control-plane" \
      io.mapi.image.user="10001"
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 mapi \
 && useradd --system --uid 10001 --gid mapi --home-dir /home/mapi --create-home \
      --shell /usr/sbin/nologin mapi \
 && install -d -o 10001 -g 10001 -m 0700 /var/lib/mapi/keys /var/lib/mapi/outbox
# /var/lib/mapi/keys: mount point of the compose `keys` volume (bundle-signing key). A new
# named volume copies this directory's owner and mode, so uid 10001 can create the key.
# /var/lib/mapi/outbox: the same for the `outbox` volume (the dev profile's e-mail outbox).
# MAPI_UV_LOCK_PATH: the image digest `api` and `worker` key the catalog by is the sha256 of
# the uv.lock the image was built from (Settings.resolve_image_digest), exactly as under
# `make dev`; without it neither command can start (no uv.lock above the WORKDIR).
ENV PATH=/opt/mapi/venv/bin:${PATH} \
    HOME=/tmp/mapi-home \
    MAPI_UV_LOCK_PATH=/opt/mapi/uv.lock
COPY --from=builder /opt/mapi/venv /opt/mapi/venv
COPY --chmod=0644 LICENSE.md LICENSE-APACHE.txt LICENSE-AGPL.txt COMMERCIAL-LICENSE.md \
     THIRD-PARTY-NOTICES.md /usr/share/doc/mapi/
COPY --chmod=0644 images/common/healthcheck.py images/common/assert_dists.py /opt/mapi/bin/
COPY --chmod=0644 uv.lock /opt/mapi/uv.lock
# Referencing the contracts stage makes BuildKit run it; the marker itself is informational.
COPY --from=contracts /tmp/import-contracts.ok /opt/mapi/import-contracts.ok
RUN python -I /opt/mapi/bin/assert_dists.py \
      --present ${MAPI_ASSERT_PRESENT} --absent ${MAPI_ASSERT_ABSENT}
USER 10001:10001
WORKDIR /home/mapi
EXPOSE 8000
# Probes the API process (the default command). Compose overrides it for `worker`/`migrate`.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-I", "/opt/mapi/bin/healthcheck.py", "http", "http://127.0.0.1:8000/healthz"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "mapi-server"]
CMD ["api", "--host", "0.0.0.0", "--port", "8000"]
