# The fixed Job inventory (plan §8.1, §8.8): {base-bayes, base-ml, base-forecast} x {S, M, L}.
# Executions override env only (MAPI_RUN_ID, MAPI_NONCE); image, CPU, memory and the service
# account cannot be overridden per execution, which is why the inventory is fixed.

locals {
  jobs = {
    for pair in setproduct(sort(keys(var.images)), var.classes) :
    "${pair[0]}-${lower(pair[1])}" => { base = pair[0], class = pair[1] }
  }
}

resource "google_cloud_run_v2_job" "runner" {
  for_each = local.jobs

  project             = var.project_id
  location            = var.region
  name                = "mapi-${var.env}-${each.key}"
  deletion_protection = var.deletion_protection
  labels = merge(var.labels, {
    mapi-env   = var.env
    mapi-pool  = var.pool
    mapi-base  = each.value.base
    mapi-class = lower(each.value.class)
  })

  dynamic "binary_authorization" {
    for_each = var.binary_authorization ? [1] : []
    content {
      use_default = true
    }
  }

  template {
    task_count  = 1
    parallelism = 1
    labels = {
      mapi-env   = var.env
      mapi-pool  = var.pool
      mapi-base  = each.value.base
      mapi-class = lower(each.value.class)
    }

    template {
      service_account       = google_service_account.exec.email
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2" # microVM sandbox
      max_retries           = 0                            # the reconciler decides retries (§8.3)
      timeout               = var.class_specs[each.value.class].timeout

      containers {
        image = var.images[each.value.base]

        resources {
          limits = {
            cpu    = var.class_specs[each.value.class].cpu
            memory = var.class_specs[each.value.class].memory
          }
        }

        env {
          name  = "MAPI_BOOTSTRAP_URL"
          value = var.bootstrap_url_template
        }
      }

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.exec.id
          subnetwork = google_compute_subnetwork.jobs.id
          tags       = [local.network_tag]
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.images[each.value.base]))
      error_message = "Runner images must be pinned by digest."
    }
  }

  # The VPC's egress controls must exist before the first execution can start.
  depends_on = [
    google_compute_firewall.deny_all_egress,
    google_compute_firewall.deny_all_egress_ipv6,
    google_dns_response_policy_rule.sinkhole_all,
    google_dns_response_policy_rule.bypass,
  ]
}
