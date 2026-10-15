# Secret Manager. Every value is written with a write-only argument from an ephemeral source
# (generated here, or the ephemeral oidc_client_secret variable): nothing secret is ever in the
# Terraform state, a plan file or a Cloud Run revision spec (services reference secrets by
# name). Secrets are replicated only in the control region (data residency).
#
#   db-url-app            API + events: pooled connection (transaction mode)
#   db-url-app-direct     API: direct connection for its single LISTEN connection (SSE fan-out)
#   db-url-orchestrator   orchestrator: direct (procrastinate LISTEN, advisory locks)
#   db-url-migrate        migrate job: direct, schema owner
#   token-pepper          HMAC pepper for API token hashes (mapi_spec.tokens.hash_token)
#   session-secret        BFF session cookie key
#   oidc-client-secret    OAuth client secret (only when oidc.client_secret_version > 0)

locals {
  secret_ids = toset([
    "db-url-app",
    "db-url-app-direct",
    "db-url-orchestrator",
    "db-url-migrate",
    "token-pepper",
    "session-secret",
    "oidc-client-secret",
  ])

  # Which workload may read which secret.
  secret_access = {
    "db-url-app"          = ["api", "events"]
    "db-url-app-direct"   = ["api"]
    "db-url-orchestrator" = ["orchestrator"]
    "db-url-migrate"      = ["migrate"]
    "token-pepper"        = ["api"]
    "session-secret"      = ["api"]
    "oidc-client-secret"  = ["api"]
  }

  secret_grants = merge([
    for secret, readers in local.secret_access : {
      for sa in readers : "${secret}|${sa}" => { secret = secret, sa = sa }
    }
  ]...)

  # (secret id => role, port) for the four connection URLs
  db_urls = {
    "db-url-app"          = { role = "app", pooled = true }
    "db-url-app-direct"   = { role = "app", pooled = false }
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

ephemeral "random_bytes" "token_pepper" {
  length = 32
}

ephemeral "random_bytes" "session_secret" {
  length = 64
}

resource "google_secret_manager_secret_version" "token_pepper" {
  secret                 = google_secret_manager_secret.this["token-pepper"].id
  secret_data_wo         = ephemeral.random_bytes.token_pepper.base64
  secret_data_wo_version = var.secret_versions.token_pepper
}

resource "google_secret_manager_secret_version" "session_secret" {
  secret                 = google_secret_manager_secret.this["session-secret"].id
  secret_data_wo         = ephemeral.random_bytes.session_secret.base64
  secret_data_wo_version = var.secret_versions.session_secret
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
