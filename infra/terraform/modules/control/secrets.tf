# Secret Manager. Every value is written with a write-only argument from an ephemeral source
# (generated here, or the ephemeral oidc_client_secret variable): nothing secret is ever in the
# Terraform state, a plan file or a Cloud Run revision spec (services reference secrets by
# name). Secrets are replicated only in the control region (data residency).
#
#   db-url-app            mapi_app (row-level security), pooled (transaction mode):
#                         MAPI_DATABASE_APP_URL of api, events and orchestrator
#   db-url-app-direct     mapi_app, direct: MAPI_DATABASE_URL of api and events (their LISTEN
#                         connection for SSE fan-out)
#   db-url-gateway        mapi_gateway (MAPI_DB_GATEWAY_ROLE, no RLS bypass), pooled:
#                         MAPI_DATABASE_GATEWAY_URL of api and events (sign-in lookups,
#                         operator routes, pushed events)
#   db-url-orchestrator   mapi_orchestrator (the platform role, MAPI_DB_SYSTEM_ROLE, the only
#                         RLS bypass), direct: the orchestrator's own URL (procrastinate
#                         LISTEN, advisory locks). The API and events services cannot read it.
#   db-url-migrate        migrate job: direct, schema owner
#   server-secret         MAPI_SERVER_SECRET: the root of the API-token pepper and the audit
#                         fingerprint key (mapi_server.services.secrets); shared by every
#                         process, rotating it invalidates every API token
#   metrics-token         MAPI_METRICS_TOKEN: the bearer Prometheus presents on GET /metrics
#   oidc-client-secret    OAuth client secret (only when oidc.client_secret_version > 0)

locals {
  secret_ids = toset([
    "db-url-app",
    "db-url-app-direct",
    "db-url-gateway",
    "db-url-orchestrator",
    "db-url-migrate",
    "server-secret",
    "metrics-token",
    "oidc-client-secret",
  ])

  # Which workload may read which secret.
  secret_access = {
    "db-url-app"          = ["api", "events", "orchestrator"]
    "db-url-app-direct"   = ["api", "events"]
    "db-url-gateway"      = ["api", "events"]
    "db-url-orchestrator" = ["orchestrator"]
    "db-url-migrate"      = ["migrate"]
    "server-secret"       = ["api", "events", "orchestrator", "migrate"]
    "metrics-token"       = ["api", "events", "orchestrator"]
    "oidc-client-secret"  = ["api"]
  }

  secret_grants = merge([
    for secret, readers in local.secret_access : {
      for sa in readers : "${secret}|${sa}" => { secret = secret, sa = sa }
    }
  ]...)

  # (secret id => role, port) for the five connection URLs
  db_urls = {
    "db-url-app"          = { role = "app", pooled = true }
    "db-url-app-direct"   = { role = "app", pooled = false }
    "db-url-gateway"      = { role = "gateway", pooled = true }
    "db-url-orchestrator" = { role = "orchestrator", pooled = false }
    "db-url-migrate"      = { role = "migrate", pooled = false }
  }
}

resource "google_secret_manager_secret" "this" {
  for_each = local.secret_ids

  project   = var.project_id
  secret_id = "${local.prefix}-${each.key}"
  labels    = merge(var.labels, { mapi-secret = each.key })

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each = local.secret_grants

  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sa[each.value.sa].email}"
}

# ------------------------------------------------------------------------- database URLs

resource "google_secret_manager_secret_version" "db_url" {
  for_each = local.db_urls

  secret = google_secret_manager_secret.this[each.key].id
  secret_data_wo = format(
    "postgresql://mapi_%s:%s@%s:%d/%s?sslmode=require",
    each.value.role,
    ephemeral.random_password.db[each.value.role].result,
    local.db_host,
    each.value.pooled ? local.db_pooled_port : 5432,
    local.db_name,
  )
  secret_data_wo_version = var.database.password_version

  depends_on = [google_sql_user.role]
}

# ---------------------------------------------------------------------- generated app secrets

ephemeral "random_bytes" "server_secret" {
  length = 32
}

ephemeral "random_bytes" "metrics_token" {
  length = 32
}

resource "google_secret_manager_secret_version" "server_secret" {
  secret                 = google_secret_manager_secret.this["server-secret"].id
  secret_data_wo         = ephemeral.random_bytes.server_secret.hex
  secret_data_wo_version = var.secret_versions.server_secret
}

resource "google_secret_manager_secret_version" "metrics_token" {
  secret                 = google_secret_manager_secret.this["metrics-token"].id
  secret_data_wo         = ephemeral.random_bytes.metrics_token.hex
  secret_data_wo_version = var.secret_versions.metrics_token
}

# ------------------------------------------------------------------------------ OAuth client

resource "google_secret_manager_secret_version" "oidc_client_secret" {
  count = var.oidc.client_secret_version > 0 ? 1 : 0

  secret                 = google_secret_manager_secret.this["oidc-client-secret"].id
  secret_data_wo         = var.oidc_client_secret
  secret_data_wo_version = var.oidc.client_secret_version

  lifecycle {
    precondition {
      condition     = var.oidc.client_id != null
      error_message = "oidc.client_secret_version > 0 needs oidc.client_id as well."
    }
  }
}
