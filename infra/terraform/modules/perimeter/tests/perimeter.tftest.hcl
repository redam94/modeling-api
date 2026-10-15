# VPC-SC perimeter shape, checked on a mocked apply.

mock_provider "google" {
  source = "../../testing/mocks/google"
}

variables {
  access_policy_id       = "123456789012"
  env                    = "test"
  protected_projects     = { data = "200", exec-paid = "300" }
  exec_project_numbers   = { paid = "300" }
  data_project_number    = "200"
  control_project_number = "100"
  build_project_number   = "400"
  control_identities = {
    storage = { api = "api@c.iam.gserviceaccount.com", url-signer = "signer@c.iam.gserviceaccount.com" }
    run     = { orchestrator = "orch@c.iam.gserviceaccount.com" }
  }
  sink_writer_identities = { paid = "serviceAccount:service-300@gcp-sa-logging.iam.gserviceaccount.com" }
}

run "enforced" {
  command = apply

  assert {
    condition     = length(google_access_context_manager_service_perimeter.exec_data.status) == 1 && length(google_access_context_manager_service_perimeter.exec_data.spec) == 0
    error_message = "Enforced: the configuration is in status."
  }

  assert {
    condition     = google_access_context_manager_service_perimeter.exec_data.status[0].resources == toset(["projects/200", "projects/300"])
    error_message = "The perimeter holds the exec and data projects, not the control project."
  }

  assert {
    condition     = contains(google_access_context_manager_service_perimeter.exec_data.status[0].restricted_services, "storage.googleapis.com")
    error_message = "Cloud Storage is restricted."
  }

  assert {
    condition     = google_access_context_manager_service_perimeter.exec_data.status[0].vpc_accessible_services[0].allowed_services == toset(["storage.googleapis.com"])
    error_message = "Executions may call Cloud Storage and nothing else."
  }
}

run "dry_run" {
  command = apply

  variables {
    dry_run = true
  }

  assert {
    condition     = length(google_access_context_manager_service_perimeter.exec_data.spec) == 1 && length(google_access_context_manager_service_perimeter.exec_data.status) == 0
    error_message = "Dry-run: the configuration is only in spec."
  }

  assert {
    condition     = google_access_context_manager_service_perimeter.exec_data.use_explicit_dry_run_spec
    error_message = "Dry-run uses the explicit dry-run spec."
  }
}
