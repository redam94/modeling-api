output "image_repository" {
  description = "Docker repository path: <region>-docker.pkg.dev/<project>/<repo>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "pypi_repository" {
  description = "pip index URL of the curated PyPI remote."
  value       = "https://${var.region}-python.pkg.dev/${var.project_id}/${google_artifact_registry_repository.pypi.repository_id}/simple/"
}

output "attestor" {
  description = "Binary Authorization attestor (stub until M5) and its signing key."
  value = {
    attestor = google_binary_authorization_attestor.ci.id
    key      = google_kms_crypto_key.attestor.id
  }
}

output "github" {
  description = "Values for the GitHub environments' variables (GCP_WIF_PROVIDER, GCP_*_SA). Null without var.github."
  value = var.github == null ? null : {
    workload_identity_provider = google_iam_workload_identity_pool_provider.github[0].name
    service_accounts           = { for k, sa in google_service_account.ci : k => sa.email }
  }
}
