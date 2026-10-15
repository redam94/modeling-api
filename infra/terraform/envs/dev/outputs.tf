# deploy/gcp/*.sh read these with `terraform output -json`.

output "env" {
  description = "Environment name."
  value       = module.stack.env
}

output "region" {
  description = "Region."
  value       = module.stack.region
}

output "control_project" {
  description = "Control project id."
  value       = module.stack.control_project
}

output "urls" {
  description = "Public origins and run.app URLs."
  value       = module.stack.urls
}

output "names" {
  description = "Cloud Run resource names."
  value       = module.stack.names
}

output "services_deployed" {
  description = "Whether the Cloud Run workloads are in this configuration."
  value       = module.stack.services_deployed
}

output "lb_ip" {
  description = "Load balancer address (point both domains here)."
  value       = module.stack.lb_ip
}

output "image_repository" {
  description = "Docker repository for build-push.sh."
  value       = module.stack.image_repository
}

output "buckets" {
  description = "Bucket names."
  value       = module.stack.buckets
}

output "exec_pools" {
  description = "Job inventory per exec pool."
  value       = module.stack.exec_pools
}

output "service_accounts" {
  description = "Service accounts."
  value       = module.stack.service_accounts
}

output "events" {
  description = "Event transport."
  value       = module.stack.events
}

output "perimeter" {
  description = "VPC-SC perimeter (null when disabled)."
  value       = module.stack.perimeter
}

output "github" {
  description = "Values for the GitHub environments (GCP_WIF_PROVIDER, service accounts)."
  value       = module.stack.github
}
