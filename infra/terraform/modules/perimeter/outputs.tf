output "perimeter" {
  description = "The service perimeter resource name."
  value       = google_access_context_manager_service_perimeter.exec_data.name
}

output "enforced" {
  description = "False while the perimeter is in dry-run (violations logged, not blocked)."
  value       = !var.dry_run
}
