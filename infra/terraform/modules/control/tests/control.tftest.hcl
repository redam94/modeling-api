# Control-plane invariants, checked on a mocked apply.

mock_provider "google" {
  source = "../../testing/mocks/google"
}

mock_provider "google-beta" {
  source = "../../testing/mocks/google-beta"
}

variables {
  project_id     = "acme-test-control"
  project_number = "111111111111"
  env            = "test"
  region         = "europe-west4"
  buckets        = { quarantine = "mapi-test-quarantine", bootstrap = "mapi-test-bootstrap", runs = "mapi-test-runs", bundles = "mapi-test-bundles", data = "mapi-test-data" }
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
  metrics_collector = {
    image = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/otelcol-google@sha256:3333333333333333333333333333333333333333333333333333333333333333"
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

run "default_residency_follows_the_dispatch_region" {
  command = plan

  assert {
    condition     = local.env_common.MAPI_DEFAULT_RESIDENCY == "eu"
    error_message = "An EU dispatch pool makes EU the default residency (a us default could not be placed)."
  }
}

run "default_residency_can_be_pinned" {
  command = plan

  variables {
    default_residency = "europe-west4"
  }

  assert {
    condition     = local.env_common.MAPI_DEFAULT_RESIDENCY == "europe-west4"
    error_message = "An explicit default_residency wins."
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
    condition     = toset([for u in google_sql_user.role : u.name]) == toset(["mapi_app", "mapi_gateway", "mapi_orchestrator", "mapi_migrate"])
    error_message = "Four roles: app (RLS-bound), gateway (RLS-bound lookups), orchestrator (BYPASSRLS), migrate (owner)."
  }

  assert {
    condition = alltrue([
      for k, g in google_secret_manager_secret_iam_member.access :
      g.member == "serviceAccount:${google_service_account.sa["orchestrator"].email}"
      if startswith(k, "db-url-orchestrator|")
    ])
    error_message = "Only the orchestrator can read the URL of the RLS-bypassing role (plan §9)."
  }

  assert {
    condition = alltrue([
      for svc in [google_cloud_run_v2_service.api[0], google_cloud_run_v2_service.events[0]] :
      length([
        for e in svc.template[0].containers[0].env : e
        if e.name == "MAPI_DATABASE_SYSTEM_URL" || try(e.value_source[0].secret_key_ref[0].secret, "") == google_secret_manager_secret.this["db-url-orchestrator"].secret_id
      ]) == 0
    ])
    error_message = "The API and events services connect without the RLS bypass (gateway role)."
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

run "cloud_armor_on_every_backend" {
  command = apply

  variables {
    domains = { web = "app.example.com", plots = "plots.example.net" }
  }

  assert {
    condition = alltrue([
      for k, b in google_compute_backend_service.run : b.security_policy == google_compute_security_policy.edge[0].self_link
    ]) && length(google_compute_backend_service.run) == 3
    error_message = "The Cloud Armor policy is attached to the api, web and plots backends."
  }

  assert {
    condition = toset([
      for r in google_compute_security_policy.edge[0].rule : r.match[0].expr[0].expression
      if r.priority >= 1000 && r.priority < 2000
      ]) == toset([
      for w in ["sqli", "xss", "lfi", "rce", "scannerdetection"] :
      "evaluatePreconfiguredWaf('${w}-v33-stable', {'sensitivity': 1})"
    ])
    error_message = "The OWASP CRS rules sqli, xss, lfi, rce and scanner detection are configured."
  }

  assert {
    condition = alltrue([
      for r in google_compute_security_policy.edge[0].rule : r.preview && r.action == "deny(403)"
      if r.priority >= 1000 && r.priority < 2000
    ])
    error_message = "The OWASP rules start in preview (logged, not blocking)."
  }

  assert {
    condition = length([
      for r in google_compute_security_policy.edge[0].rule : r
      if r.action == "rate_based_ban" && !r.preview && r.rate_limit_options[0].enforce_on_key == "IP"
    ]) == 4
    error_message = "Sign-up, /auth/*, embeds and endpoint scoring have enforced per-IP rate bans."
  }

  assert {
    condition = anytrue([
      for r in google_compute_security_policy.edge[0].rule :
      strcontains(r.match[0].expr[0].expression, "/auth/signup") && r.rate_limit_options[0].rate_limit_threshold[0].count == 10
      if r.action == "rate_based_ban"
    ])
    error_message = "Sign-up is limited to 10 requests per IP and minute by default."
  }

  assert {
    condition     = google_compute_security_policy.edge[0].adaptive_protection_config[0].layer_7_ddos_defense_config[0].enable
    error_message = "Adaptive Protection is on by default."
  }

  assert {
    condition     = google_compute_security_policy.edge[0].advanced_options_config[0].json_parsing == "STANDARD"
    error_message = "JSON bodies are parsed, so the WAF rules see field values (fewer false positives)."
  }

  assert {
    condition     = output.security_policy == google_compute_security_policy.edge[0].self_link
    error_message = "The policy is exported."
  }
}

run "cloud_armor_enforce_and_trusted_ranges" {
  command = apply

  variables {
    domains = { web = "app.example.com", plots = "plots.example.net" }
    cloud_armor = {
      waf_mode            = "enforce"
      waf_sensitivity     = 2
      adaptive_protection = false
      trusted_ip_ranges   = ["203.0.113.0/24"]
      rate_limits         = { signup_per_minute = 3 }
    }
  }

  assert {
    condition = alltrue([
      for r in google_compute_security_policy.edge[0].rule : !r.preview
      if r.priority >= 1000 && r.priority < 2000
    ])
    error_message = "waf_mode = enforce blocks OWASP matches."
  }

  assert {
    condition = [
      for r in google_compute_security_policy.edge[0].rule : [r.action, toset(r.match[0].config[0].src_ip_ranges)]
      if r.priority == 100
    ] == [["allow", toset(["203.0.113.0/24"])]]
    error_message = "Trusted ranges are allowed before every other rule."
  }

  assert {
    condition     = !google_compute_security_policy.edge[0].adaptive_protection_config[0].layer_7_ddos_defense_config[0].enable
    error_message = "Adaptive Protection can be turned off."
  }
}

run "a_supplied_policy_replaces_the_module_one" {
  command = apply

  variables {
    domains         = { web = "app.example.com", plots = "plots.example.net" }
    security_policy = "projects/acme-test-control/global/securityPolicies/custom"
  }

  assert {
    condition     = length(google_compute_security_policy.edge) == 0
    error_message = "No module policy when one is supplied."
  }

  assert {
    condition     = google_compute_backend_service.run["api"].security_policy == "projects/acme-test-control/global/securityPolicies/custom"
    error_message = "The supplied policy is attached."
  }
}

run "no_cloud_armor_without_a_load_balancer" {
  command = plan

  assert {
    condition     = length(google_compute_security_policy.edge) == 0
    error_message = "Cloud Armor needs the load balancer."
  }
}

run "cloud_armor_modes_are_checked" {
  command = plan

  variables {
    cloud_armor = { waf_mode = "block" }
  }

  expect_failures = [var.cloud_armor]
}

run "cloud_armor_never_trusts_everyone" {
  command = plan

  variables {
    cloud_armor = { trusted_ip_ranges = ["*"] }
  }

  expect_failures = [var.cloud_armor]
}

run "logs_expire_after_the_configured_retention" {
  command = plan

  variables {
    log_retention_days = 14
  }

  assert {
    condition     = google_logging_project_bucket_config.default.retention_days == 14 && google_logging_project_bucket_config.default.bucket_id == "_Default"
    error_message = "The control project's _Default log bucket keeps logs log_retention_days days (tenant deletion does not purge logs)."
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

run "metrics_reach_cloud_monitoring" {
  command = apply

  assert {
    condition = alltrue([
      for svc in [google_cloud_run_v2_service.api[0], google_cloud_run_v2_service.events[0]] :
      length(svc.template[0].containers) == 2 && svc.template[0].containers[1].name == "collector"
    ])
    error_message = "api and events run a collector sidecar."
  }

  assert {
    condition     = length(google_cloud_run_v2_worker_pool.orchestrator[0].template[0].containers) == 2
    error_message = "The orchestrator runs a collector sidecar."
  }

  assert {
    condition = anytrue([
      for e in google_cloud_run_v2_worker_pool.orchestrator[0].template[0].containers[0].env :
      e.name == "MAPI_WORKER_METRICS_PORT" && e.value == "9464"
    ])
    error_message = "The orchestrator serves its own series (dispatch, reconcile, finalize)."
  }

  assert {
    condition = alltrue([
      for pair in [
        [google_cloud_run_v2_service.api[0].template[0].containers[1], "127.0.0.1:8080"],
        [google_cloud_run_v2_service.events[0].template[0].containers[1], "127.0.0.1:8080"],
        [google_cloud_run_v2_worker_pool.orchestrator[0].template[0].containers[1], "127.0.0.1:9464"],
        ] : anytrue([
          for e in pair[0].env :
          e.name == "COLLECTOR_CONFIG" && strcontains(e.value, pair[1]) && strcontains(e.value, "googlemanagedprometheus")
      ])
    ])
    error_message = "Each collector scrapes its own workload over loopback and writes to Managed Service for Prometheus."
  }

  assert {
    condition = alltrue([
      for c in [
        google_cloud_run_v2_service.api[0].template[0].containers[1],
        google_cloud_run_v2_worker_pool.orchestrator[0].template[0].containers[1],
        ] : anytrue([
          for e in c.env : e.name == "MAPI_METRICS_TOKEN" && length(e.value_source) == 1
      ])
    ])
    error_message = "The collector presents the metrics bearer from Secret Manager."
  }
}

run "kms_rotation_keeps_old_versions_trusted" {
  command = apply

  variables {
    kms_key_version          = 2
    kms_trusted_key_versions = [1]
  }

  assert {
    condition = alltrue([
      for c in [
        google_cloud_run_v2_service.api[0].template[0].containers[0],
        google_cloud_run_v2_service.events[0].template[0].containers[0],
        google_cloud_run_v2_worker_pool.orchestrator[0].template[0].containers[0],
        ] : anytrue([
          for e in c.env : e.name == "MAPI_KMS_TRUSTED_KEY_VERSIONS" && endswith(e.value, "/cryptoKeyVersions/1")
          ]) && anytrue([
          for e in c.env : e.name == "MAPI_KMS_KEY_VERSION" && endswith(e.value, "/cryptoKeyVersions/2")
      ])
    ])
    error_message = "After a rotation every server workload signs with the new version and still trusts the old one."
  }
}

run "the_active_kms_version_is_not_a_retired_one" {
  command = plan

  variables {
    kms_key_version          = 2
    kms_trusted_key_versions = [2]
  }

  expect_failures = [var.kms_trusted_key_versions]
}

run "no_collector_without_an_image" {
  command = apply

  variables {
    metrics_collector = {}
  }

  # the release still deploys, with a warning that nothing reaches Cloud Monitoring
  expect_failures = [check.metrics_collection]

  assert {
    condition     = length(google_cloud_run_v2_service.api[0].template[0].containers) == 1
    error_message = "No sidecar until a collector image is configured."
  }

  assert {
    condition = anytrue([
      for e in google_cloud_run_v2_worker_pool.orchestrator[0].template[0].containers[0].env :
      e.name == "MAPI_WORKER_METRICS_PORT"
    ])
    error_message = "The orchestrator serves its metrics either way (a collector can be added later)."
  }
}
