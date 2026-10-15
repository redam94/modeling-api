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

  # ------------------------------------------------------------------------ environment
  # The deployment contract with mapi-server (documented in docs/operations/gcp-deployment.md).
  # Secrets arrive through Secret Manager references (secret_env below), never as values.
  env_common = {
    MAPI_ENV          = var.env
    MAPI_PROFILE      = "multi-tenant"
    MAPI_OBJECT_STORE = "gcs"
    MAPI_GCP_PROJECT  = var.project_id
    MAPI_GCP_REGION   = var.region
    MAPI_LOG_LEVEL    = "info"
    MAPI_API_URL      = local.api_url
    MAPI_WEB_ORIGIN   = local.web_origin
    MAPI_PLOTS_ORIGIN = local.plots_origin
    MAPI_DATA_DIR     = "/tmp/mapi"
    MAPI_KMS_KEY      = google_kms_crypto_key.bundle_signing.id
    MAPI_URL_SIGNER   = google_service_account.sa["url-signer"].email
  }

  env = {
    api = merge(local.env_common, {
      MAPI_ALLOWED_HOSTS  = join(",", distinct([local.host.web, local.host.api]))
      MAPI_OIDC_ISSUER    = var.oidc.issuer
      MAPI_OIDC_CLIENT_ID = var.oidc.client_id == null ? "" : var.oidc.client_id
    }, lookup(var.extra_env, "api", {}))

    events = merge(local.env_common, {
      MAPI_ALLOWED_HOSTS        = trimprefix(local.run_url.events, "https://")
      MAPI_PUSH_AUDIENCE        = local.push_audience
      MAPI_PUSH_SERVICE_ACCOUNT = google_service_account.sa["push"].email
      MAPI_PUSH_SUBSCRIPTION    = "projects/${var.project_id}/subscriptions/${local.prefix}-run-events-push"
    }, lookup(var.extra_env, "events", {}))

    orchestrator = merge(local.env_common, {
      MAPI_EXECUTOR       = "cloudrun-jobs"
      MAPI_BOOTSTRAP_URL  = var.bootstrap_url_template
      MAPI_CLOUDRUN_POOLS = jsonencode(var.exec_pools)
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

  # name of the env var => secret id (resolved to "latest" version at revision start)
  secret_env = {
    api = merge({
      MAPI_DATABASE_URL        = "db-url-app"
      MAPI_DATABASE_LISTEN_URL = "db-url-app-direct"
      MAPI_TOKEN_PEPPER        = "token-pepper"
      MAPI_SESSION_SECRET      = "session-secret"
      }, var.oidc.client_secret_version > 0 ? {
      MAPI_OIDC_CLIENT_SECRET = "oidc-client-secret"
    } : {})
    events = {
      MAPI_DATABASE_URL = "db-url-app"
    }
    orchestrator = {
      MAPI_DATABASE_URL = "db-url-orchestrator"
    }
    migrate = {
      MAPI_DATABASE_URL = "db-url-migrate"
    }
  }
}
