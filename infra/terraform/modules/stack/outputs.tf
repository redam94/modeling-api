output "env" {
  description = "Environment name."
  value       = var.env
}

output "region" {
  description = "Region of the control plane and the Jobs."
  value       = var.region
}

output "control_project" {
  description = "Control project id."
  value       = var.projects.control
}

output "urls" {
  description = "Public origins and run.app URLs."
  value       = module.control.urls
}

output "names" {
  description = "Cloud Run resource names (api, events, web, plots, orchestrator, migrate)."
  value       = module.control.names
}

output "services_deployed" {
  description = "Whether this configuration includes the Cloud Run workloads."
  value       = module.control.services_deployed
}

output "lb_ip" {
  description = "Load balancer address for the DNS records of both domains."
  value       = module.control.lb_ip
}

output "image_repository" {
  description = "Where build-push.sh pushes images."
  value       = module.build.image_repository
}

output "pypi_repository" {
  description = "Curated PyPI remote (pip index URL)."
  value       = module.build.pypi_repository
}

output "buckets" {
  description = "Bucket names per purpose."
  value       = module.data.bucket_names
}

output "exec_pools" {
  description = "Job inventory per exec pool."
  value       = { for pool, m in module.exec : pool => m.inventory }
}

output "service_accounts" {
  description = "Control-plane service accounts, plus the execution account of every pool."
  value = merge(
    module.control.service_accounts,
    { for pool, m in module.exec : "exec-${pool}" => m.exec_sa_email },
  )
}

output "database" {
  description = "Cloud SQL instance details (no credentials)."
  value       = module.control.database
}

output "events" {
  description = "Event transport: topic, push subscription, dead letters, sink filter."
  value       = module.control.events
}

output "kms_signing_key" {
  description = "Bundle-manifest signing key."
  value       = module.control.kms_signing_key
}

output "perimeter" {
  description = "VPC-SC perimeter name and whether it is enforced (null when disabled)."
  value = length(module.perimeter) == 0 ? null : {
    name     = module.perimeter[0].perimeter
    enforced = module.perimeter[0].enforced
  }
}

output "github" {
  description = "WIF provider and CI service accounts for the GitHub environments."
  value       = module.build.github
}
