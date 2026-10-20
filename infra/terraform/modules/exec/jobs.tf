# The fixed Job inventory (plan §8.1, §8.8): {base-bayes, base-ml, base-forecast, base-dl} x
# {S, M, L}, plus (v0.15, gpu_jobs) one GPU Job of base-dl. Executions override env only
# (MAPI_RUN_ID, MAPI_NONCE); image, CPU, memory, GPU and the service account cannot be
# overridden per execution, which is why the inventory is fixed.
#
# [unverified: Cloud Run Jobs GPU (L4) needs a region with GPU quota; not applied or tested
# against GCP]

locals {
  cpu_jobs = {
    for pair in setproduct(sort(keys(var.images)), var.classes) :
    "${pair[0]}-${lower(pair[1])}" => { base = pair[0], class = pair[1] }
  }
  gpu_jobs = var.gpu_jobs && contains(keys(var.images), "base-dl") ? {
    "base-dl-gpu" = { base = "base-dl", class = "GPU" }
  } : {}
  jobs       = merge(local.cpu_jobs, local.gpu_jobs)
  job_specs  = merge(var.class_specs, { GPU = var.gpu_class_spec })
  job_limits = { "nvidia.com/gpu" = tostring(var.gpu_class_spec.gpus) }
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
      timeout               = local.job_specs[each.value.class].timeout
      # GPU Jobs: no zonal redundancy (needs less GPU quota; a zone outage fails the run,
      # and the reconciler retries it)
      gpu_zonal_redundancy_disabled = each.value.class == "GPU" ? true : null

      containers {
        image = var.images[each.value.base]

        resources {
          limits = merge(
            {
              cpu    = local.job_specs[each.value.class].cpu
              memory = local.job_specs[each.value.class].memory
            },
            each.value.class == "GPU" ? local.job_limits : {},
          )
        }

        env {
          name  = "MAPI_BOOTSTRAP_URL"
          value = var.bootstrap_url_template
        }
      }

      dynamic "node_selector" {
        for_each = each.value.class == "GPU" ? [var.gpu_class_spec.accelerator] : []
        content {
          accelerator = node_selector.value
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
