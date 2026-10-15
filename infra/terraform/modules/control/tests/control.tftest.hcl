# Control-plane invariants, checked on a mocked apply.

mock_provider "google" {
  source = "../../testing/mocks/google"
}

mock_provider "google-beta" {
  source = "../../testing/mocks/google-beta"
}

variables {
  project_id             = "acme-test-control"
  project_number         = "111111111111"
  env                    = "test"
  region                 = "europe-west4"
  buckets                = { quarantine = "mapi-test-quarantine", bootstrap = "mapi-test-bootstrap", runs = "mapi-test-runs", bundles = "mapi-test-bundles", data = "mapi-test-data" }
  bootstrap_url_template = "gs://mapi-test-bootstrap/{nonce}"
  exec_pools = {
    paid = {
      project = "acme-test-exec-paid"
      region  = "europe-west4"
      classes = ["S"]
      jobs    = { base-bayes = { S = "mapi-test-base-bayes-s" } }
      public  = false
    }
  }
  images = {
    server = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/server@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    web    = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/web@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }
  # `terraform test` needs every ephemeral input set in both phases; with
  # oidc.client_secret_version = 0 (the default) the value is never read.
  oidc_client_secret = "unused"
}

run "events_path" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_service.events[0].ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    error_message = "The events service is internal-only."
  }

  assert {
    condition     = !coalesce(google_cloud_run_v2_service.events[0].invoker_iam_disabled, false)
    error_message = "The events service keeps IAM invocation checks."
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.events_push_invoker[0].member == "serviceAccount:${google_service_account.sa["push"].email}"
    error_message = "Only the push identity may invoke the events service."
  }

  assert {
    condition = (
      google_pubsub_subscription.run_events_push.push_config[0].push_endpoint == "https://mapi-test-events-111111111111.europe-west4.run.app/internal/events" &&
      google_pubsub_subscription.run_events_push.push_config[0].oidc_token[0].audience == "https://mapi-test-events-111111111111.europe-west4.run.app"
    )
    error_message = "Pushes go to /internal/events with an OIDC token for the events service."
  }

  assert {
    condition     = google_logging_project_sink.run_events["paid"].filter == "resource.type=\"cloud_run_job\" AND resource.labels.job_name=~\"^mapi-test-\" AND jsonPayload.mapi_event:*"
    error_message = "The sink routes only this environment's supervisor event lines."
  }

  assert {
    condition     = google_pubsub_subscription.run_events_push.dead_letter_policy[0].max_delivery_attempts == 10
    error_message = "Undeliverable events are dead-lettered."
  }
}

run "database" {
  command = apply

  assert {
    condition     = google_sql_database_instance.main.database_version == "POSTGRES_16"
    error_message = "PostgreSQL 16."
  }

  assert {
    condition = (
      !google_sql_database_instance.main.settings[0].ip_configuration[0].ipv4_enabled &&
      google_sql_database_instance.main.settings[0].ip_configuration[0].ssl_mode == "ENCRYPTED_ONLY"
    )
    error_message = "Private IP only, TLS required."
  }

  assert {
    condition     = one(google_sql_database_instance.main.settings[0].connection_pool_config[*].connection_pooling_enabled)
    error_message = "Managed connection pooling is on by default."
  }

  assert {
    condition     = toset([for u in google_sql_user.role : u.name]) == toset(["mapi_app", "mapi_orchestrator", "mapi_migrate"])
    error_message = "Three roles: app (RLS-bound), orchestrator (BYPASSRLS), migrate (owner)."
  }

  assert {
    condition     = alltrue([for u in google_sql_user.role : u.password == null])
    error_message = "Passwords are write-only: never in state."
  }
}

run "no_secret_values_in_env" {
  command = apply

  assert {
    condition = alltrue(flatten([
      for svc in [google_cloud_run_v2_service.api[0], google_cloud_run_v2_service.events[0]] : [
        for e in svc.template[0].containers[0].env :
        e.value == null || e.value == "" || length(regexall("(?i)(secret|pepper|password|database_url)", e.name)) == 0
      ]
    ]))
    error_message = "Secret-looking variables must come from Secret Manager references, never literal values."
  }

  assert {
    condition     = alltrue([for v in google_secret_manager_secret_version.db_url : v.secret_data == null])
    error_message = "Connection URLs are write-only."
  }

  assert {
    condition     = length([for e in google_cloud_run_v2_service.api[0].template[0].containers[0].env : e if e.name == "MAPI_OIDC_CLIENT_SECRET"]) == 0
    error_message = "No OIDC client secret reference until a secret version exists."
  }
}

run "signing_and_least_privilege" {
  command = apply

  assert {
    condition = (
      google_kms_crypto_key.bundle_signing.purpose == "ASYMMETRIC_SIGN" &&
      google_kms_crypto_key.bundle_signing.version_template[0].algorithm == "EC_SIGN_P256_SHA256"
    )
    error_message = "Bundle manifests are signed with EC P-256 (same as the local key)."
  }

  assert {
    condition     = google_kms_crypto_key_iam_member.signer.member == "serviceAccount:${google_service_account.sa["orchestrator"].email}"
    error_message = "Only the orchestrator signs."
  }

  assert {
    condition     = google_project_iam_custom_role.url_signer.permissions == toset(["iam.serviceAccounts.signBlob"])
    error_message = "URL minting is signBlob only (no token creation, no keys)."
  }

  assert {
    condition     = length([for k, m in google_project_iam_member.telemetry : m if strcontains(m.role, "admin") || strcontains(m.role, "editor") || m.role == "roles/owner"]) == 0
    error_message = "Runtime identities get no admin/editor project roles."
  }
}

run "without_load_balancer" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_service.api[0].ingress == "INGRESS_TRAFFIC_ALL"
    error_message = "Without a load balancer the API is reached on run.app."
  }

  assert {
    condition     = length(google_compute_url_map.lb) == 0
    error_message = "No load balancer without domains."
  }
}

run "with_load_balancer" {
  command = apply

  variables {
    domains = { web = "app.example.com", plots = "plots.example.net" }
  }

  assert {
    condition     = google_cloud_run_v2_service.api[0].ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "Behind the load balancer, the API accepts traffic only from it."
  }

  assert {
    condition     = google_compute_ssl_policy.lb[0].min_tls_version == "TLS_1_2"
    error_message = "TLS 1.2 or newer."
  }

  assert {
    condition = length(flatten([
      for pm in google_compute_url_map.lb[0].path_matcher : [
        for pr in pm.path_rule : [for p in pr.paths : p if startswith(p, "/internal")]
      ]
    ])) == 0
    error_message = "The load balancer never routes /internal/* (Pub/Sub push only reaches the events service)."
  }

  assert {
    condition = toset(flatten([
      for pm in google_compute_url_map.lb[0].path_matcher : [for pr in pm.path_rule : pr.paths]
    ])) == toset(["/v1", "/v1/*", "/auth/*", "/healthz", "/readyz"])
    error_message = "Only the API's public prefixes are routed to it."
  }
}

run "plot_sandbox_needs_its_own_host" {
  command = plan

  variables {
    domains = { web = "app.example.com", plots = "app.example.com" }
  }

  expect_failures = [var.domains]
}

run "pooling_needs_enterprise_plus" {
  command = plan

  variables {
    database = { edition = "ENTERPRISE", connection_pooling = true }
  }

  expect_failures = [var.database]
}
