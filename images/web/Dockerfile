# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# mapi/web - static hosting for the SPA (apps/web) and the plot sandbox (apps/plot-sandbox).
#
# One nginx (unprivileged, uid 101) with two server blocks on two ports, because the plot
# sandbox MUST be a different origin from the app (plan §10.3):
#
#   :8080  app      apps/web/dist; proxies /v1/ to the API (MAPI_API_UPSTREAM, SSE-safe);
#                   CSP default-src 'self', frame-src = the plots origin, frame-ancestors 'none'
#   :8081  plots    apps/plot-sandbox/dist; CSP default-src 'none', scripts/styles/images
#                   from its own origin only, frame-ancestors = the app origin
#
# NGINX_ENVSUBST_TEMPLATE_DIR picks the server blocks: /etc/nginx/templates/{all,web,plots}.
# Origins are runtime settings (MAPI_WEB_ORIGIN, MAPI_PLOTS_ORIGIN, MAPI_API_URL,
# MAPI_GCS_ENDPOINT) rendered into the CSP at container start. Read-only root filesystem
# friendly: everything nginx writes goes to /tmp. Build from the repository root:
#
#   docker build -f images/web/Dockerfile -t mapi/web:0.15.0 .
#
# See docs/operations/docker.md.

# Base images, pinned by digest (resolved 2026-10-15; refresh with `make docker-pin`).
ARG NODE_IMAGE=node:22-trixie-slim@sha256:154ba2f4d6fec323d28e4f4bb86bba4677f1223391a1979cf521304e03a98dfa
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.30-alpine@sha256:782bb89768cc87b7f95c18b95baa1af250e5d451f26794f59363540161147c01

ARG PNPM_VERSION=11.9.0
ARG VERSION=0.0.0-dev
ARG VCS_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z

FROM ${NODE_IMAGE} AS build
ARG PNPM_VERSION
ENV PNPM_HOME=/pnpm \
    CI=true \
    NODE_ENV=production
ENV PATH=${PNPM_HOME}:${PATH}
RUN corepack enable && corepack prepare "pnpm@${PNPM_VERSION}" --activate
WORKDIR /src
# Fetch every package in the lockfile into the store first (cached until the lockfile
# changes), then install offline. Lifecycle scripts never run (plan §9 supply chain).
RUN --mount=type=bind,source=pnpm-lock.yaml,target=pnpm-lock.yaml \
    --mount=type=bind,source=pnpm-workspace.yaml,target=pnpm-workspace.yaml \
    --mount=type=cache,target=/pnpm/store,sharing=locked \
    pnpm config set store-dir /pnpm/store \
 && pnpm fetch --frozen-lockfile --ignore-scripts
COPY . /src
RUN --mount=type=cache,target=/pnpm/store,sharing=locked \
    pnpm config set store-dir /pnpm/store \
 && pnpm install --offline --frozen-lockfile --ignore-scripts --prod=false \
 && pnpm --filter @mapi/web build \
 && VITE_API_URL=__MAPI_API_URL__ pnpm --filter @mapi/plot-sandbox build \
 && test -f apps/web/dist/index.html \
 && test -f apps/plot-sandbox/dist/index.html \
 && test -f apps/plot-sandbox/dist/embed.html

FROM ${NGINX_IMAGE} AS runtime
ARG VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="mapi/web" \
      org.opencontainers.image.description="modeling-api web app and isolated plot sandbox (nginx, two origins)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="AGPL-3.0-only OR LicenseRef-mapi-commercial" \
      org.opencontainers.image.vendor="modeling-api" \
      org.opencontainers.image.base.name="nginxinc/nginx-unprivileged:1.30-alpine" \
      io.mapi.image.role="web" \
      io.mapi.image.user="101"
COPY --from=build --chown=0:0 /src/apps/web/dist /usr/share/nginx/html/web
COPY --from=build --chown=0:0 /src/apps/plot-sandbox/dist /usr/share/nginx/html/plots
# The licences, and the third-party licence texts the minified bundles no longer carry.
COPY --chown=0:0 --chmod=0644 LICENSE.md LICENSE-APACHE.txt LICENSE-AGPL.txt COMMERCIAL-LICENSE.md \
     THIRD-PARTY-NOTICES.md /usr/share/doc/mapi/
COPY --chown=0:0 images/web/nginx.conf /etc/nginx/nginx.conf
COPY --chown=0:0 images/web/templates/web/ /etc/nginx/templates/web/
COPY --chown=0:0 images/web/templates/plots/ /etc/nginx/templates/plots/
COPY --chown=0:0 images/web/templates/web/ /etc/nginx/templates/all/
COPY --chown=0:0 images/web/templates/plots/ /etc/nginx/templates/all/
COPY --chown=0:0 --chmod=0755 images/web/05-mapi-dirs.sh /docker-entrypoint.d/05-mapi-dirs.sh
COPY --chown=0:0 --chmod=0755 images/web/healthcheck.sh /usr/local/bin/mapi-web-healthcheck
ENV NGINX_ENVSUBST_TEMPLATE_DIR=/etc/nginx/templates/all \
    NGINX_ENVSUBST_OUTPUT_DIR=/tmp/nginx/conf.d \
    NGINX_ENVSUBST_FILTER="^(MAPI_|NGINX_LOCAL_RESOLVERS)" \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENTRYPOINT_QUIET_LOGS=1 \
    MAPI_WEB_ORIGIN=http://127.0.0.1:5173 \
    MAPI_WEB_SERVER_NAMES="127.0.0.1 localhost" \
    MAPI_PLOTS_ORIGIN=http://127.0.0.1:5174 \
    MAPI_API_URL=http://127.0.0.1:8000 \
    MAPI_API_UPSTREAM=http://api:8000 \
    MAPI_GCS_ENDPOINT=http://127.0.0.1:4443 \
    MAPI_WEB_SCRIPT_SRC_EXTRA=""
USER 101
EXPOSE 8080 8081
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["/usr/local/bin/mapi-web-healthcheck"]
STOPSIGNAL SIGQUIT
