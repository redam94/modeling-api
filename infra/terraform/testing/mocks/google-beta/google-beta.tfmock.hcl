# Mock values for `terraform test` (mock_provider "google-beta" { source = ... }).

mock_resource "google_project_service_identity" {
  defaults = {
    email = "service-111111111111@gcp-sa-mock.iam.gserviceaccount.com"
  }
}
