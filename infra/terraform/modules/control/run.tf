# Cloud Run workloads of the control plane. They are planned only once build-push.sh has
# produced digest-pinned images (var.images); before that, a foundation apply creates
# everything else (network, database, secrets, buckets, exec projects, registry).
#
#   api           service   public (or LB-only)  mapi-server api
#   events        service   internal-only, IAM-invoked by the push identity only
#   web, plots    services  public; the same nginx image on two ports = two origins
#   orchestrator  worker pool (no HTTP), mapi-server worker
#   migrate       job, executed by deploy/gcp/migrate.sh before each release

locals {
  n = local.services_ready ? 1 : 0

  vpc_egress = {
    network    = google_compute_network.control.id
    subnetwork = google_compute_subnetwork.run.id
  }
}

# Revisions depend on the secret versions and accessor grants they reference, so a revision
# never starts before its secrets can be read (the depends_on lists below).

# Without images (a foundation apply) none of the workloads below is planned; the
# `services_deployed` output says which case applies, and deploy/gcp/deploy.sh refuses a
# release apply whose images file does not name every image.

# ------------------------------------------------------------------------------------- api

resource "google_cloud_run_v2_service" "api" {
  count = local.n

  project             = var.project_id
  location            = var.region
  name                = local.names.api
  description         = "mapi control-plane API"
  labels              = merge(var.labels, { mapi-workload = "api" })
  deletion_protection = var.deletion_protection
  # Authentication happens in the application (OIDC sessions, API tokens); behind the load
  # balancer only the LB may reach the service at all.
  ingress              = local.lb_enabled ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true

  template {
    service_account                  = google_service_account.sa["api"].email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "3600s" # SSE streams; clients resume with Last-Event-ID
    max_instance_request_concurrency = var.services.api.concurrency
    labels                           = { mapi-workload = "api" }

    scaling {
      min_instance_count = var.services.api.min_instances
      max_instance_count = var.services.api.max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = local.vpc_egress.network
        subnetwork = local.vpc_egress.subnetwork
      }
    }

    containers {
      image = var.images.server
      args  = ["api", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.services.api.cpu
          memory = var.services.api.memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.env.api
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env.api
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds    = 5
        failure_threshold = 24
      }

      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds = 30
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_version.server_secret,
    google_secret_manager_secret_version.metrics_token,
    google_secret_manager_secret_version.oidc_client_secret,
    google_secret_manager_secret_iam_member.access,
  ]
}

# ---------------------------------------------------------------------------------- events

resource "google_cloud_run_v2_service" "events" {
  count = local.n

  project             = var.project_id
  location            = var.region
  name                = local.names.events
  description         = "mapi events service: Pub/Sub push target (POST /internal/events)"
  labels              = merge(var.labels, { mapi-workload = "events" })
  deletion_protection = var.deletion_protection
  # Pub/Sub push from a subscription in the same project counts as internal traffic.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account                  = google_service_account.sa["events"].email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "60s"
    max_instance_request_concurrency = var.services.events.concurrency
    labels                           = { mapi-workload = "events" }

    scaling {
      min_instance_count = var.services.events.min_instances
      max_instance_count = var.services.events.max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = local.vpc_egress.network
        subnetwork = local.vpc_egress.subnetwork
      }
    }

    containers {
      image = var.images.server
      args  = var.events_args

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.services.events.cpu
          memory = var.services.events.memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.env.events
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env.events
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds    = 5
        failure_threshold = 24
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_version.server_secret,
    google_secret_manager_secret_version.metrics_token,
    google_secret_manager_secret_version.oidc_client_secret,
    google_secret_manager_secret_iam_member.access,
  ]
}

# Only the push identity may invoke the events service (Cloud Run checks the OIDC token's
# signature and audience before the request reaches the container).
resource "google_cloud_run_v2_service_iam_member" "events_push_invoker" {
  count = local.n

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.events[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["push"].email}"
}

# ---------------------------------------------------------------------- web and plot sandbox

resource "google_cloud_run_v2_service" "web" {
  for_each = local.services_ready ? { web = 8080, plots = 8081 } : {}

  project              = var.project_id
  location             = var.region
  name                 = local.names[each.key]
  description          = each.key == "web" ? "mapi web app" : "mapi plot sandbox (separate origin)"
  labels               = merge(var.labels, { mapi-workload = each.key })
  deletion_protection  = var.deletion_protection
  ingress              = local.lb_enabled ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true

  template {
    service_account                  = google_service_account.sa["web"].email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "30s"
    max_instance_request_concurrency = var.services.web.concurrency
    labels                           = { mapi-workload = each.key }

    scaling {
      min_instance_count = var.services.web.min_instances
      max_instance_count = var.services.web.max_instances
    }

    containers {
      image = var.images.web

      ports {
        container_port = each.value
      }

      resources {
        limits = {
          cpu    = var.services.web.cpu
          memory = var.services.web.memory
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = local.env.web
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        tcp_socket {
          port = each.value
        }
        period_seconds    = 2
        failure_threshold = 15
      }
    }
  }
}

# ---------------------------------------------------------------------------- orchestrator

resource "google_cloud_run_v2_worker_pool" "orchestrator" {
  count = local.n

  project             = var.project_id
  location            = var.region
  name                = local.names.orchestrator
  description         = "mapi orchestrator: dispatcher (token bucket), reconciler, finalizer"
  labels              = merge(var.labels, { mapi-workload = "orchestrator" })
  deletion_protection = var.deletion_protection

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.services.orchestrator.instances
  }

  template {
    service_account = google_service_account.sa["orchestrator"].email
    labels          = { mapi-workload = "orchestrator" }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = local.vpc_egress.network
        subnetwork = local.vpc_egress.subnetwork
      }
    }

    containers {
      image = var.images.server
      args  = ["worker"]

      resources {
        limits = {
          cpu    = var.services.orchestrator.cpu
          memory = var.services.orchestrator.memory
        }
      }

      dynamic "env" {
        for_each = local.env.orchestrator
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env.orchestrator
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_version.server_secret,
    google_secret_manager_secret_version.metrics_token,
    google_secret_manager_secret_version.oidc_client_secret,
    google_secret_manager_secret_iam_member.access,
  ]
}

# --------------------------------------------------------------------------------- migrate

resource "google_cloud_run_v2_job" "migrate" {
  count = local.n

  project             = var.project_id
  location            = var.region
  name                = local.names.migrate
  labels              = merge(var.labels, { mapi-workload = "migrate" })
  deletion_protection = var.deletion_protection

  template {
    task_count = 1
    labels     = { mapi-workload = "migrate" }

    template {
      service_account       = google_service_account.sa["migrate"].email
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
      max_retries           = 0 # a failed migration needs a human, not a retry
      timeout               = "1800s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = local.vpc_egress.network
          subnetwork = local.vpc_egress.subnetwork
        }
      }

      containers {
        image = var.images.server
        args  = ["migrate"]

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }

        dynamic "env" {
          for_each = local.env.migrate
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.secret_env.migrate
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.this[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    # deploy/gcp/migrate.sh points the job at the release's server image and executes it
    # BEFORE the new API revision rolls out (expand/contract migrations). Terraform owns
    # everything else about the job.
    ignore_changes = [template[0].template[0].containers[0].image]
  }

  depends_on = [
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_version.server_secret,
    google_secret_manager_secret_version.metrics_token,
    google_secret_manager_secret_version.oidc_client_secret,
    google_secret_manager_secret_iam_member.access,
  ]
}
