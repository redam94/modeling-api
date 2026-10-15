output "service_accounts" {
  description = "Service account e-mail per control-plane workload (api, events, orchestrator, migrate, web, push, url-signer)."
  value       = { for k, sa in google_service_account.sa : k => sa.email }
}

output "urls" {
  description = "Public origins and the run.app URL of every service."
  value = {
    web     = local.web_origin
    plots   = local.plots_origin
    api     = local.api_url
    run_app = local.run_url
  }
}

output "names" {
  description = "Resource names deploy scripts use (services, worker pool, migrate job)."
  value       = local.names
}

output "services_deployed" {
  description = "Whether the Cloud Run workloads are part of this configuration (images were given)."
  value       = local.services_ready
}

output "lb_ip" {
  description = "IPv4 address to point both domains at (null without a load balancer)."
  value       = one(google_compute_global_address.lb[*].address)
}

output "database" {
  description = "Cloud SQL instance (no credentials)."
  value = {
    instance        = google_sql_database_instance.main.name
    connection_name = google_sql_database_instance.main.connection_name
    private_ip      = google_sql_database_instance.main.private_ip_address
    database        = google_sql_database.mapi.name
    users           = [for u in google_sql_user.role : u.name]
  }
}

output "secrets" {
  description = "Secret Manager secret ids per purpose (values are never output)."
  value       = { for k, s in google_secret_manager_secret.this : k => s.secret_id }
}

output "kms_signing_key" {
  description = "The bundle-signing key (asymmetric, EC P-256)."
  value       = google_kms_crypto_key.bundle_signing.id
}

output "events" {
  description = "Event transport resources."
  value = {
    topic             = google_pubsub_topic.run_events.id
    push_subscription = google_pubsub_subscription.run_events_push.id
    dead_letter_topic = google_pubsub_topic.run_events_dlq.id
    push_endpoint     = local.push_endpoint
    push_audience     = local.push_audience
    sink_filter       = local.event_log_filter
    sink_writers      = { for k, s in google_logging_project_sink.run_events : k => s.writer_identity }
  }
}

output "network_id" {
  description = "The control VPC."
  value       = google_compute_network.control.id
}
