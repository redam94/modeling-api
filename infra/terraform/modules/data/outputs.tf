output "bucket_names" {
  description = "Bucket name per purpose (quarantine, bootstrap, runs, bundles, data)."
  value       = { for k, b in google_storage_bucket.this : k => b.name }
}

output "bootstrap_url_template" {
  description = "MAPI_BOOTSTRAP_URL for the Cloud Run Jobs: read with the execution service account, then deleted."
  value       = "gs://${google_storage_bucket.this["bootstrap"].name}/{nonce}"
}

output "custom_roles" {
  description = "The custom storage roles defined in the data project."
  value = {
    bootstrap_consumer = google_project_iam_custom_role.bootstrap_consumer.id
    object_reader      = google_project_iam_custom_role.object_reader.id
    object_writer      = google_project_iam_custom_role.object_writer.id
  }
}
