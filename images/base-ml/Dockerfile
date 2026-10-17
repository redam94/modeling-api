# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# mapi/base-ml - the tabular ML runner image (plan §7 tier T0): scikit-learn and xgboost.
#
# Ships mapi-runner with the SDK, built-in steps, plot recipes and mapi-sklearn, whose
# `sklearn.*` plugins (point, quantile, bootstrap, conformal regressors and a classifier) are
# the tabular models. It deliberately contains NO PyMC/PyTensor (the build fails if they
# appear), which keeps it small and free of a C++ toolchain. The `tabular.*` plugin package is
# not included because its `tabular.hierarchical_glm` model is a PyMC model; it ships in
# base-bayes. Build from the repository root:
#
#   docker build -f images/base-ml/Dockerfile -t mapi/base-ml:0.5.0 .
#
# See docs/operations/docker.md.

# Base images, pinned by digest (resolved 2026-10-15; refresh with `make docker-pin`).
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.13@sha256:ea61e006cfec0e8d81fae901ad703e09d2c6cf1aa58abcb6507d124b50286f9f

# ------------------------------------------------------------------- image-specific settings
ARG MAPI_IMAGE_NAME=base-ml
ARG MAPI_DESCRIPTION="modeling-api runner: scikit-learn/xgboost models (sklearn.*), steps and plot recipes"
ARG MAPI_PACKAGES="mapi-runner mapi-steps mapi-sklearn"
# xgboost pulls a 300 MB NVIDIA NCCL wheel on Linux; it is only used for multi-GPU training.
ARG MAPI_NO_INSTALL="nvidia-nccl-cu13"
ARG MAPI_APT_PACKAGES="tini"
ARG MAPI_ASSERT_PRESENT="mapi-runner mapi-sdk mapi-spec mapi-steps mapi-plots mapi-workflows statsmodels scipy mapi-sklearn scikit-learn xgboost skops vl-convert-python"
ARG MAPI_ASSERT_ABSENT="pymc pytensor nutpie numba mapi-pymc mapi-plugin-mmm mapi-plugin-ts mapi-plugin-tabular mapi-server mapi-gcs-shim nvidia-nccl-cu13"

# ===================================== shared runner build: part 1 (identical in base-* images)
ARG VERSION=0.0.0-dev
ARG VCS_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS python-base
ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Only the workspace manifests: the dependency layer below is rebuilt when these change,
# not on every source edit (the RUN output is identical, so the COPY --from is cached).
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
ARG MAPI_NO_INSTALL
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    python3 /usr/local/lib/mapi/install_packages.py --deps-only ${MAPI_PACKAGES} \
      --no-install ${MAPI_NO_INSTALL}
COPY . /src
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    python3 /usr/local/lib/mapi/install_packages.py ${MAPI_PACKAGES} \
      --no-install ${MAPI_NO_INSTALL}

FROM python-base AS runtime-base
ARG MAPI_APT_PACKAGES
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ${MAPI_APT_PACKAGES} \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 mapi \
 && useradd --system --uid 10001 --gid mapi --home-dir /home/mapi --create-home \
      --shell /usr/sbin/nologin mapi
# HOME and every cache live under /tmp so the image runs with a read-only root filesystem
# (mount a tmpfs on /tmp). PYTENSOR_FLAGS, NUMBA_CACHE_DIR and HOME are on the runner's
# worker-environment allow-list (mapi_spec.run.WORKER_ENV_ALLOWLIST), so plugin code sees them.
ENV PATH=/opt/mapi/venv/bin:${PATH} \
    HOME=/tmp/mapi-home \
    MAPI_CACHE_SEED=/opt/mapi/cache \
    MAPI_CACHE_DIR=/tmp/mapi-cache \
    PYTENSOR_FLAGS=base_compiledir=/tmp/mapi-cache/pytensor \
    NUMBA_CACHE_DIR=/tmp/mapi-cache/numba \
    MPLCONFIGDIR=/tmp/mapi-cache/matplotlib \
    MPLBACKEND=Agg
COPY --chmod=0755 images/common/runner-entrypoint.sh /usr/local/bin/mapi-entrypoint
COPY --chmod=0644 images/common/healthcheck.py images/common/assert_dists.py \
     images/common/warm.py /opt/mapi/bin/
WORKDIR /home/mapi
# ============================================================ end of shared runner build: part 1

# ===================================== shared runner build: part 2 (identical in base-* images)
FROM runtime-base AS runtime
ARG MAPI_IMAGE_NAME
ARG MAPI_DESCRIPTION
ARG MAPI_ASSERT_PRESENT
ARG MAPI_ASSERT_ABSENT
ARG VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="mapi/${MAPI_IMAGE_NAME}" \
      org.opencontainers.image.description="${MAPI_DESCRIPTION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="AGPL-3.0-only OR LicenseRef-mapi-commercial" \
      org.opencontainers.image.vendor="modeling-api" \
      org.opencontainers.image.base.name="python:3.12-slim-trixie" \
      io.mapi.image.role="runner" \
      io.mapi.image.user="10001" \
      io.mapi.catalog="/opt/mapi/catalog.json"
COPY --from=builder /opt/mapi/venv /opt/mapi/venv
COPY --chmod=0644 LICENSE.md LICENSE-APACHE.txt LICENSE-AGPL.txt COMMERCIAL-LICENSE.md \
     THIRD-PARTY-NOTICES.md /usr/share/doc/mapi/
# Fail the build if the environment is not what this image promises, then export the static
# catalog (+ digest) and bake the PyTensor/Numba caches read-only into /opt/mapi/cache.
RUN python -I /opt/mapi/bin/assert_dists.py \
      --present ${MAPI_ASSERT_PRESENT} --absent ${MAPI_ASSERT_ABSENT} \
 && PYTENSOR_FLAGS=base_compiledir=/opt/mapi/cache/pytensor \
    NUMBA_CACHE_DIR=/opt/mapi/cache/numba HOME=/tmp/build-home \
    python -I /opt/mapi/bin/warm.py --out /opt/mapi --image "${MAPI_IMAGE_NAME}" \
 && rm -rf /tmp/build-home \
 && chmod -R a+rX,go-w /opt/mapi
USER 10001:10001
# A runner container is a batch job: this only confirms the runner still imports.
HEALTHCHECK --interval=5m --timeout=30s --start-period=1m --retries=2 \
  CMD ["python", "-I", "/opt/mapi/bin/healthcheck.py", "import", "mapi_runner"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mapi-entrypoint"]
CMD ["mapi-runner"]
# ============================================================ end of shared runner build: part 2
