output "project_id" {
  description = "The exec project."
  value       = var.project_id
}

output "pool" {
  description = "Name of this exec pool."
  value       = var.pool
}

output "exec_sa_email" {
  description = "Execution service account (granted get+delete on the bootstrap bucket by modules/data)."
  value       = google_service_account.exec.email
}

output "network_id" {
  description = "The exec VPC."
  value       = google_compute_network.exec.id
}

output "jobs" {
  description = "Job name per base and class: {base-bayes = {S = \"mapi-prod-base-bayes-s\", ...}, ..., base-dl = {..., GPU = \"mapi-prod-base-dl-gpu\"}}."
  value = {
    for base in distinct([for j in values(local.jobs) : j.base]) : base => {
      for key, j in local.jobs : j.class => google_cloud_run_v2_job.runner[key].name if j.base == base
    }
  }
}

output "inventory" {
  description = "What the orchestrator needs to place runs on this pool (serialised into MAPI_CLOUDRUN_POOLS)."
  value = {
    project = var.project_id
    region  = var.region
    classes = concat(var.classes, length(local.gpu_jobs) > 0 ? ["GPU"] : [])
    jobs = {
      for base in distinct([for j in values(local.jobs) : j.base]) : base => {
        for key, j in local.jobs : j.class => google_cloud_run_v2_job.runner[key].name if j.base == base
      }
    }
  }
}
