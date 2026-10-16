locals {
  prefix = "mapi-${var.env}"

  names = {
    api          = "${local.prefix}-api"
    events       = "${local.prefix}-events"
    web          = "${local.prefix}-web"
    plots        = "${local.prefix}-plots"
    orchestrator = "${local.prefix}-orchestrator"
    migrate      = "${local.prefix}-migrate"
  }

  # Deterministic Cloud Run URLs: https://<service>-<project number>.<region>.run.app
  run_url = { for k, n in local.names : k => "https://${n}-${var.project_number}.${var.region}.run.app" }

  lb_enabled   = var.domains != null
  web_origin   = local.lb_enabled ? "https://${var.domains.web}" : local.run_url.web
  plots_origin = local.lb_enabled ? "https://${var.domains.plots}" : local.run_url.plots
  # Behind the load balancer the API shares the app origin (/v1/*), so the SPA needs no CORS.
  api_url = local.lb_enabled ? local.web_origin : local.run_url.api

  host = {
    web   = trimprefix(local.web_origin, "https://")
    plots = trimprefix(local.plots_origin, "https://")
    api   = trimprefix(local.run_url.api, "https://")
  }

  # The events service checks the OIDC token of each push: this audience and this identity.
  push_audience = local.run_url.events
  push_endpoint = "${local.run_url.events}/internal/events"

  # Only lines the supervisor marks as events, only from this environment's Jobs.
  event_log_filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=~\"^mapi-${var.env}-\"",
    "jsonPayload.mapi_event:*",
  ])

  services_ready = var.images.server != null && var.images.web != null

  # ------------------------------------------------------------------- metrics collection
  # Each server workload serves GET /metrics (bearer: the metrics-token secret). Nothing
  # outside the instance reaches it (the load balancer routes only /v1, /auth and health;
  # the orchestrator has no ingress), so a collector sidecar scrapes it over loopback and
  # writes to Managed Service for Prometheus, where the alert policies and dashboard read.
  collector_enabled = var.metrics_collector.image != null
  metrics_ports     = { api = 8080, events = 8080, orchestrator = 9464 }
  collector_config = {
    for w, port in local.metrics_ports : w => yamlencode({
      receivers = {
        prometheus = {
          config = {
            scrape_configs = [{
              job_name        = "mapi-${w}"
              scrape_interval = var.metrics_collector.scrape_interval
              metrics_path    = "/metrics"
              # expanded by the collector from its own environment (Secret Manager)
              authorization  = { type = "Bearer", credentials = "$${env:MAPI_METRICS_TOKEN}" }
              static_configs = [{ targets = ["127.0.0.1:${port}"], labels = { mapi_workload = w } }]
            }]
          }
        }
      }
      processors = {
        resourcedetection = { detectors = ["env", "gcp"], timeout = "10s" }
        memory_limiter    = { check_interval = "1s", limit_percentage = 65, spike_limit_percentage = 20 }
        batch             = {}
      }
      exporters = { googlemanagedprometheus = {} }
      service = {
        pipelines = {
          metrics = {
            receivers  = ["prometheus"]
            processors = ["resourcedetection", "memory_limiter", "batch"]
            exporters  = ["googlemanagedprometheus"]
          }
        }
      }
    })
  }

  # The one exec pool this orchestrator dispatches to (see var.dispatch_pool).
  private_pools = sort([for k, p in var.exec_pools : k if !p.public])
  dispatch_pool = coalesce(
    var.dispatch_pool,
    length(local.private_pools) > 0 ? local.private_pools[0] : sort(keys(var.exec_pools))[0],
  )

  # The residency of tenants created without one (the platform tenant first): what the
  # dispatch pool's region satisfies (europe-* -> eu, us-*/northamerica-* -> us, asia-* ->
  # asia, else the region itself), so the executor can always place them. A "us" default
  # on an EU deployment would fail every run of those tenants.
  dispatch_region = var.exec_pools[local.dispatch_pool].region
  default_residency = coalesce(
    var.default_residency,
    lookup(
      { europe = "eu", us = "us", northamerica = "us", asia = "asia" },
      split("-", local.dispatch_region)[0],
      local.dispatch_region,
    ),
  )

  # ------------------------------------------------------------------------ environment
  # The deployment contract with mapi-server: every name is a mapi_server.settings.Settings
  # field (tests/infra/test_contract.py checks it; docs/operations/gcp-deployment.md lists
  # them). Secrets arrive through Secret Manager references (secret_env below), never as values.
  env_common = {
    MAPI_ENV             = var.env
    MAPI_PROFILE         = "multi-tenant"
    MAPI_OBJECT_STORE    = "gcs"
    MAPI_GCP_PROJECT     = var.project_id
    MAPI_LOG_LEVEL       = "info"
    MAPI_API_URL         = local.api_url
    MAPI_WEB_ORIGIN      = local.web_origin
    MAPI_PLOTS_ORIGIN    = local.plots_origin
    MAPI_DATA_DIR        = "/tmp/mapi"
    MAPI_SIGNER          = "kms"
    MAPI_KMS_KEY_VERSION = "${google_kms_crypto_key.bundle_signing.id}/cryptoKeyVersions/${var.kms_key_version}"
    # retired versions that still verify the bundles they signed (key rotation)
    MAPI_KMS_TRUSTED_KEY_VERSIONS = join(",", [
      for v in var.kms_trusted_key_versions : "${google_kms_crypto_key.bundle_signing.id}/cryptoKeyVersions/${v}"
    ])
    MAPI_URL_SIGNER      = google_service_account.sa["url-signer"].email
    MAPI_URL_UPLOADER    = google_service_account.sa["url-uploader"].email
    MAPI_DB_APP_ROLE     = "mapi_app"
    MAPI_DB_SYSTEM_ROLE  = "mapi_orchestrator"
    MAPI_DB_GATEWAY_ROLE = "mapi_gateway"
    # every service (and the migration's adoption of v0.2 rows) agrees on the default
    MAPI_DEFAULT_RESIDENCY = local.default_residency
    # The hops in front of the API, skipped when it reads the caller's address from
    # X-Forwarded-For: Cloud Run's front end (link-local to the container), Google's load
    # balancer front ends, and this deployment's load balancer address. Without them every
    # per-address limit (sign-up, verification, embeds) would apply platform-wide.
    MAPI_TRUSTED_PROXIES = join(",", concat(
      ["169.254.0.0/16", "35.191.0.0/16", "130.211.0.0/22"],
      google_compute_global_address.lb[*].address,
    ))
  }

  env = {
    api = merge(local.env_common, {
      MAPI_ALLOWED_HOSTS  = join(",", distinct([local.host.web, local.host.api]))
      MAPI_OIDC_ISSUER    = var.oidc.issuer
      MAPI_OIDC_CLIENT_ID = var.oidc.client_id == null ? "" : var.oidc.client_id
    }, lookup(var.extra_env, "api", {}))

    events = merge(local.env_common, {
      MAPI_ALLOWED_HOSTS               = trimprefix(local.run_url.events, "https://")
      MAPI_PUBSUB_PUSH_AUDIENCE        = local.push_audience
      MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT = google_service_account.sa["push"].email
      MAPI_PUBSUB_PUSH_SUBSCRIPTIONS   = "projects/${var.project_id}/subscriptions/${local.prefix}-run-events-push"
    }, lookup(var.extra_env, "events", {}))

    orchestrator = merge(local.env_common, {
      MAPI_EXECUTOR         = "cloudrun-jobs"
      MAPI_CLOUDRUN_PROJECT = var.exec_pools[local.dispatch_pool].project
      MAPI_CLOUDRUN_REGION  = var.exec_pools[local.dispatch_pool].region
      # the dispatcher, reconciler and finalizer series live in this process: serve them on
      # loopback for the collector sidecar (local.metrics_ports.orchestrator)
      MAPI_WORKER_METRICS_PORT = "9464"
      MAPI_WORKER_METRICS_HOST = "127.0.0.1"
    }, lookup(var.extra_env, "orchestrator", {}))

    migrate = merge(local.env_common, lookup(var.extra_env, "migrate", {}))

    web = merge({
      MAPI_WEB_ORIGIN       = local.web_origin
      MAPI_PLOTS_ORIGIN     = local.plots_origin
      MAPI_API_URL          = local.api_url
      MAPI_API_UPSTREAM     = local.run_url.api
      MAPI_GCS_ENDPOINT     = "https://storage.googleapis.com"
      MAPI_WEB_SERVER_NAMES = join(" ", distinct([local.host.web, trimprefix(local.run_url.web, "https://")]))
    }, lookup(var.extra_env, "web", {}))
  }

  # api and events run the same app: tenant sessions on mapi_app (pooled), sign-in lookups,
  # operator routes and pushed events on the gateway role (no RLS bypass: plan §9 keeps that
  # for the orchestrator alone), LISTEN on a direct connection
  secret_env_api = {
    MAPI_DATABASE_URL         = "db-url-app-direct"
    MAPI_DATABASE_APP_URL     = "db-url-app"
    MAPI_DATABASE_GATEWAY_URL = "db-url-gateway"
    MAPI_SERVER_SECRET        = "server-secret"
    MAPI_METRICS_TOKEN        = "metrics-token"
  }

  # name of the env var => secret id (resolved to "latest" version at revision start)
  secret_env = {
    api = merge(local.secret_env_api, var.oidc.client_secret_version > 0 ? {
      MAPI_OIDC_CLIENT_SECRET = "oidc-client-secret"
    } : {})
    events = local.secret_env_api
    orchestrator = {
      MAPI_DATABASE_URL        = "db-url-orchestrator"
      MAPI_DATABASE_SYSTEM_URL = "db-url-orchestrator"
      MAPI_DATABASE_APP_URL    = "db-url-app"
      MAPI_SERVER_SECRET       = "server-secret"
      MAPI_METRICS_TOKEN       = "metrics-token"
    }
    migrate = {
      MAPI_DATABASE_URL  = "db-url-migrate"
      MAPI_SERVER_SECRET = "server-secret"
    }
  }
}
