# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# mapi/gcs-shim - the local Google Cloud Storage emulator (packages/gcs-shim), used by
# docker-compose in place of real GCS. Objects live under /data (mount a volume there).
# Build from the repository root:
#
#   docker build -f images/gcs-shim/Dockerfile -t mapi/gcs-shim:0.4.0 .
#
# Local/test use only: it is a development stand-in, never a production object store.

# Base images, pinned by digest (resolved 2026-10-15; refresh with `make docker-pin`).
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.13@sha256:ea61e006cfec0e8d81fae901ad703e09d2c6cf1aa58abcb6507d124b50286f9f

ARG MAPI_PACKAGES="mapi-gcs-shim"
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

FROM python-base AS runtime
ARG VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="mapi/gcs-shim" \
      org.opencontainers.image.description="Local Google Cloud Storage emulator for modeling-api development and tests" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="modeling-api" \
      org.opencontainers.image.base.name="python:3.12-slim-trixie" \
      io.mapi.image.role="gcs-shim" \
      io.mapi.image.user="10001"
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 mapi \
 && useradd --system --uid 10001 --gid mapi --home-dir /home/mapi --create-home \
      --shell /usr/sbin/nologin mapi \
 && install -d -o 10001 -g 10001 -m 0750 /data
ENV PATH=/opt/mapi/venv/bin:${PATH} \
    HOME=/tmp/mapi-home
COPY --from=builder /opt/mapi/venv /opt/mapi/venv
COPY --chmod=0644 LICENSE.md LICENSE-APACHE.txt LICENSE-AGPL.txt COMMERCIAL-LICENSE.md \
     THIRD-PARTY-NOTICES.md /usr/share/doc/mapi/
COPY --chmod=0644 images/common/healthcheck.py images/common/assert_dists.py /opt/mapi/bin/
RUN python -I /opt/mapi/bin/assert_dists.py \
      --present mapi-gcs-shim google-crc32c starlette uvicorn \
      --absent numpy xarray pandas mapi-server mapi-sdk mapi-runner
USER 10001:10001
WORKDIR /home/mapi
VOLUME ["/data"]
EXPOSE 4443
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-I", "/opt/mapi/bin/healthcheck.py", "tcp", "127.0.0.1:4443"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "mapi-gcs-shim"]
# State (config, principal key files, objects) lives under /data/gcs; the control plane reads
# the key files from the same volume (mounted read-only). CORS defaults to $MAPI_WEB_ORIGIN.
CMD ["serve", "--host", "0.0.0.0", "--port", "4443", "--root", "/data/gcs"]
